"""推理子进程共用的运行时（架构文档 2.8、2.9.4、3、8.1 节）。

NMT 进程和 OCR 进程都由这里驱动，各自只提供一个“处理器”（RequestHandler）：
处理器负责加载推理后端、处理一种请求，运行时负责启动参数、日志、握手、排队、取消和退出。

一个推理进程的生命周期：

1. 启动参数只有 `--address <命名管道地址>`（各进程可以追加自己的参数）。authkey 以十六进制
   从标准输入的第一行读取，读完关闭标准输入；authkey 不出现在命令行里，进程列表看不到它。
2. 启用 faulthandler（原生库崩溃时把各线程的 Python 堆栈写到标准错误），日志也写标准错误，
   由主进程的监管器收集。标准错误统一用 UTF-8 编码。
3. 连接主进程，先收 Hello：协议版本不符时以 ExitCode.PROTOCOL_MISMATCH 退出。
4. 之后启动接收线程。Configure 和请求都排进同一个先进先出队列，由推理线程（进程主线程）按顺序处理：
   Configure 调用处理器加载，成功后发 Ready；请求先发 Started，再交给处理器。
5. Cancel 只移除还在队列里的请求并回复 RequestFailed(cancelled)；正在处理的请求照常跑完。
   Shutdown 之后不再接收消息，队列里剩下的请求回复 cancelled，当前请求结束后以 0 退出。
   连接断开说明主进程已经不在了，立即退出。

加载失败的退出码：处理器抛 ImportError 或 OSError（例如 DLL 加载失败）→ DEPENDENCY_ERROR；
抛 ConfigError → CONFIG_ERROR；其他异常按崩溃处理（退出码 1），由监管器退避重启。
运行中重新加载失败同样按这三种情况退出：进程此时已经卸载了旧模型，留着它只会让后续请求全部失败。
"""

from __future__ import annotations

import argparse
import binascii
import faulthandler
import importlib
import io
import logging
import os
import sys
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
from typing import NoReturn, Protocol

from lst.contracts.errors import ErrorCode
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
    Progress,
    ProtocolError,
    Ready,
    RequestFailed,
    Shutdown,
    Started,
    TranslateRequest,
    WorkerMessage,
    WorkerReply,
    recv_message,
    send_message,
)

log = logging.getLogger("lst.workers")

# 崩溃（非确定性故障）的退出码，监管器按退避重启处理。
EXIT_CRASH = 1

type WorkerRequest = TranslateRequest | OcrRequest
type Emit = Callable[[WorkerMessage], None]


class ConfigError(Exception):
    """Configure 中的配置无效。进程以 ExitCode.CONFIG_ERROR 退出，重启也不会好转。"""


class ProgressReporter(Protocol):
    """加载期间报告进度，每次调用发送一条 Progress 消息。"""

    def __call__(
        self, kind: str, detail: str = "", done: int | None = None, total: int | None = None
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class LoadResult:
    """处理器加载完成后的结果，原样放进 Ready 消息。"""

    capabilities: dict[str, JsonValue]
    versions: dict[str, str]


class RequestHandler[R: WorkerRequest](Protocol):
    """推理进程的处理器：加载后端、处理一种请求。

    两个方法都只在推理线程上调用，处理器内部不需要加锁。
    """

    @property
    def request_type(self) -> type[R]:
        """本进程处理的请求类型（TranslateRequest 或 OcrRequest）。收到其他类型的请求时回复 RequestFailed。"""
        ...

    def load(self, settings: Mapping[str, JsonValue], progress: ProgressReporter) -> LoadResult:
        """按 Configure 的设置加载（或重新加载）后端。

        抛 ConfigError 表示配置无效，抛 ImportError 或 OSError 表示推理依赖导入失败。
        """
        ...

    def handle(self, request: R, payload: bytes | None, emit: Emit) -> WorkerReply:
        """处理一个请求。

        中间结果（例如每段的 SegmentResult）通过 emit 发送；返回值是结束消息（RequestDone、OcrResult
        或 RequestFailed），由运行时发送。抛出的异常由运行时记为 RequestFailed(engine.internal)。
        """
        ...


# ---------------------------------------------------------------- 进程初始化


def setup_process(name: str, level: int | str = logging.INFO) -> None:
    """进程启动后第一件事：标准错误改为 UTF-8，启用 faulthandler，日志写标准错误。

    之后的任何失败（包括导入推理库失败）都能在标准错误里留下记录。
    """
    stderr = sys.stderr
    if isinstance(stderr, io.TextIOWrapper):
        # 被重定向到管道时，Windows 上的默认编码是 ANSI 代码页，中文日志会写出乱码或抛编码错误。
        stderr.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
    _disable_error_reporting()
    faulthandler.enable(file=sys.stderr, all_threads=True)
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format=f"%(asctime)s %(levelname)s [{name}:%(process)d] %(name)s: %(message)s",
        force=True,
    )


def _disable_error_reporting() -> None:
    """关闭本进程的 Windows 错误报告（Windows Error Reporting，WER）。

    系统开启 WER 时，原生库段错误后 WER 会接管崩溃的进程，收集转储或弹出“已停止工作”对话框，
    进程要等它处理完才退出，监管器的崩溃检测随之推迟。关闭后崩溃直接表现为进程退出；
    faulthandler 的堆栈由向量化异常处理程序写出，不受影响。
    """
    if sys.platform != "win32":
        return
    import ctypes

    sem_failcriticalerrors, sem_nogpfaulterrorbox, sem_noopenfileerrorbox = 0x0001, 0x0002, 0x8000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    mode: int = kernel32.GetErrorMode()
    kernel32.SetErrorMode(mode | sem_failcriticalerrors | sem_nogpfaulterrorbox | sem_noopenfileerrorbox)


class _ArgumentParser(argparse.ArgumentParser):
    """启动参数无效时以 CONFIG_ERROR 退出，而不是 argparse 默认的退出码 2。"""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        log.error("启动参数无效：%s", message)
        exit_process(ExitCode.CONFIG_ERROR)


def parse_args(
    argv: Sequence[str] | None = None,
    add_arguments: Callable[[argparse.ArgumentParser], None] | None = None,
) -> argparse.Namespace:
    """解析启动参数。参数无效时以 CONFIG_ERROR 退出。"""
    parser = _ArgumentParser(add_help=False)
    parser.add_argument("--address", required=True, help="主进程命名管道的地址")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    if add_arguments is not None:
        add_arguments(parser)
    return parser.parse_args(argv)


def load_backend(spec: str) -> object:
    """按 `module:attr` 导入后端工厂并调用它，返回后端实例（各进程入口的 --backend 参数）。

    `attr` 是一个无参可调用对象。格式不对或找不到可调用的属性时以 CONFIG_ERROR 退出；
    导入模块失败（ImportError，或 DLL 加载失败的 OSError）时以 DEPENDENCY_ERROR 退出。
    返回值的类型由调用方按自己的后端协议标注。
    """
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        log.error("--backend 应为 module:attr 格式，实际为 %r", spec)
        exit_process(ExitCode.CONFIG_ERROR)
    try:
        module = importlib.import_module(module_name)
    except (ImportError, OSError) as exc:
        log.error("无法导入后端模块 %s：%s", module_name, exc)
        exit_process(ExitCode.DEPENDENCY_ERROR)
    factory = getattr(module, attr, None)
    if not callable(factory):
        log.error("后端模块 %s 中没有可调用的 %s", module_name, attr)
        exit_process(ExitCode.CONFIG_ERROR)
    return factory()


def read_authkey() -> bytes:
    """从标准输入第一行读取十六进制 authkey，然后关闭标准输入。格式不对时以 CONFIG_ERROR 退出。"""
    line = sys.stdin.buffer.readline() if sys.stdin is not None else b""
    _close_stdin()
    try:
        key = binascii.unhexlify(line.strip())
    except (binascii.Error, ValueError):
        log.error("标准输入第一行不是十六进制 authkey")
        exit_process(ExitCode.CONFIG_ERROR)
    if not key:
        log.error("没有从标准输入读到 authkey")
        exit_process(ExitCode.CONFIG_ERROR)
    return key


def _close_stdin() -> None:
    # sys.stdin 不拥有 0 号文件描述符，只关它不会释放管道。把 0 号描述符换成 NUL，原来的管道句柄随之关闭。
    if sys.stdin is not None:
        sys.stdin.close()
    try:
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
    except OSError:
        pass


def exit_process(code: int) -> NoReturn:
    """刷新日志后立即结束进程。

    不走解释器的正常退出流程：接收线程可能还阻塞在管道读取上，推理库的全局析构也可能在退出时出问题，
    这些都不应该影响退出码。
    """
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(int(code))


# ---------------------------------------------------------------- 发送与排队


class Channel:
    """对连接的发送端加锁。send_message 要求同一连接上的发送串行进行，否则帧会交错。

    发送失败说明主进程已经不在了，直接结束进程。
    """

    def __init__(self, conn: FrameConnection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    def send(self, message: WorkerMessage) -> None:
        try:
            with self._lock:
                send_message(self._conn, message)
        except (OSError, EOFError) as exc:
            log.warning("向主进程发送消息失败，进程退出：%s", exc)
            exit_process(ExitCode.OK)


@dataclass(frozen=True, slots=True)
class _ConfigureJob:
    settings: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _RequestJob[R: WorkerRequest]:
    request: R
    payload: bytes | None


class _StopJob:
    """队列结束标记：Shutdown 之后放入，推理线程取到它就退出。"""


type _Job[R: WorkerRequest] = _ConfigureJob | _RequestJob[R] | _StopJob


class WorkQueue[R: WorkerRequest]:
    """先进先出的任务队列，支持按请求号移除。接收线程放入，推理线程取出。"""

    def __init__(self) -> None:
        self._jobs: deque[_Job[R]] = deque()
        self._cond = threading.Condition()
        self._closed = False

    def put(self, job: _Job[R]) -> bool:
        """放入一个任务；队列已关闭（收到过 Shutdown）时返回 False。"""
        with self._cond:
            if self._closed:
                return False
            self._jobs.append(job)
            self._cond.notify()
            return True

    def get(self) -> _Job[R]:
        """阻塞取出队首任务。"""
        with self._cond:
            while not self._jobs:
                self._cond.wait()
            return self._jobs.popleft()

    def remove(self, request_id: str) -> _RequestJob[R] | None:
        """移除队列中请求号为 request_id 的请求；不在队列里（已开始或未知）时返回 None。"""
        with self._cond:
            for job in self._jobs:
                if isinstance(job, _RequestJob) and job.request.request_id == request_id:
                    self._jobs.remove(job)
                    return job
            return None

    def close(self) -> list[_RequestJob[R]]:
        """关闭队列：之后的 put 一律拒绝；取走剩下的全部请求并返回，丢弃排队的 Configure。"""
        with self._cond:
            self._closed = True
            pending = [job for job in self._jobs if isinstance(job, _RequestJob)]
            self._jobs.clear()
            return pending

    def stop(self) -> None:
        """放入结束标记，推理线程取到它就退出。"""
        with self._cond:
            self._jobs.append(_StopJob())
            self._cond.notify()


# ---------------------------------------------------------------- 主循环


class WorkerRuntime[R: WorkerRequest]:
    """一个已连接的推理进程：接收线程读消息，推理线程（调用 serve 的线程）按顺序处理。"""

    def __init__(self, conn: FrameConnection, handler: RequestHandler[R]) -> None:
        self._conn = conn
        self._handler = handler
        self._channel = Channel(conn)
        self._queue: WorkQueue[R] = WorkQueue()
        self._loaded = False

    # ---- 接收线程

    def _receive_loop(self) -> None:
        while True:
            try:
                message, payload = recv_message(self._conn)
            except (EOFError, OSError) as exc:
                log.warning("与主进程的连接已断开，进程退出：%s", exc or type(exc).__name__)
                exit_process(ExitCode.OK)
            except ProtocolError as exc:
                log.error("收到不符合协议的消息：%s", exc)
                exit_process(ExitCode.PROTOCOL_MISMATCH)
            if not self._dispatch(message, payload):
                return

    def _dispatch(self, message: WorkerMessage, payload: bytes | None) -> bool:
        """处理一条收到的消息；返回 False 表示不再接收（收到了 Shutdown）。"""
        if isinstance(message, self._handler.request_type):
            self._queue.put(_RequestJob(message, payload))
        elif isinstance(message, Configure):
            self._queue.put(_ConfigureJob(message.settings))
        elif isinstance(message, Cancel):
            job = self._queue.remove(message.request_id)
            if job is not None:
                log.debug("请求 %s 在排队时被取消", message.request_id)
                self._channel.send(RequestFailed(message.request_id, CANCELLED, "排队时被取消"))
        elif isinstance(message, Shutdown):
            log.info("收到 Shutdown，处理完当前请求后退出")
            for job in self._queue.close():
                self._channel.send(RequestFailed(job.request.request_id, CANCELLED, "进程正在退出"))
            # 取消回复全部发出之后才放结束标记，否则推理线程可能先一步结束进程。
            self._queue.stop()
            return False
        elif isinstance(message, TranslateRequest | OcrRequest):
            log.error("本进程不处理 %s 请求", message.TYPE)
            self._channel.send(
                RequestFailed(message.request_id, ErrorCode.BAD_REQUEST, f"本进程不处理 {message.TYPE} 请求")
            )
        else:
            log.warning("忽略不该由主进程发来的消息：%s", message.TYPE)
        return True

    # ---- 推理线程

    def serve(self) -> int:
        """启动接收线程并处理任务，直到 Shutdown。返回退出码。"""
        receiver = threading.Thread(target=self._receive_loop, name="receiver", daemon=True)
        receiver.start()
        while True:
            job = self._queue.get()
            if isinstance(job, _StopJob):
                return ExitCode.OK
            if isinstance(job, _ConfigureJob):
                code = self._load(job.settings)
                if code is not None:
                    return code
            else:
                self._process(job)

    def _load(self, settings: dict[str, JsonValue]) -> int | None:
        """加载或重新加载后端；成功时发 Ready 并返回 None，失败时返回退出码。"""

        def progress(kind: str, detail: str = "", done: int | None = None, total: int | None = None) -> None:
            self._channel.send(Progress(kind, detail, done, total))

        self._loaded = False
        try:
            result = self._handler.load(settings, progress)
        except ConfigError as exc:
            log.error("配置无效：%s", exc)
            return ExitCode.CONFIG_ERROR
        except (ImportError, OSError) as exc:
            log.error("推理依赖加载失败：%s: %s", type(exc).__name__, exc)
            return ExitCode.DEPENDENCY_ERROR
        except Exception:
            log.exception("加载时出现未预期的异常")
            return EXIT_CRASH
        self._loaded = True
        self._channel.send(Ready(PROTOCOL_VERSION, result.capabilities, result.versions))
        log.info("加载完成，已发送 Ready")
        return None

    def _process(self, job: _RequestJob[R]) -> None:
        request_id = job.request.request_id
        self._channel.send(Started(request_id))
        if not self._loaded:
            self._channel.send(RequestFailed(request_id, ErrorCode.INTERNAL, "进程尚未完成配置"))
            return
        try:
            reply = self._handler.handle(job.request, job.payload, self._channel.send)
        except Exception as exc:
            log.exception("处理请求 %s 时出现未预期的异常", request_id)
            reply = RequestFailed(request_id, ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}")
        self._channel.send(reply)


def handshake(conn: FrameConnection) -> None:
    """接收 Hello 并比较协议版本；不符时以 PROTOCOL_MISMATCH 退出。"""
    try:
        message, _ = recv_message(conn)
    except (EOFError, OSError) as exc:
        log.warning("握手前连接已断开：%s", exc or type(exc).__name__)
        exit_process(ExitCode.OK)
    except ProtocolError as exc:
        log.error("握手消息不符合协议：%s", exc)
        exit_process(ExitCode.PROTOCOL_MISMATCH)
    if not isinstance(message, Hello):
        log.error("第一条消息应为 hello，实际为 %s", message.TYPE)
        exit_process(ExitCode.PROTOCOL_MISMATCH)
    if message.protocol_version != PROTOCOL_VERSION:
        log.error("协议版本不符：主进程 %s，本进程 %s", message.protocol_version, PROTOCOL_VERSION)
        exit_process(ExitCode.PROTOCOL_MISMATCH)


def connect(address: str, authkey: bytes) -> FrameConnection:
    """连接主进程的命名管道。authkey 不符时以 CONFIG_ERROR 退出，连不上时按崩溃退出。"""
    try:
        return Client(address, authkey=authkey)
    except AuthenticationError as exc:
        log.error("authkey 校验失败：%s", exc)
        exit_process(ExitCode.CONFIG_ERROR)
    except OSError as exc:
        log.error("连接主进程失败：%s", exc)
        exit_process(EXIT_CRASH)


def run_worker[R: WorkerRequest](handler: RequestHandler[R], args: argparse.Namespace) -> NoReturn:
    """连接主进程并运行到结束，最后以相应的退出码结束进程。

    调用前应已执行 setup_process 和 parse_args。
    """
    authkey = read_authkey()
    address: str = args.address
    conn = connect(address, authkey)
    handshake(conn)
    code = WorkerRuntime(conn, handler).serve()
    log.info("进程退出，退出码 %d", code)
    exit_process(code)
