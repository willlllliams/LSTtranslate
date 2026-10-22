r"""自动化接口的同步客户端。测试、脚本与命令行（``python -m lst.automation``）都用它。

```python
from lst.automation import AutomationClient

with AutomationClient.connect(timeout=10) as client:
    print(client.call("status")["version"])
    session = client.call("capture", {"rect": [0, 0, 800, 300]})["session_id"]
    result = client.call("wait_result", {"session_id": session, "timeout": 60})
```

连接时检查管道的所有者是当前用户：别的用户抢先建了同名管道时，客户端拒绝连接，不把请求发给它。
请求与回复都是阻塞读写，没有单独的读超时；等待类方法（wait_result、storage.*）的时限由服务端执行，
服务端进程退出时读操作随管道断开而返回。
"""

from __future__ import annotations

import io
import itertools
import json
import msvcrt
import os
import time
from collections.abc import Callable
from typing import Self, cast

from lst.automation import winsec
from lst.automation.protocol import MAX_LINE_BYTES, Json, request_line


class AutomationError(Exception):
    """服务端回复了 error。code 是错误码（protocol.ErrorCode 的取值），data 是附带的数据。"""

    def __init__(self, code: str, message: str, data: Json = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class ConnectionLost(OSError):
    """连接在收到回复之前断开（程序退出或重启）。"""


class AutomationClient:
    """一条连接。用 connect 创建；用完调用 close，或者用 with 语句。"""

    def __init__(self, stream: io.RawIOBase, pipe: str, server_pid: int) -> None:
        self._stream = stream
        self._buffer = bytearray()
        self._ids = itertools.count(1)
        self.pipe = pipe
        self.server_pid = server_pid

    # ------------------------------------------------------------------ 连接

    @classmethod
    def connect(cls, pipe: str | None = None, *, timeout: float = 5.0, check_owner: bool = True) -> Self:
        """连接到正在运行的程序。timeout 秒内管道一直不存在或一直忙时抛 FileNotFoundError 或 OSError。"""
        name = winsec.normalize_pipe_name(pipe) if pipe else winsec.default_pipe_name()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                handle = winsec.open_pipe_client(name)
                break
            except FileNotFoundError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
            except OSError as exc:
                if exc.winerror != winsec.ERROR_PIPE_BUSY or time.monotonic() >= deadline:
                    raise
                winsec.wait_pipe(name, 200)
        try:
            if check_owner:
                owner, me = winsec.handle_owner(handle), winsec.current_user_sid()
                if owner != me:
                    raise PermissionError(f"管道 {name} 的所有者是 {owner}，不是当前用户 {me}，拒绝连接")
            server_pid = winsec.pipe_server_pid(handle)
            fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            winsec.close_handle(handle)
            raise
        stream = cast(io.RawIOBase, open(fd, "r+b", buffering=0))  # noqa: SIM115  # 连接的生命周期由本类管理
        return cls(stream, name, server_pid)

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 请求

    def call(self, method: str, params: dict[str, Json] | None = None) -> Json:
        """发一个请求，等回复。成功时返回 result，失败时抛 AutomationError，连接断开时抛 ConnectionLost。"""
        request_id = next(self._ids)
        try:
            self._stream.write(request_line(request_id, method, params))
        except OSError as exc:
            raise ConnectionLost(f"发送 {method} 时连接已断开：{exc}") from exc
        reply = self._read_reply()
        if reply.get("id") != request_id:
            raise AutomationError("internal", f"回复的 id {reply.get('id')!r} 与请求 {request_id} 不符")
        error = reply.get("error")
        if isinstance(error, dict):
            fields = error
            return_code = fields.get("code")
            message = fields.get("message")
            raise AutomationError(
                str(return_code), str(message) if message is not None else "", fields.get("data")
            )
        return reply.get("result")

    def _read_reply(self) -> dict[str, Json]:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                data: object = json.loads(line.decode("utf-8"))
                if not isinstance(data, dict):
                    raise AutomationError("internal", "回复不是 JSON 对象")
                return cast(dict[str, Json], data)
            if len(self._buffer) > MAX_LINE_BYTES * 64:
                raise AutomationError("internal", "回复过长")
            try:
                chunk = self._stream.read(65536)
            except OSError as exc:
                raise ConnectionLost(f"读取回复时连接已断开：{exc}") from exc
            if not chunk:
                raise ConnectionLost("连接在收到回复之前断开")
            self._buffer += chunk


def wait_for_server(
    pipe: str | None = None,
    *,
    timeout: float = 60.0,
    accept: Callable[[AutomationClient], bool] | None = None,
) -> AutomationClient:
    """反复连接，直到连上并且 accept(client) 为真（缺省只要连上），返回这条连接。

    重启之后旧进程的管道先消失，新进程启动完才重新出现；accept 用来排除还没退出的旧进程，
    例如 ``lambda c: c.server_pid != old_pid``。超时抛 TimeoutError。
    """
    deadline = time.monotonic() + timeout
    last: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            client = AutomationClient.connect(pipe, timeout=min(2.0, max(0.1, deadline - time.monotonic())))
        except OSError as exc:
            last = exc
            time.sleep(0.2)
            continue
        if accept is None or accept(client):
            return client
        client.close()
        time.sleep(0.2)
    raise TimeoutError(f"{timeout:g} 秒内没有连上自动化接口：{last}")


def restart_and_reconnect(client: AutomationClient, *, timeout: float = 120.0) -> AutomationClient:
    """调用 storage.restart（带 confirm），等旧进程退出、新进程的接口重新可用，返回新连接。旧连接随之关闭。"""
    old_pid = client.server_pid
    pipe = client.pipe
    client.call("storage.restart", {"confirm": True})
    client.close()
    return wait_for_server(pipe, timeout=timeout, accept=lambda c: c.server_pid != old_pid)


__all__ = [
    "AutomationClient",
    "AutomationError",
    "ConnectionLost",
    "restart_and_reconnect",
    "wait_for_server",
]
