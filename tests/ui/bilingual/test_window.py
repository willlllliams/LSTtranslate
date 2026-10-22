"""窗口的显示与隐藏、状态保存与恢复、置顶、键盘操作和布局（需求 BI-01、BI-02、BI-07、UI-07、CFG-05）。"""

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from lst.contracts.config import AppConfig
from lst.contracts.events import SegmentsUpdated, ToggleBilingualWindow
from lst.contracts.ids import Lang, SessionId
from lst.contracts.translation import AttemptState
from lst.ui import i18n
from lst.ui.bilingual.state import GROUP
from lst.ui.common.window import fit_on_screens
from tests.ui.bilingual.conftest import Harness, StartHarness, attempt, config, segment, statuses

_ON_TOP = Qt.WindowType.WindowStaysOnTopHint


async def _toggle(harness: Harness) -> None:
    harness.bus.send(ToggleBilingualWindow())
    await harness.flush()


def _write_state(path: Path, **values: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = QSettings(str(path), QSettings.Format.IniFormat)
    for key, value in values.items():
        settings.setValue(f"{GROUP}/{key}", value)
    settings.sync()


def _read_state(path: Path) -> dict[str, object]:
    settings = QSettings(str(path), QSettings.Format.IniFormat)
    settings.beginGroup(GROUP)
    return {key: settings.value(key) for key in settings.childKeys()}


def _available() -> QRect:
    screen = QGuiApplication.primaryScreen()
    return screen.availableGeometry()


async def test_window_is_created_hidden(harness: Harness) -> None:
    assert not harness.window.isVisible()
    assert harness.window.windowTitle() == "双语互译"


async def test_toggle_shows_focuses_input_and_hides(harness: Harness) -> None:
    window = harness.window
    await _toggle(harness)
    assert window.isVisible()
    assert window.focusWidget() is window.input_edit
    await _toggle(harness)
    assert not window.isVisible()
    await _toggle(harness)
    assert window.isVisible()


async def test_escape_and_close_button_hide_the_window(harness: Harness) -> None:
    window = harness.window
    await _toggle(harness)
    QTest.keyClick(window.input_edit, Qt.Key.Key_Escape)
    assert not window.isVisible()

    await _toggle(harness)
    window.target_combo.setFocus()
    QTest.keyClick(window.target_combo, Qt.Key.Key_Escape)
    assert not window.isVisible()

    # 标题栏的关闭按钮只隐藏窗口，窗口对象保留，下次照常显示。
    await _toggle(harness)
    assert window.close() is False
    assert not window.isVisible()
    await _toggle(harness)
    assert window.isVisible()


async def test_geometry_saved_on_hide_and_restored_after_restart(start_harness: StartHarness) -> None:
    first = await start_harness()
    area = _available()
    position = QPoint(area.x() + 40, area.y() + 50)
    size = QSize(700, 560)
    await _toggle(first)
    first.window.resize(size)
    first.window.move(position)
    await _toggle(first)  # 隐藏时保存
    saved = _read_state(first.settings_path)
    assert (int(str(saved["x"])), int(str(saved["y"]))) == (position.x(), position.y())
    assert (int(str(saved["width"])), int(str(saved["height"]))) == (size.width(), size.height())
    await first.stop()

    second = await start_harness(first.settings_path)
    assert not second.window.isVisible()
    await _toggle(second)
    assert second.window.pos() == position
    assert second.window.size() == size


async def test_geometry_saved_on_stop_while_visible(start_harness: StartHarness) -> None:
    first = await start_harness()
    area = _available()
    await _toggle(first)
    first.window.move(area.x() + 60, area.y() + 70)
    await first.stop()
    assert first.module.window is None
    saved = _read_state(first.settings_path)
    assert (int(str(saved["x"])), int(str(saved["y"]))) == (area.x() + 60, area.y() + 70)


async def test_never_shown_window_does_not_overwrite_saved_geometry(
    start_harness: StartHarness, tmp_path: Path
) -> None:
    path = tmp_path / "state.ini"
    _write_state(path, x=11, y=22, width=650, height=540)
    harness = await start_harness(path)
    await harness.stop()
    saved = _read_state(path)
    assert (str(saved["x"]), str(saved["y"])) == ("11", "22")


async def test_offscreen_position_moves_to_primary_center(
    start_harness: StartHarness, tmp_path: Path
) -> None:
    path = tmp_path / "state.ini"
    _write_state(path, x=-30000, y=-30000, width=600, height=500)
    harness = await start_harness(path)
    await _toggle(harness)
    frame = harness.window.frameGeometry()
    center = _available().center()
    assert abs(frame.center().x() - center.x()) <= 2
    assert abs(frame.center().y() - center.y()) <= 2
    assert harness.window.size() == QSize(600, 500)


async def test_first_show_without_saved_state_is_centered(harness: Harness) -> None:
    await _toggle(harness)
    frame = harness.window.frameGeometry()
    assert _available().contains(frame.center())


async def test_garbage_state_is_ignored(start_harness: StartHarness, tmp_path: Path) -> None:
    path = tmp_path / "state.ini"
    _write_state(path, x="left", y=5, width=-3, height="tall", on_top="maybe", source="xx", target="ja")
    harness = await start_harness(path)
    assert not harness.window.windowFlags() & _ON_TOP
    assert harness.window.languages() == (None, Lang("ja"))
    await _toggle(harness)
    assert _available().contains(harness.window.frameGeometry().center())


def test_fit_on_screens_keeps_visible_title_bar_and_recenters_otherwise() -> None:
    screens = [QRect(0, 0, 1920, 1040), QRect(1920, 0, 1280, 1024)]
    primary = screens[0]
    on_second = QRect(2000, 100, 600, 400)
    assert fit_on_screens(on_second, screens, primary) == on_second
    # 窗口大部分在屏外，但标题栏中点还在屏内：保留。
    hanging = QRect(-250, 900, 600, 400)
    assert fit_on_screens(hanging, screens, primary) == hanging
    # 标题栏在屏幕上方之外：移到主屏中央。
    above = QRect(100, -300, 600, 400)
    assert fit_on_screens(above, screens, primary) == QRect(660, 320, 600, 400)
    # 比主屏还大的窗口先缩小。
    huge = QRect(5000, 5000, 3000, 2000)
    assert fit_on_screens(huge, screens, primary) == QRect(0, 0, 1920, 1040)


async def test_pin_toggles_stay_on_top_and_is_restored(start_harness: StartHarness) -> None:
    first = await start_harness()
    window = first.window
    await _toggle(first)
    window.pin_button.click()
    assert window.windowFlags() & _ON_TOP
    assert window.isVisible(), "切换置顶不应隐藏窗口"
    assert window.windowHandle().flags() & _ON_TOP
    assert str(_read_state(first.settings_path)["on_top"]).lower() == "true"

    window.pin_button.click()
    assert not window.windowFlags() & _ON_TOP
    assert window.isVisible()
    window.pin_button.click()
    await first.stop()

    second = await start_harness(first.settings_path)
    assert second.window.windowFlags() & _ON_TOP
    assert second.window.pin_button.isChecked()
    await _toggle(second)
    assert second.window.windowFlags() & _ON_TOP


async def test_language_choice_is_restored(start_harness: StartHarness) -> None:
    first = await start_harness()
    first.window.source_combo.setCurrentIndex(first.window.source_combo.findData("en"))
    first.window.target_combo.setCurrentIndex(first.window.target_combo.findData("ko"))
    await first.flush()
    await first.stop()
    second = await start_harness(first.settings_path)
    assert second.window.languages() == (Lang("en"), Lang("ko"))
    assert second.presenter.languages == (Lang("en"), Lang("ko"))


async def test_tab_order_matches_visual_order(harness: Harness) -> None:
    session = (await harness.type_and_wait("one")).session_id
    await harness.updates(
        session,
        segment(
            "a",
            "one",
            attempt("deepl", AttemptState.DONE, "一", latency=20),
            attempt("argos", AttemptState.DONE, "壹", latency=5),
            winner="deepl",
        ),
    )
    window = harness.window
    rows = window.engine_list.rows()
    expected: list[QWidget] = [
        window.source_combo,
        window.swap_button,
        window.target_combo,
        window.pin_button,
        window.input_edit,
        window.clear_button,
        window.translation,
        window.copy_button,
        rows[0].copy_button,
        rows[1].copy_button,
    ]

    def chain() -> list[QWidget]:
        """从源语言下拉框出发绕焦点链一圈，收集能用 Tab 到达的可见控件。"""
        found: list[QWidget] = [window.source_combo]
        widget = window.source_combo.nextInFocusChain()
        while widget is not None and widget is not window.source_combo:
            if widget.focusPolicy() & Qt.FocusPolicy.TabFocus and widget.isVisibleTo(window):
                found.append(widget)
            widget = widget.nextInFocusChain()
        return found

    assert chain() == expected

    # 胜出者变化导致行重新排序后，焦点顺序跟着变。
    await harness.updates(
        session,
        segment(
            "a",
            "one",
            attempt("argos", AttemptState.DONE, "壹", latency=5),
            attempt("deepl", AttemptState.DONE, "一", latency=20),
            winner="argos",
        ),
    )
    rows = window.engine_list.rows()
    assert [row.engine_id for row in rows] == ["argos", "deepl"]
    assert chain()[-2:] == [rows[0].copy_button, rows[1].copy_button]


async def test_input_tab_moves_focus_instead_of_inserting(harness: Harness) -> None:
    edit = harness.window.input_edit
    assert edit.tabChangesFocus()
    QTest.keyClick(edit, Qt.Key.Key_Tab)
    assert "\t" not in edit.toPlainText()


async def test_layout_orientation_from_config(harness: Harness) -> None:
    splitter = harness.window.content_splitter
    assert splitter.orientation() == Qt.Orientation.Vertical
    await harness.publish(config(layout="horizontal", revision=2))
    assert splitter.orientation() == Qt.Orientation.Horizontal
    await harness.publish(config(layout="diagonal", revision=3))
    assert splitter.orientation() == Qt.Orientation.Vertical


async def test_interface_language_switch_retranslates(harness: Harness) -> None:
    session = (await harness.type_and_wait("hello")).session_id
    await harness.updates(
        session,
        segment("s", "hello", attempt("argos", AttemptState.DONE, "你好", latency=30), winner="argos"),
    )
    window = harness.window
    assert window.copy_button.text() == "复制译文"
    i18n.set_language("en")
    await harness.publish(config(revision=2))
    assert window.windowTitle() == "Bilingual Translator"
    assert window.copy_button.text() == "Copy translation"
    assert window.source_combo.itemText(0) == "Auto"
    assert window.target_combo.itemText(window.target_combo.findData("ja")) == "Japanese"
    row = window.engine_list.rows()[0]
    assert (row.status_label.text(), row.latency_label.text()) == ("Done", "30 ms")
    assert row.copy_button.text() == "Copy"


async def test_stop_closes_window_and_cancels_nothing_else(start_harness: StartHarness) -> None:
    harness = await start_harness()
    window = harness.window
    await _toggle(harness)
    await harness.stop()
    assert not window.isVisible()
    assert harness.module.window is None
    assert harness.module.presenter is None


async def test_stopped_presenter_ignores_late_messages(harness: Harness) -> None:
    """模块停止后、订阅取消之前排队的消息不再改动正在关闭的窗口。"""
    presenter, window = harness.presenter, harness.window
    presenter.shutdown()
    presenter.on_config(config(layout="horizontal"))
    presenter.on_segments_updated(SegmentsUpdated(SessionId("x"), (segment("a", "hello"),)))
    presenter.on_engine_statuses(statuses({"deepseek": "DeepSeek"}))
    assert window.content_splitter.orientation() == Qt.Orientation.Vertical
    assert presenter.translation_text() == ""


async def test_window_follows_the_language_in_the_config(harness: Harness) -> None:
    """配置里的界面语言由双语窗口自己应用：别的模块还没处理这份配置时，标题也已经换成英文。"""
    i18n.set_language("zh-Hans")
    await harness.publish(AppConfig(sections={"ui": {"language": "en"}}, revision=5))
    assert harness.window.windowTitle() == "Bilingual Translator"
