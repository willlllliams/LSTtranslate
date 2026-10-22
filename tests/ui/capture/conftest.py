"""截屏模块测试的夹具与构造函数。

窗口：全部蒙版窗口在显示之前加上 WA_DontShowOnScreen（CaptureModule 的 prepare_window），
在 offscreen 与原生平台（QT_QPA_PLATFORM=windows）上都不会出现在桌面上，也不抢前台。

屏幕：多数测试注入虚构的屏幕布局（FakeScreens），冻结画面是按坐标编码颜色的合成图像，
测试据此检查裁切出的像素来自正确的位置。读真实屏幕的测试只读一次画面到内存，不保存、不外传。

鼠标：用 QMouseEvent 直接发给蒙版窗口，位置是浮点数的窗口坐标，与 Qt 6 在高 DPI 屏幕上给出的一致。
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import numpy as np
import pytest
import shiboken6
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from lst.contracts.events import NotificationRequested
from lst.contracts.ocr import CaptureCompleted, RawImage
from lst.kernel import EventBus, ModuleContext
from lst.ui.capture import CaptureModule, FrozenScreen, MaskWindow, Rect, screen_from_native
from lst.ui.capture.controller import CaptureController


def gone(widget: QWidget) -> bool:
    """窗口已经关闭：隐藏了，或者 deleteLater 之后 C++ 对象已经析构。"""
    return not shiboken6.isValid(widget) or not widget.isVisible()


def hide_from_desktop(widget: QWidget) -> None:
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)


def coded_color(x: int, y: int) -> QColor:
    """合成冻结画面在 (x, y) 处的颜色：红 = x mod 256，绿 = y mod 256，蓝 = x // 256 + 16 × (y // 256)。"""
    return QColor(x % 256, y % 256, (x // 256 + 16 * (y // 256)) % 256)


def coded_pixmap(width: int, height: int) -> QPixmap:
    """按 coded_color 编码坐标的合成画面（BGRA 字节用 numpy 一次生成）。"""
    xs = np.arange(width, dtype=np.int64)[None, :]
    ys = np.arange(height, dtype=np.int64)[:, None]
    pixels = np.empty((height, width, 4), dtype=np.uint8)
    pixels[..., 0] = (xs // 256 + 16 * (ys // 256)) % 256
    pixels[..., 1] = ys % 256
    pixels[..., 2] = xs % 256
    pixels[..., 3] = 255
    data = pixels.tobytes()
    image = QImage(data, width, height, width * 4, QImage.Format.Format_RGB32).copy()
    return QPixmap.fromImage(image)


def pixel_at(image: RawImage, x: int, y: int) -> tuple[int, int, int]:
    """RawImage（BGRA）在 (x, y) 处的（红，绿，蓝）。"""
    offset = y * image.stride + x * 4
    b, g, r = image.data[offset], image.data[offset + 1], image.data[offset + 2]
    return r, g, b


@dataclass
class FakeScreens:
    """虚构的屏幕布局：每项是（屏幕名，原生矩形，设备像素比）。frozen 记录每次冻结得到的画面。"""

    layout: list[tuple[str, Rect, float]]
    calls: int = 0
    fail: bool = False
    _cache: dict[str, QPixmap] = field(default_factory=lambda: dict[str, QPixmap]())

    def __call__(self) -> list[FrozenScreen]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("读取屏幕失败")
        out: list[FrozenScreen] = []
        for name, native, dpr in self.layout:
            geometry = screen_from_native(name, native, dpr)
            pixmap = self._cache.get(name)
            if pixmap is None:
                pixmap = coded_pixmap(native.width, native.height)
                self._cache[name] = pixmap
            out.append(FrozenScreen(geometry, pixmap))
        return out


@dataclass
class Rig:
    bus: EventBus
    module: CaptureModule
    ctx: ModuleContext
    completed: list[CaptureCompleted]
    notices: list[NotificationRequested]

    @property
    def controller(self) -> CaptureController:
        controller = self.module.controller
        assert controller is not None
        return controller

    @property
    def masks(self) -> tuple[MaskWindow, ...]:
        return self.controller.masks

    def mask(self, name: str) -> MaskWindow:
        return next(m for m in self.masks if m.screen_geometry.name == name)


type MakeRig = Callable[..., Awaitable[Rig]]


@pytest.fixture
async def make_rig(bus: EventBus) -> AsyncIterator[MakeRig]:
    rigs: list[Rig] = []
    completed: list[CaptureCompleted] = []
    notices: list[NotificationRequested] = []
    bus.subscribe(CaptureCompleted, completed.append)
    bus.subscribe(NotificationRequested, notices.append)

    async def make(freezer: FakeScreens | None = None, *, real: bool = False) -> Rig:
        module = CaptureModule(freezer=None if real else freezer, prepare_window=hide_from_desktop)
        ctx = ModuleContext(bus, logging.getLogger("lst.ui.capture"))
        await module.start(ctx)
        rig = Rig(bus, module, ctx, completed, notices)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        await rig.module.stop()
        rig.ctx.close()


def mouse(widget: QWidget, kind: QEvent.Type, x: float, y: float, button: Qt.MouseButton) -> None:
    """给窗口发一个鼠标事件，位置是窗口内的浮点逻辑坐标。"""
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button
    local = QPointF(x, y)
    event = QMouseEvent(
        kind, local, widget.mapToGlobal(local), button, buttons, Qt.KeyboardModifier.NoModifier
    )
    QApplication.sendEvent(widget, event)


def drag(widget: QWidget, start: tuple[float, float], end: tuple[float, float]) -> None:
    """左键从 start 拖到 end（窗口内的逻辑坐标），中间经过一个点。"""
    left = Qt.MouseButton.LeftButton
    mouse(widget, QEvent.Type.MouseButtonPress, *start, left)
    middle = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    mouse(widget, QEvent.Type.MouseMove, *middle, left)
    mouse(widget, QEvent.Type.MouseMove, *end, left)
    mouse(widget, QEvent.Type.MouseButtonRelease, *end, left)
