"""截屏蒙版窗口（需求 CAP-01、CAP-02、CAP-04，架构文档 11.4 节）。

每块屏幕一个无边框、置顶的窗口，铺满这块屏幕，画出冻结画面，再盖一层半透明遮罩；选区内不盖遮罩，
边上画一圈强调色的细线，左上方标出选区的物理像素尺寸。

冻结画面按 1:1 画到物理像素上：图像的设备像素比设成屏幕的设备像素比，Qt 绘制时不再缩放。
选区的四条边按物理像素画（逻辑坐标 = 物理像素 ÷ 设备像素比），与最终裁切的范围完全一致。

窗口只负责绘制和收集输入，鼠标与键盘操作交给 MaskHandler（截屏控制器），坐标一律换算成全局逻辑坐标。
事件按键位表（``Keymap``，配置的 [bindings] 节，需求 UI-09，架构文档 11.8 节）查出操作再执行，
括号里是默认键位：

- 框选区域（左键拖动）：移动超过 4 个逻辑像素后从按下的位置开始框选，松开时完成；
  按下后没有移动就松开，相当于选区为空，按 CAP-04 取消；
- 取消截屏（右键单击、Esc）：按键加修饰键没有绑定拖动时，单击在按下时就生效，框选途中按下也立即取消。
拖动时只重绘选区新旧位置覆盖的区域，4K 屏上每次鼠标移动也只重绘一小块。
"""

from typing import Final, Protocol

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFontMetricsF,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QScreen,
)
from PySide6.QtWidgets import QWidget

from lst.ui.capture.geometry import ScreenGeometry, Selection, physical_size_text
from lst.ui.common.bindings import BindingsConfig, Keymap, MouseGestures, Scope, action_label
from lst.ui.i18n import tr
from lst.ui.theme import TOKENS, theme_colors, ui_font

_DIM: Final = QColor(0, 0, 0, 110)
"""遮罩颜色：黑色，透明度约 43%。冻结画面仍然看得清，选区与其余部分对比明显。"""

_LABEL_BG: Final = QColor(32, 32, 32, 220)
_LABEL_FG: Final = QColor(255, 255, 255)
_LABEL_PX: Final = 12
_HINT_PX: Final = 14
_MARGIN: Final = 6
"""尺寸标签与选区之间、提示条与屏幕顶边之间的距离（逻辑像素）。"""


class MaskHandler(Protocol):
    """蒙版窗口把用户操作交给它。坐标都是全局逻辑坐标。"""

    def mask_pressed(self, mask: "MaskWindow", x: float, y: float) -> None: ...
    def mask_moved(self, mask: "MaskWindow", x: float, y: float) -> None: ...
    def mask_released(self, mask: "MaskWindow", x: float, y: float) -> None: ...
    def cancel_requested(self) -> None: ...


class MaskWindow(QWidget):
    """一块屏幕的蒙版。

    frozen 是这块屏幕的冻结画面（物理像素）；screen 为 None 时（测试里的虚构屏幕）不指定窗口所在的屏幕。
    """

    def __init__(
        self,
        geometry: ScreenGeometry,
        frozen: QPixmap,
        screen: QScreen | None,
        handler: MaskHandler,
        bindings: BindingsConfig | None = None,
    ) -> None:
        flags = (
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setWindowTitle("LSTtranslate capture")
        self._geometry = geometry
        self._frozen = QPixmap(frozen)
        self._frozen.setDevicePixelRatio(geometry.dpr)
        self._handler = handler
        self._selection: Selection | None = None
        self._painted = False
        self._bindings = bindings if bindings is not None else BindingsConfig()
        self._keymap = Keymap.for_scope(self._bindings, Scope.CAPTURE)
        self._gestures = MouseGestures(self._keymap)
        if screen is not None:
            self.setScreen(screen)
        rect = geometry.logical
        self.setGeometry(QRect(rect.x, rect.y, rect.width, rect.height))

    # ---- 查询 ----

    @property
    def screen_geometry(self) -> ScreenGeometry:
        return self._geometry

    @property
    def frozen(self) -> QPixmap:
        """冻结画面（物理像素，设备像素比为屏幕的设备像素比）。"""
        return self._frozen

    @property
    def selection(self) -> Selection | None:
        return self._selection

    @property
    def keymap(self) -> Keymap:
        return self._keymap

    @property
    def painted(self) -> bool:
        """是否已经绘制过一次。测量“热键到蒙版出现”的耗时时用。"""
        return self._painted

    # ---- 控制器调用 ----

    def set_selection(self, selection: Selection | None) -> None:
        """更新选区，只重绘新旧选区（含尺寸标签）覆盖的区域。"""
        if selection == self._selection:
            return
        old = self._decoration_rect(self._selection)
        self._selection = selection
        new = self._decoration_rect(selection)
        dirty = old.united(new) if not old.isNull() and not new.isNull() else (old if new.isNull() else new)
        if dirty.isNull():
            return
        self.update(dirty)

    def set_bindings(self, bindings: BindingsConfig) -> None:
        """换用新的键位。正在进行的拖动按按下时的键位做完。"""
        self._bindings = bindings
        self._keymap = Keymap.for_scope(bindings, Scope.CAPTURE)
        self._gestures.keymap = self._keymap
        if self._selection is None:
            self.update()

    # ---- 几何 ----

    def selection_rect(self, selection: Selection | None = None) -> QRectF:
        """选区在本窗口里的逻辑坐标矩形，边界落在物理像素上。"""
        selection = selection if selection is not None else self._selection
        if selection is None:
            return QRectF()
        dpr = self._geometry.dpr
        pixels = selection.pixels
        return QRectF(pixels.x / dpr, pixels.y / dpr, pixels.width / dpr, pixels.height / dpr)

    def _label_rect(self, selection: Selection | None) -> QRectF:
        """尺寸标签的位置：选区左上角上方；上方放不下时放到选区内的左上角。"""
        if selection is None:
            return QRectF()
        metrics = QFontMetricsF(ui_font(_LABEL_PX))
        text = physical_size_text(selection)
        width = metrics.horizontalAdvance(text) + 2 * TOKENS.spacing_s
        height = metrics.height() + TOKENS.spacing_xs
        area = self.selection_rect(selection)
        y = area.top() - height - _MARGIN
        if y < 0:
            y = area.top() + _MARGIN
        x = min(max(area.left(), 0.0), max(0.0, self.width() - width))
        return QRectF(x, y, width, height)

    def _decoration_rect(self, selection: Selection | None) -> QRect:
        """选区和尺寸标签合起来的范围，向外留 2 个逻辑像素给边线。"""
        if selection is None:
            return QRect()
        bounds = self.selection_rect(selection).united(self._label_rect(selection))
        return bounds.adjusted(-2, -2, 2, 2).toAlignedRect()

    def _hint_rect(self, metrics: QFontMetricsF, text: str) -> QRectF:
        width = metrics.horizontalAdvance(text) + 2 * TOKENS.spacing_l
        height = metrics.height() + 2 * TOKENS.spacing_s
        return QRectF((self.width() - width) / 2, TOKENS.spacing_xl, width, height)

    # ---- 绘制 ----

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        self._painted = True
        painter = QPainter(self)
        try:
            painter.drawPixmap(QPointF(0, 0), self._frozen)
            area = self.selection_rect()
            whole = QRectF(self.rect())
            if area.isNull():
                painter.fillRect(whole, _DIM)
                self._paint_hint(painter)
                return
            # 选区之外的四块盖上遮罩，选区内保持冻结画面的原样。
            painter.fillRect(QRectF(0, 0, whole.width(), area.top()), _DIM)
            painter.fillRect(QRectF(0, area.bottom(), whole.width(), whole.height() - area.bottom()), _DIM)
            painter.fillRect(QRectF(0, area.top(), area.left(), area.height()), _DIM)
            right = QRectF(area.right(), area.top(), whole.width() - area.right(), area.height())
            painter.fillRect(right, _DIM)
            pen = QPen(theme_colors().accent)
            pen.setCosmetic(True)
            pen.setWidth(1)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # 细线画在选区外侧一圈，不盖住会被截取的像素。
            painter.drawRect(area.adjusted(-0.5, -0.5, 0.5, 0.5))
            self._paint_size_label(painter)
        finally:
            painter.end()

    def hint_text(self) -> str:
        """提示条的文字：按当前键位列出框选与取消的键位。"""
        items = [
            tr("capture.hint.item", action=tr(f"bindings.action.{action}"), keys=label)
            for action in ("capture.select", "capture.cancel")
            if (label := action_label(self._bindings, action))
        ]
        return tr("capture.hint.separator").join(items)

    def _paint_hint(self, painter: QPainter) -> None:
        font = ui_font(_HINT_PX)
        metrics = QFontMetricsF(font)
        text = self.hint_text()
        rect = self._hint_rect(metrics, text)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_LABEL_BG)
        painter.drawRoundedRect(rect, TOKENS.radius_m, TOKENS.radius_m)
        painter.setFont(font)
        painter.setPen(_LABEL_FG)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_size_label(self, painter: QPainter) -> None:
        selection = self._selection
        if selection is None:
            return
        rect = self._label_rect(selection)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_LABEL_BG)
        painter.drawRoundedRect(rect, TOKENS.radius_s, TOKENS.radius_s)
        painter.setFont(ui_font(_LABEL_PX))
        painter.setPen(_LABEL_FG)
        text = tr("capture.size", width=selection.pixels.width, height=selection.pixels.height)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    # ---- 输入 ----

    def _global(self, event: QMouseEvent) -> tuple[float, float]:
        """事件位置换算成全局逻辑坐标：窗口铺满屏幕，左上角就是屏幕的逻辑左上角。"""
        position = event.position()
        rect = self._geometry.logical
        return rect.x + position.x(), rect.y + position.y()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        fired = self._gestures.press(event.button(), event.modifiers(), event.position())
        if fired is not None:
            self._trigger(fired[1])
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        action = self._keymap.mouse_action(event.button(), event.modifiers(), "DoubleClick")
        if action is None:
            self.mousePressEvent(event)
            return
        self._trigger(action)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        step = self._gestures.move(event.position())
        if step is not None and step[1] == "capture.select":
            if step[0] == "start":
                self._handler.mask_pressed(self, *self._origin())
            self._handler.mask_moved(self, *self._global(event))
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        origin = self._origin()
        result = self._gestures.release(event.button())
        if result is not None:
            kind, action = result
            if kind == "click":
                self._trigger(action)
            elif action == "capture.select":
                if kind == "tap":
                    # 没有移动就松开：选区为空，控制器按 CAP-04 取消。
                    self._handler.mask_pressed(self, *origin)
                self._handler.mask_released(self, *self._global(event))
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        action = self._keymap.key_action(event)
        if action is None:
            super().keyPressEvent(event)
            return
        self._trigger(action)
        event.accept()

    def _origin(self) -> tuple[float, float]:
        """正在跟踪的按下位置（全局逻辑坐标）。"""
        origin = self._gestures.origin
        rect = self._geometry.logical
        if origin is None:
            return float(rect.x), float(rect.y)
        return rect.x + origin.x(), rect.y + origin.y()

    def _trigger(self, action: str) -> None:
        if action == "capture.cancel":
            self._handler.cancel_requested()
