"""覆盖窗口（需求 OV-01 至 OV-07、CAP-07、UI-04，架构文档 11.5 节）。

窗口是一个无边框、置顶的 QWidget，自己绘制全部内容。坐标规则见 overlay.geometry：
包围盒是截图坐标（相对选区的物理像素），绘制时乘以 $k$ 换到设备像素，画笔也工作在设备像素坐标上，
字号按设备像素适配。窗口被拖到缩放比例不同的屏幕上时（Qt 发出 DevicePixelRatioChange，或窗口的
screenChanged），$k$ 改变，全部段按新屏幕重新适配字号后重绘。

绘制顺序：

1. 底图：截图缩小到约 1/10 后再拉伸回原尺寸（双线性插值，效果是模糊），盖一层白色或黑色半透明层；
   亮底调亮、暗底调暗，文字颜色随之取黑或白（OV-02）。缩小与平均亮度在截图到达时各算一次。
2. 公式、图片、图表块：画原图（OV-03）。
3. 表格块：画单元格的网格线。
4. 各段：在段的包围盒内先铺一层与底图同色调的不透明度较高的底，再画选中文字的底色，最后画译文（或原文），
   字号自动适配。图表数据表的段只在点开图表后显示，数据表按行列均分图表块的范围。
5. 角落的进度（OV-07）、固定标识，以及“识别中”“识别失败”等状态文字。

文字排版（``TextBox``）：每段用 QTextLayout 按词换行，并按配置对齐实际字形，见 ``layout_text``。
绘制、选中底色与鼠标命中测试共用同一份排版结果，
选中的字符范围与画出来的字形一一对应。排版结果按段缓存，键是（显示的文字、字号、是否标题、文字区域的
设备像素宽高）；分帧适配测完字号、缩放或换屏改变文字区域时键随之改变，下次用到时重新排版。

字号适配分帧进行（架构文档 11.2 节）：新出现的段先用估算字号，计时器每轮最多测 8 毫秒，
测完的段只重绘自己的范围。测量结果按文字、框尺寸、标题样式和目标字号缓存，同样的段不重复测。

原始识别框与绘制框分别保留。字号从原文的行框估算，乘以用户设置的目标字号倍数；默认目标为原文字号的 1.1 倍。
绘制框先按宽高倍数与外边距调整，再按配置保留横向范围扩高，或同时扩宽和扩高。
扩展使用截图像素，受截图边界、相邻内容和扩框上限约束；空间不足时适配较小字号。
文字内边距与横纵对齐可配置，绘制、过渡和选字共用排版几何。表格网格与无需翻译的符号保持原框。

译文替换的过渡（RACE-06、OV-04）：显示的文字变化时，这一段播放 150 毫秒的过渡，旧文字的不透明度从 1 降到 0，
新文字从 0 升到 1。每段从自己的译文到达时刻起独立计时（SegmentItem.fade_started），后到的译文不影响
已经在过渡中的段；一个计时器按帧推进全部过渡中的段，每一帧只重绘这些段的范围。

操作（OV-05、UI-09）：鼠标、滚轮与键盘事件都按键位表（``Keymap``，配置的 [bindings] 节，架构文档 11.8 节）
查出操作再执行，括号里是默认键位：

- 切换原文与译文（右键单击）；
- 拖动窗口（右键拖动）；
- 缩放整张图（滚轮）：以光标为中心，每格乘以或除以 1.1；
- 放大、缩小（Ctrl+=、Ctrl+-）：以窗口中心为锚点，步长与滚轮一格相同；还原（Ctrl+0）回到 1 倍。
  缩放的是整张图，底图与译文一起变，窗口尺寸随之改变；
- 固定在最上层（中键单击）：再按一次取消；未固定时窗口失去焦点就关闭；
- 选择文字（左键拖动）：从按下处的字符选到当前位置的字符，见下文“文字选择”。同一个按键单击
  （没有移动、也没有绑定别的单击操作）时清除选择，点在图表上时展开或收起它的数据表；
- 关闭（Esc）。

复制选中的文字用系统标准的复制键（``QKeySequence.StandardKey.Copy``，Windows 上是 Ctrl+C 与 Ctrl+Insert），
不进键位表。键位表里有操作绑定了同一个组合键时，按键位表执行。

文字选择：选择由锚点与插入点两个位置（段号、字符位置）表示，选的是当前显示的文字（原文模式下是原文）。
两个位置按阅读顺序（``visible_items`` 的顺序）排先后：同一段内选中两者之间的字符；跨段时，
起点所在段从起点到段尾，中间各段全部，终点所在段从段首到终点。复制时各段的选中部分按阅读顺序用换行连接。
鼠标不在任何段上时按离它最近的段做命中测试，拖过段与段之间的空白时选择照常延伸。
同一个按键双击选中一个词（词边界按 ``QTextBoundaryFinder``），三击选中整段；双击或三击后继续拖动，
选择按词或按段延伸。切换原文与译文、识别出新结果时清除选择；选中范围里的段换了译文时也清除，
旧的字符位置对新文字没有意义。

按下后移动超过 4 个逻辑像素算拖动，否则算单击；按键加修饰键没有绑定拖动时，单击在按下时就生效
（``MouseGestures``）。
"""

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal, Protocol, cast

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTextBoundaryFinder,
    QTimer,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QScreen,
    QShowEvent,
    QTextLayout,
    QTextLine,
    QTextOption,
    QWheelEvent,
    QWindow,
)
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from lst.contracts.ids import SegmentId
from lst.contracts.ocr import BBox, BlockCategory, CaptureGeometry, LayoutBlock, OcrSegment, RawImage
from lst.contracts.translation import SegmentView, SkipReason
from lst.ui.common.bindings import DRAG_SLOP, Keymap, MouseGestures, Scope
from lst.ui.i18n import tr
from lst.ui.overlay.fitting import MIN_FONT_PX, estimate_font_size, fit_font_size
from lst.ui.overlay.geometry import (
    RectF,
    bbox_to_view,
    device_per_pixel,
    is_light,
    logical_per_pixel,
    luminance,
    wheel_zoom,
    window_size,
    zoom_about,
)
from lst.ui.overlay.layout import OverlayLayoutConfig, confine_box, overlaps, sized_box
from lst.ui.theme import TOKENS, style_primary_button, theme_colors, ui_font

FIT_BUDGET_MS: Final = 8.0
"""字号测量每轮最多占用的时间（架构文档 11.2 节）。"""

FADE_FRAME_MS: Final = 16
"""过渡计时器的间隔，约 60 帧每秒。"""

BLUR_FACTOR: Final = 10
"""底图先缩小到 1/BLUR_FACTOR 再拉伸回来，起模糊作用。"""

_STATUS_PX: Final = 14
_PROGRESS_PX: Final = 11
_TEXT_FLAGS: Final = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap
_IMAGE_LIKE: Final = frozenset({BlockCategory.FORMULA, BlockCategory.IMAGE, BlockCategory.CHART})


_LINE_SEPARATOR: Final = "\u2028"
"""Unicode 行分隔符：drawText 把换行符换成它，段内换行不拆成新段落。"""

type _FitKey = tuple[str, str, int, int, int, int, bool, int]
"""字号缓存的键：显示文字、原文、文字区宽高、包围盒宽高、标题样式、目标字号，尺寸单位为设备像素。"""

type _BoxKey = tuple[str, int, bool, float, float]
"""排版缓存的键：文字、字号（设备像素）、是否标题、文字区域的设备像素宽高。"""

type Unit = Literal["char", "word", "paragraph"]
"""选择的单位：拖动按字符，双击按词，三击按段。"""

type Position = tuple[SegmentId, int]
"""文字中的一个位置：段号与字符位置（0 到文字长度）。"""


class Phase(Enum):
    """窗口所处的阶段。"""

    RECOGNIZING = "recognizing"
    FAILED = "failed"
    RESULT = "result"


class OverlayActions(Protocol):
    """窗口把用户操作交给展示器的接口。"""

    def retry(self) -> None: ...
    def copy(self, text: str) -> None: ...
    def window_closed(self, window: "OverlayWindow") -> None: ...


@dataclass(slots=True)
class SegmentItem:
    """窗口里的一个段。bbox 是渲染范围（截图坐标）；chart 不为空时它属于该图表块的数据表，点开图表才显示。

    fade_from、fade_px 是过渡中旧文字与它的字号，fade_started 是这一段过渡开始的时刻（窗口时钟，秒）；
    三项在过渡结束后清空。
    """

    segment_id: SegmentId
    block_id: str
    bbox: BBox
    source: str
    display: str
    title: bool = False
    chart: str | None = None
    view: SegmentView | None = None
    font_px: int | None = None
    fade_from: str | None = None
    fade_px: int | None = None
    fade_started: float | None = None
    source_bbox: BBox | None = None
    source_px: int = MIN_FONT_PX
    fixed_box: bool = False


@dataclass(frozen=True, slots=True)
class TextBox:
    """一段文字的排版结果。layout 里各行的坐标以文字区域左上角为原点，单位是设备像素；
    top 是按实际字形高度对齐后的纵向偏移，绘制和命中测试都使用这项偏移。"""

    key: _BoxKey
    layout: QTextLayout
    top: float


def layout_text(
    text: str,
    font: QFont,
    width: float,
    height: float,
    horizontal: str = "left",
    vertical: str = "center",
) -> tuple[QTextLayout, float]:
    """按词换行并按配置对齐，返回排版结果与实际字形的纵向对齐偏移。

    规则：行宽为 width、按词换行；行与行之间隔字体的 leading，每行的纵坐标向上取整到整像素；
    行的底边达到 height 后不再排后面的行；用字形实际边界计算顶部、居中或底部对齐的偏移。
    换行符换成 Unicode 行分隔符、回车换成空格，与 drawText 的预处理相同，字符位置与原文字一一对应。
    """
    shaped = text.replace("\r", " ").replace("\n", _LINE_SEPARATOR)
    layout = QTextLayout(shaped, font)
    option = QTextOption(
        {
            "left": Qt.AlignmentFlag.AlignLeft,
            "center": Qt.AlignmentFlag.AlignHCenter,
            "right": Qt.AlignmentFlag.AlignRight,
        }[horizontal]
    )
    option.setWrapMode(QTextOption.WrapMode.WordWrap)
    layout.setTextOption(option)
    layout.setCacheEnabled(True)
    metrics = QFontMetricsF(font)
    leading = metrics.leading()
    used = -leading
    layout.beginLayout()
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(max(0.0, width))
        used = float(math.ceil(used + leading))
        line.setPosition(QPointF(0.0, used))
        used += line.height()
        if used >= height:
            break
    layout.endLayout()
    if not text:
        used = metrics.height()
    ink = layout_ink_bounds(layout)
    spare = height - ink.height()
    top = {"top": 0.0, "center": spare / 2, "bottom": spare}[vertical] - ink.top()
    return layout, top


def layout_ink_bounds(layout: QTextLayout, metrics: QFontMetricsF | None = None) -> QRectF:
    """实际字形范围；用各视觉行的字形高度消除字体行框上下留白，兼容中英文字体回退。"""
    metrics = metrics if metrics is not None else QFontMetricsF(layout.font())
    encoded = layout.text().encode("utf-16-le")
    bounds = QRectF()
    for index in range(layout.lineCount()):
        line = layout.lineAt(index)
        text = (
            encoded[2 * line.textStart() : 2 * (line.textStart() + line.textLength())]
            .decode("utf-16-le")
            .rstrip()
        )
        if not text.strip():
            continue
        ink = metrics.tightBoundingRect(text)
        ink.translate(0, line.y() + line.ascent())
        bounds = ink if bounds.isNull() else bounds.united(ink)
    return bounds


def cursor_at(layout: QTextLayout, x: float, y: float, length: int, *, on_character: bool) -> int:
    """排版坐标 (x, y) 处的字符位置。

    on_character 为假时取离 x 最近的字符间隙（拖选用），结果在 0 到 length 之间；为真时取 x 落在其上的字符
    （双击取词用），结果在 0 到 length − 1 之间。点在第一行上方取文字开头，在最后一行下方取文字末尾。
    """
    count = layout.lineCount()
    if count == 0 or length == 0:
        return 0
    last_position = length - 1 if on_character else length
    if y < layout.lineAt(0).y():
        return 0
    mode = (
        QTextLine.CursorPosition.CursorOnCharacter
        if on_character
        else QTextLine.CursorPosition.CursorBetweenCharacters
    )
    for index in range(count):
        line = layout.lineAt(index)
        if y < line.y() + line.height():
            return max(0, min(line.xToCursor(x, mode), last_position))
    return last_position


def range_rects(layout: QTextLayout, start: int, end: int) -> list[QRectF]:
    """字符范围 [start, end) 在排版坐标里占的矩形，每行一个，高度是行高。"""
    rects: list[QRectF] = []
    for index in range(layout.lineCount()):
        line = layout.lineAt(index)
        first = line.textStart()
        lo, hi = max(start, first), min(end, first + line.textLength())
        if lo >= hi:
            continue
        x0, x1 = _cursor_x(line, lo), _cursor_x(line, hi)
        left, right = min(x0, x1), max(x0, x1)
        rects.append(QRectF(left, line.y(), max(right - left, 1.0), line.height()))
    return rects


def _cursor_x(line: QTextLine, position: int) -> float:
    # PySide6 把 C++ 的 cursorToX(int *cursorPos, Edge) 绑定成返回 (横坐标, 位置) 的元组。
    result: object = line.cursorToX(position)
    if isinstance(result, tuple):
        return float(cast(tuple[float, int], result)[0])
    return float(cast(float, result))


def word_span(text: str, position: int) -> tuple[int, int]:
    """position 处字符所在的词 [起, 止)，词边界按 QTextBoundaryFinder（Unicode UAX #29）。

    西文按单词与空白、标点分开。这个规则不带词典，汉字与假名每个字各成一词，与双语窗口译文区
    （QTextEdit，同样用 QTextBoundaryFinder）的双击行为一致。
    """
    length = len(text)
    if length == 0:
        return (0, 0)
    index = max(0, min(position, length - 1))
    finder = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Word, text)
    finder.setPosition(index)
    start = index if finder.isAtBoundary() else finder.toPreviousBoundary()
    finder.setPosition(index)
    end = finder.toNextBoundary()
    return (max(0, start), end if end > index else length)


def to_qimage(image: RawImage) -> QImage:
    """RawImage（BGRA）→ 自己持有像素的 QImage。透明度字节恒为 0xFF，按 RGB32 处理。"""
    wrapped = QImage(image.data, image.width, image.height, image.stride, QImage.Format.Format_RGB32)
    return wrapped.copy()


def mean_luminance(image: QImage) -> float:
    """平均亮度：先平滑缩小到 16×16，再对 256 个像素求平均。"""
    small = image.scaled(
        16, 16, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    total = 0.0
    for y in range(small.height()):
        for x in range(small.width()):
            color = small.pixelColor(x, y)
            total += luminance(color.red(), color.green(), color.blue())
    return total / max(1, small.width() * small.height())


def chart_cell_bbox(block: BBox, row: int, col: int, rows: int, cols: int) -> BBox:
    """图表数据表的单元格范围：按行列数均分图表块的包围盒。"""
    width, height = block.width / max(1, cols), block.height / max(1, rows)
    x0, y0 = block.x0 + round(col * width), block.y0 + round(row * height)
    x1, y1 = block.x0 + round((col + 1) * width), block.y0 + round((row + 1) * height)
    return BBox(x0, y0, max(x1, x0 + 1), max(y1, y0 + 1))


class OverlayWindow(QWidget):
    """覆盖窗口。规则见模块说明。"""

    def __init__(
        self,
        image: RawImage,
        geometry: CaptureGeometry,
        actions: OverlayActions,
        keymap: Keymap | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        flags = (
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setWindowTitle("LSTtranslate")
        self._actions = actions
        self._geometry = geometry
        self._pixel_size = (image.width, image.height)
        self._image = to_qimage(image)
        small = self._image.scaled(
            max(1, image.width // BLUR_FACTOR),
            max(1, image.height // BLUR_FACTOR),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._blurred = QPixmap.fromImage(small)
        self._light = is_light(mean_luminance(self._image))
        self._text_color = QColor(0, 0, 0) if self._light else QColor(255, 255, 255)
        self._wash = QColor(255, 255, 255, 150) if self._light else QColor(0, 0, 0, 150)
        self._panel = QColor(250, 250, 250, 215) if self._light else QColor(20, 20, 20, 205)
        self._base_font = ui_font(12)
        self._metrics: dict[tuple[int, bool], QFontMetricsF] = {}
        self._layout_config = OverlayLayoutConfig()

        self._phase = Phase.RECOGNIZING
        self._status = tr("overlay.recognizing")
        self._detail = ""
        self._blocks: tuple[LayoutBlock, ...] = ()
        self._items: dict[SegmentId, SegmentItem] = {}
        self._order: list[SegmentId] = []
        self._progress: tuple[int, int] | None = None
        self._show_source = False
        self._pinned = False
        self._was_active = False
        self._zoom = 1.0
        self._expanded: set[str] = set()
        self._anchor: Position | None = None
        self._caret: Position | None = None
        self._unit: Unit = "char"
        self._unit_span: tuple[SegmentId, int, int] | None = None
        self._last_double: tuple[float, QPointF, Qt.MouseButton] | None = None
        self._boxes: dict[SegmentId, TextBox] = {}
        self._fade_boxes: dict[SegmentId, TextBox] = {}

        self._keymap = keymap if keymap is not None else Keymap.defaults(Scope.OVERLAY)
        self._gestures = MouseGestures(self._keymap)
        self._press_local = QPointF()
        self._press_global = QPointF()
        self._press_origin = QPoint()

        self._fit_cache: dict[_FitKey, int] = {}
        self._measures: dict[tuple[str, int, int, bool, int], int] = {}
        self._fit_queue: list[SegmentId] = []
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self._fit_some)
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._fading: set[SegmentId] = set()
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(FADE_FRAME_MS)
        self._fade_timer.timeout.connect(self._fade_step)
        self._screen_hooked = False

        self._retry = QPushButton(tr("overlay.retry"), self)
        style_primary_button(self._retry)
        self._retry.clicked.connect(self._actions.retry)
        self._retry.hide()

        self._place()

    # ---- 查询（测试与展示器使用） ----

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def status_text(self) -> str:
        return self._status

    @property
    def detail_text(self) -> str:
        return self._detail

    @property
    def retry_button(self) -> QPushButton:
        return self._retry

    @property
    def items(self) -> Mapping[SegmentId, SegmentItem]:
        return self._items

    @property
    def order(self) -> Sequence[SegmentId]:
        return tuple(self._order)

    @property
    def showing_source(self) -> bool:
        return self._show_source

    @property
    def pinned(self) -> bool:
        return self._pinned

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def keymap(self) -> Keymap:
        return self._keymap

    @property
    def selected(self) -> frozenset[SegmentId]:
        """选中了文字的段。"""
        return frozenset(self.selection_ranges())

    @property
    def selection_unit(self) -> Unit:
        """当前选择的单位：拖动是字符，双击是词，三击是段。"""
        return self._unit

    @property
    def progress(self) -> tuple[int, int] | None:
        return self._progress

    @property
    def light_background(self) -> bool:
        return self._light

    @property
    def fitting_pending(self) -> bool:
        return bool(self._fit_queue)

    @property
    def expanded_charts(self) -> frozenset[str]:
        return frozenset(self._expanded)

    def fading_segments(self) -> frozenset[SegmentId]:
        """正在播放过渡的段。"""
        return frozenset(self._fading)

    def fade_progress(self, segment_id: SegmentId) -> float:
        """这一段过渡的进度：0 是刚开始，1 是新文字完全显示。没有在过渡中的段为 1。"""
        item = self._items.get(segment_id)
        if item is None or item.fade_started is None or segment_id not in self._fading:
            return 1.0
        return min(1.0, max(0.0, (self._clock() - item.fade_started) * 1000.0 / TOKENS.fade_ms))

    def device_ratio(self) -> float:
        """窗口当前所在屏幕的设备像素比 r_d。"""
        return self.devicePixelRatioF()

    def scale_logical(self) -> float:
        """一个截图像素对应的窗口逻辑长度 s。"""
        return logical_per_pixel(self._geometry.dpr, self._zoom)

    def scale_device(self) -> float:
        """一个截图像素对应的设备像素数 k。"""
        return device_per_pixel(self._geometry.dpr, self._zoom, self.device_ratio())

    def shown_text(self, item: SegmentItem) -> str:
        return item.source if self._show_source else item.display

    def visible_items(self) -> list[SegmentItem]:
        """当前画出来的段：图表数据表的段只在图表点开后显示。"""
        return [
            self._items[sid]
            for sid in self._order
            if self._items[sid].chart is None or self._items[sid].chart in self._expanded
        ]

    # ---- 展示器调用 ----

    def present(self) -> None:
        """显示并激活窗口。不显示在屏幕上的窗口（WA_DontShowOnScreen，测试）不抢前台。"""
        self.show()
        if not self.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
            self.raise_()
            self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def show_recognizing(self, loading: bool) -> None:
        """识别中；OCR 进程还在加载模型时显示“识别模型加载中”（OCR-07）。"""
        self._phase = Phase.RECOGNIZING
        self._status = tr("overlay.loading_model" if loading else "overlay.recognizing")
        self._detail = ""
        self._retry.hide()
        self.update()

    def show_failed(self, reason: str, suggestion: str) -> None:
        """识别失败：显示原因与建议，以及“重试”按钮（OCR-08）。"""
        self._phase = Phase.FAILED
        self._status = tr("overlay.failed", reason=reason)
        self._detail = suggestion
        self._retry.setText(tr("overlay.retry"))
        self._layout_retry()
        self._retry.show()
        self.update()

    def show_result(self, blocks: Sequence[LayoutBlock], segments: Sequence[OcrSegment]) -> None:
        """识别完成：按版面块渲染原文（第 7 章第 6 步），译文到达前段显示原文。"""
        self._phase = Phase.RESULT
        self._retry.hide()
        self._blocks = tuple(blocks)
        by_id = {b.id: b for b in self._blocks}
        self.clear_selection()
        self._end_fades()
        self._boxes.clear()
        self._items.clear()
        self._order.clear()
        charts: dict[str, tuple[int, int]] = {}
        for segment in segments:
            block = by_id.get(segment.block_id)
            if block is not None and block.category is BlockCategory.CHART and segment.cell is not None:
                rows, cols = charts.get(block.id, (0, 0))
                charts[block.id] = (max(rows, segment.cell[0] + 1), max(cols, segment.cell[1] + 1))
        for segment in segments:
            block = by_id.get(segment.block_id)
            bbox, chart = segment.bbox, None
            if block is not None and block.id in charts and segment.cell is not None:
                rows, cols = charts[block.id]
                bbox = chart_cell_bbox(block.bbox, segment.cell[0], segment.cell[1], rows, cols)
                chart = block.id
            text = segment.segment.text
            self._items[segment.segment.id] = SegmentItem(
                segment.segment.id,
                segment.block_id,
                bbox,
                text,
                text,
                title=block is not None and block.category is BlockCategory.TITLE,
                chart=chart,
                source_bbox=bbox,
                fixed_box=block is not None
                and block.category not in (BlockCategory.TEXT, BlockCategory.TITLE),
            )
            self._order.append(segment.segment.id)
        self._expand_line_boxes(by_id)
        for item in self._items.values():
            block = by_id.get(item.block_id)
            source = item.source_bbox or item.bbox
            samples = [
                px
                for line in (() if block is None else block.lines)
                if overlaps(line.bbox, source) and any(c.isalpha() for c in line.text)
                if (px := self._source_line_px(line.text, line.bbox)) is not None
            ]
            single = self._source_line_px(item.source, source)
            if samples:
                item.source_px = sorted(samples)[len(samples) // 2]
            elif single is not None and source.height <= 1.8 * single:
                item.source_px = single
            else:
                item.source_px = self._measure(
                    item.source, source.width, source.height, item.title, max(MIN_FONT_PX, source.height)
                )
        self._reflow_boxes()
        self._status = "" if self._items else tr("overlay.no_text")
        self._refit()
        self.update()

    def apply_views(self, views: Sequence[SegmentView]) -> list[SegmentId]:
        """竞速快照（OV-04）：显示文字变化的段播放过渡，只重绘变化的段。返回显示文字变化的段号。

        每段的过渡从它自己的译文到达时刻开始计时，同一次刷新里没有变化的段、以及之前已经在过渡中的段
        都不受影响。一段在过渡中又换了译文时，旧文字取过渡开始前的那一个，过渡从这次到达重新计时。
        """
        changed: list[SegmentId] = []
        dirty = QRect()
        now = self._clock()
        for view in views:
            item = self._items.get(view.segment_id)
            if item is None:
                continue
            item.view = view
            if view.display_text != item.display:
                if not self._show_source:
                    item.fade_from = item.display
                    item.fade_px = item.font_px
                    item.fade_started = now
                    self._fading.add(item.segment_id)
                item.display = view.display_text
                changed.append(item.segment_id)
            dirty = dirty.united(self._item_rect(item))
        if changed and not self._show_source and self._selection_touches(changed):
            self.clear_selection()
        if views:
            self._reflow_boxes()
            self._refit()
            if self._fading and not self._fade_timer.isActive():
                self._fade_timer.start()
        if not dirty.isNull():
            self.update(dirty)
        return changed

    def set_progress(self, finished: int, total: int) -> None:
        self._progress = (finished, total)
        self.update(self._progress_rect().toAlignedRect().adjusted(-2, -2, 2, 2))

    def selection_ranges(self) -> dict[SegmentId, tuple[int, int]]:
        """各段选中的字符范围 [起, 止)，按阅读顺序；没有选中文字的段不出现。规则见模块说明“文字选择”。"""
        items = self.visible_items()
        bounds = self._selection_bounds(items)
        if bounds is None:
            return {}
        (first, lo), (last, hi) = bounds
        ranges: dict[SegmentId, tuple[int, int]] = {}
        for index in range(first, last + 1):
            item = items[index]
            length = len(self.shown_text(item))
            start = min(lo, length) if index == first else 0
            end = min(hi, length) if index == last else length
            if start < end:
                ranges[item.segment_id] = (start, end)
        return ranges

    def _selection_bounds(
        self, items: Sequence[SegmentItem]
    ) -> tuple[tuple[int, int], tuple[int, int]] | None:
        """选择的起点与终点（段在 items 里的序号、字符位置），按阅读顺序排好；没有选择时为 None。"""
        if self._anchor is None or self._caret is None:
            return None
        index = {item.segment_id: i for i, item in enumerate(items)}
        anchor, caret = self._anchor, self._caret
        if anchor[0] not in index or caret[0] not in index:
            return None
        a, c = (index[anchor[0]], anchor[1]), (index[caret[0]], caret[1])
        start, end = min(a, c), max(a, c)
        return None if start == end else (start, end)

    def selected_text(self) -> str:
        """选中的文字：各段的选中部分按阅读顺序，段与段之间用换行连接。没有选中时是空字符串。"""
        ranges = self.selection_ranges()
        parts: list[str] = []
        for item in self.visible_items():
            span = ranges.get(item.segment_id)
            if span is not None:
                parts.append(self.shown_text(item)[span[0] : span[1]])
        return "\n".join(parts)

    def copy_selection(self) -> None:
        """复制选中的文字（标准复制键）；没有选中时什么也不做。"""
        text = self.selected_text()
        if text:
            self._actions.copy(text)

    def select_range(self, start: Position, end: Position) -> None:
        """按字符选中从 start 到 end 的文字（测试与截图自查用）。"""
        self._unit = "char"
        self._unit_span = None
        self._set_selection(start, end)

    def clear_selection(self) -> None:
        self._unit = "char"
        self._unit_span = None
        self._set_selection(None, None)

    def _set_selection(self, anchor: Position | None, caret: Position | None) -> None:
        if (anchor, caret) != (self._anchor, self._caret):
            self._anchor, self._caret = anchor, caret
            self.update()

    def _selection_touches(self, segments: Sequence[SegmentId]) -> bool:
        """segments 里有没有段落在选择的起点与终点之间（含两端所在的段）。"""
        items = self.visible_items()
        bounds = self._selection_bounds(items)
        if bounds is None:
            return False
        inside = {items[i].segment_id for i in range(bounds[0][0], bounds[1][0] + 1)}
        return not inside.isdisjoint(segments)

    def toggle_source(self) -> None:
        self._show_source = not self._show_source
        self.clear_selection()
        self._end_fades()
        self._reflow_boxes()
        self._refit()
        self.update()

    def toggle_pinned(self) -> None:
        self._pinned = not self._pinned
        self.update()

    def set_zoom(self, zoom: float, anchor: QPointF | None = None) -> None:
        """改变缩放倍数，窗口尺寸随之改变；anchor 是窗口内的逻辑坐标，缩放时它下面的内容保持在原处。"""
        old = self._zoom
        if abs(zoom - old) < 1e-9:
            return
        self._zoom = zoom
        width, height = window_size(*self._pixel_size, self._geometry.dpr, zoom)
        cursor = (anchor.x(), anchor.y()) if anchor is not None else (0.0, 0.0)
        x, y = zoom_about((self.x(), self.y()), cursor, old, zoom)
        self.setGeometry(QRect(round(x), round(y), width, height))
        self._layout_retry()
        self._reflow_boxes()
        self._refit()
        self.update()

    def set_keymap(self, keymap: Keymap) -> None:
        """换用新的键位表（配置变化时由展示器调用）。正在进行的拖动按按下时的键位做完。"""
        self._keymap = keymap
        self._gestures.keymap = keymap

    def set_layout(self, config: OverlayLayoutConfig) -> None:
        """布局参数热更新；始终从原识别框重算，缩放与内外边距不会累计。"""
        if config == self._layout_config:
            return
        self._layout_config = config
        self._fit_cache.clear()
        self.clear_selection()
        self._end_fades()
        self._reflow_boxes()
        self._refit()
        self.update()

    def zoom_step(self, steps: int) -> None:
        """以窗口中心为锚点放大（steps > 0）或缩小，每一步与滚轮转过一格相同。"""
        self.set_zoom(wheel_zoom(self._zoom, 120 * steps), self._center())

    def zoom_reset(self) -> None:
        """以窗口中心为锚点还原到 1 倍。"""
        self.set_zoom(1.0, self._center())

    def _center(self) -> QPointF:
        return QPointF(self.width() / 2, self.height() / 2)

    def relayout(self) -> None:
        """屏幕或设备像素比变化后，全部段按新的设备像素重新适配字号（CAP-07、UI-04）。"""
        self._metrics.clear()
        self._layout_retry()
        self._reflow_boxes()
        self._refit()
        self.update()

    def focus_lost(self) -> None:
        """失去焦点：未固定时关闭（OV-05）。"""
        if not self._pinned:
            self.close()

    # ---- 位置 ----

    def _place(self) -> None:
        """在选区原位置、按选区的尺寸摆放窗口（OV-01）。"""
        screen = next((s for s in QGuiApplication.screens() if s.name() == self._geometry.screen_name), None)
        if screen is not None:
            self.setScreen(screen)
        x, y, _w, _h = self._geometry.selection
        width, height = window_size(*self._pixel_size, self._geometry.dpr, self._zoom)
        self.setGeometry(QRect(x, y, width, height))

    def _layout_retry(self) -> None:
        hint = self._retry.sizeHint()
        x = max(0, (self.width() - hint.width()) // 2)
        y = max(
            0, min(self.height() - hint.height() - TOKENS.spacing_s, self.height() // 2 + TOKENS.spacing_s)
        )
        self._retry.setGeometry(x, y, hint.width(), hint.height())

    def _item_rect(self, item: SegmentItem) -> QRect:
        """段在窗口里的逻辑坐标范围（取整后外扩 2 像素，供局部重绘）。"""
        rect = bbox_to_view(item.bbox, self.scale_logical())
        return QRectF(rect.x, rect.y, rect.width, rect.height).toAlignedRect().adjusted(-2, -2, 2, 2)

    def _item_at(self, position: QPointF) -> SegmentItem | None:
        s = self.scale_logical()
        px, py = position.x() / s, position.y() / s
        for item in reversed(self.visible_items()):
            box = item.bbox
            if box.x0 <= px < box.x1 and box.y0 <= py < box.y1:
                return item
        return None

    def _block_at(self, position: QPointF) -> LayoutBlock | None:
        s = self.scale_logical()
        px, py = position.x() / s, position.y() / s
        for block in self._blocks:
            box = block.bbox
            if box.x0 <= px < box.x1 and box.y0 <= py < box.y1:
                return block
        return None

    # ---- 文字排版与选择 ----

    def _nearest_index(self, position: QPointF, items: Sequence[SegmentItem]) -> int:
        """position（窗口逻辑坐标）所在的段在 items 里的序号；不在任何段上时取包围盒离它最近的段。"""
        s = self.scale_logical()
        px, py = position.x() / s, position.y() / s
        for index in range(len(items) - 1, -1, -1):
            box = items[index].bbox
            if box.x0 <= px < box.x1 and box.y0 <= py < box.y1:
                return index

        def distance(index: int) -> float:
            box = items[index].bbox
            dx = max(box.x0 - px, 0.0, px - box.x1)
            dy = max(box.y0 - py, 0.0, py - box.y1)
            return math.hypot(dx, dy)

        return min(range(len(items)), key=distance)

    def hit_test(self, position: QPointF, *, on_character: bool = False) -> Position | None:
        """窗口逻辑坐标处的文字位置，用与绘制相同的排版结果计算；不在结果阶段或没有显示的段时为 None。"""
        items = self.visible_items()
        if self._phase is not Phase.RESULT or not items:
            return None
        item = items[self._nearest_index(position, items)]
        box = self._text_box(item)
        inner = self._inner_device(item)
        ratio = self.device_ratio()
        outer = bbox_to_view(item.bbox, self.scale_device())
        if position.y() * ratio < outer.y:
            return item.segment_id, 0
        if position.y() * ratio >= outer.bottom:
            return item.segment_id, max(0, len(self.shown_text(item)) - int(on_character))
        x = position.x() * ratio - inner.x
        y = position.y() * ratio - inner.y - box.top
        length = len(self.shown_text(item))
        return (item.segment_id, cursor_at(box.layout, x, y, length, on_character=on_character))

    def selection_rects(self, segment_id: SegmentId) -> list[QRectF]:
        """段里选中文字的底色矩形（窗口设备像素坐标），与绘制时填充的矩形相同。"""
        item = self._items.get(segment_id)
        span = self.selection_ranges().get(segment_id)
        if item is None or span is None:
            return []
        box = self._text_box(item)
        inner = self._inner_device(item)
        outer = bbox_to_view(item.bbox, self.scale_device())
        clip = QRectF(outer.x, outer.y, outer.width, outer.height)
        visible = [
            rect.translated(inner.x, inner.y + box.top).intersected(clip)
            for rect in range_rects(box.layout, *span)
        ]
        return [rect for rect in visible if not rect.isEmpty()]

    def _text_box(self, item: SegmentItem) -> TextBox:
        """段当前显示文字的排版结果，按 _BoxKey 缓存；键变了（字号测完、缩放、换屏、换文字）就重新排版。"""
        inner = self._inner_device(item)
        key: _BoxKey = (self.shown_text(item), self.font_px(item), item.title, inner.width, inner.height)
        box = self._boxes.get(item.segment_id)
        if box is None or box.key != key:
            box = self._make_box(key)
            self._boxes[item.segment_id] = box
        return box

    def _fade_box(self, item: SegmentItem, text: str, px: int) -> TextBox:
        """过渡中旧文字的排版结果，过渡结束时丢弃。"""
        inner = self._inner_device(item)
        key: _BoxKey = (text, px, item.title, inner.width, inner.height)
        box = self._fade_boxes.get(item.segment_id)
        if box is None or box.key != key:
            box = self._make_box(key)
            self._fade_boxes[item.segment_id] = box
        return box

    def _make_box(self, key: _BoxKey) -> TextBox:
        text, px, title, width, height = key
        config = self._layout_config
        layout, top = layout_text(
            text, self._font(px, title), width, height, config.horizontal, config.vertical
        )
        return TextBox(key, layout, top)

    def _span_at(self, position: Position, unit: Unit) -> tuple[int, int]:
        """位置所在的选择单位的字符范围：字符是空范围，词按 word_span，段是整段。"""
        segment_id, offset = position
        if unit == "char":
            return (offset, offset)
        text = self.shown_text(self._items[segment_id])
        return word_span(text, offset) if unit == "word" else (0, len(text))

    def _begin_selection(self, unit: Unit, position: QPointF) -> None:
        """开始一次选择：按下处所在的单位（字符、词或段）成为固定的一端。"""
        self._unit = unit
        hit = self.hit_test(position, on_character=unit != "char")
        if hit is None:
            self._unit_span = None
            return
        start, end = self._span_at(hit, unit)
        self._unit_span = (hit[0], start, end)
        self._set_selection((hit[0], start), (hit[0], end))

    def _extend_selection(self, position: QPointF) -> None:
        """拖动中：选择从固定的一端延伸到当前位置所在单位的边界。"""
        if self._unit_span is None:
            return
        hit = self.hit_test(position, on_character=self._unit != "char")
        if hit is None:
            return
        index = {item.segment_id: i for i, item in enumerate(self.visible_items())}
        fixed, fixed_start, fixed_end = self._unit_span
        if fixed not in index or hit[0] not in index:
            return
        start, end = self._span_at(hit, self._unit)
        if (index[hit[0]], start) < (index[fixed], fixed_start):
            self._set_selection((fixed, fixed_end), (hit[0], start))
        else:
            self._set_selection((fixed, fixed_start), (hit[0], end))

    # ---- 字号适配 ----

    def _source_line_px(self, text: str, box: BBox) -> int | None:
        """单行原文在截图里的字号（截图像素）：字形的实际高度不超过包围盒高、整行宽度不超过包围盒宽的最大字号。

        OCR 的行框紧贴字形，没有下伸字母（p、g、y）的行比有的矮四分之一左右；按字形高度而不是行高反推，
        宽度约束也参与估算；空文字、显式多行或最小字号仍放不下时返回 None。段落判定由调用方处理。
        """
        stripped = text.strip()
        if not stripped or "\n" in stripped or box.width <= 0 or box.height <= 0:
            return None

        def fits(px: int) -> bool:
            metrics = QFontMetricsF(self._font(px, False))
            ink = metrics.tightBoundingRect(stripped)
            return ink.height() <= box.height and metrics.horizontalAdvance(stripped) <= box.width * 1.05

        if not fits(MIN_FONT_PX):
            return None
        return fit_font_size(fits, MIN_FONT_PX, max(MIN_FONT_PX, box.height * 2))

    def _expand_line_boxes(self, blocks: Mapping[str, LayoutBlock]) -> None:
        """把单行正文、标题段的包围盒在竖直方向扩到原文字号的行高（OV-03）。

        OCR 的行框只包住字形，按它适配字号时，没有下伸字母的行得到的字号比同字号的其他行小约 30%
        （完整截屏测试实测 17 与 22 设备像素）。扩到行高后两行的范围一致，译文字号随之一致。
        扩展以原框中线为中心，不越过截图边界，也不越过与它水平重叠的相邻段之间的中线，所以相邻段不会互相遮挡。
        表格单元格与图表数据表不扩展：它们的范围由网格线决定。
        """
        height = self._pixel_size[1]
        items = [item for item in self._items.values() if item.chart is None]
        originals = {item.segment_id: item.bbox for item in items}
        for item in items:
            block = blocks.get(item.block_id)
            if block is not None and block.category not in (BlockCategory.TEXT, BlockCategory.TITLE):
                continue
            box = item.bbox
            px = self._source_line_px(item.source, box)
            if px is None:
                continue
            line = QFontMetricsF(self._font(px, item.title)).height()
            grow = (line - box.height) / 2
            if grow <= 0:
                continue
            top, bottom = box.y0 - grow, box.y1 + grow
            for other_id, other in originals.items():
                if other_id == item.segment_id or other.x1 <= box.x0 or box.x1 <= other.x0:
                    continue
                if other.y1 <= box.y0:
                    top = max(top, (other.y1 + box.y0) / 2)
                elif other.y0 >= box.y1:
                    bottom = min(bottom, (box.y1 + other.y0) / 2)
            item.bbox = BBox(box.x0, max(0, round(top)), box.x1, min(height, round(bottom)))

    def _font(self, px: int, title: bool) -> QFont:
        font = QFont(self._base_font)
        font.setPixelSize(max(1, px))
        if title:
            font.setWeight(QFont.Weight.DemiBold)
        return font

    def _target_font(self, item: SegmentItem, scale: float | None = None) -> int:
        ratio = self.scale_device() if scale is None else scale
        return max(MIN_FONT_PX, round(item.source_px * self._layout_config.font_scale * ratio))

    def _reflow_boxes(self) -> None:
        """原始框 → 行高补偿 → 手动扩框 → 按目标字号自动扩框；几何保持截图像素。"""
        config = self._layout_config
        device_scale = self.scale_device()
        items = list(self._items.values())
        for item in items:
            item.bbox = item.source_bbox or item.bbox
        self._expand_line_boxes({block.id: block for block in self._blocks})
        bases = {item.segment_id: item.bbox for item in items}
        originals = {item.segment_id: item.source_bbox or item.bbox for item in items}
        protected = [
            block.bbox
            for block in self._blocks
            if block.category not in (BlockCategory.TEXT, BlockCategory.TITLE)
        ]
        placed: list[BBox] = []
        for item in items:
            source = originals[item.segment_id]
            preserve = item.view is not None and item.view.skip_reason is SkipReason.NO_TEXT
            if item.fixed_box or preserve:
                item.bbox = source
                placed.append(source)
                continue
            base = bases[item.segment_id]
            # 外边距独立于缩放锚点，向四周各添加指定像素。
            manual = sized_box(base, base.width * config.scale_x, base.height * config.scale_y, config)
            manual = BBox(
                math.floor(manual.x0 - config.box_padding_x),
                math.floor(manual.y0 - config.box_padding_y),
                math.ceil(manual.x1 + config.box_padding_x),
                math.ceil(manual.y1 + config.box_padding_y),
            )
            obstacles = (
                [box for sid, box in originals.items() if sid != item.segment_id] + protected + placed
                if config.avoid_overlap
                else []
            )
            target = self._target_font(item)
            minimum_height = max(
                manual.height,
                math.ceil(
                    self._text_extent(self.shown_text(item), 1e7, target, item.title).height() / device_scale
                    + 2 * config.text_padding_y
                ),
            )
            factors = (
                [1.0] if config.growth != "both" else [1 + (config.max_growth - 1) * i / 8 for i in range(9)]
            )
            candidates: list[tuple[int, int, BBox]] = []
            for factor in factors:
                width = max(1, round(manual.width * factor))
                horizontal_box = confine_box(
                    source, sized_box(manual, width, manual.height, config), self._pixel_size, obstacles
                )
                width = horizontal_box.width
                padding_x = min(config.text_padding_x, max(0, (width - 1) / 2))
                bounds = self._text_extent(
                    self.shown_text(item),
                    max(1, math.floor((width - 2 * padding_x) * device_scale)),
                    target,
                    item.title,
                )
                height = manual.height
                if config.growth != "fixed":
                    height = min(
                        math.ceil(manual.height * config.max_growth),
                        max(height, math.ceil(bounds.height() / device_scale + 2 * config.text_padding_y)),
                    )
                anchor = BBox(horizontal_box.x0, manual.y0, horizontal_box.x1, manual.y1)
                candidate = confine_box(
                    source, sized_box(anchor, width, height, config), self._pixel_size, obstacles
                )
                if candidate.width < width and config.growth != "fixed":
                    inner_width = max(1.0, candidate.width - 2 * config.text_padding_x)
                    bounds = self._text_extent(
                        self.shown_text(item),
                        max(1, math.floor(inner_width * device_scale)),
                        target,
                        item.title,
                    )
                    height = min(
                        math.ceil(manual.height * config.max_growth),
                        max(height, math.ceil(bounds.height() / device_scale + 2 * config.text_padding_y)),
                    )
                    anchor = BBox(candidate.x0, manual.y0, candidate.x1, manual.y1)
                    candidate = confine_box(
                        source,
                        sized_box(anchor, candidate.width, height, config),
                        self._pixel_size,
                        obstacles,
                    )
                px = min(config.text_padding_x, max(0, (candidate.width - 1) / 2))
                py = min(config.text_padding_y, max(0, (candidate.height - 1) / 2))
                fitted = self._measure(
                    self.shown_text(item),
                    max(1, math.floor((candidate.width - 2 * px) * device_scale)),
                    max(1, math.floor((candidate.height - 2 * py) * device_scale)),
                    item.title,
                    target,
                )
                candidates.append((fitted, -candidate.width * candidate.height, candidate))
                if fitted >= target and height <= minimum_height:
                    break
            item.bbox = max(candidates, key=lambda value: value[:2])[2]
            placed.append(item.bbox)
        self._boxes.clear()
        self._fade_boxes.clear()
        self.update()

    def _text_extent(self, text: str, width: float, px: int, title: bool) -> QRectF:
        layout, _ = layout_text(text, self._font(px, title), width, 1e7)
        key = (px, title)
        metrics = self._metrics.get(key)
        if metrics is None:
            metrics = self._metrics[key] = QFontMetricsF(self._font(px, title))
        ink = layout_ink_bounds(layout, metrics)
        # 同时覆盖字形外伸和不可拆单词的实际宽度。
        advance = max((layout.lineAt(i).naturalTextWidth() for i in range(layout.lineCount())), default=0.0)
        return QRectF(ink.x(), ink.y(), max(ink.width(), advance), ink.height())

    def _fits(self, text: str, width: float, height: float, title: bool) -> Callable[[int], bool]:
        def fits(px: int) -> bool:
            bounds = self._text_extent(text, width, px, title)
            return bounds.height() <= height and bounds.width() <= width + 0.5

        return fits

    def _inner_device(self, item: SegmentItem) -> RectF:
        """按截图像素配置内边距，换算到当前设备；文字区域至少保留 1×1 设备像素。"""
        rect = bbox_to_view(item.bbox, self.scale_device())
        config = self._layout_config
        px = min(config.text_padding_x * self.scale_device(), max(0, (rect.width - 1) / 2))
        py = min(config.text_padding_y * self.scale_device(), max(0, (rect.height - 1) / 2))
        return RectF(rect.x + px, rect.y + py, max(1.0, rect.width - 2 * px), max(1.0, rect.height - 2 * py))

    def _fit_key(self, item: SegmentItem) -> _FitKey:
        inner = self._inner_device(item)
        outer = bbox_to_view(item.bbox, self.scale_device())
        return (
            self.shown_text(item),
            item.source,
            math.floor(inner.width),
            math.floor(inner.height),
            round(outer.width),
            round(outer.height),
            item.title,
            self._target_font(item),
        )

    def _refit(self, only: Sequence[SegmentId] | None = None) -> None:
        """为段重新确定字号：缓存里有的直接用，没有的先用估算值，排进分帧测量的队列。"""
        targets = self._order if only is None else only
        queued = set(self._fit_queue)
        for sid in targets:
            item = self._items[sid]
            key = self._fit_key(item)
            cached = self._fit_cache.get(key)
            if cached is not None:
                item.font_px = cached
                continue
            item.font_px = None
            if sid not in queued:
                self._fit_queue.append(sid)
                queued.add(sid)
        if self._fit_queue and not self._fit_timer.isActive():
            self._fit_timer.start()

    def fit_all_now(self) -> None:
        """不分帧，立即测完全部排队的段（测试与截图自查用）。"""
        while self._fit_queue:
            self._fit_one(self._fit_queue.pop(0))
        self.update()

    def _fit_some(self) -> None:
        started = time.perf_counter()
        dirty = QRect()
        while self._fit_queue and (time.perf_counter() - started) * 1000 < FIT_BUDGET_MS:
            sid = self._fit_queue.pop(0)
            item = self._fit_one(sid)
            if item is not None:
                dirty = dirty.united(self._item_rect(item))
        if not dirty.isNull():
            self.update(dirty)
        if self._fit_queue:
            self._fit_timer.start()

    def _fit_one(self, sid: SegmentId) -> SegmentItem | None:
        item = self._items.get(sid)
        if item is None:
            return None
        key = self._fit_key(item)
        size = self._fit_cache.get(key)
        if size is None:
            shown, _source, width, height, _outer_width, _outer_height, title, cap = key
            size = self._measure(shown, width, height, title, cap)
            self._fit_cache[key] = size
        item.font_px = size
        return item

    def _measure(self, text: str, width: int, height: int, title: bool, hi: int) -> int:
        """text 放进 width×height（设备像素）、字号不超过 hi 时的最大字号，结果缓存。"""
        key = (text, width, height, title, hi)
        size = self._measures.get(key)
        if size is None:
            size = fit_font_size(self._fits(text, width, height, title), MIN_FONT_PX, hi)
            self._measures[key] = size
        return size

    def font_px(self, item: SegmentItem) -> int:
        """当前设备像素字号；测量前用估算值，最终以原文估算值乘目标倍数适配。"""
        if item.font_px is not None:
            return item.font_px
        inner = self._inner_device(item)
        shown = estimate_font_size(self.shown_text(item), inner.width, inner.height)
        return min(shown, self._target_font(item))

    # ---- 过渡 ----

    def _fade_step(self) -> None:
        """过渡计时器的一帧：重绘过渡中的段，结束的段清掉旧文字；全部结束时停止计时器。"""
        dirty = QRect()
        done: list[SegmentId] = []
        for sid in self._fading:
            item = self._items.get(sid)
            if item is None:
                done.append(sid)
                continue
            dirty = dirty.united(self._item_rect(item))
            if self.fade_progress(sid) >= 1.0:
                done.append(sid)
        for sid in done:
            self._fading.discard(sid)
            self._fade_boxes.pop(sid, None)
            item = self._items.get(sid)
            if item is not None:
                item.fade_from = None
                item.fade_px = None
                item.fade_started = None
        if not self._fading:
            self._fade_timer.stop()
        if not dirty.isNull():
            self.update(dirty)

    def _end_fades(self) -> None:
        """立即结束全部过渡（切换原文与译文时）。"""
        self._fade_timer.stop()
        self._fading.clear()
        self._fade_boxes.clear()
        for item in self._items.values():
            item.fade_from = None
            item.fade_px = None
            item.fade_started = None

    # ---- 绘制 ----

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.setClipRegion(event.region())
            ratio = self.device_ratio()
            painter.scale(1 / ratio, 1 / ratio)  # 之后的坐标都是设备像素
            k = self.scale_device()
            width, height = self._pixel_size
            whole = QRectF(0, 0, width * k, height * k)
            backdrop = QColor(255, 255, 255) if self._light else QColor(0, 0, 0)
            painter.fillRect(QRectF(0, 0, self.width() * ratio + 1, self.height() * ratio + 1), backdrop)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(whole, self._blurred, QRectF(self._blurred.rect()))
            painter.fillRect(whole, self._wash)
            if self._phase is Phase.RESULT:
                self._paint_blocks(painter, k)
                self._paint_items(painter)
                self._paint_progress(painter, ratio)
            if self._status:
                self._paint_status(painter, ratio)
            if self._pinned:
                pen = QPen(theme_colors().accent, 2 * ratio)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(
                    QRectF(ratio, ratio, self.width() * ratio - 2 * ratio, self.height() * ratio - 2 * ratio)
                )
        finally:
            painter.end()

    def _paint_blocks(self, painter: QPainter, k: float) -> None:
        grid = QPen(QColor(self._text_color.red(), self._text_color.green(), self._text_color.blue(), 110))
        grid.setCosmetic(True)
        for block in self._blocks:
            box = block.bbox
            target = bbox_to_view(box, k)
            target_rect = QRectF(target.x, target.y, target.width, target.height)
            if block.category in _IMAGE_LIKE:
                source = QRectF(box.x0, box.y0, box.width, box.height)
                painter.drawImage(target_rect, self._image, source)
                if block.category is BlockCategory.CHART and block.id in self._expanded:
                    painter.fillRect(target_rect, self._panel)
            if block.category is BlockCategory.TABLE or (
                block.category is BlockCategory.CHART and block.id in self._expanded
            ):
                painter.setPen(grid)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                if block.category is BlockCategory.TABLE:
                    for cell in block.cells:
                        cell_rect = bbox_to_view(cell.bbox, k)
                        painter.drawRect(QRectF(cell_rect.x, cell_rect.y, cell_rect.width, cell_rect.height))
                else:
                    for item in self._items.values():
                        if item.chart == block.id:
                            cell_rect = bbox_to_view(item.bbox, k)
                            painter.drawRect(
                                QRectF(cell_rect.x, cell_rect.y, cell_rect.width, cell_rect.height)
                            )

    def _paint_items(self, painter: QPainter) -> None:
        k = self.scale_device()
        accent = theme_colors().accent
        selection = QColor(accent.red(), accent.green(), accent.blue(), 70)
        ranges = self.selection_ranges()
        for item in self.visible_items():
            outer = bbox_to_view(item.bbox, k)
            outer_rect = QRectF(outer.x, outer.y, outer.width, outer.height)
            preserve = item.view is not None and item.view.skip_reason is SkipReason.NO_TEXT
            if preserve:
                source = item.bbox
                painter.drawImage(
                    outer_rect, self._image, QRectF(source.x0, source.y0, source.width, source.height)
                )
            elif item.chart is None:
                painter.fillRect(outer_rect, self._panel)
            inner = self._inner_device(item)
            box = self._text_box(item)
            origin = QPointF(inner.x, inner.y + box.top)
            painter.save()
            painter.setClipRect(outer_rect, Qt.ClipOperation.IntersectClip)
            span = ranges.get(item.segment_id)
            if span is not None:
                for rect in range_rects(box.layout, *span):
                    painter.fillRect(rect.translated(origin), selection)
            painter.setPen(self._text_color)
            fading = item.fade_from is not None and item.segment_id in self._fading
            if not preserve and fading and item.fade_from is not None:
                progress = self.fade_progress(item.segment_id)
                painter.setOpacity(1.0 - progress)
                old_px = item.fade_px if item.fade_px is not None else self.font_px(item)
                old = self._fade_box(item, item.fade_from, old_px)
                old.layout.draw(painter, QPointF(inner.x, inner.y + old.top))
                painter.setOpacity(progress)
            if not preserve:
                box.layout.draw(painter, origin)
            painter.restore()

    def _progress_rect(self) -> QRectF:
        """进度标签的逻辑坐标范围：右下角。"""
        metrics = QFontMetricsF(ui_font(_PROGRESS_PX))
        text = self._progress_text()
        width = metrics.horizontalAdvance(text) + 2 * TOKENS.spacing_s
        height = metrics.height() + 2
        margin = TOKENS.spacing_xs
        return QRectF(self.width() - width - margin, self.height() - height - margin, width, height)

    def _progress_text(self) -> str:
        if self._progress is None:
            return ""
        finished, total = self._progress
        return tr("overlay.progress", finished=finished, total=total)

    def _paint_progress(self, painter: QPainter, ratio: float) -> None:
        if self._progress is None or self._progress[1] == 0:
            return
        rect = self._progress_rect()
        device = QRectF(rect.x() * ratio, rect.y() * ratio, rect.width() * ratio, rect.height() * ratio)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawRoundedRect(device, TOKENS.radius_s * ratio, TOKENS.radius_s * ratio)
        font = ui_font(_PROGRESS_PX)
        font.setPixelSize(max(1, round(_PROGRESS_PX * ratio)))
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(device, Qt.AlignmentFlag.AlignCenter, self._progress_text())

    def _paint_status(self, painter: QPainter, ratio: float) -> None:
        """状态文字（识别中、识别失败、没有识别到文字）居中显示；失败时下面留出“重试”按钮的位置。"""
        width, height = self.width() * ratio, self.height() * ratio
        bottom = height if self._phase is not Phase.FAILED else self._retry.y() * ratio
        text = self._status if not self._detail else f"{self._status}\n{self._detail}"
        area = QRectF(
            TOKENS.spacing_s * ratio, 0, max(1.0, width - 2 * TOKENS.spacing_s * ratio), max(1.0, bottom)
        )
        hi = max(MIN_FONT_PX, round(_STATUS_PX * ratio))
        flags = Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap
        size = fit_font_size(self._fits(text, area.width(), area.height(), False), MIN_FONT_PX, hi)
        painter.setFont(self._font(size, False))
        painter.setPen(self._text_color)
        painter.drawText(area, int(flags), text)

    # ---- 事件 ----

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # 类型桩把 windowHandle 标成非空，窗口还没有原生句柄时它返回 None。
        handle = cast(QWindow | None, self.windowHandle())
        if handle is not None and not self._screen_hooked:
            self._screen_hooked = True
            handle.screenChanged.connect(self._screen_changed)

    def _screen_changed(self, screen: QScreen) -> None:
        del screen
        self.relayout()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            self.relayout()
        return super().event(event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self._was_active = True
            elif self._was_active:
                self.focus_lost()
        super().changeEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        tracking = self._gestures.origin is not None
        # 拖动按全局坐标判断：移动窗口时窗口内的坐标跟着变，全局坐标不变。
        fired = self._gestures.press(event.button(), event.modifiers(), event.globalPosition())
        if not tracking:
            self._press_local = event.position()
            self._press_global = event.globalPosition()
            self._press_origin = self.pos()
            self._unit = "char"
            if self._gestures.drag_action == "overlay.select" and self._triple_click(event):
                self._begin_selection("paragraph", event.position())
        if fired is not None:
            self._trigger(fired[1])
        event.accept()

    def _triple_click(self, event: QMouseEvent) -> bool:
        """这次按下是不是三击：同一个按键，在双击之后的系统双击间隔内，离双击处不超过 DRAG_SLOP。"""
        last, self._last_double = self._last_double, None
        if last is None:
            return False
        at, position, button = last
        if button != event.button():
            return False
        if (time.monotonic() - at) * 1000 > QApplication.doubleClickInterval():
            return False
        moved = event.position() - position
        return math.hypot(moved.x(), moved.y()) <= DRAG_SLOP

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        action = self._keymap.mouse_action(event.button(), event.modifiers(), "DoubleClick")
        if action is None:
            # 没有绑定双击时，第二次按下当作普通的按下，与 QWidget 的缺省行为相同；
            # 这个按键绑定的是选择文字时，选中按下处的词。
            self.mousePressEvent(event)
            if self._gestures.drag_action == "overlay.select" and self._unit == "char":
                self._begin_selection("word", event.position())
                self._last_double = (time.monotonic(), event.position(), event.button())
            return
        self._trigger(action)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        step = self._gestures.move(event.globalPosition())
        if step is not None:
            self._drag(step[0], step[1], event)
        else:
            self._update_cursor(event.position())
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        result = self._gestures.release(event.button())
        if result is not None:
            kind, action = result
            if kind == "end":
                self._drag(kind, action, event)
            elif kind == "click":
                self._trigger(action)
            elif action == "overlay.select" and self._unit == "char":
                # 双击、三击之后的松开保留刚选中的词或段。
                self._click(event.position())
        event.accept()

    def _update_cursor(self, position: QPointF) -> None:
        """鼠标在段上时显示文字光标，提示这里的文字可以选中。"""
        over_text = self._phase is Phase.RESULT and self._item_at(position) is not None
        shape = Qt.CursorShape.IBeamCursor if over_text else Qt.CursorShape.ArrowCursor
        if self.cursor().shape() != shape:
            self.setCursor(shape)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta and self._keymap.wheel_action(event.modifiers()) == "overlay.zoom":
            self.set_zoom(wheel_zoom(self._zoom, delta), event.position())
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        action = self._keymap.key_action(event)
        if action is not None:
            self._trigger(action)
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._fit_timer.stop()
        self._fade_timer.stop()
        event.accept()
        self._actions.window_closed(self)

    # ---- 操作 ----

    def _trigger(self, action: str) -> None:
        """执行单击、双击或按键触发的操作。"""
        match action:
            case "overlay.toggle_source":
                self.toggle_source()
            case "overlay.pin":
                self.toggle_pinned()
            case "overlay.zoom_in":
                self.zoom_step(1)
            case "overlay.zoom_out":
                self.zoom_step(-1)
            case "overlay.zoom_reset":
                self.zoom_reset()
            case "overlay.close":
                self.close()
            case _:
                pass

    def _drag(self, kind: str, action: str, event: QMouseEvent) -> None:
        """拖动操作的一步：拖动窗口跟随全局位移；选择文字从按下处延伸到当前位置。"""
        if action == "overlay.move":
            delta = event.globalPosition() - self._press_global
            self.move(self._press_origin + delta.toPoint())
        elif action == "overlay.select":
            if kind == "start" and self._unit == "char":
                self._begin_selection("char", self._press_local)
            self._extend_selection(event.position())

    def _click(self, position: QPointF) -> None:
        """“选择文字”按键的单击：清除选择；点在图表上时展开或收起它的数据表（OV-03）。"""
        self.clear_selection()
        block = self._block_at(position)
        if (
            block is not None
            and block.category is BlockCategory.CHART
            and any(item.chart == block.id for item in self._items.values())
        ):
            self._expanded.symmetric_difference_update({block.id})
            self._refit()
            self.update()
