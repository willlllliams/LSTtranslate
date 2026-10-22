"""自动化接口的传输层：线路协议、真实命名管道的连接与权限、关闭的语义、命令行客户端（需求 DEV-04、DEV-07）。

服务端跑在测试的 Proactor 事件循环里（Windows 上 asyncio 的缺省循环，qasync 也继承自它）；客户端是阻塞的，
放在线程里调用。每个测试用自己的管道名，不会碰到正在运行的程序。
"""

import asyncio
import json
import msvcrt
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from lst.app.automation.server import Deferred, MethodError, PipeServer
from lst.automation import AutomationClient, AutomationError, ConnectionLost, winsec
from lst.automation.protocol import (
    MAX_LINE_BYTES,
    ErrorCode,
    Json,
    ProtocolError,
    Request,
    error_line,
    parse_request,
    request_line,
    result_line,
)

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="自动化接口只在 Windows 上实现")


def _pipe() -> str:
    return winsec.normalize_pipe_name(f"lst-test-{os.getpid()}-{uuid.uuid4().hex[:8]}")


# ---------------------------------------------------------------- 线路协议


def test_request_round_trip() -> None:
    line = request_line(7, "overlay.select", {"start": [0, 1], "text": "中文"})
    assert line.endswith(b"\n") and b"\\u" not in line  # 不转义非 ASCII 字符
    request = parse_request(line.rstrip(b"\n"))
    assert request == Request(7, "overlay.select", {"start": [0, 1], "text": "中文"})


def test_missing_params_mean_empty_object() -> None:
    assert parse_request(b'{"id": "a", "method": "status"}').params == {}
    assert parse_request(b'{"id": 1, "method": "status", "params": null}').params == {}


@pytest.mark.parametrize(
    ("line", "code"),
    [
        (b"not json", ErrorCode.PARSE_ERROR),
        (b"\xff\xfe", ErrorCode.PARSE_ERROR),
        (b"[1, 2]", ErrorCode.INVALID_REQUEST),
        (b'{"id": 1}', ErrorCode.INVALID_REQUEST),
        (b'{"id": true, "method": "status"}', ErrorCode.INVALID_REQUEST),
        (b'{"id": 1, "method": "status", "params": [1]}', ErrorCode.INVALID_REQUEST),
    ],
)
def test_malformed_requests(line: bytes, code: ErrorCode) -> None:
    with pytest.raises(ProtocolError) as caught:
        parse_request(line)
    assert caught.value.code is code


def test_reply_lines() -> None:
    assert json.loads(result_line(3, {"ok": True})) == {"id": 3, "result": {"ok": True}}
    assert json.loads(error_line(3, ErrorCode.TIMEOUT, "超时", {"phase": "x"})) == {
        "id": 3,
        "error": {"code": "timeout", "message": "超时", "data": {"phase": "x"}},
    }


# ---------------------------------------------------------------- 管道名与安全描述符


def test_pipe_name_is_per_user_and_stable() -> None:
    sid = winsec.current_user_sid()
    assert re.fullmatch(r"S-1-5-21(-\d+)+", sid) or sid.startswith("S-1-")
    name = winsec.pipe_name_for(sid)
    assert name == winsec.pipe_name_for(sid)
    assert re.fullmatch(r"\\\\\.\\pipe\\lsttranslate-automation-[0-9a-f]{12}", name)
    assert winsec.pipe_name_for("S-1-5-21-1-2-3-1002") != winsec.pipe_name_for("S-1-5-21-1-2-3-1003")


def test_pipe_name_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(winsec.PIPE_ENV, "lst-custom")
    assert winsec.default_pipe_name() == r"\\.\pipe\lst-custom"
    monkeypatch.setenv(winsec.PIPE_ENV, r"\\.\pipe\full-name")
    assert winsec.default_pipe_name() == r"\\.\pipe\full-name"
    with pytest.raises(ValueError):
        winsec.normalize_pipe_name("a/b")


def _aces(sddl: str) -> list[tuple[str, ...]]:
    dacl = sddl.split("D:", 1)[1]
    return [tuple(ace.split(";")) for ace in re.findall(r"\(([^)]*)\)", dacl)]


def test_created_pipe_grants_only_the_current_user() -> None:
    """管道的 DACL 按 SDDL 断言：受保护（P，不继承），只有一条条目，允许当前用户全部权限（FA），
    SYSTEM（SY）、Administrators（BA）、所有人（WD）都不在里面；所有者是当前用户。"""
    sid = winsec.current_user_sid()
    name = _pipe()
    security = winsec.PipeSecurity(winsec.pipe_sddl(sid))
    handle = winsec.create_pipe_instance(name, security, first=True)
    try:
        sddl = winsec.handle_sddl(handle)
        assert sddl.startswith(f"O:{sid}")
        assert "D:P" in sddl
        assert _aces(sddl) == [("A", "", "FA", "", "", sid)]
        for trustee in ("SY", "BA", "WD", "AU", "BU"):
            assert f";{trustee})" not in sddl
        assert winsec.handle_owner(handle) == sid
        # 同名管道已经存在时第一个实例建不起来，不会接到别人的管道上。
        with pytest.raises(OSError):
            winsec.create_pipe_instance(name, security, first=True)
    finally:
        winsec.close_handle(handle)
        security.close()


# ---------------------------------------------------------------- 服务端与客户端


done: list[str] = []
"""Deferred 的动作与方法本身按什么顺序执行。"""


async def _echo(request: Request) -> Json | Deferred:
    match request.method:
        case "sleep":
            await asyncio.sleep(float(str(request.params.get("s", 0.2))))
            return {"slept": True}
        case "fail":
            raise MethodError(ErrorCode.UNAVAILABLE, "没有窗口", {"why": "test"})
        case "boom":
            raise RuntimeError("意外")
        case "deferred":
            done.append("after")
            return Deferred({"deferred": True}, lambda: done.append("ran"))
        case _:
            return {"method": request.method, "params": request.params}


@pytest.fixture
async def server() -> AsyncIterator[PipeServer]:
    instance = PipeServer(_pipe(), _echo)
    instance.start()
    try:
        yield instance
    finally:
        await instance.aclose()


async def _connect(server: PipeServer) -> AutomationClient:
    return await asyncio.to_thread(AutomationClient.connect, server.name, timeout=5)


async def test_current_user_connects_and_calls(server: PipeServer) -> None:
    client = await _connect(server)
    try:
        assert client.server_pid == os.getpid()
        result = await asyncio.to_thread(client.call, "hello", {"x": "中文", "n": [1, 2]})
        assert result == {"method": "hello", "params": {"x": "中文", "n": [1, 2]}}
        with pytest.raises(AutomationError) as caught:
            await asyncio.to_thread(client.call, "fail")
        assert caught.value.code == "unavailable" and caught.value.data == {"why": "test"}
        with pytest.raises(AutomationError) as internal:
            await asyncio.to_thread(client.call, "boom")
        assert internal.value.code == "internal" and "RuntimeError" in internal.value.message
        # 出错之后同一个连接照常可用。
        assert await asyncio.to_thread(client.call, "again") == {"method": "again", "params": {}}
    finally:
        client.close()


async def test_several_clients_at_once(server: PipeServer) -> None:
    clients = [await _connect(server) for _ in range(3)]
    try:
        results = await asyncio.gather(*(asyncio.to_thread(c.call, "sleep", {"s": 0.3}) for c in clients))
        assert results == [{"slept": True}] * 3
        assert server.connections == 3
    finally:
        for client in clients:
            client.close()


async def test_deferred_action_runs_after_the_reply(server: PipeServer) -> None:
    done.clear()
    client = await _connect(server)
    try:
        assert await asyncio.to_thread(client.call, "deferred") == {"deferred": True}
        await asyncio.sleep(0.05)
        assert done == ["after", "ran"]
    finally:
        client.close()


@pytest.mark.parametrize("ending", [b"", b"\n"])
async def test_garbage_and_oversized_lines(server: PipeServer, ending: bytes) -> None:
    handle = await asyncio.to_thread(winsec.open_pipe_client, server.name)
    fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    with open(fd, "r+b", buffering=0) as stream:
        await asyncio.to_thread(stream.write, b"this is not json\n")
        reply = json.loads(await asyncio.to_thread(stream.read, 65536))
        assert reply["error"]["code"] == "parse_error" and reply["id"] is None
        await asyncio.to_thread(stream.write, b"x" * (MAX_LINE_BYTES + 10) + ending)

        def read_all() -> bytes:
            data = b""
            while chunk := stream.read(65536):
                data += chunk
            return data

        tail = await asyncio.to_thread(read_all)
    assert json.loads(tail)["error"]["code"] == "invalid_request"


async def test_close_finishes_the_request_in_flight() -> None:
    """关闭服务端时，正在处理的请求照常写回复（例如“关掉自动化接口”的那一次 settings.set），空闲连接立即断开，
    之后连不上。"""
    instance = PipeServer(_pipe(), _echo)
    instance.start()
    busy, idle = await _connect(instance), await _connect(instance)
    try:
        call = asyncio.ensure_future(asyncio.to_thread(busy.call, "sleep", {"s": 0.5}))
        await asyncio.sleep(0.2)
        instance.close()
        assert not instance.listening
        assert await call == {"slept": True}
        with pytest.raises(ConnectionLost):
            await asyncio.to_thread(idle.call, "status")
        with pytest.raises(FileNotFoundError):
            await asyncio.to_thread(AutomationClient.connect, instance.name, timeout=0.3)
    finally:
        busy.close()
        idle.close()
        await instance.aclose()


async def test_a_second_server_with_the_same_name_fails(server: PipeServer) -> None:
    other = PipeServer(server.name, _echo)
    with pytest.raises(OSError):
        other.start()


async def test_connect_without_server_times_out() -> None:
    with pytest.raises(FileNotFoundError):
        await asyncio.to_thread(AutomationClient.connect, _pipe(), timeout=0.3)


# ---------------------------------------------------------------- 命令行


async def _cli(*args: str) -> tuple[int, Json]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lst.automation",
        *args,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), 60)
    assert process.returncode is not None, err
    return process.returncode, json.loads(out.decode("utf-8"))


async def test_cli_prints_json_and_sets_exit_codes(server: PipeServer) -> None:
    code, output = await _cli("echo", '{"a": "中文"}', "--pipe", server.name)
    assert code == 0 and output == {"method": "echo", "params": {"a": "中文"}}
    code, output = await _cli("fail", "--pipe", server.name)
    assert (code, _error_code(output)) == (1, "unavailable")
    code, output = await _cli("echo", "[1]", "--pipe", server.name)
    assert (code, _error_code(output)) == (64, "usage")
    code, output = await _cli("status", "--pipe", _pipe(), "--connect-timeout", "0.2")
    assert (code, _error_code(output)) == (2, "connection")


def _error_code(output: Json) -> str:
    assert isinstance(output, dict)
    error = output.get("error")
    assert isinstance(error, dict)
    return str(error.get("code"))
