"""UpdatesModule 的测试：检查、自动检查的节奏与持久化、通知、下载与校验、安装、日志脱敏。

网络全部由 httpx.MockTransport 扮演；时间由假时钟与可控的 sleep 驱动，测试不真正等待。
"""

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import timedelta

import httpx
import pytest

from lst.contracts.config import AppConfig
from lst.contracts.events import NoticeLevel, NotificationRequested, QuitRequested
from lst.contracts.updates import (
    CheckForUpdates,
    DownloadUpdate,
    InstallUpdate,
    UpdateErrorCode,
    UpdateState,
    UpdateStatus,
)
from lst.kernel import EventBus, Module
from lst.services.updates import INSTALLER_ARGS, UpdatesModule
from lst.services.updates.module import MAX_SLEEP_S
from tests.services.updates.conftest import MODULE_LOGGER, Env, Published, StartModule, wait_until
from tests.services.updates.helpers import (
    SECRET,
    START,
    FakeGitHub,
    installer_bytes,
    release,
    setup_name,
    sha256_line,
)

NAME = setup_name("0.2.0")


def write_record(env: Env, **items: object) -> None:
    env.record_file.parent.mkdir(parents=True, exist_ok=True)
    env.record_file.write_text(json.dumps(items), encoding="utf-8")


def read_record(env: Env) -> dict[str, object]:
    data: object = json.loads(env.record_file.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data  # pyright: ignore[reportUnknownVariableType]


async def check(bus: EventBus, *, manual: bool = True) -> None:
    bus.send(CheckForUpdates(manual=manual))
    await bus.drain()


async def run_auto_check(env: Env, published: Published, github: FakeGitHub) -> None:
    """放行一次 sleep，等调度循环做完检查（或判断不需要检查）后再次睡下。"""
    calls = len(env.sleep.calls)
    await wait_until(lambda: env.sleep.pending == 1)
    env.sleep.release()
    await wait_until(lambda: len(env.sleep.calls) > calls and env.sleep.pending == 1)


# ---------------------------------------------------------------- 启动与手动检查


async def test_module_satisfies_protocol(start_module: StartModule, published: Published) -> None:
    module: Module = await start_module()
    assert module.name == "updates"
    assert published.status == UpdateStatus(UpdateState.IDLE, "0.1.0", can_install=True)


async def test_start_twice_is_rejected(start_module: StartModule, bus: EventBus) -> None:
    from lst.kernel import ModuleContext

    module = await start_module()
    with pytest.raises(RuntimeError):
        await module.start(ModuleContext(bus, logging.getLogger("x")))


async def test_manual_check_finds_highest_release(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    github.releases = [release("v0.2.0"), release("v0.3.0", with_checksum=False), release("v0.1.0")]
    await start_module()
    await check(bus)
    assert published.states() == [UpdateState.IDLE, UpdateState.CHECKING, UpdateState.AVAILABLE]
    status = published.status
    assert status.available is not None
    assert status.available.version == "0.2.0"
    assert status.available.installer_bytes == 5000
    assert status.last_checked == START.isoformat()
    assert status.error_code is None
    # 手动检查的结果由界面提示，模块不发通知，但记下这个版本已经告诉过用户。
    assert published.notices == []
    assert read_record(env)["notified_version"] == "0.2.0"


async def test_request_headers_and_query(
    start_module: StartModule, bus: EventBus, github: FakeGitHub
) -> None:
    await start_module(current_version="0.1.0")
    await check(bus)
    [request] = github.requests
    assert request.method == "GET"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["User-Agent"] == "LSTtranslate/0.1.0"
    assert "Authorization" not in request.headers
    assert request.url.params["per_page"] == "30"


async def test_manual_check_up_to_date(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub
) -> None:
    github.releases = [release("v0.1.0", prerelease=True)]
    await start_module(current_version="0.1.0")
    await check(bus)
    assert published.status.state is UpdateState.UP_TO_DATE
    assert published.status.available is None


async def test_include_prerelease_from_config(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub
) -> None:
    github.releases = [release("v0.3.0-beta.1", prerelease=True), release("v0.2.0")]
    bus.publish(AppConfig(sections={"updates": {"include_prerelease": False}}, revision=1))
    await start_module()
    await check(bus)
    assert published.status.available is not None
    assert published.status.available.version == "0.2.0"
    bus.publish(AppConfig(sections={"updates": {"include_prerelease": True}}, revision=2))
    await check(bus)
    assert published.status.available is not None
    assert published.status.available.version == "0.3.0-beta.1"


def _rate_limited(github: FakeGitHub) -> None:
    github.releases_status = 403


def _offline(github: FakeGitHub) -> None:
    github.fail_with = httpx.ConnectError("refused")


def _slow(github: FakeGitHub) -> None:
    github.fail_with = httpx.ReadTimeout("slow")


def _not_a_list(github: FakeGitHub) -> None:
    github.releases = {"message": "x"}  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (_rate_limited, UpdateErrorCode.HTTP),
        (_offline, UpdateErrorCode.NETWORK),
        (_slow, UpdateErrorCode.TIMEOUT),
        (_not_a_list, UpdateErrorCode.BAD_RELEASE),
    ],
)
async def test_manual_check_failure_is_error(
    start_module: StartModule,
    bus: EventBus,
    published: Published,
    github: FakeGitHub,
    setup: Callable[[FakeGitHub], None],
    code: UpdateErrorCode,
) -> None:
    setup(github)
    await start_module()
    await check(bus)
    assert published.status.state is UpdateState.ERROR
    assert published.status.error_code is code
    assert published.status.last_checked is None


async def test_manual_failure_keeps_available_version(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub
) -> None:
    github.releases = [release("v0.2.0")]
    await start_module()
    await check(bus)
    github.releases_status = 500
    await check(bus)
    assert published.status.state is UpdateState.ERROR
    assert published.status.available is not None
    assert published.status.available.version == "0.2.0"


# ---------------------------------------------------------------- 自动检查的节奏与持久化


async def test_auto_check_after_startup_delay_then_by_interval(
    start_module: StartModule, published: Published, github: FakeGitHub, env: Env
) -> None:
    github.releases = [release("v0.1.0")]
    await start_module(startup_delay=30.0)
    await wait_until(lambda: env.sleep.pending == 1)
    assert env.sleep.calls == [30.0]
    assert github.requests == []
    await run_auto_check(env, published, github)
    assert len(github.requests) == 1
    assert published.status.state is UpdateState.UP_TO_DATE
    # 下一次在 24 小时后到期；调度循环每次最多睡一小时，醒来后重新计算。
    assert env.sleep.calls[-1] == MAX_SLEEP_S
    env.clock.advance(hours=23)
    await run_auto_check(env, published, github)
    assert len(github.requests) == 1
    assert env.sleep.calls[-1] == pytest.approx(3600.0)
    env.clock.advance(minutes=30)
    await run_auto_check(env, published, github)
    assert len(github.requests) == 1
    assert env.sleep.calls[-1] == pytest.approx(1800.0)
    env.clock.advance(minutes=30)
    await run_auto_check(env, published, github)
    assert len(github.requests) == 2
    record = read_record(env)
    assert record["last_checked"] == (START + timedelta(hours=24)).isoformat()


async def test_recent_record_skips_check_after_restart(
    start_module: StartModule, published: Published, github: FakeGitHub, env: Env
) -> None:
    write_record(env, last_checked=(START - timedelta(hours=2)).isoformat())
    await start_module()
    assert published.status.last_checked == (START - timedelta(hours=2)).isoformat()
    await run_auto_check(env, published, github)
    assert github.requests == []
    assert env.sleep.calls[-1] == MAX_SLEEP_S
    env.clock.advance(hours=22)
    await run_auto_check(env, published, github)
    assert len(github.requests) == 1


async def test_stale_record_checks_after_startup_delay(
    start_module: StartModule, published: Published, github: FakeGitHub, env: Env
) -> None:
    write_record(env, last_checked=(START - timedelta(hours=25)).isoformat())
    await start_module()
    await run_auto_check(env, published, github)
    assert len(github.requests) == 1


async def test_custom_interval_from_config(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    write_record(env, last_checked=(START - timedelta(hours=3)).isoformat())
    bus.publish(AppConfig(sections={"updates": {"check_interval_hours": 2}}, revision=1))
    await start_module()
    await run_auto_check(env, published, github)
    assert len(github.requests) == 1


async def test_auto_check_disabled(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    bus.publish(AppConfig(sections={"updates": {"auto_check": False}}, revision=1))
    await start_module()
    await run_auto_check(env, published, github)
    await run_auto_check(env, published, github)
    assert github.requests == []
    # 打开自动检查后立即按新配置重新计算到期时间。
    bus.publish(AppConfig(sections={"updates": {"auto_check": True}}, revision=2))
    await bus.drain()
    await wait_until(lambda: len(github.requests) == 1)


async def test_auto_check_failure_is_silent_and_retried_later(
    start_module: StartModule,
    published: Published,
    github: FakeGitHub,
    env: Env,
    caplog: pytest.LogCaptureFixture,
) -> None:
    github.fail_with = httpx.ConnectError("offline")
    caplog.set_level(logging.INFO, logger=MODULE_LOGGER)
    await start_module()
    await run_auto_check(env, published, github)
    assert len(github.requests) == 1
    assert published.states() == [UpdateState.IDLE, UpdateState.CHECKING, UpdateState.IDLE]
    assert published.status.error_code is None
    assert published.notices == []
    assert "自动检查更新失败" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    record = read_record(env)
    assert record["last_checked"] is None
    assert record["last_attempt"] == START.isoformat()
    # 一小时内不重试，之后再试。
    await run_auto_check(env, published, github)
    assert len(github.requests) == 1
    env.clock.advance(hours=1)
    github.fail_with = None
    await run_auto_check(env, published, github)
    assert len(github.requests) == 2
    assert published.status.state is UpdateState.UP_TO_DATE


async def test_corrupt_record_is_treated_as_empty(
    start_module: StartModule, published: Published, github: FakeGitHub, env: Env
) -> None:
    env.record_file.parent.mkdir(parents=True, exist_ok=True)
    env.record_file.write_text("{not json", encoding="utf-8")
    await start_module()
    await run_auto_check(env, published, github)
    assert len(github.requests) == 1
    assert read_record(env)["last_checked"] == START.isoformat()


async def test_record_in_the_future_is_ignored(
    start_module: StartModule, published: Published, github: FakeGitHub, env: Env
) -> None:
    write_record(env, last_checked=(START + timedelta(days=3)).isoformat())
    await start_module()
    await run_auto_check(env, published, github)
    assert len(github.requests) == 1


# ---------------------------------------------------------------- 发现新版本的通知


async def test_auto_check_notifies_new_version_once(
    start_module: StartModule, published: Published, github: FakeGitHub, env: Env
) -> None:
    github.releases = [release("v0.2.0")]
    await start_module()
    await run_auto_check(env, published, github)
    assert published.notices == [
        NotificationRequested(
            "update.notify.available.title",
            "update.notify.available.message",
            NoticeLevel.INFO,
            {"version": "0.2.0"},
        )
    ]
    assert read_record(env)["notified_version"] == "0.2.0"
    env.clock.advance(hours=24)
    await run_auto_check(env, published, github)
    assert len(github.requests) == 2
    assert len(published.notices) == 1
    github.releases = [release("v0.3.0"), release("v0.2.0")]
    env.clock.advance(hours=24)
    await run_auto_check(env, published, github)
    assert [notice.params for notice in published.notices] == [{"version": "0.2.0"}, {"version": "0.3.0"}]


async def test_notified_version_survives_restart(
    start_module: StartModule, published: Published, github: FakeGitHub, env: Env
) -> None:
    write_record(env, notified_version="0.2.0")
    github.releases = [release("v0.2.0")]
    await start_module()
    await run_auto_check(env, published, github)
    assert published.status.state is UpdateState.AVAILABLE
    assert published.notices == []


async def test_source_mode_notification_uses_source_text(
    start_module: StartModule, published: Published, github: FakeGitHub, env: Env
) -> None:
    github.releases = [release("v0.2.0")]
    await start_module(can_install=False)
    await run_auto_check(env, published, github)
    assert [notice.message_key for notice in published.notices] == ["update.notify.available_source.message"]


async def test_language_texts_exist_for_notification_keys() -> None:
    from lst.ui import i18n
    from lst.ui.tray.strings import UPDATE_CATALOG  # 导入时登记文案

    assert UPDATE_CATALOG

    for key in (
        "update.notify.available.title",
        "update.notify.available.message",
        "update.notify.available_source.message",
    ):
        assert i18n.tr(key, version="0.2.0") != key


# ---------------------------------------------------------------- 下载与校验


async def test_download_writes_verified_installer(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    github.releases = [release("v0.2.0", size=5000)]
    ticks = iter(range(1000))
    await start_module(clock=lambda: float(next(ticks)), progress_interval=1.0, write_chunk_bytes=1500)
    await check(bus)
    bus.send(DownloadUpdate())
    await bus.drain()
    status = published.status
    assert status.state is UpdateState.READY
    assert status.progress == 1.0
    final = env.download_dir / NAME
    assert final.read_bytes() == installer_bytes("0.2.0")
    assert list(env.download_dir.iterdir()) == [final]
    progress = [s.progress for s in published.statuses if s.state is UpdateState.DOWNLOADING]
    known = [value for value in progress if value is not None]
    assert known == progress
    assert known[0] == 0.0
    assert known == sorted(known)
    assert len(known) >= 3
    assert all(0.0 <= value <= 1.0 for value in known)
    # 校验文件先于安装包下载，两者都经过重定向。
    assert [p.rsplit("/", 1)[-1] for p in github.paths()[1:]] == [f"{NAME}.sha256"] * 2 + [NAME] * 2


async def test_progress_is_throttled(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub
) -> None:
    github.releases = [release("v0.2.0", size=5000)]
    await start_module(clock=lambda: 0.0, progress_interval=1.0)
    await check(bus)
    bus.send(DownloadUpdate())
    await bus.drain()
    # 时钟不走，除了开始时的 0 之外不发布中间进度。
    progress = [s.progress for s in published.statuses if s.state is UpdateState.DOWNLOADING]
    assert progress == [0.0]


async def test_checksum_mismatch_deletes_download(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    github.releases = [release("v0.2.0")]
    github.files[NAME + ".sha256"] = sha256_line(b"something else", NAME).encode()
    await start_module()
    await check(bus)
    bus.send(DownloadUpdate())
    await bus.drain()
    assert published.status.state is UpdateState.ERROR
    assert published.status.error_code is UpdateErrorCode.CHECKSUM
    assert published.status.available is not None
    assert list(env.download_dir.iterdir()) == []


async def test_checksum_file_name_mismatch_is_bad_release(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    github.releases = [release("v0.2.0")]
    github.files[NAME + ".sha256"] = sha256_line(installer_bytes("0.2.0"), "other.exe").encode()
    await start_module()
    await check(bus)
    bus.send(DownloadUpdate())
    await bus.drain()
    assert published.status.error_code is UpdateErrorCode.BAD_RELEASE
    assert not github.asset_requests(".exe")
    assert not (env.download_dir / NAME).exists()


async def test_checksum_with_binary_marker_is_accepted(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub
) -> None:
    github.releases = [release("v0.2.0")]
    github.files[NAME + ".sha256"] = sha256_line(installer_bytes("0.2.0"), NAME, star=True).encode()
    await start_module()
    await check(bus)
    bus.send(DownloadUpdate())
    await bus.drain()
    assert published.status.state is UpdateState.READY


async def test_existing_verified_installer_is_reused(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    github.releases = [release("v0.2.0")]
    env.download_dir.mkdir(parents=True)
    (env.download_dir / NAME).write_bytes(installer_bytes("0.2.0"))
    await start_module()
    await check(bus)
    bus.send(DownloadUpdate())
    await bus.drain()
    assert published.status.state is UpdateState.READY
    assert not github.asset_requests(".exe")


async def test_source_mode_does_not_download(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    github.releases = [release("v0.2.0")]
    await start_module(can_install=False)
    await check(bus)
    assert published.status.can_install is False
    bus.send(DownloadUpdate())
    await bus.drain()
    assert published.status.state is UpdateState.ERROR
    assert published.status.error_code is UpdateErrorCode.NOT_INSTALLED
    bus.send(InstallUpdate())
    await bus.drain()
    assert published.status.error_code is UpdateErrorCode.NOT_INSTALLED
    assert len(github.requests) == 1
    assert env.launcher.calls == []
    assert not env.download_dir.exists()


async def test_download_without_available_version_is_ignored(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub
) -> None:
    await start_module()
    bus.send(DownloadUpdate())
    await bus.drain()
    assert published.states() == [UpdateState.IDLE]
    assert github.requests == []


async def test_startup_removes_partial_and_installed_installers(start_module: StartModule, env: Env) -> None:
    env.download_dir.mkdir(parents=True)
    for name in (f"{NAME}.part", setup_name("0.1.0"), setup_name("0.0.9"), setup_name("0.2.0"), "notes.txt"):
        (env.download_dir / name).write_bytes(b"x")
    await start_module(current_version="0.1.0")
    assert sorted(path.name for path in env.download_dir.iterdir()) == [NAME, "notes.txt"]


# ---------------------------------------------------------------- 安装


async def ready_module(start_module: StartModule, bus: EventBus, github: FakeGitHub) -> UpdatesModule:
    github.releases = [release("v0.2.0")]
    module = await start_module()
    await check(bus)
    bus.send(DownloadUpdate())
    await bus.drain()
    return module


async def test_install_launches_installer_and_quits(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    await ready_module(start_module, bus, github)
    bus.send(InstallUpdate())
    await bus.drain()
    assert env.launcher.calls == [(env.download_dir / NAME, INSTALLER_ARGS)]
    assert INSTALLER_ARGS == ("/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=yes")
    assert published.status.state is UpdateState.INSTALLING
    assert published.quits == [QuitRequested()]


async def test_install_launch_failure(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    env.launcher.error = OSError("access denied")
    await ready_module(start_module, bus, github)
    bus.send(InstallUpdate())
    await bus.drain()
    assert published.status.state is UpdateState.ERROR
    assert published.status.error_code is UpdateErrorCode.LAUNCH_FAILED
    assert published.quits == []
    # 再次下载时直接复用已校验的安装包，然后可以重试安装。
    env.launcher.error = None
    bus.send(DownloadUpdate())
    await bus.drain()
    assert published.status.state is UpdateState.READY
    assert len(github.asset_requests(".exe")) == 2  # 只有第一次下载的重定向与正文
    bus.send(InstallUpdate())
    await bus.drain()
    assert published.quits == [QuitRequested()]


async def test_install_before_ready_is_ignored(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    github.releases = [release("v0.2.0")]
    await start_module()
    await check(bus)
    bus.send(InstallUpdate())
    await bus.drain()
    assert published.status.state is UpdateState.AVAILABLE
    assert env.launcher.calls == []
    assert published.quits == []


async def test_check_is_ignored_while_ready(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub
) -> None:
    await ready_module(start_module, bus, github)
    requests = len(github.requests)
    await check(bus)
    assert published.status.state is UpdateState.READY
    assert len(github.requests) == requests


# ---------------------------------------------------------------- 日志脱敏


async def test_logs_contain_no_query_strings_or_tokens(
    start_module: StartModule,
    bus: EventBus,
    github: FakeGitHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    github.releases = [release("v0.2.0")]
    github.files[NAME + ".sha256"] = sha256_line(b"bad", NAME).encode()
    await start_module()
    await check(bus)
    bus.send(DownloadUpdate())
    await bus.drain()
    github.fail_with = httpx.ConnectError(f"cannot reach https://objects.invalid/x?{SECRET}")
    await check(bus)
    # httpx 自己的日志器在 lst.app.logging_setup 里被调到 WARNING，不会写出请求地址；这里只看本模块的日志。
    text = "\n".join(
        record.getMessage() + (record.exc_text or "")
        for record in caplog.records
        if record.name == MODULE_LOGGER
    )
    assert "topsecret" not in text
    assert "X-Amz-Signature" not in text
    assert "per_page" not in text
    assert "Authorization" not in text
    assert "下载更新失败" in text
    assert "检查更新失败" in text


def test_checksum_digest_helper_matches_hashlib() -> None:
    data = installer_bytes("0.2.0")
    assert sha256_line(data, NAME).split()[0] == hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 并发命令


async def test_concurrent_download_commands_download_once(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub
) -> None:
    github.releases = [release("v0.2.0", size=5000)]
    github.chunk_size = 50
    await start_module(write_chunk_bytes=100)
    await check(bus)

    # 不等分发完就连发两条：第二条到达时第一条还停在第一个 await 上。
    bus.send(DownloadUpdate())
    bus.send(DownloadUpdate())
    await bus.drain()

    assert published.status.state is UpdateState.READY
    assert len(github.asset_requests("-setup.exe")) == 2  # 一次下载：重定向与正文各一个请求


async def test_concurrent_install_commands_launch_once(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    await ready_module(start_module, bus, github)

    bus.send(InstallUpdate())
    bus.send(InstallUpdate())
    await bus.drain()

    assert len(env.launcher.calls) == 1
    assert published.quits == [QuitRequested()]


async def test_stop_cancels_running_download(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    github.releases = [release("v0.2.0", size=5000)]
    module = await start_module()
    await check(bus)
    gate = github.hold_installer()
    bus.send(DownloadUpdate())
    await wait_until(lambda: gate.reached.is_set())

    await module.stop()

    assert not any(path.name.endswith(".part") for path in env.download_dir.iterdir())
    await bus.drain()


async def test_ignored_check_republishes_status(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub
) -> None:
    await ready_module(start_module, bus, github)
    count = len(published.statuses)

    await check(bus)

    # 界面点了“检查更新”之后在等新的状态；忽略检查时也要重新发布一次当前状态，界面不会卡在“正在检查”。
    assert len(published.statuses) == count + 1
    assert published.status.state is UpdateState.READY


async def test_check_removes_installers_of_superseded_versions(
    start_module: StartModule, bus: EventBus, github: FakeGitHub, env: Env
) -> None:
    env.download_dir.mkdir(parents=True)
    for name in (setup_name("0.2.0"), setup_name("0.3.0"), "notes.txt"):
        (env.download_dir / name).write_bytes(b"x")
    github.releases = [release("v0.3.0"), release("v0.2.0")]
    await start_module(current_version="0.1.0")
    assert sorted(path.name for path in env.download_dir.iterdir()) == [
        setup_name("0.2.0"),
        setup_name("0.3.0"),
        "notes.txt",
    ]

    await check(bus)

    # 0.2.0 已经被 0.3.0 取代，只保留当前可用版本的安装包。
    assert sorted(path.name for path in env.download_dir.iterdir()) == [setup_name("0.3.0"), "notes.txt"]
