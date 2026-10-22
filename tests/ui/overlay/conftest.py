"""覆盖窗口测试的夹具与消息构造。

窗口：全部覆盖窗口在显示之前加上 WA_DontShowOnScreen（OverlayModule 的 prepare_window），
offscreen 与原生平台上都不出现在桌面上。剪贴板换成记录列表，不改动用户的系统剪贴板。
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import numpy as np
import pytest
import shiboken6
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from lst.contracts.events import SegmentsUpdated, SessionProgress
from lst.contracts.ids import EngineId, SegmentId, SessionId
from lst.contracts.ocr import (
    BBox,
    BlockCategory,
    CaptureCompleted,
    CaptureGeometry,
    LayoutBlock,
    OcrCompleted,
    OcrSegment,
    RawImage,
    RetryOcr,
    TableCell,
)
from lst.contracts.translation import AttemptState, AttemptView, Segment, SegmentView
from lst.kernel import EventBus, ModuleContext
from lst.ui.overlay import OverlayModule, OverlayWindow

WIDTH, HEIGHT = 600, 300
"""测试截图的物理像素尺寸。"""


def hide_from_desktop(widget: QWidget) -> None:
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)


def gone(widget: QWidget) -> bool:
    return not shiboken6.isValid(widget) or not widget.isVisible()


def raw_image(width: int = WIDTH, height: int = HEIGHT, *, light: bool = True) -> RawImage:
    """合成截图：浅色（或深色）底，上面几条深色（或浅色）横条模拟文字行。"""
    base, ink = (235, 30) if light else (25, 220)
    pixels = np.full((height, width, 4), base, dtype=np.uint8)
    pixels[..., 3] = 255
    for row in range(20, height - 20, 40):
        pixels[row : row + 14, 20 : width - 20, :3] = ink
    return RawImage(width, height, width * 4, pixels.tobytes())


def capture(
    session: str, *, dpr: float = 1.5, light: bool = True, at: tuple[int, int] = (100, 80)
) -> CaptureCompleted:
    width, height = round(WIDTH / dpr), round(HEIGHT / dpr)
    geometry = CaptureGeometry("screen", (0, 0, 1707, 960), (at[0], at[1], width, height), dpr)
    return CaptureCompleted(SessionId(session), raw_image(light=light), geometry)


def segment(
    sid: str, text: str, bbox: BBox, block: str = "0", cell: tuple[int, int] | None = None
) -> OcrSegment:
    return OcrSegment(Segment(SegmentId(sid), text), block, bbox, cell)


def text_result(session: str) -> OcrCompleted:
    """文本模式：一个覆盖整张截图的正文块，三个段各有自己的范围（架构文档 8.4 节）。"""
    block = LayoutBlock("0", BlockCategory.TEXT, BBox(0, 0, WIDTH, HEIGHT), 0, 1.0)
    segments = (
        segment("p1", "Hello world", BBox(20, 20, 580, 60)),
        segment("p2", "The quick brown fox jumps over the lazy dog.", BBox(20, 100, 580, 180)),
        segment("p3", "Short", BBox(20, 220, 200, 280)),
    )
    return OcrCompleted(SessionId(session), (block,), segments)


def layout_result(session: str) -> OcrCompleted:
    """版面模式：标题、表格（2×2）、图片、公式与图表（数据表 2×2）。"""
    cells = (
        TableCell(0, 0, 1, 1, BBox(20, 70, 160, 110), "Region"),
        TableCell(0, 1, 1, 1, BBox(160, 70, 300, 110), "Sales"),
        TableCell(1, 0, 1, 1, BBox(20, 110, 160, 150), "North"),
        TableCell(1, 1, 1, 1, BBox(160, 110, 300, 150), "120"),
    )
    blocks = (
        LayoutBlock("0", BlockCategory.TITLE, BBox(20, 10, 580, 50), 0, 1.0, text="Quarterly report"),
        LayoutBlock("1", BlockCategory.TABLE, BBox(20, 70, 300, 150), 1, 1.0, cells=cells),
        LayoutBlock("2", BlockCategory.IMAGE, BBox(320, 70, 580, 150), 2, 1.0),
        LayoutBlock("3", BlockCategory.FORMULA, BBox(20, 170, 300, 210), 3, 1.0, latex="E=mc^2"),
        LayoutBlock(
            "4",
            BlockCategory.CHART,
            BBox(320, 170, 580, 290),
            4,
            1.0,
            chart_table=(("Year", "Value"), ("2024", "7")),
        ),
    )
    segments = (
        segment("b0", "Quarterly report", BBox(20, 10, 580, 50), "0"),
        segment("b1-r0c0", "Region", cells[0].bbox, "1", (0, 0)),
        segment("b1-r0c1", "Sales", cells[1].bbox, "1", (0, 1)),
        segment("b1-r1c0", "North", cells[2].bbox, "1", (1, 0)),
        segment("b1-r1c1", "120", cells[3].bbox, "1", (1, 1)),
        segment("b4-r0c0", "Year", BBox(320, 170, 580, 290), "4", (0, 0)),
        segment("b4-r0c1", "Value", BBox(320, 170, 580, 290), "4", (0, 1)),
        segment("b4-r1c0", "2024", BBox(320, 170, 580, 290), "4", (1, 0)),
        segment("b4-r1c1", "7", BBox(320, 170, 580, 290), "4", (1, 1)),
    )
    return OcrCompleted(SessionId(session), blocks, segments)


def view(
    sid: str, source: str, text: str | None = None, *, engine: str = "argos", finished: bool = False
) -> SegmentView:
    state = AttemptState.DONE if text is not None else AttemptState.RUNNING
    attempt = AttemptView(
        EngineId(engine), state, text, None, 12.0 if text is not None else None, started=True
    )
    return SegmentView(
        segment_id=SegmentId(sid),
        source_text=source,
        display_text=text if text is not None else source,
        shown_engine=EngineId(engine) if text is not None else None,
        finished=finished,
        attempts=(attempt,),
    )


@dataclass
class Rig:
    bus: EventBus
    module: OverlayModule
    ctx: ModuleContext
    copied: list[str]
    retries: list[RetryOcr]

    @property
    def window(self) -> OverlayWindow:
        window = self.module.window
        assert window is not None, "当前没有覆盖窗口"
        return window

    async def publish(self, *messages: object) -> None:
        for message in messages:
            self.bus.publish(message)  # pyright: ignore[reportArgumentType]
        await self.bus.drain()

    async def updates(self, session: str, *views: SegmentView) -> None:
        await self.publish(SegmentsUpdated(SessionId(session), views))

    async def progress(self, session: str, finished: int, total: int) -> None:
        await self.publish(SessionProgress(SessionId(session), finished, total))


type MakeRig = Callable[[], Awaitable[Rig]]


@pytest.fixture
async def make_rig(bus: EventBus) -> AsyncIterator[MakeRig]:
    rigs: list[Rig] = []
    retries: list[RetryOcr] = []
    bus.subscribe(RetryOcr, retries.append)

    async def make() -> Rig:
        copied: list[str] = []
        module = OverlayModule(clipboard=copied.append, prepare_window=hide_from_desktop)
        ctx = ModuleContext(bus, logging.getLogger("lst.ui.overlay"))
        await module.start(ctx)
        await bus.drain()
        rig = Rig(bus, module, ctx, copied, retries)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        await rig.module.stop()
        rig.ctx.close()
    await bus.drain()


def mouse(
    widget: QWidget,
    kind: QEvent.Type,
    x: float,
    y: float,
    button: Qt.MouseButton,
    *,
    global_pos: QPointF | None = None,
) -> None:
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button
    if kind == QEvent.Type.MouseMove and button == Qt.MouseButton.NoButton:
        buttons = Qt.MouseButton.NoButton
    local = QPointF(x, y)
    position = global_pos if global_pos is not None else widget.mapToGlobal(local)
    event = QMouseEvent(kind, local, position, button, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)
