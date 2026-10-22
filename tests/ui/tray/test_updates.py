"""托盘的“检查更新”菜单项：随 UpdateStatus 变化的文字、点击时发送的命令、手动操作的结果通知。"""

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from lst.contracts.events import NoticeLevel
from lst.contracts.messages import Message
from lst.contracts.updates import (
    CheckForUpdates,
    DownloadUpdate,
    InstallUpdate,
    ReleaseInfo,
    UpdateErrorCode,
    UpdateState,
    UpdateStatus,
)
from lst.kernel import EventBus, ModuleContext
from lst.ui import i18n
from lst.ui.tray.presenter import MenuEntry, Notice, TrayAction, TrayPresenter, update_entry
from tests.ui.tray.fakes import FakeTimer, FakeView

APP = "LSTtranslate"
RELEASE = ReleaseInfo(
    version="0.2.0",
    prerelease=False,
    published_at="2026-10-01T00:00:00Z",
    notes="",
    page_url="https://github.invalid/willlllliams/LSTtranslate/releases/tag/v0.2.0",
    installer_bytes=1000,
)
BASE = UpdateStatus(current_version="0.1.0", can_install=True)


def status(state: UpdateState, **changes: object) -> UpdateStatus:
    return replace(BASE, state=state, **changes)  # pyright: ignore[reportArgumentType]


@dataclass
class Rig:
    presenter: TrayPresenter
    view: FakeView
    bus: EventBus
    received: list[Message]

    def entry(self) -> MenuEntry:
        entries = [entry for entry in self.view.menu.entries if entry is not None]
        return next(entry for entry in entries if entry.action is TrayAction.UPDATE)

    async def publish(self, message: UpdateStatus) -> None:
        self.bus.publish(message)
        await self.bus.drain()

    async def choose(self) -> None:
        self.presenter.choose(TrayAction.UPDATE)
        await self.bus.drain()


@pytest.fixture
async def rig(bus: EventBus, received: list[Message], tmp_path: Path) -> AsyncIterator[Rig]:
    view = FakeView()

    def timer_factory(callback: Callable[[], None]) -> FakeTimer:
        return FakeTimer(callback)

    ctx = ModuleContext(bus, logging.getLogger("tests.ui.tray.updates"))
    presenter = TrayPresenter(ctx, view, app_name=APP, log_dir=tmp_path, timer_factory=timer_factory)
    presenter.start()
    await bus.drain()
    yield Rig(presenter, view, bus, received)
    presenter.close()


# ---------------------------------------------------------------- 菜单项文字


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (None, MenuEntry(TrayAction.UPDATE, "检查更新(&U)…", enabled=False)),
        (status(UpdateState.IDLE), MenuEntry(TrayAction.UPDATE, "检查更新(&U)…")),
        (status(UpdateState.UP_TO_DATE), MenuEntry(TrayAction.UPDATE, "检查更新(&U)…")),
        (status(UpdateState.CHECKING), MenuEntry(TrayAction.UPDATE, "正在检查更新…", enabled=False)),
        (
            status(UpdateState.AVAILABLE, available=RELEASE),
            MenuEntry(TrayAction.UPDATE, "安装更新 0.2.0(&U)…"),
        ),
        (
            status(UpdateState.DOWNLOADING, available=RELEASE, progress=0.456),
            MenuEntry(TrayAction.UPDATE, "正在下载更新 0.2.0（45%）", enabled=False),
        ),
        (
            status(UpdateState.DOWNLOADING, available=RELEASE),
            MenuEntry(TrayAction.UPDATE, "正在下载更新 0.2.0…", enabled=False),
        ),
        (
            status(UpdateState.READY, available=RELEASE, progress=1.0),
            MenuEntry(TrayAction.UPDATE, "安装更新 0.2.0(&U)…"),
        ),
        (
            status(UpdateState.INSTALLING, available=RELEASE),
            MenuEntry(TrayAction.UPDATE, "正在启动安装程序…", enabled=False),
        ),
        (
            status(UpdateState.ERROR, available=RELEASE, error_code=UpdateErrorCode.CHECKSUM),
            MenuEntry(TrayAction.UPDATE, "安装更新 0.2.0(&U)…"),
        ),
        (
            status(UpdateState.ERROR, error_code=UpdateErrorCode.NETWORK),
            MenuEntry(TrayAction.UPDATE, "检查更新(&U)…"),
        ),
        # 源码运行：发现新版本也只能再检查一次。
        (
            status(UpdateState.AVAILABLE, available=RELEASE, can_install=False),
            MenuEntry(TrayAction.UPDATE, "检查更新(&U)…"),
        ),
    ],
)
def test_update_entry_follows_status(message: UpdateStatus | None, expected: MenuEntry) -> None:
    assert update_entry(message) == expected


def test_update_entry_in_english() -> None:
    i18n.set_language("en")
    assert update_entry(status(UpdateState.AVAILABLE, available=RELEASE)).text == "Install &update 0.2.0…"
    assert update_entry(status(UpdateState.IDLE)).text == "Check for &updates…"


async def test_status_replay_updates_menu(rig: Rig) -> None:
    assert not rig.entry().enabled
    await rig.publish(status(UpdateState.AVAILABLE, available=RELEASE))
    assert rig.entry() == MenuEntry(TrayAction.UPDATE, "安装更新 0.2.0(&U)…")


# ---------------------------------------------------------------- 点击时发送的命令


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (status(UpdateState.IDLE), [CheckForUpdates(manual=True)]),
        (status(UpdateState.UP_TO_DATE), [CheckForUpdates(manual=True)]),
        (status(UpdateState.ERROR, error_code=UpdateErrorCode.NETWORK), [CheckForUpdates(manual=True)]),
        (status(UpdateState.AVAILABLE, available=RELEASE), [DownloadUpdate()]),
        (status(UpdateState.ERROR, available=RELEASE), [DownloadUpdate()]),
        (status(UpdateState.READY, available=RELEASE), [InstallUpdate()]),
        (status(UpdateState.CHECKING), []),
        (status(UpdateState.DOWNLOADING, available=RELEASE), []),
        (status(UpdateState.INSTALLING, available=RELEASE), []),
        (status(UpdateState.AVAILABLE, available=RELEASE, can_install=False), [CheckForUpdates(manual=True)]),
    ],
)
async def test_click_sends_command_for_state(
    rig: Rig, message: UpdateStatus, expected: list[Message]
) -> None:
    await rig.publish(message)
    await rig.choose()
    assert rig.received == expected


async def test_click_without_update_service_sends_nothing(rig: Rig) -> None:
    await rig.choose()
    assert rig.received == []


# ---------------------------------------------------------------- 手动操作的结果通知


async def test_manual_check_up_to_date_is_notified(rig: Rig) -> None:
    await rig.publish(status(UpdateState.UP_TO_DATE))
    await rig.choose()
    # 命令发出后、更新服务处理之前到达的旧状态不算结果。
    await rig.publish(status(UpdateState.UP_TO_DATE))
    assert rig.view.notices == []
    await rig.publish(status(UpdateState.CHECKING))
    await rig.publish(status(UpdateState.UP_TO_DATE))
    assert rig.view.notices == [Notice("已是最新版本", "当前版本 0.1.0 已是最新。")]
    # 之后的状态变化不再通知。
    await rig.publish(status(UpdateState.UP_TO_DATE, last_checked="x"))
    assert len(rig.view.notices) == 1


async def test_manual_check_available_is_notified(rig: Rig) -> None:
    await rig.publish(status(UpdateState.IDLE))
    await rig.choose()
    await rig.publish(status(UpdateState.CHECKING))
    await rig.publish(status(UpdateState.AVAILABLE, available=RELEASE))
    assert rig.view.notices == [
        Notice("发现新版本", "LSTtranslate 0.2.0 已发布。点托盘菜单中的“安装更新 0.2.0…”下载并安装。")
    ]


async def test_manual_check_available_in_source_mode(rig: Rig) -> None:
    await rig.publish(status(UpdateState.IDLE, can_install=False))
    await rig.choose()
    await rig.publish(status(UpdateState.CHECKING, can_install=False))
    await rig.publish(status(UpdateState.AVAILABLE, available=RELEASE, can_install=False))
    assert rig.view.notices[0].message.startswith("LSTtranslate 0.2.0 已发布。源码运行不能自动安装")


async def test_manual_check_failure_is_notified(rig: Rig) -> None:
    await rig.publish(status(UpdateState.IDLE))
    await rig.choose()
    await rig.publish(status(UpdateState.CHECKING))
    await rig.publish(status(UpdateState.ERROR, error_code=UpdateErrorCode.TIMEOUT))
    assert rig.view.notices == [Notice("检查更新失败", "连接 GitHub 超时，请稍后重试。", NoticeLevel.WARNING)]


async def test_automatic_results_are_not_notified_by_tray(rig: Rig) -> None:
    for message in (
        status(UpdateState.CHECKING),
        status(UpdateState.UP_TO_DATE),
        status(UpdateState.CHECKING),
        status(UpdateState.ERROR, error_code=UpdateErrorCode.NETWORK),
    ):
        await rig.publish(message)
    assert rig.view.notices == []


async def test_download_ready_and_install(rig: Rig) -> None:
    await rig.publish(status(UpdateState.AVAILABLE, available=RELEASE))
    await rig.choose()
    assert rig.received == [DownloadUpdate()]
    await rig.publish(status(UpdateState.DOWNLOADING, available=RELEASE, progress=0.0))
    await rig.publish(status(UpdateState.DOWNLOADING, available=RELEASE, progress=0.5))
    assert rig.entry().text == "正在下载更新 0.2.0（50%）"
    await rig.publish(status(UpdateState.READY, available=RELEASE, progress=1.0))
    assert [notice.title for notice in rig.view.notices] == ["更新已下载"]
    await rig.choose()
    assert rig.received == [DownloadUpdate(), InstallUpdate()]
    await rig.publish(status(UpdateState.INSTALLING, available=RELEASE))
    assert len(rig.view.notices) == 1


async def test_download_failure_is_notified(rig: Rig) -> None:
    await rig.publish(status(UpdateState.AVAILABLE, available=RELEASE))
    await rig.choose()
    await rig.publish(status(UpdateState.DOWNLOADING, available=RELEASE, progress=0.0))
    await rig.publish(status(UpdateState.ERROR, available=RELEASE, error_code=UpdateErrorCode.CHECKSUM))
    assert rig.view.notices == [
        Notice("下载更新失败", "下载的安装包没有通过 SHA-256 校验，已经删除，请重试。", NoticeLevel.WARNING)
    ]
    assert rig.entry().text == "安装更新 0.2.0(&U)…"


async def test_install_launch_failure_is_notified(rig: Rig) -> None:
    await rig.publish(status(UpdateState.READY, available=RELEASE))
    await rig.choose()
    await rig.publish(status(UpdateState.ERROR, available=RELEASE, error_code=UpdateErrorCode.LAUNCH_FAILED))
    assert rig.view.notices == [
        Notice("无法安装更新", "无法启动安装程序，请到发布页面手动下载安装。", NoticeLevel.WARNING)
    ]


@pytest.mark.parametrize("code", list(UpdateErrorCode))
def test_every_update_error_code_has_texts(code: UpdateErrorCode) -> None:
    for language in i18n.SUPPORTED:
        i18n.set_language(language)
        key = f"update.error.{code.value.removeprefix('update.')}"
        assert i18n.tr(key) != key
