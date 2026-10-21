"""事件总线：模块之间跨边界通信的唯一通道（架构文档第 5 章）。

总线持有一个 asyncio 事件循环（运行在主线程上），并提供下面这些保证：

- 排队投递：``publish`` 与 ``send`` 只把一次分发放进事件循环的就绪队列，然后立即返回。
  处理函数在事件循环的后续轮次中执行，永远不在发布方的调用栈里同步执行。
- 线程：``publish`` 与 ``send`` 可以在任何线程调用，所有处理函数都在循环线程执行。
  ``subscribe`` 只允许在循环线程调用。
- 顺序：同一线程发布的消息按发布顺序分发；同一条消息按订阅顺序交给各处理函数。
- 精确匹配：订阅键是消息类本身，按 ``type(message)`` 查找，不向父类分发。
- 订阅快照：一条消息的接收者在它被分发的那一刻确定。分发过程中新增的订阅收不到这条消息；
  分发过程中被取消的订阅，若还没轮到它，也不会再收到。
- 状态：总线按类保存最新值，每个 State 值发布时得到一个全局递增的版本号。每个订阅者收到的版本号
  严格递增：新订阅者先收到订阅时刻的最新值（回放），之后收到每一个新值；
  比订阅时刻最新值更旧、仍在排队的值不再交给它。

异常处理分两种模式：

- 默认模式：处理函数抛出的异常用 ``logger.exception`` 记录，然后继续交给其余处理函数。
- ``raise_errors=True``（开发与测试用）：记录方式不变，此外
  同步处理函数的异常在这条消息交给全部处理函数之后，从分发回调中重新抛出，
  由事件循环交给它的异常处理器（``loop.call_exception_handler``）；
  协程处理函数的异常被收集起来，在下一次 ``drain()`` 时以 ``ExceptionGroup`` 抛出；
  ``send`` 发现命令处理者数量不是 1 时直接抛出 ``RuntimeError``。

总线内部有一把很短的线程锁，保护订阅表、状态最新值和待分发计数。处理函数从不在锁内执行。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import cast

from lst.contracts.messages import Command, Event, Message, State

# 总线内部保存处理函数时擦除参数类型；按精确类型分发保证传入的消息就是订阅时的 kind。
type _Handler = Callable[..., Awaitable[None] | None]

# 非 State 消息不参与版本比较，用 0 表示。
_NO_VERSION = 0


def _describe(handler: _Handler) -> str:
    """返回处理函数在日志里的名字。"""
    name = getattr(handler, "__qualname__", None)
    return name if isinstance(name, str) else repr(handler)


class Subscription:
    """一次订阅的句柄，由 ``EventBus.subscribe`` 创建。

    ``cancel()`` 幂等，可以在任何线程调用，调用后立即生效：
    已经排队但还没交给这个订阅者的消息（包括 State 回放）不再投递给它。
    """

    __slots__ = ("_active", "_bus", "_handler", "_kind", "_state_version")

    def __init__(self, bus: EventBus, kind: type[Message], handler: _Handler) -> None:
        self._bus = bus
        self._kind = kind
        self._handler = handler
        self._active = True
        # 交给这个订阅者的 State 版本号下限：只投递比它大的版本，投递后更新为该版本。
        # 回放与正常分发都检查它，同一个状态值因此不重复投递，旧值也不会排在新值之后到达。
        self._state_version = _NO_VERSION

    @property
    def kind(self) -> type[Message]:
        """订阅的消息类。"""
        return self._kind

    @property
    def active(self) -> bool:
        """订阅尚未取消时为 True。"""
        return self._active

    def cancel(self) -> None:
        """取消订阅。重复调用没有效果。"""
        self._bus._cancel(self)

    def __repr__(self) -> str:
        state = "active" if self._active else "cancelled"
        return f"<Subscription {self._kind.__name__} -> {_describe(self._handler)} ({state})>"


class EventBus:
    """进程内事件总线。

    ``loop`` 是处理函数运行的事件循环；``logger`` 缺省时使用本模块的日志器；
    ``raise_errors`` 的含义见模块文档。
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        logger: logging.Logger | None = None,
        raise_errors: bool = False,
    ) -> None:
        self._loop = loop
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._raise_errors = raise_errors
        self._lock = threading.Lock()
        self._subscribers: dict[type[Message], list[Subscription]] = {}
        # 每个 State 类的最新值及其版本号。版本号在发布时全局递增分配。
        self._latest: dict[type[Message], tuple[int, State]] = {}
        self._state_version = _NO_VERSION
        # 已经放进事件循环、还没执行的分发回调个数，drain() 用它判断队列是否清空。
        self._pending = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._errors: list[Exception] = []
        self._closed = False

    # ------------------------------------------------------------------ 公开接口

    def subscribe[M: Message](
        self, kind: type[M], handler: Callable[[M], Awaitable[None] | None]
    ) -> Subscription:
        """订阅 ``kind`` 类的消息，返回订阅句柄。

        只能在循环线程调用。``kind`` 是 State 类且总线已有它的最新值时，
        订阅这一刻的最新值会排队投递一次给这个新订阅者，其他订阅者不受影响。
        """
        if not self._on_loop_thread():
            raise RuntimeError("EventBus.subscribe 只能在事件循环线程调用")
        sub = Subscription(self, kind, handler)
        with self._lock:
            if self._closed:
                raise RuntimeError("事件总线已关闭，不能再订阅")
            self._subscribers.setdefault(kind, []).append(sub)
            entry = self._latest.get(kind)
            if entry is not None:
                version, value = entry
                # 比订阅时最新值更旧、还在排队的分发不再交给新订阅者。
                sub._state_version = version - 1
                # 回放的值在订阅时取定，并且和发布一样在锁内入队：更早的分发都排在回放之前，
                # 之后发布的值都排在回放之后，订阅者因此不会漏掉订阅之后发布的任何一个值。
                self._pending += 1
                self._loop.call_soon(self._replay, sub, version, value)
        return sub

    def publish(self, message: Event | State) -> None:
        """发布事件或状态。可以在任何线程调用，立即返回。"""
        if isinstance(message, Command):
            raise TypeError(f"{type(message).__name__} 是命令，应当用 EventBus.send 发送")
        if not isinstance(message, Event | State):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"publish 只接受 Event 或 State，收到 {type(message).__name__}")
        self._post(message)

    def send(self, command: Command) -> None:
        """发送命令。可以在任何线程调用，立即返回。

        命令应当恰好有一个处理者。数量不对时记录 ERROR 日志：没有处理者则不投递，
        多于一个则仍然投递给全部处理者。``raise_errors=True`` 时改为抛出 ``RuntimeError``，
        并且不投递。
        """
        if not isinstance(command, Command):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"send 只接受 Command，收到 {type(command).__name__}")
        kind = type(command)
        with self._lock:
            closed = self._closed
            count = len(self._subscribers.get(kind, ()))
        if closed:
            self._logger.debug("事件总线已关闭，丢弃命令 %s", kind.__name__)
            return
        if count != 1:
            template = "命令 %s 应当恰好有 1 个处理者，实际有 %d 个"
            if self._raise_errors:
                raise RuntimeError(template % (kind.__name__, count))
            self._logger.error(template, kind.__name__, count)
            if count == 0:
                return
        self._post(command)

    def latest[S: State](self, kind: type[S]) -> S | None:
        """返回 ``kind`` 状态类最近一次发布的值，从未发布过时返回 None。

        最新值在 ``publish`` 调用时就已更新，不必等到分发完成。
        """
        with self._lock:
            entry = self._latest.get(kind)
        if entry is None:
            return None
        # 最新值表按精确类型存放，取出的值一定是 kind 的实例。
        return cast(S, entry[1])

    async def drain(self) -> None:
        """等待队列中的分发全部执行完、所有协程处理函数结束。

        处理函数在此期间发布的新消息也会被等待。``raise_errors=True`` 时，
        若有协程处理函数抛出过异常，等待结束后把它们全部打包成一个 ``ExceptionGroup`` 抛出
        （只有一个异常时也打包），并清空收集到的异常。同步处理函数的异常已经抛给了事件循环，
        不在这里重复抛出。不能在总线的协程处理函数里调用。
        """
        current = asyncio.current_task()
        if current is not None and current in self._tasks:
            raise RuntimeError("不能在总线的协程处理函数里调用 drain()，它会等待自己结束")
        while True:
            tasks = set(self._tasks)
            if tasks:
                await asyncio.wait(tasks)
                continue
            with self._lock:
                pending = self._pending
            if pending == 0:
                break
            # 分发回调已经在就绪队列里，让出一轮循环让它们执行。
            await asyncio.sleep(0)
        if self._errors:
            errors, self._errors = self._errors, []
            raise ExceptionGroup("事件总线的协程处理函数抛出了异常", errors)

    async def aclose(self) -> None:
        """关闭总线，取消尚未结束的处理任务并等待它们退出。重复调用没有效果。

        关闭后，已排队的分发不再执行，新的 ``publish``/``send`` 被丢弃，``subscribe`` 抛出 ``RuntimeError``。
        """
        with self._lock:
            self._closed = True
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ 内部实现

    def _on_loop_thread(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def _post(self, message: Message) -> None:
        """把一次分发放进事件循环。总线已关闭时丢弃消息。"""
        on_loop_thread = self._on_loop_thread()
        with self._lock:
            closed = self._closed
            if not closed:
                self._enqueue_locked(message, on_loop_thread)
        if closed:
            self._logger.debug("事件总线已关闭，丢弃消息 %s", type(message).__name__)

    def _enqueue_locked(self, message: Message, on_loop_thread: bool) -> None:
        """在持有锁时调用：分配版本号、入队分发回调、更新最新值。"""
        version = _NO_VERSION
        if isinstance(message, State):
            self._state_version += 1
            version = self._state_version
        # 入队也在锁内完成，State 的分发顺序因此与版本号顺序一致。
        if on_loop_thread:
            self._loop.call_soon(self._dispatch, message, version)
        else:
            self._loop.call_soon_threadsafe(self._dispatch, message, version)
        self._pending += 1
        if isinstance(message, State):
            self._latest[type(message)] = (version, message)

    def _dispatch(self, message: Message, version: int) -> None:
        """在循环线程上把一条消息交给分发时刻的全部订阅者。"""
        with self._lock:
            self._pending -= 1
            if self._closed:
                return
            subs = tuple(self._subscribers.get(type(message), ()))
        self._deliver(message, version, subs)

    def _replay(self, sub: Subscription, version: int, value: State) -> None:
        """在循环线程上把订阅时的 State 最新值回放给这个新订阅者。"""
        with self._lock:
            self._pending -= 1
            if self._closed:
                return
        self._deliver(value, version, (sub,))

    def _deliver(self, message: Message, version: int, subs: tuple[Subscription, ...]) -> None:
        first_error: Exception | None = None
        for sub in subs:
            # 每次交付前都检查，前面的处理函数可能刚刚取消了后面的订阅。
            if not sub._active:
                continue
            if version != _NO_VERSION:
                if sub._state_version >= version:
                    continue
                sub._state_version = version
            error = self._invoke(sub._handler, message)
            if first_error is None:
                first_error = error
        if first_error is not None and self._raise_errors:
            raise first_error

    def _invoke(self, handler: _Handler, message: Message) -> Exception | None:
        """调用一个处理函数，返回它同步抛出的异常。返回值可等待时包装成任务跟踪。"""
        try:
            result = handler(message)
        except Exception as exc:
            self._log_handler_error(message, handler)
            return exc
        if inspect.isawaitable(result):
            task = self._loop.create_task(
                self._run_async(result, message, handler),
                name=f"bus:{type(message).__name__}:{_describe(handler)}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return None

    async def _run_async(self, awaitable: Awaitable[None], message: Message, handler: _Handler) -> None:
        try:
            await awaitable
        except Exception as exc:
            self._log_handler_error(message, handler)
            if self._raise_errors:
                self._errors.append(exc)

    def _log_handler_error(self, message: Message, handler: _Handler) -> None:
        """在 except 块内调用，用 logger.exception 记录异常和完整堆栈。"""
        self._logger.exception("处理消息 %s 时出错，处理函数 %s", type(message).__name__, _describe(handler))

    def _cancel(self, sub: Subscription) -> None:
        with self._lock:
            if not sub._active:
                return
            sub._active = False
            subs = self._subscribers.get(sub._kind)
            if subs is not None:
                subs.remove(sub)
                if not subs:
                    del self._subscribers[sub._kind]
