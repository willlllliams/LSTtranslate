"""译文区、淡入、状态行与复制（需求 BI-05、BI-06、RACE-04、RACE-06）。"""

import asyncio

import pytest
from PySide6.QtCore import QAbstractAnimation, Qt
from PySide6.QtGui import QGuiApplication, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang, SessionId
from lst.contracts.translation import AttemptState, SkipReason
from lst.ui.bilingual import system_clipboard
from lst.ui.bilingual import widgets as bilingual_widgets
from lst.ui.i18n import error_text
from lst.ui.theme import TOKENS
from tests.ui.bilingual.conftest import Harness, attempt, segment, statuses
from tests.ui.conftest import wait_until

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
    await harness.updates(
        old, segment("s1", "hello", attempt("argos", D, "旧", latency=50), shown_engine="argos")
    )
    await harness.updates(
        SessionId("screenshot"), segment("x", "shot", attempt("argos", D, "截图", latency=5))
    )
    assert pane.segment_count() == 0

    await harness.updates(
        new, segment("s1", "hello again", attempt("argos", D, "新", latency=40), shown_engine="argos")
    )
    assert pane.segment_text(0) == "新"
    # 当前会话之后，旧会话的刷新仍然丢弃。
    await harness.updates(
        old, segment("s1", "hello", attempt("argos", D, "旧", latency=50), shown_engine="argos")
    )
    assert pane.segment_text(0) == "新"


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

    calls: list[list[tuple[int, str]]] = []
    original = pane.update_segments

    def record(changes: list[tuple[int, str]], fade: list[int]) -> None:
        calls.append(list(changes))
        original(changes, fade)

    monkeypatch.setattr(pane, "update_segments", record)
    await harness.updates(
        session,
        segment(
            "p2", "second", attempt("argos", D, "第二段", latency=90), attempt("deepl"), shown_engine="argos"
        ),
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
        session, segment("p4", "fourth", attempt("argos", D, "第四段", latency=70), shown_engine="argos")
    )
    assert pane.segment_count() == 4
    assert pane.segment_text(3) == "第四段"


async def test_shown_engine_change_plays_fade_in(harness: Harness) -> None:
    session = await _session(harness, "hello")
    await harness.updates(session, segment("s1", "hello", attempt("argos", R), attempt("deepseek", R)))
    assert not _fading(harness, 0), "还没有译文时显示原文，不播放淡入"

    await harness.updates(
        session,
        segment(
            "s1",
            "hello",
            attempt("argos", D, "你好", latency=90),
            attempt("deepseek", R),
            shown_engine="argos",
        ),
    )
    animation = harness.window.translation.fade_animation(0)
    assert animation is not None
    assert animation.state() == QAbstractAnimation.State.Running
    assert animation.duration() == TOKENS.fade_ms == 150
    await wait_until(lambda: harness.window.translation.fade_animation(0) is None, timeout=2.0)

    # 只有其他实例的状态变化，显示的实例不变：不播放。
    await harness.updates(
        session,
        segment(
            "s1",
            "hello",
            attempt("argos", D, "你好", latency=90),
            attempt("deepseek", R),
            shown_engine="argos",
        ),
    )
    assert not _fading(harness, 0)

    # 更高优先级的实例返回，显示的实例变化：再播放一次。
    await harness.updates(
        session,
        segment(
            "s1",
            "hello",
            attempt("deepseek", D, "您好", latency=900),
            attempt("argos", D, "你好", latency=90),
            shown_engine="deepseek",
            finished=True,
        ),
    )
    assert _fading(harness, 0)
    assert harness.window.translation.segment_text(0) == "您好"


async def test_fades_of_one_update_share_one_animation(harness: Harness) -> None:
    session = await _session(harness)
    views = [
        segment(f"p{i}", f"text {i}", attempt("argos", D, f"译文 {i}", latency=10), shown_engine="argos")
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


def test_source_markers_and_engine_list_are_removed() -> None:
    """来源标记与引擎结果列表已经删除：译文区没有标记接口，模块里也没有结果列表控件。"""
    pane_type = bilingual_widgets.TranslationPane
    assert not hasattr(pane_type, "set_markers_visible")
    assert not hasattr(pane_type, "marker_color")
    assert not hasattr(bilingual_widgets, "EngineResultList")
    assert not hasattr(bilingual_widgets, "EngineRowWidget")


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
    reason, suggestion = error_text(ErrorCode.QUOTA)
    status = harness.presenter.status_text()
    assert status == f"翻译失败：{reason}。{suggestion}"
    assert harness.window.status_label.text() == status
    assert harness.window.status_label.isVisibleTo(harness.window)


async def test_failure_status_hides_technical_detail_and_clears_on_success(harness: Harness) -> None:
    session = await _session(harness, "hello")
    detail = "HTTP 401: invalid key SECRET"
    await harness.updates(
        session,
        segment(
            "s", "hello", attempt("argos", F, code=ErrorCode.AUTH, error=detail, latency=5), finished=True
        ),
    )
    assert detail not in harness.presenter.status_text()
    assert error_text(ErrorCode.AUTH)[0] in harness.presenter.status_text()

    retry = (await harness.type_and_wait("hello there")).session_id
    await harness.updates(
        retry, segment("s", "hello there", attempt("argos", D, "你好", latency=5), shown_engine="argos")
    )
    assert harness.presenter.status_text() == ""
    assert not harness.window.status_label.isVisibleTo(harness.window)


async def test_unsupported_pair_is_explained(harness: Harness) -> None:
    await harness.publish(statuses({"argos": "Argos 本地"}))
    session = await _session(harness, "hello")
    await harness.updates(session, segment("s", "hello", finished=True, skip_reason=SkipReason.NO_ENGINE))
    assert harness.window.translation.segment_text(0) == "hello"
    assert harness.presenter.status_text() == "“Argos 本地”不支持这段文字的语言对，显示原文。"


async def test_copy_translation(harness: Harness) -> None:
    session = await _session(harness, "one\ntwo")
    await harness.updates(
        session,
        segment("a", "one", attempt("argos", D, "一", latency=10), shown_engine="argos"),
        segment("b", "two", attempt("argos", D, "二", latency=10), shown_engine="argos"),
    )
    window = harness.window
    window.copy_button.click()
    assert harness.copied == ["一\n二"]
    assert window.copy_button.toolTip() == "复制全部译文", "复制不在键位表里，提示不写键位"

    # 复制译文不再有快捷键：Ctrl+Shift+C 不复制，也不往输入框里插入字符。
    QTest.keyClick(
        window.input_edit,
        Qt.Key.Key_C,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )
    assert harness.copied == ["一\n二"]
    assert window.input_edit.toPlainText() == "one\ntwo"


async def test_translation_is_selectable_and_ctrl_c_copies(harness: Harness) -> None:
    """译文区的文字可以用鼠标、键盘选中；标准的 Ctrl+C 在输入框和译文区里照常复制选中的部分，
    应用级的键位过滤器不拦截它。"""
    if QApplication.platformName() != "offscreen":
        pytest.skip("原生平台上不改动用户的系统剪贴板")
    session = await _session(harness, "one\ntwo")
    await harness.updates(
        session,
        segment("a", "one", attempt("argos", D, "第一段", latency=5), shown_engine="argos"),
        segment("b", "two", attempt("argos", D, "第二段", latency=5), shown_engine="argos"),
    )
    window = harness.window
    window.present()
    selectable = (
        Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
    )
    assert window.translation.textInteractionFlags() & selectable == selectable
    clipboard = QGuiApplication.clipboard()
    ctrl = Qt.KeyboardModifier.ControlModifier

    # 译文区：选中“第一”两个字，Ctrl+C 复制的正是它们。
    cursor = window.translation.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(2, QTextCursor.MoveMode.KeepAnchor)
    window.translation.setTextCursor(cursor)
    window.translation.setFocus()
    QTest.keyClick(window.translation, Qt.Key.Key_C, ctrl)
    assert clipboard.text() == "第一"

    # 输入框：Ctrl+A 全选、Ctrl+C 复制，与普通输入框相同。
    window.input_edit.setFocus()
    QTest.keyClick(window.input_edit, Qt.Key.Key_A, ctrl)
    QTest.keyClick(window.input_edit, Qt.Key.Key_C, ctrl)
    assert clipboard.text() == "one\ntwo"
    assert harness.copied == [], "标准复制由控件写剪贴板，不经过复制按钮的通路"


async def test_carried_translation_avoids_flash_while_typing(harness: Harness) -> None:
    """停顿后开始的新会话里，没改动的段先沿用上一个会话的译文，结果相同时不再淡入。"""
    first = await _session(harness, "one\ntwo")
    done = (attempt("argos", D, "一", latency=10),)
    await harness.updates(
        first,
        segment("a", "one", *done, shown_engine="argos"),
        segment("b", "two", attempt("argos", D, "二", latency=10), shown_engine="argos"),
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
    assert not any(_fading(harness, i) for i in range(3))

    # 缓存结果与占位相同：不淡入；新段有了译文：淡入。
    await harness.updates(
        second,
        segment("a2", "one", attempt("argos", D, "一", latency=0, cached=True), shown_engine="argos"),
        segment("c2", "three", attempt("argos", D, "三", latency=20), shown_engine="argos"),
    )
    assert not _fading(harness, 0)
    assert _fading(harness, 2)
    # 显示已定仍没有译文的段，按 RACE-04 回到原文。
    await harness.updates(
        second, segment("b2", "two", attempt("argos", F, code=ErrorCode.TIMEOUT), finished=True)
    )
    assert [pane.segment_text(i) for i in range(3)] == ["一", "two", "三"]


async def test_no_carry_over_when_service_changes(harness: Harness) -> None:
    """换了服务之后，上一个服务的译文不作占位：新会话里没有译文的段显示原文。"""
    await harness.publish(statuses({"argos": "Argos", "deepl": "DeepL"}))
    first = await _session(harness, "one")
    await harness.updates(
        first, segment("a", "one", attempt("argos", D, "一", latency=10), shown_engine="argos")
    )
    combo = harness.window.engine_combo
    combo.setCurrentIndex(combo.findText("DeepL"))
    await harness.flush()
    second = harness.last
    assert second.engine_id == "deepl"
    await harness.updates(second.session_id, segment("a2", "one", attempt("deepl")))
    assert harness.window.translation.segment_text(0) == "one"


async def test_no_carry_over_when_languages_change(harness: Harness) -> None:
    first = await _session(harness, "one")
    await harness.updates(
        first, segment("a", "one", attempt("argos", D, "一", latency=10), shown_engine="argos")
    )
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
