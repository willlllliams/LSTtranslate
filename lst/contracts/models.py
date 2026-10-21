"""本地模型管理的总线消息与共用规则（需求 MOD-01、MOD-04、INS-03、INS-07，架构文档 8.5 节）。

services.models 发布 ``ModelCatalog`` 状态，处理四个命令；界面（设置窗口的模型页、首次运行引导）订阅状态、
发送命令。模型装好或删掉后发布 ``ModelsInstalled``，组合根据此让 NMT 进程重新扫描模型目录。

本模块还放两条服务与界面共用的纯规则：版本号比较（``version_key``）和互译对需要哪些模型
（``default_model_ids``）。界面模块不能依赖 services（架构文档 4.2 节），所以这两条规则写在 contracts 里，
与 ``contracts.langs`` 由主进程和推理进程共用是同样的安排。
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from lst.contracts.ids import Lang
from lst.contracts.langs import normalize_lang
from lst.contracts.messages import Command, Event, State

PIVOT_LANG: Final = Lang("en")
"""NMT 进程的中转语言：没有直连模型的语言对经英语翻译两跳（架构文档 2.5 节）。"""


class ModelKind(StrEnum):
    """模型类别。第一版只管理本地 NMT 模型；OCR 模型由 OCR 进程自己下载（架构文档 8.5 节）。"""

    NMT = "nmt"


class DownloadState(StrEnum):
    """一个模型的下载状态。"""

    QUEUED = "queued"  # 排队等待：同一时间只下载一个模型
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"  # 下载完毕，正在校验模型包
    DONE = "done"  # 已校验并安装
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_DOWNLOAD_STATES: Final = frozenset(
    {DownloadState.QUEUED, DownloadState.DOWNLOADING, DownloadState.VERIFYING}
)
"""还没有结束的下载状态。"""


class ModelErrorCode(StrEnum):
    """模型索引获取或模型下载失败的原因分类。界面按错误码显示原因与处理建议（架构文档 10.3 节）。"""

    NETWORK = "models.network"  # 连接失败、DNS 失败、传输中断
    TIMEOUT = "models.timeout"  # 连接或读取超时
    HTTP = "models.http"  # 服务器返回错误状态码
    BAD_INDEX = "models.bad_index"  # 模型索引不是预期的格式
    INVALID_PACKAGE = "models.invalid_package"  # 下载的文件不是可用的模型包
    PAIR_MISMATCH = "models.pair_mismatch"  # 模型包的语言对与索引不符
    DISK = "models.disk"  # 读写模型目录失败
    UNAVAILABLE = "models.unavailable"  # 模型索引里没有这个模型
    INTERNAL = "models.internal"  # 未预期的异常


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """模型目录里的一个模型：一个语言对的 Argos 模型包。

    - id：模型标识，由 ``nmt_model_id`` 按语言对生成，在索引项和已安装的包之间保持一致；
    - source、target：规范语言码（``contracts.langs``）；
    - version：可以下载的最新版本；索引里没有这个模型时等于已安装的版本；
    - size_bytes：下载大小，索引没有给出时为已安装文件的大小，都不知道时为 None；
    - installed：模型目录里有这个语言对的可用模型包；installed_version 是其版本，未安装时为 None；
    - url：下载地址，索引里没有这个模型时为空串。
    """

    id: str
    kind: ModelKind
    source: Lang
    target: Lang
    version: str
    size_bytes: int | None
    installed: bool
    url: str = ""
    installed_version: str | None = None

    @property
    def update_available(self) -> bool:
        """已安装，且索引里有更高的版本。"""
        if not self.installed or not self.url or self.installed_version is None:
            return False
        return version_key(self.version) > version_key(self.installed_version)


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    """一个模型的下载进度。

    done_bytes 是已经收到的字节数；total_bytes 来自响应头或索引，都没有时为 None。
    失败时 error_code 给出原因分类，error 是技术细节（例如“HTTP 404”“缺少 metadata.json”），
    只包含给用户看的内容，不包含下载地址和查询参数。
    """

    state: DownloadState
    done_bytes: int = 0
    total_bytes: int | None = None
    error: str | None = None
    error_code: ModelErrorCode | None = None

    @property
    def fraction(self) -> float | None:
        """完成比例，0 到 1；总大小未知时为 None。"""
        if not self.total_bytes or self.total_bytes <= 0:
            return None
        return min(1.0, self.done_bytes / self.total_bytes)


@dataclass(frozen=True, slots=True)
class ModelCatalog(State):
    """本地模型目录：可下载与已安装的模型，以及各模型的下载进度（需求 MOD-01）。

    - models：按 (source, target) 排序；
    - index_loaded：本次运行中成功获取过模型索引。为 False 时 models 只含已安装的模型；
    - refreshing：正在获取模型索引；
    - index_error、index_error_code：最近一次获取索引失败的技术细节与原因分类，成功后清空。
      获取失败不影响已安装的模型，也不清掉上一次成功获取的索引；
    - downloads：模型 id → 本次运行中提交过的下载的进度，结束的下载保留终态。
    """

    models: tuple[ModelInfo, ...] = ()
    downloads: Mapping[str, DownloadProgress] = field(default_factory=lambda: dict[str, DownloadProgress]())
    index_loaded: bool = False
    refreshing: bool = False
    index_error: str | None = None
    index_error_code: ModelErrorCode | None = None

    def model(self, model_id: str) -> ModelInfo | None:
        """按 id 取模型，没有时返回 None。"""
        return next((model for model in self.models if model.id == model_id), None)


@dataclass(frozen=True, slots=True)
class RefreshModelCatalog(Command):
    """重新获取模型索引并发布 ModelCatalog。

    这是一次网络请求，只在用户打开模型相关的界面时发送（需求 PRV-01）。
    """


@dataclass(frozen=True, slots=True)
class DownloadModels(Command):
    """把这些模型加入下载队列。

    已在队列中或正在下载的忽略；同一语言对已经装有相同或更高版本的跳过（需求 INS-07）。
    失败或取消的模型再次发送即重试。
    """

    ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CancelModelDownload(Command):
    """取消一个排队中或正在下载的模型，删除已下载的部分。"""

    id: str


@dataclass(frozen=True, slots=True)
class DeleteModel(Command):
    """删除一个已安装的模型：该语言对的全部模型包文件。"""

    id: str


@dataclass(frozen=True, slots=True)
class ModelsInstalled(Event):
    """模型目录里的已安装模型变了：ids 是刚装好的模型，removed 是刚删除的模型。

    组合根收到后让 NMT 进程重新扫描模型目录（监管器 reload，架构文档 8.2.1 节）。
    """

    ids: tuple[str, ...]
    removed: tuple[str, ...] = ()


# ---------------------------------------------------------------- 共用规则


def nmt_model_id(source: str, target: str) -> str:
    """本地 NMT 模型的 id。参数是规范语言码，例如 ``nmt_model_id("zh-Hans", "en") == "argos:zh-Hans:en"``。"""
    return f"argos:{source}:{target}"


def version_key(version: str) -> tuple[int, ...]:
    """把版本号转换成可比较的元组：取出其中全部数字串，例如 "1.9" → (1, 9)，"1.10" → (1, 10)。

    与 NMT 进程选择同一语言对的多个模型包时的规则相同。没有数字的版本号是空元组，比任何带数字的版本都低。
    """
    return tuple(int(part) for part in re.findall(r"\d+", version))


def default_model_ids(catalog: ModelCatalog, pair: Sequence[str]) -> tuple[str, ...]:
    """互译对 ``pair = [a, b]`` 需要的本地 NMT 模型 id，供首次运行引导列出（需求 INS-03）。

    两个方向 a→b、b→a 各自按 NMT 进程的路线规则挑选（架构文档 2.5 节）：

    1. 目录里有直连模型时用它；
    2. 否则两端都不是英语、并且目录里有“源语言→英语”和“英语→目标语言”两个模型时，用这两个模型；
    3. 都没有时这个方向没有可用的模型，跳过。

    “目录里有”包括已安装的模型和索引里可以下载的模型。结果按 a→b、b→a 的顺序排列，重复的 id 只出现一次。
    pair 不是两个合法且不同的语言码时返回空元组。
    """
    if len(pair) != 2:
        return ()
    try:
        first, second = normalize_lang(pair[0]), normalize_lang(pair[1])
    except ValueError:
        return ()
    if first == second:
        return ()
    by_pair = {
        (model.source, model.target): model.id for model in catalog.models if model.kind is ModelKind.NMT
    }
    chosen: list[str] = []
    for source, target in ((first, second), (second, first)):
        direct = by_pair.get((source, target))
        if direct is not None:
            route = [direct]
        elif PIVOT_LANG in (source, target):
            route = []
        else:
            legs = [by_pair.get((source, PIVOT_LANG)), by_pair.get((PIVOT_LANG, target))]
            route = [leg for leg in legs if leg is not None] if all(legs) else []
        chosen.extend(model_id for model_id in route if model_id not in chosen)
    return tuple(chosen)
