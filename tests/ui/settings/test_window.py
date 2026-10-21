"""设置窗口模块与窗口的测试：OpenSettings 打开与再次打开、导航、窗口状态、界面语言切换。"""

import asyncio
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt
from PySide6.QtTest import QTest

from lst.contracts.config import ConfigRejected
from lst.kernel import Module
from lst.ui import i18n
from lst.ui.settings import SettingsModule
from lst.ui.settings.controls import ChoiceEditor, DirectoryEditor
from lst.ui.settings.page_engines import EnginesPage
from lst.ui.settings.pages import RacePage
from lst.ui.settings.state import GROUP, WindowStateStore, fit_on_screens
from lst.ui.settings.window import SettingsWindow, describe_location
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import all_models, default_config, engine_kinds

PAGE_IDS = ["general", "hotkeys", "capture", "engines", "race", "models", "about"]
ZH_NAMES = ["常规", "热键", "截屏与识别", "翻译引擎", "竞速", "模型", "关于与诊断"]
EN_NAMES = [
    "General",
    "Hotkeys",
    "Capture & recognition",
    "Translation engines",
    "Racing",
    "Models",
    "About & diagnostics",
]


def engines_page(window: SettingsWindow) -> EnginesPage:
    page = window.page("engines")
    assert isinstance(page, EnginesPage)
    return page


def race_page(window: SettingsWindow) -> RacePage:
    page = window.page("race")
    assert isinstance(page, RacePage)
    return page


def nav_texts(window: SettingsWindow) -> list[str]:
    return [window.nav.item(row).text() for row in range(window.nav.count())]


def test_module_satisfies_the_protocol(settings_path: Path) -> None:
    module: Module = SettingsModule(settings_path, sections=all_models(), engine_kinds=engine_kinds())
    assert module.name == "ui.settings"


async def test_window_is_created_hidden_and_opened_by_the_command(harness: Harness) -> None:
    assert harness.window is not None
    assert not harness.window.isVisible()
    window = await harness.open()
    assert window.isVisible()
    assert window.windowTitle() == "设置"


async def test_opening_again_keeps_one_window_and_brings_it_forward(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("race")
    await harness.open()
    assert harness.window is window and window.isVisible()
    assert window.current_page.page_id == "race"  # 再次打开不改变当前页面
    assert window.nav.hasFocus() or window.nav.focusPolicy() != Qt.FocusPolicy.NoFocus


async def test_open_after_close_and_after_minimize(harness: Harness) -> None:
    window = await harness.open()
    window.close()  # 标题栏的关闭按钮只隐藏
    assert not window.isVisible()
    await harness.open()
    assert window.isVisible()
    window.showMinimized()
    assert window.isMinimized()
    await harness.open()
    assert not window.isMinimized() and window.isVisible()


async def test_close_hides_but_does_not_quit_the_application(harness: Harness) -> None:
    window = await harness.open()
    assert not window.testAttribute(Qt.WidgetAttribute.WA_QuitOnClose)
    window.close()
    assert not window.isVisible()
    assert harness.module.window is window  # 没有被销毁


async def test_stop_closes_for_good_and_ignores_later_commands(start_settings: StartSettings) -> None:
    harness = await start_settings()
    window = await harness.open()
    await harness.stop()
    assert harness.module.window is None and harness.module.presenter is None
    assert not window.isVisible()


async def test_start_twice_is_an_error(harness: Harness) -> None:
    with pytest.raises(RuntimeError, match="已经启动"):
        await harness.module.start(harness.ctx)


async def test_seven_pages_in_the_documented_order(harness: Harness) -> None:
    window = harness.window
    assert [page.page_id for page in window.pages] == PAGE_IDS
    assert nav_texts(window) == ZH_NAMES
    assert window.nav.count() == window.stack.count() == 7
    for row, name in enumerate(ZH_NAMES):
        assert window.pages[row].heading.text() == name
        assert not window.nav.item(row).icon().isNull()  # 每个导航项都有 Fluent 图标
        assert f"Ctrl+{row + 1}" in window.nav.item(row).toolTip()


async def test_navigation_switches_the_stack(harness: Harness) -> None:
    window = await harness.open()
    for row, page_id in enumerate(PAGE_IDS):
        window.nav.setCurrentRow(row)
        assert window.stack.currentWidget() is window.page(page_id)
    window.show_page("hotkeys")
    assert window.nav.currentRow() == 1


async def test_keyboard_navigation_between_pages(harness: Harness) -> None:
    window = await harness.open()
    QTest.keyClick(window, Qt.Key.Key_3, Qt.KeyboardModifier.ControlModifier)
    assert window.current_page.page_id == "capture"
    QTest.keyClick(window, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
    assert window.current_page.page_id == "engines"
    QTest.keyClick(
        window,
        Qt.Key.Key_Tab,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )
    assert window.current_page.page_id == "capture"
    QTest.keyClick(window, Qt.Key.Key_9, Qt.KeyboardModifier.ControlModifier)  # 没有第九页：不变
    assert window.current_page.page_id == "capture"
    window.show_page("general")
    QTest.keyClick(
        window, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    )
    assert window.current_page.page_id == "about"  # 循环


async def test_escape_hides_the_window(harness: Harness) -> None:
    window = await harness.open()
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert not window.isVisible()


# ---------------------------------------------------------------- 窗口状态


def read_group(path: Path) -> dict[str, str]:
    settings = QSettings(str(path), QSettings.Format.IniFormat)
    settings.beginGroup(GROUP)
    return {key: str(settings.value(key)) for key in settings.childKeys()}


async def test_geometry_and_page_are_saved_in_their_own_group(
    start_settings: StartSettings, settings_path: Path
) -> None:
    other = QSettings(str(settings_path), QSettings.Format.IniFormat)
    other.setValue("bilingual_window/x", 11)  # 与双语窗口、引导共用一个文件
    other.setValue("onboarding/completed", True)
    other.sync()

    harness = await start_settings()
    window = await harness.open()
    window.show_page("race")
    window.resize(QSize(900, 600))
    window.move(QPoint(120, 90))
    window.hide()

    saved = read_group(settings_path)
    assert (saved["width"], saved["height"], saved["page"]) == ("900", "600", "4")
    assert (saved["x"], saved["y"]) == ("120", "90")
    check = QSettings(str(settings_path), QSettings.Format.IniFormat)
    assert check.value("bilingual_window/x") is not None and check.value("onboarding/completed") is not None


async def test_state_is_restored_after_restart(start_settings: StartSettings) -> None:
    first = await start_settings()
    window = await first.open()
    window.show_page("models")
    window.resize(QSize(1000, 640))
    window.move(QPoint(50, 40))
    await first.stop()

    second = await start_settings()
    reopened = await second.open()
    assert reopened.size() == QSize(1000, 640)
    assert reopened.pos() == QPoint(50, 40)
    assert reopened.current_page.page_id == "models"


async def test_window_saved_off_screen_is_moved_back(
    start_settings: StartSettings, settings_path: Path
) -> None:
    saved = QSettings(str(settings_path), QSettings.Format.IniFormat)
    for key, value in (("x", 99000), ("y", 99000), ("width", 900), ("height", 600), ("page", 0)):
        saved.setValue(f"{GROUP}/{key}", value)
    saved.sync()
    harness = await start_settings()
    window = await harness.open()
    from PySide6.QtGui import QGuiApplication

    screens = [screen.availableGeometry() for screen in QGuiApplication.screens()]
    assert any(screen.contains(window.frameGeometry().center()) for screen in screens)


def test_garbage_in_the_state_file_is_ignored(tmp_path: Path) -> None:
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    for key, value in (("x", "abc"), ("width", "-5"), ("height", "10"), ("page", "x")):
        settings.setValue(f"{GROUP}/{key}", value)
    state = WindowStateStore(settings).load()
    assert (state.position, state.size, state.page) == (None, None, None)


def test_fit_on_screens() -> None:
    screen = QRect(0, 0, 1920, 1040)
    inside = QRect(100, 100, 800, 600)
    assert fit_on_screens(inside, [screen], screen) == inside
    outside = QRect(5000, 100, 800, 600)
    moved = fit_on_screens(outside, [screen], screen)
    assert screen.contains(moved) and moved.size() == outside.size()
    huge = fit_on_screens(QRect(5000, 0, 4000, 3000), [screen], screen)
    assert huge.size() == screen.size()


# ---------------------------------------------------------------- 界面语言


async def test_language_switch_refreshes_every_text_immediately(harness: Harness) -> None:
    window = await harness.open()
    assert nav_texts(window) == ZH_NAMES
    zh_engine_buttons = (
        engines_page(window).add_button.text(),
        race_page(window).stats.reset_button.text(),
    )

    await harness.publish(default_config({"ui": {"language": "en"}}, revision=2))
    assert i18n.current_language() == "en"
    assert window.windowTitle() == "Settings"
    assert nav_texts(window) == EN_NAMES
    assert window.title_label.text() == "Settings"
    assert [page.heading.text() for page in window.pages] == EN_NAMES
    assert window.page("general").bindings[0].card.title_label.text() == "Interface language"
    assert engines_page(window).add_button.text() == "&Add…"
    assert race_page(window).stats.reset_button.text() == "&Reset statistics"
    assert (
        engines_page(window).add_button.text(),
        race_page(window).stats.reset_button.text(),
    ) != zh_engine_buttons
    assert window.page("hotkeys").bindings[0].card.title_label.text() == "Capture and translate"
    # 下拉框的显示名也随语言变化，选中的取值不变。
    theme = window.page("general").bindings[1].editor
    assert isinstance(theme, ChoiceEditor)
    assert theme.combo.currentText() == "Follow system" and theme.json_value() == "system"
    assert "Ctrl+3" in window.nav.item(2).toolTip()

    await harness.publish(default_config({"ui": {"language": "zh-Hans"}}, revision=3))
    assert nav_texts(window) == ZH_NAMES
    assert theme.combo.currentText() == "跟随系统"
    assert engines_page(window).add_button.text() == "添加(&A)…"


async def test_every_page_has_texts_in_both_languages(harness: Harness) -> None:
    """两种语言下所有可见文字都已登记：没有退回成文案键。"""
    from PySide6.QtWidgets import QLabel

    window = harness.window
    for language in ("zh-Hans", "en"):
        await harness.publish(default_config({"ui": {"language": language}}, revision=10 + len(language)))
        for page in window.pages:
            for label in page.findChildren(QLabel):
                assert not label.text().startswith("settings."), (language, page.page_id, label.text())


# ---------------------------------------------------------------- 提示条与防抖提交


async def test_unclaimed_rejections_appear_in_the_notice_bar(harness: Harness) -> None:
    window = await harness.open()
    assert window.notice_label.isHidden()
    await harness.publish(ConfigRejected(("race", "mode"), "不是可选值"))  # 有卡片负责：不进提示条
    assert window.notice_label.isHidden()
    await harness.publish(ConfigRejected(("nope",), "未知的配置节"))
    assert not window.notice_label.isHidden()
    assert "nope" in window.notice_label.text() and "未知的配置节" in window.notice_label.text()
    assert [loc for loc, _ in window.unclaimed_rejections()] == [("nope",)]


def test_describe_location() -> None:
    i18n.set_language("zh-Hans")
    assert describe_location(()) == "配置"
    assert describe_location(("engines", 2, "priority")) == "engines › 第 3 项 › priority"


async def test_hiding_the_window_commits_pending_text(start_settings: StartSettings) -> None:
    harness = await start_settings(debounce_ms=5000)
    window = await harness.open()
    window.show_page("models")
    directory = window.page("models").bindings[0].editor
    assert isinstance(directory, DirectoryEditor)
    directory.edit.setText(r"D:\models")
    directory.edit.textEdited.emit(r"D:\models")
    await harness.flush()
    assert harness.recorded.updates == []
    window.hide()
    await harness.flush()
    assert [command.patch for command in harness.recorded.updates] == [{"nmt": {"model_dir": r"D:\models"}}]


async def test_stopping_the_module_commits_pending_text(start_settings: StartSettings) -> None:
    harness = await start_settings(debounce_ms=5000)
    window = await harness.open()
    window.show_page("models")
    directory = window.page("models").bindings[0].editor
    assert isinstance(directory, DirectoryEditor)
    directory.edit.setText("E:/m")
    directory.edit.textEdited.emit("E:/m")
    await harness.stop()
    await asyncio.sleep(0.05)
    assert [command.patch for command in harness.recorded.updates] == [{"nmt": {"model_dir": "E:/m"}}]


async def test_missing_sections_hide_their_cards_instead_of_failing(start_settings: StartSettings) -> None:
    harness = await start_settings(sections=["ui"])
    window = harness.window
    assert len(window.page("general").bindings) == 2
    assert window.page("hotkeys").bindings == []
    assert window.page("capture").bindings == []
    assert window.page("race").bindings == []
    await harness.open()
    assert window.isVisible()
