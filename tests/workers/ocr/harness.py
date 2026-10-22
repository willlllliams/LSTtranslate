"""测试辅助：在测试进程里扮演主进程，启动真实的 OCR 子进程并与它通信。

做法与 tests/workers/harness.py（NMT 进程）相同，区别在于：

- 解释器可选。真实 Paddle 后端的测试用 OCR 环境的 `.venv-ocr\\Scripts\\python.exe`，假后端的测试用
  当前解释器（主环境）。两种情况的工作目录都是仓库根目录，lst 包从工作目录导入。
- OCR 请求后跟一帧像素，ocr() 负责发送并等到这次请求的结束消息。
- render_document() 用 OCR 环境运行 render_document.py，得到合成文档截图的 BGRA 像素（主环境没有 Pillow）。

OcrWorkerProcess 是上下文管理器，退出时无论测试成败都结束子进程并等待它退出，不留残留进程。
`.venv-ocr\\Scripts\\python.exe` 是一个启动器，真正的解释器是它的子进程，并且在启动器的作业对象里
（架构文档 8.2.3 节），结束启动器时解释器随之结束；close() 之后再按命令行确认没有残留（leftover_workers）。
"""

import contextlib
import io
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from multiprocessing.connection import Client, Listener
from pathlib import Path
from types import TracebackType
from typing import Self, cast

from lst.contracts.worker_protocol import (
    PROTOCOL_VERSION,
    Configure,
    FrameConnection,
    Hello,
    JsonValue,
    OcrRequest,
    OcrResult,
    Progress,
    Ready,
    RequestFailed,
    WorkerMessage,
    recv_message,
    send_message,
)

ROOT = Path(__file__).resolve().parents[3]
OCR_PYTHON = ROOT / ".venv-ocr" / "Scripts" / "python.exe"
MODEL_CACHE = ROOT / "assets" / "PaddleOCR_and_PP-Structure"
OFFICIAL_MODELS = MODEL_CACHE / "official_models"
RENDER_SCRIPT = Path(__file__).with_name("render_document.py")
FAKE_BACKEND = "tests.workers.ocr.fake_backend:FakeOcrBackend"
PADDLE_BACKEND = "lst.workers.ocr_worker.paddle_backend:PaddleBackend"


def has_ocr_env() -> bool:
    return OCR_PYTHON.is_file()


class WorkerExited(Exception):
    """等待消息时子进程已经退出或连接已经断开。"""


class OcrWorkerProcess:
    """一个由测试驱动的 OCR 子进程。"""

    def __init__(
        self,
        backend: str | None = FAKE_BACKEND,
        *,
        python: Path | str | None = None,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        command: Sequence[str] | None = None,
    ) -> None:
        """command 不为 None 时代替 ``python -m lst.workers.ocr_worker``，例如经安装版启动器运行。"""
        self._command = None if command is None else list(command)
        self.authkey = secrets.token_bytes(32)
        self.address = rf"\\.\pipe\lst-test-ocr-{os.getpid()}-{secrets.token_hex(8)}"
        self._python = str(python or sys.executable)
        self._backend = backend
        self._extra_args = list(extra_args)
        self._env = {**os.environ, **(env or {})}
        self._listener: Listener | None = None
        self._accepted: list[FrameConnection] = []
        self._accept_thread: threading.Thread | None = None
        self.conn: FrameConnection | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self._stderr = bytearray()
        self._stderr_thread: threading.Thread | None = None
        self.progress: list[Progress] = []

    # ---- 启动与结束

    def start(self, *, connect_timeout: float = 60.0) -> Self:
        """启动子进程并等待它连上；子进程在连接前退出时不报错，由测试检查退出码。"""
        self._listener = Listener(self.address, family="AF_PIPE", authkey=self.authkey)
        self._accept_thread = threading.Thread(target=self._accept, daemon=True)
        self._accept_thread.start()
        entry = self._command or [self._python, "-m", "lst.workers.ocr_worker"]
        args = [*entry, "--address", self.address, *self._extra_args]
        if self._backend is not None:
            args += ["--backend", self._backend]
        self.proc = subprocess.Popen(
            args,
            cwd=ROOT,
            env=self._env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(self.authkey.hex().encode() + b"\n")
            self.proc.stdin.close()
        except OSError:
            pass  # 子进程已经退出
        deadline = time.monotonic() + connect_timeout
        while self._accept_thread.is_alive() and self.proc.poll() is None and time.monotonic() < deadline:
            self._accept_thread.join(0.02)
        if self._accepted:
            self.conn = self._accepted[0]
        return self

    def _accept(self) -> None:
        assert self._listener is not None
        with contextlib.suppress(Exception):
            self._accepted.append(self._listener.accept())

    def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        stream = cast(io.BufferedReader, self.proc.stderr)
        while chunk := stream.read1(4096):
            self._stderr.extend(chunk)

    def close(self) -> None:
        """结束子进程并等待退出，关闭连接和监听端。可以重复调用。"""
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=30)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5)
        if self.conn is not None:
            self._close_conn(self.conn)
            self.conn = None
        if self._accept_thread is not None and self._accept_thread.is_alive():
            # 子进程没连上就退出了，accept 还阻塞着：自己连一次让它返回。
            with contextlib.suppress(Exception):
                Client(self.address, authkey=self.authkey).close()
            self._accept_thread.join(timeout=5)
            for conn in self._accepted:
                self._close_conn(conn)
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    @staticmethod
    def _close_conn(conn: FrameConnection) -> None:
        close: Callable[[], None] | None = getattr(conn, "close", None)
        if close is not None:
            with contextlib.suppress(OSError):
                close()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    # ---- 收发

    def send(self, message: WorkerMessage, payload: bytes | None = None) -> None:
        assert self.conn is not None, "子进程没有连上：\n" + self.stderr
        send_message(self.conn, message, payload)

    def recv(self, timeout: float = 10.0) -> WorkerMessage:
        """接收一条消息；超时抛 TimeoutError，连接断开抛 WorkerExited。"""
        assert self.conn is not None, "子进程没有连上：\n" + self.stderr
        poll: Callable[[float], bool] = getattr(self.conn, "poll")  # noqa: B009
        try:
            ready = poll(timeout)
            message = recv_message(self.conn)[0] if ready else None
        except (EOFError, OSError) as exc:
            raise WorkerExited(str(exc)) from exc
        if message is None:
            raise TimeoutError(f"{timeout} 秒内没有收到消息")
        return message

    def recv_until(self, done: Callable[[WorkerMessage], bool], timeout: float = 10.0) -> list[WorkerMessage]:
        """接收消息直到 done(消息) 为真，返回收到的全部消息（含最后一条）。"""
        deadline = time.monotonic() + timeout
        received: list[WorkerMessage] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{timeout} 秒内没有等到期望的消息，已收到：{received}")
            message = self.recv(remaining)
            received.append(message)
            if done(message):
                return received

    def handshake(self, settings: Mapping[str, JsonValue] | None = None, timeout: float = 30.0) -> Ready:
        """发送 Hello 和 Configure，等待 Ready（期间的 Progress 记在 self.progress 里）。"""
        self.send(Hello(PROTOCOL_VERSION))
        return self.configure(settings or {}, timeout)

    def configure(self, settings: Mapping[str, JsonValue], timeout: float = 30.0) -> Ready:
        self.send(Configure(dict(settings)))
        messages = self.recv_until(lambda m: isinstance(m, Ready), timeout)
        self.progress = [m for m in messages if isinstance(m, Progress)]
        others = [m for m in messages[:-1] if not isinstance(m, Progress)]
        assert not others, f"Ready 之前收到了意外的消息：{others}"
        ready = messages[-1]
        assert isinstance(ready, Ready)
        return ready

    def ocr(self, request: OcrRequest, payload: bytes, timeout: float = 30.0) -> OcrResult | RequestFailed:
        """发送一个 OCR 请求，返回它的结束消息（OcrResult 或 RequestFailed）。"""
        rid = request.request_id
        self.send(request, payload)

        def finished(message: WorkerMessage) -> bool:
            return isinstance(message, OcrResult | RequestFailed) and message.request_id == rid

        reply = self.recv_until(finished, timeout)[-1]
        assert isinstance(reply, OcrResult | RequestFailed)
        return reply

    # ---- 进程状态

    def wait_exit(self, timeout: float = 30.0) -> int:
        """等待子进程退出并返回退出码；超时抛 subprocess.TimeoutExpired。"""
        assert self.proc is not None
        code = self.proc.wait(timeout=timeout)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5)
        return code

    @property
    def stderr(self) -> str:
        return self._stderr.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 辅助


def render_document(spec: Mapping[str, object]) -> bytes:
    """用 OCR 环境渲染页面描述，返回 BGRA 像素（stride = width × 4）。"""
    result = subprocess.run(
        [str(OCR_PYTHON), str(RENDER_SCRIPT)],
        input=json.dumps(spec, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        cwd=ROOT,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    width, height = cast(int, spec["width"]), cast(int, spec["height"])
    assert len(result.stdout) == width * height * 4
    return result.stdout


def pad_rows(pixels: bytes, width: int, height: int, padding: int) -> tuple[bytes, int]:
    """给每行像素末尾补 padding 字节的填充，返回（新像素帧，stride）。"""
    row = width * 4
    stride = row + padding
    out = bytearray(stride * height)
    for y in range(height):
        out[y * stride : y * stride + row] = pixels[y * row : (y + 1) * row]
    return bytes(out), stride


def leftover_workers(address: str | None = None) -> list[str]:
    """本机上仍在运行的 OCR 子进程的命令行。

    address 给出时只找连到这个管道的进程，否则找本测试进程启动的全部 OCR 子进程（管道名前缀相同）。
    """
    marker = address or f"lst-test-ocr-{os.getpid()}-"
    script = "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | ForEach-Object { $_.CommandLine }"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    return [
        line for line in result.stdout.splitlines() if "lst.workers.ocr_worker" in line and marker in line
    ]
