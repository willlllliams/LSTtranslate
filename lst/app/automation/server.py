r"""自动化接口的命名管道服务端，运行在主进程的事件循环里（架构文档 16.2 节）。

qasync 在 Windows 上的循环继承自 asyncio 的 ProactorEventLoop，用 I/O 完成端口（IOCP）收发，命名管道的
连接、读、写都能直接交给它。asyncio 自带的 ``start_serving_pipe`` 创建管道时不带安全描述符（用系统默认的
DACL，SYSTEM、Administrators 与“所有人读”都在里面），所以这里自己创建管道实例
（``winsec.create_pipe_instance``，带只允许当前用户的 DACL），再用循环的 proactor 完成
``ConnectNamedPipe``、``ReadFile``、``WriteFile``。
proactor 是循环的内部属性 ``_proactor``，CPython 3.8 至 3.13 的 ProactorEventLoop 与 qasync 都有它；
循环不是 Proactor 时 ``start`` 抛 RuntimeError，自动化接口不可用，程序照常运行。

接受连接的方式与 ``start_serving_pipe`` 相同：始终保留一个空闲实例等待下一个客户端，
客户端连上后立即再建一个新实例，然后在这个连接上循环“读一行、处理、写回复”。

关闭（``close``）分两步：先关掉空闲实例、停止接受，空闲的连接立即断开；正在处理请求的连接写完这次回复再断开。
这样“关闭自动化接口”这个请求本身也能收到回复。``aclose`` 等这些连接结束，超过时限后取消。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from lst.automation import winsec
from lst.automation.protocol import (
    MAX_LINE_BYTES,
    ErrorCode,
    Json,
    ProtocolError,
    Request,
    error_line,
    parse_request,
    result_line,
)


class MethodError(Exception):
    """方法以错误结束。code 与 message 写进回复的 error，data 可以带上当时的状态。"""

    def __init__(self, code: ErrorCode, message: str, data: Json = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True, slots=True)
class Deferred:
    """方法的结果，外加回复写出之后才执行的动作（退出、重启）：动作先执行的话，回复可能来不及写出。"""

    result: Json
    after: Callable[[], None]


type Handler = Callable[[Request], Awaitable[Json | Deferred]]


class _Proactor(Protocol):
    def accept_pipe(self, pipe: Any) -> asyncio.Future[Any]: ...
    def recv(self, conn: Any, nbytes: int) -> asyncio.Future[bytes]: ...
    def send(self, conn: Any, buf: bytes) -> asyncio.Future[int]: ...


class _Handle:
    """管道句柄的包装：proactor 要用 fileno() 取句柄，并把对象放进弱引用集合登记到完成端口。"""

    __slots__ = ("__weakref__", "_handle")

    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle

    def fileno(self) -> int:
        if self._handle is None:
            raise ValueError("管道句柄已关闭")
        return self._handle

    @property
    def closed(self) -> bool:
        return self._handle is None

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            winsec.close_handle(handle)


@dataclass(eq=False, slots=True)
class _Connection:
    pipe: _Handle
    task: asyncio.Task[None] | None = None
    busy: bool = False
    buffer: bytearray = field(default_factory=bytearray)


def _proactor_of(loop: asyncio.AbstractEventLoop) -> _Proactor:
    proactor = getattr(loop, "_proactor", None)
    if proactor is None or not hasattr(proactor, "accept_pipe"):
        raise RuntimeError("自动化接口需要 Proactor 事件循环（Windows 上的 asyncio 缺省循环或 qasync）")
    return cast(_Proactor, proactor)


class PipeServer:
    """一个命名管道服务端。handler 处理一个请求；日志只记方法名、结果与耗时，不记参数与结果的内容。"""

    def __init__(self, name: str, handler: Handler, *, logger: logging.Logger | None = None) -> None:
        self.name = name
        self._handler = handler
        self._logger = logger or logging.getLogger(__name__)
        self._security: winsec.PipeSecurity | None = None
        self._proactor: _Proactor | None = None
        self._listening: _Handle | None = None
        self._accept_task: asyncio.Task[None] | None = None
        self._connections: set[_Connection] = set()
        self._closing = False

    @property
    def listening(self) -> bool:
        return self._accept_task is not None and not self._closing

    @property
    def connections(self) -> int:
        return len(self._connections)

    def start(self) -> None:
        """创建第一个管道实例并开始接受连接。管道名已被占用时抛 OSError（winerror 为 5 或 231）。"""
        if self._accept_task is not None:
            raise RuntimeError("服务端已经启动")
        loop = asyncio.get_running_loop()
        self._proactor = _proactor_of(loop)
        self._security = winsec.PipeSecurity(winsec.pipe_sddl(winsec.current_user_sid()))
        try:
            self._listening = _Handle(winsec.create_pipe_instance(self.name, self._security, first=True))
        except OSError:
            self._security.close()
            self._security = None
            raise
        self._accept_task = loop.create_task(self._accept_loop(), name="automation:accept")

    def close(self) -> None:
        """停止接受连接，断开空闲的连接；正在处理请求的连接写完回复后断开。重复调用没有效果。"""
        if self._closing:
            return
        self._closing = True
        if self._accept_task is not None:
            self._accept_task.cancel()
        if self._listening is not None:
            self._listening.close()
            self._listening = None
        for connection in tuple(self._connections):
            if not connection.busy and connection.task is not None:
                connection.task.cancel()

    async def aclose(self, grace: float = 2.0) -> None:
        """close 之后等全部连接结束；超过 grace 秒仍在处理的请求被取消。"""
        self.close()
        tasks = [c.task for c in self._connections if c.task is not None]
        if self._accept_task is not None:
            tasks.append(self._accept_task)
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=grace)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if self._security is not None:
            self._security.close()
            self._security = None

    # ------------------------------------------------------------------ 接受连接

    async def _accept_loop(self) -> None:
        proactor, security = self._proactor, self._security
        assert proactor is not None and security is not None
        loop = asyncio.get_running_loop()
        try:
            while not self._closing:
                pipe = self._listening
                if pipe is None:
                    return
                try:
                    await proactor.accept_pipe(pipe)
                except BrokenPipeError:
                    # 客户端连上又立即断开：这个实例作废，换一个新的继续等。
                    pipe.close()
                    self._listening = _Handle(winsec.create_pipe_instance(self.name, security, first=False))
                    continue
                if self._closing:
                    pipe.close()
                    return
                # 先建下一个空闲实例，再处理这个连接：任何时刻都有实例在等，客户端不会遇到“管道不存在”。
                self._listening = _Handle(winsec.create_pipe_instance(self.name, security, first=False))
                connection = _Connection(pipe)
                self._connections.add(connection)
                connection.task = loop.create_task(self._serve(connection), name="automation:connection")
        except asyncio.CancelledError:
            return
        except OSError:
            self._logger.exception("自动化接口停止接受连接")

    # ------------------------------------------------------------------ 一个连接

    async def _serve(self, connection: _Connection) -> None:
        proactor = self._proactor
        assert proactor is not None
        try:
            while True:
                line = await self._read_line(proactor, connection)
                if line is None:
                    return
                connection.busy = True
                after: Callable[[], None] | None = None
                try:
                    reply, after = await self._handle(line)
                    await proactor.send(connection.pipe, reply)
                finally:
                    connection.busy = False
                if after is not None:
                    after()
                if self._closing:
                    return
        except asyncio.CancelledError:
            return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return  # 客户端断开
        finally:
            connection.pipe.close()
            self._connections.discard(connection)

    async def _read_line(self, proactor: _Proactor, connection: _Connection) -> bytes | None:
        buffer = connection.buffer
        while True:
            newline = buffer.find(b"\n")
            length = newline if newline >= 0 else len(buffer)
            if length > MAX_LINE_BYTES:
                with contextlib.suppress(OSError):
                    await proactor.send(
                        connection.pipe, error_line(None, ErrorCode.INVALID_REQUEST, "请求超过 1 MiB")
                    )
                return None
            if newline >= 0:
                line = bytes(buffer[:newline]).rstrip(b"\r")
                del buffer[: newline + 1]
                if line.strip():
                    return line
                continue
            chunk = await proactor.recv(connection.pipe, 65536)
            if not chunk:
                return None
            buffer += chunk

    async def _handle(self, line: bytes) -> tuple[bytes, Callable[[], None] | None]:
        started = time.perf_counter()
        try:
            request = parse_request(line)
        except ProtocolError as exc:
            self._logger.info("自动化接口：无法解析的请求（%s）", exc.code.value)
            return error_line(exc.request_id, exc.code, exc.message), None
        outcome = "ok"
        after: Callable[[], None] | None = None
        try:
            result = await self._handler(request)
            if isinstance(result, Deferred):
                after = result.after
                result = result.result
            reply = result_line(request.id, result)
        except MethodError as exc:
            outcome = exc.code.value
            reply = error_line(request.id, exc.code, exc.message, exc.data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.exception("自动化接口：方法 %s 出错", request.method)
            outcome = ErrorCode.INTERNAL.value
            reply = error_line(request.id, ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}")
        elapsed = (time.perf_counter() - started) * 1000
        self._logger.info("自动化接口：%s → %s，%.0f 毫秒", request.method, outcome, elapsed)
        return reply, after


__all__ = ["Deferred", "Handler", "MethodError", "PipeServer"]
