"""流程图组件：把一条处理流水线画成从左到右的节点与箭头（需求 UI-05，架构文档 11.7 节）。

组成
====

- ``FlowStep``、``FlowBranch``：流程的结构。``FlowStep`` 是一个节点；``FlowBranch`` 是一处分支，
  上一个节点之后分成几条并列的路（每条路是一串节点），在下一个节点之前汇合。
- ``NodeState``：一个节点的显示内容（图标、名称、一行状态、可选的开关、是否被跳过）。
- ``layout_flow``：纯几何计算，给出每个节点的位置与全部连线，不依赖 QApplication，可以单独测试。
- ``FlowChart``：画出节点与连线的控件。节点是 ``FlowNode`` 子控件，连线由流程图自己在节点下面绘制。
- ``FlowView``：流程图加下方的设置面板区。点开一个节点，面板区只显示这个节点的面板（同一时间只展开一个）；
  面板就是页面已有的设置组件，流程图只负责显示与隐藏。

排版
====

节点从左到右排成一行，放不下时换行。换行处的连线从上一行最后一个节点的右侧引出，向下走到两行之间，
折回左侧留白，再向下接到下一行第一个节点的左侧，箭头指向它。分支整体作为一个块参与换行：
入口处分叉，每条路的第一个节点前有箭头，各条路的末端在出口处汇合，汇合点之后的连线带箭头指向下一个节点。

被跳过的节点（``NodeState.bypassed``，例如开关关闭的环节）整体变淡、边框改为虚线，进出它的连线也变淡；
它上方画一条强调色的虚线，从它前面的连线上引出、越过它、落回它后面的连线上，表示数据直接流向下一个节点。
同一行里相邻的几个被跳过的节点共用一条绕行虚线。

键盘与辅助功能
==============

流程图在 Tab 顺序里只占一个位置（当前节点）：Tab 进入流程图时落在当前节点上，左右（上下）方向键在节点之间
移动，Home、End 到首尾；Enter 打开当前节点的设置；空格切换节点上的开关，没有开关的节点按空格同样打开设置。
每个节点的可访问名称是节点名，可访问描述写出状态、开关状态、第几步与操作方法。
"""

import itertools
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, QSizeF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QEnterEvent,
    QFocusEvent,
    QFontMetricsF,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPolygonF,
    QResizeEvent,
)
from PySide6.QtWidgets import QPushButton, QSizePolicy, QVBoxLayout, QWidget

from lst.ui.i18n import tr
from lst.ui.settings.controls import SettingCard
from lst.ui.theme import TOKENS, fluent_icon_font, theme_colors, ui_font

# ---------------------------------------------------------------- 结构


@dataclass(frozen=True, slots=True)
class FlowStep:
    """流程里的一个节点。key 在一个流程图里唯一。"""

    key: str


@dataclass(frozen=True, slots=True)
class FlowBranch:
    """一处分支：几条并列的路，每条路是从左到右的一串节点。路在入口分叉，在出口汇合。"""

    lanes: tuple[tuple[str, ...], ...]


type FlowItem = FlowStep | FlowBranch


def flow_keys(items: Sequence[FlowItem]) -> list[str]:
    """流程里全部节点的 key，按阅读顺序：分支里先第一条路，再第二条路。"""
    keys: list[str] = []
    for item in items:
        match item:
            case FlowStep(key):
                keys.append(key)
            case FlowBranch(lanes):
                for lane in lanes:
                    keys.extend(lane)
    return keys


@dataclass(frozen=True, slots=True)
class NodeState:
    """一个节点的显示内容。

    switch 为 None 时节点上没有开关，否则是开关的当前值；bypassed 为真时节点变淡并画绕行虚线。
    """

    title: str
    status: str = ""
    glyph: str = ""
    switch: bool | None = None
    bypassed: bool = False


# ---------------------------------------------------------------- 几何

NODE_MARGIN: Final = 3
"""节点控件比卡片矩形四周各大出的像素，用来画焦点框。"""
GAP: Final = 36
"""同一行相邻两个块之间的水平距离（箭头所在的空白）。"""
LANE_GAP_X: Final = 28
"""分支里同一条路上相邻节点之间的水平距离。"""
LANE_GAP_Y: Final = 14
"""分支里相邻两条路之间的垂直距离。"""
BRANCH_PAD: Final = 30
"""分支块入口到第一列节点、最后一列节点到出口的水平距离；分叉与汇合的竖线在它的中点。"""
GUTTER: Final = 24
"""左右两侧的留白。换行连线在左侧留白里折返。"""
ROW_GAP: Final = 46
"""两行之间的距离：换行连线从中间穿过，下一行的绕行虚线画在它的下半部分。"""
TOP: Final = 20
"""第一行上方的空白，给绕行虚线留位置。"""
BOTTOM: Final = 6
HUMP: Final = 13
"""绕行虚线高出节点顶边的距离。"""
BYPASS_INSET: Final = 13
"""绕行虚线的起落点与被跳过节点左右边缘的水平距离。"""
WRAP_OUT: Final = 13
"""换行连线从行末节点右侧伸出的长度。"""
ARROW_LENGTH: Final = 7.0
ARROW_WIDTH: Final = 6.0
CORNER: Final = 6.0
"""折线拐角的圆角半径。"""


class ConnectorKind(StrEnum):
    FLOW = "flow"
    """数据流动的连线，实线。"""
    BYPASS = "bypass"
    """绕过被跳过节点的虚线。"""


@dataclass(frozen=True, slots=True)
class Connector:
    """一条连线：折线的顶点、种类、终点是否画箭头、是否变淡（连着被跳过的节点）。

    source、target 是连线两端的节点 key；分支的分叉、汇合线一端是分支本身，记为空串。
    """

    points: tuple[QPointF, ...]
    kind: ConnectorKind
    arrow: bool
    faded: bool = False
    source: str = ""
    target: str = ""


@dataclass(frozen=True, slots=True)
class FlowLayout:
    """一次排版的结果：节点的卡片矩形、每行的节点、全部连线与总高度。"""

    nodes: dict[str, QRectF]
    rows: tuple[tuple[str, ...], ...]
    connectors: tuple[Connector, ...]
    height: float
    width: float


@dataclass(slots=True)
class _Block:
    item: FlowItem
    width: float
    height: float
    x: float = 0.0
    row: int = 0
    nodes: dict[str, QRectF] = field(default_factory=dict[str, QRectF])

    @property
    def keys(self) -> list[str]:
        return flow_keys([self.item])


def _block_size(item: FlowItem, node: QSizeF) -> tuple[float, float]:
    match item:
        case FlowStep():
            return node.width(), node.height()
        case FlowBranch(lanes):
            longest = max((len(lane) for lane in lanes), default=1)
            lane_width = longest * node.width() + (longest - 1) * LANE_GAP_X
            count = max(len(lanes), 1)
            return 2 * BRANCH_PAD + lane_width, count * node.height() + (count - 1) * LANE_GAP_Y


def minimum_width(items: Sequence[FlowItem], node: QSizeF) -> float:
    """不让任何块越出边界所需的最小宽度。"""
    widest = max((_block_size(item, node)[0] for item in items), default=node.width())
    return widest + 2 * GUTTER


def layout_flow(
    items: Sequence[FlowItem], *, width: float, node: QSizeF, bypassed: frozenset[str] = frozenset()
) -> FlowLayout:
    """排版：块按顺序从左到右放，放不下另起一行；然后生成连线，规则见模块说明。"""
    blocks = [_Block(item, *_block_size(item, node)) for item in items]
    rows: list[list[_Block]] = []
    right = max(width, minimum_width(items, node)) - GUTTER
    x = GUTTER
    for block in blocks:
        if rows and rows[-1] and x + block.width > right:
            rows.append([])
            x = GUTTER
        if not rows:
            rows.append([])
        block.x, block.row = x, len(rows) - 1
        rows[-1].append(block)
        x += block.width + GAP

    row_tops: list[float] = []
    row_heights: list[float] = []
    y = TOP
    for row in rows:
        height = max(block.height for block in row)
        row_tops.append(y)
        row_heights.append(height)
        y += height + ROW_GAP
    total = (y - ROW_GAP + BOTTOM) if rows else TOP + BOTTOM

    nodes: dict[str, QRectF] = {}
    for block in blocks:
        top = row_tops[block.row] + (row_heights[block.row] - block.height) / 2
        match block.item:
            case FlowStep(key):
                block.nodes[key] = QRectF(block.x, top, node.width(), node.height())
            case FlowBranch(lanes):
                for index, lane in enumerate(lanes):
                    lane_top = top + index * (node.height() + LANE_GAP_Y)
                    for column, key in enumerate(lane):
                        left = block.x + BRANCH_PAD + column * (node.width() + LANE_GAP_X)
                        block.nodes[key] = QRectF(left, lane_top, node.width(), node.height())
        nodes.update(block.nodes)

    def center_y(block: _Block) -> float:
        return row_tops[block.row] + row_heights[block.row] / 2

    def skipped(block: _Block) -> bool:
        return isinstance(block.item, FlowStep) and block.item.key in bypassed

    def entry_key(block: _Block) -> str:
        return block.item.key if isinstance(block.item, FlowStep) else ""

    connectors: list[Connector] = []
    for block in blocks:
        if isinstance(block.item, FlowBranch):
            connectors.extend(_branch_connectors(block, center_y(block)))
    for first, second in itertools.pairwise(blocks):
        y1, y2 = center_y(first), center_y(second)
        start = QPointF(first.x + first.width, y1)
        end = QPointF(second.x, y2)
        if first.row == second.row:
            points = (start, end)
        else:
            out = start.x() + WRAP_OUT
            middle = row_tops[first.row] + row_heights[first.row] + ROW_GAP / 2
            back = GUTTER / 2
            points = (
                start,
                QPointF(out, y1),
                QPointF(out, middle),
                QPointF(back, middle),
                QPointF(back, y2),
                end,
            )
        connectors.append(
            Connector(
                points,
                ConnectorKind.FLOW,
                arrow=isinstance(second.item, FlowStep),
                faded=skipped(first) or skipped(second),
                source=first.keys[-1] if isinstance(first.item, FlowStep) else "",
                target=entry_key(second),
            )
        )
    connectors.extend(_bypass_connectors(rows, nodes, center_y, bypassed))
    return FlowLayout(
        nodes=nodes,
        rows=tuple(tuple(key for block in row for key in block.keys) for row in rows),
        connectors=tuple(connectors),
        height=total,
        width=max(width, minimum_width(items, node)),
    )


def _branch_connectors(block: _Block, middle: float) -> Iterator[Connector]:
    """分支内部的连线：入口分叉到每条路，路上相邻节点之间，每条路的末端汇合到出口。"""
    assert isinstance(block.item, FlowBranch)
    fork = block.x + BRANCH_PAD / 2
    join = block.x + block.width - BRANCH_PAD / 2
    exit_x = block.x + block.width
    for lane in block.item.lanes:
        if not lane:
            continue
        rects = [block.nodes[key] for key in lane]
        lane_y = rects[0].center().y()
        yield Connector(
            (
                QPointF(block.x, middle),
                QPointF(fork, middle),
                QPointF(fork, lane_y),
                QPointF(rects[0].left(), lane_y),
            ),
            ConnectorKind.FLOW,
            arrow=True,
            target=lane[0],
        )
        for (key1, rect1), (key2, rect2) in zip(
            zip(lane, rects, strict=True), zip(lane[1:], rects[1:], strict=True), strict=False
        ):
            yield Connector(
                (QPointF(rect1.right(), lane_y), QPointF(rect2.left(), lane_y)),
                ConnectorKind.FLOW,
                arrow=True,
                source=key1,
                target=key2,
            )
        yield Connector(
            (
                QPointF(rects[-1].right(), lane_y),
                QPointF(join, lane_y),
                QPointF(join, middle),
                QPointF(exit_x, middle),
            ),
            ConnectorKind.FLOW,
            arrow=False,
            source=lane[-1],
        )


def _bypass_connectors(
    rows: list[list[_Block]],
    nodes: Mapping[str, QRectF],
    center_y: Callable[[_Block], float],
    bypassed: frozenset[str],
) -> Iterator[Connector]:
    """每一行里相邻的被跳过节点合成一段，画一条从它们上方越过的虚线。"""
    for row in rows:
        run: list[_Block] = []
        for block in [*row, None]:
            if block is not None and isinstance(block.item, FlowStep) and block.item.key in bypassed:
                run.append(block)
                continue
            if run:
                first, last = nodes[run[0].keys[0]], nodes[run[-1].keys[0]]
                y = center_y(run[0])
                top = first.top() - HUMP
                left = max(first.left() - BYPASS_INSET, GUTTER / 2 + 4)
                right = last.right() + BYPASS_INSET
                yield Connector(
                    (QPointF(left, y), QPointF(left, top), QPointF(right, top), QPointF(right, y)),
                    ConnectorKind.BYPASS,
                    arrow=True,
                    source=run[0].keys[0],
                    target=run[-1].keys[0],
                )
                run = []


# ---------------------------------------------------------------- 绘制工具


def rounded_path(points: Sequence[QPointF], radius: float = CORNER) -> QPainterPath:
    """经过各顶点的折线，拐角处改成圆角。"""
    path = QPainterPath(points[0])
    for index in range(1, len(points) - 1):
        before, corner, after = points[index - 1], points[index], points[index + 1]
        into = _toward(corner, before, radius)
        out = _toward(corner, after, radius)
        path.lineTo(into)
        path.quadTo(corner, out)
    path.lineTo(points[-1])
    return path


def _toward(origin: QPointF, target: QPointF, distance: float) -> QPointF:
    """从 origin 向 target 走 distance（不超过两点距离的一半）。"""
    dx, dy = target.x() - origin.x(), target.y() - origin.y()
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return QPointF(origin)
    step = min(distance, length / 2) / length
    return QPointF(origin.x() + dx * step, origin.y() + dy * step)


def arrow_head(tip: QPointF, previous: QPointF) -> QPolygonF:
    """终点 tip 处的实心三角箭头，方向由 previous → tip 决定。"""
    dx, dy = tip.x() - previous.x(), tip.y() - previous.y()
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / length, dy / length
    base = QPointF(tip.x() - ux * ARROW_LENGTH, tip.y() - uy * ARROW_LENGTH)
    half = ARROW_WIDTH / 2
    return QPolygonF(
        [
            tip,
            QPointF(base.x() - uy * half, base.y() + ux * half),
            QPointF(base.x() + uy * half, base.y() - ux * half),
        ]
    )


def _with_alpha(color: QColor, alpha: float) -> QColor:
    result = QColor(color)
    result.setAlphaF(alpha)
    return result


# ---------------------------------------------------------------- 节点

_PAD: Final = 10
_ICON_PX: Final = 20
_TITLE_PX: Final = 13
_STATUS_PX: Final = 12
_SWITCH_W: Final = 28.0
_SWITCH_H: Final = 14.0
NODE_HEIGHT: Final = 58
MIN_NODE_WIDTH: Final = 136
MAX_NODE_WIDTH: Final = 176


class FlowNode(QWidget):
    """流程图里的一个节点：圆角卡片，左侧图标，右侧名称与一行状态；有开关时开关在名称行的右端。

    鼠标点开关切换开关，点其他位置打开设置；键盘操作见模块说明。节点自己不保存选中状态以外的数据，
    事件都交给所在的 ``FlowChart`` 处理。
    """

    def __init__(self, chart: "FlowChart", key: str) -> None:
        super().__init__(chart)
        self.key = key
        self.state = NodeState(key)
        self.selected = False
        self._chart = chart
        self._hover = False
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # ---- 几何

    @property
    def card_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(
            NODE_MARGIN + 0.5, NODE_MARGIN + 0.5, -NODE_MARGIN - 0.5, -NODE_MARGIN - 0.5
        )

    @property
    def switch_rect(self) -> QRectF | None:
        """开关的位置（节点坐标）；没有开关时为 None。"""
        if self.state.switch is None:
            return None
        card = self.card_rect
        return QRectF(card.right() - _PAD - _SWITCH_W, card.top() + _PAD + 2, _SWITCH_W, _SWITCH_H)

    # ---- 状态

    def set_state(self, state: NodeState) -> None:
        self.state = state
        self.setToolTip(state.status)
        self.update()

    def set_selected(self, selected: bool) -> None:
        if selected != self.selected:
            self.selected = selected
            self.update()

    # ---- 事件

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._chart.focus_node(self.key)
        switch = self.switch_rect
        if switch is not None and switch.adjusted(-4, -4, 4, 4).contains(event.position()):
            self._chart.toggle(self.key)
        else:
            self._chart.activate(self.key)
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._chart.handle_key(self.key, event):
            super().keyPressEvent(event)

    def focusInEvent(self, event: QFocusEvent) -> None:
        self._chart.focus_node(self.key, move=False)
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self.update()
        super().focusOutEvent(event)

    def enterEvent(self, event: QEnterEvent) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    # ---- 绘制

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        colors = theme_colors()
        card = self.card_rect
        state = self.state
        radius = TOKENS.card_radius
        fill = QColor(colors.card_hover if self._hover else colors.card)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(card, radius, radius)
        if self.selected:
            painter.setBrush(_with_alpha(colors.accent, 0.16 if colors.dark else 0.10))
            painter.drawRoundedRect(card, radius, radius)
        border = QPen(colors.accent if self.selected else colors.card_border)
        border.setWidthF(1.5 if self.selected else 1.0)
        if state.bypassed and not self.selected:
            border.setColor(_with_alpha(colors.text, 0.35))
            border.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(border)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(card, radius, radius)
        if self.hasFocus():
            ring = QPen(colors.text)
            ring.setWidthF(1.5)
            painter.setPen(ring)
            outer = card.adjusted(-2.5, -2.5, 2.5, 2.5)
            painter.drawRoundedRect(outer, radius + 2, radius + 2)

        painter.save()
        if state.bypassed:
            painter.setOpacity(0.45)
        icon_rect = QRectF(card.left() + _PAD, card.top(), _ICON_PX, card.height())
        painter.setFont(fluent_icon_font(_ICON_PX))
        painter.setPen(colors.accent_strong if self.selected else colors.text)
        painter.drawText(icon_rect, int(Qt.AlignmentFlag.AlignCenter), state.glyph)
        text_left = icon_rect.right() + _PAD
        switch = self.switch_rect
        title_right = (switch.left() - 6) if switch is not None else card.right() - _PAD
        title_font = ui_font(_TITLE_PX, semibold=True)
        status_font = ui_font(_STATUS_PX)
        title_height = QFontMetricsF(title_font).height()
        status_height = QFontMetricsF(status_font).height()
        block = title_height + 2 + status_height
        top = card.top() + (card.height() - block) / 2
        title_rect = QRectF(text_left, top, max(0.0, title_right - text_left), title_height)
        status_rect = QRectF(
            text_left, top + title_height + 2, card.right() - _PAD - text_left, status_height
        )
        painter.setFont(title_font)
        painter.setPen(colors.text)
        title = QFontMetricsF(title_font).elidedText(
            state.title, Qt.TextElideMode.ElideRight, title_rect.width()
        )
        painter.drawText(title_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)
        painter.setFont(status_font)
        painter.setPen(colors.text_secondary)
        status = QFontMetricsF(status_font).elidedText(
            state.status, Qt.TextElideMode.ElideRight, status_rect.width()
        )
        painter.drawText(status_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), status)
        painter.restore()
        if switch is not None:
            self._paint_switch(painter, switch, bool(state.switch))

    @staticmethod
    def _paint_switch(painter: QPainter, rect: QRectF, on: bool) -> None:
        colors = theme_colors()
        radius = rect.height() / 2
        knob = 8.0
        if on:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colors.accent)
            painter.drawRoundedRect(rect, radius, radius)
            painter.setBrush(colors.accent_text)
            center = QPointF(rect.right() - radius, rect.center().y())
        else:
            painter.setPen(_with_alpha(colors.text, 0.6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius - 0.5, radius - 0.5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_with_alpha(colors.text, 0.75))
            center = QPointF(rect.left() + radius, rect.center().y())
        painter.drawEllipse(center, knob / 2, knob / 2)


# ---------------------------------------------------------------- 流程图


class FlowChart(QWidget):
    """节点与连线。keys 是这个流程图可能出现的全部节点，构造时一次建好（焦点链里的位置因此固定），
    ``set_items`` 决定当前显示哪些、怎样连接。

    信号：activated(key) 请求打开节点的设置；toggled(key, 新值) 请求切换节点上的开关。
    流程图不修改开关的值，由页面写配置，配置变化后再用 ``set_state`` 刷新。
    """

    activated = Signal(str)
    toggled = Signal(str, bool)

    def __init__(self, keys: Sequence[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.nodes: dict[str, FlowNode] = {key: FlowNode(self, key) for key in keys}
        self.items: tuple[FlowItem, ...] = ()
        self.order: list[str] = []
        self.selected: str | None = None
        self.current: str | None = None
        self.layout_result = FlowLayout({}, (), (), TOP + BOTTOM, 0.0)
        self._node_size = QSizeF(MIN_NODE_WIDTH, NODE_HEIGHT)
        for node in self.nodes.values():
            node.hide()
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    # ---- 内容

    def set_items(self, items: Sequence[FlowItem]) -> None:
        """显示这些节点。items 里的 key 必须在构造时给出过。"""
        unknown = [key for key in flow_keys(items) if key not in self.nodes]
        if unknown:
            raise KeyError(f"流程图没有这些节点：{', '.join(unknown)}")
        self.items = tuple(items)
        self.order = flow_keys(items)
        shown = set(self.order)
        for key, node in self.nodes.items():
            node.setVisible(key in shown)
        if self.current not in shown:
            self.current = (
                self.selected if self.selected in shown else (self.order[0] if self.order else None)
            )
        self._update_focus_policy()
        self._relayout()

    def set_state(self, key: str, state: NodeState) -> None:
        node = self.nodes[key]
        changed_size = (state.title, state.status, state.switch is None) != (
            node.state.title,
            node.state.status,
            node.state.switch is None,
        )
        bypass_changed = state.bypassed != node.state.bypassed
        node.set_state(state)
        self._describe(key)
        if changed_size or bypass_changed:
            self._relayout()

    def state(self, key: str) -> NodeState:
        return self.nodes[key].state

    def set_selected(self, key: str | None) -> None:
        self.selected = key
        for node_key, node in self.nodes.items():
            node.set_selected(node_key == key)
        if key is not None and key in self.order:
            self.current = key
            self._update_focus_policy()
        for node_key in self.order:
            self._describe(node_key)

    # ---- 节点的操作（节点控件调用）

    def activate(self, key: str) -> None:
        self.activated.emit(key)

    def toggle(self, key: str) -> None:
        value = self.nodes[key].state.switch
        if value is not None:
            self.toggled.emit(key, not value)

    def focus_node(self, key: str, *, move: bool = True) -> None:
        """把当前节点（Tab 进入流程图时落到的节点）设为 key；move 为真时同时把键盘焦点移过去。"""
        if key not in self.order:
            return
        self.current = key
        self._update_focus_policy()
        if move:
            self.nodes[key].setFocus(Qt.FocusReason.OtherFocusReason)

    def handle_key(self, key: str, event: QKeyEvent) -> bool:
        """节点上的按键。返回是否已处理。"""
        index = self.order.index(key) if key in self.order else -1
        match event.key():
            case Qt.Key.Key_Right | Qt.Key.Key_Down:
                target = min(index + 1, len(self.order) - 1)
            case Qt.Key.Key_Left | Qt.Key.Key_Up:
                target = max(index - 1, 0)
            case Qt.Key.Key_Home:
                target = 0
            case Qt.Key.Key_End:
                target = len(self.order) - 1
            case Qt.Key.Key_Return | Qt.Key.Key_Enter:
                self.activate(key)
                return True
            case Qt.Key.Key_Space:
                if self.nodes[key].state.switch is not None:
                    self.toggle(key)
                else:
                    self.activate(key)
                return True
            case _:
                return False
        if 0 <= target < len(self.order):
            self.focus_node(self.order[target])
        return True

    def _update_focus_policy(self) -> None:
        """只有当前节点接受 Tab 焦点，流程图在 Tab 顺序里只占一个位置。"""
        for key, node in self.nodes.items():
            policy = Qt.FocusPolicy.StrongFocus if key == self.current else Qt.FocusPolicy.ClickFocus
            node.setFocusPolicy(policy)

    def _describe(self, key: str) -> None:
        node = self.nodes[key]
        state = node.state
        parts: list[str] = []
        if state.status:
            parts.append(state.status)
        if state.switch is not None:
            parts.append(tr("settings.flow.switch_on" if state.switch else "settings.flow.switch_off"))
        if state.bypassed:
            parts.append(tr("settings.flow.bypassed"))
        if key in self.order:
            parts.append(tr("settings.flow.position", index=self.order.index(key) + 1, count=len(self.order)))
        if key == self.selected:
            parts.append(tr("settings.flow.opened"))
        parts.append(tr("settings.flow.keys_switch" if state.switch is not None else "settings.flow.keys"))
        node.setAccessibleName(state.title)
        node.setAccessibleDescription(tr("settings.flow.separator").join(parts))

    # ---- 排版

    def node_size(self) -> QSizeF:
        """全部节点共用的卡片大小：按最长的名称与状态算宽度，限制在上下限之间。"""
        title_metrics = QFontMetricsF(ui_font(_TITLE_PX, semibold=True))
        status_metrics = QFontMetricsF(ui_font(_STATUS_PX))
        needed = 0.0
        for key in self.order:
            state = self.nodes[key].state
            title = title_metrics.horizontalAdvance(state.title)
            if state.switch is not None:
                title += _SWITCH_W + 6
            needed = max(needed, title, status_metrics.horizontalAdvance(state.status))
        width = _PAD + _ICON_PX + _PAD + needed + _PAD + 2
        return QSizeF(min(max(width, MIN_NODE_WIDTH), MAX_NODE_WIDTH), NODE_HEIGHT)

    def compute(self, width: float) -> FlowLayout:
        bypassed = frozenset(key for key in self.order if self.nodes[key].state.bypassed)
        return layout_flow(self.items, width=width, node=self._node_size, bypassed=bypassed)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return int(self.compute(width).height + 0.5)

    def sizeHint(self) -> QSize:
        width = max(self.width(), int(minimum_width(self.items, self._node_size)))
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:
        width = int(minimum_width(self.items, self._node_size) + 0.5)
        return QSize(width, self.heightForWidth(width))

    def _relayout(self) -> None:
        self._node_size = self.node_size()
        self.layout_result = self.compute(self.width())
        for key, rect in self.layout_result.nodes.items():
            outer = rect.adjusted(-NODE_MARGIN, -NODE_MARGIN, NODE_MARGIN, NODE_MARGIN)
            self.nodes[key].setGeometry(outer.toAlignedRect())
        self.updateGeometry()
        self.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._relayout()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self.update()
        super().changeEvent(event)

    # ---- 连线

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        colors = theme_colors()
        line = _with_alpha(colors.text, 0.5 if colors.dark else 0.45)
        faded = _with_alpha(colors.text, 0.2 if colors.dark else 0.16)
        for connector in self.layout_result.connectors:
            bypass = connector.kind is ConnectorKind.BYPASS
            color = colors.accent_strong if bypass else (faded if connector.faded else line)
            pen = QPen(color)
            pen.setWidthF(1.5)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            if bypass:
                pen.setDashPattern([3.0, 2.5])
            points = list(connector.points)
            if connector.arrow and len(points) >= 2:
                head = arrow_head(points[-1], points[-2])
                points[-1] = _toward(points[-1], points[-2], ARROW_LENGTH - 1)
            else:
                head = None
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(rounded_path(points))
            if head is not None:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawPolygon(head)


# ---------------------------------------------------------------- 说明卡片


class NodeNote(SettingCard):
    """面板里的说明卡片：节点的图标、名称与说明；button 不为 None 时放在右侧（例如“转到某页”）。

    没有可调设置的节点，面板里只有这一张卡片；有设置的节点把它放在设置组件之前或之后。
    """

    def __init__(self, glyph: str, button: QPushButton | None = None) -> None:
        super().__init__(button, icon=glyph)
        self.button = button


# ---------------------------------------------------------------- 流程图加设置面板


class FlowView(QWidget):
    """流程图与下方的设置面板。每个节点最多一个面板，同一时间只显示被打开的节点的面板。

    面板控件由页面创建后用 ``add_panel`` 交进来；页面先建 ``FlowView``、再建面板，焦点链里节点在面板之前，
    Tab 从流程图进入面板。
    """

    opened = Signal(str)

    def __init__(self, keys: Sequence[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.chart = FlowChart(keys)
        self.panels: dict[str, QWidget] = {}
        self._panel_box = QVBoxLayout()
        self._panel_box.setContentsMargins(0, 0, 0, 0)
        self._panel_box.setSpacing(TOKENS.card_gap)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(TOKENS.spacing_s)
        layout.addWidget(self.chart)
        layout.addLayout(self._panel_box)
        self.chart.activated.connect(self.open)

    @property
    def selected(self) -> str | None:
        return self.chart.selected

    def add_panel(self, key: str, panel: QWidget) -> None:
        if key not in self.chart.nodes:
            raise KeyError(f"流程图没有节点 {key}")
        self.panels[key] = panel
        self._panel_box.addWidget(panel)
        panel.setVisible(key == self.chart.selected)

    def set_items(self, items: Sequence[FlowItem], *, fallback: str | None = None) -> None:
        """换一组节点。打开的节点不在新的流程里时，改为打开 fallback（不在流程里时打开第一个节点）。"""
        self.chart.set_items(items)
        order = self.chart.order
        if self.chart.selected not in order:
            target = fallback if fallback in order else (order[0] if order else None)
            if target is not None:
                self.open(target)

    def open(self, key: str) -> None:
        """打开节点：高亮它，显示它的面板，隐藏其他面板。"""
        if key not in self.chart.order:
            return
        changed = key != self.chart.selected
        self.chart.set_selected(key)
        for panel_key, panel in self.panels.items():
            panel.setVisible(panel_key == key)
        if changed:
            self.opened.emit(key)


__all__ = [
    "Connector",
    "ConnectorKind",
    "FlowBranch",
    "FlowChart",
    "FlowItem",
    "FlowLayout",
    "FlowNode",
    "FlowStep",
    "FlowView",
    "NodeNote",
    "NodeState",
    "arrow_head",
    "flow_keys",
    "layout_flow",
    "minimum_width",
    "rounded_path",
]
