"""常规、热键、截屏与识别、竞速四个页面的测试：控件读出 AppConfig 的值，修改发出正确的 UpdateConfig，
文本框 500 毫秒防抖，ConfigRejected 显示在对应控件旁边、换一个合法值后消失。"""

import asyncio

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from lst.contracts.config import ConfigRejected
from lst.contracts.ocr_engines import OcrStage
from lst.ui.settings.cards import Binding
from lst.ui.settings.controls import (
    ChoiceEditor,
    HotkeyEditor,
    IntEditor,
    PairEditor,
    SwitchEditor,
)
from lst.ui.settings.page_ocr_engine import OcrEnginePage
from lst.ui.settings.pages import SettingsPage
from tests.ui.conftest import wait_until
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import default_config


def binding(page: SettingsPage, *location: str) -> Binding:
    return next(b for b in page.bindings if b.location == location)


def patches(harness: Harness) -> list[dict[str, object]]:
    return [dict(command.patch) for command in harness.recorded.updates]


# ---------------------------------------------------------------- 读出配置的值


async def test_general_page_reads_the_config(start_settings: StartSettings) -> None:
    harness = await start_settings(config=default_config({"ui": {"language": "en", "theme": "dark"}}))
    page = harness.window.page("general")
    language, theme = binding(page, "ui", "language").editor, binding(page, "ui", "theme").editor
    assert isinstance(language, ChoiceEditor) and isinstance(theme, ChoiceEditor)
    assert (language.json_value(), theme.json_value()) == ("en", "dark")
    assert language.combo.count() == 3 and theme.combo.count() == 3


async def test_hotkeys_page_reads_the_config(start_settings: StartSettings) -> None:
    harness = await start_settings(config=default_config({"hotkeys": {"capture": "Ctrl+Alt+S"}}))
    page = harness.window.page("hotkeys")
    capture = binding(page, "hotkeys", "capture").editor
    bilingual = binding(page, "hotkeys", "bilingual").editor
    assert isinstance(capture, HotkeyEditor) and isinstance(bilingual, HotkeyEditor)
    assert capture.button.text() == "Ctrl+Alt+S"
    assert bilingual.button.text() == "Alt+E"


async def test_capture_page_reads_every_ocr_field(start_settings: StartSettings) -> None:
    harness = await start_settings(
        config=default_config(
            {
                "ocr": {
                    "enabled": False,
                    "mode": "text",
                    "device": "gpu",
                    "language": "ja",
                    "table": False,
                    "formula": True,
                    "chart": True,
                    "doc_orientation": True,
                    "doc_unwarping": True,
                    "textline_orientation": True,
                    "grayscale": True,
                    "resize_max_side": 2048,
                },
                "screenshot": {"target_lang": "ko"},
            }
        )
    )
    page = harness.window.page("capture.options")
    values = {b.location: b.editor.json_value() for b in page.bindings}
    assert values == {
        ("ocr", "enabled"): False,
        ("ocr", "mode"): "text",
        ("ocr", "device"): "gpu",
        ("ocr", "language"): "ja",
        ("ocr", "grayscale"): True,
        ("ocr", "resize_max_side"): 2048,
        ("screenshot", "target_lang"): "ko",
    }
    # 表格、公式等环节开关在 PaddleOCR 子页的流水线里，RapidOCR 子页只有文本行方向。
    paddle = harness.window.page("capture.paddle")
    assert isinstance(paddle, OcrEnginePage)
    assert {stage.value: toggle.editor.json_value() for stage, toggle in paddle.pipeline.toggles.items()} == {
        "table": False,
        "formula": True,
        "chart": True,
        "doc_orientation": True,
        "doc_unwarping": True,
        "textline_orientation": True,
        "region": True,
    }
    rapid = harness.window.page("capture.rapid")
    assert isinstance(rapid, OcrEnginePage)
    assert {stage.value: toggle.editor.json_value() for stage, toggle in rapid.pipeline.toggles.items()} == {
        "textline_orientation": True
    }
    windows = harness.window.page("capture.windows")
    assert isinstance(windows, OcrEnginePage)
    assert windows.pipeline.toggles == {}
    resize = binding(page, "ocr", "resize_max_side").editor
    assert isinstance(resize, IntEditor)
    assert (resize.spin.minimum(), resize.spin.maximum()) == (0, 16384)  # 范围来自模型
    assert resize.spin.suffix() == " 像素"


async def test_capture_page_shows_model_defaults(harness: Harness) -> None:
    page = harness.window.page("capture.options")
    assert binding(page, "ocr", "mode").editor.json_value() == "structure"
    assert binding(page, "ocr", "device").editor.json_value() == "auto"
    assert binding(page, "ocr", "language").editor.json_value() == "auto"
    paddle = harness.window.page("capture.paddle")
    assert isinstance(paddle, OcrEnginePage)
    assert paddle.pipeline.toggles[OcrStage.REGION].editor.json_value() is True
    assert binding(page, "ocr", "resize_max_side").editor.json_value() == 4000


async def test_race_page_reads_the_config(start_settings: StartSettings) -> None:
    """竞速页：覆盖窗口的来源标记与双语窗口的设置。结束方式与双语窗口的来源标记已经删除，没有卡片。"""
    harness = await start_settings(
        config=default_config(
            {
                "race": {"show_source_marker": {"overlay": True}},
                "bilingual": {"pair": ["ja", "en"], "debounce_ms": 750, "layout": "horizontal"},
            }
        )
    )
    page = harness.window.page("race")
    values = {b.location: b.editor.json_value() for b in page.bindings}
    assert values == {
        ("race", "show_source_marker", "overlay"): True,
        ("bilingual", "pair"): ["ja", "en"],
        ("bilingual", "debounce_ms"): 750,
        ("bilingual", "layout"): "horizontal",
    }
    debounce = binding(page, "bilingual", "debounce_ms").editor
    assert isinstance(debounce, IntEditor)
    assert (debounce.spin.minimum(), debounce.spin.maximum()) == (100, 5000)


async def test_config_changes_refresh_the_controls(harness: Harness) -> None:
    page = harness.window.page("race")
    await harness.publish(
        default_config(
            {"race": {"show_source_marker": {"overlay": True}}, "bilingual": {"debounce_ms": 900}}, revision=2
        )
    )
    assert binding(page, "race", "show_source_marker", "overlay").editor.json_value() is True
    assert binding(page, "bilingual", "debounce_ms").editor.json_value() == 900
    assert harness.recorded.updates == []  # 显示配置的值不会反过来提交


# ---------------------------------------------------------------- 修改发出 UpdateConfig


async def test_dropdown_commits_immediately(harness: Harness) -> None:
    page = harness.window.page("general")
    editor = binding(page, "ui", "theme").editor
    assert isinstance(editor, ChoiceEditor)
    editor.combo.setCurrentIndex(editor.combo.findData("dark"))
    await harness.flush()
    assert patches(harness) == [{"ui": {"theme": "dark"}}]


async def test_switch_commits_immediately(harness: Harness) -> None:
    page = harness.window.page("capture.paddle")
    assert isinstance(page, OcrEnginePage)
    editor = page.pipeline.toggles[OcrStage.FORMULA].editor
    assert isinstance(editor, SwitchEditor)
    editor.switch.click()
    await harness.flush()
    assert patches(harness) == [{"ocr": {"formula": True}}]


async def test_nested_switch_sends_a_nested_patch(harness: Harness) -> None:
    editor = binding(harness.window.page("race"), "race", "show_source_marker", "overlay").editor
    assert isinstance(editor, SwitchEditor)
    editor.switch.click()
    await harness.flush()
    assert patches(harness) == [{"race": {"show_source_marker": {"overlay": True}}}]


async def test_language_dropdowns_send_language_codes(harness: Harness) -> None:
    page = harness.window.page("capture.options")
    ocr_language = binding(page, "ocr", "language").editor
    target = binding(page, "screenshot", "target_lang").editor
    assert isinstance(ocr_language, ChoiceEditor) and isinstance(target, ChoiceEditor)
    assert ocr_language.combo.itemData(0) == "auto"
    ocr_language.combo.setCurrentIndex(ocr_language.combo.findData("fr"))
    target.combo.setCurrentIndex(target.combo.findData("en"))
    await harness.flush()
    assert patches(harness) == [{"ocr": {"language": "fr"}}, {"screenshot": {"target_lang": "en"}}]


async def test_pair_sends_both_languages(harness: Harness) -> None:
    editor = binding(harness.window.page("race"), "bilingual", "pair").editor
    assert isinstance(editor, PairEditor)
    editor.first.setCurrentIndex(editor.first.findData("de"))
    await harness.flush()
    assert patches(harness) == [{"bilingual": {"pair": ["de", "en"]}}]


async def test_hotkey_recording_commits_the_new_hotkey(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("hotkeys")
    editor = binding(window.page("hotkeys"), "hotkeys", "capture").editor
    assert isinstance(editor, HotkeyEditor)
    editor.button.setFocus()
    QTest.keyClick(editor.button, Qt.Key.Key_Return)
    QTest.keyClick(
        editor.button, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
    )
    await harness.flush()
    assert patches(harness) == [{"hotkeys": {"capture": "Ctrl+Alt+W"}}]


async def test_number_box_commits_after_the_debounce(harness: Harness) -> None:
    editor = binding(harness.window.page("race"), "bilingual", "debounce_ms").editor
    assert isinstance(editor, IntEditor)
    editor.spin.setValue(600)
    editor.spin.setValue(650)
    await harness.flush()
    assert harness.recorded.updates == []  # 还在等待
    await wait_until(lambda: bool(harness.recorded.updates))
    await harness.flush()
    assert patches(harness) == [{"bilingual": {"debounce_ms": 650}}]  # 连续修改只提交最后一次


async def test_default_debounce_for_text_boxes_is_500_ms(start_settings: StartSettings) -> None:
    harness = await start_settings(debounce_ms=500)
    editor = binding(harness.window.page("models.nmt"), "nmt", "model_dir").editor
    editor.edit.setText("C:/models")  # type: ignore[attr-defined]
    editor.edit.textEdited.emit("C:/models")  # type: ignore[attr-defined]
    await asyncio.sleep(0.3)
    await harness.flush()
    assert harness.recorded.updates == []
    await asyncio.sleep(0.35)
    await harness.flush()
    assert patches(harness) == [{"nmt": {"model_dir": "C:/models"}}]


# ---------------------------------------------------------------- ConfigRejected


async def test_rejection_is_shown_beside_its_control_only(harness: Harness) -> None:
    page = harness.window.page("capture.options")
    await harness.publish(ConfigRejected(("ocr", "resize_max_side"), "缩放上限为 0 或不小于 256"))
    shown = binding(page, "ocr", "resize_max_side").card
    assert shown.error_text == "未保存：缩放上限为 0 或不小于 256"
    assert all(
        b.card.error_text == "" for b in page.bindings if b is not binding(page, "ocr", "resize_max_side")
    )


async def test_rejection_disappears_when_the_value_is_changed(harness: Harness) -> None:
    page = harness.window.page("race")
    card = binding(page, "bilingual", "pair").card
    await harness.publish(ConfigRejected(("bilingual", "pair"), "互译对的两个语言必须不同"))
    assert card.error_text != ""
    editor = binding(page, "bilingual", "pair").editor
    assert isinstance(editor, PairEditor)
    editor.second.setCurrentIndex(editor.second.findData("ja"))  # 换一个合法值
    assert card.error_text == ""
    await harness.flush()
    assert patches(harness) == [{"bilingual": {"pair": ["zh-Hans", "ja"]}}]


async def test_text_rejection_clears_as_soon_as_the_user_types(harness: Harness) -> None:
    page = harness.window.page("models.nmt")
    card = binding(page, "nmt", "model_dir").card
    await harness.publish(ConfigRejected(("nmt", "model_dir"), "目录无效"))
    assert card.error_text != ""
    editor = binding(page, "nmt", "model_dir").editor
    editor.edit.setText("D:/ok")  # type: ignore[attr-defined]
    editor.edit.textEdited.emit("D:/ok")  # type: ignore[attr-defined]
    assert card.error_text == ""


async def test_rejected_input_stays_in_the_control(harness: Harness) -> None:
    """被拒绝的输入留在控件里（旁边有原因），不被别的设置项触发的配置刷新改回去。"""
    page = harness.window.page("capture.options")
    resize = binding(page, "ocr", "resize_max_side").editor
    assert isinstance(resize, IntEditor)
    resize.spin.setValue(100)
    await wait_until(lambda: bool(harness.recorded.updates))
    await harness.publish(ConfigRejected(("ocr", "resize_max_side"), "不小于 256"))
    await harness.publish(
        default_config({"ocr": {"table": False, "grayscale": True}}, revision=2)
    )  # 别的设置项变了
    assert resize.spin.value() == 100
    assert binding(page, "ocr", "resize_max_side").card.error_text != ""
    assert binding(page, "ocr", "grayscale").editor.json_value() is True  # 其他项照常刷新
    paddle = harness.window.page("capture.paddle")
    assert isinstance(paddle, OcrEnginePage)
    assert paddle.pipeline.toggles[OcrStage.TABLE].editor.json_value() is False


async def test_other_settings_are_saved_while_one_is_rejected(harness: Harness) -> None:
    await harness.publish(ConfigRejected(("ocr", "resize_max_side"), "不小于 256"))
    editor = binding(harness.window.page("capture.options"), "ocr", "grayscale").editor
    assert isinstance(editor, SwitchEditor)
    editor.switch.click()
    await harness.flush()
    assert patches(harness) == [{"ocr": {"grayscale": True}}]
    assert binding(harness.window.page("capture.options"), "ocr", "resize_max_side").card.error_text != ""
