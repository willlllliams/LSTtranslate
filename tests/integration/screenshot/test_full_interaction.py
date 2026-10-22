"""完整截图翻译交互：真实组合根、真实 Windows OCR 子进程与真实 NMT 子进程，只替换屏幕。

lst.app.bootstrap.Application 按产品的方式启动全部模块，OCR 进程用 Windows 自带 OCR
（[ocr].engine = "windows"，识别语言 en），NMT 进程加载本机 assets/argosmodel 里的 Argos 英中模型
（组合根的 LEGACY_NMT_MODELS 回退），引擎列表只有本地 NMT 一个实例，截屏目标语言是简体中文。替换掉的外部依赖：

- 屏幕：虚构的两屏布局，左侧副屏 150% 缩放、坐标为负。冻结画面用 QPainter 画白底黑字的英文：
  左屏上两行要翻译的句子和一行选区之外的干扰文字，主屏上另一行干扰文字；
- 窗口：蒙版与覆盖窗口在显示前加上 WA_DontShowOnScreen（CaptureModule、OverlayModule 的 prepare_window），
  不出现在桌面上；
- 剪贴板：覆盖窗口的复制先交给记录函数；offscreen 平台上再写进 Qt 剪贴板并读回核对，
  原生平台（QT_QPA_PLATFORM=windows）上不写系统剪贴板，不覆盖用户的剪贴板内容；
- 数据目录：tmp_path；密钥：MemorySecretStore；HTTP：MockTransport，索引地址是 .invalid，全程不应有请求；
- 热键：配置成不会与用户按键冲突的 Ctrl+Alt+Shift+F20/F21。截屏由总线上的 StartCapture 触发，
  与热键模块收到 WM_HOTKEY 后发出的命令相同；
- 首次运行引导：ui_state.ini 预先记为已完成，引导窗口不弹出。

交互顺序：StartCapture → 蒙版出现 → 在 150% 屏上拖出覆盖两行英文的选区 → CaptureCompleted
（截图像素与冻结画面的选区逐字节相同）→ OcrCompleted（两段英文）→ SegmentsUpdated 给出含汉字的译文
→ 覆盖窗口进入结果阶段并画出译文 → 左键拖选第一段、Ctrl+C 只复制它 → 左键单击清除选择、Ctrl+C 复制全部
→ 右键单击切到原文、再单击切回译文 → Esc 关闭覆盖窗口；
之后三次截屏分别用右键、Esc、过小的选区取消，都不发布 CaptureCompleted。

设置环境变量 ``LST_SCREENSHOT_SHOTS=1`` 时，把带选区的蒙版和覆盖窗口的最终画面存到
data/tmp/capture-shots/full-*.png，默认不存；full-overlay-selection.png 是跨段选中时的画面。

跳过条件：本机没有 assets/argosmodel 下的英中模型包；Windows 没有英文 OCR 语言（或缺 pywinrt）；
没有用来画英文图的微软雅黑字体文件（offscreen 平台没有系统字体库，要显式加载，
见 tests/workers/ocr/screens.py）。
"""

import asyncio
import ctypes
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import tomlkit
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

import lst.app.bootstrap as bootstrap
from lst.app.bootstrap import Application, AppOptions
from lst.app.paths import AppPaths
from lst.contracts.events import EngineRoster, SegmentsUpdated, SessionProgress, StartCapture
from lst.contracts.ids import EngineId, Lang
from lst.contracts.ocr import CaptureCompleted, OcrCompleted, OcrFailed
from lst.contracts.ports import WorkerState
from lst.services.config import MemorySecretStore
from lst.ui.capture import CaptureModule, FrozenScreen, MaskWindow, Rect, screen_from_native, to_raw_image
from lst.ui.overlay import OverlayModule, OverlayWindow, Phase, system_clipboard
from lst.ui.overlay import SegmentItem as OverlaySegment
from lst.workers.ocr_worker.winocr_backend import WinRtOcrApi, match_language
from tests.ui.capture.conftest import drag, gone, hide_from_desktop, mouse
from tests.workers.ocr.screens import FONT_FAMILY, FONT_FILE, ensure_font
from tests.workers.packages import REAL_EN_ZH

ROOT = Path(__file__).resolve().parents[3]
SHOTS_DIR = ROOT / "data" / "tmp" / "capture-shots"
INDEX_URL = "https://index.invalid/index.json"
DICT_INDEX_URL = "https://dicts.invalid/dict-index.json"

LEFT = ("left", Rect(-2560, -200, 2560, 1440), 1.5)
MAIN = ("main", Rect(0, 0, 1920, 1080), 1.0)
TEXT_PX = 48
"""冻结画面上英文的字号（物理像素），Windows OCR 在这个字号上识别稳定。"""

SENTENCES = ("The file could not be found.", "Please check the name and try again.")
LEFT_LINES = ((SENTENCES[0], 150, 300), (SENTENCES[1], 150, 500), ("Unrelated footer outside.", 150, 1100))
"""左屏上画的各行：（文字，左边 x，基线 y），物理像素。前两行在选区内，两行间距约两倍行高，合并成两段。"""
MAIN_LINES = (("Main screen text is not selected.", 100, 200),)

SELECTION = ((60.0, 140.0), (900.0, 380.0))
"""在左屏蒙版上拖动的起点与终点（窗口内逻辑坐标），即物理像素 (90, 210)–(1350, 570)。"""

_HAN = re.compile(r"[一-鿿]")


def _windows_ocr_languages() -> list[str]:
    try:
        return WinRtOcrApi().available_languages()
    except (ImportError, OSError):
        return []


pytestmark = [
    pytest.mark.skipif(not REAL_EN_ZH.is_file(), reason="本机没有 assets/argosmodel 下的 Argos 英中模型包"),
    pytest.mark.skipif(
        match_language("en", _windows_ocr_languages()) is None,
        reason="Windows 没有英文 OCR 语言（或缺少 pywinrt），Windows OCR 无法识别英文截图",
    ),
]


# ---------------------------------------------------------------- 屏幕替身


def _text_pixmap(width: int, height: int, lines: Sequence[tuple[str, int, int]]) -> QPixmap:
    """白底黑字的冻结画面（物理像素）。"""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    font = QFont(FONT_FAMILY)
    font.setPixelSize(TEXT_PX)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setFont(font)
        painter.setPen(QColor("black"))
        for text, x, baseline in lines:
            painter.drawText(x, baseline, text)
    finally:
        painter.end()
    return QPixmap.fromImage(image)


@dataclass
class TextScreens:
    """虚构的两屏布局，冻结画面是画好的英文图。每次冻结返回同样的画面。"""

    layout: Sequence[tuple[str, Rect, float]]
    lines: dict[str, Sequence[tuple[str, int, int]]]
    calls: int = 0
    pixmaps: dict[str, QPixmap] = field(default_factory=lambda: dict[str, QPixmap]())

    def __call__(self) -> list[FrozenScreen]:
        self.calls += 1
        out: list[FrozenScreen] = []
        for name, native, dpr in self.layout:
            pixmap = self.pixmaps.get(name)
            if pixmap is None:
                pixmap = _text_pixmap(native.width, native.height, self.lines.get(name, ()))
                self.pixmaps[name] = pixmap
            out.append(FrozenScreen(screen_from_native(name, native, dpr), pixmap))
        return out


# ---------------------------------------------------------------- 工具


class _Network:
    """假网络：记录全部请求，一律 404。启动与截屏翻译都不应发出请求。"""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        return httpx.Response(404)


async def _wait(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.02)
    except TimeoutError:
        raise AssertionError(f"等待超时（{timeout:g} 秒）：{what}") from None


def _words(text: str) -> str:
    """比较识别结果用：小写，去掉标点，空白归一。"""
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def _process_alive(pid: int) -> bool:
    """进程是否还在运行：OpenProcess 后等待 0 毫秒，未结束返回 WAIT_TIMEOUT。只查询，不影响进程。"""
    synchronize, query_limited, wait_timeout = 0x00100000, 0x1000, 0x102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(synchronize | query_limited, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def _dark_pixels(image: QImage, x0: int, y0: int, x1: int, y1: int) -> int:
    """矩形内接近黑色的像素数（每隔一个像素取样），用来判断这里画出了文字。"""
    count = 0
    for y in range(max(0, y0), min(image.height(), y1), 2):
        for x in range(max(0, x0), min(image.width(), x1), 2):
            color = image.pixelColor(x, y)
            if color.red() < 90 and color.green() < 90 and color.blue() < 90:
                count += 1
    return count


def _save_shot(widget: QWidget, name: str) -> Path | None:
    if not os.environ.get("LST_SCREENSHOT_SHOTS"):
        return None
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOTS_DIR / f"full-{name}.png"
    assert widget.grab().save(str(path)), path
    return path


def _write_config(paths: AppPaths) -> None:
    """预先写好的配置：Windows OCR（英文）、只有本地 NMT 一个引擎实例、截屏译成简体中文、不冲突的热键。"""
    paths.ensure()
    document: dict[str, Any] = {
        "ocr": {"engine": "windows", "language": "en"},
        "screenshot": {"target_lang": "zh-Hans"},
        "hotkeys": {"capture": "Ctrl+Alt+Shift+F20", "bilingual": "Ctrl+Alt+Shift+F21"},
        "engines": [{"kind": "local_nmt", "id": "argos", "name": "Argos", "priority": 1}],
    }
    paths.config_file.write_text(tomlkit.dumps(document), encoding="utf-8")
    # 首次运行引导已完成（QSettings 的 INI 格式），启动时不弹出引导窗口。
    paths.ui_state_file.write_text("[onboarding]\ncompleted=true\n", encoding="utf-8")


@dataclass
class Rig:
    """启动好的应用与测试替换进去的模块。"""

    app: Application
    screens: TextScreens
    capture: CaptureModule
    overlay: OverlayModule
    copied: list[str]
    captures: list[CaptureCompleted] = field(default_factory=lambda: list[CaptureCompleted]())
    results: list[OcrCompleted] = field(default_factory=lambda: list[OcrCompleted]())
    failures: list[OcrFailed] = field(default_factory=lambda: list[OcrFailed]())
    updates: list[SegmentsUpdated] = field(default_factory=lambda: list[SegmentsUpdated]())
    progress: list[SessionProgress] = field(default_factory=lambda: list[SessionProgress]())

    def masks(self) -> tuple[MaskWindow, ...]:
        controller = self.capture.controller
        return controller.masks if controller is not None else ()

    def mask(self, name: str) -> MaskWindow:
        return next(m for m in self.masks() if m.screen_geometry.name == name)

    async def open_mask(self) -> tuple[MaskWindow, ...]:
        """像按下截屏热键一样发出 StartCapture，等全部蒙版显示并画出第一帧。"""
        bus = self.app.bus
        assert bus is not None
        frozen_before = self.screens.calls
        bus.send(StartCapture())
        await bus.drain()

        def shown() -> bool:
            masks = self.masks()
            return len(masks) == 2 and all(m.isVisible() for m in masks)

        await _wait(shown, 5, "蒙版显示")
        masks = self.masks()
        for mask in masks:
            mask.repaint()
            assert mask.painted
            assert mask.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        assert self.screens.calls == frozen_before + 1
        return masks


# ---------------------------------------------------------------- 测试


@pytest.mark.timeout(300)
async def test_full_capture_translate_and_overlay_interaction(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not ensure_font():
        pytest.skip(f"没有用来画英文冻结画面的字体文件 {FONT_FILE}")
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    offscreen = QGuiApplication.platformName() == "offscreen"

    # ---- 替换屏幕、窗口显示方式与剪贴板，其余模块都由组合根按产品的方式创建
    screens = TextScreens([MAIN, LEFT], {"left": LEFT_LINES, "main": MAIN_LINES})
    copied: list[str] = []

    def clipboard(text: str) -> None:
        copied.append(text)
        if offscreen:
            system_clipboard(text)

    created: dict[str, Any] = {}

    def make_capture() -> CaptureModule:
        module = CaptureModule(freezer=screens, prepare_window=hide_from_desktop)
        created["capture"] = module
        return module

    def make_overlay() -> OverlayModule:
        module = OverlayModule(clipboard=clipboard, prepare_window=hide_from_desktop)
        created["overlay"] = module
        return module

    monkeypatch.setattr(bootstrap, "CaptureModule", make_capture)
    monkeypatch.setattr(bootstrap, "OverlayModule", make_overlay)

    paths = AppPaths(tmp_path / "data")
    _write_config(paths)
    network = _Network()
    app = Application(
        AppOptions(
            paths=paths,
            secrets=MemorySecretStore(),
            ocr_command="auto",
            tray_icon=None,
            updates_startup_delay=3600,
            http_transport=httpx.MockTransport(network.handle),
            models_index_url=INDEX_URL,
            dict_index_url=DICT_INDEX_URL,
        ),
        qapp,
    )
    pids: list[int] = []
    started = time.perf_counter()
    await app.start()
    try:
        bus, nmt, ocr = app.bus, app.nmt, app.ocr
        assert bus is not None and nmt is not None and ocr is not None
        rig = Rig(app, screens, created["capture"], created["overlay"], copied)
        bus.subscribe(CaptureCompleted, rig.captures.append)
        bus.subscribe(OcrCompleted, rig.results.append)
        bus.subscribe(OcrFailed, rig.failures.append)
        bus.subscribe(SegmentsUpdated, rig.updates.append)
        bus.subscribe(SessionProgress, rig.progress.append)

        # ---- 1. 启动：两个推理进程就绪，名册里只有本地 NMT 实例且支持英→中
        ocr_state = await asyncio.wait_for(ocr.wait_for_state(WorkerState.READY, WorkerState.FAILED), 90)
        assert ocr_state is WorkerState.READY, ocr.state_detail
        assert ocr.capabilities is not None and ocr.capabilities.get("engine") == "windows"
        assert ocr.capabilities.get("language") == "en"
        nmt_state = await asyncio.wait_for(nmt.wait_for_state(WorkerState.READY, WorkerState.FAILED), 120)
        assert nmt_state is WorkerState.READY, nmt.state_detail

        def roster_ready() -> bool:
            roster = bus.latest(EngineRoster)
            return roster is not None and any(
                e.info.id == EngineId("argos") and e.supports(Lang("en"), Lang("zh-Hans"))
                for e in roster.engines
            )

        await _wait(roster_ready, 60, "本地 NMT 实例进入引擎名册")
        roster = bus.latest(EngineRoster)
        assert roster is not None and [e.info.id for e in roster.engines] == [EngineId("argos")]
        assert app.onboarding is not None and app.onboarding.window is not None
        assert not app.onboarding.window.isVisible()
        pids = [pid for pid in (nmt.pid, ocr.pid) if pid is not None]
        assert len(pids) == 2
        startup_s = time.perf_counter() - started

        # ---- 2. 截屏：蒙版出现，在 150% 的左屏上拖出覆盖两行英文的选区
        await rig.open_mask()
        left = rig.mask("left")
        assert left.geometry().topLeft() == QPoint(-2560, -200)
        (sx, sy), (ex, ey) = SELECTION
        button = Qt.MouseButton.LeftButton
        mouse(left, QEvent.Type.MouseButtonPress, sx, sy, button)
        mouse(left, QEvent.Type.MouseMove, (sx + ex) / 2, (sy + ey) / 2, button)
        mouse(left, QEvent.Type.MouseMove, ex, ey, button)
        selection = left.selection
        assert selection is not None
        assert selection.screen.name == "left"
        assert selection.pixels.as_tuple() == (90, 210, 1260, 360)
        expected_pixels = to_raw_image(left.frozen, selection.pixels)
        left.repaint()
        mask_shot = _save_shot(left, "mask-selection")
        capture_started = time.perf_counter()
        mouse(left, QEvent.Type.MouseButtonRelease, ex, ey, button)
        await bus.drain()

        # ---- 3. CaptureCompleted：蒙版关闭，截图就是冻结画面里选区的物理像素
        assert len(rig.captures) == 1
        event = rig.captures[0]
        assert all(gone(m) for m in (left, *rig.masks())) and not rig.masks()
        assert event.geometry.screen_name == "left"
        assert event.geometry.dpr == 1.5
        assert event.geometry.selection == (-2500, -60, 840, 240)
        assert (event.image.width, event.image.height) == (1260, 360)
        assert event.image.data == expected_pixels.data

        # ---- 4. OcrCompleted：Windows OCR 识别出选区内的两行英文，各成一段；选区外的文字不在其中
        await _wait(lambda: bool(rig.results) or bool(rig.failures), 60, "OCR 结果")
        assert not rig.failures, rig.failures
        [result] = rig.results
        assert result.session_id == event.session_id
        recognized = [s.segment.text for s in result.segments]
        assert [_words(t) for t in recognized] == [_words(s) for s in SENTENCES], recognized
        for segment in result.segments:
            assert 0 <= segment.bbox.x0 < segment.bbox.x1 <= 1260
            assert 0 <= segment.bbox.y0 < segment.bbox.y1 <= 360
        ocr_s = time.perf_counter() - capture_started

        # ---- 5. SegmentsUpdated：本地 NMT 给出含汉字的译文，两段都已定
        def finished_views() -> dict[str, str]:
            views: dict[str, str] = {}
            for update in rig.updates:
                if update.session_id != event.session_id:
                    continue
                for view in update.views:
                    if view.finished and view.shown_engine == EngineId("argos"):
                        views[view.segment_id] = view.display_text
            return views

        segment_ids = [s.segment.id for s in result.segments]
        await _wait(lambda: set(finished_views()) == set(segment_ids), 60, "两段译文")
        translations = [finished_views()[sid] for sid in segment_ids]
        assert all(_HAN.search(t) for t in translations), translations
        translate_s = time.perf_counter() - capture_started

        # ---- 6. 覆盖窗口：在选区原位置弹出，进入结果阶段，画出译文
        window = rig.overlay.window
        assert window is not None and isinstance(window, OverlayWindow)
        assert window.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        assert window.isVisible()
        assert (window.x(), window.y(), window.width(), window.height()) == (-2500, -60, 840, 240)
        await _wait(lambda: window.progress == (2, 2), 10, "覆盖窗口进度 2/2")
        assert window.phase is Phase.RESULT
        assert window.status_text == ""
        assert [window.shown_text(i) for i in window.visible_items()] == translations
        window.fit_all_now()
        await _wait(lambda: not window.fitting_pending, 5, "字号适配完成")
        await asyncio.sleep(0.4)  # 等 150 毫秒的淡入动画结束
        assert all(i.fade_from is None for i in window.visible_items())
        image = window.grab().toImage()
        scale = image.width() / 1260
        for item in window.visible_items():
            box = item.bbox
            dark = _dark_pixels(
                image,
                round(box.x0 * scale),
                round(box.y0 * scale),
                round(box.x1 * scale),
                round(box.y1 * scale),
            )
            assert dark > 20, (item.display, dark)
        overlay_shot = _save_shot(window, "overlay-result")
        layout = [(i.bbox, window.font_px(i)) for i in window.visible_items()]

        # ---- 7. 覆盖窗口上的交互（OV-05）
        first, second = window.visible_items()
        s = window.scale_logical()
        ctrl = Qt.KeyboardModifier.ControlModifier

        def edge(item: OverlaySegment, index: int, *, after: bool = False) -> tuple[float, float]:
            """显示文字里第 index 个字符的左边缘（after 为真时是右边缘）往字符里收 1 个设备像素的点，
            取那一行的竖直中点，窗口逻辑坐标。位置由覆盖窗口的排版结果算出，与画出来的字形一致。"""
            window.select_range((item.segment_id, index), (item.segment_id, index + 1))
            rect = window.selection_rects(item.segment_id)[0]
            window.clear_selection()
            ratio = window.device_ratio()
            x = rect.right() - 1 if after else rect.left() + 1
            return (x / ratio, rect.center().y() / ratio)

        # 没有选中时 Ctrl+C 什么也不做。
        QTest.keyClick(window, Qt.Key.Key_C, ctrl)
        assert copied == []
        # 左键在第一段里从第 2 个字拖到第 4 个字的右边：Ctrl+C 复制的正好是这三个字。
        drag(window, edge(first, 1), edge(first, 3, after=True))
        assert window.selected_text() == translations[0][1:4]
        QTest.keyClick(window, Qt.Key.Key_C, ctrl)
        assert copied == [translations[0][1:4]]
        if offscreen:
            assert QGuiApplication.clipboard().text() == translations[0][1:4]
        # 从第一段的第 3 个字拖到第二段第 2 个字的右边：第一段后半、换行、第二段前半。
        drag(window, edge(first, 2), edge(second, 1, after=True))
        crossing = translations[0][2:] + "\n" + translations[1][:2]
        assert window.selected_text() == crossing
        QTest.keyClick(window, Qt.Key.Key_C, ctrl)
        assert copied[-1] == crossing
        if offscreen:
            assert QGuiApplication.clipboard().text() == crossing
        selection_shot = _save_shot(window, "overlay-selection")
        # 左键单击清除选择，之后 Ctrl+C 不写剪贴板。
        point = (second.bbox.x0 * s + 10, (second.bbox.y0 + second.bbox.y1) / 2 * s)
        mouse(window, QEvent.Type.MouseButtonPress, *point, Qt.MouseButton.LeftButton)
        mouse(window, QEvent.Type.MouseButtonRelease, *point, Qt.MouseButton.LeftButton)
        assert window.selected == frozenset()
        count = len(copied)
        QTest.keyClick(window, Qt.Key.Key_C, ctrl)
        assert len(copied) == count
        if offscreen:
            assert QGuiApplication.clipboard().text() == crossing
        # 右键单击切换原文与译文；切到原文后选中、复制的是原文。
        mouse(window, QEvent.Type.MouseButtonPress, *point, Qt.MouseButton.RightButton)
        mouse(window, QEvent.Type.MouseButtonRelease, *point, Qt.MouseButton.RightButton)
        assert window.showing_source
        assert [window.shown_text(i) for i in window.visible_items()] == recognized
        window.fit_all_now()
        drag(window, edge(first, 4), edge(first, 7, after=True))
        assert window.selected_text() == recognized[0][4:8]
        QTest.keyClick(window, Qt.Key.Key_C, ctrl)
        assert copied[-1] == recognized[0][4:8]
        mouse(window, QEvent.Type.MouseButtonPress, *point, Qt.MouseButton.RightButton)
        mouse(window, QEvent.Type.MouseButtonRelease, *point, Qt.MouseButton.RightButton)
        assert not window.showing_source
        # Esc 关闭覆盖窗口，展示器放下这个窗口。
        QTest.keyClick(window, Qt.Key.Key_Escape)
        await bus.drain()
        assert gone(window)
        assert rig.overlay.window is None

        # ---- 8. 取消截屏：右键、Esc、过小的选区，都不发布 CaptureCompleted，也不弹出覆盖窗口
        for how in ("right-click", "escape", "tiny"):
            masks = await rig.open_mask()
            target = rig.mask("left")
            if how == "right-click":
                mouse(target, QEvent.Type.MouseButtonPress, 300, 300, Qt.MouseButton.RightButton)
                mouse(target, QEvent.Type.MouseButtonRelease, 300, 300, Qt.MouseButton.RightButton)
            elif how == "escape":
                QTest.keyClick(target, Qt.Key.Key_Escape)
            else:
                drag(target, (300, 300), (302, 340))
            await bus.drain()
            await asyncio.sleep(0.1)
            await bus.drain()
            assert all(gone(m) for m in masks), how
            assert not rig.masks(), how
            assert len(rig.captures) == 1, how
            assert rig.overlay.window is None, how

        # ---- 9. 整个过程没有网络请求，桌面上没有出现任何窗口
        assert network.requests == []
        visible = [
            w
            for w in QApplication.topLevelWidgets()
            if w.isVisible() and not w.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        ]
        assert visible == [], [type(w).__name__ for w in visible]
        sys.stderr.write(
            f"\n[full-interaction] 平台 {QGuiApplication.platformName()}，启动到就绪 {startup_s:.1f} 秒，"
            f"截屏到识别完成 {ocr_s:.2f} 秒，到译文全部已定 {translate_s:.2f} 秒\n"
            f"[full-interaction] 识别：{recognized}\n[full-interaction] 译文：{translations}\n"
            f"[full-interaction] 段的包围盒与字号（设备像素）：{layout}\n"
            + (
                f"[full-interaction] 截图：{mask_shot}、{overlay_shot}、{selection_shot}\n"
                if mask_shot
                else ""
            )
        )
    finally:
        await app.stop()
        loop.set_exception_handler(None)

    # ---- 10. 清理：两个推理进程都已退出
    assert app.nmt is not None and app.nmt.pid is None
    assert app.ocr is not None and app.ocr.pid is None
    deadline = time.monotonic() + 10
    while any(_process_alive(pid) for pid in pids) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert not any(_process_alive(pid) for pid in pids), pids
    assert not loop_errors, [c.get("exception") or c.get("message") for c in loop_errors]
