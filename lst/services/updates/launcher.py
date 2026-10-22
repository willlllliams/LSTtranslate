"""启动安装程序。

安装程序必须与本程序脱离关系：本程序随后退出，安装程序还要继续运行，装完后按 ``/RELAUNCH=yes``
重新启动新版本。因此在 Windows 上用 ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` 创建进程：

- DETACHED_PROCESS：不继承本程序的控制台；
- CREATE_NEW_PROCESS_GROUP：不接收发给本程序进程组的 Ctrl+C / Ctrl+Break；
- ``close_fds=True`` 且标准输入输出都指向 DEVNULL：不继承任何句柄，本程序退出时不会因为句柄被占用而
  拖住安装程序，也不会把日志文件之类的句柄带进安装程序。

lst.app.jobobject 只把推理进程放进带 KILL_ON_JOB_CLOSE 的作业对象，主进程不在作业里，
所以从主进程启动的安装程序不会在主进程退出时被系统结束。
"""

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

INSTALLER_ARGS: Final[tuple[str, ...]] = ("/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=yes")
"""静默安装的命令行参数（lst/contracts/updates.py 的模块说明）。"""

type Launcher = Callable[[Path, Sequence[str]], None]
"""启动安装程序：参数是安装包路径与命令行参数。启动失败时抛出 OSError。"""


_launched: list[subprocess.Popen[bytes]] = []


def _creation_flags() -> int:
    if sys.platform == "win32":
        return subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def launch_detached(installer: Path, args: Sequence[str]) -> None:
    """以分离进程运行安装包，立即返回，不等待安装程序结束。"""
    # 安装包路径由更新服务确定，并且已经通过 SHA-256 校验。
    process = subprocess.Popen(
        [str(installer), *args],
        cwd=str(installer.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=_creation_flags(),
    )
    # 保留进程对象：Popen 对象在子进程仍在运行时被回收会发出 ResourceWarning。程序随后就退出了。
    _launched.append(process)
