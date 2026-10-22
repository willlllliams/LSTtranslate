"""键盘操作的测试（需求 UI-07）：Tab 顺序与视觉顺序一致、按钮带助记键、控件都能用键盘到达和操作。"""

from collections.abc import Iterator
from itertools import pairwise
from typing import cast

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QWidget,
)

from lst.contracts.events import EngineHealth, EngineStatus, EngineStatuses
from lst.contracts.ids import EngineId
from lst.contracts.ocr_engines import (
    OcrEngineCatalog,
    OcrEngineInfo,
    OcrEngineKind,
    OcrEngineState,
    OcrStage,
)
from lst.ui import i18n
from lst.ui.settings.controls import ChoiceEditor, HotkeyButton, ToggleSwitch
from lst.ui.settings.page_engines import AddEngineDialog, EnginesPage
from lst.ui.settings.page_ocr_engine import OcrEnginePage
from lst.ui.settings.pages import SettingsPage
from lst.ui.settings.window import SettingsWindow
from tests.ui.settings.conftest import Harness
from tests.ui.settings.support import default_config

ROW_TOLERANCE = 14
"""两个控件的顶边相差不超过这个像素数，就算在同一行。"""


def tab_stops(window: SettingsWindow, scope: QWidget) -> list[QWidget]:
    """窗口里 scope 之内、按 Tab 依次到达的控件（按焦点链的顺序，只含可见、可用、接受 Tab 焦点的）。"""
    stops: list[QWidget] = []
    start = window.nav
    widget = start.nextInFocusChain()
    seen = {id(start)}
    while widget is not None and id(widget) not in seen:
        seen.add(id(widget))
        if (
            scope.isAncestorOf(widget)
            and widget.isVisibleTo(window)
            and widget.isEnabled()
            and widget.focusPolicy() & Qt.FocusPolicy.TabFocus
            and widget.focusProxy() is None
            and not isinstance(widget.parent(), QAbstractSpinBox)
        ):
            stops.append(widget)
        widget = widget.nextInFocusChain()
    return stops


def position(window: QWidget, widget: QWidget) -> tuple[int, int]:
    point = widget.mapTo(window, QPoint(0, 0))
    return (point.y(), point.x())


def assert_visual_order(window: QWidget, stops: list[QWidget], where: str) -> None:
    """相邻两个控件：后一个不在前一个上方；在同一行时后一个在右边。"""
    for first, second in pairwise(stops):
        (y1, x1), (y2, x2) = position(window, first), position(window, second)
        label = f"{where}: {type(first).__name__}@({x1},{y1}) → {type(second).__name__}@({x2},{y2})"
        assert y2 >= y1 - ROW_TOLERANCE, label
        if abs(y2 - y1) <= ROW_TOLERANCE:
            assert x2 >= x1, label


@pytest.fixture
async def shown(harness: Harness) -> SettingsWindow:
    await harness.publish(
        EngineStatuses({EngineId("deepseek"): EngineStatus(EngineHealth.READY)}, {}, {}),
        default_config(revision=2),
        OcrEngineCatalog(
            tuple(
                OcrEngineInfo(kind, OcrEngineState.INSTALLED, needs_download=False, languages=("en",))
                for kind in OcrEngineKind
            ),
            OcrEngineKind.WINDOWS,
        ),
    )
    window = await harness.open()
    window.resize(980, 760)
    QApplication.processEvents()
    return window


def pages_with_selection(window: SettingsWindow) -> Iterator[SettingsPage]:
    for page in window.pages:
        window.show_page(page.page_id)
        if isinstance(page, EnginesPage):
            page.select("deepseek")
        QApplication.processEvents()
        yield page


async def test_tab_order_follows_the_visual_order_on_every_page(shown: SettingsWindow) -> None:
    for page in pages_with_selection(shown):
        stops = tab_stops(shown, page)
        if page.page_id == "models.dicts":
            # 词典在没有模型目录时列表不显示，页面上只有说明文字。
            assert not stops
            continue
        assert stops, f"{page.page_id} 页没有可以用键盘到达的控件"
        assert_visual_order(shown, stops, page.page_id)


async def test_tab_order_on_the_engines_page_covers_list_buttons_and_form(shown: SettingsWindow) -> None:
    shown.show_page("engines")
    page = shown.page("engines")
    assert isinstance(page, EnginesPage)
    page.select("deepseek")
    QApplication.processEvents()
    stops = tab_stops(shown, page)
    # 列表在前，然后是五个按钮，最后才是参数表单。
    assert stops[0] is page.list
    assert stops[1:6] == [
        page.add_button,
        page.remove_button,
        page.up_button,
        page.down_button,
        page.test_button,
    ]
    assert page.secret_field is not None and page.secret_field.editor.edit in stops
    assert stops.index(page.secret_field.editor.edit) < stops.index(page.secret_field.clear_button) or (
        not page.secret_field.clear_button.isEnabled()
    )


async def test_navigation_comes_first_then_the_page(shown: SettingsWindow) -> None:
    shown.show_page("race")
    first = tab_stops(shown, shown)[0]  # 导航列表之后的第一个 Tab 停靠点
    assert shown.page("race").isAncestorOf(first)


async def test_the_nav_list_works_with_arrow_keys(shown: SettingsWindow) -> None:
    shown.show_page("general")
    shown.nav.setFocus()
    QTest.keyClick(shown.nav, Qt.Key.Key_Down)
    assert shown.current_page.page_id == "hotkeys.global"
    QTest.keyClick(shown.nav, Qt.Key.Key_Down)
    assert shown.current_page.page_id == "hotkeys.capture"
    QTest.keyClick(shown.nav, Qt.Key.Key_Up)
    assert shown.current_page.page_id == "hotkeys.global"


INTERACTIVE = (
    QAbstractButton,
    QComboBox,
    QAbstractSpinBox,
    QLineEdit,
    QPlainTextEdit,
    QAbstractItemView,
)


def interactive_widgets(page: QWidget) -> list[QWidget]:
    found: list[QWidget] = []
    for widget in page.findChildren(QWidget):
        if not isinstance(widget, INTERACTIVE) or not widget.isVisibleTo(page):
            continue
        parent = widget.parent()
        if isinstance(parent, QAbstractSpinBox | QComboBox):  # 数值框、下拉框内部的文本框
            continue
        if widget.objectName().startswith("qt_"):  # Qt 内部的子控件（滚动条按钮等）
            continue
        found.append(widget)
    return found


async def test_every_interactive_control_accepts_tab_focus(shown: SettingsWindow) -> None:
    for page in pages_with_selection(shown):
        for widget in interactive_widgets(page):
            if isinstance(widget, QAbstractItemView) and widget.focusPolicy() == Qt.FocusPolicy.NoFocus:
                continue
            proxy = widget.focusProxy() or widget
            assert proxy.focusPolicy() & Qt.FocusPolicy.TabFocus or widget.focusProxy() is not None, (
                page.page_id,
                type(widget).__name__,
                widget.accessibleName(),
            )


async def test_every_input_control_has_an_accessible_name(shown: SettingsWindow) -> None:
    for page in pages_with_selection(shown):
        for widget in interactive_widgets(page):
            if isinstance(widget, QAbstractButton) and widget.text():
                continue
            if isinstance(widget, QAbstractItemView) and not widget.accessibleName():
                continue  # 列表类控件的名称单独检查
            proxy = widget.focusProxy()
            name = widget.accessibleName() or (proxy.accessibleName() if proxy is not None else "")
            if isinstance(widget, HotkeyButton):
                name = widget.accessibleName()
            assert name, (page.page_id, type(widget).__name__)


@pytest.mark.parametrize("language", ["zh-Hans", "en"])
async def test_buttons_have_unique_mnemonics_within_a_page(harness: Harness, language: str) -> None:
    await harness.publish(default_config({"ui": {"language": language}}, revision=5))
    window = await harness.open()
    assert i18n.current_language() == language
    for page in pages_with_selection(window):
        mnemonics: dict[str, str] = {}
        for button in page.findChildren(QAbstractButton):
            if not button.isVisibleTo(page) or isinstance(button, ToggleSwitch | HotkeyButton):
                continue
            if button.objectName().startswith("qt_") or not button.text():
                continue
            key = QKeySequence.mnemonic(button.text()).toString()
            assert key, f"{page.page_id}: 按钮“{button.text()}”没有助记键"
            assert key not in mnemonics, (
                f"{page.page_id}: {key} 同时给了“{mnemonics[key]}”和“{button.text()}”"
            )
            mnemonics[key] = button.text()


async def test_chinese_mnemonics_use_the_bracket_form(harness: Harness) -> None:
    window = await harness.open()
    page = window.page("engines")
    assert isinstance(page, EnginesPage)
    assert page.add_button.text() == "添加(&A)…" and page.remove_button.text() == "删除(&D)"
    assert QKeySequence.mnemonic(page.add_button.text()).toString() == "Alt+A"


async def test_dropdown_changes_with_arrow_keys(harness: Harness) -> None:
    window = await harness.open()
    editor = window.page("general").bindings[1].editor
    assert isinstance(editor, ChoiceEditor)
    editor.combo.setFocus()
    QTest.keyClick(editor.combo, Qt.Key.Key_Down)
    await harness.flush()
    assert [c.patch for c in harness.recorded.updates] == [{"ui": {"theme": "light"}}]


async def test_switch_works_with_the_space_key(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("capture.paddle")
    page = window.page("capture.paddle")
    assert isinstance(page, OcrEnginePage)
    switch = page.pipeline.toggles[OcrStage.FORMULA].editor.focus_widget()
    assert isinstance(switch, ToggleSwitch)
    switch.setFocus()
    QTest.keyClick(switch, Qt.Key.Key_Space)
    await harness.flush()
    assert [c.patch for c in harness.recorded.updates] == [{"ocr": {"formula": True}}]


async def test_engine_list_toggles_with_space_and_reorders_with_buttons(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("engines")
    page = window.page("engines")
    assert isinstance(page, EnginesPage)
    page.select("deepl")
    page.list.setFocus()
    QTest.keyClick(page.list, Qt.Key.Key_Space)  # 勾选框用空格切换：停用
    await harness.flush()
    entries = harness.recorded.updates[-1].patch["engines"]
    assert isinstance(entries, list) and isinstance(entries[2], dict) and entries[2]["enabled"] is False
    page.up_button.click()  # 没有鼠标也能调整优先级
    await harness.flush()
    assert len(harness.recorded.updates) == 2


async def test_add_dialog_is_keyboard_complete(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("engines")
    page = window.page("engines")
    assert isinstance(page, EnginesPage)
    page.add_button.click()
    dialog = page.add_dialog
    assert isinstance(dialog, AddEngineDialog)
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("deepl"))
    assert dialog.add_button.isDefault()
    dialog.show()
    QApplication.processEvents()
    QTest.keyClick(dialog.name_edit, Qt.Key.Key_Return)  # Enter 触发默认按钮“添加”
    await harness.flush()
    assert page.add_dialog is None
    entries = harness.recorded.updates[-1].patch["engines"]
    assert isinstance(entries, list) and len(entries) == 5

    page.add_button.click()
    dialog = page.add_dialog
    assert isinstance(dialog, AddEngineDialog)
    dialog.show()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)  # Esc 取消
    assert page.add_dialog is None


async def test_add_dialog_tab_order_follows_the_visual_order(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("engines")
    page = window.page("engines")
    assert isinstance(page, EnginesPage)
    page.add_button.click()
    dialog = page.add_dialog
    assert isinstance(dialog, AddEngineDialog)
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("openai_compat"))
    dialog.resize(520, 420)
    dialog.show()
    QApplication.processEvents()
    chain: list[QWidget] = []
    widget: QWidget = dialog.kind_combo
    while True:
        if (
            widget.isVisibleTo(dialog)
            and widget.isEnabled()
            and widget.focusPolicy() & Qt.FocusPolicy.TabFocus
        ):
            chain.append(widget)
        widget = cast(QWidget, widget.nextInFocusChain())
        if widget is dialog.kind_combo:
            break
    inside = [w for w in chain if dialog.isAncestorOf(w) and w.focusProxy() is None]
    assert inside[0] is dialog.kind_combo and inside[-2:] == [dialog.add_button, dialog.cancel_button]
    assert_visual_order(dialog, inside, "添加对话框")


async def test_hotkey_control_does_not_trap_the_keyboard(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("hotkeys")
    editor = window.page("hotkeys").bindings[0].editor
    button = editor.focus_widget()
    assert isinstance(button, HotkeyButton)
    assert not button.recording  # 平时 Tab 照常移动焦点；只有进入录入状态才接管按键
    assert button.focusPolicy() & Qt.FocusPolicy.TabFocus
