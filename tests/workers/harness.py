"""测试辅助：在测试进程里扮演主进程，启动真实的 NMT 子进程并与它通信。

- 测试进程建一个 multiprocessing.connection.Listener（命名管道，地址随机生成，带 authkey）；
- 用 subprocess 启动 `python -m lst.workers.nmt_worker --address ... --backend ...`，cwd 为仓库根目录，
  authkey 以十六进制写进子进程的标准输入；
- 子进程的标准错误由后台线程持续读走，子进程不会因为写满管道而阻塞；
- 接收消息用 poll 加超时，在测试线程里进行。子进程有专门的接收线程一直在读管道，测试线程发出的小消息
  不会阻塞，所以收发放在同一个线程里也不会互相等待。

WorkerProcess 是上下文管理器，退出时无论测试成败都结束子进程并等待它退出，不留残留进程。
"""

import contextlib
import io
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
    Progress,
    Ready,
    WorkerMessage,
    recv_message,
    send_message,
)

ROOT = Path(__file__).resolve().parents[2]
FAKE_BACKEND = "tests.workers.fake_backend:FakeBackend"
REAL_BACKEND = "lst.workers.nmt_worker.ct2_backend:Ct2Backend"


class WorkerExited(Exception):
    """等待消息时子进程已经退出或连接已经断开。"""


class WorkerProcess:
    """一个由测试驱动的 NMT 子进程。"""

    def __init__(
        self,
        backend: str | None = FAKE_BACKEND,
        *,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        stdin_line: bytes | None = None,
        child_authkey: bytes | None = None,
        address: str | None = None,
    ) -> None:
        """
        stdin_line 覆盖写进标准输入的内容（默认是 authkey 的十六进制加换行）；
        child_authkey 让子进程拿到与监听端不同的 authkey；
        address 为 None 时随机生成管道地址并监听，否则不建监听端（用于子进程连接之前就该退出的场景）。
        """
        self.authkey = secrets.token_bytes(32)
        self._listen = address is None
        self.address = address or rf"\\.\pipe\lst-test-nmt-{os.getpid()}-{secrets.token_hex(8)}"
        self._backend = backend
        self._extra_args = list(extra_args)
        self._env = {**os.environ, **(env or {})}
        key = child_authkey if child_authkey is not None else self.authkey
        self._stdin_line = stdin_line if stdin_line is not None else key.hex().encode() + b"\n"
        self._listener: Listener | None = None
        self._accepted: list[FrameConnection] = []
        self._accept_thread: threading.Thread | None = None
        self.conn: FrameConnection | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self._stderr = bytearray()
        self._stderr_thread: threading.Thread | None = None
        self.progress: list[Progress] = []

    # ---- 启动与结束

    def start(self, *, connect_timeout: float = 20.0) -> Self:
        """启动子进程；需要连接时等待它连上，子进程在连接前退出时不报错，由测试检查退出码。"""
        if self._listen:
            self._listener = Listener(self.address, family="AF_PIPE", authkey=self.authkey)
            self._accept_thread = threading.Thread(target=self._accept, daemon=True)
            self._accept_thread.start()
        args = [sys.executable, "-m", "lst.workers.nmt_worker", "--address", self.address, *self._extra_args]
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
            self.proc.stdin.write(self._stdin_line)
            self.proc.stdin.close()
        except OSError:
            pass  # 子进程已经退出
        if self._accept_thread is not None:
            deadline = time.monotonic() + connect_timeout
            while self._accept_thread.is_alive() and self.proc.poll() is None and time.monotonic() < deadline:
                self._accept_thread.join(0.02)
            if self._accepted:
                self.conn = self._accepted[0]
        return self

    def _accept(self) -> None:
        assert self._listener is not None
        # authkey 不符等情况下连接失败，由测试检查子进程的退出码。
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

    def close_connection(self) -> None:
        """只关闭主进程一端的连接，模拟主进程消失；子进程应自行退出。"""
        if self.conn is not None:
            self._close_conn(self.conn)
            self.conn = None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    # ---- 收发

    def send(self, message: WorkerMessage, payload: bytes | None = None) -> None:
        assert self.conn is not None, "子进程没有连上"
        send_message(self.conn, message, payload)

    def recv(self, timeout: float = 10.0) -> WorkerMessage:
        """接收一条消息；超时抛 TimeoutError，连接断开抛 WorkerExited。"""
        assert self.conn is not None, "子进程没有连上"
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

    def handshake(self, settings: Mapping[str, JsonValue] | None = None, timeout: float = 20.0) -> Ready:
        """发送 Hello 和 Configure，等待 Ready（期间的 Progress 记在 self.progress 里）。"""
        self.send(Hello(PROTOCOL_VERSION))
        return self.configure(settings or {}, timeout)

    def configure(self, settings: Mapping[str, JsonValue], timeout: float = 20.0) -> Ready:
        self.send(Configure(dict(settings)))
        messages = self.recv_until(lambda m: isinstance(m, Ready), timeout)
        self.progress = [m for m in messages if isinstance(m, Progress)]
        others = [m for m in messages[:-1] if not isinstance(m, Progress)]
        assert not others, f"Ready 之前收到了意外的消息：{others}"
        ready = messages[-1]
        assert isinstance(ready, Ready)
        return ready

    # ---- 进程状态

    def wait_exit(self, timeout: float = 10.0) -> int:
        """等待子进程退出并返回退出码；超时抛 subprocess.TimeoutExpired。"""
        assert self.proc is not None
        code = self.proc.wait(timeout=timeout)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5)
        return code

    @property
    def stderr(self) -> str:
        return self._stderr.decode("utf-8", errors="replace")
