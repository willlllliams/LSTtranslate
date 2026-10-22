"""本地模型管理的总线消息与共用规则（需求 MOD-01、MOD-04、INS-03、INS-07，架构文档 8.5 节）。

services.models 发布 ``ModelCatalog`` 状态，处理四个命令；界面（设置窗口的模型页、首次运行引导）订阅状态、
发送命令。模型装好或删掉后发布 ``ModelsInstalled``，组合根据此让 NMT 进程重新扫描模型目录。

模型分两类（``ModelKind``）：本地 NMT 模型（Argos 模型包）和词典包（字典翻译引擎逐词查词用的
``dict-<src>-<tgt>.tsv.gz``）。两类共用下载队列、进度、取消与删除的命令，模型 id 的前缀区分类别。

本模块还放几条服务与界面共用的纯规则：版本号比较（``version_key``）、互译对需要哪些模型
（``default_model_ids``、``default_dict_ids``）和词典包的文件名（``dict_pack_file_name``）。
界面模块不能依赖 services（架构文档 4.2 节），模型管理与字典引擎两个业务模块也互不依赖，所以这些规则写在
contracts 里，与 ``contracts.langs`` 由主进程和推理进程共用是同样的安排。
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
    """模型类别。OCR 模型由 services.ocr_engines 下载与删除，不在此列（架构文档 8.6 节）。"""

    NMT = "nmt"  # 本地 NMT 模型（Argos 模型包）
    DICT = "dict"  # 字典翻译引擎的词典包


DICT_PACK_PREFIX: Final = "dict-"
DICT_PACK_SUFFIX: Final = ".tsv.gz"
"""词典包文件名的前缀与后缀：``dict-<src>-<tgt>.tsv.gz``，与发布页上的文件名相同。"""


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
    """模型目录里的一个模型：一个语言对的 Argos 模型包或词典包。

    - id：模型标识，由 ``nmt_model_id`` 或 ``dict_model_id`` 按语言对生成，在索引项和已安装的包之间保持一致；
    - source、target：规范语言码（``contracts.langs``）；
    - version：可以下载的最新版本；索引里没有这个模型时等于已安装的版本。词典索引不带版本号，
      词典包的 version 是已安装包头里的构建日期（YYYYMMDD），未安装时为空串；
    - size_bytes：下载大小，索引没有给出时为已安装文件的大小，都不知道时为 None；
    - installed：模型目录里有这个语言对的可用模型包；installed_version 是其版本，未安装时为 None；
    - url：下载地址，索引里没有这个模型时为空串；
    - license、attribution：数据的许可证（SPDX 标识，例如 CC-BY-SA-4.0、MIT）与署名全文，只有词典包有。
      CC BY-SA 要求署名可见，界面在词典的每一行显示它；
    - differs_from_index：词典包专用。已安装的文件与索引里的文件 SHA-256 不同，说明发布页上的包已经重新构建。
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
    license: str = ""
    attribution: str = ""
    differs_from_index: bool = False

    @property
    def update_available(self) -> bool:
        """已安装，且索引里有更新的包：NMT 模型比较版本号，词典包比较文件的 SHA-256。"""
        if not self.installed or not self.url:
            return False
        if self.kind is ModelKind.DICT:
            return self.differs_from_index
        if self.installed_version is None:
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

    以上 index_*、refreshing 只描述 Argos 模型索引。词典索引（dict-index.json）单独获取，状态在 dict_*
    字段里，两个索引的获取失败互不影响：

    - dicts_enabled：模型管理模块管理词典包（组合根给了词典目录）。为 False 时目录里没有词典包，
      界面不显示词典部分；
    - dict_index_loaded、dict_refreshing、dict_index_error、dict_index_error_code：
      含义与 Argos 索引的对应字段相同。
    """

    models: tuple[ModelInfo, ...] = ()
    downloads: Mapping[str, DownloadProgress] = field(default_factory=lambda: dict[str, DownloadProgress]())
    index_loaded: bool = False
    refreshing: bool = False
    index_error: str | None = None
    index_error_code: ModelErrorCode | None = None
    dicts_enabled: bool = False
    dict_index_loaded: bool = False
    dict_refreshing: bool = False
    dict_index_error: str | None = None
    dict_index_error_code: ModelErrorCode | None = None

    def model(self, model_id: str) -> ModelInfo | None:
        """按 id 取模型，没有时返回 None。"""
        return next((model for model in self.models if model.id == model_id), None)

    def of_kind(self, kind: ModelKind) -> tuple[ModelInfo, ...]:
        """某一类模型，顺序与 models 相同。"""
        return tuple(model for model in self.models if model.kind is kind)


@dataclass(frozen=True, slots=True)
class RefreshModelCatalog(Command):
    """重新获取模型索引并发布 ModelCatalog。

    这是一次网络请求（管理词典包时还有一次词典索引的请求，两者同时进行），只在用户打开模型相关的界面时发送
    （需求 PRV-01）。
    """


@dataclass(frozen=True, slots=True)
class DownloadModels(Command):
    """把这些模型加入下载队列。NMT 模型与词典包共用一个队列，同一时间只下载一个。

    已在队列中或正在下载的忽略；同一语言对已经装有相同或更高版本的跳过（需求 INS-07），
    词典包按 SHA-256 判断，与索引里的文件相同时跳过。失败或取消的模型再次发送即重试。
    """

    ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CancelModelDownload(Command):
    """取消一个排队中或正在下载的模型，删除已下载的部分。"""

    id: str


@dataclass(frozen=True, slots=True)
class DeleteModel(Command):
    """删除一个已安装的模型：该语言对的全部模型包文件（词典包是包文件和它的说明文件）。"""

    id: str


@dataclass(frozen=True, slots=True)
class ModelsInstalled(Event):
    """模型目录里的已安装模型变了：ids 是刚装好的模型，removed 是刚删除的模型。

    组合根收到后让 NMT 进程重新扫描模型目录（监管器 reload，架构文档 8.2.1 节）。只涉及词典包时
    （``kinds()`` 里没有 NMT）不需要重载 NMT 进程：字典引擎自己发现词典目录的变化，重新读取词典包。
    """

    ids: tuple[str, ...]
    removed: tuple[str, ...] = ()

    def kinds(self) -> frozenset[ModelKind]:
        """这次变化涉及的模型类别。"""
        return frozenset(model_kind_of(model_id) for model_id in (*self.ids, *self.removed))


# ---------------------------------------------------------------- 共用规则


def nmt_model_id(source: str, target: str) -> str:
    """本地 NMT 模型的 id。参数是规范语言码，例如 ``nmt_model_id("zh-Hans", "en") == "argos:zh-Hans:en"``。"""
    return f"argos:{source}:{target}"


_DICT_ID_PREFIX: Final = "dict:"


def dict_model_id(source: str, target: str) -> str:
    """词典包的 id。参数是规范语言码，例如 ``dict_model_id("en", "zh-Hans") == "dict:en:zh-Hans"``。"""
    return f"{_DICT_ID_PREFIX}{source}:{target}"


def model_kind_of(model_id: str) -> ModelKind:
    """按 id 的前缀判断模型类别：``dict:`` 开头的是词典包，其余是 NMT 模型。"""
    return ModelKind.DICT if model_id.startswith(_DICT_ID_PREFIX) else ModelKind.NMT


def dict_pack_file_name(source: str, target: str) -> str:
    """词典包的文件名，例如 ``dict_pack_file_name("en", "zh-Hans") == "dict-en-zh-Hans.tsv.gz"``。"""
    return f"{DICT_PACK_PREFIX}{source}-{target}{DICT_PACK_SUFFIX}"


def parse_dict_pack_file_name(name: str) -> tuple[Lang, Lang] | None:
    """从词典包文件名取出语言对；不是 ``dict-<src>-<tgt>.tsv.gz`` 的形式时返回 None。

    语言码本身可以带连字符（zh-Hans），所以逐个连字符尝试切分，取两边都是规范写法、且两者不同的那一种。
    ``dict-en-zh-Hans.tsv.gz`` 只能切成 (en, zh-Hans)：在第二个连字符处切出的 ``en-zh`` 的规范写法是 ``en``，
    不等于它自己。
    """
    if not name.startswith(DICT_PACK_PREFIX) or not name.endswith(DICT_PACK_SUFFIX):
        return None
    middle = name[len(DICT_PACK_PREFIX) : -len(DICT_PACK_SUFFIX)]
    for position, char in enumerate(middle):
        if char != "-":
            continue
        source, target = middle[:position], middle[position + 1 :]
        try:
            if normalize_lang(source) == source and normalize_lang(target) == target and source != target:
                return Lang(source), Lang(target)
        except ValueError:
            continue
    return None


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


def default_dict_ids(catalog: ModelCatalog, pair: Sequence[str]) -> tuple[str, ...]:
    """互译对 ``pair = [a, b]`` 需要的词典包 id，供首次运行引导列出。

    字典引擎默认只用直连的词典包（经英语中转是可选设置），所以只取 a→b 与 b→a 两个方向中目录里有的包，
    按 a→b、b→a 的顺序排列。“目录里有”包括已安装的和词典索引里可以下载的。
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
    present = {model.id for model in catalog.models if model.kind is ModelKind.DICT}
    wanted = (dict_model_id(first, second), dict_model_id(second, first))
    return tuple(model_id for model_id in wanted if model_id in present)
