"""应用更新服务：检查 GitHub Releases 上的新版本，下载、校验并启动安装包（lst/contracts/updates.py）。

- config：配置节 [updates] 与 CONFIG_SECTIONS；
- version：语义化版本号的解析与比较，当前运行版本的读取；
- releases：版本列表与校验文件的解析、可用版本的选择；
- store：检查时间与已通知版本的持久化记录；
- launcher：以分离进程启动安装程序；
- net：请求头、超时、错误分类与日志脱敏；
- module：UpdatesModule，实现 kernel 的 Module 协议。
"""

from lst.services.updates.config import CONFIG_SECTIONS, UpdatesConfig
from lst.services.updates.launcher import INSTALLER_ARGS, launch_detached
from lst.services.updates.module import UpdatesModule
from lst.services.updates.releases import DEFAULT_RELEASES_URL, installer_name
from lst.services.updates.version import Version, parse_version, running_version

__all__ = [
    "CONFIG_SECTIONS",
    "DEFAULT_RELEASES_URL",
    "INSTALLER_ARGS",
    "UpdatesConfig",
    "UpdatesModule",
    "Version",
    "installer_name",
    "launch_detached",
    "parse_version",
    "running_version",
]
