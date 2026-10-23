"""覆盖窗口：状态机、会话过滤、增量渲染、操作与跨屏重新布局（OV-01 至 OV-07、CAP-06、OCR-07、OCR-08）。"""

import math

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from lst.contracts.errors import ErrorCode
from lst.contracts.events import WorkerStatus, WorkerStatuses
from lst.contracts.ids import SegmentId, SessionId
from lst.contracts.ocr import BBox, BlockCategory, LayoutBlock, OcrCompleted, OcrFailed
from lst.contracts.ports import WorkerState
from lst.contracts.translation import SegmentView, SkipReason
from lst.ui.overlay import Phase
from lst.ui.overlay.window import OverlayWindow
from tests.ui.conftest import wait_until
from tests.ui.overlay.conftest import (
    HEIGHT,
    WIDTH,
    MakeRig,
    Rig,
    capture,
    gone,
    hide_from_desktop,
    layout_result,
    mouse,
    raw_image,
    segment,
    text_result,
    view,
)
from tests.workers.ocr.screens import FONT_FAMILY, ensure_font

P1, P2, P3 = SegmentId("p1"), SegmentId("p2"), SegmentId("p3")


async def _shown(make_rig: MakeRig, session: str = "s1", **kwargs: object) -> Rig:
    rig = await make_rig()
    await rig.publish(capture(session, **kwargs))  # pyright: ignore[reportArgumentType]
    return rig


async def _with_text(make_rig: MakeRig, session: str = "s1") -> Rig:
    rig = await _shown(make_rig, session)
    await rig.publish(text_result(session))
    rig.window.fit_all_now()
    return rig


# ---------------------------------------------------------------- 弹出与识别中（OV-01、OV-02、OCR-07）


async def test_window_pops_up_at_selection_with_selection_size(make_rig: MakeRig) -> None:
    rig = await _shown(make_rig)
    window = rig.window
    assert window.isVisible()
    assert window.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    flags = window.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    # 600×300 物理像素、150%：400×200 逻辑像素，左上角在选区的位置。
    assert window.geometry() == QRect(100, 80, 400, 200)
    assert window.phase is Phase.RECOGNIZING
    assert window.status_text == "识别中…"


async def test_text_color_follows_background_brightness(make_rig: MakeRig) -> None:
    rig = await _shown(make_rig, light=True)
    assert rig.window.light_background
    await rig.publish(capture("s2", light=False))
    assert not rig.window.light_background


@pytest.mark.parametrize("dpr", [1.0, 1.5, 2.0])
async def test_symbols_and_numbers_keep_original_pixels(make_rig: MakeRig, dpr: float) -> None:
    rig = await _shown(make_rig, dpr=dpr)
    box = BBox(20, 20, 120, 60)
    block = LayoutBlock("0", BlockCategory.TEXT, box, 0, 1.0, text="99")
    await rig.publish(OcrCompleted(SessionId("s1"), (block,), (segment("p1", "99", box),)))
    rig.window.apply_views([SegmentView(P1, "99", "99", None, True, (), SkipReason.NO_TEXT)])
    rig.window.fit_all_now()
    shot = rig.window.grab().toImage()
    # 截图坐标按窗口当前屏幕的实际缩放转换到 grab() 的设备像素。
    position = round(25 * rig.window.scale_device())
    assert shot.pixelColor(position, position).red() == 30


async def test_loading_model_status(make_rig: MakeRig) -> None:
    rig = await make_rig()
    await rig.publish(WorkerStatuses({"ocr": WorkerStatus(WorkerState.LOADING)}))
    await rig.publish(capture("s1"))
    assert rig.window.status_text == "识别模型加载中…"
    await rig.publish(WorkerStatuses({"ocr": WorkerStatus(WorkerState.READY)}))
    assert rig.window.status_text == "识别中…"


# ---------------------------------------------------------------- 结果与增量更新（OV-03、OV-04、OV-07）


async def test_result_renders_source_first_then_translations(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    assert window.phase is Phase.RESULT
    assert list(window.order) == [P1, P2, P3]
    assert [window.shown_text(i) for i in window.visible_items()] == [
        "Hello world",
        "The quick brown fox jumps over the lazy dog.",
        "Short",
    ]
    assert window.status_text == ""

    await rig.updates(
        "s1",
        view("p1", "Hello world", "你好世界"),
        view("p2", "The quick brown fox jumps over the lazy dog."),
    )
    assert window.items[P1].display == "你好世界"
    assert window.items[P1].fade_from == "Hello world"
    # p2 还在翻译中，显示原文，不播放过渡。
    assert window.items[P2].fade_from is None
    await wait_until(lambda: window.items[P1].fade_from is None, timeout=2)

    await rig.progress("s1", 1, 3)
    assert window.progress == (1, 3)


async def test_only_changed_segments_fade(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    changed = window.apply_views([view("p1", "Hello world", "你好"), view("p3", "Short", None)])
    assert changed == [P1]
    changed = window.apply_views([view("p1", "Hello world", "你好")])
    assert changed == []


async def test_font_size_fits_bbox_and_is_measured_in_frames(make_rig: MakeRig) -> None:
    rig = await _shown(make_rig)
    await rig.publish(text_result("s1"))
    window = rig.window
    # 分帧测量：显示结果时先用估算值，计时器随后测完。
    await wait_until(lambda: not window.fitting_pending)
    p1, p3 = window.items[P1], window.items[P3]
    assert p1.font_px is not None and p3.font_px is not None
    # 字号不超过包围盒高度（设备像素是截图像素的 k 倍）。单行正文的包围盒已按原文字号扩到行高，
    # 所以按扩展后的 bbox 断言，不用 OCR 给出的 40。
    k = window.scale_device()
    assert p1.font_px <= p1.bbox.height * k
    assert p1.font_px >= 10


async def test_translation_font_uses_original_font_target(make_rig: MakeRig) -> None:
    """短译文使用原文字号目标；扩大绘制框不会再次放大原文字号估算。"""
    rig = await _with_text(make_rig)
    window = rig.window
    p2 = window.items[P2]
    source_px = window.font_px(p2)
    window.apply_views([view("p2", p2.source, "狐狸")])
    window.fit_all_now()
    _shown_text, _source, width, height, _outer_w, _outer_h, _title, cap = window._fit_key(p2)  # pyright: ignore[reportPrivateUsage]
    measure = window._measure  # pyright: ignore[reportPrivateUsage]
    free = measure("狐狸", width, height, False, height)
    # 两个字单独放进包围盒能用的字号远大于原文的字号；上限生效后取原文在整个包围盒里的字号。
    assert free > cap
    assert window.font_px(p2) == cap
    assert source_px <= cap
    # 换回原文时用原文自己的字号。
    window.toggle_source()
    window.fit_all_now()
    assert window.font_px(p2) == source_px


async def test_text_area_keeps_padding_inside_bbox(make_rig: MakeRig) -> None:
    """文字区域在包围盒内留出内边距，文字不贴在边线上。"""
    rig = await _with_text(make_rig)
    window = rig.window
    item = window.items[P1]
    k = window.scale_device()
    inner = window._inner_device(item)  # pyright: ignore[reportPrivateUsage]
    assert inner.x - item.bbox.x0 * k == pytest.approx(2 * k)
    assert item.bbox.x1 * k - inner.right == pytest.approx(2 * k)
    assert inner.y - item.bbox.y0 * k == pytest.approx(k)


async def test_results_before_ocr_completed_are_applied_later(make_rig: MakeRig) -> None:
    rig = await _shown(make_rig)
    await rig.updates("s1", view("p1", "Hello world", "你好世界"))
    await rig.progress("s1", 1, 3)
    await rig.publish(text_result("s1"))
    assert rig.window.items[P1].display == "你好世界"
    assert rig.window.progress == (1, 3)


async def test_layout_blocks_table_and_chart(make_rig: MakeRig) -> None:
    rig = await _shown(make_rig)
    await rig.publish(layout_result("s1"))
    window = rig.window
    window.fit_all_now()
    visible = [i.segment_id for i in window.visible_items()]
    # 图表数据表的段在点开图表之前不显示。
    assert visible == ["b0", "b1-r0c0", "b1-r0c1", "b1-r1c0", "b1-r1c1"]
    assert window.items[SegmentId("b0")].title
    # 单击图表（截图坐标 (450, 230)）展开数据表；数据表按 2×2 均分图表块。
    s = window.scale_logical()
    point = (450 * s, 230 * s)
    mouse(window, QEvent.Type.MouseButtonPress, *point, Qt.MouseButton.LeftButton)
    mouse(window, QEvent.Type.MouseButtonRelease, *point, Qt.MouseButton.LeftButton)
    assert window.expanded_charts == {"4"}
    chart_items = [i for i in window.visible_items() if i.chart == "4"]
    assert len(chart_items) == 4
    assert window.items[SegmentId("b4-r1c1")].bbox.x0 == 450
    assert window.items[SegmentId("b4-r1c1")].bbox.y0 == 230
    image = window.grab().toImage()
    assert not image.isNull()


# ---------------------------------------------------------------- 会话过滤（CAP-06）


async def test_old_session_results_are_dropped(make_rig: MakeRig) -> None:
    rig = await _shown(make_rig, "old")
    old_window = rig.window
    await rig.publish(capture("new"))
    new_window = rig.window
    assert new_window is not old_window
    assert gone(old_window)
    await rig.publish(text_result("old"))
    assert new_window.phase is Phase.RECOGNIZING
    await rig.publish(OcrFailed(SessionId("old"), ErrorCode.TIMEOUT, "late"))
    assert new_window.phase is Phase.RECOGNIZING
    await rig.publish(text_result("new"))
    await rig.updates("old", view("p1", "Hello world", "旧译文"))
    await rig.progress("old", 3, 3)
    assert new_window.items[P1].display == "Hello world"
    assert new_window.progress is None


async def test_messages_after_window_closed_are_ignored(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    QTest.keyClick(window, Qt.Key.Key_Escape)
    await rig.bus.drain()
    assert rig.module.window is None
    await rig.updates("s1", view("p1", "Hello world", "你好"))
    assert rig.module.window is None


# ---------------------------------------------------------------- 失败与重试（OCR-08）


async def test_failure_shows_reason_and_retry_sends_command(make_rig: MakeRig) -> None:
    rig = await _shown(make_rig)
    await rig.publish(OcrFailed(SessionId("s1"), ErrorCode.WORKER_CRASHED, "detail"))
    window = rig.window
    assert window.phase is Phase.FAILED
    assert window.status_text.startswith("识别失败：")
    assert window.detail_text
    assert window.retry_button.isVisible()
    QTest.mouseClick(window.retry_button, Qt.MouseButton.LeftButton)
    await rig.bus.drain()
    assert [r.session_id for r in rig.retries] == ["s1"]
    assert window.phase is Phase.RECOGNIZING
    assert not window.retry_button.isVisible()
    await rig.publish(text_result("s1"))
    assert window.phase is Phase.RESULT


async def test_retry_only_from_failed_state(make_rig: MakeRig) -> None:
    rig = await _shown(make_rig)
    presenter = rig.module.presenter
    assert presenter is not None
    presenter.retry()
    await rig.bus.drain()
    assert rig.retries == []


# ---------------------------------------------------------------- 操作（OV-05）


async def test_right_click_toggles_source_and_translation(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    window.apply_views([view("p1", "Hello world", "你好世界")])
    right = Qt.MouseButton.RightButton
    mouse(window, QEvent.Type.MouseButtonPress, 50, 20, right)
    mouse(window, QEvent.Type.MouseMove, 52, 22, right)
    mouse(window, QEvent.Type.MouseButtonRelease, 52, 22, right)
    assert window.showing_source
    assert window.shown_text(window.items[P1]) == "Hello world"
    mouse(window, QEvent.Type.MouseButtonPress, 50, 20, right)
    mouse(window, QEvent.Type.MouseButtonRelease, 50, 20, right)
    assert not window.showing_source
    assert window.shown_text(window.items[P1]) == "你好世界"


async def test_right_drag_moves_window_without_toggling(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    start = window.pos()
    right = Qt.MouseButton.RightButton
    origin = QPointF(start.x() + 50, start.y() + 20)
    mouse(window, QEvent.Type.MouseButtonPress, 50, 20, right, global_pos=origin)
    mouse(window, QEvent.Type.MouseMove, 50, 20, right, global_pos=origin + QPointF(30, 0))
    mouse(window, QEvent.Type.MouseMove, 50, 20, right, global_pos=origin + QPointF(120, 45))
    mouse(window, QEvent.Type.MouseButtonRelease, 50, 20, right, global_pos=origin + QPointF(120, 45))
    assert window.pos() == start + QPoint(120, 45)
    assert not window.showing_source


async def test_wheel_zooms_about_cursor(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    before = window.geometry()
    cursor = QPointF(100, 50)
    event = QWheelEvent(
        cursor,
        window.mapToGlobal(cursor),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(window, event)
    assert window.zoom == pytest.approx(1.1)
    after = window.geometry()
    assert after.width() == round(600 * 1.1 / 1.5)
    assert after.height() == round(300 * 1.1 / 1.5)
    # 光标下的点保持在原处：新左上角 = 旧左上角 + c − c × 1.1。
    assert after.x() == round(before.x() + 100 - 110)
    assert after.y() == round(before.y() + 50 - 55)


async def test_middle_click_pins_and_focus_loss_closes_unpinned(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    middle = Qt.MouseButton.MiddleButton
    mouse(window, QEvent.Type.MouseButtonPress, 10, 10, middle)
    mouse(window, QEvent.Type.MouseButtonRelease, 10, 10, middle)
    assert window.pinned
    window.focus_lost()
    assert window.isVisible()
    mouse(window, QEvent.Type.MouseButtonPress, 10, 10, middle)
    assert not window.pinned
    window.focus_lost()
    await rig.bus.drain()
    assert gone(window)
    assert rig.module.window is None


async def test_escape_closes(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    QTest.keyClick(window, Qt.Key.Key_Escape)
    await rig.bus.drain()
    assert gone(window)


async def test_ctrl_c_copies_only_the_selected_text(make_rig: MakeRig) -> None:
    """没有选中时 Ctrl+C 什么也不做；拖选后复制选中的文字。逐字选择的细节见 test_selection.py。"""
    rig = await _with_text(make_rig)
    window = rig.window
    window.apply_views([view("p1", "Hello world", "你好世界"), view("p2", "The quick…", "敏捷的狐狸")])
    QTest.keyClick(window, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert rig.copied == []
    # 左键从 p2 的左边缘拖到右边缘（截图坐标 y 100–180）：选中 p2 的全部文字。
    s = window.scale_logical()
    left = Qt.MouseButton.LeftButton
    mouse(window, QEvent.Type.MouseButtonPress, 21 * s, 140 * s, left)
    mouse(window, QEvent.Type.MouseMove, 300 * s, 140 * s, left)
    mouse(window, QEvent.Type.MouseMove, 575 * s, 140 * s, left)
    mouse(window, QEvent.Type.MouseButtonRelease, 575 * s, 140 * s, left)
    assert window.selected == {P2}
    QTest.keyClick(window, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert rig.copied == ["敏捷的狐狸"]
    # 单击清除选择，之后 Ctrl+C 不再写剪贴板。
    mouse(window, QEvent.Type.MouseButtonPress, 5, 5, left)
    mouse(window, QEvent.Type.MouseButtonRelease, 5, 5, left)
    assert window.selected == frozenset()
    QTest.keyClick(window, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert rig.copied == ["敏捷的狐狸"]


# ---------------------------------------------------------------- 跨屏重新布局（CAP-07、UI-04）


async def test_moving_to_screen_with_other_scale_relayouts(
    make_rig: MakeRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    size = window.size()
    ratio = window.device_ratio()
    before = window.font_px(window.items[P2])
    k_before = window.scale_device()

    monkeypatch.setattr(window, "device_ratio", lambda: ratio * 2)
    QApplication.sendEvent(window, QEvent(QEvent.Type.DevicePixelRatioChange))
    assert window.scale_device() == pytest.approx(k_before * 2)
    window.fit_all_now()
    after = window.font_px(window.items[P2])
    # 字号按新的设备像素重新适配：设备像素翻倍，字号约翻倍（整数字号与换行位置会带来少量偏差）。
    assert after >= before * 1.7
    assert window.size() == size


async def test_no_text_result(make_rig: MakeRig) -> None:
    rig = await _shown(make_rig)
    await rig.publish(OcrCompleted(SessionId("s1"), (), ()))
    assert rig.window.phase is Phase.RESULT
    assert rig.window.status_text == "没有识别到文字"


def test_window_without_presenter_can_be_built() -> None:
    """视图可以单独构造（截图自查脚本用）：给出截图与几何即可。"""

    class Actions:
        def retry(self) -> None: ...
        def copy(self, text: str) -> None: ...
        def window_closed(self, window: OverlayWindow) -> None: ...

    window = OverlayWindow(raw_image(), capture("x").geometry, Actions())
    hide_from_desktop(window)
    assert window.size().width() == 400
    window.deleteLater()


async def test_lines_of_the_same_font_get_the_same_translation_size(make_rig: MakeRig) -> None:
    """OCR 的行框紧贴字形：同字号的两行，没有下伸字母的那行框更矮。扩到行高后译文字号一致。

    完整截屏测试里两行 48 像素的英文原来分别得到 17 与 22 设备像素的译文字号（OV-03）。
    """
    rig = await _shown(make_rig)
    window = rig.window
    if not ensure_font():
        pytest.skip("没有微软雅黑：offscreen 平台的替代字体里各字形一样高，看不出下伸字母的差别")
    window._base_font = QFont(FONT_FAMILY)  # pyright: ignore[reportPrivateUsage]
    window._metrics.clear()  # pyright: ignore[reportPrivateUsage]
    metrics = QFontMetricsF(window._font(18, False))  # pyright: ignore[reportPrivateUsage]
    texts = ("The file could not be found.", "Please check the name and try again.")
    boxes: list[BBox] = []
    for row, text in enumerate(texts):
        ink = metrics.tightBoundingRect(text)
        top, right = 20 + row * 120, 20 + math.ceil(metrics.horizontalAdvance(text))
        boxes.append(BBox(20, top, right, top + math.ceil(ink.height())))
    assert boxes[1].height > boxes[0].height * 1.15  # 下伸字母让第二行的框明显更高
    block = LayoutBlock("0", BlockCategory.TEXT, BBox(0, 0, WIDTH, HEIGHT), 0, 1.0)
    segments = (segment("a", texts[0], boxes[0]), segment("b", texts[1], boxes[1]))
    await rig.publish(OcrCompleted(SessionId("s1"), (block,), segments))
    window.apply_views([view("a", texts[0], "无法找到文件。"), view("b", texts[1], "请检查名称后再试。")])
    window.fit_all_now()
    first, second = (window.font_px(window.items[SegmentId(sid)]) for sid in ("a", "b"))
    assert abs(first - second) <= 1, (first, second)
    # 扩展后的范围互不重叠，也没有越出截图。
    a, b = window.items[SegmentId("a")].bbox, window.items[SegmentId("b")].bbox
    assert a.y1 <= b.y0 and a.y0 >= 0 and b.y1 <= HEIGHT
