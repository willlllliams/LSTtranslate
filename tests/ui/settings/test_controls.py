"""控件的测试：提交时机（立即与防抖）、用户输入不被配置刷新覆盖、热键录入、设置卡片的错误提示。"""

import asyncio

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from lst.contracts.worker_protocol import JsonValue
from lst.ui.settings.controls import (
    DEFAULT_DEBOUNCE_MS,
    ChoiceEditor,
    DirectoryEditor,
    FloatEditor,
    HotkeyEditor,
    IntEditor,
    PairEditor,
    SettingCard,
    SwitchEditor,
    TextAreaEditor,
    TextEditor,
    ValueEditor,
    hotkey_text,
)
from tests.ui.settings.conftest import wait_until
from tests.ui.settings.support import FakeDialogs

SHORT = 30

Mod = Qt.KeyboardModifier
Key = Qt.Key


class Spy:
    """记录编辑器发出的 committed 与 edited。"""

    def __init__(self, editor: ValueEditor) -> None:
        self.commits: list[JsonValue] = []
        self.edits = 0
        editor.committed.connect(self.commits.append)
        editor.edited.connect(self._edited)

    def _edited(self) -> None:
        self.edits += 1


def type_text(editor: TextEditor, text: str) -> None:
    """模拟用户键入：textEdited 只在用户修改时发出。"""
    editor.edit.setText(text)
    editor.edit.textEdited.emit(text)


def test_default_debounce_is_500_ms() -> None:
    assert DEFAULT_DEBOUNCE_MS == 500
    assert TextEditor()._debounce_ms == 500
    assert IntEditor(0, 10)._debounce_ms == 500


async def test_text_commits_500_ms_after_typing_stops(qapp: QApplication) -> None:
    editor = TextEditor()  # 默认防抖时间
    spy = Spy(editor)
    type_text(editor, "a")
    await asyncio.sleep(0.3)
    assert spy.commits == []
    type_text(editor, "ab")  # 继续输入：重新计时
    await asyncio.sleep(0.3)
    assert spy.commits == []
    assert editor.pending
    await wait_until(lambda: bool(spy.commits), timeout=2.0)
    assert spy.commits == ["ab"]
    assert spy.edits == 2
    assert not editor.pending


async def test_enter_or_focus_loss_commits_immediately() -> None:
    editor = TextEditor(debounce_ms=5000)
    spy = Spy(editor)
    type_text(editor, "x")
    editor.edit.editingFinished.emit()
    assert spy.commits == ["x"]
    assert not editor.pending
    editor.edit.editingFinished.emit()  # 没有待提交的内容：不重复提交
    assert spy.commits == ["x"]


async def test_flush_commits_pending_input_only() -> None:
    editor = TextEditor(debounce_ms=5000)
    spy = Spy(editor)
    editor.flush()
    assert spy.commits == []
    type_text(editor, "y")
    editor.flush()
    assert spy.commits == ["y"]


async def test_set_json_does_not_commit_or_mark_edited() -> None:
    editor = TextEditor(debounce_ms=SHORT)
    spy = Spy(editor)
    editor.set_json("loaded")
    await asyncio.sleep(0.1)
    assert (spy.commits, spy.edits) == ([], 0)
    assert editor.edit.text() == "loaded" and not editor.dirty


async def test_user_input_survives_config_refreshes_until_confirmed() -> None:
    """用户改过的内容（提交后可能被拒绝）不被别的设置项触发的刷新覆盖，直到配置里的值等于它。"""
    editor = TextEditor(debounce_ms=SHORT)
    editor.set_json("old")
    type_text(editor, "typed")
    editor.set_json("old")  # 配置刷新，但还没有这次修改
    assert editor.edit.text() == "typed" and editor.dirty
    editor.set_json("typed")  # 配置确认了
    assert not editor.dirty
    editor.set_json("external")  # 之后配置里的变化正常显示
    assert editor.edit.text() == "external"


async def test_reload_discards_user_input() -> None:
    editor = TextEditor(debounce_ms=5000)
    spy = Spy(editor)
    type_text(editor, "typed")
    editor.reload("fresh")
    assert editor.edit.text() == "fresh" and not editor.dirty and not editor.pending
    await asyncio.sleep(0.05)
    assert spy.commits == []


def test_text_editor_value_modes() -> None:
    plain = TextEditor()
    plain.set_json("abc")
    assert plain.json_value() == "abc"
    optional = TextEditor(optional=True)
    optional.set_json(None)
    assert optional.edit.text() == "" and optional.json_value() is None
    optional.edit.setText("  ")
    assert optional.json_value() is None
    as_list = TextEditor(as_list=True)
    as_list.set_json(["zh-Hans", "en"])
    assert as_list.edit.text() == "zh-Hans, en"
    as_list.edit.setText("ja,  ko ,")
    assert as_list.json_value() == ["ja", "ko"]
    as_list.edit.setText("")
    assert as_list.json_value() is None
    number = TextEditor(number=True)
    number.set_json(8.0)
    assert number.edit.text() == "8" and number.json_value() == 8.0
    number.edit.setText("")
    assert number.json_value() is None
    number.edit.setText("1.5")
    assert number.json_value() == 1.5


def test_password_editor_hides_input() -> None:
    from PySide6.QtWidgets import QLineEdit

    assert TextEditor(password=True).edit.echoMode() == QLineEdit.EchoMode.Password


async def test_text_area_commits_after_debounce() -> None:
    editor = TextAreaEditor(debounce_ms=SHORT)
    spy = Spy(editor)
    editor.set_json("提示词")
    assert editor.edit.toPlainText() == "提示词" and spy.commits == []
    editor.edit.setPlainText("新的提示词")
    await wait_until(lambda: bool(spy.commits))
    assert spy.commits == ["新的提示词"]
    editor.edit.setPlainText("   ")
    assert editor.json_value() is None
    assert editor.edit.tabChangesFocus()


def test_switch_commits_immediately_and_label_follows() -> None:
    editor = SwitchEditor()
    spy = Spy(editor)
    editor.set_json(True)
    assert editor.switch.isChecked() and editor.state_label.text() == "开" and spy.commits == []
    editor.switch.click()
    assert spy.commits == [False] and editor.state_label.text() == "关" and spy.edits == 1


def test_switch_toggles_with_the_space_key() -> None:
    editor = SwitchEditor()
    spy = Spy(editor)
    editor.show()
    editor.switch.setFocus()
    QTest.keyClick(editor.switch, Key.Key_Space)
    assert spy.commits == [True]
    editor.close()


def test_choice_commits_immediately_and_keeps_unknown_values() -> None:
    editor = ChoiceEditor([("a", "甲"), ("b", "乙")])
    spy = Spy(editor)
    editor.set_json("b")
    assert editor.combo.currentText() == "乙" and spy.commits == []
    editor.combo.setCurrentIndex(0)
    assert spy.commits == ["a"]
    editor.reload("zz")  # 列表里没有的取值：照实显示（reload 丢弃用户刚做的选择）
    assert editor.combo.currentText() == "zz" and editor.json_value() == "zz"
    assert spy.commits == ["a"]


def test_choice_relabel_keeps_selection_without_commit() -> None:
    editor = ChoiceEditor([("a", "甲"), ("b", "乙")])
    spy = Spy(editor)
    editor.set_json("b")
    editor.set_options([("a", "A"), ("b", "B")])
    assert editor.combo.currentText() == "B" and editor.json_value() == "b"
    assert spy.commits == [] and spy.edits == 0


def test_pair_commits_the_whole_pair() -> None:
    editor = PairEditor()
    spy = Spy(editor)
    editor.set_json(["ja", "en"])
    assert editor.json_value() == ["ja", "en"] and spy.commits == []
    editor.second.setCurrentIndex(editor.second.findData("ko"))
    assert spy.commits == [["ja", "ko"]]
    editor.reload(["xx", "yy"])  # 列表外的语言码照实显示
    assert editor.json_value() == ["xx", "yy"]


async def test_int_editor_debounces_and_flushes_on_finish() -> None:
    editor = IntEditor(100, 5000, suffix=" 毫秒", debounce_ms=SHORT)
    spy = Spy(editor)
    editor.set_json(400)
    assert editor.spin.value() == 400 and spy.commits == []
    editor.spin.setValue(900)
    assert spy.commits == [] and editor.pending
    await wait_until(lambda: bool(spy.commits))
    assert spy.commits == [900]
    assert editor.spin.suffix() == " 毫秒"
    assert editor.spin.minimum() == 100 and editor.spin.maximum() == 5000

    slow = IntEditor(0, 10, debounce_ms=5000)
    slow_spy = Spy(slow)
    slow.spin.setValue(3)
    slow.spin.editingFinished.emit()
    assert slow_spy.commits == [3]


def test_int_editor_special_value_text_for_zero() -> None:
    editor = IntEditor(0, 16384, special_zero="不缩放")
    editor.set_json(0)
    assert editor.spin.text() == "不缩放"
    plain = IntEditor(1, 32, special_zero="不缩放")
    assert plain.spin.specialValueText() == ""  # 只有最小值是 0 时才有意义


async def test_float_editor() -> None:
    editor = FloatEditor(0.01, 600.0, debounce_ms=SHORT)
    spy = Spy(editor)
    editor.set_json(8.0)
    editor.spin.setValue(12.5)
    await wait_until(lambda: bool(spy.commits))
    assert spy.commits == [12.5]


def test_directory_editor_browse_commits_the_chosen_folder() -> None:
    dialogs = FakeDialogs(directory=r"D:\models")
    editor = DirectoryEditor(dialogs, debounce_ms=5000)
    spy = Spy(editor)
    editor.set_json("")
    editor.browse()
    assert spy.commits == [r"D:\models"] and editor.edit.text() == r"D:\models"
    assert dialogs.directories[-1][1] == ""
    dialogs.directory = None  # 用户取消
    editor.browse()
    assert spy.commits == [r"D:\models"]


# ---------------------------------------------------------------- 热键


def test_hotkey_text_canonical_order_and_names() -> None:
    all_mods = Mod.MetaModifier | Mod.ShiftModifier | Mod.AltModifier | Mod.ControlModifier
    assert hotkey_text(int(Key.Key_Q.value), Mod.AltModifier) == "Alt+Q"
    assert hotkey_text(int(Key.Key_Q.value), all_mods) == "Ctrl+Alt+Shift+Win+Q"
    assert hotkey_text(int(Key.Key_5.value), Mod.ControlModifier) == "Ctrl+5"
    assert hotkey_text(int(Key.Key_F5.value), Mod.ShiftModifier) == "Shift+F5"
    assert hotkey_text(int(Key.Key_Space.value), Mod.ControlModifier) == "Ctrl+Space"
    assert hotkey_text(int(Key.Key_Return.value), Mod.AltModifier) == "Alt+Enter"
    assert hotkey_text(int(Key.Key_PageUp.value), Mod.ControlModifier) == "Ctrl+PageUp"
    assert hotkey_text(int(Key.Key_Print.value), Mod.ControlModifier) == "Ctrl+PrintScreen"
    assert hotkey_text(int(Key.Key_QuoteLeft.value), Mod.ControlModifier) == "Ctrl+`"
    # 只按修饰键：等待主键。
    assert hotkey_text(int(Key.Key_Control.value), Mod.ControlModifier) is None
    assert hotkey_text(int(Key.Key_Alt.value), Mod.AltModifier) is None


def hotkey_editor() -> HotkeyEditor:
    editor = HotkeyEditor()
    editor.retranslate()
    editor.show()
    editor.button.setFocus()
    return editor


def test_hotkey_recording_with_the_keyboard_only() -> None:
    editor = hotkey_editor()
    spy = Spy(editor)
    editor.set_json("Alt+Q")
    assert editor.button.text() == "Alt+Q" and spy.commits == []

    QTest.keyClick(editor.button, Key.Key_Return)  # 键盘进入录入
    assert editor.button.recording
    assert "按下新的快捷键" in editor.button.text()
    QTest.keyClick(editor.button, Key.Key_Control, Mod.ControlModifier)  # 只按修饰键：继续等
    assert editor.button.recording and spy.commits == []
    QTest.keyClick(editor.button, Key.Key_K, Mod.ControlModifier | Mod.AltModifier)
    assert not editor.button.recording
    assert spy.commits == ["Ctrl+Alt+K"] and editor.button.text() == "Ctrl+Alt+K" and spy.edits == 1
    editor.close()


def test_hotkey_recording_with_a_click_and_escape_cancels() -> None:
    editor = hotkey_editor()
    spy = Spy(editor)
    editor.set_json("Alt+E")
    editor.button.click()
    assert editor.button.recording
    QTest.keyClick(editor.button, Key.Key_Escape)
    assert not editor.button.recording and editor.button.text() == "Alt+E" and spy.commits == []
    editor.close()


def test_hotkey_recording_swallows_tab_and_alt_shortcuts() -> None:
    editor = hotkey_editor()
    spy = Spy(editor)
    editor.button.click()
    QTest.keyClick(editor.button, Key.Key_Tab, Mod.ControlModifier)
    assert spy.commits == ["Ctrl+Tab"]  # Tab 在录入状态下是普通按键，被录入而不是移动焦点
    assert not editor.button.recording
    # 不在录入状态时，Tab 照常移动焦点（键盘用户不会被困住）：控件不处理它。
    editor.button.click()
    editor.button.stop_recording()
    assert not editor.button.recording
    editor.close()


def test_hotkey_recording_ignores_auto_repeat_and_loses_on_focus_out() -> None:
    editor = hotkey_editor()
    editor.button.click()
    assert editor.button.recording
    QApplication.sendEvent(editor.button, QFocusEvent(QEvent.Type.FocusOut))
    assert not editor.button.recording
    editor.close()


def test_hotkey_without_modifier_is_still_submitted_for_the_service_to_reject() -> None:
    editor = hotkey_editor()
    spy = Spy(editor)
    editor.button.click()
    QTest.keyClick(editor.button, Key.Key_Q)
    assert spy.commits == ["Q"]
    editor.close()


# ---------------------------------------------------------------- 设置卡片


def test_card_shows_and_clears_an_error_next_to_the_control() -> None:
    editor = SwitchEditor()
    card = SettingCard(editor, location=("race", "mode"))
    card.set_texts("结束方式", "说明文字")
    card.show()
    assert card.title_label.text() == "结束方式" and card.desc_label.text() == "说明文字"
    assert card.error_text == "" and card.error_label.isHidden()
    card.set_error("不是可选值")
    assert card.error_text == "未保存：不是可选值"
    assert not card.error_label.isHidden()
    # 错误提示与控件在同一张卡片里，位于控件所在行之下。
    assert card.error_label.geometry().top() > editor.geometry().top()
    card.set_error(None)
    assert card.error_text == "" and card.error_label.isHidden()
    card.close()


def test_card_names_the_focus_widget_for_accessibility() -> None:
    editor = TextEditor()
    card = SettingCard(editor)
    card.set_texts("接口地址", "OpenAI 兼容接口的地址")
    assert editor.edit.accessibleName() == "接口地址"
    assert editor.edit.accessibleDescription() == "OpenAI 兼容接口的地址"
    card.set_texts("接口地址", "")
    assert card.desc_label.isHidden()


@pytest.mark.parametrize("stacked", [False, True])
def test_card_layouts(stacked: bool) -> None:
    editor = TextEditor()
    card = SettingCard(editor, stacked=stacked)
    card.set_texts("标题", "说明")
    card.resize(600, 200)
    card.show()
    QApplication.processEvents()
    if stacked:
        assert editor.geometry().top() >= card.desc_label.geometry().bottom()
    else:
        assert editor.geometry().left() > card.title_label.geometry().left()
    card.close()
