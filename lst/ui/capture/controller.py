"""截屏控制器（需求 CAP-01 至 CAP-07，架构文档第 7 章第 1、2 步与 11.4 节）。

一次截屏的过程：

1. 收到 StartCapture：对每块屏幕调用一次 ``grabWindow(0)`` 得到冻结画面（物理像素），然后每块屏幕创建一个
   蒙版窗口并显示，鼠标所在屏幕的蒙版获得焦点，Esc 由它接收。冻结在显示蒙版之前完成，画面里不会有蒙版本身。
2. 鼠标按下时记下所在的屏幕，之后的拖动限制在这块屏幕内（CAP-02），坐标换算见 geometry 模块。
3. 松开鼠标：选区宽或高小于 5 个逻辑像素视为取消（CAP-04）；否则从冻结画面裁出选区的物理像素，
   转成 BGRA 的 RawImage，用 UUID4 生成会话号，关闭蒙版后发布 CaptureCompleted。
   蒙版先关闭、事件后发布：覆盖窗口在下一轮事件循环里弹出时，蒙版已经不在了。
4. “取消截屏”（默认 Esc 与右键单击，键位见架构文档 11.8 节）关闭蒙版，不发布任何事件。

自动化接口另有一条不显示蒙版的路径（``capture_rect``，架构文档 16.4 节）：冻结全部屏幕，按原生坐标的矩形
（全局物理像素，可以跨屏）裁出截图，同样用 UUID4 生成会话号并发布 CaptureCompleted。

截屏期间屏幕布局变化（插拔显示器、改分辨率或缩放比例、旋转）时，冻结画面与屏幕已经对不上，
控制器取消这次截屏并发布 NotificationRequested 请用户重来（CAP-07）。判断有两道：

- QGuiApplication 的 screenAdded、screenRemoved、primaryScreenChanged，以及每块屏幕的 geometryChanged、
  logicalDotsPerInchChanged、physicalDotsPerInchChanged、orientationChanged 信号；
- 松开鼠标时再比较一次各屏的几何与设备像素比，防止信号漏发（例如两次变化抵消前的中间状态）。

冻结画面由 freezer 函数给出，缺省时读真实屏幕。测试注入虚构的屏幕布局（负坐标、混合缩放、竖屏），
这时屏幕对象为 None，不做第一道判断。

日志只记录屏幕数、尺寸和耗时，不记录画面内容（需求 PRV-02）。
"""

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import cast

from PySide6.QtCore import QPoint, QRect, Qt, SignalInstance
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QImage, QPainter, QPixmap, QScreen
from PySide6.QtWidgets import QWidget

from lst.contracts.events import NoticeLevel, NotificationRequested
from lst.contracts.ids import SessionId
from lst.contracts.ocr import CaptureCompleted, CaptureGeometry, RawImage
from lst.kernel import EventBus
from lst.ui.capture.geometry import (
    Rect,
    ScreenGeometry,
    Selection,
    anchor_piece,
    logical_rect,
    native_pieces,
    native_rect,
    selection_from_drag,
)
from lst.ui.capture.mask import MaskWindow
from lst.ui.common.bindings import BindingsConfig


@dataclass(frozen=True, slots=True)
class FrozenScreen:
    """一块屏幕的冻结画面。screen 为 None 表示虚构的屏幕（测试用），不跟踪它的变化。"""

    geometry: ScreenGeometry
    pixmap: QPixmap
    screen: QScreen | None = None


type Freezer = Callable[[], Sequence[FrozenScreen]]
"""冻结全部屏幕的函数。返回空序列或抛出异常表示截屏失败。"""

type WindowHook = Callable[[QWidget], None]
"""新建窗口在显示之前交给它，测试用来加上 WA_DontShowOnScreen。"""


def describe_screen(screen: QScreen, pixmap: QPixmap) -> ScreenGeometry:
    """QScreen 与它的冻结画面 → ScreenGeometry。像素尺寸取冻结画面的实际尺寸。"""
    rect = screen.geometry()
    return ScreenGeometry(
        name=screen.name(),
        logical=Rect(rect.x(), rect.y(), rect.width(), rect.height()),
        dpr=screen.devicePixelRatio(),
        pixel_width=pixmap.width(),
        pixel_height=pixmap.height(),
    )


def freeze_screens() -> list[FrozenScreen]:
    """对每块屏幕调用一次 grabWindow(0)，得到物理像素的冻结画面（架构文档 11.4 节第 1 步）。"""
    frozen: list[FrozenScreen] = []
    for screen in QGuiApplication.screens():
        pixmap = screen.grabWindow(0)
        if pixmap.isNull():
            raise RuntimeError(f"屏幕 {screen.name()} 的画面读取失败")
        frozen.append(FrozenScreen(describe_screen(screen, pixmap), pixmap, screen))
    return frozen


def live_signature(screens: Sequence[QScreen]) -> tuple[tuple[str, Rect, float], ...]:
    """真实屏幕当前的名称、逻辑矩形与设备像素比，用于松开鼠标时再确认一次布局没有变化。"""
    out: list[tuple[str, Rect, float]] = []
    for screen in screens:
        rect = screen.geometry()
        logical = Rect(rect.x(), rect.y(), rect.width(), rect.height())
        out.append((screen.name(), logical, screen.devicePixelRatio()))
    return tuple(out)


def to_raw_image(pixmap: QPixmap, pixels: Rect) -> RawImage:
    """从冻结画面裁出物理像素矩形，转成 BGRA 的 RawImage。

    Format_ARGB32 在小端机器上的内存顺序就是 B、G、R、A；grabWindow 给出的是 RGB32，透明度字节恒为 0xFF，
    转换只是一次内存拷贝。QPixmap.copy 的矩形按像素计，不受设备像素比影响。
    """
    image = pixmap.copy(QRect(pixels.x, pixels.y, pixels.width, pixels.height)).toImage()
    return _raw(image)


def _raw(image: QImage) -> RawImage:
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    data = bytes(cast(memoryview, image.constBits()))
    return RawImage(image.width(), image.height(), image.bytesPerLine(), data)


def compose_native(frozen: Sequence[FrozenScreen], rect: Rect) -> tuple[RawImage, CaptureGeometry] | None:
    """按原生坐标的矩形（全局物理像素，可以跨屏）从冻结画面裁出截图，以及覆盖窗口摆放用的几何信息。

    矩形只落在一块屏幕上时与框选的裁切完全相同（to_raw_image）；跨屏时把各屏的部分按原生坐标拼起来，
    屏幕之间的空隙留黑。覆盖窗口按矩形面积最大的那块屏幕摆放，选区是矩形在那块屏幕的坐标系里换算出的逻辑矩形。
    矩形不与任何屏幕相交时返回 None。
    """
    screens = [item.geometry for item in frozen]
    pieces = native_pieces(screens, rect)
    anchor = anchor_piece(pieces)
    if anchor is None:
        return None
    if len(pieces) == 1 and (anchor.source.width, anchor.source.height) == (rect.width, rect.height):
        image = to_raw_image(frozen[anchor.index].pixmap, anchor.source)
    else:
        canvas = QImage(rect.width, rect.height, QImage.Format.Format_RGB32)
        canvas.fill(QColor(0, 0, 0))
        painter = QPainter(canvas)
        try:
            for piece in pieces:
                source = piece.source
                part = frozen[piece.index].pixmap.copy(QRect(source.x, source.y, source.width, source.height))
                part_image = part.toImage()
                part_image.setDevicePixelRatio(1.0)  # 冻结画面带着屏幕的设备像素比，按 1:1 像素拼接
                painter.drawImage(QPoint(piece.target.x, piece.target.y), part_image)
        finally:
            painter.end()
        image = _raw(canvas)
    screen = screens[anchor.index]
    origin = native_rect(screen)
    relative = Rect(rect.x - origin.x, rect.y - origin.y, rect.width, rect.height)
    geometry = CaptureGeometry(
        screen_name=screen.name,
        screen_rect=screen.logical.as_tuple(),
        selection=logical_rect(screen, relative).as_tuple(),
        dpr=screen.dpr,
    )
    return image, geometry


@dataclass(frozen=True, slots=True)
class CaptureTiming:
    """一次截屏开始阶段的耗时（毫秒）：冻结全部屏幕、创建并显示蒙版。"""

    screens: int
    freeze_ms: float
    show_ms: float

    @property
    def total_ms(self) -> float:
        return self.freeze_ms + self.show_ms


@dataclass(eq=False, slots=True)
class _Active:
    """进行中的一次截屏。"""

    frozen: tuple[FrozenScreen, ...]
    masks: list[MaskWindow]
    live: tuple[tuple[str, Rect, float], ...]
    signals: list[SignalInstance] = field(default_factory=lambda: list[SignalInstance]())
    drag_mask: MaskWindow | None = None
    drag_start: tuple[float, float] | None = None


class CaptureController:
    """截屏控制器，实现 MaskHandler。规则见模块说明。"""

    def __init__(
        self,
        bus: EventBus,
        *,
        freezer: Freezer | None = None,
        prepare_window: WindowHook | None = None,
        logger: logging.Logger | None = None,
        session_ids: Callable[[], SessionId] | None = None,
    ) -> None:
        self._bus = bus
        self._freezer: Freezer = freezer if freezer is not None else freeze_screens
        self._tracks_screens = freezer is None
        self._prepare = prepare_window
        self._logger = logger or logging.getLogger(__name__)
        self._session_ids = session_ids or (lambda: SessionId(uuid.uuid4().hex))
        self._active: _Active | None = None
        self._bindings = BindingsConfig()
        self.last_timing: CaptureTiming | None = None
        self.last_session: SessionId | None = None
        """最近一次发布 CaptureCompleted 的会话号。"""

    # ---- 查询 ----

    @property
    def active(self) -> bool:
        return self._active is not None

    @property
    def masks(self) -> tuple[MaskWindow, ...]:
        return tuple(self._active.masks) if self._active is not None else ()

    @property
    def bindings(self) -> BindingsConfig:
        return self._bindings

    def set_bindings(self, bindings: BindingsConfig) -> None:
        """换用新的键位（UI-09）：正在显示的蒙版立即生效，之后的截屏也用它。"""
        self._bindings = bindings
        if self._active is not None:
            for mask in self._active.masks:
                mask.set_bindings(bindings)

    # ---- 开始与结束 ----

    def start(self) -> None:
        """开始截屏。已经在截屏时把蒙版带到前面，不重新冻结。"""
        if self._active is not None:
            self._focus_mask(self._active)
            return
        started = time.perf_counter()
        try:
            frozen = tuple(self._freezer())
        except Exception:
            self._logger.exception("冻结屏幕画面失败")
            frozen = ()
        if not frozen:
            self._notify("capture.failed")
            return
        frozen_at = time.perf_counter()
        screens = [f.screen for f in frozen if f.screen is not None]
        live = live_signature(screens) if self._tracks_screens else ()
        active = _Active(frozen, [], live)
        self._active = active
        for item in frozen:
            mask = MaskWindow(item.geometry, item.pixmap, item.screen, self, self._bindings)
            if self._prepare is not None:
                self._prepare(mask)
            active.masks.append(mask)
        for mask in active.masks:
            mask.show()
        self._focus_mask(active)
        if self._tracks_screens:
            self._watch_screens(active)
        shown_at = time.perf_counter()
        self.last_timing = CaptureTiming(
            len(frozen), (frozen_at - started) * 1000, (shown_at - frozen_at) * 1000
        )
        self._logger.info(
            "截屏蒙版已显示：%d 块屏幕（%s），冻结 %.1f 毫秒，创建并显示蒙版 %.1f 毫秒",
            len(frozen),
            "、".join(
                f"{f.geometry.pixel_width}×{f.geometry.pixel_height}@{f.geometry.dpr:g}" for f in frozen
            ),
            self.last_timing.freeze_ms,
            self.last_timing.show_ms,
        )

    def capture_rect(self, rect: Rect) -> SessionId:
        """不显示蒙版，直接冻结全部屏幕、按原生坐标的矩形截取，然后发布 CaptureCompleted（自动化接口用）。

        截图与几何信息见 compose_native。正在框选或冻结屏幕失败时抛 RuntimeError；
        矩形太小或不与任何屏幕相交时抛 ValueError。
        """
        if self._active is not None:
            raise RuntimeError("正在框选，不能同时直接截取")
        if rect.width < 1 or rect.height < 1:
            raise ValueError("矩形的宽高至少为 1 物理像素")
        try:
            frozen = tuple(self._freezer())
        except Exception as exc:
            raise RuntimeError(f"冻结屏幕画面失败：{exc}") from exc
        if not frozen:
            raise RuntimeError("冻结屏幕画面失败：没有屏幕")
        composed = compose_native(frozen, rect)
        if composed is None:
            raise ValueError("矩形不在任何屏幕上")
        image, geometry = composed
        session_id = self._session_ids()
        self.last_session = session_id
        self._logger.info(
            "直接截取：会话 %s，屏幕 %s，%d×%d 物理像素",
            session_id,
            geometry.screen_name,
            image.width,
            image.height,
        )
        self._bus.publish(CaptureCompleted(session_id, image, geometry))
        return session_id

    def cancel(self) -> None:
        """取消截屏：关闭蒙版，不发布事件。没有在截屏时什么也不做。"""
        if self._active is not None:
            self._logger.info("截屏已取消")
            self._close()

    def screens_changed(self, *_args: object) -> None:
        """截屏期间屏幕布局变化：取消并提示重来（CAP-07）。"""
        if self._active is None:
            return
        self._logger.warning("截屏期间屏幕布局发生变化，取消这次截屏")
        self._close()
        self._notify("capture.screens_changed", NoticeLevel.WARNING)

    # ---- MaskHandler ----

    def cancel_requested(self) -> None:
        self.cancel()

    def mask_pressed(self, mask: MaskWindow, x: float, y: float) -> None:
        active = self._active
        if active is None or mask not in active.masks:
            return
        for other in active.masks:
            other.set_selection(None)
        active.drag_mask = mask
        active.drag_start = (x, y)

    def mask_moved(self, mask: MaskWindow, x: float, y: float) -> None:
        active = self._active
        if active is None or active.drag_mask is not mask or active.drag_start is None:
            return
        mask.set_selection(selection_from_drag(mask.screen_geometry, active.drag_start, (x, y)))

    def mask_released(self, mask: MaskWindow, x: float, y: float) -> None:
        active = self._active
        if active is None or active.drag_mask is not mask or active.drag_start is None:
            return
        selection = selection_from_drag(mask.screen_geometry, active.drag_start, (x, y))
        if selection is None:
            self._logger.info("选区过小，视为取消")
            self._close()
            return
        if self._tracks_screens and self._layout_changed(active):
            self.screens_changed()
            return
        self._complete(mask, selection)

    # ---- 内部 ----

    def _complete(self, mask: MaskWindow, selection: Selection) -> None:
        image = to_raw_image(mask.frozen, selection.pixels)
        screen = selection.screen
        geometry = CaptureGeometry(
            screen_name=screen.name,
            screen_rect=screen.logical.as_tuple(),
            selection=selection.logical.as_tuple(),
            dpr=screen.dpr,
        )
        session_id = self._session_ids()
        self.last_session = session_id
        self._close()
        self._logger.info(
            "截屏完成：会话 %s，屏幕 %s（缩放 %g），选区 %d×%d 物理像素",
            session_id,
            screen.name,
            screen.dpr,
            image.width,
            image.height,
        )
        self._bus.publish(CaptureCompleted(session_id, image, geometry))

    def _close(self) -> None:
        active, self._active = self._active, None
        if active is None:
            return
        for signal in active.signals:
            signal.disconnect(self.screens_changed)
        for mask in active.masks:
            mask.hide()
            mask.deleteLater()

    def _focus_mask(self, active: _Active) -> None:
        """鼠标所在屏幕的蒙版获得焦点，用来接收 Esc。不显示在屏幕上的窗口（测试）不抢前台。"""
        cursor = QCursor.pos()
        target = next(
            (m for m in active.masks if m.screen_geometry.contains(cursor.x(), cursor.y())),
            active.masks[0],
        )
        if not target.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
            target.raise_()
            target.activateWindow()
        target.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _watch_screens(self, active: _Active) -> None:
        app = cast(QGuiApplication, QGuiApplication.instance())
        signals = [app.screenAdded, app.screenRemoved, app.primaryScreenChanged]
        for item in active.frozen:
            screen = item.screen
            if screen is not None:
                signals += [
                    screen.geometryChanged,
                    screen.logicalDotsPerInchChanged,
                    screen.physicalDotsPerInchChanged,
                    screen.orientationChanged,
                ]
        for signal in signals:
            signal.connect(self.screens_changed)
            active.signals.append(signal)

    def _layout_changed(self, active: _Active) -> bool:
        screens = [f.screen for f in active.frozen if f.screen is not None]
        if len(QGuiApplication.screens()) != len(active.frozen):
            return True
        return live_signature(screens) != active.live

    def _notify(self, key: str, level: NoticeLevel = NoticeLevel.ERROR) -> None:
        self._bus.publish(NotificationRequested(f"{key}.title", f"{key}.message", level))


__all__ = [
    "CaptureController",
    "CaptureTiming",
    "Freezer",
    "FrozenScreen",
    "WindowHook",
    "compose_native",
    "describe_screen",
    "freeze_screens",
    "to_raw_image",
]
