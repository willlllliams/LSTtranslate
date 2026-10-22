"""关于页的更新卡片与 [updates] 节设置项的测试（需求 UPD-01 至 UPD-06、UI-05、UI-07）。"""

from dataclasses import dataclass

import pytest
from PySide6.QtWidgets import QApplication

from lst.contracts.updates import (
    CheckForUpdates,
    DownloadUpdate,
    InstallUpdate,
    ReleaseInfo,
    UpdateErrorCode,
    UpdateState,
    UpdateStatus,
)
from lst.ui.settings.controls import IntEditor, SwitchEditor
from lst.ui.settings.page_about import AboutPage, UpdateCard
from lst.ui.settings.strings import format_timestamp
from lst.ui.theme import Glyph
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import FakeDialogs, default_config
from tests.ui.settings.test_keyboard import assert_visual_order, tab_stops

PAGE_URL = "https://github.com/willlllliams/LSTtranslate/releases/tag/v0.2.0"

RELEASE = ReleaseInfo(
    version="0.2.0",
    prerelease=False,
    published_at="2026-10-20T08:00:00Z",
    notes="## 新功能",
    page_url=PAGE_URL,
    installer_bytes=150 * 1024 * 1024,
)


def status(
    state: UpdateState,
    *,
    available: ReleaseInfo | None = None,
    progress: float | None = None,
    error_code: UpdateErrorCode | None = None,
    can_install: bool = True,
    last_checked: str | None = None,
) -> UpdateStatus:
    return UpdateStatus(
        state=state,
        current_version="0.1.0",
        available=available,
        progress=progress,
        last_checked=last_checked,
        error_code=error_code,
        can_install=can_install,
    )


def about(harness: Harness) -> AboutPage:
    page = harness.window.page("about")
    assert isinstance(page, AboutPage)
    return page


def update_card(harness: Harness) -> UpdateCard:
    return about(harness).update_card


def shown_buttons(card: UpdateCard) -> list[str]:
    buttons = (
        ("notes", card.notes_button),
        ("check", card.check_button),
        ("download", card.download_button),
        ("install", card.install_button),
    )
    return [name for name, button in buttons if not button.isHidden()]


# ---------------------------------------------------------------- 没有更新状态


async def test_without_update_status_the_card_says_updates_are_unavailable(harness: Harness) -> None:
    card = update_card(harness)
    assert card.title_label.text() == "当前版本 9.9.9"  # 取自 SettingsModule 的 app_version
    assert card.status_label.text() == "更新功能暂时不可用。"
    assert not card.check_button.isEnabled()
    assert shown_buttons(card) == ["check"]
    assert card.source_label.isHidden()


# ---------------------------------------------------------------- 各状态


@dataclass(frozen=True)
class Case:
    status: UpdateStatus
    text: str
    buttons: list[str]
    check_enabled: bool
    progress: int | None = None  # None：不显示；-1：忙碌动画；否则是进度条的取值（0 至 1000）
    error: bool = False


CASES = {
    "idle": Case(status(UpdateState.IDLE), "还没有检查过新版本。", ["check"], True),
    "checking": Case(status(UpdateState.CHECKING), "正在检查新版本…", ["check"], False),
    "up_to_date": Case(status(UpdateState.UP_TO_DATE), "已是最新版本。", ["check"], True),
    "available": Case(
        status(UpdateState.AVAILABLE, available=RELEASE),
        f"发现新版本 0.2.0，发布于 {format_timestamp(RELEASE.published_at, with_time=False)}。",
        ["notes", "check", "download"],
        True,
    ),
    "downloading": Case(
        status(UpdateState.DOWNLOADING, available=RELEASE, progress=0.42),
        "正在下载 0.2.0：42%",
        ["notes", "check"],
        False,
        420,
    ),
    "downloading_unknown": Case(
        status(UpdateState.DOWNLOADING, available=RELEASE),
        "正在下载 0.2.0…",
        ["notes", "check"],
        False,
        -1,
    ),
    "ready": Case(
        status(UpdateState.READY, available=RELEASE),
        "0.2.0 已下载并通过校验。安装时程序会退出，装完后自动重新启动。",
        ["notes", "check", "install"],
        False,  # 安装包已下载时更新服务不再检查
    ),
    "installing": Case(
        status(UpdateState.INSTALLING, available=RELEASE),
        "正在启动安装程序，程序即将退出…",
        ["notes", "check"],
        False,
    ),
    "error_with_release": Case(
        status(UpdateState.ERROR, available=RELEASE, error_code=UpdateErrorCode.CHECKSUM),
        "出错：安装包没有通过 SHA-256 校验，已经删除。处理建议：重新下载。",
        ["notes", "check", "download"],
        True,
        error=True,
    ),
    "error_without_release": Case(
        status(UpdateState.ERROR, error_code=UpdateErrorCode.NETWORK),
        "出错：网络连接失败。处理建议：检查网络连接或代理设置后重试。",
        ["check"],
        True,
        error=True,
    ),
}


@pytest.mark.parametrize("name", list(CASES))
async def test_card_follows_every_update_state(harness: Harness, name: str) -> None:
    case = CASES[name]
    await harness.publish(case.status)
    card = update_card(harness)
    assert card.title_label.text() == "当前版本 0.1.0"
    assert card.status_label.text() == case.text
    assert shown_buttons(card) == case.buttons
    assert card.check_button.isEnabled() is case.check_enabled
    assert (card.status_icon.text() == Glyph.ERROR) is case.error
    if case.progress is None:
        assert card.progress_bar.isHidden()
    elif case.progress < 0:
        assert not card.progress_bar.isHidden() and card.progress_bar.maximum() == 0
    else:
        assert not card.progress_bar.isHidden() and card.progress_bar.value() == case.progress


async def test_prerelease_size_and_last_checked_are_shown(harness: Harness) -> None:
    pre = ReleaseInfo("0.2.0-beta.1", True, "2026-10-20T08:00:00Z", "", PAGE_URL, 3 * 1024**3)
    checked = "2026-10-21T09:30:00Z"
    await harness.publish(status(UpdateState.AVAILABLE, available=pre, last_checked=checked))
    card = update_card(harness)
    assert card.status_label.text().startswith("发现新版本 0.2.0-beta.1（预发布版本）")
    assert card.desc_label.text() == f"上次检查：{format_timestamp(checked)}；安装包约 3.0 GB"


async def test_running_from_source_only_checks(harness: Harness) -> None:
    await harness.publish(status(UpdateState.AVAILABLE, available=RELEASE, can_install=False))
    card = update_card(harness)
    assert shown_buttons(card) == ["notes", "check"]  # 不提供下载
    assert not card.source_label.isHidden()
    assert "只检查新版本，不下载也不安装" in card.source_label.text()
    assert "安装包" not in card.desc_label.text()


# ---------------------------------------------------------------- 命令


async def test_buttons_send_download_and_install_and_open_the_release_notes(
    start_settings: StartSettings,
) -> None:
    dialogs = FakeDialogs(confirm=False)
    harness = await start_settings(dialogs=dialogs)
    await harness.publish(status(UpdateState.AVAILABLE, available=RELEASE))
    card = update_card(harness)
    card.download_button.click()
    card.notes_button.click()
    await harness.publish(status(UpdateState.READY, available=RELEASE))
    card.install_button.click()  # 用户在确认框里选了“否”
    await harness.flush()
    assert harness.recorded.commands == [DownloadUpdate()]
    assert dialogs.urls == [PAGE_URL]
    assert "安装 0.2.0？" in dialogs.confirms[0][1]
    dialogs.confirm_answer = True
    card.install_button.click()
    await harness.flush()
    assert harness.recorded.commands == [DownloadUpdate(), InstallUpdate()]


async def test_manual_check_reports_up_to_date(harness: Harness) -> None:
    await harness.publish(status(UpdateState.UP_TO_DATE))
    card = update_card(harness)
    card.check_button.click()
    await harness.flush()
    assert harness.recorded.commands == [CheckForUpdates(manual=True)]
    assert card.status_label.text() == "正在检查新版本…"  # 点击后立即有反馈，不等更新服务
    await harness.publish(status(UpdateState.CHECKING))
    assert not card.check_button.isEnabled()
    await harness.publish(status(UpdateState.UP_TO_DATE, last_checked="2026-10-22T10:00:00Z"))
    assert card.status_label.text() == "检查完成：0.1.0 已是最新版本。"
    assert card.check_button.isEnabled()


async def test_manual_check_reports_a_new_version(harness: Harness) -> None:
    await harness.publish(status(UpdateState.IDLE))
    card = update_card(harness)
    card.check_button.click()
    await harness.publish(status(UpdateState.CHECKING), status(UpdateState.AVAILABLE, available=RELEASE))
    assert card.status_label.text().startswith("检查完成：发现新版本 0.2.0，发布于")


async def test_manual_check_reports_a_failure(harness: Harness) -> None:
    await harness.publish(status(UpdateState.IDLE))
    card = update_card(harness)
    card.check_button.click()
    await harness.publish(status(UpdateState.CHECKING))
    await harness.publish(status(UpdateState.ERROR, error_code=UpdateErrorCode.TIMEOUT))
    assert card.status_label.text() == "检查失败：连接 GitHub 超时。处理建议：稍后重试。"
    assert card.status_icon.text() == Glyph.ERROR


async def test_automatic_checks_do_not_say_check_finished(harness: Harness) -> None:
    await harness.publish(status(UpdateState.CHECKING), status(UpdateState.UP_TO_DATE))
    assert update_card(harness).status_label.text() == "已是最新版本。"


# ---------------------------------------------------------------- [updates] 节


async def test_updates_settings_are_on_the_about_page(harness: Harness) -> None:
    page = about(harness)
    auto, interval, pre = (
        page.binding(name) for name in ("auto_check", "check_interval_hours", "include_prerelease")
    )
    assert auto is not None and interval is not None and pre is not None
    assert auto.card.title_label.text() == "自动检查更新"
    assert interval.card.title_label.text() == "检查间隔"
    assert pre.card.title_label.text() == "接收预发布版本"
    assert isinstance(auto.editor, SwitchEditor) and auto.editor.json_value() is True
    assert isinstance(interval.editor, IntEditor)
    assert interval.editor.spin.suffix() == " 小时" and interval.editor.json_value() == 24
    assert (interval.editor.spin.minimum(), interval.editor.spin.maximum()) == (1, 720)
    assert isinstance(pre.editor, SwitchEditor) and pre.editor.json_value() is True

    auto.editor.switch.click()
    await harness.flush()
    assert harness.recorded.updates[-1].patch == {"updates": {"auto_check": False}}
    await harness.publish(default_config({"updates": {"auto_check": False}}, revision=2))
    assert not interval.card.isEnabled()  # 不自动检查时间隔没有意义


async def test_without_the_updates_section_the_settings_are_absent(start_settings: StartSettings) -> None:
    harness = await start_settings(sections=["ui", "ocr"])
    page = about(harness)
    assert page.binding("auto_check") is None
    assert update_card(harness).status_label.text() == "更新功能暂时不可用。"  # 卡片照常显示


# ---------------------------------------------------------------- 语言与键盘


async def test_update_card_in_english(harness: Harness) -> None:
    await harness.publish(
        default_config({"ui": {"language": "en"}}, revision=2),
        status(UpdateState.AVAILABLE, available=RELEASE),
    )
    card = update_card(harness)
    assert card.title_label.text() == "Current version 0.1.0"
    assert card.status_label.text().startswith("Version 0.2.0 is available, released on")
    assert card.check_button.text() == "Chec&k for updates"
    assert card.notes_button.text() == "Release &notes"
    interval = about(harness).binding("check_interval_hours")
    assert interval is not None and interval.card.title_label.text() == "Check interval"


async def test_tab_order_on_the_about_page(harness: Harness) -> None:
    await harness.publish(status(UpdateState.AVAILABLE, available=RELEASE))
    window = await harness.open()
    window.resize(1000, 760)
    window.show_page("about")
    QApplication.processEvents()
    card = update_card(harness)
    stops = tab_stops(window, about(harness))
    assert stops[:3] == [card.notes_button, card.check_button, card.download_button]
    auto = about(harness).binding("auto_check")
    assert auto is not None and stops[3] is auto.editor.focus_widget()
    assert_visual_order(window, stops, "about")
