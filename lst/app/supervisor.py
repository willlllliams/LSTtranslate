"""推理进程监管器（架构文档 2.8、8.1、8.2 节）。

``WorkerSupervisor`` 为一个推理进程实现 ``WorkerChannel`` 端口：启动子进程并握手，转发请求与回复，
检测崩溃和卡死，按退避规则重启，以及停止和重载。

线程模型
========

监管器的全部状态只在事件循环线程上读写，业务代码不需要加锁。可能阻塞的调用都放在后台线程里，
结果通过 ``loop.call_soon_threadsafe`` 交回事件循环（需求 REL-04）：

- 创建命名管道、``subprocess.Popen``、写 authkey、加入作业对象：在线程池里执行（``asyncio.to_thread``）；
- 每个子进程配一组专用线程：accept、读消息、写消息、读标准错误、读标准输出、等待进程退出；
- ``Listener.accept`` 没有超时参数，关闭 Listener 也不能让 Windows 上阻塞中的 accept 返回
  （它等待的管道实例已经从 Listener 的队列里取出）。需要放弃 accept 时，另起一个短命线程以客户端身份
  连接管道再立即断开，accept 随即因认证失败返回。

事件循环线程上只做消息分发、队列维护和计时器，每一步都在微秒级。

使用约定：``submit``、``cancel``、``retry`` 以及各个 ``async`` 方法都只能在事件循环线程上调用。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from multiprocessing.connection import Listener
from types import MappingProxyType
from typing import IO, Final, Protocol

from lst.app import jobobject
from lst.contracts.errors import ErrorCode
from lst.contracts.ports import WorkerState
from lst.contracts.worker_protocol import (
    CANCELLED,
    PROTOCOL_VERSION,
    Cancel,
    Configure,
    ExitCode,
    FrameConnection,
    Hello,
    JsonValue,
    OcrRequest,
    OcrResult,
    Progress,
    ProtocolError,
    Ready,
    RequestDone,
    RequestFailed,
    SegmentResult,
    Shutdown,
    Started,
    TranslateRequest,
    WorkerMessage,
    WorkerReply,
    recv_message,
    send_message,
)

__all__ = ["SupervisedRequest", "SupervisorTimings", "WorkerSupervisor"]

_NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,32}")
_CREATION_FLAGS: Final = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
# 子进程的环境变量默认值：启动即启用 faulthandler（导入推理库时的崩溃也能留下堆栈），标准输出与错误用 UTF-8。
# 只对源码版生效：安装版用 -I 启动子进程，解释器忽略全部 PYTHON* 环境变量。安装版的编码由命令行的 -X utf8
# 保证，faulthandler 由进程入口的 _runtime.setup_process 启用，只是覆盖不到 setup_process 之前的导入阶段。
_CHILD_ENV_DEFAULTS: Final = {"PYTHONFAULTHANDLER": "1", "PYTHONIOENCODING": "utf-8"}
_TERMINAL_REPLIES: Final = (RequestDone, RequestFailed, OcrResult)
_REPLY_TYPES: Final = (Started, SegmentResult, OcrResult, RequestDone, RequestFailed)
# 子进程输出的一行写进日志时的长度上限。
_MAX_LOG_LINE: Final = 4000
# 放弃 accept 时反复连接管道的间隔与总时长。
_POKE_INTERVAL_S: Final = 0.02
_POKE_DEADLINE_S: Final = 5.0
# 强制结束后等待进程句柄变为已退出的上限。TerminateProcess 之后通常在毫秒内完成。
_KILL_WAIT_S: Final = 5.0


@dataclass(frozen=True, slots=True)
class SupervisorTimings:
    """监管器的各项时限，单位为秒。默认值取自架构文档 8.2 节，测试用很小的值。"""

    accept_timeout_s: float = 30.0
    """子进程启动后连接管道的时限。超时按崩溃处理。"""
    ready_timeout_s: float = 300.0
    """发出 Configure 后等待 Ready 的时限。模型加载可能很久。超时按崩溃处理。"""
    shutdown_grace_s: float = 2.0
    """stop() 发出 Shutdown 后等待进程退出的总时长，之后强制结束（8.2 节第 8 条）。"""
    reload_grace_s: float = 3.0
    """reload() 发出 Shutdown 后等待进程退出的总时长，之后强制结束（8.2 节第 7 条）。"""
    backoff_s: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
    """崩溃后重启前的等待时间。窗口内第 n 次崩溃取第 n 项，超出长度时取最后一项。"""
    crash_window_s: float = 600.0
    """统计崩溃次数的时间窗口。"""
    max_crashes: int = 5
    """窗口内第几次崩溃进入 FAILED。"""
    io_drain_s: float = 1.0
    """进程退出后，等待读线程交完剩余消息、标准错误读完的上限。"""
    pipe_closed_grace_s: float = 1.0
    """管道断开而进程还在时，等它自行退出的时长，之后强制结束。"""
    stop_timeout_s: float = 10.0
    """stop() 结束子进程之后等待监管任务收尾的上限。超时记 ERROR 并放弃等待（取消监管任务）。
    默认值覆盖强制结束后的等待（5 秒）与读线程交完消息的等待（io_drain_s），再留出余量。"""


class _Termination(Enum):
    """监管器主动结束子进程的原因。进程自行退出时没有这一项。"""

    STOP = auto()
    RELOAD = auto()
    ACCEPT_TIMEOUT = auto()
    READY_TIMEOUT = auto()
    PROTOCOL_MISMATCH = auto()
    PROTOCOL_ERROR = auto()
    PIPE_CLOSED = auto()
    REQUEST_TIMEOUT = auto()
    INTERNAL_ERROR = auto()


class _Outcome(Enum):
    """一个子进程结束后，生命周期循环的下一步。"""

    STOPPED = auto()  # stop() 发起的退出
    RESTART = auto()  # reload() 发起的退出：立即用新配置启动，不计崩溃
    CRASHED = auto()  # 计一次崩溃，退避后重启
    FAILED = auto()  # 确定性故障，不再重启


class _Phase(Enum):
    QUEUED = auto()  # 在监管器里排队
    SENT = auto()  # 已交给子进程
    DONE = auto()  # 已产出结束回复


class _PipeEnd(FrameConnection, Protocol):
    """管道连接中监管器用到的部分。

    Windows 上 Listener.accept 返回 PipeConnection，其他平台返回 Connection，两者都满足这个协议。
    """

    def close(self) -> None: ...


def _post[*Ts](loop: asyncio.AbstractEventLoop, callback: Callable[[*Ts], object], *args: *Ts) -> None:
    """从后台线程把回调交给事件循环。事件循环已关闭时丢弃。"""
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(callback, *args)


def _resolve[T](future: asyncio.Future[T], value: T) -> None:
    if not future.done():
        future.set_result(value)


def _describe_exit(code: int) -> str:
    """把退出码写成便于查阅的形式。Windows 的异常退出码（例如访问违例 0xC0000005）附十六进制。"""
    if code < 0 or code >= 0x8000_0000:
        return f"退出码 {code}（0x{code & 0xFFFF_FFFF:08X}）"
    return f"退出码 {code}"


def _deterministic_exit(code: int) -> ExitCode | None:
    try:
        exit_code = ExitCode(code)
    except ValueError:
        return None
    return exit_code if exit_code.deterministic else None


class SupervisedRequest:
    """一个已提交的请求，实现 ``WorkerRequestHandle``。

    回复先进入一个无界队列，消费者用 ``replies()`` 按到达顺序取出；队列保证消费者开始迭代之前
    到达的回复不会丢失。``replies()`` 只支持一个消费者。
    """

    def __init__(
        self,
        owner: WorkerSupervisor,
        message: TranslateRequest | OcrRequest,
        payload: bytes | None,
        timeout_s: float,
    ) -> None:
        self._owner = owner
        self.message = message
        self.payload = payload
        self.timeout_s = timeout_s
        self.phase = _Phase.QUEUED
        self.process: _Process | None = None
        self.started = False
        self.cancel_requested = False
        self.timer: asyncio.TimerHandle | None = None
        self._inbox: asyncio.Queue[WorkerReply] = asyncio.Queue()
        self._drained = False

    @property
    def request_id(self) -> str:
        return self.message.request_id

    @property
    def done(self) -> bool:
        """已经产出结束回复（RequestDone、RequestFailed 或 OcrResult）。"""
        return self.phase is _Phase.DONE

    async def replies(self) -> AsyncIterator[WorkerReply]:
        """按到达顺序产出回复，以 RequestDone、RequestFailed 或 OcrResult 结束。"""
        while not self._drained:
            reply = await self._inbox.get()
            if isinstance(reply, _TERMINAL_REPLIES):
                self._drained = True
            yield reply

    def cancel(self) -> None:
        """取消请求。还在监管器排队的直接以 cancelled 结束；已发给子进程的发送 Cancel。可以重复调用。"""
        self._owner._cancel(self)

    def deliver(self, reply: WorkerReply) -> None:
        self._inbox.put_nowait(reply)


class _Process:
    """一次启动的子进程，以及为它服务的线程。

    future 与标志位只在事件循环线程上读写；各线程只通过 ``_post`` 回报结果。
    """

    def __init__(
        self,
        *,
        popen: subprocess.Popen[bytes],
        listener: Listener,
        address: str,
        label: str,
        loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
        on_message: Callable[[_Process, WorkerMessage], None],
        on_reader_closed: Callable[[_Process, ProtocolError | None], None],
    ) -> None:
        self.popen = popen
        self.pid = popen.pid
        self.address = address
        self.label = label
        self._listener = listener
        self._loop = loop
        self._logger = logger
        self._on_message = on_message
        self._on_reader_closed = on_reader_closed

        self.exited: asyncio.Future[int] = loop.create_future()
        self.accepted: asyncio.Future[_PipeEnd | None] = loop.create_future()
        self.ready: asyncio.Future[Ready] = loop.create_future()
        self.reader_done: asyncio.Future[None] = loop.create_future()
        self.stderr_done: asyncio.Future[None] = loop.create_future()
        self.conn: _PipeEnd | None = None
        # 进入 READY 之后、出现任何退出迹象之前为 True。只有这段时间把新请求直接发给子进程。
        self.serving = False
        self.termination: _Termination | None = None
        self.kill_timer: asyncio.TimerHandle | None = None

        self._outbox: queue.SimpleQueue[tuple[WorkerMessage, bytes | None] | None] = queue.SimpleQueue()
        self._accept_abort = threading.Event()
        self._accept_finished = threading.Event()
        self._writer: threading.Thread | None = None

    # ---- 启动与连接（事件循环线程）

    def start_threads(self) -> None:
        """启动等待退出、读标准错误与标准输出、accept 四个线程。"""
        stdout, stderr = self.popen.stdout, self.popen.stderr
        assert stdout is not None and stderr is not None
        self._thread("wait", self._wait_main)
        self._thread("stderr", lambda: self._pump_main(stderr, logging.WARNING, "stderr", self.stderr_done))
        self._thread("stdout", lambda: self._pump_main(stdout, logging.DEBUG, "stdout", None))
        self._thread("accept", self._accept_main)

    def abort_accept(self) -> None:
        """放弃 accept：标记放弃，并反复连接管道让阻塞中的 accept 返回。重复调用没有效果。"""
        if self._accept_abort.is_set():
            return
        self._accept_abort.set()
        _resolve(self.accepted, None)
        if not self._accept_finished.is_set():
            self._thread("poke", self._poke_main)

    def attach(self, conn: _PipeEnd) -> None:
        """连接建立后启动读、写两个线程。"""
        self.conn = conn
        self._writer = self._thread("writer", lambda: self._writer_main(conn))
        self._thread("reader", lambda: self._reader_main(conn))

    def send(self, message: WorkerMessage, payload: bytes | None = None) -> None:
        """把消息交给写线程。连接建立之前调用时丢弃。"""
        if self.conn is not None:
            self._outbox.put((message, payload))

    def kill(self, reason: _Termination) -> None:
        """立即结束子进程（TerminateProcess 不阻塞），退出由等待线程报告。

        进程已经退出时只停止接收请求，不记录结束原因，退出码照常按进程自己的退出归类。
        """
        self.serving = False
        if self.exited.done():
            return
        if self.termination is None:
            self.termination = reason
        try:
            self.popen.kill()
        except OSError as exc:
            self._logger.warning("结束推理进程 %s 失败：%s", self.label, exc)

    def io_futures(self) -> list[asyncio.Future[None]]:
        """进程退出后需要等它们完成的 future：读线程交完消息、标准错误读完。"""
        futures = [self.stderr_done]
        if self.conn is not None:
            futures.append(self.reader_done)
        return futures

    def _thread(self, role: str, target: Callable[[], None]) -> threading.Thread:
        thread = threading.Thread(target=target, name=f"lst-{self.label}-{role}", daemon=True)
        thread.start()
        return thread

    # ---- 以下方法在后台线程上执行

    def _wait_main(self) -> None:
        code = self.popen.wait()
        _post(self._loop, _resolve, self.exited, code)

    def _pump_main(
        self, stream: IO[bytes], level: int, channel: str, done: asyncio.Future[None] | None
    ) -> None:
        """逐行读取子进程的输出写进日志，同时防止管道写满后子进程阻塞。"""
        try:
            with stream:
                for raw in iter(stream.readline, b""):
                    text = raw.decode("utf-8", "replace").rstrip()
                    if text:
                        self._logger.log(level, "[%s %s] %s", self.label, channel, text[:_MAX_LOG_LINE])
        except (OSError, ValueError):
            pass
        finally:
            if done is not None:
                _post(self._loop, _resolve, done, None)

    def _accept_main(self) -> None:
        conn: _PipeEnd | None = None
        try:
            while not self._accept_abort.is_set():
                try:
                    conn = self._listener.accept()
                except Exception as exc:
                    if self._accept_abort.is_set():
                        break
                    # 未通过认证的连接（例如别的程序连上了这个管道）不影响等待真正的子进程。
                    self._logger.warning("推理进程 %s 的管道拒绝了一个连接：%r", self.label, exc)
                    continue
                break
        finally:
            self._accept_finished.set()
            with contextlib.suppress(OSError):
                self._listener.close()
            _post(self._loop, self._on_accept_result, conn)

    def _poke_main(self) -> None:
        deadline = time.monotonic() + _POKE_DEADLINE_S
        while not self._accept_finished.wait(_POKE_INTERVAL_S):
            if time.monotonic() > deadline:
                self._logger.warning("推理进程 %s 的 accept 线程没有按时结束", self.label)
                return
            # Listener 同时持有两个管道实例，第一次连接可能落在没有被等待的那个上，所以反复连接。
            with contextlib.suppress(OSError), open(self.address, "r+b", buffering=0):
                pass

    def _reader_main(self, conn: _PipeEnd) -> None:
        error: ProtocolError | None = None
        try:
            while True:
                message, _ = recv_message(conn)
                _post(self._loop, self._on_message, self, message)
        except (EOFError, OSError):
            pass
        except ProtocolError as exc:
            error = exc
        # 先通知事件循环（它可能需要结束子进程，写线程才能从阻塞的发送中返回），再收尾。
        _post(self._loop, self._on_reader_closed, self, error)
        self._outbox.put(None)
        if self._writer is not None:
            self._writer.join()
        with contextlib.suppress(OSError):
            conn.close()
        # 同一线程先后提交的回调按顺序执行，这条执行时，读到的消息都已交给事件循环。
        _post(self._loop, _resolve, self.reader_done, None)

    def _writer_main(self, conn: _PipeEnd) -> None:
        while True:
            item = self._outbox.get()
            if item is None:
                return
            message, payload = item
            try:
                send_message(conn, message, payload)
            except (OSError, ValueError) as exc:
                self._logger.debug("向推理进程 %s 发送 %s 失败：%r", self.label, message.TYPE, exc)
                return

    # ---- 事件循环线程

    def _on_accept_result(self, conn: _PipeEnd | None) -> None:
        if conn is not None and (self._accept_abort.is_set() or self.accepted.done()):
            conn.close()
            conn = None
        _resolve(self.accepted, conn)


class WorkerSupervisor:
    """一个推理进程的监管器，实现 ``WorkerChannel`` 端口（架构文档 8.2 节）。

    状态机：

    - ``start()``：STOPPED → STARTING → LOADING → READY；有在途请求时为 BUSY；
    - 子进程非预期退出、连接或就绪超时、请求处理超时：→ CRASHED，退避后重新 STARTING；
      窗口内第 ``max_crashes`` 次崩溃 → FAILED；
    - 确定性故障（ExitCode 中的 64、65、66，或 Ready 的协议版本不符）：直接 FAILED；
    - ``stop()``：→ STOPPED；``reload()``：结束当前进程后用新配置重新 STARTING；
    - ``retry()``：FAILED 时清零崩溃计数并重新 STARTING。

    启动命令可以在构造时给出，也可以留到第一次 ``reload(command=…)`` 给出（OCR 进程的命令随引擎而定）；
    还没有命令时 ``start()``、``retry()`` 与不带命令的 ``reload()`` 抛 RuntimeError。

    ``start()`` 只启动监管任务，不等待子进程就绪：模型加载可能需要几分钟，期间提交的请求在监管器里排队
    （需求 OCR-07），就绪后按提交顺序发出。需要等待某个状态时用 ``wait_for_state``。
    """

    def __init__(
        self,
        name: str,
        command: Sequence[str] | None,
        *,
        settings: Mapping[str, JsonValue],
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        on_state: Callable[[WorkerState, str], None] | None = None,
        on_progress: Callable[[Progress], None] | None = None,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timings: SupervisorTimings | None = None,
    ) -> None:
        """
        参数：
            name：进程名，用于管道地址、线程名和日志，只能包含字母、数字、下划线和连字符。
            command：启动命令（解释器路径加参数），监管器在末尾追加 ``--address <管道地址>``；
                None 表示命令由第一次 ``reload(command=…)`` 给出。
            settings：Configure 消息的内容。
            cwd、env：子进程的工作目录与额外的环境变量（叠加在当前进程的环境变量上）。
            on_state：状态变化回调 ``on_state(state, detail)``，在事件循环线程上同步调用；
                detail 是给日志和界面的说明，例如崩溃原因。回调里不要同步调用监管器的方法。
            on_progress：子进程发来 Progress 时调用。
            clock、sleep：计算崩溃窗口的时钟与退避等待函数，测试用来跳过真实等待。
            timings：各项时限。
        """
        if not _NAME_PATTERN.fullmatch(name):
            raise ValueError(f"推理进程名只能包含字母、数字、下划线和连字符：{name!r}")
        if command is not None and not command:
            raise ValueError("启动命令不能为空")
        self._name = name
        self._command: tuple[str, ...] | None = None if command is None else tuple(command)
        self._settings: dict[str, JsonValue] = dict(settings)
        self._cwd = cwd
        self._env = dict(env) if env is not None else {}
        self._on_state = on_state
        self._on_progress = on_progress
        self._logger = logger if logger is not None else logging.getLogger(f"{__name__}.{name}")
        self._clock = clock
        self._sleep = sleep
        self._timings = timings if timings is not None else SupervisorTimings()

        self._state = WorkerState.STOPPED
        self._detail = ""
        self._capabilities: dict[str, JsonValue] | None = None
        self._pending: deque[SupervisedRequest] = deque()
        self._inflight: dict[str, SupervisedRequest] = {}
        self._proc: _Process | None = None
        self._lifecycle: asyncio.Task[None] | None = None
        self._backoff_task: asyncio.Future[None] | None = None
        self._stopping = False
        self._crash_times: deque[float] = deque()
        self._generation = 0
        self._last_exit_code: int | None = None
        self._control = asyncio.Lock()
        self._state_waiters: list[tuple[frozenset[WorkerState], asyncio.Future[WorkerState]]] = []

    # ---------------------------------------------------------------- 查询

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def state_detail(self) -> str:
        """最近一次状态变化的说明，例如崩溃原因或进入 FAILED 的原因。"""
        return self._detail

    @property
    def capabilities(self) -> Mapping[str, JsonValue] | None:
        """最近一次 Ready 消息里的能力清单；子进程从未就绪时为 None。"""
        return None if self._capabilities is None else MappingProxyType(self._capabilities)

    @property
    def pid(self) -> int | None:
        """当前子进程的进程号；没有子进程时为 None。"""
        return None if self._proc is None else self._proc.pid

    @property
    def last_exit_code(self) -> int | None:
        """最近一个已退出的子进程的退出码。"""
        return self._last_exit_code

    @property
    def recent_crashes(self) -> int:
        """崩溃窗口内的崩溃次数。"""
        self._prune_crashes(self._clock())
        return len(self._crash_times)

    async def wait_for_state(self, *states: WorkerState) -> WorkerState:
        """等到状态变为 states 之一，返回该状态；当前已经是其中之一时立即返回。"""
        if self._state in states:
            return self._state
        future: asyncio.Future[WorkerState] = asyncio.get_running_loop().create_future()
        self._state_waiters.append((frozenset(states), future))
        return await future

    # ---------------------------------------------------------------- 控制

    async def start(self) -> None:
        """启动监管任务，状态变为 STARTING 后返回。已在运行时没有效果；FAILED 时等同于 ``retry()``。"""
        async with self._control:
            if not self._running():
                self._crash_times.clear()
                self._launch()

    async def stop(self) -> None:
        """停止子进程：发送 Shutdown，总共等待 ``shutdown_grace_s``，仍未退出就强制结束。

        所有未结束的请求以 RequestFailed(cancelled) 结束，状态变为 STOPPED。重复调用没有效果。
        """
        async with self._control:
            task = self._lifecycle
            if task is not None and not task.done():
                self._stopping = True
                if self._backoff_task is not None:
                    self._backoff_task.cancel()
                proc = self._proc
                if proc is not None:
                    await self._terminate(proc, self._timings.shutdown_grace_s, _Termination.STOP)
                # 子进程已退出或退避已取消，监管任务随即结束。
                # 用 wait 而不是 await：任务自身的异常不传给 stop() 的调用方。
                done, _ = await asyncio.wait({task}, timeout=self._timings.stop_timeout_s)
                if not done:
                    await self._abandon(task)
            self._lifecycle = None
            self._stopping = False
            self._fail_all(CANCELLED, "推理进程已停止")
            self._set_state(WorkerState.STOPPED)

    async def reload(
        self, settings: Mapping[str, JsonValue], *, command: Sequence[str] | None = None
    ) -> None:
        """用新配置重启子进程（需求 CFG-03，架构文档 8.2 节第 7 条），并清零崩溃计数。

        做法是结束当前进程再启动新进程：发送 Shutdown，等待 ``reload_grace_s``，仍未退出就强制结束。
        已发给旧进程的请求以 cancelled 结束；还在监管器排队的请求保留，新进程就绪后发出。
        未运行（STOPPED、FAILED）时直接启动。返回时旧进程已经退出，新进程正在启动。
        command 不为 None 时，新进程改用这条启动命令（切换 OCR 引擎时换解释器与后端）。

        重载落在新进程正在创建（``_spawn`` 在线程里执行）的窗口里时，那个进程用的是创建时取下的旧命令；
        ``_run_process`` 在创建返回后核对命令，不同就按 RELOAD 结束它，再用新命令启动。
        """
        if command is not None and not command:
            raise ValueError("启动命令不能为空")
        async with self._control:
            self._settings = dict(settings)
            if command is not None:
                self._command = tuple(command)
            self._crash_times.clear()
            if not self._running():
                self._launch()
                return
            if self._backoff_task is not None:
                # 正在退避等待：取消等待，立即用新配置启动。
                self._backoff_task.cancel()
                return
            proc = self._proc
            if proc is not None:
                await self._terminate(proc, self._timings.reload_grace_s, _Termination.RELOAD)
            # proc 为 None 说明新进程正在创建，还没有发送 Configure：配置在握手时读取，会用到新配置；
            # 命令变了时由 _run_process 在创建返回后核对并重启。

    def retry(self) -> None:
        """从 FAILED 重新开始：清零崩溃计数后启动。其他状态下没有效果。"""
        if self._state is WorkerState.FAILED and not self._running():
            self._crash_times.clear()
            self._launch()

    # ---------------------------------------------------------------- 请求

    def submit(
        self, request: TranslateRequest | OcrRequest, *, payload: bytes | None = None, timeout_s: float
    ) -> SupervisedRequest:
        """提交请求。

        子进程就绪时立即交给写线程；STARTING、LOADING、CRASHED 期间在监管器里排队，就绪后按提交顺序发出。
        FAILED 或未启动时，返回的请求已经以 RequestFailed(engine.worker_crashed) 结束；
        stop() 进行中提交的请求以 cancelled 结束。

        timeout_s 是子进程处理该请求的时限，从收到 Started 时开始计时；排队时间不计入。
        """
        if (payload is not None) != request.HAS_PAYLOAD:
            raise ValueError(f"{request.TYPE} 请求{'需要' if request.HAS_PAYLOAD else '不能带'}原始字节帧")
        if not timeout_s > 0:
            raise ValueError(f"处理时限必须为正数，收到 {timeout_s}")
        request_id = request.request_id
        if request_id in self._inflight or any(r.request_id == request_id for r in self._pending):
            raise ValueError(f"请求号 {request_id} 已有未结束的请求")
        req = SupervisedRequest(self, request, payload, timeout_s)
        if self._state is WorkerState.FAILED:
            self._fail(req, ErrorCode.WORKER_CRASHED, f"推理进程已停用：{self._detail}")
        elif self._stopping:
            self._fail(req, CANCELLED, "推理进程正在停止")
        elif not self._running():
            self._fail(req, ErrorCode.WORKER_CRASHED, "推理进程未运行")
        else:
            proc = self._proc
            if proc is not None and proc.serving and not self._pending:
                self._dispatch(proc, req)
                self._refresh_busy()
            else:
                self._pending.append(req)
        return req

    def _cancel(self, req: SupervisedRequest) -> None:
        if req.phase is _Phase.DONE or req.cancel_requested:
            return
        req.cancel_requested = True
        if req.phase is _Phase.QUEUED:
            self._fail(req, CANCELLED, "请求在发给推理进程之前被取消")
            return
        proc = req.process
        if proc is not None and not proc.exited.done():
            proc.send(Cancel(req.request_id))

    def _dispatch(self, proc: _Process, req: SupervisedRequest) -> None:
        req.phase = _Phase.SENT
        req.process = proc
        self._inflight[req.request_id] = req
        proc.send(req.message, req.payload)
        # 像素帧由写线程的队列持有，发完即释放，请求对象不再保留一份。
        req.payload = None

    def _flush_pending(self, proc: _Process) -> None:
        while self._pending and proc.serving:
            self._dispatch(proc, self._pending.popleft())
        self._refresh_busy()

    def _finish(self, req: SupervisedRequest, reply: WorkerReply) -> None:
        if req.phase is _Phase.DONE:
            return
        if req.phase is _Phase.QUEUED:
            with contextlib.suppress(ValueError):
                self._pending.remove(req)
        elif self._inflight.get(req.request_id) is req:
            del self._inflight[req.request_id]
        req.phase = _Phase.DONE
        if req.timer is not None:
            req.timer.cancel()
            req.timer = None
        req.process = None
        req.payload = None
        req.deliver(reply)
        self._refresh_busy()

    def _fail(self, req: SupervisedRequest, code: str, detail: str) -> None:
        self._finish(req, RequestFailed(req.request_id, code, detail))

    def _fail_inflight(self, code: str, detail: str) -> None:
        for req in list(self._inflight.values()):
            self._fail(req, code, detail)

    def _fail_all(self, code: str, detail: str) -> None:
        self._fail_inflight(code, detail)
        while self._pending:
            self._fail(self._pending[0], code, detail)

    def _refresh_busy(self) -> None:
        if self._state not in (WorkerState.READY, WorkerState.BUSY):
            return
        proc = self._proc
        if proc is None or not proc.serving:
            return
        self._set_state(WorkerState.BUSY if self._inflight else WorkerState.READY)

    # ---------------------------------------------------------------- 消息（事件循环线程）

    def _on_message(self, proc: _Process, message: WorkerMessage) -> None:
        if proc is not self._proc:
            self._logger.debug("丢弃已结束的推理进程 %s 的消息：%s", proc.label, message.TYPE)
            return
        if isinstance(message, _REPLY_TYPES):
            self._on_reply(proc, message)
        elif isinstance(message, Ready):
            if proc.ready.done():
                self._logger.debug("推理进程 %s 重复发送 ready，已忽略", proc.label)
            else:
                proc.ready.set_result(message)
        elif isinstance(message, Progress):
            self._logger.debug(
                "推理进程 %s 进度：%s %s %s/%s",
                proc.label,
                message.kind,
                message.detail,
                message.done,
                message.total,
            )
            if self._on_progress is not None:
                try:
                    self._on_progress(message)
                except Exception:
                    self._logger.exception("进度回调出错")
        else:
            self._logger.warning(
                "推理进程 %s 发来了只应由主进程发送的消息 %s，已忽略", proc.label, message.TYPE
            )

    def _on_reply(self, proc: _Process, reply: WorkerReply) -> None:
        req = self._inflight.get(reply.request_id)
        if req is None or req.process is not proc:
            # 例如已经超时结束的请求迟到的回复。只记类型和请求号，不记内容（需求 PRV-02）。
            self._logger.debug("丢弃不属于在途请求的回复：%s（request_id=%s）", reply.TYPE, reply.request_id)
            return
        if isinstance(reply, Started):
            if not req.started:
                req.started = True
                req.timer = asyncio.get_running_loop().call_later(
                    req.timeout_s, self._on_request_timeout, req
                )
            req.deliver(reply)
        elif isinstance(reply, _TERMINAL_REPLIES):
            self._finish(req, reply)
        else:
            req.deliver(reply)

    def _on_request_timeout(self, req: SupervisedRequest) -> None:
        proc = req.process
        if req.phase is not _Phase.SENT or proc is None or proc is not self._proc:
            return
        self._logger.warning(
            "推理进程 %s 处理请求 %s 超过 %g 秒，结束并重启进程", proc.label, req.request_id, req.timeout_s
        )
        # 先结束进程再结算请求：进程不再接收新请求，状态不会在 CRASHED 之前闪回 READY。
        proc.kill(_Termination.REQUEST_TIMEOUT)
        self._fail(req, ErrorCode.TIMEOUT, f"推理进程处理该请求超过 {req.timeout_s:g} 秒")

    def _on_reader_closed(self, proc: _Process, error: ProtocolError | None) -> None:
        proc.serving = False
        if proc is not self._proc or proc.exited.done():
            return
        if error is not None:
            self._logger.error("推理进程 %s 发来无法解析的消息，结束进程：%s", proc.label, error)
            proc.kill(_Termination.PROTOCOL_ERROR)
            return
        # 管道断开而进程还在：给它一点时间自行退出，之后强制结束。
        proc.kill_timer = asyncio.get_running_loop().call_later(
            self._timings.pipe_closed_grace_s, proc.kill, _Termination.PIPE_CLOSED
        )

    # ---------------------------------------------------------------- 生命周期

    def _running(self) -> bool:
        return self._lifecycle is not None and not self._lifecycle.done()

    def _launch(self) -> None:
        if self._command is None:
            raise RuntimeError(f"推理进程 {self._name} 还没有启动命令，先用 reload(command=…) 给出")
        self._stopping = False
        self._set_state(WorkerState.STARTING)
        self._lifecycle = asyncio.get_running_loop().create_task(
            self._run(), name=f"lst-{self._name}-supervisor"
        )

    async def _run(self) -> None:
        """监管任务：启动子进程，等它结束，再按结束原因决定重启、停用或退出。"""
        try:
            while not self._stopping:
                outcome, detail = await self._run_process()
                if self._stopping or outcome is _Outcome.STOPPED:
                    return
                if outcome is _Outcome.RESTART:
                    self._fail_inflight(CANCELLED, "推理进程正在按新配置重启")
                    continue
                if outcome is _Outcome.FAILED:
                    self._enter_failed(detail)
                    return
                delay = self._register_crash()
                if delay is None:
                    minutes = self._timings.crash_window_s / 60
                    self._enter_failed(
                        f"{minutes:g} 分钟内崩溃 {self._timings.max_crashes} 次，最近一次：{detail}"
                    )
                    return
                self._logger.warning("推理进程 %s 崩溃：%s；%g 秒后重启", self._name, detail, delay)
                self._fail_inflight(ErrorCode.WORKER_CRASHED, f"推理进程崩溃：{detail}")
                self._set_state(WorkerState.CRASHED, f"{detail}；{delay:g} 秒后重启")
                await self._backoff(delay)
        except Exception:
            self._logger.exception("推理进程 %s 的监管任务出错", self._name)
            proc, self._proc = self._proc, None
            if proc is not None:
                proc.kill(_Termination.INTERNAL_ERROR)
            self._enter_failed("监管器内部错误，详见日志")

    async def _run_process(self) -> tuple[_Outcome, str]:
        """启动一个子进程并服务到它退出，返回结束原因。"""
        self._set_state(WorkerState.STARTING)
        self._generation += 1
        # 命令在事件循环线程上取快照交给线程：_spawn 执行期间 reload 可能换掉 self._command。
        command = self._command
        assert command is not None, "_launch 已检查启动命令"
        try:
            popen, listener, address = await asyncio.to_thread(self._spawn, command)
        except (OSError, ValueError) as exc:
            self._logger.error("无法启动推理进程 %s：%r", self._name, exc)
            return _Outcome.FAILED, f"无法启动推理进程：{exc}"
        proc = _Process(
            popen=popen,
            listener=listener,
            address=address,
            label=f"{self._name}#{self._generation}",
            loop=asyncio.get_running_loop(),
            logger=self._logger,
            on_message=self._on_message,
            on_reader_closed=self._on_reader_closed,
        )
        self._proc = proc
        proc.start_threads()
        self._logger.info("推理进程 %s 已启动，pid=%d", proc.label, proc.pid)
        try:
            if self._stopping:
                proc.kill(_Termination.STOP)
            elif command != self._command:
                self._logger.info("推理进程 %s 创建期间启动命令已改变，按新命令重启", proc.label)
                proc.kill(_Termination.RELOAD)
            else:
                await self._handshake(proc)
            await proc.exited
        except BaseException:
            # 监管任务被取消（例如事件循环关闭）或出现意外错误：不留下无人管理的子进程。
            proc.kill(_Termination.INTERNAL_ERROR)
            raise
        return await self._settle(proc)

    def _spawn(self, command: tuple[str, ...]) -> tuple[subprocess.Popen[bytes], Listener, str]:
        """在线程池里执行：建命名管道、启动子进程、加入作业对象、写入 authkey。

        command 由事件循环线程传入；这里不读监管器的任何状态。
        """
        address = f"\\\\.\\pipe\\lst-{self._name}-{uuid.uuid4().hex}"
        authkey = secrets.token_bytes(32)
        listener = Listener(address, family="AF_PIPE", authkey=authkey)
        try:
            popen = subprocess.Popen(
                [*command, "--address", address],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._cwd,
                env={**os.environ, **_CHILD_ENV_DEFAULTS, **self._env},
                creationflags=_CREATION_FLAGS,
            )
        except BaseException:
            listener.close()
            raise
        if not jobobject.assign_process(popen.pid) and sys.platform == "win32":
            self._logger.warning("推理进程 %s（pid=%d）没有加入作业对象", self._name, popen.pid)
        stdin = popen.stdin
        assert stdin is not None
        # authkey 只经标准输入传递，不出现在命令行里（架构文档 8.1 节）。子进程已经退出时写入失败，
        # 由等待线程报告退出。
        with contextlib.suppress(OSError):
            stdin.write(authkey.hex().encode("ascii") + b"\n")
        with contextlib.suppress(OSError):
            stdin.close()
        return popen, listener, address

    async def _handshake(self, proc: _Process) -> None:
        """accept → Hello、Configure → 等 Ready → READY。任何一步失败都结束子进程，由 _settle 归类。"""
        timings = self._timings
        await asyncio.wait(
            {proc.accepted, proc.exited},
            timeout=timings.accept_timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        conn = proc.accepted.result() if proc.accepted.done() else None
        if conn is None:
            # 超时，或进程在连接之前就退出了（kill 对已退出的进程不记录原因）。
            proc.abort_accept()
            proc.kill(_Termination.ACCEPT_TIMEOUT)
            return
        proc.attach(conn)
        proc.send(Hello(PROTOCOL_VERSION))
        proc.send(Configure(dict(self._settings)))
        self._set_state(WorkerState.LOADING)

        await asyncio.wait(
            {proc.ready, proc.exited}, timeout=timings.ready_timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
        if proc.exited.done() or proc.termination is not None:
            return
        if not proc.ready.done():
            proc.kill(_Termination.READY_TIMEOUT)
            return
        ready = proc.ready.result()
        if ready.protocol_version != PROTOCOL_VERSION:
            self._logger.error(
                "推理进程 %s 的协议版本为 %d，主进程为 %d",
                proc.label,
                ready.protocol_version,
                PROTOCOL_VERSION,
            )
            proc.kill(_Termination.PROTOCOL_MISMATCH)
            return
        self._capabilities = dict(ready.capabilities)
        proc.serving = True
        self._logger.info("推理进程 %s 就绪，依赖版本：%s", proc.label, ready.versions)
        self._set_state(WorkerState.READY)
        self._flush_pending(proc)

    async def _settle(self, proc: _Process) -> tuple[_Outcome, str]:
        """子进程已退出：等读线程交完剩余消息，回收线程，归类退出原因。"""
        code = proc.exited.result()
        proc.serving = False
        self._last_exit_code = code
        if proc.kill_timer is not None:
            proc.kill_timer.cancel()
        # 子进程退出前发出的回复（例如最后一条 RequestDone）要先交给请求，再结算在途请求。
        pending_io = [f for f in proc.io_futures() if not f.done()]
        if pending_io:
            await asyncio.wait(pending_io, timeout=self._timings.io_drain_s)
        proc.abort_accept()
        if self._proc is proc:
            self._proc = None
        outcome, detail = self._classify(proc, code)
        self._logger.info("推理进程 %s 已退出：%s（%s）", proc.label, _describe_exit(code), detail)
        return outcome, detail

    def _classify(self, proc: _Process, code: int) -> tuple[_Outcome, str]:
        exit_text = _describe_exit(code)
        if self._stopping:
            return _Outcome.STOPPED, "已停止"
        match proc.termination:
            case _Termination.RELOAD:
                return _Outcome.RESTART, "按新配置重启"
            case _Termination.STOP:
                return _Outcome.STOPPED, "已停止"
            case _Termination.PROTOCOL_MISMATCH:
                ready = proc.ready.result()
                return (
                    _Outcome.FAILED,
                    f"协议版本不符：推理进程 {ready.protocol_version}，主进程 {PROTOCOL_VERSION}",
                )
            case _Termination.ACCEPT_TIMEOUT:
                return _Outcome.CRASHED, f"{self._timings.accept_timeout_s:g} 秒内没有连接管道"
            case _Termination.READY_TIMEOUT:
                return _Outcome.CRASHED, f"{self._timings.ready_timeout_s:g} 秒内没有就绪"
            case _Termination.REQUEST_TIMEOUT:
                return _Outcome.CRASHED, "请求处理超时，已结束进程"
            case _Termination.PROTOCOL_ERROR:
                return _Outcome.CRASHED, "发来无法解析的消息，已结束进程"
            case _Termination.PIPE_CLOSED:
                return _Outcome.CRASHED, "管道断开后进程没有退出，已结束进程"
            case _Termination.INTERNAL_ERROR:
                return _Outcome.FAILED, "监管器内部错误，详见日志"
            case None:
                deterministic = _deterministic_exit(code)
                if deterministic is not None:
                    return _Outcome.FAILED, f"确定性故障 {deterministic.name}（{exit_text}）"
                return _Outcome.CRASHED, f"进程意外退出（{exit_text}）"

    def _prune_crashes(self, now: float) -> None:
        window = self._timings.crash_window_s
        while self._crash_times and now - self._crash_times[0] >= window:
            self._crash_times.popleft()

    def _register_crash(self) -> float | None:
        """记录一次崩溃。返回重启前的等待秒数；窗口内崩溃次数达到上限时返回 None。"""
        now = self._clock()
        self._crash_times.append(now)
        self._prune_crashes(now)
        count = len(self._crash_times)
        if count >= self._timings.max_crashes:
            return None
        backoff = self._timings.backoff_s
        return backoff[min(count, len(backoff)) - 1] if backoff else 0.0

    async def _backoff(self, delay: float) -> None:
        """退避等待。stop() 或 reload() 取消等待时立即返回。"""
        task = asyncio.ensure_future(self._sleep(delay))
        self._backoff_task = task
        try:
            await asyncio.wait({task})
        finally:
            self._backoff_task = None
            if not task.done():
                task.cancel()
            elif not task.cancelled() and task.exception() is not None:
                self._logger.warning("退避等待出错：%r", task.exception())

    async def _abandon(self, task: asyncio.Task[None]) -> None:
        """stop() 等监管任务超时：记 ERROR，取消任务，强制结束仍在的子进程，不再等待。"""
        self._logger.error(
            "推理进程 %s 的监管任务在 %g 秒内没有结束，放弃等待",
            self._name,
            self._timings.stop_timeout_s,
        )
        task.cancel()
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.kill(_Termination.STOP)
        # 给取消一次运行机会；任务卡在线程里的阻塞调用上时，取消只是放弃等待那个调用。
        await asyncio.wait({task}, timeout=0.1)

    async def _terminate(self, proc: _Process, grace_s: float, reason: _Termination) -> None:
        """发送 Shutdown，总共等待 grace_s，仍未退出就强制结束，然后等到进程退出。"""
        if proc.exited.done():
            return
        if proc.termination is None:
            proc.termination = reason
        proc.serving = False
        if proc.conn is not None:
            proc.send(Shutdown())
            await asyncio.wait({proc.exited}, timeout=grace_s)
        if not proc.exited.done():
            if proc.conn is not None:
                self._logger.warning("推理进程 %s 在 %g 秒内没有退出，强制结束", proc.label, grace_s)
            proc.kill(reason)
            await asyncio.wait({proc.exited}, timeout=_KILL_WAIT_S)
            if not proc.exited.done():
                self._logger.error("推理进程 %s（pid=%d）强制结束后仍未退出", proc.label, proc.pid)

    def _enter_failed(self, detail: str) -> None:
        self._proc = None
        self._logger.error("推理进程 %s 已停用：%s", self._name, detail)
        self._set_state(WorkerState.FAILED, detail)
        self._fail_all(ErrorCode.WORKER_CRASHED, f"推理进程已停用：{detail}")

    def _set_state(self, state: WorkerState, detail: str = "") -> None:
        if state is self._state:
            return
        previous, self._state, self._detail = self._state, state, detail
        level = logging.DEBUG if {previous, state} == {WorkerState.READY, WorkerState.BUSY} else logging.INFO
        self._logger.log(level, "推理进程 %s：%s → %s %s", self._name, previous, state, detail)
        if self._on_state is not None:
            try:
                self._on_state(state, detail)
            except Exception:
                self._logger.exception("状态回调出错")
        waiters, self._state_waiters = self._state_waiters, []
        for states, future in waiters:
            if future.done():
                continue
            if state in states:
                future.set_result(state)
            else:
                self._state_waiters.append((states, future))
