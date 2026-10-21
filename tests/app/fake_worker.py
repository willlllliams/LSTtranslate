"""监管器测试用的假推理进程：用 contracts.worker_protocol 的编解码按协议说话。

启动方式与真实推理进程相同：``python -m tests.app.fake_worker --address <管道地址>``，
authkey 的十六进制从标准输入第一行读入。

连接管道之前的行为由命令行参数控制：

- ``--exit-code N``：读完 authkey 后立即以 N 退出，不连接管道；
- ``--no-connect``：读完 authkey 后一直等待，不连接管道。

握手之后的行为由 Configure 的 settings 控制，键见 ``Behavior``。请求在一个处理线程上按到达顺序处理；
接收线程（主线程）处理 Cancel 和 Shutdown，与真实推理进程的运行时一致。

翻译请求的段文本为 ``SEEN_PROBE`` 时，译文是本进程到目前为止收到的请求号和取消记录（``cancel:<请求号>``），
以逗号连接。测试用它确认哪些消息真正到达了子进程。
"""

from __future__ import annotations

import argparse
import ctypes
import faulthandler
import os
import sys
import threading
import time
import zlib
from collections import deque
from dataclasses import dataclass
from multiprocessing.connection import Client
from typing import NoReturn

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
    Ready,
    RequestDone,
    RequestFailed,
    SegmentResult,
    Shutdown,
    Started,
    TranslateRequest,
    WorkerMessage,
    decode,
    recv_message,
    send_message,
)

SEEN_PROBE = "__seen__"


def _float(settings: dict[str, JsonValue], key: str) -> float:
    value = settings.get(key, 0)
    return float(value) if isinstance(value, int | float) else 0.0


def _strings(settings: dict[str, JsonValue], key: str) -> frozenset[str]:
    value = settings.get(key, [])
    return frozenset(v for v in value if isinstance(v, str)) if isinstance(value, list) else frozenset()


@dataclass(frozen=True, slots=True)
class Behavior:
    """Configure.settings 中可用的键。"""

    capabilities: dict[str, JsonValue]
    """原样放进 Ready。"""
    ready_delay: float
    """发送 Ready 之前等待的秒数（模拟模型加载）。"""
    exit_on_configure: int | None
    """收到 Configure 后以该退出码退出（模拟确定性故障或加载崩溃）。"""
    protocol_version: int
    """Ready 中报告的协议版本。"""
    segment_delay: float
    """每段（OCR 请求为整个请求）处理前等待的秒数。"""
    crash_on: dict[str, str]
    """请求号 → "exit"（os._exit(3)）或 "segfault"（真实的访问违例）。在发送 Started 之后执行。"""
    hang_on: frozenset[str]
    """这些请求发送 Started 之后不再回复，处理线程卡住。"""
    late_duplicates: frozenset[str]
    """这些请求结束之后，再发一遍 SegmentResult 和 RequestDone（迟到的重复回复）。"""
    ignore_shutdown: bool
    """收到 Shutdown 后不退出。"""
    payload_read_delay: float
    """收到 OCR 请求的 JSON 帧后，等待这么多秒再读像素帧，让主进程的发送阻塞。"""
    stdout_lines: int
    """发送 Ready 之前向标准输出写这么多行。"""
    stderr_text: str
    """发送 Ready 之前写到标准错误的文本。"""
    progress_steps: int
    """发送 Ready 之前发送的 Progress 消息条数。"""

    @classmethod
    def parse(cls, settings: dict[str, JsonValue]) -> Behavior:
        capabilities = settings.get("capabilities", {})
        exit_code = settings.get("exit_on_configure")
        protocol_version = settings.get("protocol_version", PROTOCOL_VERSION)
        crash_on = settings.get("crash_on", {})
        stdout_lines = settings.get("stdout_lines", 0)
        stderr_text = settings.get("stderr_text", "")
        progress_steps = settings.get("progress_steps", 0)
        return cls(
            capabilities=capabilities if isinstance(capabilities, dict) else {},
            ready_delay=_float(settings, "ready_delay"),
            exit_on_configure=exit_code if isinstance(exit_code, int) else None,
            protocol_version=protocol_version if isinstance(protocol_version, int) else PROTOCOL_VERSION,
            segment_delay=_float(settings, "segment_delay"),
            crash_on=(
                {k: v for k, v in crash_on.items() if isinstance(v, str)}
                if isinstance(crash_on, dict)
                else {}
            ),
            hang_on=_strings(settings, "hang_on"),
            late_duplicates=_strings(settings, "late_duplicates"),
            ignore_shutdown=settings.get("ignore_shutdown") is True,
            payload_read_delay=_float(settings, "payload_read_delay"),
            stdout_lines=stdout_lines if isinstance(stdout_lines, int) else 0,
            stderr_text=stderr_text if isinstance(stderr_text, str) else "",
            progress_steps=progress_steps if isinstance(progress_steps, int) else 0,
        )


def _access_violation() -> NoReturn:
    """制造真实的访问违例。

    ``ctypes.string_at(0)`` 这类经由外部函数调用的读取会被 ctypes 的结构化异常处理接住，变成 OSError；
    ``c_char_p.value`` 在 C 代码里直接对指针调用 strlen，不经过那层保护，进程以 0xC0000005 结束。
    """
    _ = ctypes.c_char_p(1).value
    raise AssertionError("访问违例没有发生")


class FakeWorker:
    def __init__(self, conn: FrameConnection, behavior: Behavior) -> None:
        self.conn = conn
        self.behavior = behavior
        self.send_lock = threading.Lock()
        self.cond = threading.Condition()
        self.queue: deque[tuple[TranslateRequest | OcrRequest, bytes | None]] = deque()
        self.seen: list[str] = []

    def send(self, message: WorkerMessage) -> None:
        with self.send_lock:
            send_message(self.conn, message)

    def receive(self) -> tuple[WorkerMessage, bytes | None]:
        message = decode(self.conn.recv_bytes())
        payload = None
        if message.HAS_PAYLOAD:
            time.sleep(self.behavior.payload_read_delay)
            payload = self.conn.recv_bytes()
        return message, payload

    def serve(self) -> int:
        threading.Thread(target=self.process_loop, name="fake-worker-process", daemon=True).start()
        while True:
            try:
                message, payload = self.receive()
            except (EOFError, OSError):
                return 0
            if isinstance(message, TranslateRequest | OcrRequest):
                with self.cond:
                    self.seen.append(message.request_id)
                    self.queue.append((message, payload))
                    self.cond.notify()
            elif isinstance(message, Cancel):
                self.cancel(message.request_id)
            elif isinstance(message, Shutdown) and not self.behavior.ignore_shutdown:
                return 0

    def cancel(self, request_id: str) -> None:
        with self.cond:
            self.seen.append(f"cancel:{request_id}")
            for item in self.queue:
                if item[0].request_id == request_id:
                    self.queue.remove(item)
                    self.send(RequestFailed(request_id, CANCELLED, "取消"))
                    return

    def process_loop(self) -> None:
        while True:
            with self.cond:
                while not self.queue:
                    self.cond.wait()
                request, payload = self.queue.popleft()
            self.handle(request, payload)

    def handle(self, request: TranslateRequest | OcrRequest, payload: bytes | None) -> None:
        behavior = self.behavior
        request_id = request.request_id
        self.send(Started(request_id))
        action = behavior.crash_on.get(request_id)
        if action == "exit":
            sys.stderr.write(f"fake worker: crashing on {request_id}\n")
            sys.stderr.flush()
            os._exit(3)
        if action == "segfault":
            _access_violation()
        if request_id in behavior.hang_on:
            threading.Event().wait()
        if isinstance(request, OcrRequest):
            data = payload or b""
            time.sleep(behavior.segment_delay)
            self.send(OcrResult(request_id, [{"bytes": len(data), "crc32": zlib.crc32(data)}]))
        else:
            for segment in request.segments:
                time.sleep(behavior.segment_delay)
                if segment.text == SEEN_PROBE:
                    with self.cond:
                        text = ",".join(self.seen)
                else:
                    text = f"<{segment.text}>"
                self.send(SegmentResult(request_id, segment.id, text=text))
            self.send(RequestDone(request_id))
        if request_id in behavior.late_duplicates:
            self.send(SegmentResult(request_id, "late", text="late"))
            self.send(RequestDone(request_id))


def main(argv: list[str] | None = None) -> int:
    faulthandler.enable()
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True)
    parser.add_argument("--exit-code", type=int, default=None)
    parser.add_argument("--no-connect", action="store_true")
    args = parser.parse_args(argv)
    authkey = bytes.fromhex(sys.stdin.readline().strip())
    exit_code: int | None = args.exit_code
    if exit_code is not None:
        sys.stderr.write(f"fake worker: exiting with {exit_code} before connecting\n")
        return exit_code
    if args.no_connect:
        threading.Event().wait()

    address: str = args.address
    conn = Client(address, family="AF_PIPE", authkey=authkey)
    hello, _ = recv_message(conn)
    if not isinstance(hello, Hello) or hello.protocol_version != PROTOCOL_VERSION:
        return ExitCode.PROTOCOL_MISMATCH
    configure, _ = recv_message(conn)
    if not isinstance(configure, Configure):
        return ExitCode.CONFIG_ERROR
    behavior = Behavior.parse(configure.settings)
    if behavior.exit_on_configure is not None:
        sys.stderr.write(f"fake worker: exiting with {behavior.exit_on_configure} after configure\n")
        sys.stderr.flush()
        os._exit(behavior.exit_on_configure)
    for i in range(behavior.stdout_lines):
        sys.stdout.write(f"stdout line {i} {'x' * 100}\n")
    sys.stdout.flush()
    if behavior.stderr_text:
        sys.stderr.write(behavior.stderr_text + "\n")
        sys.stderr.flush()
    for step in range(behavior.progress_steps):
        send_message(conn, Progress("load", f"step {step}", step + 1, behavior.progress_steps))
    time.sleep(behavior.ready_delay)
    send_message(conn, Ready(behavior.protocol_version, behavior.capabilities, {"fake": "1"}))
    return FakeWorker(conn, behavior).serve()


if __name__ == "__main__":
    sys.exit(main())
