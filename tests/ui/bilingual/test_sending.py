"""输入停顿、Ctrl+Enter、清空与语言切换时发出的 TranslateText（需求 BI-03、BI-04、BI-07）。"""

import asyncio
import re
import time

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from lst.contracts.ids import Lang, SessionId
from lst.contracts.translation import AttemptState
from lst.ui.bilingual.options import DEFAULT_DEBOUNCE_MS
from tests.ui.bilingual.conftest import (
    DEBOUNCE_MS,
    Harness,
    StartHarness,
    attempt,
    config,
    segment,
    wait_until,
)

_PAUSE = DEBOUNCE_MS / 1000


def _combo_index(harness: Harness, lang: str | None) -> int:
    return harness.window.target_combo.findData(lang or "")


async def test_sends_once_after_pause(harness: Harness) -> None:
    started = time.monotonic()
    QTest.keyClicks(harness.window.input_edit, "hello world")
    await harness.flush()
    assert harness.sent == []
    await wait_until(lambda: len(harness.sent) == 1)
    elapsed = time.monotonic() - started
    assert elapsed >= _PAUSE * 0.9

    command = harness.last
    assert command.text == "hello world"
    assert (command.source, command.target, command.include_billing) == (None, None, False)
    assert re.fullmatch(r"[0-9a-f]{32}", command.session_id)
    assert harness.presenter.session_id == command.session_id

    # 停顿只触发一次。
    await asyncio.sleep(_PAUSE * 2)
    assert len(harness.sent) == 1


async def test_typing_during_pause_restarts_timer_and_sends_last_text(start_harness: StartHarness) -> None:
    harness = await start_harness(with_config=False)  # 使用默认的 400 毫秒停顿，计时余量更大
    timer = harness.presenter.debounce_timer
    edit = harness.window.input_edit
    QTest.keyClicks(edit, "a")
    assert timer.isActive()
    await asyncio.sleep(0.1)
    before = timer.remainingTime()
    QTest.keyClicks(edit, "bc")
    after = timer.remainingTime()
    assert harness.sent == []
    assert before <= DEFAULT_DEBOUNCE_MS - 50
    assert after > before + 50, "新的输入应当让停顿重新计时"

    await wait_until(lambda: len(harness.sent) == 1)
    assert harness.last.text == "abc"
    await asyncio.sleep(DEFAULT_DEBOUNCE_MS / 1000 + 0.1)
    assert len(harness.sent) == 1


async def test_same_text_is_not_sent_again(harness: Harness) -> None:
    first = await harness.type_and_wait("hello")
    edit = harness.window.input_edit
    # 输入又删掉，文字回到上次发送的样子。
    QTest.keyClicks(edit, "x")
    QTest.keyClick(edit, Qt.Key.Key_Backspace)
    await asyncio.sleep(_PAUSE * 2.5)
    # 首尾空白不算文本变化。
    QTest.keyClicks(edit, "  ")
    await asyncio.sleep(_PAUSE * 2.5)
    assert harness.sent == [first]


async def test_ctrl_enter_sends_immediately_with_billing(start_harness: StartHarness) -> None:
    harness = await start_harness(with_config=False)
    edit = harness.window.input_edit
    QTest.keyClicks(edit, "hello")
    assert harness.presenter.debounce_timer.isActive()
    QTest.keyClick(edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    await harness.flush()

    assert len(harness.sent) == 1
    assert harness.last.text == "hello"
    assert harness.last.include_billing is True
    assert not harness.presenter.debounce_timer.isActive(), "Ctrl+Enter 先停止停顿计时器"
    assert edit.toPlainText() == "hello", "Ctrl+Enter 不往输入框里插入换行"
    await asyncio.sleep(DEFAULT_DEBOUNCE_MS / 1000 + 0.1)
    assert len(harness.sent) == 1


async def test_ctrl_enter_keeps_session_for_same_text(harness: Harness) -> None:
    auto = await harness.type_and_wait("hello")
    assert auto.include_billing is False
    # 小键盘的 Enter 同样有效。
    QTest.keyClick(
        harness.window.input_edit,
        Qt.Key.Key_Enter,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier,
    )
    await harness.flush()
    assert len(harness.sent) == 2
    assert harness.last.session_id == auto.session_id
    assert harness.last.include_billing is True
    assert harness.last.text == "hello"


async def test_ctrl_enter_starts_new_session_for_new_text(harness: Harness) -> None:
    auto = await harness.type_and_wait("hello")
    edit = harness.window.input_edit
    QTest.keyClicks(edit, " world")
    QTest.keyClick(edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    await harness.flush()
    assert len(harness.sent) == 2
    assert harness.last.session_id != auto.session_id
    assert (harness.last.text, harness.last.include_billing) == ("hello world", True)
    # 停顿计时器已停，不会再为同一段文字发一次。
    await asyncio.sleep(_PAUSE * 2)
    assert len(harness.sent) == 2


async def test_ctrl_enter_with_empty_input_does_nothing(harness: Harness) -> None:
    QTest.keyClick(harness.window.input_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    await harness.flush()
    assert harness.sent == []


async def test_clear_button_sends_empty_text_and_clears_display(harness: Harness) -> None:
    command = await harness.type_and_wait("hello")
    await harness.updates(
        command.session_id,
        segment("s1", "hello", attempt("argos", AttemptState.DONE, "你好", latency=80), winner="argos"),
    )
    window = harness.window
    assert window.translation.segment_count() == 1
    assert len(window.engine_list.rows()) == 1

    window.clear_button.click()
    await harness.flush()
    assert len(harness.sent) == 2
    cleared = harness.last
    assert cleared.text == ""
    assert cleared.session_id != command.session_id
    assert window.input_edit.toPlainText() == ""
    assert window.translation.segment_count() == 0
    assert window.translation.toPlainText() == ""
    assert window.engine_list.rows() == []
    assert harness.presenter.translation_text() == ""

    # 旧会话迟到的刷新被丢弃。
    await harness.updates(
        command.session_id,
        segment("s1", "hello", attempt("deepl", AttemptState.DONE, "您好", latency=300), winner="deepl"),
    )
    assert window.translation.segment_count() == 0
    # 再清空一次（输入框已经是空的）不再发送。
    window.clear_button.click()
    await harness.flush()
    assert len(harness.sent) == 2


async def test_deleting_all_text_sends_empty_text_immediately(harness: Harness) -> None:
    await harness.type_and_wait("hi")
    edit = harness.window.input_edit
    edit.selectAll()
    QTest.keyClick(edit, Qt.Key.Key_Backspace)
    await harness.flush()
    assert harness.last.text == ""
    assert not harness.presenter.debounce_timer.isActive()


async def test_nothing_is_sent_when_input_is_cleared_before_first_send(harness: Harness) -> None:
    edit = harness.window.input_edit
    QTest.keyClicks(edit, "a")
    QTest.keyClick(edit, Qt.Key.Key_Backspace)
    await asyncio.sleep(_PAUSE * 2)
    assert harness.sent == []


async def test_language_change_resends_immediately(harness: Harness) -> None:
    first = await harness.type_and_wait("hello")
    window = harness.window
    window.target_combo.setCurrentIndex(_combo_index(harness, "ja"))
    await harness.flush()  # 不等停顿
    assert len(harness.sent) == 2
    second = harness.last
    assert (second.text, second.source, second.target) == ("hello", None, Lang("ja"))
    assert second.session_id != first.session_id
    assert second.include_billing is False

    window.source_combo.setCurrentIndex(window.source_combo.findData("en"))
    await harness.flush()
    assert (harness.last.source, harness.last.target) == (Lang("en"), Lang("ja"))
    # 恢复为“自动”同样立即重发。
    window.target_combo.setCurrentIndex(0)
    await harness.flush()
    assert (harness.last.source, harness.last.target) == (Lang("en"), None)
    assert len(harness.sent) == 4


async def test_language_change_with_empty_input_only_records_choice(harness: Harness) -> None:
    harness.window.target_combo.setCurrentIndex(_combo_index(harness, "de"))
    await harness.flush()
    assert harness.sent == []
    assert harness.presenter.languages == (None, Lang("de"))
    command = await harness.type_and_wait("hello")
    assert command.target == Lang("de")


async def test_swap_languages(harness: Harness) -> None:
    window = harness.window
    window.source_combo.setCurrentIndex(window.source_combo.findData("en"))
    window.target_combo.setCurrentIndex(window.target_combo.findData("zh-Hans"))
    first = await harness.type_and_wait("hello")
    assert (first.source, first.target) == (Lang("en"), Lang("zh-Hans"))

    window.swap_button.click()
    await harness.flush()
    assert len(harness.sent) == 2
    assert (harness.last.source, harness.last.target) == (Lang("zh-Hans"), Lang("en"))
    assert window.languages() == (Lang("zh-Hans"), Lang("en"))

    # 一边是“自动”时照样交换：自动检测源语言、译成日语，交换后变成从日语译出、目标语言按互译对决定。
    window.source_combo.setCurrentIndex(0)
    window.target_combo.setCurrentIndex(window.target_combo.findData("ja"))
    await harness.flush()
    count = len(harness.sent)
    window.swap_button.click()
    await harness.flush()
    assert window.languages() == (Lang("ja"), None)
    assert (harness.last.source, harness.last.target) == (Lang("ja"), None)
    assert len(harness.sent) == count + 1


async def test_button_shortcuts_work_from_the_input(harness: Harness) -> None:
    """Ctrl+Shift+S 交换、Ctrl+Shift+T 置顶、Ctrl+Shift+L 清空，焦点在输入框里也生效，且不插入字符。"""
    window = harness.window
    ctrl_shift = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    window.source_combo.setCurrentIndex(window.source_combo.findData("en"))
    window.target_combo.setCurrentIndex(window.target_combo.findData("zh-Hans"))
    await harness.type_and_wait("hello")
    edit = window.input_edit

    QTest.keyClick(edit, Qt.Key.Key_S, ctrl_shift)
    await harness.flush()
    assert window.languages() == (Lang("zh-Hans"), Lang("en"))
    assert (harness.last.source, harness.last.target) == (Lang("zh-Hans"), Lang("en"))

    assert not window.pin_button.isChecked()
    QTest.keyClick(edit, Qt.Key.Key_T, ctrl_shift)
    assert window.pin_button.isChecked()
    QTest.keyClick(edit, Qt.Key.Key_T, ctrl_shift)
    assert not window.pin_button.isChecked()
    assert edit.toPlainText() == "hello", "快捷键不往输入框里插入字符"

    QTest.keyClick(edit, Qt.Key.Key_L, ctrl_shift)
    await harness.flush()
    assert edit.toPlainText() == ""
    assert harness.last.text == ""


async def test_swap_with_both_auto_does_nothing(harness: Harness) -> None:
    await harness.type_and_wait("hello")
    harness.window.swap_button.click()
    await harness.flush()
    assert len(harness.sent) == 1
    assert harness.window.languages() == (None, None)


async def test_debounce_comes_from_config_with_fallback(start_harness: StartHarness) -> None:
    harness = await start_harness(with_config=False)
    presenter = harness.presenter
    assert presenter.options.debounce_ms == DEFAULT_DEBOUNCE_MS
    assert presenter.debounce_timer.interval() == DEFAULT_DEBOUNCE_MS

    await harness.publish(config(debounce_ms=250, revision=2))
    assert presenter.debounce_timer.interval() == 250
    for invalid in ("fast", True, -1, 2.5, None):
        await harness.publish(config(debounce_ms=invalid, revision=3))
        assert presenter.debounce_timer.interval() == DEFAULT_DEBOUNCE_MS, invalid


async def test_session_ids_are_unique(harness: Harness) -> None:
    ids: set[SessionId] = set()
    for text in ("a", "b", "c"):
        ids.add((await harness.type_and_wait(text)).session_id)
    assert len(ids) == 3
