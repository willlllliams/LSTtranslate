"""截屏与 OCR 的数据类型和总线消息（需求 CAP、OCR、OV 各条，架构文档第 7 章、8.4 节）。

坐标约定：版面块、文本行、单元格的包围盒都是相对截图左上角的物理像素坐标，左上闭、右下开。
"""

from dataclasses import dataclass
from enum import StrEnum

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import SessionId
from lst.contracts.messages import Command, Event
from lst.contracts.translation import Segment


@dataclass(frozen=True, slots=True)
class RawImage:
    """截图像素：BGRA，每像素 4 字节，每行 stride 字节（stride ≥ width × 4）。

    不用 QImage：contracts 不依赖 Qt。
    不编码成 PNG：压缩约 8 MB 的像素，主线程承担不起（架构文档第 7 章）。
    """

    width: int
    height: int
    stride: int
    data: bytes

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or self.stride < self.width * 4:
            raise ValueError("RawImage 的宽、高必须为正，stride 不能小于 width × 4")
        if len(self.data) < self.stride * self.height:
            raise ValueError("RawImage 的像素数据不足 stride × height 字节")


@dataclass(frozen=True, slots=True)
class CaptureGeometry:
    """截屏选区在屏幕上的位置，覆盖窗口据此在原位置弹出（需求 OV-01）。

    screen_rect 与 selection 是逻辑像素（Qt 的全局坐标），dpr 是该屏幕的设备像素比；
    选区的物理像素尺寸等于截图的宽高。
    """

    screen_name: str
    screen_rect: tuple[int, int, int, int]  # x, y, w, h
    selection: tuple[int, int, int, int]  # x, y, w, h
    dpr: float


@dataclass(frozen=True, slots=True)
class BBox:
    """轴对齐包围盒，物理像素，相对截图左上角。"""

    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0


class BlockCategory(StrEnum):
    """版面块类别（需求 OCR-01）。"""

    TEXT = "text"
    TITLE = "title"
    TABLE = "table"
    FORMULA = "formula"
    CHART = "chart"
    IMAGE = "image"
    HEADER_FOOTER = "header_footer"


@dataclass(frozen=True, slots=True)
class TextLine:
    """一行识别结果。score 是识别置信度，0 到 1。"""

    bbox: BBox
    text: str
    score: float


@dataclass(frozen=True, slots=True)
class TableCell:
    """表格单元格（需求 OCR-02）。行列号从 0 开始。"""

    row: int
    col: int
    row_span: int
    col_span: int
    bbox: BBox
    text: str


@dataclass(frozen=True, slots=True)
class LayoutBlock:
    """一个版面块（需求 OCR-02）。

    order 是阅读顺序（从 0 开始）；lines 是块内的文本行（文本类块才有）；
    表格块带 cells，公式块带 latex，图表块带 chart_table（图表转换出的数据表，按行排列）。
    """

    id: str
    category: BlockCategory
    bbox: BBox
    order: int
    score: float
    text: str = ""
    lines: tuple[TextLine, ...] = ()
    cells: tuple[TableCell, ...] = ()
    latex: str | None = None
    chart_table: tuple[tuple[str, ...], ...] | None = None


@dataclass(frozen=True, slots=True)
class OcrSegment:
    """参加竞速的一个段，以及覆盖窗口渲染它所需的位置信息。

    block_id 指向所属的版面块；表格单元格的段带 cell=(row, col)；bbox 是这一段在截图中的范围。
    """

    segment: Segment
    block_id: str
    bbox: BBox
    cell: tuple[int, int] | None = None


# ---------------------------------------------------------------- 消息


@dataclass(frozen=True, slots=True)
class CaptureCompleted(Event):
    """用户完成框选（需求 CAP-01 至 CAP-05）。会话号由 ui.capture 用 UUID4 生成，跨重启唯一。"""

    session_id: SessionId
    image: RawImage
    geometry: CaptureGeometry


@dataclass(frozen=True, slots=True)
class OcrCompleted(Event):
    """识别与段落合并完成。blocks 按阅读顺序排列；segments 是要参加竞速的段，按阅读顺序排列。"""

    session_id: SessionId
    blocks: tuple[LayoutBlock, ...]
    segments: tuple[OcrSegment, ...]


@dataclass(frozen=True, slots=True)
class OcrFailed(Event):
    """识别失败（需求 OCR-08）。error 是技术细节，界面按 code 显示原因与建议。"""

    session_id: SessionId
    code: ErrorCode
    error: str


@dataclass(frozen=True, slots=True)
class RetryOcr(Command):
    """重新识别上一次失败的截图（需求 OCR-08）。"""

    session_id: SessionId
