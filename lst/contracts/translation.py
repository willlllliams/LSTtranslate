"""翻译与竞速相关的数据类型和引擎协议。"""

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Protocol

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import EngineId, Lang, SegmentId, SessionId


class EngineKind(StrEnum):
    """引擎类别，见需求 ENG-01。"""

    LOCAL_NMT = "local_nmt"
    ONLINE_LLM = "online_llm"
    LOCAL_LLM = "local_llm"
    ONLINE_MT = "online_mt"


@dataclass(frozen=True, slots=True)
class EngineInfo:
    """一个引擎实例的静态信息。priority 数值越小优先级越高。

    max_batch_segments 与 max_batch_chars 决定调度器如何把段打包成任务（架构文档 6.4 节）；
    一个段本身超过 max_batch_chars 时单独成为一个任务。timeout_s 是单个任务的总时限，None 表示不限；
    配置服务保证正式运行时它是有限值（架构文档 6.5 节）。

    fingerprint 是影响译文的配置（模型、提示词、温度等）的哈希，参与缓存键。
    revision 在实例的任何配置变化时改变，包括 API key 这类不影响译文的配置；
    调度器以 EngineInfo 整体是否相等判断实例是否需要替换。

    image_max_side 不为 None 表示实例在截屏翻译时接收截图（需求 ENG-07），值是发送前图片长边的上限
    （物理像素），竞速模块按它缩小截图；None 表示实例不接收截图。是否附带截图不进 fingerprint：
    带图的尝试在缓存键里另有图片摘要（CacheKey.image_digest），不带图的请求与关闭该选项时完全相同。
    """

    id: EngineId
    name: str
    kind: EngineKind
    priority: int
    billing: bool
    max_concurrency: int = 1
    max_batch_segments: int = 1
    max_batch_chars: int = 4000
    timeout_s: float | None = None
    color: str = ""
    fingerprint: str = ""
    revision: int = 0
    image_max_side: int | None = None

    def __post_init__(self) -> None:
        if self.image_max_side is not None and self.image_max_side < 1:
            raise ValueError("EngineInfo.image_max_side 必须为正数或 None")


@dataclass(frozen=True, slots=True)
class Segment:
    """翻译与渲染的最小单位。"""

    id: SegmentId
    text: str


PNG_MEDIA_TYPE = "image/png"


@dataclass(frozen=True, slots=True)
class JobImage:
    """随任务发给多模态 LLM 的截图（需求 ENG-07），已经编码并缩小到发送尺寸。

    - media_type 是 data 的格式，例如 image/png；
    - width、height 是编码后图片的像素尺寸；
    - sha256 是 data 的 SHA-256 十六进制摘要，参与缓存键：同样的文字配不同的截图可能得到不同的译文。

    data 不进 repr：Job 可能随异常或调试日志被打印，图片内容不能因此写进日志（需求 PRV-02）。
    用 from_encoded 创建，摘要由它计算。
    """

    media_type: str
    width: int
    height: int
    sha256: str
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("JobImage 的宽、高必须为正")
        if not self.data:
            raise ValueError("JobImage 的图片数据不能为空")
        if len(self.sha256) != 64:
            raise ValueError("JobImage.sha256 必须是 64 个十六进制字符")

    @classmethod
    def from_encoded(cls, media_type: str, data: bytes, width: int, height: int) -> "JobImage":
        """由编码好的图片字节创建，同时计算摘要。"""
        return cls(media_type, width, height, hashlib.sha256(data).hexdigest(), data)


@dataclass(frozen=True, slots=True)
class Job:
    """调度器交给一个引擎实例的一次任务，可以包含多个段。

    context 是整个会话的原文，供 LLM 引擎保持术语一致（需求 ENG-04）；其他引擎可以忽略。
    image 是截屏会话的截图，只交给 EngineInfo.image_max_side 不为 None 的实例（需求 ENG-07）；
    双语窗口会话和不接收截图的实例都是 None。
    """

    session_id: SessionId
    engine_id: EngineId
    segments: tuple[Segment, ...]
    source_lang: Lang
    target_lang: Lang
    context: str = ""
    image: JobImage | None = None


@dataclass(frozen=True, slots=True)
class SegmentOutcome:
    """引擎对一个段的翻译结果。text 与 error 恰好一个非空。

    失败时 error 是技术细节（写日志、诊断用），code 是错误分类（界面据此显示原因与建议）。
    code 为 None 的失败按 ErrorCode.INTERNAL 显示。
    """

    segment_id: SegmentId
    text: str | None = None
    error: str | None = None
    code: ErrorCode | None = None

    def __post_init__(self) -> None:
        if (self.text is None) == (self.error is None):
            raise ValueError("SegmentOutcome 的 text 与 error 必须恰好一个非空")
        if self.text is not None and self.code is not None:
            raise ValueError("成功的 SegmentOutcome 不能带错误码")

    @property
    def ok(self) -> bool:
        return self.text is not None


class Engine(Protocol):
    """翻译引擎协议。竞速模块只认识这个协议，不认识具体引擎（需求 MNT-02）。"""

    @property
    def info(self) -> EngineInfo: ...

    def supports(self, source: Lang, target: Lang) -> bool:
        """是否支持这个语言对。"""
        ...

    def translate(self, job: Job) -> AsyncIterator[SegmentOutcome]:
        """翻译一个任务。每完成一个段就产出一次结果，顺序不限。

        - 每个段至多产出一次结果，应当为每个段都产出结果；引擎内部出错时，为剩下的段产出失败结果，
          而不是抛出异常。
        - 调度器（services.race.scheduler）兜底：流结束时仍没有结果的段记为 MISSING_SEGMENT，
          抛出异常或超过 EngineInfo.timeout_s 时剩下的段记为 INTERNAL 或 TIMEOUT，重复的段号与
          不属于本任务的段号被忽略。所以引擎提前结束（例如推理进程退出）时可以直接返回，不必补齐结果。
        - 原文非空时，成功结果的译文去掉空白后不能为空；模型给出空译文时按失败处理。
        - 调用方取消任务时，生成器会收到 CancelledError。
        """
        ...


class AttemptState(Enum):
    """一个引擎实例在一个段上的尝试状态。"""

    QUEUED = 1
    RUNNING = 2
    DONE = 3
    FAILED = 4
    CANCELLED = 5

    @property
    def pending(self) -> bool:
        """尚未结束，结果仍可能到达。"""
        return self in (AttemptState.QUEUED, AttemptState.RUNNING)


@dataclass(frozen=True, slots=True)
class AttemptView:
    """发给界面的一个尝试的快照。

    latency_ms 是从段派发到结束的耗时。cached 为真表示结果来自译文缓存；started 为真表示尝试进入过
    RUNNING 状态。竞速统计（RACE-07）只计入 started 为真且 cached 为假的尝试。
    """

    engine_id: EngineId
    state: AttemptState
    text: str | None
    error: str | None
    latency_ms: float | None
    cached: bool = False
    started: bool = False
    error_code: ErrorCode | None = None


class SkipReason(StrEnum):
    """段没有参加竞速的原因。"""

    NO_TEXT = "no_text"  # 段里没有可翻译的文字：只有数字、符号，或只有一个非汉字字母（RACE-09）
    SAME_LANGUAGE = "same_language"  # 检测到的源语言与目标语言相同（RACE-09）
    NO_ENGINE = "no_engine"  # 没有任何已启用的实例支持这个语言对


@dataclass(frozen=True, slots=True)
class SegmentView:
    """发给界面的一个段的竞速快照。

    shown_engine 是当前显示的译文来自哪个实例，即竞速的当前胜出者：已成功返回的实例中优先级最高的那个
    （RACE-03）。为 None 时还没有任何实例成功，界面显示原文。
    finished：显示已定（架构文档 6.3 节的 H 为空），之后 shown_engine 与 display_text 不再变化。
    """

    segment_id: SegmentId
    source_text: str
    display_text: str
    shown_engine: EngineId | None
    finished: bool
    attempts: tuple[AttemptView, ...]
    skip_reason: SkipReason | None = None
