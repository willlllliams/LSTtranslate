"""竞速与引擎相关的总线消息（架构文档 5.3 节），以及设置窗口用到的统计、缓存与连接测试消息。"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import EngineId, Lang, SegmentId, SessionId
from lst.contracts.messages import Command, Event, State
from lst.contracts.ports import WorkerState
from lst.contracts.translation import AttemptView, Engine, SegmentView


@dataclass(frozen=True, slots=True)
class SegmentsUpdated(Event):
    """一次合帧刷新：自上次刷新以来有变化的段的快照（架构文档 6.6 节）。"""

    session_id: SessionId
    views: tuple[SegmentView, ...]


@dataclass(frozen=True, slots=True)
class SessionProgress(Event):
    """显示已定的段数和总段数（需求 OV-07）。"""

    session_id: SessionId
    finished: int
    total: int


@dataclass(frozen=True, slots=True)
class AttemptRecorded(Event):
    """一个尝试进入终态。快照里只有状态、耗时与标记，统计模块不保存任何文本（需求 PRV-02）。"""

    session_id: SessionId
    segment_id: SegmentId
    attempt: AttemptView


@dataclass(frozen=True, slots=True)
class TranslateText(Command):
    """双语窗口请求翻译一段文本（需求 BI-03 至 BI-06）。

    source、target 为 None 表示“自动”：源语言按段检测，目标语言按互译对规则确定（RACE-10）。

    会话号由双语窗口决定，竞速模块据此区分两种请求（架构文档 11.6 节）：
    - 会话号与当前会话相同、include_billing 为真：用户按了 Ctrl+Enter 而文本与语言都没变，
      还没有参赛的计费实例加入当前会话（6.3 节“晚加入”），结果仍以这个会话号发布；
    - 其他情况：开始新会话，取消旧会话。

    “文本与语言是否没变”只由双语窗口判断，竞速模块不再按切段结果比较；两边各自判断时，
    "a\\nb" 改成 "a\\n\\nb" 这类只改空行的输入会让两边结论相反，计费实例的结果以旧会话号发布后被窗口丢弃。

    text 去掉空白后为空表示取消当前会话：调度器取消双语入口的会话，不发布任何事件。
    """

    session_id: SessionId
    text: str
    source: Lang | None = None
    target: Lang | None = None
    include_billing: bool = False


@dataclass(frozen=True, slots=True)
class EngineRoster(State):
    """当前全部已启用的引擎实例。services.race 订阅它并调用调度器的 set_engines（架构文档 6.5 节）。"""

    engines: tuple[Engine, ...]


class EngineHealth(StrEnum):
    """托盘显示的引擎实例状态（需求 TR-03）。"""

    DISABLED = "disabled"
    LOADING = "loading"
    READY = "ready"
    BUSY = "busy"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class EngineStatus:
    health: EngineHealth
    detail: str = ""
    error_code: ErrorCode | None = None


@dataclass(frozen=True, slots=True)
class EngineStatuses(State):
    """每个引擎实例（包括未启用的）的状态、名称与标识颜色，供托盘、设置窗口和双语窗口显示。

    colors 是配置里的标识颜色（可能为空串，界面这时按实例号取默认颜色）；为 None 表示发布方没有提供。
    """

    statuses: Mapping[EngineId, EngineStatus]
    names: Mapping[EngineId, str]
    colors: Mapping[EngineId, str] | None = None


@dataclass(frozen=True, slots=True)
class WorkerStatus:
    state: WorkerState
    detail: str = ""


@dataclass(frozen=True, slots=True)
class WorkerStatuses(State):
    """全部推理进程的状态（需求 TR-03、OCR-07），键为进程名 "ocr"、"nmt"。

    State 消息按类型只保存最新值，所以各进程的状态放在同一条消息里：发布方每次发布完整的映射，
    新订阅者收到的回放包含每个进程的当前状态。
    """

    statuses: Mapping[str, WorkerStatus]


# ---------------------------------------------------------------- 界面与应用层的命令和事件


@dataclass(frozen=True, slots=True)
class StartCapture(Command):
    """开始截屏（热键或托盘菜单，需求 CAP-01）。"""


@dataclass(frozen=True, slots=True)
class ToggleBilingualWindow(Command):
    """显示或隐藏双语窗口（需求 BI-01）。"""


@dataclass(frozen=True, slots=True)
class OpenSettings(Command):
    """打开设置窗口。"""


@dataclass(frozen=True, slots=True)
class ShowModelDownloads(Command):
    """打开模型下载窗口（需求 MOD-01）。由 ui.onboarding 处理：显示首次运行引导窗口，列出互译对需要的模型。"""


@dataclass(frozen=True, slots=True)
class QuitRequested(Event):
    """用户要求退出程序。组合根按相反顺序停止全部模块（架构文档 4.3 节）。"""


@dataclass(frozen=True, slots=True)
class HotkeyRegistrationFailed(Event):
    """全局热键注册失败（需求 TR-05）。reason 是给用户看的原因说明。"""

    hotkey: str
    reason: str


class NoticeLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class NotificationRequested(Event):
    """请托盘弹出一条通知。title、message 是 ui.i18n 的文案键，params 用于填充占位符。

    业务模块不包含界面文案（架构文档 10.3 节），所以这里只传键和参数。
    """

    title_key: str
    message_key: str
    level: NoticeLevel = NoticeLevel.INFO
    params: Mapping[str, str] | None = None


# ---------------------------------------------------------------- 设置窗口：竞速统计、译文缓存与连接测试


@dataclass(frozen=True, slots=True)
class EngineStatsRow:
    """一个引擎实例的累计竞速统计（需求 RACE-07）。字段含义与 services.stats 的 EngineStats 相同。

    - executed：真正执行过的尝试数，命中缓存的和执行前被取消的不计入；
    - wins：以该实例的译文成为最终显示的段数；
    - p50_ms、p95_ms：成功尝试的结果耗时的中位数与第 95 百分位数，没有样本时为 None；
    - failure_rate：执行过的尝试中失败所占的比例，没有执行过时为 None。

    行里不含任何文本（需求 PRV-02）。
    """

    engine_id: EngineId
    executed: int
    wins: int
    p50_ms: float | None = None
    p95_ms: float | None = None
    failure_rate: float | None = None


@dataclass(frozen=True, slots=True)
class StatsSnapshot(State):
    """全部引擎实例的累计统计，按实例号排序。services.stats 发布，设置窗口的竞速页显示（需求 RACE-07）。

    统计库里没有数据时 engines 为空元组。有新的尝试记录时，发布方合并一段时间内的变化再发布，
    所以界面看到的值可能比实际落后一秒左右。
    """

    engines: tuple[EngineStatsRow, ...] = ()


@dataclass(frozen=True, slots=True)
class ResetStats(Command):
    """清空全部竞速统计（设置窗口的“重置统计”）。处理者是 services.stats，处理后立即发布空快照。"""


@dataclass(frozen=True, slots=True)
class ClearCache(Command):
    """清空译文缓存（设置窗口的“清空译文缓存”）。处理者是 services.cache，完成后发布 CacheCleared。"""


@dataclass(frozen=True, slots=True)
class CacheCleared(Event):
    """译文缓存已清空，removed 是清空前的条目数。"""

    removed: int


@dataclass(frozen=True, slots=True)
class CheckEngineConnection(Command):
    """测试一个引擎实例能否正常工作（设置窗口的“测试连接”）。处理者是 services.engines。

    处理者按实例当前的配置翻译一个很短的固定词，不使用缓存与调度器，完成后发布 EngineConnectionChecked。
    """

    engine_id: EngineId


@dataclass(frozen=True, slots=True)
class EngineConnectionChecked(Event):
    """一次连接测试的结果。ok 为真表示实例返回了译文；失败时 error_code 给出原因分类，界面据此显示原因与建议。

    latency_ms 是测试请求的耗时，没有发出请求（例如缺少 API key）时为 None。
    """

    engine_id: EngineId
    ok: bool
    error_code: ErrorCode | None = None
    latency_ms: float | None = None
