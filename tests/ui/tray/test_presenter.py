"""托盘展示器的测试：菜单模型、状态映射、通知与聚合、菜单命令。视图和计时器用假实现，总线是真实的。"""

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from lst.contracts.config import AppConfig, ConfigRejected
from lst.contracts.errors import ErrorCode
from lst.contracts.events import (
    EngineHealth,
    EngineStatus,
    EngineStatuses,
    HotkeyRegistrationFailed,
    NoticeLevel,
    NotificationRequested,
    OpenSettings,
    QuitRequested,
    ShowModelDownloads,
    StartCapture,
    ToggleBilingualWindow,
    WorkerStatus,
    WorkerStatuses,
)
from lst.contracts.ids import EngineId
from lst.contracts.messages import Message
from lst.contracts.ports import WorkerState
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus, ModuleContext
from lst.ui import i18n
from lst.ui.tray.presenter import (
    CONFIG_REJECT_WINDOW_MS,
    MENU_LAYOUT,
    WORKER_HEALTH,
    Hotkeys,
    MenuEntry,
    Notice,
    StatusLine,
    TrayAction,
    TrayPresenter,
    build_menu,
    overall_status,
    read_hotkeys,
)
from tests.ui.tray.fakes import FakeTimer, FakeView

APP = "LSTtranslate"


@dataclass
class Rig:
    presenter: TrayPresenter
    view: FakeView
    timers: list[FakeTimer]
    bus: EventBus
    received: list[Message]
    log_dir: Path

    @property
    def timer(self) -> FakeTimer:
        """ConfigRejected 聚合用的计时器。展示器只创建这一个。"""
        assert len(self.timers) == 1
        return self.timers[0]

    def status_texts(self) -> list[str]:
        return [line.text for line in self.view.menu.status]


@pytest.fixture
async def rig(bus: EventBus, received: list[Message], tmp_path: Path) -> AsyncIterator[Rig]:
    view = FakeView()
    timers: list[FakeTimer] = []

    def timer_factory(callback: Callable[[], None]) -> FakeTimer:
        timer = FakeTimer(callback)
        timers.append(timer)
        return timer

    ctx = ModuleContext(bus, logging.getLogger("tests.ui.tray.presenter"))
    presenter = TrayPresenter(ctx, view, app_name=APP, log_dir=tmp_path, timer_factory=timer_factory)
    presenter.start()
    await bus.drain()
    yield Rig(presenter, view, timers, bus, received, tmp_path)
    presenter.close()


def engines(*items: tuple[str, str, EngineStatus]) -> EngineStatuses:
    """按 (实例号, 名称, 状态) 构造 EngineStatuses，保持给定顺序。名称为空串时不登记名称。"""
    return EngineStatuses(
        statuses={EngineId(engine_id): status for engine_id, _, status in items},
        names={EngineId(engine_id): name for engine_id, name, _ in items if name},
    )


def workers(**states: WorkerState) -> WorkerStatuses:
    return WorkerStatuses({name: WorkerStatus(state) for name, state in states.items()})


# ---------------------------------------------------------------- 菜单模型


def test_menu_layout_and_default_texts() -> None:
    menu = build_menu(APP, {}, None, Hotkeys())
    assert MENU_LAYOUT == (
        TrayAction.CAPTURE,
        TrayAction.BILINGUAL,
        TrayAction.SETTINGS,
        TrayAction.MODELS,
        TrayAction.OPEN_LOGS,
        TrayAction.EXPORT_DIAGNOSTICS,
        None,
        TrayAction.QUIT,
    )
    assert menu.entries == (
        MenuEntry(TrayAction.CAPTURE, "截屏翻译(&C)", "Alt+Q"),
        MenuEntry(TrayAction.BILINGUAL, "显示/隐藏双语窗口(&B)", "Alt+E"),
        MenuEntry(TrayAction.SETTINGS, "设置(&S)"),
        MenuEntry(TrayAction.MODELS, "下载翻译模型(&M)…"),
        MenuEntry(TrayAction.OPEN_LOGS, "打开日志目录(&L)"),
        MenuEntry(TrayAction.EXPORT_DIAGNOSTICS, "导出诊断信息(&D)…", enabled=False),
        None,
        MenuEntry(TrayAction.QUIT, "退出(&X)"),
    )
    # 还没有任何状态时，状态区显示一行占位文字，悬停提示写“没有启用的组件”。
    assert menu.status == (StatusLine("none", "暂无组件状态", EngineHealth.DISABLED),)
    assert menu.tooltip == "LSTtranslate\n没有启用的组件"


def test_menu_texts_in_english() -> None:
    i18n.set_language("en")
    menu = build_menu(APP, {"ocr": WorkerStatus(WorkerState.READY)}, None, Hotkeys("Ctrl+1", "Ctrl+2"))
    assert [entry.text for entry in menu.entries if entry is not None] == [
        "&Capture and translate",
        "Show/hide &bilingual window",
        "&Settings",
        "Download translation &models…",
        "Open &log folder",
        "Export &diagnostics…",
        "E&xit",
    ]
    assert [entry.shortcut for entry in menu.entries if entry is not None][:2] == ["Ctrl+1", "Ctrl+2"]
    assert menu.status[0].text == "OCR: Ready"
    assert menu.tooltip == "LSTtranslate\nAll ready"


@pytest.mark.parametrize(
    ("sections", "expected"),
    [
        ({}, Hotkeys("Alt+Q", "Alt+E")),
        ({"hotkeys": "Alt+W"}, Hotkeys("Alt+Q", "Alt+E")),
        ({"hotkeys": {"capture": 5, "bilingual": None}}, Hotkeys("Alt+Q", "Alt+E")),
        ({"hotkeys": {"capture": "  ", "bilingual": ["Alt+B"]}}, Hotkeys("Alt+Q", "Alt+E")),
        ({"hotkeys": {"capture": "Ctrl+Alt+T"}}, Hotkeys("Ctrl+Alt+T", "Alt+E")),
        ({"hotkeys": {"capture": "Ctrl+1", "bilingual": "Ctrl+2", "other": 1}}, Hotkeys("Ctrl+1", "Ctrl+2")),
    ],
)
def test_read_hotkeys_falls_back_to_defaults(sections: dict[str, JsonValue], expected: Hotkeys) -> None:
    assert read_hotkeys(AppConfig(sections=sections, revision=1)) == expected


@pytest.mark.parametrize(
    ("healths", "expected"),
    [
        ([], "没有启用的组件"),
        ([EngineHealth.DISABLED], "没有启用的组件"),
        ([EngineHealth.READY, EngineHealth.BUSY, EngineHealth.DISABLED], "全部就绪"),
        ([EngineHealth.READY, EngineHealth.LOADING], "正在加载"),
        ([EngineHealth.ERROR, EngineHealth.LOADING, EngineHealth.ERROR, EngineHealth.READY], "有 2 项出错"),
    ],
)
def test_overall_status(healths: list[EngineHealth], expected: str) -> None:
    assert overall_status(healths) == expected


# ---------------------------------------------------------------- 状态区


def test_worker_health_mapping_covers_every_state() -> None:
    assert set(WORKER_HEALTH) == set(WorkerState)


@pytest.mark.parametrize(
    ("state", "shown", "reason"),
    [
        (WorkerState.STOPPED, "未启用", None),
        (WorkerState.STARTING, "加载中", None),
        (WorkerState.LOADING, "加载中", None),
        (WorkerState.READY, "就绪", None),
        (WorkerState.BUSY, "忙碌", None),
        (WorkerState.CRASHED, "出错", "原因：推理进程崩溃，正在自动重启"),
        (WorkerState.FAILED, "出错", "原因：推理进程多次崩溃或无法启动，已停止自动重启"),
    ],
)
async def test_worker_state_is_shown_per_architecture_table(
    rig: Rig, state: WorkerState, shown: str, reason: str | None
) -> None:
    rig.bus.publish(WorkerStatuses({"ocr": WorkerStatus(state, detail="C:\\secret\\path 技术细节")}))
    await rig.bus.drain()
    (line,) = rig.view.menu.status
    assert line.key == "worker:ocr"
    assert line.text == f"OCR：{shown}"
    assert line.health is WORKER_HEALTH[state]
    if reason is None:
        assert line.details == ()
    else:
        assert line.details[0] == reason
        assert line.details[1].startswith("处理建议：")
    # 监管器的 detail 是技术细节，不出现在菜单里（架构文档 10.3 节）。
    assert all("技术细节" not in text for text in (line.text, *line.details))


async def test_workers_are_listed_ocr_first_then_others(rig: Rig) -> None:
    rig.bus.publish(workers(extra=WorkerState.READY, nmt=WorkerState.BUSY, ocr=WorkerState.LOADING))
    await rig.bus.drain()
    assert [line.key for line in rig.view.menu.status] == ["worker:ocr", "worker:nmt", "worker:extra"]
    assert rig.status_texts() == ["OCR：加载中", "本地翻译模型：忙碌", "extra：就绪"]


async def test_engine_lines_follow_statuses_with_error_reason(rig: Rig) -> None:
    rig.bus.publish(workers(ocr=WorkerState.READY))
    rig.bus.publish(
        engines(
            ("deepseek", "DeepSeek Flash", EngineStatus(EngineHealth.ERROR, "HTTP 401", ErrorCode.AUTH)),
            ("argos", "Argos", EngineStatus(EngineHealth.READY)),
            ("google", "", EngineStatus(EngineHealth.DISABLED)),
            ("broken", "Broken", EngineStatus(EngineHealth.ERROR)),
        )
    )
    await rig.bus.drain()
    menu = rig.view.menu
    assert rig.status_texts() == [
        "OCR：就绪",
        "DeepSeek Flash：出错",
        "Argos：就绪",
        "google：未启用",  # 没有名称时用实例号
        "Broken：出错",
    ]
    assert menu.status[1].details == (
        "原因：API key 无效或没有权限",
        "处理建议：在设置 → 翻译引擎中检查 API key",
    )
    assert menu.status[2].details == ()
    # 没有错误码时按 engine.internal 显示。
    assert menu.status[4].details == ("原因：内部错误", "处理建议：导出诊断信息并检查日志")
    # 技术细节 HTTP 401 只写日志，不进菜单。
    assert all("HTTP 401" not in text for line in menu.status for text in (line.text, *line.details))
    assert menu.tooltip == "LSTtranslate\n有 2 项出错"


async def test_state_published_before_start_is_replayed(bus: EventBus, tmp_path: Path) -> None:
    bus.publish(workers(ocr=WorkerState.READY, nmt=WorkerState.STOPPED))
    bus.publish(AppConfig(sections={"hotkeys": {"capture": "Ctrl+Q"}}, revision=3))
    view = FakeView()
    ctx = ModuleContext(bus, logging.getLogger("tests.ui.tray.presenter"))
    presenter = TrayPresenter(ctx, view, app_name=APP, log_dir=tmp_path, timer_factory=FakeTimer)
    presenter.start()
    # start 先按默认值渲染一次，菜单立即可用。
    assert view.menu.status[0].key == "none"
    await bus.drain()
    assert [line.text for line in view.menu.status] == ["OCR：就绪", "本地翻译模型：未启用"]
    assert view.menu.entries[0] == MenuEntry(TrayAction.CAPTURE, "截屏翻译(&C)", "Ctrl+Q")
    presenter.close()


async def test_unchanged_state_does_not_rerender(rig: Rig) -> None:
    rig.bus.publish(workers(ocr=WorkerState.READY))
    await rig.bus.drain()
    renders = len(rig.view.menus)
    rig.bus.publish(workers(ocr=WorkerState.READY))
    rig.bus.publish(AppConfig(sections={}, revision=2))
    await rig.bus.drain()
    assert len(rig.view.menus) == renders
    rig.bus.publish(workers(ocr=WorkerState.BUSY))
    await rig.bus.drain()
    assert len(rig.view.menus) == renders + 1


async def test_hotkeys_follow_app_config(rig: Rig) -> None:
    def shortcuts() -> list[str]:
        return [entry.shortcut for entry in rig.view.menu.entries if entry is not None][:2]

    assert shortcuts() == ["Alt+Q", "Alt+E"]
    rig.bus.publish(
        AppConfig(sections={"hotkeys": {"capture": "Ctrl+Alt+T", "bilingual": "Alt+B"}}, revision=1)
    )
    await rig.bus.drain()
    assert shortcuts() == ["Ctrl+Alt+T", "Alt+B"]
    rig.bus.publish(AppConfig(sections={"hotkeys": {"capture": 1}}, revision=2))
    await rig.bus.drain()
    assert shortcuts() == ["Alt+Q", "Alt+E"]


async def test_menu_opening_rerenders_in_current_language(rig: Rig) -> None:
    assert rig.view.menu.entries[0] == MenuEntry(TrayAction.CAPTURE, "截屏翻译(&C)", "Alt+Q")
    i18n.set_language("en")
    rig.presenter.menu_opening()
    assert rig.view.menu.entries[0] == MenuEntry(TrayAction.CAPTURE, "&Capture and translate", "Alt+Q")
    assert rig.view.menu.status[0].text == "No component status yet"


# ---------------------------------------------------------------- 菜单命令


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (TrayAction.CAPTURE, StartCapture()),
        (TrayAction.BILINGUAL, ToggleBilingualWindow()),
        (TrayAction.SETTINGS, OpenSettings()),
        (TrayAction.MODELS, ShowModelDownloads()),
        (TrayAction.QUIT, QuitRequested()),
    ],
)
async def test_menu_actions_send_commands(rig: Rig, action: TrayAction, expected: Message) -> None:
    rig.presenter.choose(action)
    await rig.bus.drain()
    assert rig.received == [expected]


async def test_click_icon_toggles_bilingual_window(rig: Rig) -> None:
    rig.presenter.click_icon()
    await rig.bus.drain()
    assert rig.received == [ToggleBilingualWindow()]


async def test_export_diagnostics_sends_nothing(rig: Rig) -> None:
    rig.presenter.choose(TrayAction.EXPORT_DIAGNOSTICS)
    await rig.bus.drain()
    assert rig.received == []
    assert rig.view.notices == []


async def test_open_logs_opens_log_dir(rig: Rig) -> None:
    rig.presenter.choose(TrayAction.OPEN_LOGS)
    assert rig.view.opened == [rig.log_dir]
    assert rig.view.notices == []


async def test_open_logs_failure_is_reported(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.view.open_result = False
    rig.presenter.choose(TrayAction.OPEN_LOGS)
    (notice,) = rig.view.notices
    assert notice.title == "无法打开日志目录"
    assert notice.message.startswith(f"{rig.log_dir}\n")
    assert notice.level is NoticeLevel.WARNING
    assert "打开日志目录失败" in caplog.text


# ---------------------------------------------------------------- 通知


@pytest.mark.parametrize("level", list(NoticeLevel))
async def test_notification_fills_texts_and_keeps_level(rig: Rig, level: NoticeLevel) -> None:
    i18n.register_catalog(
        {
            "test.tray.title": {"zh-Hans": "{component} 已重启", "en": "{component} restarted"},
            "test.tray.body": {
                "zh-Hans": "{component} 崩溃了 {count} 次",
                "en": "{component} crashed {count} times",
            },
        }
    )
    rig.bus.publish(
        NotificationRequested("test.tray.title", "test.tray.body", level, {"component": "OCR", "count": "2"})
    )
    await rig.bus.drain()
    assert rig.view.notices == [Notice("OCR 已重启", "OCR 崩溃了 2 次", level)]


async def test_notification_without_params_uses_plain_texts(rig: Rig) -> None:
    rig.bus.publish(NotificationRequested("health.ready", "health.busy"))
    await rig.bus.drain()
    assert rig.view.notices == [Notice("就绪", "忙碌", NoticeLevel.INFO)]


async def test_hotkey_failure_notice(rig: Rig) -> None:
    rig.bus.publish(HotkeyRegistrationFailed("Alt+Q", "已被其他程序占用"))
    await rig.bus.drain()
    assert rig.view.notices == [
        Notice(
            "热键注册失败",
            "热键 Alt+Q 注册失败：已被其他程序占用。请在设置 → 热键中换一个组合。",
            NoticeLevel.WARNING,
        )
    ]


async def test_config_rejections_within_window_are_merged(rig: Rig) -> None:
    locations: list[tuple[str | int, ...]] = [
        ("race", "mode"),
        ("engines", 2, "priority"),
        ("race", "mode"),  # 同一位置只算一处
        ("ocr", "device"),
        ("bilingual", "debounce_ms"),
    ]
    for location in locations:
        rig.bus.publish(ConfigRejected(location, "Input should be 'saver' or 'full'"))
    await rig.bus.drain()
    # 窗口从第一条开始计时，后续消息不重新计时；到时之前不弹通知。
    assert rig.timer.starts == [CONFIG_REJECT_WINDOW_MS]
    assert rig.view.notices == []
    rig.timer.fire()
    (notice,) = rig.view.notices
    assert notice.title == "配置有误"
    assert notice.level is NoticeLevel.WARNING
    assert notice.message.splitlines() == [
        "有 4 处配置无效，已使用默认值。请在设置中检查：",
        "race › mode",
        "engines › 第 3 项 › priority",
        "ocr › device",
        "另有 1 处未列出",
    ]
    # 正文只列位置，不带原因。
    assert "saver" not in notice.message


async def test_config_rejection_after_flush_opens_new_window(rig: Rig) -> None:
    rig.bus.publish(ConfigRejected(("race", "mode"), "bad"))
    await rig.bus.drain()
    rig.timer.fire()
    rig.bus.publish(ConfigRejected((), "TOML 语法错误"))
    await rig.bus.drain()
    assert rig.timer.starts == [CONFIG_REJECT_WINDOW_MS, CONFIG_REJECT_WINDOW_MS]
    rig.timer.fire()
    assert [notice.message.splitlines() for notice in rig.view.notices] == [
        ["有 1 处配置无效，已使用默认值。请在设置中检查：", "race › mode"],
        ["有 1 处配置无效，已使用默认值。请在设置中检查：", "整个配置文件"],
    ]


async def test_config_rejection_notice_in_english(rig: Rig) -> None:
    i18n.set_language("en")
    rig.bus.publish(ConfigRejected(("engines", 0, "timeout_s"), "bad"))
    rig.bus.publish(ConfigRejected(("ui", "theme"), "bad"))
    await rig.bus.drain()
    rig.timer.fire()
    (notice,) = rig.view.notices
    assert notice.title == "Invalid settings"
    assert notice.message.splitlines() == [
        "2 settings were invalid and have been reset to defaults. Check them in Settings:",
        "engines › item 1 › timeout_s",
        "ui › theme",
    ]


# ---------------------------------------------------------------- 关闭


async def test_closed_presenter_ignores_everything(rig: Rig) -> None:
    rig.bus.publish(ConfigRejected(("race", "mode"), "bad"))
    await rig.bus.drain()
    assert rig.timer.active
    renders = len(rig.view.menus)
    rig.presenter.close()
    rig.presenter.close()  # 重复调用没有效果
    assert rig.presenter.closed
    assert not rig.timer.active
    rig.bus.publish(workers(ocr=WorkerState.READY))
    rig.bus.publish(NotificationRequested("health.ready", "health.ready"))
    rig.bus.publish(HotkeyRegistrationFailed("Alt+Q", "x"))
    rig.bus.publish(ConfigRejected(("ui", "theme"), "bad"))
    await rig.bus.drain()
    for action in TrayAction:
        rig.presenter.choose(action)
    rig.presenter.click_icon()
    rig.presenter.menu_opening()
    await rig.bus.drain()
    assert len(rig.view.menus) == renders
    assert rig.view.notices == []
    assert rig.view.opened == []
    assert rig.received == []
    assert rig.timer.starts == [CONFIG_REJECT_WINDOW_MS]
