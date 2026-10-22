"""应用更新的总线消息（需求 UPD-01 至 UPD-06，架构文档 15.9 节）。

新版本从 GitHub Releases 获取（仓库 willlllliams/LSTtranslate）。每个版本的附件是安装包
``LSTtranslate-<版本>-setup.exe`` 和它的校验文件 ``LSTtranslate-<版本>-setup.exe.sha256``。

``services.updates`` 发布 ``UpdateStatus`` 状态，处理三个命令；托盘与设置窗口订阅状态、发送命令。
安装时运行新安装包的静默安装（``/SILENT /SUPPRESSMSGBOXES /NORESTART /RELAUNCH=yes``），
然后发布 ``QuitRequested`` 让程序退出；安装程序装完后按 ``/RELAUNCH=yes`` 重新启动程序。
源码运行时只检查、不下载也不安装。
"""

from dataclasses import dataclass
from enum import StrEnum

from lst.contracts.messages import Command, State


class UpdateState(StrEnum):
    IDLE = "idle"  # 还没有检查过
    CHECKING = "checking"
    UP_TO_DATE = "up_to_date"
    AVAILABLE = "available"  # 有新版本，尚未下载
    DOWNLOADING = "downloading"  # progress 给出进度
    READY = "ready"  # 安装包已下载并通过校验，可以安装
    INSTALLING = "installing"  # 已启动安装程序，程序即将退出
    ERROR = "error"  # error_code 说明原因；available 保留上一次检查到的版本


class UpdateErrorCode(StrEnum):
    NETWORK = "update.network"
    TIMEOUT = "update.timeout"
    HTTP = "update.http"
    BAD_RELEASE = "update.bad_release"  # 版本信息或附件不是预期的格式
    CHECKSUM = "update.checksum"  # 安装包的 SHA-256 与校验文件不符
    DISK = "update.disk"
    NOT_INSTALLED = "update.not_installed"  # 源码运行，不能自动安装
    LAUNCH_FAILED = "update.launch_failed"  # 无法启动安装程序
    INTERNAL = "update.internal"


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    """一个可用版本。notes 是发布说明原文（Markdown）。"""

    version: str
    prerelease: bool
    published_at: str  # ISO 8601
    notes: str
    page_url: str  # 版本页面，供用户在浏览器里查看
    installer_bytes: int


@dataclass(frozen=True, slots=True)
class UpdateStatus(State):
    """更新状态。current_version 是正在运行的版本；last_checked 是最近一次成功检查的时间（ISO 8601）。"""

    state: UpdateState = UpdateState.IDLE
    current_version: str = ""
    available: ReleaseInfo | None = None
    progress: float | None = None
    last_checked: str | None = None
    error_code: UpdateErrorCode | None = None
    can_install: bool = False  # 安装版为 True；源码运行为 False


@dataclass(frozen=True, slots=True)
class CheckForUpdates(Command):
    """立即检查新版本。manual 为真表示用户点了“检查更新”，没有新版本时界面也给出提示。"""

    manual: bool = False


@dataclass(frozen=True, slots=True)
class DownloadUpdate(Command):
    """下载 UpdateStatus.available 指向的安装包并校验。"""


@dataclass(frozen=True, slots=True)
class InstallUpdate(Command):
    """运行已下载的安装包并退出程序。只在 READY 状态有效。"""
