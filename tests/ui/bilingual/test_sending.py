"""输入停顿、立即翻译、清空、语言与服务切换时发出的 TranslateText（需求 BI-03、BI-04、BI-07）。"""

import asyncio
import re
import time

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from lst.contracts.events import EngineHealth
from lst.contracts.ids import EngineId, Lang, SessionId
from lst.contracts.translation import AttemptState
from lst.ui.bilingual.options import DEFAULT_DEBOUNCE_MS
from tests.ui.bilingual.conftest import (
    DEBOUNCE_MS,
    Harness,
    StartHarness,
    attempt,
    config,
    segment,
    statuses,
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
    assert (command.source, command.target, command.engine_id) == (None, None, EngineId("argos"))
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


async def test_ctrl_enter_sends_immediately(start_harness: StartHarness) -> None:
    harness = await start_harness(with_config=False)
    edit = harness.window.input_edit
    QTest.keyClicks(edit, "hello")
    assert harness.presenter.debounce_timer.isActive()
    QTest.keyClick(edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    await harness.flush()

    assert len(harness.sent) == 1
    assert (harness.last.text, harness.last.engine_id) == ("hello", EngineId("argos"))
    assert not harness.presenter.debounce_timer.isActive(), "Ctrl+Enter 先停止停顿计时器"
    assert edit.toPlainText() == "hello", "Ctrl+Enter 不往输入框里插入换行"
    await asyncio.sleep(DEFAULT_DEBOUNCE_MS / 1000 + 0.1)
    assert len(harness.sent) == 1


async def test_ctrl_enter_retranslates_same_text_in_a_new_session(harness: Harness) -> None:
    """文本没变时按 Ctrl+Enter 也重新翻译（例如上次失败后重试），用新的会话号。"""
    auto = await harness.type_and_wait("hello")
    # 小键盘的 Enter 同样有效。
    QTest.keyClick(
        harness.window.input_edit,
        Qt.Key.Key_Enter,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier,
    )
    await harness.flush()
    assert len(harness.sent) == 2
    assert harness.last.session_id != auto.session_id
    assert (harness.last.text, harness.last.engine_id) == ("hello", auto.engine_id)


async def test_ctrl_enter_starts_new_session_for_new_text(harness: Harness) -> None:
    auto = await harness.type_and_wait("hello")
    edit = harness.window.input_edit
    QTest.keyClicks(edit, " world")
    QTest.keyClick(edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    await harness.flush()
    assert len(harness.sent) == 2
    assert harness.last.session_id != auto.session_id
    assert harness.last.text == "hello world"
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
        segment("s1", "hello", attempt("argos", AttemptState.DONE, "你好", latency=80), shown_engine="argos"),
    )
    window = harness.window
    assert window.translation.segment_count() == 1

    window.clear_button.click()
    await harness.flush()
    assert len(harness.sent) == 2
    cleared = harness.last
    assert cleared.text == ""
    assert cleared.session_id != command.session_id
    assert window.input_edit.toPlainText() == ""
    assert window.translation.segment_count() == 0
    assert window.translation.toPlainText() == ""
    assert harness.presenter.translation_text() == ""

    # 旧会话迟到的刷新被丢弃。
    await harness.updates(
        command.session_id,
        segment(
            "s1", "hello", attempt("deepl", AttemptState.DONE, "您好", latency=300), shown_engine="deepl"
        ),
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


# ---------------------------------------------------------------- 翻译服务（BI-04）


async def test_engine_list_shows_enabled_services_by_priority(harness: Harness) -> None:
    """下拉框列出启用的服务，按优先级排列；计费的标“计费”，出错的标“出错”且不能选，未启用的不列出。"""
    await harness.publish(
        statuses(
            {"deepseek": "DeepSeek", "argos": "Argos", "google": "Google", "baidu": "百度"},
            billing={"deepseek", "baidu"},
            health={"google": EngineHealth.DISABLED, "baidu": EngineHealth.ERROR},
        )
    )
    combo = harness.window.engine_combo
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert labels == ["DeepSeek（计费）", "Argos", "百度（出错）"]
    model = combo.model()
    assert [model.index(i, 0).flags() & Qt.ItemFlag.ItemIsEnabled for i in range(3)] == [
        Qt.ItemFlag.ItemIsEnabled,
        Qt.ItemFlag.ItemIsEnabled,
        Qt.ItemFlag.NoItemFlags,
    ]
    # 用户还没有选过服务：用列表里第一个能用的，也就是优先级最高的那个。
    assert harness.presenter.preferred_engine is None
    assert combo.currentText() == "DeepSeek（计费）"
    assert harness.presenter.engine == EngineId("deepseek")


async def test_choosing_a_service_resends_and_is_remembered(start_harness: StartHarness) -> None:
    first = await start_harness()
    await first.publish(statuses({"argos": "Argos", "deepseek": "DeepSeek"}, billing={"deepseek"}))
    command = await first.type_and_wait("hello")
    assert command.engine_id == EngineId("argos")

    combo = first.window.engine_combo
    combo.setCurrentIndex(combo.findText("DeepSeek（计费）"))
    await first.flush()  # 不等停顿
    assert len(first.sent) == 2
    assert (first.last.text, first.last.engine_id) == ("hello", EngineId("deepseek"))
    assert first.last.session_id != command.session_id
    # 选了计费服务之后，输入停顿照样自动翻译，并且只交给它。
    QTest.keyClicks(first.window.input_edit, " there")
    await wait_until(lambda: len(first.sent) == 3)
    assert (first.last.text, first.last.engine_id) == ("hello there", EngineId("deepseek"))
    await first.stop()

    second = await start_harness(first.settings_path, with_engines=False)
    await second.publish(statuses({"argos": "Argos", "deepseek": "DeepSeek"}, billing={"deepseek"}))
    assert second.presenter.engine == EngineId("deepseek")
    assert second.window.engine_combo.currentText() == "DeepSeek（计费）"


async def test_unavailable_service_falls_back_with_notice(start_harness: StartHarness) -> None:
    """记住的服务被停用或出错：改用列表里第一个能用的，状态行说明；它恢复后自动换回。"""
    harness = await start_harness(with_engines=False)
    await harness.publish(statuses({"argos": "Argos", "deepl": "DeepL"}))
    combo = harness.window.engine_combo
    combo.setCurrentIndex(combo.findText("DeepL"))
    command = await harness.type_and_wait("hello")
    assert command.engine_id == EngineId("deepl")

    await harness.publish(
        statuses({"argos": "Argos", "deepl": "DeepL"}, health={"deepl": EngineHealth.DISABLED})
    )
    assert harness.presenter.engine == EngineId("argos")
    assert harness.presenter.preferred_engine == EngineId("deepl")
    assert combo.currentText() == "Argos"
    assert harness.presenter.status_text() == "“DeepL”现在不可用，暂时改用“Argos”。"
    assert harness.window.status_label.text() == harness.presenter.status_text()
    # 服务变了，当前文本立即按新服务重发。
    assert (harness.last.text, harness.last.engine_id) == ("hello", EngineId("argos"))

    await harness.publish(
        statuses({"argos": "Argos", "deepl": "DeepL"}, health={"deepl": EngineHealth.ERROR})
    )
    assert harness.presenter.engine == EngineId("argos")

    await harness.publish(statuses({"argos": "Argos", "deepl": "DeepL"}))
    assert harness.presenter.engine == EngineId("deepl")
    assert harness.presenter.status_text() == ""
    assert not harness.window.status_label.isVisibleTo(harness.window)
    assert harness.last.engine_id == EngineId("deepl")


async def test_no_available_service(start_harness: StartHarness) -> None:
    harness = await start_harness(with_engines=False)
    await harness.publish(statuses({"argos": "Argos"}, health={"argos": EngineHealth.DISABLED}))
    assert harness.window.engine_combo.count() == 0
    assert not harness.window.engine_combo.isEnabled()
    assert harness.presenter.status_text() == "没有可用的翻译服务，请在设置的“翻译引擎”页启用一个。"
    command = await harness.type_and_wait("hello")
    assert command.engine_id is None


async def test_session_ids_are_unique(harness: Harness) -> None:
    ids: set[SessionId] = set()
    for text in ("a", "b", "c"):
        ids.add((await harness.type_and_wait(text)).session_id)
    assert len(ids) == 3
