"""Windows 作业对象测试（需求 REL-03）。"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from lst.app import jobobject
from tests.app.conftest import ROOT

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="作业对象只在 Windows 上存在")

# 模拟主进程崩溃：中间进程创建作业对象、把自己启动的进程加入作业，然后不做任何清理直接结束。
_OWNER_SCRIPT = textwrap.dedent(
    """
    import os, subprocess, sys
    from lst.app import jobobject
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    print(child.pid, jobobject.assign_process(child.pid), flush=True)
    sys.stdin.readline()
    os._exit(1)
    """
)


def test_processes_in_job_die_when_owner_exits_abruptly() -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    synchronize, process_terminate, wait_object_0 = 0x0010_0000, 0x0001, 0

    owner = subprocess.Popen(
        [sys.executable, "-c", _OWNER_SCRIPT],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert owner.stdin is not None and owner.stdout is not None
    pid_text, assigned = owner.stdout.readline().split()
    assert assigned == "True"
    # 在中间进程退出之前打开句柄，之后等待它，进程号不会被复用到别的进程上。
    handle = kernel32.OpenProcess(synchronize | process_terminate, False, int(pid_text))
    assert handle
    try:
        owner.stdin.write("\n")
        owner.stdin.flush()
        assert owner.wait(timeout=10) == 1
        assert kernel32.WaitForSingleObject(handle, 5000) == wait_object_0
    finally:
        # 失败时由测试结束自己间接启动的这个进程。
        if kernel32.WaitForSingleObject(handle, 0) != wait_object_0:
            kernel32.TerminateProcess(handle, 1)
        kernel32.CloseHandle(handle)
        if owner.poll() is None:
            owner.kill()
            owner.wait()


def test_membership_queries() -> None:
    assert not jobobject.contains_process(os.getpid())  # 测试进程自己不在作业里
    assert os.getpid() not in jobobject.process_ids()
    assert not jobobject.assign_process(0)  # 进程号 0 无法打开
