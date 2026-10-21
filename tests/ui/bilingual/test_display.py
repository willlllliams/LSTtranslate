"""译文区、来源标记、淡入、引擎结果列表与复制（需求 BI-05、BI-06、RACE-04、RACE-06）。"""

import asyncio

import pytest
from PySide6.QtCore import QAbstractAnimation, QPoint, Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang, SessionId
from lst.contracts.translation import AttemptState
from lst.ui.bilingual import SegmentItem, system_clipboard
from lst.ui.bilingual.widgets import MARKER_OFFSET
from lst.ui.i18n import error_text, tr
from lst.ui.theme import TOKENS, engine_color
from tests.ui.bilingual.conftest import Harness, attempt, config, segment, statuses, wait_until

Q, R, D, F, C = (
    AttemptState.QUEUED,
    AttemptState.RUNNING,
    AttemptState.DONE,
    AttemptState.FAILED,
    AttemptState.CANCELLED,
)


async def _session(harness: Harness, text: str = "first\nsecond\nthird") -> SessionId:
    return (await harness.type_and_wait(text)).session_id


def _fading(harness: Harness, index: int) -> bool:
    animation = harness.window.translation.fade_animation(index)
    return animation is not None and animation.state() == QAbstractAnimation.State.Running


async def test_only_updates_of_own_latest_session_are_shown(harness: Harness) -> None:
    old = await _session(harness, "hello")
    edit = harness.window.input_edit
    QTest.keyClicks(edit, " again")
    new = await wait_until_new_session(harness, old)
    pane = harness.window.translation

    # 已被取代的会话、截屏会话的刷新都丢弃。
    await harness.updates(old, segment("s1", "hello", attempt("argos", D, "旧", latency=50), winner="argos"))
    await harness.updates(
        SessionId("screenshot"), segment("x", "shot", attempt("argos", D, "截图", latency=5))
    )
    assert pane.segment_count() == 0

    await harness.updates(
        new, segment("s1", "hello again", attempt("argos", D, "新", latency=40), winner="argos")
    )
    assert pane.segment_text(0) == "新"
    # 当前会话之后，旧会话的刷新仍然丢弃。
    await harness.updates(old, segment("s1", "hello", attempt("argos", D, "旧", latency=50), winner="argos"))
    assert pane.segment_text(0) == "新"
    assert [row.engine_id for row in harness.presenter.engine_rows()] == ["argos"]


async def wait_until_new_session(harness: Harness, old: SessionId) -> SessionId:
    await wait_until(lambda: harness.last.session_id != old)
    return harness.last.session_id


async def test_segments_shown_in_order_and_updated_incrementally(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _session(harness)
    pane = harness.window.translation
    attempts = (attempt("argos"), attempt("deepl"))
    await harness.updates(
        session,
        segment("p1", "first", *attempts),
        segment("p2", "second", *attempts),
        segment("p3", "third\nline", *attempts),
    )
    # 还没有译文时显示原文；段内换行保留在同一段里。
    assert [pane.segment_text(i) for i in range(3)] == ["first", "second", "third\nline"]
    assert pane.document().blockCount() == 3

    calls: list[list[tuple[int, SegmentItem]]] = []
    original = pane.update_segments

    def record(changes: list[tuple[int, SegmentItem]], fade: list[int]) -> None:
        calls.append(list(changes))
        original(changes, fade)

    monkeypatch.setattr(pane, "update_segments", record)
    await harness.updates(
        session,
        segment("p2", "second", attempt("argos", D, "第二段", latency=90), attempt("deepl"), winner="argos"),
    )
    assert [[index for index, _ in call] for call in calls] == [[1]], "只改动有变化的那一段"
    assert [pane.segment_text(i) for i in range(3)] == ["first", "第二段", "third\nline"]
    assert harness.presenter.translation_text() == "first\n第二段\nthird\nline"

    # 只有尝试状态变化、显示内容不变的段不改动译文区。
    calls.clear()
    await harness.updates(session, segment("p1", "first", attempt("argos", R), attempt("deepl")))
    assert calls == []

    # 第一帧之后出现的新段追加到末尾。
    await harness.updates(
        session, segment("p4", "fourth", attempt("argos", D, "第四段", latency=70), winner="argos")
    )
    assert pane.segment_count() == 4
    assert pane.segment_text(3) == "第四段"


async def test_winner_change_plays_fade_in(harness: Harness) -> None:
    session = await _session(harness, "hello")
    await harness.updates(session, segment("s1", "hello", attempt("argos", R), attempt("deepseek", R)))
    assert not _fading(harness, 0), "没有胜出者时显示原文，不播放淡入"

    await harness.updates(
        session,
        segment(
            "s1", "hello", attempt("argos", D, "你好", latency=90), attempt("deepseek", R), winner="argos"
        ),
    )
    animation = harness.window.translation.fade_animation(0)
    assert animation is not None
    assert animation.state() == QAbstractAnimation.State.Running
    assert animation.duration() == TOKENS.fade_ms == 150
    await wait_until(lambda: harness.window.translation.fade_animation(0) is None, timeout=2.0)

    # 只有其他实例的状态变化，胜出者不变：不播放。
    await harness.updates(
        session,
        segment(
            "s1", "hello", attempt("argos", D, "你好", latency=90), attempt("deepseek", R), winner="argos"
        ),
    )
    assert not _fading(harness, 0)

    # 更高优先级的实例返回，胜出者变化：再播放一次。
    await harness.updates(
        session,
        segment(
            "s1",
            "hello",
            attempt("deepseek", D, "您好", latency=900),
            attempt("argos", D, "你好", latency=90),
            winner="deepseek",
            finished=True,
        ),
    )
    assert _fading(harness, 0)
    assert harness.window.translation.segment_text(0) == "您好"


async def test_fades_of_one_update_share_one_animation(harness: Harness) -> None:
    session = await _session(harness)
    views = [
        segment(f"p{i}", f"text {i}", attempt("argos", D, f"译文 {i}", latency=10), winner="argos")
        for i in range(3)
    ]
    await harness.updates(session, *views)
    pane = harness.window.translation
    animations = {id(pane.fade_animation(i)) for i in range(3)}
    assert len(animations) == 1 and pane.fade_animation(0) is not None
    await wait_until(lambda: all(pane.fade_animation(i) is None for i in range(3)), timeout=2.0)
    # 动画结束后文字恢复为默认颜色，没有残留的透明度。
    cursor = pane.textCursor()
    cursor.setPosition(pane.document().findBlockByNumber(1).position() + 1)
    assert not cursor.charFormat().hasProperty(cursor.charFormat().Property.ForegroundBrush)


async def test_source_marker_colors_and_switch(harness: Harness) -> None:
    await harness.publish(statuses({"argos": "Argos", "deepl": "DeepL"}, {"argos": "#123456", "deepl": ""}))
    session = await _session(harness, "one\ntwo\nthree")
    await harness.updates(
        session,
        segment("a", "one", attempt("argos", D, "一", latency=10), winner="argos"),
        segment("b", "two", attempt("deepl", D, "二", latency=10), winner="deepl"),
        segment("c", "three", attempt("argos", R)),
    )
    pane = harness.window.translation
    assert pane.markers_visible()
    assert pane.marker_color(0) == QColor("#123456")
    assert pane.marker_color(1) == engine_color("deepl"), "空串按实例号取默认颜色"
    assert pane.marker_color(2) is None, "没有胜出者的段不画标记"

    # 标记真的画在段旁。
    pane.resize(400, 300)
    image = pane.viewport().grab().toImage()
    block = pane.document().findBlockByNumber(0)
    rect = pane.document().documentLayout().blockBoundingRect(block)
    point = QPoint(int(rect.left()) + MARKER_OFFSET + 1, int(rect.center().y()))
    dpr = image.devicePixelRatio()
    assert image.pixelColor(int(point.x() * dpr), int(point.y() * dpr)) == QColor("#123456")

    await harness.publish(config(marker=False, revision=2))
    assert not pane.markers_visible()
    assert pane.marker_color(0) is None
    await harness.publish(config(marker=True, revision=3))
    assert pane.marker_color(0) == QColor("#123456")

    # 实例颜色在配置里改了，段旁的标记跟着变。
    await harness.publish(statuses({"argos": "Argos", "deepl": "DeepL"}, {"argos": "#00ff00", "deepl": ""}))
    assert pane.marker_color(0) == QColor("#00ff00")


async def test_marker_defaults_on_and_follows_config_types(harness: Harness) -> None:
    assert harness.window.translation.markers_visible()
    await harness.publish(config(marker="no", revision=2))  # 类型不对：默认打开
    assert harness.window.translation.markers_visible()


async def test_engine_rows_content_and_order(harness: Harness) -> None:
    await harness.publish(statuses({"deepseek": "DeepSeek", "argos": "Argos", "deepl": "DeepL"}))
    session = await _session(harness, "one\ntwo")
    secret_detail = "HTTP 401: invalid key"
    # 竞速模块给出的尝试顺序：胜出者第一，其余按优先级。
    first = segment(
        "a",
        "one",
        attempt("deepseek", D, "第一段", latency=1234.4),
        attempt("deepl", F, code=ErrorCode.AUTH, error=secret_detail, latency=200),
        attempt("ollama", R),
        attempt("argos", D, "一", latency=0, cached=True),
        attempt("google", C, latency=5),
        winner="deepseek",
    )
    second = segment(
        "b",
        "two",
        attempt("deepseek", D, "第二段", latency=1500),
        attempt("deepl", F, code=ErrorCode.AUTH, error=secret_detail, latency=210),
        attempt("ollama", Q),
        attempt("argos", D, "二", latency=0, cached=True),
        attempt("google", C, latency=6),
        winner="deepseek",
    )
    await harness.updates(session, first, second)

    rows = harness.presenter.engine_rows()
    assert [row.engine_id for row in rows] == ["deepseek", "deepl", "ollama", "argos", "google"]
    deepseek, deepl, ollama, argos, google = rows
    assert (deepseek.name, deepseek.status, deepseek.latency) == ("DeepSeek", "完成", "1500 毫秒")
    assert deepseek.text == "第一段\n第二段"
    assert deepseek.error == ""
    assert (argos.status, argos.latency, argos.text) == ("完成", tr("bilingual.latency.cached"), "一\n二")
    reason, suggestion = error_text(ErrorCode.AUTH)
    assert deepl.status == "失败"
    assert reason in deepl.error and suggestion in deepl.error
    assert secret_detail not in deepl.error, "技术细节不显示给用户"
    assert (ollama.name, ollama.status, ollama.latency) == ("ollama", "翻译中", ""), "没有名称时显示实例号"
    assert (google.status, google.text) == ("已取消", "")

    widgets = harness.window.engine_list.rows()
    assert [w.engine_id for w in widgets] == [row.engine_id for row in rows]
    top = widgets[0]
    assert top.name_label.text() == "DeepSeek"
    assert top.status_label.text() == "完成"
    assert top.latency_label.text() == "1500 毫秒"
    assert top.text_label.text() == "第一段\n第二段"
    assert not top.error_label.isVisibleTo(top)
    assert widgets[1].error_label.isVisibleTo(widgets[1])
    assert widgets[1].error_label.text() == deepl.error
    assert not widgets[1].copy_button.isEnabled(), "没有译文的行不能复制"

    # Argos 两段都胜出后排到第一行。
    await harness.updates(
        session,
        segment("a", "one", attempt("argos", D, "一", latency=0, cached=True), winner="argos"),
        segment("b", "two", attempt("argos", D, "二", latency=0, cached=True), winner="argos"),
    )
    assert harness.presenter.engine_rows()[0].engine_id == "argos"
    assert harness.window.engine_list.rows()[0].engine_id == "argos"


async def test_engine_names_follow_engine_statuses(harness: Harness) -> None:
    session = await _session(harness, "hello")
    await harness.updates(
        session, segment("s", "hello", attempt("argos", D, "你好", latency=30), winner="argos")
    )
    assert harness.window.engine_list.rows()[0].name_label.text() == "argos"
    await harness.publish(statuses({"argos": "Argos 本地"}))
    assert harness.window.engine_list.rows()[0].name_label.text() == "Argos 本地"


async def test_all_failed_segment_shows_source_and_reasons(harness: Harness) -> None:
    session = await _session(harness, "hello")
    await harness.updates(
        session,
        segment(
            "s",
            "hello",
            attempt("deepl", F, code=ErrorCode.QUOTA, latency=100),
            attempt("argos", F, code=ErrorCode.MODEL_MISSING, latency=3),
            finished=True,
        ),
    )
    assert harness.window.translation.segment_text(0) == "hello"
    assert harness.window.translation.marker_color(0) is None
    errors = {str(row.engine_id): row.error for row in harness.presenter.engine_rows()}
    assert error_text(ErrorCode.QUOTA)[0] in errors["deepl"]
    assert error_text(ErrorCode.MODEL_MISSING)[0] in errors["argos"]


async def test_copy_translation_and_single_row(harness: Harness) -> None:
    session = await _session(harness, "one\ntwo")
    await harness.updates(
        session,
        segment(
            "a",
            "one",
            attempt("deepl", D, "一", latency=10),
            attempt("argos", D, "壹", latency=5),
            winner="deepl",
        ),
        segment(
            "b",
            "two",
            attempt("deepl", D, "二", latency=10),
            attempt("argos", D, "贰", latency=5),
            winner="deepl",
        ),
    )
    window = harness.window
    window.copy_button.click()
    assert harness.copied == ["一\n二"]

    # Ctrl+Shift+C 在窗口里任何位置都能复制译文。
    QTest.keyClick(
        window.input_edit,
        Qt.Key.Key_C,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )
    QTest.keyClick(
        window.translation,
        Qt.Key.Key_C,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )
    assert harness.copied == ["一\n二"] * 3
    assert window.input_edit.toPlainText() == "one\ntwo", "快捷键不往输入框里插入字符"

    rows = {str(row.engine_id): row for row in window.engine_list.rows()}
    rows["argos"].copy_button.click()
    assert harness.copied[-1] == "壹\n贰"


async def test_carried_translation_avoids_flash_while_typing(harness: Harness) -> None:
    """停顿后开始的新会话里，没改动的段先沿用上一个会话的译文，结果相同时不再淡入。"""
    first = await _session(harness, "one\ntwo")
    done = (attempt("argos", D, "一", latency=10),)
    await harness.updates(
        first,
        segment("a", "one", *done, winner="argos"),
        segment("b", "two", attempt("argos", D, "二", latency=10), winner="argos"),
    )
    await wait_until(lambda: harness.window.translation.fade_animation(0) is None, timeout=2.0)

    # QTest.keyClicks 遇到换行符会让 Qt 进程直接退出（PySide6 6.12 实测），所以用 insertPlainText 追加一段。
    harness.window.input_edit.insertPlainText("\nthree")
    second = await wait_until_new_session(harness, first)
    pane = harness.window.translation
    await harness.updates(
        second,
        segment("a2", "one", attempt("argos")),
        segment("b2", "two", attempt("argos")),
        segment("c2", "three", attempt("argos")),
    )
    assert [pane.segment_text(i) for i in range(3)] == ["一", "二", "three"]
    assert pane.marker_color(0) == engine_color("argos")
    assert not any(_fading(harness, i) for i in range(3))

    # 缓存结果与占位相同：不淡入；新段有了译文：淡入。
    await harness.updates(
        second,
        segment("a2", "one", attempt("argos", D, "一", latency=0, cached=True), winner="argos"),
        segment("c2", "three", attempt("argos", D, "三", latency=20), winner="argos"),
    )
    assert not _fading(harness, 0)
    assert _fading(harness, 2)
    # 竞速结束仍没有胜出者的段，按 RACE-04 回到原文。
    await harness.updates(
        second, segment("b2", "two", attempt("argos", F, code=ErrorCode.TIMEOUT), finished=True)
    )
    assert [pane.segment_text(i) for i in range(3)] == ["一", "two", "三"]


async def test_no_carry_over_when_languages_change(harness: Harness) -> None:
    first = await _session(harness, "one")
    await harness.updates(first, segment("a", "one", attempt("argos", D, "一", latency=10), winner="argos"))
    harness.window.target_combo.setCurrentIndex(harness.window.target_combo.findData("ja"))
    await harness.flush()
    second = harness.last
    assert second.target == Lang("ja")
    await harness.updates(second.session_id, segment("a2", "one", attempt("argos")))
    assert harness.window.translation.segment_text(0) == "one"


async def test_system_clipboard_writes_qclipboard(qapp: QApplication) -> None:
    if QApplication.platformName() != "offscreen":
        pytest.skip("原生平台上不改动用户的系统剪贴板")
    system_clipboard("复制的文字")
    await asyncio.sleep(0)
    assert QGuiApplication.clipboard().text() == "复制的文字"
