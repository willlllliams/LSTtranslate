"""双语窗口用到的两个控件：译文区与引擎结果列表。只负责绘制和收集输入。"""

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from functools import partial

from PySide6.QtCore import QEasingCurve, QEvent, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPaintEvent,
    QPalette,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from lst.contracts.ids import EngineId
from lst.ui.bilingual.presenter import EngineRow, SegmentItem
from lst.ui.i18n import tr
from lst.ui.theme import TOKENS

# 来源标记条相对段落文字区域左边缘的位置与宽度，以及给标记条让出的文字缩进（逻辑像素）。
MARKER_OFFSET = 1
MARKER_WIDTH = 3
TEXT_INDENT = 10

# 引擎结果列表每行最多显示的译文字数。自动换行的 QLabel 排版耗时随字数增长，100 段的长文能让一次刷新
# 超过 16 毫秒（需求 REL-04）；超出部分用省略号表示，复制按钮仍然复制该实例的全部译文。
ROW_PREVIEW_CHARS = 300

# 段内换行用 Unicode 行分隔符（U+2028）表示，一段因此恰好对应文档里的一个文本块，序号可以直接换算。
_LINE_SEPARATOR = chr(0x2028)
_PARAGRAPH_SEPARATOR = chr(0x2029)


def _one_block(text: str) -> str:
    """把一段文字里的各种换行统一成行分隔符。"""
    unified = text.replace("\r\n", "\n").replace("\r", "\n").replace(_PARAGRAPH_SEPARATOR, "\n")
    return unified.replace("\n", _LINE_SEPARATOR)


class TranslationPane(QTextEdit):
    """译文区。每段一个文本块，段旁可画来源标记，段的译文替换时播放淡入（需求 RACE-06）。

    更新只改动给出的文本块，并且整批更新放在一个编辑块里，文档只重新排版一次（需求 REL-04）。
    淡入用文字颜色的透明度实现：同一批开始淡入的段共用一个 QVariantAnimation，每一帧也只排版一次。
    一段再次淡入时离开原来那一组，动画结束后恢复为默认文字格式。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setUndoRedoEnabled(False)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._block_format = QTextBlockFormat()
        self._block_format.setLeftMargin(TEXT_INDENT)
        self._block_format.setBottomMargin(TOKENS.spacing_s)
        self._colors: list[QColor | None] = []
        self._markers_visible = True
        self._fade_of: dict[int, QVariantAnimation] = {}
        self._fade_members: dict[QVariantAnimation, set[int]] = {}

    # ------------------------------------------------------------------ 查询

    def segment_count(self) -> int:
        return len(self._colors)

    def segment_text(self, index: int) -> str:
        return self.document().findBlockByNumber(index).text().replace(_LINE_SEPARATOR, "\n")

    def markers_visible(self) -> bool:
        return self._markers_visible

    def marker_color(self, index: int) -> QColor | None:
        """第 index 段旁实际画出的标记颜色；标记关闭或该段没有胜出者时为 None。"""
        if not self._markers_visible:
            return None
        return self._colors[index]

    def fade_animation(self, index: int) -> QVariantAnimation | None:
        """第 index 段正在播放的淡入动画。"""
        return self._fade_of.get(index)

    # ------------------------------------------------------------------ 更新

    def set_markers_visible(self, visible: bool) -> None:
        if visible != self._markers_visible:
            self._markers_visible = visible
            self.viewport().update()

    def set_segments(self, items: Sequence[SegmentItem], fade: Collection[int]) -> None:
        """整体替换全部段。"""
        self._stop_fades()
        self._colors = [item.color for item in items]
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        # 在原文档上整体替换，不用 setPlainText：后者会把滚动条拉回顶部，用户边输入边看长文的末尾时，
        # 每次停顿都会跳回开头。没有段时文档里留一个空文本块。
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText("\n".join(_one_block(item.text) for item in items), QTextCharFormat())
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.setBlockFormat(self._block_format)
        transparent = self._alpha_format(0.0)
        for index in fade:
            self._select_block(cursor, index)
            cursor.mergeCharFormat(transparent)
        cursor.endEditBlock()
        self._start_fade(fade)
        self.viewport().update()

    def update_segments(self, changes: Sequence[tuple[int, SegmentItem]], fade: Collection[int]) -> None:
        """只替换给出的段；序号等于当前段数时追加。fade 中的段从透明开始淡入。"""
        fading = set(fade)
        transparent = self._alpha_format(0.0)
        opaque = QTextCharFormat()
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        for index, item in changes:
            if index in fading:
                char_format = transparent
            else:
                self._leave_fade(index)
                char_format = opaque
            if index < len(self._colors):
                self._select_block(cursor, index)
                self._colors[index] = item.color
            elif index == len(self._colors):
                if self._colors:
                    cursor.movePosition(QTextCursor.MoveOperation.End)
                    cursor.insertBlock(self._block_format, QTextCharFormat())
                else:
                    # 空文档里只有一个空文本块，第一段直接写进去。
                    cursor.select(QTextCursor.SelectionType.Document)
                    cursor.setBlockFormat(self._block_format)
                self._colors.append(item.color)
            else:
                raise IndexError(f"段序号 {index} 超出当前段数 {len(self._colors)}")
            cursor.insertText(_one_block(item.text), char_format)
        cursor.endEditBlock()
        self._start_fade(fading)
        self.viewport().update()

    def set_colors(self, colors: Mapping[int, QColor | None]) -> None:
        for index, color in colors.items():
            if 0 <= index < len(self._colors):
                self._colors[index] = color
        self.viewport().update()

    # ------------------------------------------------------------------ 淡入

    def _select_block(self, cursor: QTextCursor, index: int) -> None:
        block = self.document().findBlockByNumber(index)
        cursor.setPosition(block.position())
        cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)

    def _alpha_format(self, alpha: float) -> QTextCharFormat:
        color = QColor(self.palette().color(QPalette.ColorRole.Text))
        color.setAlphaF(max(0.0, min(1.0, alpha)))
        char_format = QTextCharFormat()
        char_format.setForeground(QBrush(color))
        return char_format

    def _start_fade(self, indices: Iterable[int]) -> None:
        members = {index for index in indices if 0 <= index < len(self._colors)}
        if not members:
            return
        for index in members:
            self._leave_fade(index)
        animation = QVariantAnimation(self)
        animation.setDuration(TOKENS.fade_ms)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.valueChanged.connect(partial(self._apply_alpha, animation))
        animation.finished.connect(partial(self._finish_fade, animation))
        self._fade_members[animation] = members
        for index in members:
            self._fade_of[index] = animation
        animation.start()

    def _apply_alpha(self, animation: QVariantAnimation, value: object) -> None:
        members = self._fade_members.get(animation)
        if not members:
            return
        alpha = float(value) if isinstance(value, int | float) else 1.0
        self._format_blocks(members, self._alpha_format(alpha))

    def _finish_fade(self, animation: QVariantAnimation) -> None:
        members = self._fade_members.pop(animation, set[int]())
        for index in members:
            if self._fade_of.get(index) is animation:
                del self._fade_of[index]
        self._format_blocks(members, QTextCharFormat(), replace=True)
        animation.deleteLater()

    def _leave_fade(self, index: int) -> None:
        """让一段离开它所在的淡入组；组空了就停止并释放动画。"""
        animation = self._fade_of.pop(index, None)
        if animation is None:
            return
        members = self._fade_members.get(animation)
        if members is not None:
            members.discard(index)
            if not members:
                del self._fade_members[animation]
                animation.stop()
                animation.deleteLater()

    def _stop_fades(self) -> None:
        for animation in self._fade_members:
            animation.stop()
            animation.deleteLater()
        self._fade_members.clear()
        self._fade_of.clear()

    def _format_blocks(
        self, indices: Iterable[int], char_format: QTextCharFormat, *, replace: bool = False
    ) -> None:
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        for index in indices:
            if index >= len(self._colors):
                continue
            self._select_block(cursor, index)
            if replace:
                cursor.setCharFormat(char_format)
            else:
                cursor.mergeCharFormat(char_format)
        cursor.endEditBlock()

    # ------------------------------------------------------------------ 绘制

    def paintEvent(self, e: QPaintEvent) -> None:
        super().paintEvent(e)
        if not self._markers_visible or all(color is None for color in self._colors):
            return
        layout = self.document().documentLayout()
        dx = self.horizontalScrollBar().value()
        dy = self.verticalScrollBar().value()
        top, bottom = e.rect().top(), e.rect().bottom()
        painter = QPainter(self.viewport())
        block = self.document().begin()
        for color in self._colors:
            if not block.isValid():
                break
            if color is not None:
                rect = layout.blockBoundingRect(block).translated(-dx, -dy)
                if rect.top() > bottom:
                    break
                if rect.bottom() >= top:
                    painter.fillRect(
                        QRectF(rect.left() + MARKER_OFFSET, rect.top(), MARKER_WIDTH, rect.height()), color
                    )
            block = block.next()
        painter.end()


class _Swatch(QWidget):
    """引擎实例标识颜色的小色块。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor()
        self.setFixedSize(10, 10)

    def color(self) -> QColor:
        return QColor(self._color)

    def set_color(self, color: QColor) -> None:
        if color != self._color:
            self._color = QColor(color)
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(self.rect())
        painter.end()


class EngineRowWidget(QFrame):
    """引擎结果列表的一行：实例名、状态、耗时、复制按钮、译文与失败原因。"""

    def __init__(
        self, engine_id: EngineId, on_copy: Callable[[EngineId], None], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.engine_id = engine_id
        self._row: EngineRow | None = None
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.swatch = _Swatch()
        self.name_label = QLabel()
        bold = QFont(self.name_label.font())
        bold.setBold(True)
        self.name_label.setFont(bold)
        self.status_label = QLabel()
        self.latency_label = QLabel()
        self.copy_button = QPushButton()
        self.copy_button.clicked.connect(lambda: on_copy(self.engine_id))
        self.text_label = QLabel()
        self.text_label.setWordWrap(True)
        self.text_label.setTextFormat(Qt.TextFormat.PlainText)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setVisible(False)
        self._apply_error_color()

        grid = QGridLayout(self)
        grid.setContentsMargins(TOKENS.spacing_m, TOKENS.spacing_s, TOKENS.spacing_m, TOKENS.spacing_s)
        grid.setHorizontalSpacing(TOKENS.spacing_s)
        grid.setVerticalSpacing(TOKENS.spacing_xs)
        grid.addWidget(self.swatch, 0, 0, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self.name_label, 0, 1)
        grid.addWidget(self.status_label, 0, 2)
        grid.addWidget(self.latency_label, 0, 3)
        grid.setColumnStretch(4, 1)
        grid.addWidget(self.copy_button, 0, 5)
        grid.addWidget(self.text_label, 1, 1, 1, 5)
        grid.addWidget(self.error_label, 2, 1, 1, 5)
        self.retranslate()

    @property
    def row(self) -> EngineRow | None:
        return self._row

    def retranslate(self) -> None:
        self.copy_button.setText(tr("bilingual.row.copy"))
        if self._row is not None:
            self.copy_button.setAccessibleName(tr("bilingual.row.copy.name", name=self._row.name))

    def set_row(self, row: EngineRow) -> None:
        self._row = row
        self.swatch.set_color(row.color)
        self.name_label.setText(row.name)
        self.status_label.setText(row.status)
        self.latency_label.setText(row.latency)
        preview = row.text if len(row.text) <= ROW_PREVIEW_CHARS else row.text[:ROW_PREVIEW_CHARS] + "…"
        self.text_label.setText(preview)
        self.text_label.setVisible(bool(row.text))
        self.error_label.setText(row.error)
        self.error_label.setVisible(bool(row.error))
        self.copy_button.setEnabled(bool(row.text))
        self.copy_button.setAccessibleName(tr("bilingual.row.copy.name", name=row.name))

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self._apply_error_color()

    def _apply_error_color(self) -> None:
        palette = self.error_label.palette()
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        # Fluent 的错误色：浅色主题 #C42B1C，深色主题 #FF99A4。
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#FF99A4" if dark else "#C42B1C"))
        self.error_label.setPalette(palette)


class EngineResultList(QScrollArea):
    """引擎结果列表（需求 BI-05）。每个参赛实例一行，只更新变化的行；顺序变化时只移动行控件。"""

    def __init__(self, on_copy: Callable[[EngineId], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_copy = on_copy
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # 列表本身不占 Tab 焦点，焦点落在各行的复制按钮上；QScrollArea 会把获得焦点的行滚动到可见处。
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(TOKENS.spacing_xs)
        self._layout.addStretch(1)
        self.setWidget(container)
        self._widgets: dict[EngineId, EngineRowWidget] = {}
        self._order: list[EngineId] = []

    def rows(self) -> list[EngineRowWidget]:
        """各行控件，按显示顺序。"""
        return [self._widgets[engine_id] for engine_id in self._order]

    def show_rows(self, order: Sequence[EngineId], changed: Sequence[EngineRow]) -> None:
        for row in changed:
            widget = self._widgets.get(row.engine_id)
            if widget is None:
                widget = EngineRowWidget(row.engine_id, self._on_copy)
                self._widgets[row.engine_id] = widget
            widget.set_row(row)
        wanted = [engine_id for engine_id in order if engine_id in self._widgets]
        for engine_id in set(self._widgets) - set(wanted):
            removed = self._widgets.pop(engine_id)
            self._layout.removeWidget(removed)
            removed.deleteLater()
        if wanted != self._order:
            for position, engine_id in enumerate(wanted):
                widget = self._widgets[engine_id]
                if self._layout.indexOf(widget) != position:
                    self._layout.removeWidget(widget)
                    self._layout.insertWidget(position, widget)
            self._order = wanted

    def clear_rows(self) -> None:
        for widget in self._widgets.values():
            self._layout.removeWidget(widget)
            widget.deleteLater()
        self._widgets.clear()
        self._order = []

    def retranslate(self) -> None:
        for widget in self._widgets.values():
            widget.retranslate()
