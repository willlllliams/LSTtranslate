"""“热键与键位”下三个窗口键位子页的测试（需求 UI-09）：每个子页只列出本窗口的操作与默认键位，录制按键、单击、
双击、拖动、滚轮生成正确的键位并发出 UpdateConfig，类型不符的输入被忽略，同窗口冲突的提示与替换，删除、
恢复默认、恢复本页默认（只动本窗口），配置被拒绝时回到原来的键位、显示原因并切到负责的子页，中英文文案齐全。

设置环境变量 ``LST_SETTINGS_SHOTS=1`` 时，``test_bindings_screenshots`` 把覆盖窗口子页截成浅色、深色两张图，
存到 data/tmp/settings-shots/bindings-overlay-*.png。
"""

import asyncio
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from lst.contracts.config import ConfigRejected
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus
from lst.ui import i18n
from lst.ui.common.bindings import ACTIONS, ACTIONS_BY_ID, Scope
from lst.ui.common.messages import HotkeyRecordingChanged
from lst.ui.settings.page_bindings import ActionRow, BindingRecorder, BindingsPage, binding_display, page_id
from lst.ui.settings.pages import GlobalHotkeysPage
from lst.ui.settings.strings_bindings import CATALOG
from lst.ui.theme import apply_app_style
from tests.ui.conftest import on_native_platform, wait_until
from tests.ui.settings.conftest import Harness, StartFull, StartSettings
from tests.ui.settings.support import FakeDialogs, default_config

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def page_of(harness: Harness, scope: Scope) -> BindingsPage:
    page = harness.window.page(page_id(scope))
    assert isinstance(page, BindingsPage)
    return page


def row_of(harness: Harness, action: str) -> ActionRow:
    """操作所在子页上的卡片。窗口可见时先切到那个子页，录制区要在可见的页面上才能获得焦点。"""
    scope = ACTIONS_BY_ID[action].scope
    window = harness.window
    if window.isVisible() and window.current_page.page_id != page_id(scope):
        window.show_page(page_id(scope))
        QApplication.processEvents()
    return page_of(harness, scope).keymap.row(action)


def shown_bindings(row: ActionRow) -> list[str]:
    return [chip.text for chip in row.chips if not chip.isHidden()]


def patches(harness: Harness) -> list[dict[str, JsonValue]]:
    return [dict(command.patch) for command in harness.recorded.updates]


@pytest.fixture
async def shown(harness: Harness) -> Harness:
    window = await harness.open()
    window.resize(1000, 800)
    window.show_page("hotkeys.overlay")
    QApplication.processEvents()
    return harness


def start(row: ActionRow) -> BindingRecorder:
    row.add_button.click()
    recorder = row.recorder
    assert recorder.recording and not row.recorder_row.isHidden()
    recorder.click_timer.setInterval(20)
    return recorder


def send_mouse(
    recorder: BindingRecorder,
    kind: QEvent.Type,
    at: tuple[float, float],
    button: Qt.MouseButton,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    position = QPointF(*at)
    held = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button
    if kind == QEvent.Type.MouseMove:
        button = Qt.MouseButton.NoButton
    event = QMouseEvent(kind, position, recorder.mapToGlobal(position), button, held, modifiers)
    QApplication.sendEvent(recorder, event)


def click(recorder: BindingRecorder, button: Qt.MouseButton, modifiers: Qt.KeyboardModifier) -> None:
    send_mouse(recorder, QEvent.Type.MouseButtonPress, (20, 20), button, modifiers)
    send_mouse(recorder, QEvent.Type.MouseButtonRelease, (20, 20), button, modifiers)


def drag(recorder: BindingRecorder, button: Qt.MouseButton, modifiers: Qt.KeyboardModifier) -> None:
    send_mouse(recorder, QEvent.Type.MouseButtonPress, (10, 10), button, modifiers)
    send_mouse(recorder, QEvent.Type.MouseMove, (40, 30), button, modifiers)
    send_mouse(recorder, QEvent.Type.MouseButtonRelease, (40, 30), button, modifiers)


def wheel(recorder: BindingRecorder, modifiers: Qt.KeyboardModifier) -> None:
    event = QWheelEvent(
        QPointF(20, 20),
        recorder.mapToGlobal(QPointF(20, 20)),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(recorder, event)


# ---------------------------------------------------------------- 列出操作


async def test_each_window_page_lists_only_its_own_actions(shown: Harness) -> None:
    for scope in Scope:
        page = page_of(shown, scope)
        assert page.heading.text() == f"热键与键位 › {i18n.tr(f'settings.nav.hotkeys.{scope.value}')}"
        expected = [spec.id for spec in ACTIONS if spec.scope is scope]
        assert list(page.keymap.rows) == expected
        assert not page.keymap.reset_button.isEnabled()  # 全是默认值
        assert page.bindings == []  # 全局热键不在窗口子页上
    for spec in ACTIONS:
        row = row_of(shown, spec.id)
        assert shown_bindings(row) == list(spec.defaults), spec.id
        assert row.title_label.text() == i18n.tr(f"bindings.action.{spec.id}")
        assert not row.reset_button.isEnabled()  # 已经是默认值


async def test_global_hotkeys_have_their_own_page(shown: Harness) -> None:
    page = shown.window.page("hotkeys.global")
    assert isinstance(page, GlobalHotkeysPage)
    assert shown.window.page("hotkeys") is page  # 旧的一级标识映射到第一个子页
    assert page.heading.text() == "热键与键位 › 全局热键"
    assert [b.location for b in page.bindings] == [("hotkeys", "capture"), ("hotkeys", "bilingual")]


async def test_overlay_group_explains_that_zoom_scales_the_whole_image(shown: Harness) -> None:
    texts = [label.text() for label, _ in page_of(shown, Scope.OVERLAY)._intros]  # pyright: ignore[reportPrivateUsage]
    assert any("缩放的是整张图（底图与译文一起），默认用滚轮" in text for text in texts)


async def test_copy_is_not_listed_and_the_intros_explain_selection(shown: Harness) -> None:
    """复制用系统标准的复制键，不进键位表：设置页没有“复制”一行，覆盖窗口与双语窗口的说明里写明怎么复制。"""
    pages = [page_of(shown, scope) for scope in Scope]
    rows = [row for page in pages for row in page.keymap.rows.values()]
    assert not {"overlay.copy", "bilingual.copy"} & {row.spec.id for row in rows}
    titles = [row.title_label.text() for row in rows]
    assert "复制" not in titles and "复制译文" not in titles
    assert row_of(shown, "overlay.select").title_label.text() == "选择文字"
    texts = [label.text() for page in pages for label, _ in page._intros]  # pyright: ignore[reportPrivateUsage]
    assert any("逐字选中" in text and "Ctrl+C" in text for text in texts)
    assert any("复制译文" in text and "Ctrl+C" in text for text in texts)


async def test_bindings_are_shown_with_localized_names(shown: Harness) -> None:
    assert binding_display("RightClick") == "右键单击"
    assert binding_display("Ctrl+LeftDrag") == "Ctrl+左键拖动"
    assert binding_display("MiddleDoubleClick") == "中键双击"
    assert binding_display("Wheel") == "滚轮"
    assert binding_display("Ctrl+Shift+C") == "Ctrl+Shift+C"
    toggle = row_of(shown, "overlay.toggle_source")
    assert toggle.chips[0].label.text() == "右键单击"
    await shown.publish(default_config({"ui": {"language": "en"}}, revision=2))
    assert binding_display("RightClick") == "Right click"
    assert binding_display("Ctrl+LeftDrag") == "Ctrl+Left drag"
    assert toggle.chips[0].label.text() == "Right click"
    assert toggle.title_label.text() == "Switch between source and translation"


# ---------------------------------------------------------------- 录制


async def test_recording_a_key_sends_update_config(shown: Harness, bus: EventBus) -> None:
    states: list[bool] = []
    bus.subscribe(HotkeyRecordingChanged, lambda message: states.append(message.active))
    row = row_of(shown, "overlay.zoom_reset")
    recorder = start(row)
    QTest.keyClick(recorder, Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier)  # 只按修饰键：继续等
    assert recorder.recording
    QTest.keyClick(
        recorder, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    )
    await shown.flush()
    assert not recorder.recording and row.recorder_row.isHidden()
    assert patches(shown) == [{"bindings": {"overlay_zoom_reset": ["Ctrl+0", "Ctrl+Shift+K"]}}]
    assert shown_bindings(row) == ["Ctrl+0", "Ctrl+Shift+K"]  # 新配置到达之前先显示提交的键位
    assert states == [True, False]  # 录制期间暂停全局热键


async def test_recording_clicks(shown: Harness) -> None:
    row = row_of(shown, "overlay.zoom_reset")
    recorder = start(row)
    click(recorder, Qt.MouseButton.RightButton, Qt.KeyboardModifier.ControlModifier)
    assert recorder.recording  # 等双击间隔过去
    await wait_until(lambda: not recorder.recording)
    await shown.flush()
    assert patches(shown)[-1] == {"bindings": {"overlay_zoom_reset": ["Ctrl+0", "Ctrl+RightClick"]}}

    row = row_of(shown, "bilingual.pin")
    recorder = start(row)
    click(recorder, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    send_mouse(recorder, QEvent.Type.MouseButtonDblClick, (20, 20), Qt.MouseButton.LeftButton)
    send_mouse(recorder, QEvent.Type.MouseButtonRelease, (20, 20), Qt.MouseButton.LeftButton)
    await shown.flush()
    assert not recorder.recording
    assert patches(shown)[-1] == {"bindings": {"bilingual_pin": ["Ctrl+Shift+T", "LeftDoubleClick"]}}


async def test_recording_a_drag(shown: Harness) -> None:
    row = row_of(shown, "overlay.move")
    recorder = start(row)
    click(recorder, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)  # 单击不是拖动：忽略
    QTest.keyClick(recorder, Qt.Key.Key_M, Qt.KeyboardModifier.ControlModifier)  # 按键：忽略
    wheel(recorder, Qt.KeyboardModifier.NoModifier)  # 滚轮：忽略
    await asyncio.sleep(0.05)
    assert recorder.recording and patches(shown) == []
    drag(recorder, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier)
    await shown.flush()
    assert patches(shown) == [{"bindings": {"overlay_move": ["RightDrag", "Ctrl+LeftDrag"]}}]


async def test_recording_the_wheel(shown: Harness) -> None:
    row = row_of(shown, "overlay.zoom")
    recorder = start(row)
    QTest.keyClick(recorder, Qt.Key.Key_Z)  # 按键：忽略
    drag(recorder, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)  # 拖动：忽略
    assert recorder.recording
    wheel(recorder, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    await shown.flush()
    assert patches(shown) == [{"bindings": {"overlay_zoom": ["Wheel", "Ctrl+Shift+Wheel"]}}]


async def test_trigger_ignores_drags_and_the_wheel(shown: Harness) -> None:
    recorder = start(row_of(shown, "overlay.pin"))
    drag(recorder, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    wheel(recorder, Qt.KeyboardModifier.NoModifier)
    await asyncio.sleep(0.05)
    assert recorder.recording and patches(shown) == []


async def test_escape_cancels_and_does_not_close_the_window(shown: Harness, bus: EventBus) -> None:
    row = row_of(shown, "overlay.zoom_reset")
    recorder = start(row)
    QTest.keyClick(recorder, Qt.Key.Key_Escape)
    await shown.flush()
    assert not recorder.recording and row.recorder_row.isHidden()
    assert shown.window.isVisible()
    assert patches(shown) == []


async def test_manual_entry_binds_escape(start_settings: StartSettings) -> None:
    harness = await start_settings(config=default_config({"bindings": {"overlay_close": ["F4"]}}))
    await harness.open()
    row = row_of(harness, "overlay.pin")
    start(row)
    row.manual_button.click()
    assert not row.recorder.recording and not row.manual_row.isHidden()
    QTest.keyClicks(row.manual_edit, "esc")
    QTest.keyClick(row.manual_edit, Qt.Key.Key_Return)
    await harness.flush()
    assert row.manual_row.isHidden() and row.conflict_row.isHidden()
    assert patches(harness) == [{"bindings": {"overlay_pin": ["MiddleClick", "Esc"]}}]


async def test_manual_entry_is_checked_with_parse_binding(shown: Harness) -> None:
    row = row_of(shown, "overlay.zoom")
    row.open_manual()
    row.manual_edit.setText("Ctrl+Bogus+Q")
    row.manual_ok.click()
    assert "Bogus" in row.error_text or "无效" in row.error_text
    row.manual_edit.setText("Ctrl+C")  # 键盘键位不能给滚轮操作
    row.manual_ok.click()
    assert "只能用滚轮" in row.error_text
    QTest.keyClick(row.manual_edit, Qt.Key.Key_Escape)  # Esc 只关闭手动输入
    assert row.manual_row.isHidden() and shown.window.isVisible()
    assert patches(shown) == []


# ---------------------------------------------------------------- 冲突


async def test_conflict_in_the_same_window_offers_replace(shown: Harness) -> None:
    pin = row_of(shown, "overlay.pin")
    pin.open_manual()
    pin.manual_edit.setText("Esc")
    pin.manual_ok.click()  # Esc 已经是覆盖窗口“关闭”的键位
    assert patches(shown) == []
    assert not pin.conflict_row.isHidden()
    assert "与“关闭”冲突" in pin.conflict_label.text() and "Esc" in pin.conflict_label.text()
    pin.replace_button.click()
    await shown.flush()
    assert patches(shown) == [{"bindings": {"overlay_close": [], "overlay_pin": ["MiddleClick", "Esc"]}}]
    assert pin.conflict_row.isHidden()
    assert shown_bindings(pin) == ["MiddleClick", "Esc"]
    assert shown_bindings(row_of(shown, "overlay.close")) == []


async def test_conflict_cancel_changes_nothing(shown: Harness) -> None:
    clear = row_of(shown, "bilingual.clear")
    recorder = start(clear)
    QTest.keyClick(recorder, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)  # 双语窗口“立即翻译”在用
    assert not clear.conflict_row.isHidden()
    clear.conflict_cancel.click()
    assert clear.conflict_row.isHidden() and patches(shown) == []


async def test_same_binding_in_another_window_is_allowed(shown: Harness) -> None:
    row = row_of(shown, "bilingual.clear")
    recorder = start(row)
    # 覆盖窗口的“还原大小”也是 Ctrl+0
    QTest.keyClick(recorder, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier)
    await shown.flush()
    assert row.conflict_row.isHidden()
    assert patches(shown) == [{"bindings": {"bilingual_clear": ["Ctrl+Shift+L", "Ctrl+0"]}}]


async def test_existing_binding_is_not_added_twice(shown: Harness) -> None:
    row = row_of(shown, "overlay.zoom_reset")
    recorder = start(row)
    QTest.keyClick(recorder, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier)
    assert "已经有 Ctrl+0" in row.error_text and patches(shown) == []


# ---------------------------------------------------------------- 删除与恢复默认


async def test_remove_a_binding(shown: Harness) -> None:
    row = row_of(shown, "capture.cancel")
    assert shown_bindings(row) == ["RightClick", "Esc"]
    row.chips[0].remove_button.click()
    await shown.flush()
    assert patches(shown) == [{"bindings": {"capture_cancel": ["Esc"]}}]
    assert shown_bindings(row) == ["Esc"]


async def test_the_add_button_is_disabled_at_the_limit(start_settings: StartSettings) -> None:
    config = default_config({"bindings": {"overlay_zoom_reset": ["Ctrl+0", "Ctrl+Shift+F2", "F2"]}})
    harness = await start_settings(config=config)
    row = row_of(harness, "overlay.zoom_reset")
    assert shown_bindings(row) == ["Ctrl+0", "Ctrl+Shift+F2", "F2"]
    assert not row.add_button.isEnabled()


async def test_reset_one_and_reset_this_page(start_settings: StartSettings) -> None:
    dialogs = FakeDialogs()
    config = default_config(
        {"bindings": {"overlay_zoom_reset": ["F3"], "overlay_close": [], "bilingual_swap": ["Ctrl+W"]}}
    )
    harness = await start_settings(config=config, dialogs=dialogs)
    await harness.open()
    zoom_reset = row_of(harness, "overlay.zoom_reset")
    assert zoom_reset.reset_button.isEnabled()
    zoom_reset.reset_button.click()
    await harness.flush()
    assert patches(harness) == [{"bindings": {"overlay_zoom_reset": ["Ctrl+0"]}}]

    overlay = page_of(harness, Scope.OVERLAY).keymap
    bilingual = page_of(harness, Scope.BILINGUAL).keymap
    assert not page_of(harness, Scope.CAPTURE).keymap.reset_button.isEnabled()
    assert overlay.reset_button.isEnabled() and bilingual.reset_button.isEnabled()
    dialogs.confirm_answer = False
    overlay.reset_button.click()
    assert len(dialogs.confirms) == 1 and len(patches(harness)) == 1  # 没有确认：不改
    assert "覆盖窗口" in dialogs.confirms[0][1] and "其他窗口的键位不变" in dialogs.confirms[0][1]
    dialogs.confirm_answer = True
    overlay.reset_button.click()
    await harness.flush()
    # 只恢复本页（覆盖窗口）的键位，双语窗口的修改保留。
    assert patches(harness)[-1] == {"bindings": {"overlay_close": ["Esc"]}}
    for spec in ACTIONS:
        if spec.scope is Scope.OVERLAY:
            assert shown_bindings(row_of(harness, spec.id)) == list(spec.defaults), spec.id
    assert shown_bindings(row_of(harness, "bilingual.swap")) == ["Ctrl+W"]
    assert bilingual.reset_button.isEnabled()

    bilingual.reset_button.click()
    await harness.flush()
    assert patches(harness)[-1] == {"bindings": {"bilingual_swap": ["Ctrl+Shift+S"]}}
    assert "双语窗口" in dialogs.confirms[-1][1]


async def test_reset_to_a_default_that_another_action_took_offers_replace(
    start_settings: StartSettings,
) -> None:
    config = default_config({"bindings": {"overlay_close": ["F4"], "overlay_pin": ["MiddleClick", "Esc"]}})
    harness = await start_settings(config=config)
    close = row_of(harness, "overlay.close")
    close.reset_button.click()
    assert not close.conflict_row.isHidden() and patches(harness) == []
    close.replace_button.click()
    await harness.flush()
    assert patches(harness) == [{"bindings": {"overlay_pin": ["MiddleClick"], "overlay_close": ["Esc"]}}]


# ---------------------------------------------------------------- 配置的变化与拒绝


async def test_new_config_replaces_the_pending_value(shown: Harness) -> None:
    row = row_of(shown, "overlay.close")
    row.chips[0].remove_button.click()
    await shown.flush()
    assert shown_bindings(row) == []
    await shown.publish(default_config({"bindings": {"overlay_close": []}}, revision=2))
    assert row.pending is None and shown_bindings(row) == []
    await shown.publish(default_config({"bindings": {"overlay_close": ["F5"]}}, revision=3))
    assert shown_bindings(row) == ["F5"]


async def test_rejection_rolls_back_and_shows_the_reason(shown: Harness) -> None:
    row = row_of(shown, "overlay.close")
    recorder = start(row)
    QTest.keyClick(recorder, Qt.Key.Key_F6)
    await shown.flush()
    assert shown_bindings(row) == ["Esc", "F6"]
    await shown.publish(ConfigRejected(("bindings", "overlay_close"), "Value error, 写入配置文件失败"))
    assert shown_bindings(row) == ["Esc"]  # 回到保存前的键位
    assert row.error_text == "未保存：写入配置文件失败"
    assert shown.window.notice_label.isHidden()  # 卡片负责显示，不进窗口底部的提示条
    start(row)  # 再次修改时提示消失
    assert row.error_text == ""


async def test_rejection_on_another_window_switches_to_its_page(shown: Harness) -> None:
    """[bindings] 里双语窗口的字段被拒绝时，窗口从覆盖窗口子页切到双语窗口子页，原因显示在对应卡片下。"""
    window = shown.window
    assert window.current_page.page_id == "hotkeys.overlay"
    await shown.publish(ConfigRejected(("bindings", "bilingual_clear"), "写入配置文件失败"))
    assert window.current_page.page_id == "hotkeys.bilingual"
    assert window.nav.currentItem() is window.nav.item_of("hotkeys.bilingual")
    assert (
        page_of(shown, Scope.BILINGUAL).keymap.row("bilingual.clear").error_text == "未保存：写入配置文件失败"
    )
    assert window.notice_label.isHidden()


async def test_round_trip_through_the_config_service(start_full: StartFull) -> None:
    full = await start_full()
    window = await full.open()
    window.show_page("hotkeys.overlay")
    pin = row_of(full, "overlay.pin")
    pin.open_manual()
    pin.manual_edit.setText("Esc")
    pin.manual_ok.click()
    pin.replace_button.click()
    await full.settle()
    config = full.presenter.config
    assert config is not None
    section = config.sections["bindings"]
    assert isinstance(section, dict)
    assert section["overlay_pin"] == ["MiddleClick", "Esc"] and section["overlay_close"] == []
    assert pin.pending is None and shown_bindings(pin) == ["MiddleClick", "Esc"]
    assert "overlay_pin" in full.config_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 文案


def test_texts_have_both_languages_with_the_same_placeholders() -> None:
    for key, texts in CATALOG.items():
        assert set(texts) == set(i18n.SUPPORTED), key
        assert all(text for text in texts.values()), key
        zh, en = texts["zh-Hans"], texts["en"]
        assert set(_PLACEHOLDER.findall(zh)) == set(_PLACEHOLDER.findall(en)), key
    for scope in Scope:
        assert i18n.is_registered(f"settings.nav.hotkeys.{scope.value}")
        assert f"settings.bindings.scope.{scope.value}.intro" in CATALOG
    for spec in ACTIONS:
        assert i18n.is_registered(f"bindings.action.{spec.id}"), spec.id
        assert f"settings.bindings.record.{spec.kind.value}" in CATALOG


@pytest.mark.parametrize("language", ["zh-Hans", "en"])
async def test_every_label_is_translated(harness: Harness, language: str) -> None:
    await harness.publish(default_config({"ui": {"language": language}}, revision=5))
    for spec in ACTIONS:
        row = page_of(harness, spec.scope).keymap.row(spec.id)
        row.start_recording()
        row.recorder.cancel()
        texts = [
            row.title_label.text(),
            row.recorder.label.text(),
            row.manual_button.text(),
            row.add_button.accessibleName(),
            row.reset_button.toolTip(),
        ]
        assert all(text and "settings." not in text for text in texts), (language, spec.id, texts)


# ---------------------------------------------------------------- 截图（手动开启）


@pytest.fixture
def restore_scheme(qapp: QApplication) -> Iterator[None]:
    yield
    apply_app_style(qapp, "system")


@pytest.mark.skipif(not os.environ.get("LST_SETTINGS_SHOTS"), reason="只在需要截图时运行")
@pytest.mark.usefixtures("restore_scheme")
async def test_bindings_screenshots(start_settings: StartSettings, qapp: QApplication) -> None:
    target = Path(__file__).resolve().parents[3] / "data" / "tmp" / "settings-shots"
    target.mkdir(parents=True, exist_ok=True)
    for scheme in ("light", "dark"):
        apply_app_style(qapp, scheme)
        config = default_config(
            {"bindings": {"overlay_zoom_reset": ["Ctrl+0", "Ctrl+Shift+F2"], "overlay_pin": []}}
        )
        harness = await start_settings(config=config)
        window = await harness.open()
        if on_native_platform():
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.resize(1100, 900)
        window.show_page("hotkeys.overlay")
        close = row_of(harness, "overlay.close")
        close.open_manual()
        close.manual_edit.setText("RightClick")
        close.manual_ok.click()  # 与“切换原文与译文”冲突：显示冲突提示
        await harness.publish(ConfigRejected(("bindings", "overlay_zoom"), "写入配置文件失败"))
        QApplication.processEvents()
        content = page_of(harness, Scope.OVERLAY).widget()
        assert content is not None
        content.adjustSize()
        content.grab().save(str(target / f"bindings-overlay-{scheme}.png"))
        await harness.stop()
