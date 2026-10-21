"""主进程与推理进程之间的通信协议（架构文档 2.8、8.1 节）。

传输层是 `multiprocessing.connection` 的命名管道，每次 send_bytes 发送一帧。
一条消息是一帧 UTF-8 JSON；带像素数据的消息（OcrRequest）后面紧跟一帧原始字节。

本模块只依赖标准库，主进程的监管器和子进程都使用它，所以两边的代码不同步时，
协议版本检查会在握手阶段失败（子进程以 ExitCode.PROTOCOL_MISMATCH 退出）。
"""

import json
from dataclasses import asdict, dataclass, field, fields
from enum import IntEnum
from typing import Any, ClassVar, Protocol, cast

PROTOCOL_VERSION = 1

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


class ExitCode(IntEnum):
    """子进程的退出码。确定性故障重启也不会好转，监管器收到后直接进入 FAILED（架构文档 8.2 节）。"""

    OK = 0
    PROTOCOL_MISMATCH = 64  # 协议版本不符
    DEPENDENCY_ERROR = 65  # 推理依赖导入失败，例如 DLL 加载失败
    CONFIG_ERROR = 66  # 启动参数或配置无效

    @property
    def deterministic(self) -> bool:
        return self in (ExitCode.PROTOCOL_MISMATCH, ExitCode.DEPENDENCY_ERROR, ExitCode.CONFIG_ERROR)


# ---------------------------------------------------------------- 消息


@dataclass(frozen=True, slots=True)
class WorkerMessage:
    """全部协议消息的基类。TYPE 是 JSON 中 type 字段的取值；HAS_PAYLOAD 为真时消息后紧跟一帧原始字节。"""

    TYPE: ClassVar[str] = ""
    HAS_PAYLOAD: ClassVar[bool] = False


# ---- 主进程 → 子进程


@dataclass(frozen=True, slots=True)
class Hello(WorkerMessage):
    TYPE: ClassVar[str] = "hello"
    protocol_version: int


@dataclass(frozen=True, slots=True)
class Configure(WorkerMessage):
    """该进程需要的配置，内容由各推理进程自己定义（例如设备、模型目录、子能力开关）。"""

    TYPE: ClassVar[str] = "configure"
    settings: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class TranslateSegment:
    id: str
    text: str


@dataclass(frozen=True, slots=True)
class TranslateRequest(WorkerMessage):
    """NMT 翻译请求。子进程对每个段发送一次 SegmentResult，全部发完后发送 RequestDone。"""

    TYPE: ClassVar[str] = "translate"
    request_id: str
    session_id: str
    source: str
    target: str
    segments: tuple[TranslateSegment, ...]


@dataclass(frozen=True, slots=True)
class OcrRequest(WorkerMessage):
    """OCR 请求，后跟一帧 BGRA 像素（stride × height 字节）。子进程以 OcrResult 或 RequestFailed 结束。"""

    TYPE: ClassVar[str] = "ocr"
    HAS_PAYLOAD: ClassVar[bool] = True
    request_id: str
    session_id: str
    width: int
    height: int
    stride: int
    options: dict[str, JsonValue] = field(default_factory=lambda: cast(dict[str, JsonValue], {}))


@dataclass(frozen=True, slots=True)
class Cancel(WorkerMessage):
    """取消一个请求：尚未开始的直接丢弃并回复 RequestFailed；正在执行的跑完后结果照常发回，由主进程丢弃。"""

    TYPE: ClassVar[str] = "cancel"
    request_id: str


@dataclass(frozen=True, slots=True)
class Shutdown(WorkerMessage):
    TYPE: ClassVar[str] = "shutdown"


# ---- 子进程 → 主进程


@dataclass(frozen=True, slots=True)
class Ready(WorkerMessage):
    """握手与加载完成。capabilities 由各推理进程定义，例如 NMT 进程列出已安装的语言对。"""

    TYPE: ClassVar[str] = "ready"
    protocol_version: int
    capabilities: dict[str, JsonValue]
    versions: dict[str, str]


@dataclass(frozen=True, slots=True)
class Progress(WorkerMessage):
    """模型下载或加载进度。done、total 未知时为 None。"""

    TYPE: ClassVar[str] = "progress"
    kind: str
    detail: str = ""
    done: int | None = None
    total: int | None = None


@dataclass(frozen=True, slots=True)
class Started(WorkerMessage):
    """子进程开始处理该请求。监管器的处理时限从此刻计时（架构文档 8.2 节第 3 条）。"""

    TYPE: ClassVar[str] = "started"
    request_id: str


@dataclass(frozen=True, slots=True)
class SegmentResult(WorkerMessage):
    """一个段的翻译结果。text 与 error 恰好一个非空；code 是 contracts.errors.ErrorCode 的取值。"""

    TYPE: ClassVar[str] = "segment_result"
    request_id: str
    segment_id: str
    text: str | None = None
    error: str | None = None
    code: str | None = None


@dataclass(frozen=True, slots=True)
class OcrResult(WorkerMessage):
    """OCR 结果。版面块的结构在 M4 定义，这里先按 JSON 传递。"""

    TYPE: ClassVar[str] = "ocr_result"
    request_id: str
    blocks: list[JsonValue]


@dataclass(frozen=True, slots=True)
class RequestDone(WorkerMessage):
    """翻译请求的全部段都已发回。"""

    TYPE: ClassVar[str] = "request_done"
    request_id: str


@dataclass(frozen=True, slots=True)
class RequestFailed(WorkerMessage):
    """请求整体失败或被取消。code 是 contracts.errors.ErrorCode 的取值；被取消时为 "cancelled"。"""

    TYPE: ClassVar[str] = "request_failed"
    request_id: str
    code: str
    error: str


CANCELLED = "cancelled"

type MainToWorker = Hello | Configure | TranslateRequest | OcrRequest | Cancel | Shutdown
type WorkerToMain = Ready | Progress | Started | SegmentResult | OcrResult | RequestDone | RequestFailed
type WorkerReply = Started | SegmentResult | OcrResult | RequestDone | RequestFailed

_MESSAGE_TYPES: dict[str, type[WorkerMessage]] = {
    cls.TYPE: cls
    for cls in (
        Hello,
        Configure,
        TranslateRequest,
        OcrRequest,
        Cancel,
        Shutdown,
        Ready,
        Progress,
        Started,
        SegmentResult,
        OcrResult,
        RequestDone,
        RequestFailed,
    )
}


class ProtocolError(Exception):
    """收到无法解析或不符合协议的消息。"""


# ---------------------------------------------------------------- 编解码


def encode(message: WorkerMessage) -> bytes:
    """把消息编码成一帧 UTF-8 JSON。"""
    body: dict[str, Any] = {"type": message.TYPE, **asdict(message)}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode(frame: bytes) -> WorkerMessage:
    """把一帧 JSON 解码成消息对象；格式不对时抛出 ProtocolError。"""
    try:
        body = json.loads(frame.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"消息不是合法的 UTF-8 JSON：{exc}") from exc
    if not isinstance(body, dict):
        raise ProtocolError("消息必须是 JSON 对象")
    payload = cast(dict[str, Any], body)
    kind = payload.pop("type", None)
    cls = _MESSAGE_TYPES.get(kind) if isinstance(kind, str) else None
    if cls is None:
        raise ProtocolError(f"未知的消息类型：{kind!r}")
    names = {f.name for f in fields(cls)}
    if set(payload) != names:
        raise ProtocolError(f"{kind} 消息的字段不符：期望 {sorted(names)}，实际 {sorted(payload)}")
    if cls is TranslateRequest:
        segments = payload["segments"]
        if not isinstance(segments, list):
            raise ProtocolError("translate.segments 必须是数组")
        try:
            payload["segments"] = tuple(TranslateSegment(**s) for s in cast(list[dict[str, Any]], segments))
        except TypeError as exc:
            raise ProtocolError(f"translate.segments 的元素不符：{exc}") from exc
    try:
        return cls(**payload)
    except TypeError as exc:
        raise ProtocolError(f"{kind} 消息无法构造：{exc}") from exc


class FrameConnection(Protocol):
    """`multiprocessing.connection.Connection` 中本协议用到的部分。"""

    def send_bytes(self, buf: bytes) -> None: ...

    def recv_bytes(self) -> bytes: ...


def send_message(conn: FrameConnection, message: WorkerMessage, payload: bytes | None = None) -> None:
    """发送一条消息；带像素数据的消息必须同时给出 payload，其他消息不能给。

    调用方负责串行化：同一个连接上的两次 send_message 不能并发，否则帧会交错。
    """
    if (payload is not None) != message.HAS_PAYLOAD:
        raise ValueError(f"{message.TYPE} 消息{'需要' if message.HAS_PAYLOAD else '不能带'}原始字节帧")
    conn.send_bytes(encode(message))
    if payload is not None:
        conn.send_bytes(payload)


def recv_message(conn: FrameConnection) -> tuple[WorkerMessage, bytes | None]:
    """接收一条消息及其原始字节帧（如果有）。连接关闭时抛出 EOFError。"""
    message = decode(conn.recv_bytes())
    payload = conn.recv_bytes() if message.HAS_PAYLOAD else None
    return message, payload
