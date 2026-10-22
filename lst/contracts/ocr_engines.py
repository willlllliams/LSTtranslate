"""OCR 引擎的总线消息（需求 OCRE-01 至 OCRE-06，架构文档 8.6 节）。

截屏识别可以使用多个 OCR 引擎，每个引擎独立安装、启用和卸载，安装包不捆绑任何需要下载的引擎：

- ``windows``：Windows 自带的 OCR（Windows.Media.Ocr），不需要下载，识别语言取决于系统已装的 OCR 语言包；
  只有文本模式，没有版面结构；
- ``paddle``：PaddleOCR，提供版面结构、表格、公式；运行环境（约 1 GB 的 Python 包）与模型都按需下载。

``services.ocr_engines`` 发布 ``OcrEngineCatalog`` 状态，处理安装、取消、卸载三个命令；
设置窗口订阅状态、发送命令。
当前使用哪个引擎、各引擎是否启用写在配置的 ``[ocr]`` 节（``engine`` 字段），通过 ``UpdateConfig`` 修改。
引擎装好或卸载后发布 ``OcrEnginesChanged``，组合根据此重新决定 OCR 进程的启动方式。

位置信息的统一约定（全部引擎都遵守，输出格式见 lst/services/ocr/blocks.py）：
- 能给出文字位置的引擎，按行返回各自的 bbox（相对原始截图的物理像素）；
- 不能给出位置的引擎，把整张截图作为一个大 bbox，返回一个 text 块，块的 text 为全部识别文字，
  lines 为空或只有一行同样覆盖整张截图的行。主进程据此把译文铺满整个选区。
"""

from dataclasses import dataclass
from enum import StrEnum

from lst.contracts.messages import Command, Event, State


class OcrEngineKind(StrEnum):
    """OCR 引擎。值也是配置 ``[ocr].engine`` 的取值。"""

    WINDOWS = "windows"
    PADDLE = "paddle"


class OcrEngineState(StrEnum):
    """一个 OCR 引擎在本机的状态。设置窗口按它显示状态文字与可用的按钮。

    安装相关的状态由 services.ocr_engines 给出；进程相关的状态（加载中、就绪、出错）由组合根
    根据 OCR 进程的 ``WorkerStatuses`` 合并进来，只对当前使用的引擎有意义。
    """

    UNAVAILABLE = "unavailable"  # 本机无法使用，例如 Windows 没有装任何 OCR 语言包；error_code 说明原因
    NOT_INSTALLED = "not_installed"  # 需要下载，尚未安装
    QUEUED = "queued"  # 安装排队中
    DOWNLOADING = "downloading"  # 正在下载运行环境或模型；progress 给出进度
    INSTALLING = "installing"  # 下载完毕，正在解压、安装、校验
    INSTALLED = "installed"  # 已安装，不是当前使用的引擎，或 OCR 已关闭
    LOADING = "loading"  # 当前引擎，OCR 进程正在启动或加载模型
    READY = "ready"  # 当前引擎，OCR 进程就绪
    ERROR = "error"  # 安装失败或 OCR 进程出错；error_code 说明原因


class OcrEngineErrorCode(StrEnum):
    """OCR 引擎安装或运行失败的原因分类。界面按错误码显示原因与处理建议（架构文档 10.3 节）。"""

    NO_LANGUAGE = "ocr_engine.no_language"  # Windows 没有装 OCR 语言包
    NETWORK = "ocr_engine.network"
    TIMEOUT = "ocr_engine.timeout"
    HTTP = "ocr_engine.http"
    DISK = "ocr_engine.disk"  # 写入失败或磁盘空间不足
    INVALID_PACKAGE = "ocr_engine.invalid_package"  # 下载内容校验失败
    INSTALL_FAILED = "ocr_engine.install_failed"  # 运行环境安装失败（uv 返回非零）
    WORKER_FAILED = "ocr_engine.worker_failed"  # OCR 进程反复退出
    INTERNAL = "ocr_engine.internal"


@dataclass(frozen=True, slots=True)
class OcrEngineInfo:
    """一个 OCR 引擎的展示信息。

    - needs_download：是否需要下载才能使用（windows 为 False）；
    - download_bytes：全部下载内容的大致大小，未知为 None；
    - progress：0 到 1 的安装进度，不在下载或安装中为 None；
    - languages：可识别的语言（规范语言码），windows 来自系统 OCR 语言包；
    - features：支持的能力，取值 "text"、"layout"、"table"、"formula"；
    - error_code：UNAVAILABLE 或 ERROR 时的原因，其他状态为 None；
    - detail：技术细节，只写日志，界面不显示。
    """

    kind: OcrEngineKind
    state: OcrEngineState
    needs_download: bool
    download_bytes: int | None = None
    progress: float | None = None
    languages: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    error_code: OcrEngineErrorCode | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class OcrEngineCatalog(State):
    """全部 OCR 引擎的状态，按 OcrEngineKind 的定义顺序排列。

    active 是配置里当前使用的引擎，OCR 关闭时为 None。
    """

    engines: tuple[OcrEngineInfo, ...] = ()
    active: OcrEngineKind | None = None

    def engine(self, kind: OcrEngineKind) -> OcrEngineInfo | None:
        return next((e for e in self.engines if e.kind is kind), None)


@dataclass(frozen=True, slots=True)
class InstallOcrEngine(Command):
    """下载并安装一个 OCR 引擎。已安装或不需要下载时不做任何事。"""

    kind: OcrEngineKind


@dataclass(frozen=True, slots=True)
class CancelOcrEngineInstall(Command):
    """取消正在排队或下载的安装。已经进入 INSTALLING 阶段时忽略。"""

    kind: OcrEngineKind


@dataclass(frozen=True, slots=True)
class UninstallOcrEngine(Command):
    """卸载一个 OCR 引擎：删除它的运行环境与模型。是当前引擎时，组合根先停 OCR 进程。"""

    kind: OcrEngineKind


@dataclass(frozen=True, slots=True)
class OcrEnginesChanged(Event):
    """某个 OCR 引擎装好或卸载了。组合根据此重新决定 OCR 进程的启动方式。"""

    kind: OcrEngineKind
    installed: bool
