"""覆盖窗口的译文过渡：每段从自己的译文到达时刻起独立计时（RACE-06、OV-04）。

旧实现整个窗口只有一个过渡进度，每次刷新都把它清零：一张截图的译文陆续到达时，先到的段一次次回到
“几乎全是原文”。这里用可控的时钟逐段核对进度。
"""

from collections.abc import Iterator
from typing import Protocol

import pytest

from lst.contracts.ids import EngineId, SegmentId, SessionId
from lst.contracts.ocr import BBox, BlockCategory, LayoutBlock, OcrCompleted
from lst.contracts.translation import AttemptState, AttemptView, SegmentView
from lst.ui.overlay import OverlayWindow
from lst.ui.theme import TOKENS
from tests.ui.conftest import wait_until
from tests.ui.overlay.conftest import HEIGHT, WIDTH, capture, hide_from_desktop, raw_image, segment

FADE_S = TOKENS.fade_ms / 1000


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Actions:
    def retry(self) -> None:
        pass

    def copy(self, text: str) -> None:
        pass

    def window_closed(self, window: OverlayWindow) -> None:
        window.deleteLater()


def lines_result(count: int) -> OcrCompleted:
    block = LayoutBlock("0", BlockCategory.TEXT, BBox(0, 0, WIDTH, HEIGHT), 0, 1.0)
    height = HEIGHT // count
    segments = tuple(
        segment(f"p{i}", f"Line {i}", BBox(10, i * height, WIDTH - 10, (i + 1) * height - 2))
        for i in range(count)
    )
    return OcrCompleted(SessionId("s1"), (block,), segments)


def translated(index: int) -> SegmentView:
    attempt = AttemptView(EngineId("argos"), AttemptState.DONE, f"第 {index} 行", None, 30.0, started=True)
    return SegmentView(
        segment_id=SegmentId(f"p{index}"),
        source_text=f"Line {index}",
        display_text=f"第 {index} 行",
        shown_engine=EngineId("argos"),
        finished=True,
        attempts=(attempt,),
    )


class MakeWindow(Protocol):
    def __call__(self, count: int, clock: FakeClock | None = None) -> OverlayWindow: ...


@pytest.fixture
def make_window() -> Iterator[MakeWindow]:
    windows: list[OverlayWindow] = []

    def make(count: int, clock: FakeClock | None = None) -> OverlayWindow:
        shot = capture("s1")
        window = OverlayWindow(raw_image(), shot.geometry, Actions(), clock=clock)
        hide_from_desktop(window)
        window.show()
        result = lines_result(count)
        window.show_result(result.blocks, result.segments)
        windows.append(window)
        return window

    yield make
    for window in windows:
        window.close()


def test_later_arrival_does_not_reset_earlier_fade(make_window: MakeWindow) -> None:
    """A 到达 100 毫秒后 B 到达：A 的过渡继续，不被清零；B 从 0 开始。"""
    clock = FakeClock()
    window = make_window(2, clock)
    a, b = SegmentId("p0"), SegmentId("p1")

    start = clock.now
    window.apply_views([translated(0)])
    assert window.fade_progress(a) == 0.0
    clock.now = start + 0.100
    before = window.fade_progress(a)
    assert before == pytest.approx(0.100 / FADE_S)

    window.apply_views([translated(1)])
    assert window.fade_progress(a) == pytest.approx(before)
    assert window.fade_progress(b) == 0.0
    assert window.fading_segments() == {a, b}

    clock.now = start + FADE_S
    assert window.fade_progress(a) == pytest.approx(1.0)
    assert window.fade_progress(b) == pytest.approx((FADE_S - 0.100) / FADE_S)


def test_each_of_eleven_segments_finishes_within_its_own_150_ms(make_window: MakeWindow) -> None:
    """间隔 30 毫秒连续到达 11 段：每段在自己到达后 150 毫秒内完全显示，与后到的段无关。"""
    clock = FakeClock()
    window = make_window(11, clock)
    start = clock.now
    arrivals: list[float] = []
    for index in range(11):
        clock.now = start + index * 0.030
        arrivals.append(clock.now)
        window.apply_views([translated(index)])
        # 已经到达的段，进度只由各自的到达时刻决定。
        for earlier in range(index + 1):
            expected = min(1.0, (clock.now - arrivals[earlier]) / FADE_S)
            assert window.fade_progress(SegmentId(f"p{earlier}")) == pytest.approx(expected)

    for index, arrived in enumerate(arrivals):
        clock.now = arrived + FADE_S
        assert window.fade_progress(SegmentId(f"p{index}")) == pytest.approx(1.0)
        clock.now = arrived + FADE_S * 0.5
        assert window.fade_progress(SegmentId(f"p{index}")) == pytest.approx(0.5)


async def test_fading_set_is_empty_after_everything_finishes(make_window: MakeWindow) -> None:
    """真实时钟：过渡全部结束后，过渡中的段集合为空，计时器停下，旧文字被清掉。"""
    window = make_window(11)
    for index in range(11):
        window.apply_views([translated(index)])
    assert len(window.fading_segments()) == 11
    await wait_until(lambda: not window.fading_segments(), timeout=2)
    assert all(item.fade_from is None and item.fade_started is None for item in window.items.values())
    assert [window.shown_text(item) for item in window.visible_items()] == [f"第 {i} 行" for i in range(11)]
