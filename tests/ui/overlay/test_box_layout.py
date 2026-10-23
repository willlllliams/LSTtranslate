"""翻译框扩大后保持字号、独立内容与选字几何一致。"""

from collections.abc import Iterator
from typing import cast

import pytest
from pydantic import ValidationError
from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QApplication

from lst.contracts.config import AppConfig
from lst.contracts.ids import SegmentId
from lst.contracts.ocr import BBox, BlockCategory, LayoutBlock
from lst.ui.overlay import OverlayLayoutConfig, OverlayWindow
from lst.ui.overlay.layout import confine_box, overlaps, sized_box
from lst.ui.overlay.window import layout_ink_bounds
from tests.ui.overlay.conftest import MakeRig, capture, raw_image, segment, text_result, view


class Actions:
    def retry(self) -> None:
        pass

    def copy(self, text: str) -> None:
        pass

    def window_closed(self, window: OverlayWindow) -> None:
        pass


@pytest.fixture
def window(qapp: QApplication) -> Iterator[OverlayWindow]:
    del qapp
    result = OverlayWindow(raw_image(), capture("layout", dpr=1).geometry, Actions())
    result.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    yield result
    result.close()


def show(window: OverlayWindow, neighbors: bool = False) -> BBox:
    source = BBox(80, 70, 260, 94)
    line = segment("a", "Hello world", source)
    others = [segment("b", "Next control", BBox(80, 135, 260, 159))] if neighbors else []
    block = LayoutBlock("0", BlockCategory.TEXT, BBox(0, 0, 600, 300), 0, 1.0)
    window.show_result((block,), (line, *others))
    window.fit_all_now()
    return source


def test_vertical_growth_keeps_horizontal_range_and_original_font(window: OverlayWindow) -> None:
    window.set_layout(
        OverlayLayoutConfig(
            growth="vertical", font_scale=1, vertical="top", text_padding_x=0, text_padding_y=0
        )
    )
    source = show(window)
    item = window.items[SegmentId("a")]
    window.apply_views([view("a", item.source, "这是一段比英文原文更长的中文翻译文字")])
    window.fit_all_now()
    assert item.source_bbox == source
    assert (item.bbox.x0, item.bbox.x1) == (source.x0, source.x1)
    assert item.bbox.y1 > source.y1
    assert window.font_px(item) >= round(item.source_px * window.scale_device())
    box = window._text_box(item)  # pyright: ignore[reportPrivateUsage]
    assert box.top + layout_ink_bounds(box.layout).top() == pytest.approx(0)


def test_user_can_enlarge_font_beyond_original_and_expand_both_axes(window: OverlayWindow) -> None:
    window.set_layout(OverlayLayoutConfig(growth="both", font_scale=1.5, max_growth=5, vertical="top"))
    show(window)
    item = window.items[SegmentId("a")]
    window.apply_views([view("a", item.source, "中文字体也可以比原文更大，并且完整显示")])
    window.fit_all_now()
    # 目标和设备字号分别取整，字体回退还会产生一像素的度量差。
    target = item.source_px * 1.5 * window.scale_device()
    assert round(target) - 1 <= window.font_px(item) <= round(target)
    assert window.font_px(item) > item.source_px * 1.4 * window.scale_device()
    assert item.bbox.width > 180 or item.bbox.height > 24
    layout = window._text_box(item).layout  # pyright: ignore[reportPrivateUsage]
    last = layout.lineAt(layout.lineCount() - 1)
    assert last.textStart() + last.textLength() == len(item.display)


def test_expansion_avoids_neighbor_and_returns_to_raw_geometry_without_drift(window: OverlayWindow) -> None:
    original = show(window, neighbors=True)
    config = OverlayLayoutConfig(scale_x=2, scale_y=3, box_padding_x=10, box_padding_y=8)
    window.set_layout(config)
    first = window.items[SegmentId("a")].bbox
    assert not overlaps(first, window.items[SegmentId("b")].bbox)
    assert first.y1 <= 135
    window.set_layout(OverlayLayoutConfig(growth="fixed"))
    window.set_layout(config)
    assert window.items[SegmentId("a")].bbox == first
    assert window.items[SegmentId("a")].source_bbox == original


@pytest.mark.parametrize("horizontal", ["left", "center", "right"])
@pytest.mark.parametrize("vertical", ["top", "center", "bottom"])
def test_alignment_and_hit_testing_share_the_same_geometry(
    window: OverlayWindow, horizontal: str, vertical: str
) -> None:
    window.set_layout(
        OverlayLayoutConfig.model_validate(
            {
                "growth": "fixed",
                "scale_x": 2,
                "scale_y": 3,
                "horizontal": horizontal,
                "vertical": vertical,
            }
        )
    )
    show(window)
    item = window.items[SegmentId("a")]
    window.apply_views([view("a", item.source, "测试")])
    window.fit_all_now()
    box = window._text_box(item)  # pyright: ignore[reportPrivateUsage]
    inner = window._inner_device(item)  # pyright: ignore[reportPrivateUsage]
    line = box.layout.lineAt(0)
    left = cast(tuple[float, int], line.cursorToX(0))[0]
    right = cast(tuple[float, int], line.cursorToX(1))[0]
    point = QPointF(
        (inner.x + (left + right) / 2) / window.device_ratio(),
        (inner.y + box.top + line.height() / 2) / window.device_ratio(),
    )
    assert window.hit_test(point, on_character=True) == (item.segment_id, 0)
    ink = layout_ink_bounds(box.layout)
    available = inner.height - ink.height()
    assert box.top + ink.top() == pytest.approx(
        {"top": 0, "center": available / 2, "bottom": available}[vertical]
    )
    if horizontal == "right":
        assert left > inner.width / 2
    if horizontal == "center":
        assert 0 < left < inner.width / 2


def test_zero_padding_and_skipped_symbols_keep_original_pixels(window: OverlayWindow) -> None:
    window.set_layout(OverlayLayoutConfig(scale_x=2, scale_y=2, text_padding_x=0, text_padding_y=0))
    source = show(window)
    item = window.items[SegmentId("a")]
    from dataclasses import replace

    from lst.contracts.translation import SkipReason

    window.apply_views([replace(view("a", "123", "123"), skip_reason=SkipReason.NO_TEXT)])
    assert item.bbox == source
    inner = window._inner_device(item)  # pyright: ignore[reportPrivateUsage]
    assert inner.width == pytest.approx(source.width * window.scale_device())


def test_confine_expanded_box_at_canvas_and_diagonal_neighbor() -> None:
    source = BBox(20, 20, 60, 40)
    neighbor = BBox(70, 50, 95, 90)
    result = confine_box(source, BBox(-10, -10, 130, 130), (100, 100), [neighbor])
    assert not overlaps(result, neighbor)
    assert 0 <= result.x0 <= source.x0 < source.x1 <= result.x1 <= 100
    assert 0 <= result.y0 <= source.y0 < source.y1 <= result.y1 <= 100


def test_growth_anchor_preserves_right_and_bottom() -> None:
    config = OverlayLayoutConfig(horizontal="right", vertical="bottom")
    assert sized_box(BBox(50, 50, 100, 100), 80, 90, config) == BBox(20, 10, 100, 100)


@pytest.mark.parametrize(
    "patch",
    [
        {"scale_x": 0.9},
        {"scale_y": float("nan")},
        {"max_growth": float("inf")},
        {"font_scale": 0},
        {"box_padding_x": -1},
        {"text_padding_y": -1},
        {"horizontal": "invalid"},
    ],
)
def test_invalid_layout_settings_are_rejected(patch: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OverlayLayoutConfig.model_validate(patch)


async def test_config_updates_existing_and_future_overlay_windows(make_rig: MakeRig) -> None:
    rig = await make_rig()
    await rig.publish(capture("a"), text_result("a"))
    source = rig.window.items[SegmentId("p1")].source_bbox
    await rig.publish(
        AppConfig({"overlay_layout": {"growth": "fixed", "scale_x": 1.4, "vertical": "top"}}, 2)
    )
    first = rig.window.items[SegmentId("p1")].bbox
    assert source is not None and first.width > source.width
    await rig.publish(capture("b"), text_result("b"))
    assert rig.window.items[SegmentId("p1")].bbox == first


def test_slightly_overlapping_ocr_lines_get_separate_drawing_boxes() -> None:
    first = BBox(10, 20, 100, 44)
    second = BBox(10, 42, 100, 65)
    a = confine_box(first, BBox(0, 10, 150, 70), (200, 200), [second])
    b = confine_box(second, BBox(0, 20, 150, 90), (200, 200), [first, a])
    assert a.y1 <= b.y0


def test_manual_width_growth_at_canvas_edge_remeasures_wrapping(window: OverlayWindow) -> None:
    window.set_layout(OverlayLayoutConfig(scale_x=3, growth="both", font_scale=1.1, vertical="top"))
    block = LayoutBlock("0", BlockCategory.TEXT, BBox(0, 0, 600, 300), 0, 1)
    window.show_result((block,), (segment("a", "Hello", BBox(480, 40, 590, 62)),))
    item = window.items[SegmentId("a")]
    window.apply_views([view("a", item.source, "画面边缘的译文仍然完整显示")])
    window.fit_all_now()
    assert item.bbox.x1 <= 600
    assert window.font_px(item) >= round(item.source_px * window.scale_device())
