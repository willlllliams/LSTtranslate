"""TrayModule 的测试：真实的 QSystemTrayIcon 与 QMenu（offscreen 平台）、真实的事件总线。

showMessage 与 openUrl 由 conftest 换成记录调用的假函数。
"""

import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
from pytestqt.qtbot import QtBot
from shiboken6 import Shiboken

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
from lst.kernel import EventBus, Module, ModuleContext
from lst.ui import i18n
from lst.ui.tray import TrayAction, TrayModule
from lst.ui.tray.view import NOTICE_DURATION_MS, QtTrayView
from tests.ui.tray.fakes import DesktopRecorder, ShownMessage

APP = "LSTtranslate"
ROOT = Path(__file__).resolve().parents[3]
ICON = ROOT / "assets" / "icons" / "lsttranslate.ico"

EXPECTED_COMMANDS_ZH = [
    "---",
    "截屏翻译(&C)\tAlt+Q",
    "显示/隐藏双语窗口(&B)\tAlt+E",
    "设置(&S)",
    "下载翻译模型(&M)…",
    "打开日志目录(&L)",
    "导出诊断信息(&D)…",
    "---",
    "退出(&X)",
]


@pytest.fixture
async def modules(qapp: QApplication) -> AsyncIterator[list[TrayModule]]:
    """记录测试里启动的模块，结束时全部停止。只在 offscreen 平台运行，避免在桌面上弹出托盘图标。"""
    if QApplication.platformName() != "offscreen":
        pytest.skip("托盘测试只在 offscreen 平台运行")
    started: list[TrayModule] = []
    yield started
    for module in started:
        await module.stop()


async def start_module(
    bus: EventBus,
    modules: list[TrayModule],
    log_dir: Path,
    *,
    icon_path: Path | None = ICON,
    tray_available: bool | None = False,
    reject_window_ms: int = 1000,
) -> TrayModule:
    module = TrayModule(
        APP, icon_path, log_dir, tray_available=tray_available, reject_window_ms=reject_window_ms
    )
    modules.append(module)
    await module.start(ModuleContext(bus, logging.getLogger("tests.ui.tray.module")))
    await bus.drain()
    return module


def view_of(module: TrayModule) -> QtTrayView:
    view = module.view
    assert view is not None
    return view


def menu_texts(view: QtTrayView) -> list[str]:
    return ["---" if action.isSeparator() else action.text() for action in view.menu.actions()]


def submenu_of(action: QAction) -> QMenu | None:
    menu = action.menu()
    return menu if isinstance(menu, QMenu) else None


# ---------------------------------------------------------------- 构造与生命周期


async def test_module_satisfies_protocol(bus: EventBus, modules: list[TrayModule], tmp_path: Path) -> None:
    module: Module = await start_module(bus, modules, tmp_path)
    assert module.name == "ui.tray"


async def test_menu_entries_and_order(bus: EventBus, modules: list[TrayModule], tmp_path: Path) -> None:
    view = view_of(await start_module(bus, modules, tmp_path))
    assert menu_texts(view) == ["暂无组件状态", *EXPECTED_COMMANDS_ZH]
    assert not view.command_action(TrayAction.EXPORT_DIAGNOSTICS).isEnabled()
    enabled = [kind for kind in TrayAction if view.command_action(kind).isEnabled()]
    assert enabled == [
        TrayAction.CAPTURE,
        TrayAction.BILINGUAL,
        TrayAction.SETTINGS,
        TrayAction.MODELS,
        TrayAction.OPEN_LOGS,
        TrayAction.QUIT,
    ]
    assert view.tray.contextMenu() is view.menu
    assert view.tray.toolTip() == "LSTtranslate\n没有启用的组件"


async def test_unavailable_tray_still_builds_menu(
    bus: EventBus,
    modules: list[TrayModule],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    shown_messages: list[ShownMessage],
) -> None:
    # tray_available=None 时询问 Qt；offscreen 平台没有系统托盘。
    assert not QSystemTrayIcon.isSystemTrayAvailable()
    module = await start_module(bus, modules, tmp_path, tray_available=None)
    view = view_of(module)
    assert not view.tray_available
    assert "系统托盘不可用" in caplog.text
    assert menu_texts(view)[-1] == "退出(&X)"
    bus.publish(HotkeyRegistrationFailed("Alt+Q", "已被其他程序占用"))
    await bus.drain()
    # 通知照常交给 Qt（Qt 在没有托盘时不显示），另记一条警告，内容不会悄悄丢失。
    assert len(shown_messages) == 1
    assert "通知没有显示：热键注册失败" in caplog.text


async def test_available_tray_is_shown_and_hidden_on_stop(
    bus: EventBus, modules: list[TrayModule], tmp_path: Path
) -> None:
    module = await start_module(bus, modules, tmp_path, tray_available=True)
    tray = view_of(module).tray
    assert tray.isVisible()
    await module.stop()
    assert module.view is None
    assert module.presenter is None
    assert not tray.isVisible()
    # 托盘对象与菜单在事件循环处理延迟删除时销毁。
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not Shiboken.isValid(tray)
    await module.stop()  # 重复调用没有效果


async def test_start_twice_is_rejected(bus: EventBus, modules: list[TrayModule], tmp_path: Path) -> None:
    module = await start_module(bus, modules, tmp_path)
    with pytest.raises(RuntimeError):
        await module.start(ModuleContext(bus, logging.getLogger("tests.ui.tray.module")))


async def test_icon_from_file_and_fallback(
    bus: EventBus, modules: list[TrayModule], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    if ICON.is_file():
        icon = view_of(await start_module(bus, modules, tmp_path)).tray.icon()
        assert not icon.isNull()
    missing = tmp_path / "missing.ico"
    fallback = view_of(await start_module(bus, modules, tmp_path, icon_path=missing)).tray.icon()
    assert not fallback.isNull()
    assert {size.width() for size in fallback.availableSizes()} >= {16, 32, 256}
    assert "托盘图标文件不存在" in caplog.text
    broken = tmp_path / "broken.ico"
    broken.write_bytes(b"not an icon")
    assert not view_of(await start_module(bus, modules, tmp_path, icon_path=broken)).tray.icon().isNull()
    assert "无法读取" in caplog.text


# ---------------------------------------------------------------- 菜单命令与托盘图标


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
async def test_menu_action_sends_command(
    bus: EventBus,
    modules: list[TrayModule],
    tmp_path: Path,
    received: list[Message],
    action: TrayAction,
    expected: Message,
) -> None:
    view = view_of(await start_module(bus, modules, tmp_path))
    view.command_action(action).trigger()
    await bus.drain()
    assert received == [expected]


async def test_open_log_dir_uses_desktop_services(
    bus: EventBus,
    modules: list[TrayModule],
    tmp_path: Path,
    desktop: DesktopRecorder,
    shown_messages: list[ShownMessage],
) -> None:
    view = view_of(await start_module(bus, modules, tmp_path))
    view.command_action(TrayAction.OPEN_LOGS).trigger()
    assert desktop.urls == [QUrl.fromLocalFile(str(tmp_path))]
    assert shown_messages == []
    desktop.result = False
    view.command_action(TrayAction.OPEN_LOGS).trigger()
    assert [message.title for message in shown_messages] == ["无法打开日志目录"]


async def test_missing_log_dir_is_reported_without_opening(
    bus: EventBus,
    modules: list[TrayModule],
    tmp_path: Path,
    desktop: DesktopRecorder,
    shown_messages: list[ShownMessage],
) -> None:
    missing = tmp_path / "logs"
    view = view_of(await start_module(bus, modules, missing))
    view.command_action(TrayAction.OPEN_LOGS).trigger()
    assert desktop.urls == []
    (message,) = shown_messages
    assert message.icon is QSystemTrayIcon.MessageIcon.Warning
    assert message.message.startswith(str(missing))


async def test_left_click_toggles_bilingual_window(
    bus: EventBus, modules: list[TrayModule], tmp_path: Path, received: list[Message]
) -> None:
    tray = view_of(await start_module(bus, modules, tmp_path)).tray
    tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
    await bus.drain()
    assert received == [ToggleBilingualWindow()]
    # 右键、双击、中键不切换：右键由 Qt 弹出菜单，双击之前 Qt 已经发过一次 Trigger。
    for reason in (
        QSystemTrayIcon.ActivationReason.Context,
        QSystemTrayIcon.ActivationReason.DoubleClick,
        QSystemTrayIcon.ActivationReason.MiddleClick,
    ):
        tray.activated.emit(reason)
    await bus.drain()
    assert received == [ToggleBilingualWindow()]


async def test_hotkeys_shown_follow_app_config(
    bus: EventBus, modules: list[TrayModule], tmp_path: Path
) -> None:
    view = view_of(await start_module(bus, modules, tmp_path))
    capture = view.command_action(TrayAction.CAPTURE)
    bilingual = view.command_action(TrayAction.BILINGUAL)
    bus.publish(AppConfig(sections={"hotkeys": {"capture": "Ctrl+Alt+T", "bilingual": "Ctrl+&"}}, revision=1))
    await bus.drain()
    # 条目在原地更新，热键文本里的 & 被转义，不会被当成助记符。
    assert view.command_action(TrayAction.CAPTURE) is capture
    assert capture.text() == "截屏翻译(&C)\tCtrl+Alt+T"
    assert bilingual.text() == "显示/隐藏双语窗口(&B)\tCtrl+&&"
    bus.publish(AppConfig(sections={"ui": {"language": "en"}}, revision=2))
    await bus.drain()
    assert capture.text() == "截屏翻译(&C)\tAlt+Q"
    assert bilingual.text() == "显示/隐藏双语窗口(&B)\tAlt+E"


# ---------------------------------------------------------------- 状态区


async def test_status_lines_and_error_submenu(
    bus: EventBus, modules: list[TrayModule], tmp_path: Path
) -> None:
    view = view_of(await start_module(bus, modules, tmp_path))
    bus.publish(
        WorkerStatuses({"ocr": WorkerStatus(WorkerState.READY), "nmt": WorkerStatus(WorkerState.FAILED)})
    )
    bus.publish(
        EngineStatuses(
            statuses={
                EngineId("deepseek"): EngineStatus(EngineHealth.ERROR, "HTTP 456", ErrorCode.QUOTA),
                EngineId("rd"): EngineStatus(EngineHealth.BUSY),
            },
            names={EngineId("deepseek"): "DeepSeek", EngineId("rd"): "R&D 引擎"},
        )
    )
    await bus.drain()
    assert menu_texts(view) == [
        "OCR：就绪",
        "本地翻译模型：出错",
        "DeepSeek：出错",
        "R&&D 引擎：忙碌",  # 引擎名里的 & 按字面显示
        *EXPECTED_COMMANDS_ZH,
    ]
    ocr, nmt, deepseek, rd = view.status_actions()
    # 正常状态的条目不可点击；出错的条目可以展开，子菜单里是原因与处理建议。
    assert not ocr.isEnabled()
    assert not rd.isEnabled()
    assert submenu_of(ocr) is None
    for action in (nmt, deepseek):
        assert action.isEnabled()
    deepseek_menu = submenu_of(deepseek)
    assert deepseek_menu is not None
    assert [action.text() for action in deepseek_menu.actions()] == [
        "原因：配额或余额已用完",
        "处理建议：到服务商控制台查看用量，或暂时停用该实例",
    ]
    assert not any(action.isEnabled() for action in deepseek_menu.actions())
    nmt_menu = submenu_of(nmt)
    assert nmt_menu is not None
    assert nmt_menu.actions()[0].text() == "原因：推理进程多次崩溃或无法启动，已停止自动重启"
    assert view.tray.toolTip() == "LSTtranslate\n有 2 项出错"


@pytest.mark.parametrize("state", list(WorkerState))
async def test_every_worker_state_is_rendered(
    bus: EventBus, modules: list[TrayModule], tmp_path: Path, state: WorkerState
) -> None:
    view = view_of(await start_module(bus, modules, tmp_path))
    bus.publish(WorkerStatuses({"ocr": WorkerStatus(state)}))
    await bus.drain()
    expected = {
        WorkerState.STOPPED: "未启用",
        WorkerState.STARTING: "加载中",
        WorkerState.LOADING: "加载中",
        WorkerState.READY: "就绪",
        WorkerState.BUSY: "忙碌",
        WorkerState.CRASHED: "出错",
        WorkerState.FAILED: "出错",
    }[state]
    (action,) = view.status_actions()
    assert action.text() == f"OCR：{expected}"
    has_reason = state in (WorkerState.CRASHED, WorkerState.FAILED)
    assert action.isEnabled() is has_reason
    assert (submenu_of(action) is not None) is has_reason


async def test_status_updates_in_place_until_structure_changes(
    bus: EventBus, modules: list[TrayModule], tmp_path: Path
) -> None:
    view = view_of(await start_module(bus, modules, tmp_path))
    bus.publish(WorkerStatuses({"nmt": WorkerStatus(WorkerState.READY)}))
    await bus.drain()
    (ready,) = view.status_actions()
    bus.publish(WorkerStatuses({"nmt": WorkerStatus(WorkerState.BUSY)}))
    await bus.drain()
    # 同一组条目、都没有原因时只改文字，不重建菜单，菜单打开期间也不会闪烁。
    assert view.status_actions() == [ready]
    assert ready.text() == "本地翻译模型：忙碌"
    bus.publish(WorkerStatuses({"nmt": WorkerStatus(WorkerState.CRASHED)}))
    await bus.drain()
    (crashed,) = view.status_actions()
    assert crashed is not ready
    assert submenu_of(crashed) is not None
    bus.publish(WorkerStatuses({"nmt": WorkerStatus(WorkerState.READY)}))
    await bus.drain()
    (recovered,) = view.status_actions()
    assert submenu_of(recovered) is None
    assert menu_texts(view) == ["本地翻译模型：就绪", *EXPECTED_COMMANDS_ZH]


# ---------------------------------------------------------------- 通知


@pytest.mark.parametrize(
    ("level", "icon"),
    [
        (NoticeLevel.INFO, QSystemTrayIcon.MessageIcon.Information),
        (NoticeLevel.WARNING, QSystemTrayIcon.MessageIcon.Warning),
        (NoticeLevel.ERROR, QSystemTrayIcon.MessageIcon.Critical),
    ],
)
async def test_notification_level_maps_to_message_icon(
    bus: EventBus,
    modules: list[TrayModule],
    tmp_path: Path,
    shown_messages: list[ShownMessage],
    level: NoticeLevel,
    icon: QSystemTrayIcon.MessageIcon,
) -> None:
    i18n.register_catalog(
        {
            "test.tray.module.title": {"zh-Hans": "{name} 通知", "en": "{name} notice"},
            "test.tray.module.body": {"zh-Hans": "第 {n} 条", "en": "number {n}"},
        }
    )
    await start_module(bus, modules, tmp_path)
    bus.publish(
        NotificationRequested(
            "test.tray.module.title", "test.tray.module.body", level, {"name": "OCR", "n": "7"}
        )
    )
    await bus.drain()
    assert shown_messages == [ShownMessage("OCR 通知", "第 7 条", icon, NOTICE_DURATION_MS)]


async def test_hotkey_failure_notification(
    bus: EventBus, modules: list[TrayModule], tmp_path: Path, shown_messages: list[ShownMessage]
) -> None:
    await start_module(bus, modules, tmp_path)
    bus.publish(HotkeyRegistrationFailed("Alt+E", "已被其他程序占用"))
    await bus.drain()
    assert shown_messages == [
        ShownMessage(
            "热键注册失败",
            "热键 Alt+E 注册失败：已被其他程序占用。请在设置 → 热键中换一个组合。",
            QSystemTrayIcon.MessageIcon.Warning,
            NOTICE_DURATION_MS,
        )
    ]


async def test_config_rejections_merge_within_one_second(
    bus: EventBus,
    modules: list[TrayModule],
    tmp_path: Path,
    qtbot: QtBot,
    shown_messages: list[ShownMessage],
) -> None:
    await start_module(bus, modules, tmp_path)
    started = time.monotonic()
    bus.publish(ConfigRejected(("race", "mode"), "bad"))
    bus.publish(ConfigRejected(("engines", 1, "priority"), "bad"))
    await bus.drain()
    qtbot.wait(400)
    bus.publish(ConfigRejected(("ocr", "device"), "bad"))
    await bus.drain()
    assert shown_messages == []
    qtbot.waitUntil(lambda: len(shown_messages) == 1, timeout=2000)
    elapsed = time.monotonic() - started
    # 第一条到达后 1 秒弹出；第三条在窗口内到达，合并进同一条通知。
    assert elapsed >= 0.9
    (message,) = shown_messages
    assert message.title == "配置有误"
    assert message.icon is QSystemTrayIcon.MessageIcon.Warning
    assert message.message.splitlines() == [
        "有 3 处配置无效，已使用默认值。请在设置中检查：",
        "race › mode",
        "engines › 第 2 项 › priority",
        "ocr › device",
    ]
    qtbot.wait(300)
    assert len(shown_messages) == 1


# ---------------------------------------------------------------- 停止与语言


async def test_stopped_module_no_longer_responds(
    bus: EventBus,
    modules: list[TrayModule],
    tmp_path: Path,
    qtbot: QtBot,
    received: list[Message],
    shown_messages: list[ShownMessage],
) -> None:
    module = await start_module(bus, modules, tmp_path, reject_window_ms=200)
    view = view_of(module)
    capture = view.command_action(TrayAction.CAPTURE)
    tray = view.tray
    bus.publish(ConfigRejected(("race", "mode"), "bad"))
    await bus.drain()
    await module.stop()
    # stop 之后、组合根调用 ctx.close 之前仍在排队的消息，不会再交给已经销毁的视图。
    bus.publish(WorkerStatuses({"ocr": WorkerStatus(WorkerState.CRASHED)}))
    bus.publish(NotificationRequested("health.ready", "health.ready"))
    bus.publish(HotkeyRegistrationFailed("Alt+Q", "x"))
    bus.publish(ConfigRejected(("ui", "theme"), "bad"))
    await bus.drain()
    if Shiboken.isValid(capture):
        capture.trigger()
    if Shiboken.isValid(tray):
        tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
    await bus.drain()
    qtbot.wait(400)
    assert shown_messages == []
    assert received == []


async def test_language_switch_updates_menu(bus: EventBus, modules: list[TrayModule], tmp_path: Path) -> None:
    view = view_of(await start_module(bus, modules, tmp_path))
    bus.publish(WorkerStatuses({"ocr": WorkerStatus(WorkerState.READY)}))
    await bus.drain()
    assert menu_texts(view)[:2] == ["OCR：就绪", "---"]
    i18n.set_language("en")
    # 菜单弹出前按当前语言重新生成。
    view.menu.aboutToShow.emit()
    assert menu_texts(view) == [
        "OCR: Ready",
        "---",
        "&Capture and translate\tAlt+Q",
        "Show/hide &bilingual window\tAlt+E",
        "&Settings",
        "Download translation &models…",
        "Open &log folder",
        "Export &diagnostics…",
        "---",
        "E&xit",
    ]
    assert view.tray.toolTip() == "LSTtranslate\nAll ready"
    # 新的 AppConfig 到达时同样按当前语言重新生成。
    i18n.set_language("zh-Hans")
    bus.publish(AppConfig(sections={"ui": {"language": "zh-Hans"}}, revision=5))
    await bus.drain()
    assert menu_texts(view)[0] == "OCR：就绪"
