"""本地翻译（NMT）显卡运行库的总线消息（需求 MOD-06，架构文档 8.3.1 节）。

CTranslate2 的显卡推理需要 NVIDIA cuBLAS 12（``cublas64_12.dll`` 与 ``cublasLt64_12.dll``）。安装包不带它
（约 550 MB），用户在设置里按需下载：``services.nmt_runtime`` 从 PyPI 下载 ``nvidia-cublas-cu12`` 的 wheel，
校验 SHA-256 后只取出这两个 DLL，放在数据目录 ``runtimes/cublas12/bin``；NMT 进程按完整路径加载，
不按 PATH 搜索。

``services.nmt_runtime`` 发布 ``NmtRuntimeInfo`` 状态，处理下载、取消、删除三个命令；装好或删除后发布
``NmtRuntimeChanged``，组合根据此重新启动 NMT 进程。推理设备写在配置的 ``[nmt].device``（auto、cpu、cuda）。
"""

from dataclasses import dataclass
from enum import StrEnum

from lst.contracts.messages import Command, Event, State


class NmtRuntimeState(StrEnum):
    """显卡运行库在本机的状态。"""

    NOT_INSTALLED = "not_installed"
    DOWNLOADING = "downloading"  # progress 给出进度
    INSTALLING = "installing"  # 下载完毕，正在校验与解压
    INSTALLED = "installed"
    ERROR = "error"  # 下载或安装失败；error_code 说明原因，之前装好的版本（如果有）仍然可用


class NmtRuntimeErrorCode(StrEnum):
    """下载或安装失败的原因。界面按错误码显示原因与处理建议。"""

    NETWORK = "nmt_runtime.network"
    TIMEOUT = "nmt_runtime.timeout"
    HTTP = "nmt_runtime.http"
    DISK = "nmt_runtime.disk"  # 写入失败或磁盘空间不足
    INVALID_PACKAGE = "nmt_runtime.invalid_package"  # 大小或 SHA-256 不符，或包里缺少所需的 DLL
    INTERNAL = "nmt_runtime.internal"


@dataclass(frozen=True, slots=True)
class NmtRuntimeInfo(State):
    """显卡运行库的展示信息。

    - version：可下载的 cuBLAS 版本；installed_version：已装的版本，未安装为 None；
    - download_bytes：下载大小（wheel 文件），installed_bytes：已装文件的大小；
    - gpus：本机的 NVIDIA 显卡名称；为空时显卡运行库没有用处，界面说明原因但不禁止下载；
    - progress：0 到 1 的下载进度，不在下载中为 None；
    - error_code：ERROR 时的原因，detail 是技术细节，只写日志。
    """

    state: NmtRuntimeState = NmtRuntimeState.NOT_INSTALLED
    version: str = ""
    installed_version: str | None = None
    download_bytes: int = 0
    installed_bytes: int = 0
    gpus: tuple[str, ...] = ()
    progress: float | None = None
    error_code: NmtRuntimeErrorCode | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class InstallNmtRuntime(Command):
    """下载并安装显卡运行库。已装同一版本或正在下载时不做任何事。"""


@dataclass(frozen=True, slots=True)
class CancelNmtRuntimeInstall(Command):
    """取消正在进行的下载。已经进入 INSTALLING 阶段时忽略。"""


@dataclass(frozen=True, slots=True)
class DeleteNmtRuntime(Command):
    """删除已装的显卡运行库。组合根先停 NMT 进程，删除后再按配置重新启动。"""


@dataclass(frozen=True, slots=True)
class NmtRuntimeChanged(Event):
    """显卡运行库装好（installed 为真）或删除了。bin_dir 是 DLL 所在目录，删除时为空串。"""

    installed: bool
    bin_dir: str = ""
