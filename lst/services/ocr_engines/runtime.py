r"""需要下载的 OCR 引擎的运行环境安装：uv 命令行、环境变量、外部命令运行器与失败分类。

安装版里没有 pip，运行环境用随安装包分发的 ``tools\uv.exe`` 安装：

    uv.exe pip install --python runtime\python.exe --target <site.part> --no-deps
        --no-build [--require-hashes] [--index-url <镜像>] -r app\ocr-<引擎>-<版本>-requirements.txt

- ``--no-build``：只装预编译的 wheel。从源码包构建要执行包里的构建脚本，用户机器上也没有编译器；
  清单里某个包没有 Windows wheel 时安装直接失败，而不是尝试构建；
- ``--no-deps``：版本清单是构建时按 .venv-ocr 冻结（GPU 版另外解析过推理库的依赖）的完整清单，不再解析依赖，
  装出的版本与开发环境一致；清单带 RESOLVE_MARKER 时例外，见该常量；
- ``--require-hashes``：清单带 ``--hash=`` 时加上，任何一个包的哈希不符都整体失败；
- ``--no-config``：不读用户机器上的 uv.toml / pyproject.toml，安装结果只由命令行决定；
- 缓存目录 ``UV_CACHE_DIR`` 指向引擎目录下的 uv-cache：一来与运行环境在同一个卷上，uv 可以用硬链接；
  二来模块按它的大小估计运行环境阶段的进度；装完整个删除。

外部命令通过 ``CommandRunner`` 执行，测试注入假运行器。默认的 ``run_command`` 在线程里调用
subprocess，而不用 asyncio 的子进程接口：主程序的事件循环是 qasync，它在 Windows 上不保证支持子进程。
"""

import asyncio
import os
import subprocess
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from lst.contracts.ocr_engines import OcrEngineErrorCode
from lst.services._shared.download import file_sha256
from lst.services.ocr_engines.net import scrub

__all__ = [
    "RESOLVE_MARKER",
    "CommandResult",
    "CommandRunner",
    "classify_uv_failure",
    "environment_from_os",
    "failure_detail",
    "file_sha256",
    "requires_hashes",
    "requires_resolution",
    "run_command",
    "uv_environment",
    "uv_install_args",
]

OUTPUT_TAIL_LINES: Final = 40
"""外部命令的输出只保留最后这么多行，用于失败时的技术细节。"""

KILL_WAIT_S: Final = 10.0
"""取消外部命令后，最多等收集输出的线程这么久。"""

_DROPPED_ENV_PREFIXES: Final = ("UV_", "PYTHON", "VIRTUAL_ENV", "CONDA_", "PIP_")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """外部命令的结果：退出码与合并后的标准输出、标准错误的末尾若干行。"""

    returncode: int
    output: str = ""


type CommandRunner = Callable[[Sequence[str], Mapping[str, str]], Awaitable[CommandResult]]
"""执行外部命令：参数是完整的命令行与环境变量，返回结果。被取消时必须先结束子进程再把取消传出去。"""


def _creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class _ProcessTree:
    """一个子进程及其全部后代。

    Windows 上把子进程放进一个专用的作业对象，结束时用 TerminateJobObject 一次结束整棵进程树：
    只结束 uv 本身时，它启动的子进程仍持有输出管道，收集输出的线程会一直等下去。
    作业对象建不起来（例如权限受限）或不在 Windows 上时退回只结束子进程本身。
    子进程在放进作业对象之前启动的后代不在作业里；uv 只在安装过程中才启动子进程，这个窗口可以忽略。
    """

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._job: int | None = None
        if sys.platform == "win32":
            self._job = _assign_job(process)

    def kill(self) -> None:
        if self._job is not None and _terminate_job(self._job):
            return
        self._process.kill()

    def close(self) -> None:
        if self._job is not None:
            _close_handle(self._job)
            self._job = None


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    def _assign_job(process: subprocess.Popen[bytes]) -> int | None:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        # Popen 不公开进程句柄；Windows 上的实现把它存在 _handle 里。
        handle = int(process._handle)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]
        if not _kernel32.AssignProcessToJobObject(job, handle):
            _kernel32.CloseHandle(job)
            return None
        return int(job)

    def _terminate_job(job: int) -> bool:
        return bool(_kernel32.TerminateJobObject(job, 1))

    def _close_handle(handle: int) -> None:
        _kernel32.CloseHandle(handle)

else:

    def _assign_job(process: subprocess.Popen[bytes]) -> int | None:
        return None

    def _terminate_job(job: int) -> bool:
        return False

    def _close_handle(handle: int) -> None:
        return None


async def run_command(args: Sequence[str], env: Mapping[str, str]) -> CommandResult:
    """默认的运行器：在线程里运行子进程并收集输出。

    被取消时结束整棵进程树（见 _ProcessTree），等收集输出的线程退出后再把取消传出去；
    线程在 KILL_WAIT_S 秒内仍没有退出时不再等它，取消照常传出。
    """
    process = subprocess.Popen(  # 命令行由本模块按固定格式拼出
        list(args),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=_creation_flags(),
    )
    tree = _ProcessTree(process)
    tail: deque[str] = deque(maxlen=OUTPUT_TAIL_LINES)

    def collect() -> int:
        assert process.stdout is not None
        for raw in process.stdout:
            tail.append(raw.decode("utf-8", errors="replace").rstrip())
        return process.wait()

    future = asyncio.ensure_future(asyncio.to_thread(collect))
    try:
        returncode = await asyncio.shield(future)
    except asyncio.CancelledError:
        tree.kill()
        done, _ = await asyncio.wait({future}, timeout=KILL_WAIT_S)
        if done and not future.cancelled():
            future.exception()
        raise
    finally:
        tree.close()
    return CommandResult(returncode, "\n".join(tail))


RESOLVE_MARKER: Final = "# lst: resolve-dependencies"
"""版本清单里这一行表示安装时要解析依赖（不加 --no-deps）。

构建机离线、无法在构建时解析 GPU 版依赖（CUDA 运行库）时，scripts/build_installer.py 写出不带哈希、带这一行的
清单：清单里的包仍是精确版本，uv 只补上清单没有列出的依赖（例如 paddlepaddle-gpu 需要的 nvidia-*-cu12）。
"""


def requires_hashes(requirements: str) -> bool:
    """版本清单是否带哈希（uv pip compile --generate-hashes 的输出带 ``--hash=sha256:``）。"""
    return "--hash=" in requirements


def requires_resolution(requirements: str) -> bool:
    """版本清单是否要求安装时解析依赖（带 RESOLVE_MARKER 这一行）。"""
    return any(line.strip() == RESOLVE_MARKER for line in requirements.splitlines())


def uv_install_args(
    uv: Path,
    python: Path,
    target: Path,
    requirements: Path,
    *,
    hashed: bool,
    index_url: str | None = None,
    resolve: bool = False,
) -> list[str]:
    """安装运行环境的 uv 命令行，见模块说明。resolve 为真时不加 --no-deps（见 RESOLVE_MARKER）。"""
    args = [
        str(uv), "pip", "install",
        "--no-config", "--no-progress", "--color", "never",
        "--python", str(python),
        "--target", str(target),
        *(() if resolve else ("--no-deps",)), "--no-build",
    ]  # fmt: skip
    if hashed:
        args.append("--require-hashes")
    if index_url:
        args += ["--index-url", index_url]
    args += ["-r", str(requirements)]
    return args


def uv_environment(base: Mapping[str, str], cache_dir: Path) -> dict[str, str]:
    """uv 的环境变量：去掉会改变 uv 或 Python 行为的变量（UV_*、PYTHON*、虚拟环境、conda、pip），
    保留代理等其余变量，再指定缓存目录、禁止下载解释器。"""
    env = {key: value for key, value in base.items() if not key.upper().startswith(_DROPPED_ENV_PREFIXES)}
    env["UV_CACHE_DIR"] = str(cache_dir)
    env["UV_PYTHON_DOWNLOADS"] = "never"
    return env


_DISK_HINTS: Final = ("os error 112", "no space left", "not enough space", "磁盘空间不足")
_TIMEOUT_HINTS: Final = ("timed out", "timeout", "operation timed out")
_NETWORK_HINTS: Final = (
    "dns error",
    "error sending request",
    "failed to fetch",
    "connection refused",
    "connection reset",
    "network",
    "tcp connect error",
)


def classify_uv_failure(output: str) -> OcrEngineErrorCode:
    """按 uv 的输出估计失败原因：磁盘空间、超时、网络，其余归为 INSTALL_FAILED。

    uv 没有机器可读的错误码，这里按它的错误信息里常见的字样判断，判断不出时一律 INSTALL_FAILED。
    """
    text = output.lower()
    if any(hint in text for hint in _DISK_HINTS):
        return OcrEngineErrorCode.DISK
    if any(hint in text for hint in _TIMEOUT_HINTS):
        return OcrEngineErrorCode.TIMEOUT
    if any(hint in text for hint in _NETWORK_HINTS):
        return OcrEngineErrorCode.NETWORK
    return OcrEngineErrorCode.INSTALL_FAILED


def failure_detail(result: CommandResult, lines: int = 8) -> str:
    """失败时的技术细节：退出码加输出的最后几行，地址脱敏。"""
    tail = [line for line in result.output.splitlines() if line.strip()][-lines:]
    message = scrub(" | ".join(tail), limit=600)
    return f"uv 退出码 {result.returncode}" + (f"：{message}" if message else "")


def environment_from_os() -> dict[str, str]:
    return dict(os.environ)
