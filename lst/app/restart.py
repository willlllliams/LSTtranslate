"""退出后重新启动程序（设置里更改数据目录后的“立即重启”，需求 CFG-06）。

应用内更新的重新启动由安装程序完成（``/RELAUNCH=yes``，架构文档 15.4 节），那条路径要先装新版本，这里用不上；
这里沿用它启动分离进程的做法（``lst.services.updates.launcher``）：

- 命令行是本进程的解释器加原来的参数（``sys.executable`` 与 ``sys.orig_argv[1:]``），安装版的
  ``pythonw.exe -I -X utf8 launcher\\lst_main.pyw`` 与源码版的 ``python -m lst --data-dir …`` 都原样保留；
- 入口在全部模块停止、单实例锁与运行互斥体释放之后才调用 ``relaunch``，新进程的单实例检查不会连上正在退出的
  旧进程，全局热键也已经注销；
- Windows 上用 ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` 创建，标准输入输出指向 DEVNULL，不继承句柄。
"""

import logging
import os
import subprocess
import sys
from collections.abc import Sequence

logger = logging.getLogger(__name__)


def restart_command() -> list[str]:
    """重新启动用的命令行：本进程的解释器与原来的参数。"""
    return [sys.executable, *sys.orig_argv[1:]]


def _creation_flags() -> int:
    if sys.platform == "win32":
        return subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def relaunch(command: Sequence[str] | None = None, cwd: str | None = None) -> bool:
    """以分离进程启动程序，立即返回。返回是否启动成功；失败时记错误日志。"""
    args = list(command) if command is not None else restart_command()
    try:
        subprocess.Popen(  # 命令行来自本进程自己的启动参数
            args,
            cwd=cwd or os.getcwd(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_creation_flags(),
        )
    except OSError:
        logger.exception("重新启动程序失败")
        return False
    logger.info("已启动新的程序进程")
    return True


__all__ = ["relaunch", "restart_command"]
