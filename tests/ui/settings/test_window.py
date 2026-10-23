"""设置窗口模块与窗口的测试：OpenSettings 打开与再次打开、导航、窗口状态、界面语言切换。"""

import asyncio
import os
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from lst.contracts.config import ConfigRejected
from lst.kernel import Module
from lst.ui import i18n
from lst.ui.common.window import fit_on_screens
from lst.ui.settings import SettingsModule
from lst.ui.settings.controls import ChoiceEditor, DirectoryEditor
from lst.ui.settings.page_engines import EnginesPage
from lst.ui.settings.pages import RacePage
from lst.ui.settings.state import GROUP, WindowStateStore
from lst.ui.settings.window import SettingsWindow, describe_location
from lst.ui.theme import TOKENS
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import all_models, default_config, engine_kinds

GROUP_IDS = ["general", "hotkeys", "capture", "engines", "race", "models", "about"]
LEAF_IDS = [
    "general",
    "hotkeys.global",
    "hotkeys.capture",
    "hotkeys.overlay",
    "hotkeys.bilingual",
    "capture.engines",
    "capture.windows",
    "capture.rapid",
    "capture.paddle",
    "capture.normalize",
    "capture.options",
    "engines",
    "race",
    "models.nmt",
    "models.gpu",
    "models.dicts",
    "about",
]
ZH_NAMES = ["常规", "热键与键位", "截屏与识别", "翻译引擎", "竞速", "模型", "关于与诊断"]
ZH_HOTKEYS_CHILDREN = ["全局热键", "截屏蒙版", "覆盖窗口", "双语窗口"]
ZH_CAPTURE_CHILDREN = ["OCR 引擎", "Windows OCR", "RapidOCR", "PaddleOCR", "结构归一化", "截图与识别选项"]
ZH_MODELS_CHILDREN = ["本地翻译模型", "显卡加速", "词典"]
EN_NAMES = [
    "General",
    "Hotkeys & bindings",
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
    """一级项的文字。"""
    return [item.text(0) for item in window.nav.top_items()]


def children_texts(window: SettingsWindow, group: str) -> list[str]:
    item = window.nav.item_of(group)
    return [child.text(0) for child in window.nav.children_of(item)]


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


async def test_menu_has_seven_groups_and_seventeen_pages_in_the_documented_order(harness: Harness) -> None:
    window = harness.window
    assert [page.page_id for page in window.pages] == LEAF_IDS
    assert nav_texts(window) == ZH_NAMES
    assert window.nav.topLevelItemCount() == 7
    assert window.stack.count() == len(LEAF_IDS) == 17
    assert children_texts(window, "hotkeys") == ZH_HOTKEYS_CHILDREN
    assert children_texts(window, "capture") == ZH_CAPTURE_CHILDREN
    assert children_texts(window, "models") == ZH_MODELS_CHILDREN
    for row, (top, name) in enumerate(zip(window.nav.top_items(), ZH_NAMES, strict=True)):
        assert not top.icon(0).isNull()  # 每个一级项都有 Fluent 图标
        assert f"Ctrl+{row + 1}" in top.toolTip(0)
        assert top.data(0, Qt.ItemDataRole.UserRole) == GROUP_IDS[row]
        children = {"热键与键位": 4, "截屏与识别": 6, "模型": 3}
        assert top.childCount() == children.get(name, 0)


async def test_page_headings_name_the_group_and_the_page(harness: Harness) -> None:
    window = harness.window
    assert window.page("general").heading.text() == "常规"
    assert window.page("capture.paddle").heading.text() == "截屏与识别 › PaddleOCR"
    assert window.page("models.gpu").heading.text() == "模型 › 显卡加速"
    assert window.page("hotkeys.overlay").heading.text() == "热键与键位 › 覆盖窗口"


async def test_clicking_a_group_expands_it_and_shows_its_first_page(harness: Harness) -> None:
    window = await harness.open()
    assert window.nav.visible_ids()[:3] == ["general", "hotkeys", "capture"]
    assert not window.nav.is_expanded("capture")
    capture_rect = window.nav.visualItemRect(window.nav.item_of("capture"))
    QTest.mouseClick(window.nav.viewport(), Qt.MouseButton.LeftButton, pos=capture_rect.center())
    assert window.current_page.page_id == "capture.engines"
    assert window.nav.is_expanded("capture")
    assert window.nav.currentItem() is window.nav.item_of("capture.engines")
    # 只展开当前分支：去模型分支后，截屏与识别收起。
    models_rect = window.nav.visualItemRect(window.nav.item_of("models"))
    QTest.mouseClick(window.nav.viewport(), Qt.MouseButton.LeftButton, pos=models_rect.center())
    assert window.current_page.page_id == "models.nmt"
    assert window.nav.is_expanded("models") and not window.nav.is_expanded("capture")
    # 没有子项的一级项直接显示自己的页面。
    race_rect = window.nav.visualItemRect(window.nav.item_of("race"))
    QTest.mouseClick(window.nav.viewport(), Qt.MouseButton.LeftButton, pos=race_rect.center())
    assert window.current_page.page_id == "race"
    assert not window.nav.is_expanded("models")


async def test_clicking_a_child_item_shows_that_page(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("capture.engines")
    rect = window.nav.visualItemRect(window.nav.item_of("capture.paddle"))
    QTest.mouseClick(window.nav.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert window.current_page.page_id == "capture.paddle"
    assert window.stack.currentWidget() is window.page("capture.paddle")


async def test_show_page_accepts_new_and_legacy_ids(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("capture.rapid")
    assert window.current_page.page_id == "capture.rapid"
    assert window.nav.currentItem() is window.nav.item_of("capture.rapid")
    window.show_page("models")  # 旧的一级标识：该分支的第一个子页
    assert window.current_page.page_id == "models.nmt"
    window.show_page("capture")
    assert window.current_page.page_id == "capture.engines"
    window.show_page("hotkeys")
    assert window.current_page.page_id == "hotkeys.global"
    assert window.nav.is_expanded("hotkeys")
    assert window.page("capture") is window.page("capture.engines")
    with pytest.raises(KeyError):
        window.show_page("nope")


async def test_tree_arrow_keys_move_collapse_and_expand(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("capture.engines")
    window.nav.setFocus()
    QTest.keyClick(window.nav, Qt.Key.Key_Down)
    assert window.current_page.page_id == "capture.windows"
    QTest.keyClick(window.nav, Qt.Key.Key_Up)
    # 越过已展开的一级项，到上一个一级项；它是收起的分支：展开并显示它的第一个子页。
    QTest.keyClick(window.nav, Qt.Key.Key_Up)
    assert window.current_page.page_id == "hotkeys.global"
    assert window.nav.is_expanded("hotkeys") and not window.nav.is_expanded("capture")
    for expected in ("hotkeys.capture", "hotkeys.overlay", "hotkeys.bilingual"):
        QTest.keyClick(window.nav, Qt.Key.Key_Down)
        assert window.current_page.page_id == expected
    QTest.keyClick(window.nav, Qt.Key.Key_Down)  # 收起的一级项：展开并显示第一个子页
    assert window.current_page.page_id == "capture.engines"
    assert window.nav.is_expanded("capture")
    # 左键收起当前分支，高亮回到一级项，页面不变。
    QTest.keyClick(window.nav, Qt.Key.Key_Left)
    assert not window.nav.is_expanded("capture")
    assert window.nav.currentItem() is window.nav.item_of("capture")
    assert window.current_page.page_id == "capture.engines"
    QTest.keyClick(window.nav, Qt.Key.Key_Right)
    assert window.nav.is_expanded("capture")
    assert window.nav.currentItem() is window.nav.item_of("capture.engines")
    # 顺序走到底，再往下不循环。
    for _ in range(20):
        QTest.keyClick(window.nav, Qt.Key.Key_Down)
    assert window.current_page.page_id == "about"
    QTest.keyClick(window.nav, Qt.Key.Key_Home)
    assert window.current_page.page_id == "general"


async def test_keyboard_navigation_between_pages(harness: Harness) -> None:
    window = await harness.open()
    ctrl = Qt.KeyboardModifier.ControlModifier
    # Ctrl+数字：第 n 个一级项，有子项时到第一个子页。
    expected = ["general", "hotkeys.global", "capture.engines", "engines", "race", "models.nmt", "about"]
    for number, page_id in enumerate(expected, start=1):
        QTest.keyClick(window, Qt.Key(int(Qt.Key.Key_1.value) + number - 1), ctrl)
        assert window.current_page.page_id == page_id
    QTest.keyClick(window, Qt.Key.Key_9, ctrl)  # 没有第九项：不变
    assert window.current_page.page_id == "about"
    # Ctrl+Tab 按叶子页顺序循环。
    window.show_page("hotkeys")
    seen: list[str] = []
    for _ in range(len(LEAF_IDS)):
        QTest.keyClick(window, Qt.Key.Key_Tab, ctrl)
        seen.append(window.current_page.page_id)
    assert seen == [*LEAF_IDS[2:], *LEAF_IDS[:2]]
    shift = ctrl | Qt.KeyboardModifier.ShiftModifier
    QTest.keyClick(window, Qt.Key.Key_Tab, shift)
    assert window.current_page.page_id == "general"
    QTest.keyClick(window, Qt.Key.Key_Tab, shift)
    assert window.current_page.page_id == "about"  # 循环
    window.show_page("capture.options")
    QTest.keyClick(window, Qt.Key.Key_Tab, ctrl)
    assert window.current_page.page_id == "engines"
    QTest.keyClick(window, Qt.Key.Key_Tab, shift)
    assert window.current_page.page_id == "capture.options"


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
    assert (saved["width"], saved["height"], saved["page_id"]) == ("900", "600", "race")
    assert (saved["x"], saved["y"]) == ("120", "90")
    check = QSettings(str(settings_path), QSettings.Format.IniFormat)
    assert check.value("bilingual_window/x") is not None and check.value("onboarding/completed") is not None


async def test_state_is_restored_after_restart(start_settings: StartSettings) -> None:
    first = await start_settings()
    window = await first.open()
    window.show_page("capture.paddle")
    window.resize(QSize(1000, 640))
    window.move(QPoint(50, 40))
    await first.stop()

    second = await start_settings()
    reopened = await second.open()
    assert reopened.size() == QSize(1000, 640)
    assert reopened.pos() == QPoint(50, 40)
    assert reopened.current_page.page_id == "capture.paddle"  # 记的是叶子页
    assert reopened.nav.is_expanded("capture")


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        ({"page": 1}, "hotkeys.global"),  # 旧版本保存的页面序号：第二个一级页面，现在有子页
        ({"page": 2}, "capture.engines"),  # 旧版本保存的页面序号：第三个一级页面
        ({"page": 5}, "models.nmt"),
        ({"page": 6}, "about"),
        ({"page": 99}, "general"),  # 越界：保持默认
        ({"page_id": "capture"}, "capture.engines"),  # 旧的一级标识
        ({"page_id": "hotkeys"}, "hotkeys.global"),
        ({"page_id": "hotkeys.overlay"}, "hotkeys.overlay"),
        ({"page_id": "models.dicts"}, "models.dicts"),
        ({"page_id": "no.such", "page": 4}, "race"),  # 标识不认识时退回旧序号
        ({"page_id": "no.such"}, "general"),
    ],
)
async def test_saved_page_accepts_new_ids_and_legacy_values(
    start_settings: StartSettings, settings_path: Path, saved: dict[str, object], expected: str
) -> None:
    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    for key, value in saved.items():
        settings.setValue(f"{GROUP}/{key}", value)
    settings.sync()
    harness = await start_settings()
    window = await harness.open()
    assert window.current_page.page_id == expected


async def test_window_saved_off_screen_is_moved_back(
    start_settings: StartSettings, settings_path: Path
) -> None:
    saved = QSettings(str(settings_path), QSettings.Format.IniFormat)
    for key, value in (("x", 99000), ("y", 99000), ("width", 900), ("height", 600), ("page_id", "general")):
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
    assert (state.position, state.size, state.page, state.page_id) == (None, None, None, None)


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
    assert children_texts(window, "capture")[0] == "OCR engines"
    assert children_texts(window, "models") == [
        "Local translation models",
        "GPU acceleration",
        "Dictionaries",
    ]
    assert (
        window.page("capture.options").heading.text()
        == "Capture & recognition › Capture & recognition options"
    )
    assert window.page("general").heading.text() == "General"
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
    assert "Ctrl+3" in window.nav.item_of("capture").toolTip(0)

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
    await harness.publish(ConfigRejected(("bilingual", "layout"), "不是可选值"))  # 有卡片负责：不进提示条
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
    window.show_page("models.nmt")
    directory = window.page("models.nmt").bindings[0].editor
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
    window.show_page("models.nmt")
    directory = window.page("models.nmt").bindings[0].editor
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
    assert window.page("capture.options").bindings == []
    assert window.page("race").bindings == []
    await harness.open()
    assert window.isVisible()


# ---------------------------------------------------------------- ConfigRejected 跳到负责的子页


@pytest.mark.parametrize(
    ("location", "page_id"),
    [
        (("ocr", "resize_max_side"), "capture.options"),
        (("ocr", "engine"), "capture.engines"),
        (("ocr", "chart"), "capture.paddle"),  # 环节开关在 PaddleOCR 子页
        (("ocr_paddle", "table_model"), "capture.paddle"),
        (("ocr_rapid", "recognition_model"), "capture.rapid"),
        (("ocr_normalize", "clean"), "capture.normalize"),
        (("screenshot", "target_lang"), "capture.options"),
        (("nmt", "device"), "models.gpu"),
        (("nmt", "model_dir"), "models.nmt"),
        (("bilingual", "layout"), "race"),
        (("hotkeys", "capture"), "hotkeys.global"),
        (("bindings", "overlay_zoom"), "hotkeys.overlay"),
        (("bindings", "capture_cancel"), "hotkeys.capture"),
    ],
)
async def test_rejection_switches_to_the_sub_page_that_owns_the_field(
    harness: Harness, location: tuple[str, ...], page_id: str
) -> None:
    window = await harness.open()
    window.show_page("about")
    await harness.publish(ConfigRejected(location, "不行"))
    assert window.current_page.page_id == page_id
    assert window.nav.currentItem() is window.nav.item_of(page_id)
    assert window.page_for_location(location) is window.page(page_id)
    assert window.unclaimed_rejections() == []


async def test_rejection_prefers_the_page_of_the_current_ocr_engine(harness: Harness) -> None:
    window = await harness.open()
    assert window.page_for_location(("ocr", "textline_orientation")) is window.page("capture.rapid")
    await harness.publish(default_config({"ocr": {"engine": "paddle"}}, revision=2))
    assert window.page_for_location(("ocr", "textline_orientation")) is window.page("capture.paddle")


async def test_rejection_does_not_leave_the_page_that_already_shows_it(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("capture.options")
    await harness.publish(ConfigRejected(("ocr", "grayscale"), "不行"))
    assert window.current_page.page_id == "capture.options"
    # 没有页面负责的位置：留在原处，由底部提示条显示。
    window.show_page("race")
    await harness.publish(ConfigRejected(("nope",), "未知的配置节"))
    assert window.current_page.page_id == "race"
    assert not window.notice_label.isHidden()


async def test_rejection_does_not_switch_pages_while_the_window_is_hidden(harness: Harness) -> None:
    window = harness.window
    assert not window.isVisible()
    await harness.publish(ConfigRejected(("nmt", "device"), "不行"))
    assert window.current_page.page_id == "general"


async def test_show_location_scrolls_to_the_card(harness: Harness) -> None:
    window = await harness.open()
    binding = window.show_location(("ocr", "resize_max_side"))
    assert binding is not None and binding.location == ("ocr", "resize_max_side")
    assert window.current_page.page_id == "capture.options"
    assert window.show_location(("nope",)) is None
    assert window.current_page.page_id == "capture.options"


# ---------------------------------------------------------------- 外观与截图


async def test_child_rows_are_indented_and_shorter_than_group_rows(harness: Harness) -> None:
    from PySide6.QtWidgets import QStyleOptionViewItem

    from lst.ui.settings.window import NavDelegate

    window = await harness.open()
    window.show_page("capture.engines")
    delegate = window.nav.itemDelegate()
    assert isinstance(delegate, NavDelegate)
    option = QStyleOptionViewItem()
    option.rect.setSize(QSize(TOKENS.nav_width, 10))
    group = window.nav.indexFromItem(window.nav.item_of("capture"))
    child = window.nav.indexFromItem(window.nav.item_of("capture.engines"))
    assert delegate.sizeHint(option, group).height() == TOKENS.nav_item_height
    assert 0 < delegate.sizeHint(option, child).height() < TOKENS.nav_item_height
    assert window.nav.item_of("capture.engines").icon(0).isNull()  # 二级项没有图标
    assert not window.nav.item_of("capture").icon(0).isNull()
    assert window.nav.item_of("capture").childCount() == 6


@pytest.mark.skipif(not os.environ.get("LST_SETTINGS_SHOTS"), reason="只在需要截图时运行")
async def test_nav_screenshots(start_settings: StartSettings, qapp: QApplication) -> None:
    """capture.paddle 与 models.gpu 两页在浅色、深色下的截图，存到 data/tmp/settings-shots/nav-*.png。"""
    from PySide6.QtGui import QImage, QPainter

    from lst.ui.theme import apply_app_style, theme_colors

    target = Path(__file__).resolve().parents[3] / "data" / "tmp" / "settings-shots"
    target.mkdir(parents=True, exist_ok=True)
    try:
        for scheme in ("light", "dark"):
            apply_app_style(qapp, scheme)
            harness = await start_settings(config=default_config({"nmt": {"device": "auto"}}))
            window = await harness.open()
            window.resize(1240, 900)
            for page_id in ("capture.paddle", "models.gpu"):
                window.show_page(page_id)
                QApplication.processEvents()
                # Mica 生效时窗口背景透明，深色截图会是白底白字；先铺上主题底色再画窗口。
                grabbed = window.grab()
                image = QImage(grabbed.size(), QImage.Format.Format_ARGB32)
                image.setDevicePixelRatio(grabbed.devicePixelRatio())
                image.fill(theme_colors().window)
                painter = QPainter(image)
                painter.drawPixmap(0, 0, grabbed)
                painter.end()
                image.save(str(target / f"nav-{page_id.replace('.', '-')}-{scheme}.png"))
            await harness.stop()
    finally:
        apply_app_style(qapp, "system")
