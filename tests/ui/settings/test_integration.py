"""设置窗口与真实服务的往返：真实的配置服务（临时目录里的配置文件、内存密钥存储）、引擎模块、统计模块、缓存模块。

这里检查的是“界面发出的修改经过真实校验后，界面上看到什么”：ConfigRejected 显示在对应控件旁边，
换一个合法值后消失，其他项照常保存；API key 经由 UpdateSecret 进入密钥存储，不出现在配置文件、日志和界面里。
"""

import logging
import time
import tomllib
from typing import Any

import httpx
import pytest
from PySide6.QtCore import QModelIndex
from PySide6.QtWidgets import QLabel, QLineEdit

from lst.contracts.config import AppConfig
from lst.contracts.events import AttemptRecorded, SegmentsUpdated
from lst.contracts.ids import EngineId, Lang, SegmentId, SessionId
from lst.contracts.ports import CacheKey
from lst.contracts.translation import AttemptState, AttemptView, SegmentView
from lst.ui import i18n
from lst.ui.settings.cards import Binding
from lst.ui.settings.controls import (
    ChoiceEditor,
    DirectoryEditor,
    HotkeyEditor,
    IntEditor,
    PairEditor,
    SwitchEditor,
    TextEditor,
)
from lst.ui.settings.page_engines import EnginesPage
from lst.ui.settings.pages import RacePage, SettingsPage
from tests.ui.conftest import wait_until
from tests.ui.settings.conftest import Full, StartFull

KEY = "sk-live-INTEGRATION-0042"


def binding(page: SettingsPage, *location: str) -> Binding:
    return next(b for b in page.bindings if b.location == location)


def read_config_text(full: Full) -> str:
    """读配置文件。配置服务用 os.replace 原子替换文件，Windows 上替换瞬间读取会被拒绝，稍等重试。"""
    for _ in range(100):
        try:
            return full.config_path.read_text(encoding="utf-8")
        except PermissionError:
            time.sleep(0.01)
    return full.config_path.read_text(encoding="utf-8")


def published(full: Full, section: str, key: str) -> object:
    """总线上最新的 AppConfig 里的值。等待配置生效时轮询它，不轮询文件：
    配置服务用 os.replace 替换文件，Windows 上替换瞬间文件被读取会让写入失败。"""
    config = full.bus.latest(AppConfig)
    table = config.section(section) if config is not None else None
    return table.get(key) if isinstance(table, dict) else None


def config_file(full: Full) -> dict[str, Any]:
    return tomllib.loads(read_config_text(full))


async def engines_page(full: Full) -> EnginesPage:
    window = await full.open()
    window.show_page("engines")
    page = window.page("engines")
    assert isinstance(page, EnginesPage)
    return page


async def test_changes_are_saved_to_the_file_and_shown_back(start_full: StartFull) -> None:
    full = await start_full()
    window = await full.open()
    window.show_page("race")
    mode = binding(window.page("race"), "race", "mode").editor
    assert isinstance(mode, ChoiceEditor)
    mode.combo.setCurrentIndex(mode.combo.findData("full"))
    toggle = binding(window.page("race"), "race", "show_source_marker", "overlay").editor
    assert isinstance(toggle, SwitchEditor)
    toggle.switch.click()
    await full.settle()
    assert config_file(full)["race"] == {
        "mode": "full",
        "show_source_marker": {"overlay": True, "bilingual": True},
    }
    assert mode.json_value() == "full" and not mode.dirty


async def test_language_switch_round_trip(start_full: StartFull) -> None:
    full = await start_full()
    window = await full.open()
    language = binding(window.page("general"), "ui", "language").editor
    assert isinstance(language, ChoiceEditor)
    language.combo.setCurrentIndex(language.combo.findData("en"))
    await full.settle()
    assert i18n.current_language() == "en"
    assert window.windowTitle() == "Settings"
    assert [window.nav.item(i).text() for i in range(3)] == ["General", "Hotkeys", "Capture & recognition"]
    assert config_file(full)["ui"] == {"language": "en", "theme": "system"}


async def test_service_rejection_is_shown_beside_the_control_and_clears(start_full: StartFull) -> None:
    full = await start_full()
    window = await full.open()
    window.show_page("capture")
    page = window.page("capture")
    resize = binding(page, "ocr", "resize_max_side")
    grayscale = binding(page, "ocr", "grayscale").editor
    assert isinstance(resize.editor, IntEditor) and isinstance(grayscale, SwitchEditor)

    resize.editor.spin.setValue(100)  # 配置服务要求 0 或不小于 256
    await wait_until(lambda: resize.card.error_text != "")
    grayscale.switch.click()  # 其他项照常保存
    await full.settle()
    assert "256" in resize.card.error_text  # 原因来自配置服务
    assert config_file(full)["ocr"]["grayscale"] is True
    assert config_file(full)["ocr"]["resize_max_side"] == 4000
    assert resize.editor.spin.value() == 100  # 被拒绝的输入留在控件里，用户直接改正
    assert full.window.unclaimed_rejections() == []

    resize.editor.spin.setValue(512)  # 换一个合法值：提示消失，值保存
    assert resize.card.error_text == ""
    await wait_until(lambda: published(full, "ocr", "resize_max_side") == 512)
    await full.settle()
    assert resize.card.error_text == "" and not resize.editor.dirty


async def test_equal_language_pair_is_rejected_by_the_service(start_full: StartFull) -> None:
    full = await start_full()
    window = await full.open()
    window.show_page("race")
    pair = binding(window.page("race"), "bilingual", "pair")
    assert isinstance(pair.editor, PairEditor)
    pair.editor.second.setCurrentIndex(pair.editor.second.findData("zh-Hans"))
    await wait_until(lambda: pair.card.error_text != "")
    assert "不同" in pair.card.error_text
    pair.editor.second.setCurrentIndex(pair.editor.second.findData("ja"))
    assert pair.card.error_text == ""
    await wait_until(lambda: published(full, "bilingual", "pair") == ["zh-Hans", "ja"])


async def test_hotkey_without_modifier_is_rejected_by_the_service(start_full: StartFull) -> None:
    full = await start_full()
    window = await full.open()
    window.show_page("hotkeys")
    capture = binding(window.page("hotkeys"), "hotkeys", "capture")
    assert isinstance(capture.editor, HotkeyEditor)
    capture.editor._chosen("Q")  # 录入了没有修饰键的组合
    await wait_until(lambda: capture.card.error_text != "")
    assert "修饰键" in capture.card.error_text
    capture.editor._chosen("Ctrl+Alt+Q")
    assert capture.card.error_text == ""
    await wait_until(lambda: published(full, "hotkeys", "capture") == "Ctrl+Alt+Q")


async def test_duplicate_hotkey_is_rejected_beside_the_edited_hotkey(start_full: StartFull) -> None:
    """两个热键设成同一组合：配置服务拒绝这次修改，原因显示在刚改的热键旁边，另一个热键不受影响。"""
    full = await start_full()
    window = await full.open()
    window.show_page("hotkeys")
    page = window.page("hotkeys")
    bilingual = binding(page, "hotkeys", "bilingual")
    assert isinstance(bilingual.editor, HotkeyEditor)
    bilingual.editor._chosen("Alt+Q")  # 与截屏翻译的默认热键相同
    await wait_until(lambda: bilingual.card.error_text != "")
    assert "同一个热键" in bilingual.card.error_text
    assert binding(page, "hotkeys", "capture").card.error_text == ""
    assert full.window.unclaimed_rejections() == []
    await full.settle()
    assert published(full, "hotkeys", "bilingual") == "Alt+E"


async def test_instances_survive_edits_and_reorder_through_the_real_service(start_full: StartFull) -> None:
    full = await start_full()
    page = await engines_page(full)
    assert page.ids() == ["argos"]  # 配置文件不存在时的默认列表
    for _ in range(2):
        page.add_button.click()
        dialog = page.add_dialog
        assert dialog is not None
        dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("deepl"))
        dialog.add_button.click()
        await full.settle()
    assert page.ids() == ["argos", "deepl", "deepl-2"] and page.selected_id == "deepl-2"

    page.list.model().moveRow(QModelIndex(), 2, QModelIndex(), 0)  # 拖拽
    await full.settle()
    assert page.ids() == ["deepl-2", "argos", "deepl"]
    saved = config_file(full)["engines"]
    assert [(e["id"], e["priority"]) for e in saved] == [("deepl-2", 1), ("argos", 2), ("deepl", 3)]

    editor = page.editor_editor("name")
    assert isinstance(editor, TextEditor)
    editor.edit.setText("")  # 空名称：界面先校验，不发送，实例不会消失
    editor.edit.textEdited.emit("")
    editor.edit.editingFinished.emit()
    await full.settle()
    assert page.ids() == ["deepl-2", "argos", "deepl"]
    assert page.editor_card("name") is not None and page.editor_card("name").error_text != ""  # type: ignore[union-attr]


async def add_deepl(full: Full, key: str | None) -> EnginesPage:
    page = await engines_page(full)
    page.add_button.click()
    dialog = page.add_dialog
    assert dialog is not None
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("deepl"))
    if key:
        dialog.key_edit.setText(key)
    dialog.add_button.click()
    await full.settle()
    return page


def visible_text(root: SettingsPage) -> str:
    return "\n".join(
        [label.text() for label in root.findChildren(QLabel)]
        + [edit.text() for edit in root.findChildren(QLineEdit)]
    )


async def test_api_key_flows_to_the_secret_store_and_nowhere_else(
    start_full: StartFull, caplog: pytest.LogCaptureFixture
) -> None:
    handled: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        handled.append(request)
        return httpx.Response(200, json={"translations": [{"text": "你好"}]})

    new_key = "sk-live-REPLACED-0099"
    with caplog.at_level(logging.DEBUG):
        full = await start_full(handler=respond)
        page = await add_deepl(full, KEY)
        assert full.secrets.get_secret("deepl") == KEY  # 经由 UpdateSecret 进入密钥存储
        assert page.secret_field is not None and page.secret_field.status_label.text() == "已设置"
        assert KEY not in read_config_text(full)  # 配置文件里只有条目名
        assert config_file(full)["engines"][1]["secret"] == "deepl"

        page.secret_field.editor.edit.setText(new_key)  # 换一个 key
        page.secret_field.editor.edit.textEdited.emit(new_key)
        page.secret_field.editor.edit.editingFinished.emit()
        await full.settle()
        assert full.secrets.get_secret("deepl") == new_key
        page.test_button.click()  # 真实引擎用保存的 key 发出一次请求
        await wait_until(lambda: page.test_label.text().startswith("连接正常"))
    assert len(handled) == 1 and handled[0].headers["Authorization"] == f"DeepL-Auth-Key {new_key}"
    # 只检查本程序自己的日志器：qasync 在 DEBUG 级别会记录线程池调用的参数（含密钥），
    # 那一条路径由挂在日志 handler 上的脱敏过滤器兜底（架构文档 10.2 节），不在设置窗口的控制范围内。
    ours = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("lst"))
    for secret in (KEY, new_key):
        assert secret not in ours
        assert secret not in visible_text(page)
        assert secret not in read_config_text(full)


async def test_instance_without_a_key_shows_unset_and_the_test_fails_with_advice(
    start_full: StartFull,
) -> None:
    full = await start_full()
    page = await add_deepl(full, None)
    assert page.secret_field is not None and page.secret_field.status_label.text() == "未设置"
    assert "出错" in page.list.item(1).text()
    page.test_button.click()
    await wait_until(lambda: page.test_label.text().startswith("连接失败"))
    assert "API key" in page.test_label.text()
    assert not page.secret_field.clear_button.isEnabled()  # 没有 key，不能清除

    page.secret_field.editor.edit.setText(KEY)
    page.secret_field.editor.edit.textEdited.emit(KEY)
    page.secret_field.editor.edit.editingFinished.emit()
    await full.settle()
    assert page.secret_field.status_label.text() == "已设置" and page.secret_field.clear_button.isEnabled()
    page.secret_field.clear_button.click()
    await full.settle()
    assert full.secrets.get_secret("deepl") is None
    assert page.secret_field.status_label.text() == "未设置"


async def test_removing_an_instance_deletes_its_key(start_full: StartFull) -> None:
    full = await start_full()
    page = await add_deepl(full, KEY)
    assert full.secrets.get_secret("deepl") == KEY
    page.select("deepl")
    page.remove_button.click()
    await full.settle()
    assert page.ids() == ["argos"]
    assert full.secrets.get_secret("deepl") is None
    assert [e["id"] for e in config_file(full)["engines"]] == ["argos"]


async def test_statistics_table_updates_and_resets_through_the_real_stats_module(
    start_full: StartFull,
) -> None:
    full = await start_full()
    window = await full.open()
    window.show_page("race")
    page = window.page("race")
    assert isinstance(page, RacePage)
    attempt = AttemptView(EngineId("argos"), AttemptState.DONE, "译文", None, 150.0, started=True)
    segment = SegmentView(SegmentId("s1"), "原文", "译文", EngineId("argos"), True, (attempt,))
    full.bus.publish(AttemptRecorded(SessionId("sess"), SegmentId("s1"), attempt))
    full.bus.publish(SegmentsUpdated(SessionId("sess"), (segment,)))

    def cell(column: int) -> str:
        item = page.stats.table.item(0, column)
        return item.text() if item is not None else ""

    await wait_until(lambda: cell(1) == "1")
    assert [cell(c) for c in range(7)] == ["Argos", "1", "1", "100%", "150 毫秒", "150 毫秒", "0%"]

    page.stats.reset_button.click()  # FakeDialogs 默认确认
    await wait_until(lambda: cell(1) == "0")
    assert not page.stats.empty_label.isHidden()


async def test_clear_cache_through_the_real_cache_module(start_full: StartFull) -> None:
    full = await start_full()
    assert full.cache is not None
    for text in ("a", "b", "c"):
        await full.cache.put(CacheKey(text, Lang("en"), Lang("zh-Hans"), EngineId("argos"), "fp"), "译")
    window = await full.open()
    window.show_page("race")
    page = window.page("race")
    assert isinstance(page, RacePage)
    page.stats.clear_cache_button.click()
    await wait_until(lambda: not page.stats.cache_label.isHidden())
    assert page.stats.cache_label.text() == "已清空译文缓存（3 条）。"
    assert await full.cache.count() == 0


async def test_text_box_saves_after_the_debounce_through_the_real_service(start_full: StartFull) -> None:
    full = await start_full(debounce_ms=40)
    window = await full.open()
    window.show_page("models")
    editor = binding(window.page("models"), "nmt", "model_dir").editor
    assert isinstance(editor, DirectoryEditor)
    editor.edit.setText("D:/models")
    editor.edit.textEdited.emit("D:/models")
    await wait_until(lambda: published(full, "nmt", "model_dir") == "D:/models")
    assert config_file(full)["nmt"]["model_dir"] == "D:/models"
