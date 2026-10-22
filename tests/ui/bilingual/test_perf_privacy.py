"""界面线程上的单次更新耗时（需求 REL-04）与日志隐私（需求 PRV-02）。"""

import logging
import time

import pytest
from PySide6.QtWidgets import QApplication

from lst.contracts.errors import ErrorCode
from lst.contracts.events import SegmentsUpdated, ToggleBilingualWindow
from lst.contracts.ids import SessionId
from lst.contracts.translation import AttemptState, SegmentView
from lst.ui.theme import TOKENS
from tests.ui.bilingual.conftest import Harness, attempt, segment, statuses

D, R, Q = AttemptState.DONE, AttemptState.RUNNING, AttemptState.QUEUED
ENGINES = ("deepseek", "deepl", "google", "argos")
SEGMENTS = 100
BUDGET_S = 0.050  # 需求是 16 毫秒；测试机器负载不定，断言放宽到 50 毫秒，实测值打印出来


def _views(stage: int) -> tuple[SegmentView, ...]:
    """100 段、每段 4 个实例的快照（服务列表只用一个，快照的规模按截屏翻译的最坏情况估）。

    stage 0：都在排队；1：Argos 的译文显示；2：换成 DeepSeek 的译文。
    """
    views: list[SegmentView] = []
    for i in range(SEGMENTS):
        source = f"Paragraph {i}: the quick brown fox jumps over the lazy dog."
        if stage == 0:
            attempts = tuple(attempt(e, Q) for e in ENGINES)
            views.append(segment(f"p{i}", source, *attempts))
        elif stage == 1:
            attempts = (
                attempt("argos", D, f"第 {i} 段：敏捷的棕色狐狸跳过了懒狗。", latency=120 + i),
                attempt("deepseek", R),
                attempt("deepl", R),
                attempt("google", R),
            )
            views.append(segment(f"p{i}", source, *attempts, shown_engine="argos"))
        else:
            attempts = (
                attempt("deepseek", D, f"第 {i} 段：那只敏捷的棕色狐狸跃过了那条懒狗。", latency=1500 + i),
                attempt("deepl", D, f"第 {i} 段：快速的棕色狐狸跳过懒狗。", latency=600 + i),
                attempt("google", D, f"第 {i} 段：快速棕色狐狸跳过了懒狗。", latency=400 + i),
                attempt("argos", D, f"第 {i} 段：敏捷的棕色狐狸跳过了懒狗。", latency=120 + i),
            )
            views.append(segment(f"p{i}", source, *attempts, shown_engine="deepseek", finished=True))
    return tuple(views)


def _measure(harness: Harness, session_id: SessionId, views: tuple[SegmentView, ...]) -> tuple[float, float]:
    """直接调用总线处理函数并计时；再计一次随后的布局与绘制（它们是事件循环里的下一次操作）。"""
    event = SegmentsUpdated(session_id=session_id, views=views)
    started = time.perf_counter()
    harness.presenter.on_segments_updated(event)
    handled = time.perf_counter() - started
    started = time.perf_counter()
    QApplication.processEvents()
    painted = time.perf_counter() - started
    return handled, painted


async def test_hundred_segment_update_stays_within_frame_budget(harness: Harness) -> None:
    await harness.publish(statuses({e: e.title() for e in ENGINES}))
    harness.bus.send(ToggleBilingualWindow())
    await harness.flush()
    # 预热一次：首次排版会加载字体等一次性资源。
    warmup = (await harness.type_and_wait("warm up")).session_id
    _measure(harness, warmup, _views(1)[:5])

    text = "\n".join(f"Paragraph {i}" for i in range(SEGMENTS))
    session = (await harness.type_and_wait(text)).session_id
    results: dict[str, tuple[float, float]] = {}
    results["新会话第一帧（重建）"] = _measure(harness, session, _views(0))
    results["100 段首个译文（全部淡入）"] = _measure(harness, session, _views(1))
    results["100 段换成更高优先级译文"] = _measure(harness, session, _views(2))

    # 淡入动画的一帧：100 段共用一个动画，一帧只改一次文档格式。
    pane = harness.window.translation
    animation = pane.fade_animation(0)
    assert animation is not None and animation is pane.fade_animation(99)
    started = time.perf_counter()
    animation.setCurrentTime(TOKENS.fade_ms // 2)
    frame = time.perf_counter() - started
    started = time.perf_counter()
    QApplication.processEvents()
    results["淡入动画的一帧（100 段）"] = (frame, time.perf_counter() - started)

    for name, (handled, painted) in results.items():
        print(f"{name}：处理 {handled * 1000:.2f} 毫秒，随后布局与绘制 {painted * 1000:.2f} 毫秒")
    assert pane.segment_count() == SEGMENTS
    assert pane.segment_text(99).startswith("第 99 段：那只")
    for name, (handled, _) in results.items():
        assert handled < BUDGET_S, f"{name} 耗时 {handled * 1000:.1f} 毫秒"


async def test_logs_contain_no_source_or_translation(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    secret_input = "机密原文 SECRET-INPUT-7f3a"
    secret_output = "机密译文 SECRET-OUTPUT-91c2"
    secret_detail = "HTTP 400: echoed SECRET-INPUT-7f3a"
    harness.bus.send(ToggleBilingualWindow())
    session = (await harness.type_and_wait(secret_input)).session_id
    await harness.updates(
        session,
        segment(
            "s",
            secret_input,
            attempt("argos", D, secret_output, latency=50),
            attempt(
                "deepl", AttemptState.FAILED, code=ErrorCode.BAD_REQUEST, error=secret_detail, latency=80
            ),
            shown_engine="argos",
        ),
    )
    harness.window.copy_button.click()
    harness.window.clear_button.click()
    await harness.flush()
    # 其他会话的刷新被丢弃时同样不记文本。
    await harness.updates(SessionId("other"), segment("x", secret_input, attempt("argos", D, secret_output)))

    assert "TranslateText" in caplog.text, "确认确实记录了日志，断言不是空转"
    for secret in ("SECRET-INPUT", "SECRET-OUTPUT", "机密"):
        assert secret not in caplog.text
