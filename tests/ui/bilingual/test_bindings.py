"""双语窗口的键位（需求 UI-09、BI-04，架构文档 11.8 节）：改键后旧键位失效、新键位生效，
打字不被键位吃掉，按钮提示随键位变化，配置变化立即生效。
默认键位下的操作见 test_sending.py、test_display.py 与 test_window.py。"""

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from lst.contracts.config import AppConfig
from lst.contracts.ids import Lang
from lst.contracts.worker_protocol import JsonValue
from tests.ui.bilingual.conftest import Harness, config

NONE = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


def with_bindings(revision: int = 2, /, **fields: JsonValue) -> AppConfig:
    sections = dict(config().sections)
    sections["bindings"] = dict(fields)
    return AppConfig(sections, revision)


async def _shown(harness: Harness, **fields: JsonValue) -> Harness:
    await harness.publish(with_bindings(**fields))
    harness.window.present()
    return harness


def middle_click(widget: QWidget) -> None:
    point = QPointF(5, 5)
    middle = Qt.MouseButton.MiddleButton
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else middle
        event = QMouseEvent(kind, point, widget.mapToGlobal(point), middle, buttons, NONE)
        QApplication.sendEvent(widget, event)


async def test_translate_rebound_to_f5(harness: Harness) -> None:
    await _shown(harness, bilingual_translate=["F5"])
    edit = harness.window.input_edit
    QTest.keyClicks(edit, "hello")
    QTest.keyClick(edit, Qt.Key.Key_Return, CTRL)
    await harness.flush()
    assert harness.sent == [], "Ctrl+Enter 不再立即翻译"
    QTest.keyClick(edit, Qt.Key.Key_F5)
    await harness.flush()
    assert harness.last.engine_id == "argos"
    assert harness.last.text == "hello"


async def test_binding_on_a_key_the_input_handles_itself(harness: Harness) -> None:
    """置顶改到 Ctrl+A：输入框里按 Ctrl+A 置顶，不再全选。"""
    await _shown(harness, bilingual_pin=["Ctrl+A"])
    edit = harness.window.input_edit
    QTest.keyClicks(edit, "hello")
    QTest.keyClick(edit, Qt.Key.Key_A, CTRL)
    assert harness.window.pin_button.isChecked()
    assert not edit.textCursor().hasSelection()
    QTest.keyClick(edit, Qt.Key.Key_T, CTRL | SHIFT)
    assert harness.window.pin_button.isChecked(), "Ctrl+Shift+T 不再切换置顶"


async def test_typing_is_not_swallowed_by_plain_key_bindings(harness: Harness) -> None:
    """交换改到不带修饰键的 S：输入框里打 s 照常输入；焦点在按钮上时按 S 才交换。"""
    await _shown(harness, bilingual_swap=["S"])
    window = harness.window
    window.source_combo.setCurrentIndex(window.source_combo.findData("en"))
    window.target_combo.setCurrentIndex(window.target_combo.findData("zh-Hans"))
    QTest.keyClicks(window.input_edit, "s")
    assert window.input_edit.toPlainText() == "s"
    assert window.languages() == (Lang("en"), Lang("zh-Hans"))
    window.pin_button.setFocus()
    QTest.keyClick(window.pin_button, Qt.Key.Key_S)
    assert window.languages() == (Lang("zh-Hans"), Lang("en"))


async def test_clear_and_close_rebound(harness: Harness) -> None:
    await _shown(harness, bilingual_clear=["Ctrl+Shift+X"], bilingual_close=["Ctrl+W"])
    window = harness.window
    edit = window.input_edit
    QTest.keyClicks(edit, "hello")
    QTest.keyClick(edit, Qt.Key.Key_L, CTRL | SHIFT)
    assert edit.toPlainText() == "hello"
    QTest.keyClick(edit, Qt.Key.Key_X, CTRL | SHIFT)
    assert edit.toPlainText() == ""
    QTest.keyClick(edit, Qt.Key.Key_Escape)
    assert window.isVisible(), "Esc 不再关闭窗口"
    QTest.keyClick(edit, Qt.Key.Key_W, CTRL)
    assert not window.isVisible(), "关闭窗口只隐藏，与原来的 Esc 相同"


async def test_mouse_binding_does_not_reach_the_widget(harness: Harness) -> None:
    """置顶改到中键单击：在译文区或清空按钮上中键单击都切换置顶，清空按钮本身不被触发。"""
    await _shown(harness, bilingual_pin=["MiddleClick"])
    window = harness.window
    QTest.keyClicks(window.input_edit, "hello")
    middle_click(window.translation.viewport())
    assert window.pin_button.isChecked()
    middle_click(window.clear_button)
    assert not window.pin_button.isChecked()
    assert window.input_edit.toPlainText() == "hello"
    QTest.mouseClick(window.clear_button, Qt.MouseButton.LeftButton)
    assert window.input_edit.toPlainText() == "", "没有绑定的左键照常点击按钮"


async def test_tooltips_and_placeholder_follow_bindings(harness: Harness) -> None:
    window = harness.window
    assert window.swap_button.toolTip() == "交换源语言与目标语言（Ctrl+Shift+S）"
    assert window.copy_button.toolTip() == "复制全部译文", "复制不在键位表里，提示不写键位"
    assert "Ctrl+Return" in window.input_edit.placeholderText()
    assert "Esc" in window.input_edit.placeholderText()
    await harness.publish(
        with_bindings(bilingual_swap=["F6", "Ctrl+Shift+S"], bilingual_pin=[], bilingual_close=[])
    )
    assert window.swap_button.toolTip() == "交换源语言与目标语言（F6 / Ctrl+Shift+S）"
    assert window.pin_button.toolTip() == "让窗口保持在最前"
    assert (
        window.input_edit.placeholderText() == "输入要翻译的文字，停顿后自动翻译。按 Ctrl+Return 立即翻译。"
    )


async def test_config_change_applies_while_shown(harness: Harness) -> None:
    window = harness.window
    window.present()
    edit = window.input_edit
    QTest.keyClicks(edit, "hello")
    await harness.publish(with_bindings(bilingual_clear=["F9"]))
    QTest.keyClick(edit, Qt.Key.Key_F9)
    assert edit.toPlainText() == ""
    await harness.publish(with_bindings(3))
    QTest.keyClicks(edit, "again")
    QTest.keyClick(edit, Qt.Key.Key_F9)
    assert edit.toPlainText() == "again", "恢复默认后 F9 不再清空"
    QTest.keyClick(edit, Qt.Key.Key_L, CTRL | SHIFT)
    assert edit.toPlainText() == ""
