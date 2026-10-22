"""界面审查确认的设置窗口问题的回归测试：更新卡片、API key 输入、英文界面的文案、键盘切页、
引擎拒绝位置的换算、推理进程的重新启动、热键录入，以及重复刷新与关闭后的守卫。"""

import asyncio
from collections.abc import Callable

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from lst.contracts.config import ConfigRejected, UpdateConfig, UpdateSecret
from lst.contracts.errors import ErrorCode
from lst.contracts.events import (
    CacheCleared,
    EngineHealth,
    EngineStatus,
    EngineStatuses,
    RestartWorker,
    WorkerStatus,
    WorkerStatuses,
)
from lst.contracts.ids import EngineId
from lst.contracts.ports import WorkerState
from lst.contracts.updates import CheckForUpdates, ReleaseInfo, UpdateErrorCode, UpdateState, UpdateStatus
from lst.kernel import EventBus
from lst.ui.common import HotkeyRecordingChanged
from lst.ui.settings.controls import HotkeyEditor
from lst.ui.settings.entries import EngineDraft
from lst.ui.settings.page_about import AboutPage
from lst.ui.settings.page_engines import EnginesPage
from lst.ui.settings.pages import RacePage, SettingsPage
from tests.ui.settings.conftest import Harness
from tests.ui.settings.support import default_config

RELEASE = ReleaseInfo("0.2.0", False, "2026-10-20T08:00:00Z", "", "https://example.invalid/r", 1000)
ENGLISH = default_config({"ui": {"language": "en"}}, revision=7)


def status(state: UpdateState, **fields: object) -> UpdateStatus:
    return UpdateStatus(state=state, current_version="0.1.0", can_install=True, **fields)  # type: ignore[arg-type]


def about(harness: Harness) -> AboutPage:
    page = harness.window.page("about")
    assert isinstance(page, AboutPage)
    return page


def engines_page(harness: Harness) -> EnginesPage:
    page = harness.window.page("engines")
    assert isinstance(page, EnginesPage)
    return page


def race_page(harness: Harness) -> RacePage:
    page = harness.window.page("race")
    assert isinstance(page, RacePage)
    return page


# ---------------------------------------------------------------------- 应用更新


async def test_check_button_is_disabled_while_the_installer_is_ready(harness: Harness) -> None:
    """安装包已下载（READY）时更新服务不再检查，卡片不能停在“正在检查”。"""
    await harness.publish(status(UpdateState.READY, available=RELEASE))
    card = about(harness).update_card
    assert not card.check_button.isEnabled()
    harness.presenter.check_updates()  # 别处（例如键盘助记键）仍然调用了检查
    await harness.flush()
    assert harness.recorded.of(CheckForUpdates) == []
    assert "正在检查" not in card.status_label.text()


async def test_check_feedback_ends_when_the_service_resends_ready(harness: Harness) -> None:
    """更新服务忽略检查请求时重发当前状态：反馈随之结束，不停在“正在检查”。"""
    await harness.publish(status(UpdateState.AVAILABLE, available=RELEASE))
    harness.presenter.check_updates()
    await harness.publish(status(UpdateState.READY, available=RELEASE))
    assert harness.presenter.check_feedback is not None
    assert "正在检查" not in about(harness).update_card.status_label.text()


async def test_stale_check_failure_does_not_label_a_later_download_error(harness: Harness) -> None:
    presenter = harness.presenter
    await harness.publish(status(UpdateState.UP_TO_DATE))
    presenter.check_updates()
    await harness.publish(status(UpdateState.CHECKING))
    await harness.publish(status(UpdateState.ERROR, error_code=UpdateErrorCode.NETWORK))
    assert about(harness).update_card.status_label.text().startswith("检查失败")
    await harness.publish(status(UpdateState.CHECKING))  # 自动检查
    await harness.publish(status(UpdateState.AVAILABLE, available=RELEASE))
    await harness.publish(status(UpdateState.DOWNLOADING, available=RELEASE, progress=0.5))
    await harness.publish(status(UpdateState.ERROR, available=RELEASE, error_code=UpdateErrorCode.DISK))
    text = about(harness).update_card.status_label.text()
    assert not text.startswith("检查失败"), text
    assert presenter.check_feedback is None


# ---------------------------------------------------------------------- API key


async def test_api_key_is_committed_only_when_editing_finishes(harness: Harness) -> None:
    """输入中途停顿不提交半截 key；按 Enter 或离开输入框时提交一次，然后清空。"""
    window = await harness.open()
    window.show_page("engines")
    page = engines_page(harness)
    page.select("deepseek")
    field = page.secret_field
    assert field is not None
    edit = field.editor.edit
    edit.setFocus()
    for part in ("sk-abc", "def"):
        edit.insert(part)
        edit.textEdited.emit(edit.text())  # insert() 不发 textEdited，补发一次，等同用户键入
        await asyncio.sleep(0.7)  # 比默认防抖的 500 毫秒长
        await harness.flush()
    assert harness.recorded.of(UpdateSecret) == []
    edit.editingFinished.emit()
    await harness.flush()
    assert [c.value for c in harness.recorded.of(UpdateSecret)] == ["sk-abcdef"]
    assert edit.text() == ""


async def test_api_key_is_stripped_before_sending(harness: Harness) -> None:
    harness.presenter.set_api_key("deepseek", "  sk-abc\n")
    await harness.flush()
    assert [c.value for c in harness.recorded.of(UpdateSecret)] == ["sk-abc"]


async def test_blank_key_of_a_new_engine_is_not_saved_or_marked_set(harness: Harness) -> None:
    presenter = harness.presenter
    errors = presenter.add_engine("deepl", {"name": "DeepL 2"}, "   ")
    assert errors == {}
    await harness.flush()
    assert harness.recorded.of(UpdateSecret) == []
    new_id = presenter.pending_select
    assert new_id is not None
    await harness.publish(
        default_config(engines=[*harness.recorded.updates[-1].patch["engines"]], revision=3)  # type: ignore[misc]
    )
    assert presenter.key_state(new_id).value != "set"


async def test_add_engine_after_close_sends_nothing(harness: Harness) -> None:
    harness.presenter.close()
    harness.presenter.add_engine("deepl", {"name": "DeepL 2"}, "sk-1")
    await harness.flush()
    assert harness.recorded.commands == []


# ---------------------------------------------------------------------- 英文界面


async def test_cache_result_follows_the_interface_language(harness: Harness) -> None:
    await harness.publish(CacheCleared(3))
    label = race_page(harness).stats.cache_label
    before = label.text()
    await harness.publish(ENGLISH)
    assert label.text() != before and label.text().isascii(), label.text()
    assert label.accessibleName().isascii(), label.accessibleName()


async def test_english_ui_uses_english_punctuation(harness: Harness) -> None:
    await harness.publish(
        ENGLISH,
        EngineStatuses({EngineId("deepseek"): EngineStatus(EngineHealth.ERROR, "x", ErrorCode.AUTH)}, {}, {}),
        WorkerStatuses({"ocr": WorkerStatus(WorkerState.CRASHED), "nmt": WorkerStatus(WorkerState.FAILED)}),
    )
    page = engines_page(harness)
    tip = next(
        page.list.item(row).toolTip()
        for row in range(page.list.count())
        if page.list.item(row).data(Qt.ItemDataRole.UserRole) == "deepseek"
    )
    workers = [label.text() for label in about(harness).worker_labels.values()]
    assert tip and "。" not in tip, tip
    assert all("：" not in text and "，" not in text for text in workers), workers


# ---------------------------------------------------------------------- 键盘


async def _ctrl_tab_from(harness: Harness, page_id: str, widget: Callable[[SettingsPage], QWidget]) -> str:
    window = await harness.open()
    window.show_page(page_id)
    target = widget(window.page(page_id))
    target.setFocus()
    QApplication.processEvents()
    QTest.keyClick(target, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
    return window.current_page.page_id


async def test_ctrl_tab_from_the_stats_table(harness: Harness) -> None:
    def table(page: SettingsPage) -> QWidget:
        assert isinstance(page, RacePage)
        page.stats.table.setCurrentCell(0, 0)
        return page.stats.table

    assert await _ctrl_tab_from(harness, "race", table) == "models"


async def test_ctrl_tab_from_the_engine_list(harness: Harness) -> None:
    def engine_list(page: SettingsPage) -> QWidget:
        assert isinstance(page, EnginesPage)
        page.select("deepseek")
        return page.list

    assert await _ctrl_tab_from(harness, "engines", engine_list) == "race"


async def test_tab_leaves_the_stats_table(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("race")
    table = race_page(harness).stats.table
    table.setFocus()
    table.setCurrentCell(0, 0)
    QTest.keyClick(table, Qt.Key.Key_Tab)
    assert not table.hasFocus()


# ---------------------------------------------------------------------- 引擎拒绝位置


async def test_engine_rejection_is_not_pinned_on_the_wrong_instance(harness: Harness) -> None:
    """两次发送之间顺序变了：拒绝属于哪一次无法确定时，不把它显示在任何一个实例旁边。"""
    presenter = harness.presenter
    assert presenter.set_engine_field("argos", "name", "A")  # 第一次发送：argos 在下标 0
    await harness.flush()
    assert presenter.reorder_engines(["deepseek", "argos", "deepl", "ollama"])  # 第二次：deepseek 在下标 0
    await harness.flush()
    await harness.publish(ConfigRejected(("engines", 0, "name"), "被拒绝"))
    assert presenter.rejection_for(("engines", "deepseek")) is None
    assert presenter.rejection_for(("engines", "argos")) is None
    assert presenter.rejections == {("engines", 0, "name"): "被拒绝"}  # 窗口底部的提示条显示它


async def test_engine_rejection_maps_when_every_pending_send_agrees(harness: Harness) -> None:
    presenter = harness.presenter
    assert presenter.set_engine_field("deepseek", "name", "A")
    await harness.flush()
    assert presenter.set_engine_field("deepl", "name", "B")  # 顺序没变，下标 1 都是 deepseek
    await harness.flush()
    await harness.publish(ConfigRejected(("engines", 1, "name"), "被拒绝"))
    assert presenter.rejection_for(("engines", "deepseek", "name")) == "被拒绝"


async def test_confirmed_send_no_longer_takes_part(harness: Harness) -> None:
    presenter = harness.presenter
    assert presenter.set_engine_field("argos", "name", "A")
    await harness.flush()
    first = harness.recorded.updates[-1].patch["engines"]
    await harness.publish(default_config(engines=[*first], revision=2))  # type: ignore[misc]
    assert presenter.reorder_engines(["deepseek", "argos", "deepl", "ollama"])
    await harness.flush()
    await harness.publish(ConfigRejected(("engines", 0, "name"), "被拒绝"))
    assert presenter.rejection_for(("engines", "deepseek", "name")) == "被拒绝"


def test_engine_draft_expires_without_a_new_config() -> None:
    now = [0.0]
    draft = EngineDraft(max_age_s=2.0, clock=lambda: now[0])
    draft.sent([{"id": "a"}])
    assert draft.working(None) == [{"id": "a"}]
    now[0] = 3.0
    assert draft.working(None) == []  # 过期的草稿不再作为修改的基础
    assert not draft.pending


# ---------------------------------------------------------------------- 推理进程


async def test_restart_button_for_failed_workers(harness: Harness) -> None:
    await harness.publish(
        WorkerStatuses({"ocr": WorkerStatus(WorkerState.FAILED), "nmt": WorkerStatus(WorkerState.READY)})
    )
    page = about(harness)
    assert page.worker_restart_buttons["ocr"].isVisibleTo(page)
    assert not page.worker_restart_buttons["nmt"].isVisibleTo(page)
    page.worker_restart_buttons["ocr"].click()
    await harness.flush()
    assert harness.recorded.of(RestartWorker) == [RestartWorker("ocr")]
    assert page.worker_restart_buttons["ocr"].text() == "重新启动"


# ---------------------------------------------------------------------- 热键录入


def _hotkey_editor(harness: Harness, name: str) -> HotkeyEditor:
    page = harness.window.page("hotkeys")
    binding = next(b for b in page.bindings if b.location == ("hotkeys", name))
    assert isinstance(binding.editor, HotkeyEditor)
    return binding.editor


async def test_recording_pauses_and_resumes_global_hotkeys(harness: Harness, bus: EventBus) -> None:
    seen: list[HotkeyRecordingChanged] = []
    bus.subscribe(HotkeyRecordingChanged, seen.append)
    window = await harness.open()
    window.show_page("hotkeys")
    button = _hotkey_editor(harness, "capture").button
    button.setFocus()
    button.click()
    await harness.flush()
    assert seen == [HotkeyRecordingChanged(True)]
    QTest.keyClick(button, Qt.Key.Key_Escape)
    await harness.flush()
    assert seen == [HotkeyRecordingChanged(True), HotkeyRecordingChanged(False)]


async def test_closing_the_module_while_recording_resumes_hotkeys(harness: Harness, bus: EventBus) -> None:
    seen: list[HotkeyRecordingChanged] = []
    bus.subscribe(HotkeyRecordingChanged, seen.append)
    await harness.open()
    _hotkey_editor(harness, "capture").button.click()
    await harness.flush()
    await harness.stop()
    await bus.drain()
    assert seen[-1] == HotkeyRecordingChanged(False)


@pytest.mark.parametrize(("digit", "shifted"), [(1, Qt.Key.Key_Exclam), (2, Qt.Key.Key_At)])
async def test_shift_digit_is_recorded_by_virtual_key(harness: Harness, digit: int, shifted: Qt.Key) -> None:
    """Shift+数字时 Qt 给出的键是 Shift 之后的字符（! @），录入用虚拟键码得到数字本身。"""
    await harness.open()
    editor = _hotkey_editor(harness, "capture")
    editor.button.setFocus()
    editor.button.click()
    modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    event = QKeyEvent(QEvent.Type.KeyPress, shifted, modifiers, 0, 0x30 + digit, 0, "")
    QApplication.sendEvent(editor.button, event)
    await harness.flush()
    assert editor.button.hotkey() == f"Ctrl+Shift+{digit}"
    assert harness.recorded.updates[-1] == UpdateConfig({"hotkeys": {"capture": f"Ctrl+Shift+{digit}"}})


# ---------------------------------------------------------------------- 重复刷新


async def test_config_refreshes_the_engine_list_once(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = engines_page(harness)
    calls: list[int] = []
    original = page._sync_items
    monkeypatch.setattr(page, "_sync_items", lambda: (calls.append(1), original())[1])
    await harness.publish(default_config(revision=5))
    assert len(calls) == 1


async def test_language_switch_retranslates_the_engine_page_once(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = engines_page(harness)
    calls: list[int] = []
    original = page.retranslate
    monkeypatch.setattr(page, "retranslate", lambda: (calls.append(1), original())[1])
    await harness.publish(ENGLISH)
    assert len(calls) == 1
