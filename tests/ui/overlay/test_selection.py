"""覆盖窗口的逐字选择（需求 OV-05，架构文档 11.5 节）：命中测试与绘制共用排版，选中底色落在对应字形上；
跨段按阅读顺序选择；双击取词、三击取段；单击、切换原文清除选择；缩放后选择仍与字形对齐；标准复制键复制选中的文字。

字形位置用截图里的像素核对：浅色底上的文字是黑色，段底与选中底色都很浅，暗像素只来自字形。
"""

from itertools import pairwise

import pytest
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QFont, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from lst.contracts.ids import SegmentId
from lst.ui.overlay.window import OverlayWindow, word_span
from tests.ui.overlay.conftest import MakeRig, Rig, capture, mouse, text_result, view
from tests.workers.ocr.screens import FONT_FAMILY, ensure_font

P1, P2, P3 = SegmentId("p1"), SegmentId("p2"), SegmentId("p3")
LEFT = Qt.MouseButton.LeftButton
CTRL = Qt.KeyboardModifier.ControlModifier
INK = 128
"""亮度低于它的像素算字形。"""


async def _with_text(make_rig: MakeRig) -> Rig:
    rig = await make_rig()
    await rig.publish(capture("s1"), text_result("s1"))
    window = rig.window
    if ensure_font():
        # 固定字体，字形之间的空隙不随系统字体变化。
        window._base_font = QFont(FONT_FAMILY)  # pyright: ignore[reportPrivateUsage]
        window._metrics.clear()  # pyright: ignore[reportPrivateUsage]
        window.relayout()
    window.fit_all_now()
    return rig


def drag(window: OverlayWindow, start: QPointF, *points: QPointF) -> None:
    mouse(window, QEvent.Type.MouseButtonPress, start.x(), start.y(), LEFT)
    for point in points:
        mouse(window, QEvent.Type.MouseMove, point.x(), point.y(), LEFT)
    end = points[-1] if points else start
    mouse(window, QEvent.Type.MouseButtonRelease, end.x(), end.y(), LEFT)


def double_click(window: OverlayWindow, point: QPointF) -> None:
    mouse(window, QEvent.Type.MouseButtonPress, point.x(), point.y(), LEFT)
    mouse(window, QEvent.Type.MouseButtonRelease, point.x(), point.y(), LEFT)
    mouse(window, QEvent.Type.MouseButtonDblClick, point.x(), point.y(), LEFT)
    mouse(window, QEvent.Type.MouseButtonRelease, point.x(), point.y(), LEFT)


def segment_rect(window: OverlayWindow, sid: SegmentId) -> QRectF:
    """段的包围盒，窗口设备像素坐标（与 grab 出来的图像像素一致）。"""
    box = window.items[sid].bbox
    k = window.scale_device()
    return QRectF(box.x0 * k, box.y0 * k, box.width * k, box.height * k)


def ink_runs(image: QImage, area: QRectF) -> list[tuple[int, int]]:
    """area 里有字形像素的列，合并成连续的段 [起, 止]（含两端）。"""
    columns: list[int] = []
    for x in range(int(area.left()) + 1, int(area.right()) - 1):
        if any(
            image.pixelColor(x, y).lightness() < INK
            for y in range(int(area.top()) + 1, int(area.bottom()) - 1)
        ):
            columns.append(x)
    runs: list[tuple[int, int]] = []
    for x in columns:
        if runs and x == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], x)
        else:
            runs.append((x, x))
    return runs


def widest_gap(runs: list[tuple[int, int]]) -> tuple[int, int]:
    """相邻两段字形之间最宽的空隙 (左段的止, 右段的起)：单行英文里它是单词之间的空格。"""
    return max(((a[1], b[0]) for a, b in pairwise(runs)), key=lambda g: g[1] - g[0])


def logical(window: OverlayWindow, x: float, y: float) -> QPointF:
    ratio = window.device_ratio()
    return QPointF(x / ratio, y / ratio)


def rect_center(window: OverlayWindow, rect: QRectF) -> QPointF:
    return logical(window, rect.center().x(), rect.center().y())


def check_world_on_glyphs(window: OverlayWindow) -> None:
    """p1 显示 Hello world、选中了 world：底色矩形从空格与 w 之间开始，盖住 world 的全部字形，
    画出来的底色只在这个矩形里。"""
    window.clear_selection()
    plain = window.grab().toImage()
    area = segment_rect(window, P1)
    runs = ink_runs(plain, area)
    gap_left, gap_right = widest_gap(runs)
    window.select_range((P1, 6), (P1, 11))
    assert window.selected_text() == "world"
    [rect] = window.selection_rects(P1)
    # 选区左边在空格里，最多压到 w 的第一列字形上一个多像素（字形边缘的抗锯齿像素）；
    # 右边不早于 d 的最后一列字形。
    assert gap_left < rect.left() <= gap_right + 1.5, (gap_left, rect.left(), gap_right)
    last_ink = runs[-1][1]
    assert last_ink - 0.5 <= rect.right() <= last_ink + 0.25 * rect.height(), (rect.right(), last_ink)
    # 画出来的底色：矩形里不是字形的像素被染色，矩形外（Hello 与空格的左半）不变。
    selected = window.grab().toImage()
    y = int(rect.top() + 2)
    inside = [x for x in range(int(rect.left()) + 1, int(rect.right()) - 1)]
    tinted = [x for x in inside if selected.pixelColor(x, y) != plain.pixelColor(x, y)]
    assert len(tinted) >= len(inside) * 0.8
    for x in range(int(area.left()) + 2, int(rect.left()) - 1):
        assert selected.pixelColor(x, y) == plain.pixelColor(x, y), x


# ---------------------------------------------------------------- 命中测试与绘制一致


async def test_drag_selects_the_characters_under_the_mouse(make_rig: MakeRig) -> None:
    """从 w 的第一列字形拖到段的右端：选中 world，选中底色的矩形落在 world 的字形上。"""
    rig = await _with_text(make_rig)
    window = rig.window
    plain = window.grab().toImage()
    area = segment_rect(window, P1)
    runs = ink_runs(plain, area)
    _gap_left, gap_right = widest_gap(runs)
    rows = [
        y
        for y in range(int(area.top()), int(area.bottom()))
        if any(plain.pixelColor(x, y).lightness() < INK for x in range(runs[0][0], runs[-1][1] + 1))
    ]
    middle = (rows[0] + rows[-1]) / 2
    start = logical(window, gap_right + 1, middle)
    drag(window, start, logical(window, runs[-1][1] + 20, middle))
    assert window.selection_ranges() == {P1: (6, 11)}
    assert window.selected_text() == "world"
    check_world_on_glyphs(window)


async def test_selection_is_drawn_per_character_not_per_segment(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    plain = window.grab().toImage()
    window.select_range((P2, 4), (P2, 9))
    assert window.selected_text() == "quick"
    selected = window.grab().toImage()
    [rect] = window.selection_rects(P2)
    area = segment_rect(window, P2)
    assert rect.width() < area.width() / 3, "只盖住 quick，不再整段填充"
    # 段内选区以外的地方（右端）不变。
    y = int(rect.center().y())
    for x in range(int(rect.right()) + 3, int(area.right()) - 2):
        assert selected.pixelColor(x, y) == plain.pixelColor(x, y), x


# ---------------------------------------------------------------- 跨段


async def test_drag_across_segments_follows_reading_order(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    p1, p2 = window.items[P1].source, window.items[P2].source
    window.select_range((P1, 6), (P1, 11))
    start = rect_center(window, window.selection_rects(P1)[0])  # world 的中间
    window.select_range((P2, 4), (P2, 9))
    end = rect_center(window, window.selection_rects(P2)[0])  # quick 的中间
    window.clear_selection()
    a = window.hit_test(start)
    b = window.hit_test(end)
    assert a is not None and b is not None and a[0] == P1 and b[0] == P2
    assert 6 < a[1] < 11 and 4 < b[1] < 9

    # 从第一段拖到第二段：第一段从起点到段尾，换行，第二段从段首到终点。
    drag(window, start, QPointF(start.x(), (start.y() + end.y()) / 2), end)
    assert window.selection_ranges() == {P1: (a[1], len(p1)), P2: (0, b[1])}
    QTest.keyClick(window, Qt.Key.Key_C, CTRL)
    assert rig.copied == [p1[a[1] :] + "\n" + p2[: b[1]]]

    # 反方向拖动选中同样的文字。
    drag(window, end, start)
    assert window.selected_text() == p1[a[1] :] + "\n" + p2[: b[1]]

    # 从第一段拖到第三段：中间的第二段整段选中。
    p3_point = rect_center(window, QRectF(segment_rect(window, P3)))
    drag(window, start, p3_point)
    ranges = window.selection_ranges()
    assert list(ranges) == [P1, P2, P3]
    assert ranges[P2] == (0, len(p2))


async def test_dragging_through_the_gap_between_segments_keeps_extending(make_rig: MakeRig) -> None:
    """鼠标不在任何段上时按离它最近的段命中：拖到两段之间的空白时选到 p1 的末尾，
    再拖到 p2 右边的空白（截图坐标 x 590，在 p2 的包围盒外）时延伸到 p2 第一行的末尾。"""
    rig = await _with_text(make_rig)
    window = rig.window
    s = window.scale_logical()
    p1_bottom = window.items[P1].bbox.y1
    p2_top = window.items[P2].bbox.y0
    start = QPointF(21 * s, 40 * s)
    gap = QPointF(300 * s, (p1_bottom + 1) * s)
    assert p1_bottom < p2_top
    drag(window, start, gap)
    assert window.selection_ranges() == {P1: (0, len("Hello world"))}
    drag(window, start, gap, QPointF(590 * s, 112 * s))
    ranges = window.selection_ranges()
    assert ranges[P1] == (0, len("Hello world"))
    assert ranges[P2][0] == 0 and ranges[P2][1] > len("The quick"), ranges


# ---------------------------------------------------------------- 双击、三击


async def test_double_click_selects_an_english_word(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    window.select_range((P2, 4), (P2, 9))
    point = rect_center(window, window.selection_rects(P2)[0])
    window.clear_selection()
    double_click(window, point)
    assert window.selection_unit == "word"
    assert window.selected_text() == "quick"
    QTest.keyClick(window, Qt.Key.Key_C, CTRL)
    assert rig.copied == ["quick"]


async def test_double_click_selects_a_chinese_word_by_text_boundaries(make_rig: MakeRig) -> None:
    """中文按 QTextBoundaryFinder 的词边界：这个规则没有词典，每个汉字各成一词，标点单独成词。"""
    rig = await _with_text(make_rig)
    window = rig.window
    window.apply_views([view("p1", "Hello world", "你好世界，再见")])
    window.fit_all_now()
    window.select_range((P1, 2), (P1, 3))
    point = rect_center(window, window.selection_rects(P1)[0])
    window.clear_selection()
    double_click(window, point)
    assert window.selected_text() == "世"
    assert word_span("你好世界，再见", 2) == (2, 3)


async def test_double_click_then_drag_extends_by_words(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    window.select_range((P2, 4), (P2, 9))
    quick = rect_center(window, window.selection_rects(P2)[0])
    window.select_range((P2, 16), (P2, 19))
    fox = rect_center(window, window.selection_rects(P2)[0])
    window.clear_selection()
    mouse(window, QEvent.Type.MouseButtonPress, quick.x(), quick.y(), LEFT)
    mouse(window, QEvent.Type.MouseButtonRelease, quick.x(), quick.y(), LEFT)
    mouse(window, QEvent.Type.MouseButtonDblClick, quick.x(), quick.y(), LEFT)
    mouse(window, QEvent.Type.MouseMove, fox.x(), fox.y(), LEFT)
    mouse(window, QEvent.Type.MouseButtonRelease, fox.x(), fox.y(), LEFT)
    assert window.selected_text() == "quick brown fox", "两端都落在词的边界上"


async def test_triple_click_selects_the_paragraph_and_a_click_clears(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    window.select_range((P2, 4), (P2, 9))
    point = rect_center(window, window.selection_rects(P2)[0])
    window.clear_selection()
    double_click(window, point)
    mouse(window, QEvent.Type.MouseButtonPress, point.x(), point.y(), LEFT)
    mouse(window, QEvent.Type.MouseButtonRelease, point.x(), point.y(), LEFT)
    assert window.selection_unit == "paragraph"
    assert window.selected_text() == window.items[P2].source
    # 再单击一次（第四次按下）是普通的单击：清除选择。
    mouse(window, QEvent.Type.MouseButtonPress, point.x(), point.y(), LEFT)
    mouse(window, QEvent.Type.MouseButtonRelease, point.x(), point.y(), LEFT)
    assert window.selected == frozenset()


async def test_triple_click_needs_the_same_place(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    window.select_range((P2, 4), (P2, 9))
    point = rect_center(window, window.selection_rects(P2)[0])
    window.clear_selection()
    double_click(window, point)
    far = QPointF(point.x() + 40, point.y())
    mouse(window, QEvent.Type.MouseButtonPress, far.x(), far.y(), LEFT)
    mouse(window, QEvent.Type.MouseButtonRelease, far.x(), far.y(), LEFT)
    assert window.selected == frozenset(), "离双击处太远：普通单击"


# ---------------------------------------------------------------- 清除选择


async def test_click_clears_and_ctrl_c_then_copies_nothing(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    window.select_range((P1, 0), (P1, 5))
    s = window.scale_logical()
    mouse(window, QEvent.Type.MouseButtonPress, 300 * s, 140 * s, LEFT)
    mouse(window, QEvent.Type.MouseButtonRelease, 300 * s, 140 * s, LEFT)
    assert window.selection_ranges() == {}
    QTest.keyClick(window, Qt.Key.Key_C, CTRL)
    QTest.keyClick(window, Qt.Key.Key_Insert, CTRL)
    assert rig.copied == []


async def test_toggling_source_clears_and_then_selects_the_source(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    window.apply_views([view("p1", "Hello world", "你好世界")])
    window.fit_all_now()
    window.select_range((P1, 0), (P1, 2))
    assert window.selected_text() == "你好"
    window.toggle_source()
    assert window.selection_ranges() == {}
    window.fit_all_now()
    window.select_range((P1, 6), (P1, 11))
    point = rect_center(window, window.selection_rects(P1)[0])
    window.clear_selection()
    double_click(window, point)
    assert window.selected_text() == "world"
    QTest.keyClick(window, Qt.Key.Key_C, CTRL)
    assert rig.copied == ["world"]


async def test_new_translation_inside_the_selection_clears_it(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    window.select_range((P1, 0), (P1, 5))
    window.apply_views([view("p3", "Short", "短")])
    assert window.selected_text() == "Hello", "选区以外的段换了译文：保留"
    window.apply_views([view("p1", "Hello world", "你好世界")])
    assert window.selection_ranges() == {}, "选区里的段换了译文：旧的字符位置不再有意义"


async def test_new_result_clears_the_selection(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    window.select_range((P1, 0), (P1, 5))
    await rig.publish(text_result("s1"))
    assert window.selection_ranges() == {}


# ---------------------------------------------------------------- 缩放与换屏


async def test_selection_stays_on_the_glyphs_after_zooming(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    window.select_range((P1, 6), (P1, 11))
    before = window.selection_rects(P1)[0]
    window.zoom_step(3)
    window.fit_all_now()
    assert window.selected_text() == "world", "选择按字符位置记录，缩放不改变选中的文字"
    after = window.selection_rects(P1)[0]
    assert after.width() > before.width() * 1.15, "排版随字号重新计算"
    check_world_on_glyphs(window)


async def test_selection_follows_a_device_ratio_change(
    make_rig: MakeRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    window.select_range((P2, 4), (P2, 9))
    ratio = window.device_ratio()
    before = window.selection_rects(P2)[0]
    monkeypatch.setattr(window, "device_ratio", lambda: ratio * 2)
    QApplication.sendEvent(window, QEvent(QEvent.Type.DevicePixelRatioChange))
    window.fit_all_now()
    after = window.selection_rects(P2)[0]
    assert after.left() == pytest.approx(before.left() * 2, rel=0.15)
    # 命中测试用新的排版：quick 中间的逻辑坐标仍然落在 quick 里。
    center = QPointF(after.center().x() / (ratio * 2), after.center().y() / (ratio * 2))
    hit = window.hit_test(center)
    assert hit is not None and hit[0] == P2 and 4 <= hit[1] <= 9


# ---------------------------------------------------------------- 纯函数


@pytest.mark.parametrize(
    ("text", "position", "expected"),
    [
        ("The quick brown fox", 5, (4, 9)),
        ("The quick brown fox", 4, (4, 9)),
        ("The quick brown fox", 3, (3, 4)),  # 空格单独成词
        ("The quick brown fox", 99, (16, 19)),  # 超出末尾取最后一个词
        ("你好世界，再见", 4, (4, 5)),
        ("", 0, (0, 0)),
    ],
)
def test_word_span(text: str, position: int, expected: tuple[int, int]) -> None:
    assert word_span(text, position) == expected
