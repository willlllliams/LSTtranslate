"""托盘的展示器（架构文档 11.1 节）：把总线上的状态和事件转换成菜单模型与通知，把菜单操作转换成命令。

本模块不依赖 Qt。展示器与视图之间通过两个协议连接：

- ``TrayView``：展示器调用它刷新菜单、弹出通知、打开目录；
- ``TrayIntents``：视图在用户选择菜单项、单击托盘图标、即将弹出菜单时调用它，展示器实现这个协议。

菜单模型 ``TrayMenu`` 是纯数据，``build_menu`` 按当前界面语言生成它，所以菜单的条目、顺序、热键和状态文本
都可以在没有 Qt 对象的情况下检查。
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from lst.contracts.config import AppConfig, ConfigRejected
from lst.contracts.events import (
    EngineHealth,
    EngineStatuses,
    HotkeyRegistrationFailed,
    NoticeLevel,
    NotificationRequested,
    OpenSettings,
    QuitRequested,
    RestartWorker,
    ShowModelDownloads,
    StartCapture,
    ToggleBilingualWindow,
    WorkerStatus,
    WorkerStatuses,
)
from lst.contracts.messages import Command
from lst.contracts.ports import WorkerState
from lst.contracts.updates import (
    CheckForUpdates,
    DownloadUpdate,
    InstallUpdate,
    UpdateErrorCode,
    UpdateState,
    UpdateStatus,
)
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import ModuleContext, Subscription
from lst.ui.i18n import apply_config_language, error_text, tr
from lst.ui.tray.strings import LOCATION_SEPARATOR

DEFAULT_CAPTURE_HOTKEY: Final = "Alt+Q"
DEFAULT_BILINGUAL_HOTKEY: Final = "Alt+E"

CONFIG_REJECT_WINDOW_MS: Final = 1000
"""第一条 ConfigRejected 到达后等待的毫秒数，窗口内到达的全部合并成一条通知。"""

CONFIG_REJECT_LISTED: Final = 3
"""配置无效的通知里最多列出的位置数。"""

WORKER_HEALTH: Final[Mapping[WorkerState, EngineHealth]] = {
    WorkerState.STOPPED: EngineHealth.DISABLED,
    WorkerState.STARTING: EngineHealth.LOADING,
    WorkerState.LOADING: EngineHealth.LOADING,
    WorkerState.READY: EngineHealth.READY,
    WorkerState.BUSY: EngineHealth.BUSY,
    WorkerState.CRASHED: EngineHealth.ERROR,
    WorkerState.FAILED: EngineHealth.ERROR,
}
"""监管器状态到托盘五种显示状态的映射（架构文档 8.2 节，需求 TR-03）。"""

RESTARTABLE_STATES: Final = frozenset({WorkerState.CRASHED, WorkerState.FAILED})
"""状态区里提供“重新启动”的推理进程状态。"""

# 已知推理进程的显示顺序与名称文案键；其他进程排在后面，直接显示进程名。
_WORKER_ORDER: Final = ("ocr", "nmt")
_WORKER_NAME_KEYS: Final[Mapping[str, str]] = {"ocr": "worker.ocr", "nmt": "worker.nmt"}


class TrayAction(StrEnum):
    """托盘菜单里的命令（需求 TR-02）。"""

    CAPTURE = "capture"
    BILINGUAL = "bilingual"
    SETTINGS = "settings"
    MODELS = "models"
    OPEN_LOGS = "open_logs"
    EXPORT_DIAGNOSTICS = "export_diagnostics"
    UPDATE = "update"
    QUIT = "quit"


MENU_LAYOUT: Final[tuple[TrayAction | None, ...]] = (
    TrayAction.CAPTURE,
    TrayAction.BILINGUAL,
    TrayAction.SETTINGS,
    TrayAction.MODELS,
    TrayAction.OPEN_LOGS,
    TrayAction.EXPORT_DIAGNOSTICS,
    TrayAction.UPDATE,
    None,
    TrayAction.QUIT,
)
"""状态区下方命令区的条目顺序，None 表示分隔线。"""


@dataclass(frozen=True, slots=True)
class StatusLine:
    """状态区的一行，不可执行。

    key 在多次渲染之间保持不变（``worker:ocr``、``engine:<实例号>``），视图据此复用菜单条目。
    details 只在出错时非空，依次是原因和处理建议（需求 TR-03、OBS-04）。text 与 details 都是纯文本，
    视图负责转义其中的 ``&``。restart 是出错的推理进程的进程名，非空时子菜单里有“重新启动”，
    选择它发送 ``RestartWorker``；restart_text 是这一项的文字（含助记符）。
    """

    key: str
    text: str
    health: EngineHealth
    details: tuple[str, ...] = ()
    restart: str = ""
    restart_text: str = ""


@dataclass(frozen=True, slots=True)
class MenuEntry:
    """命令区的一项。text 含 Qt 助记符 ``&``；shortcut 是显示在右侧的热键文本，空串表示不显示。"""

    action: TrayAction
    text: str
    shortcut: str = ""
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class TrayMenu:
    """一次渲染的完整内容：状态区、命令区（None 为分隔线）和托盘图标的悬停提示。"""

    status: tuple[StatusLine, ...]
    entries: tuple[MenuEntry | None, ...]
    tooltip: str


@dataclass(frozen=True, slots=True)
class Notice:
    """一条托盘通知，标题和正文已经按当前界面语言填好。"""

    title: str
    message: str
    level: NoticeLevel = NoticeLevel.INFO


@dataclass(frozen=True, slots=True)
class Hotkeys:
    """菜单里显示的两个热键。"""

    capture: str = DEFAULT_CAPTURE_HOTKEY
    bilingual: str = DEFAULT_BILINGUAL_HOTKEY


class TrayView(Protocol):
    """展示器对视图的要求。"""

    def render(self, menu: TrayMenu) -> None:
        """按菜单模型更新菜单条目和托盘图标的悬停提示。"""
        ...

    def notify(self, notice: Notice) -> None:
        """弹出一条托盘通知。"""
        ...

    def open_folder(self, path: Path) -> bool:
        """用系统文件管理器打开目录，返回是否成功。"""
        ...


class TrayIntents(Protocol):
    """视图把用户操作交给展示器的接口。"""

    def choose(self, action: TrayAction) -> None:
        """用户选择了一个菜单命令。"""
        ...

    def click_icon(self) -> None:
        """用户左键单击了托盘图标。"""
        ...

    def menu_opening(self) -> None:
        """菜单即将弹出。"""
        ...

    def restart_worker(self, worker: str) -> None:
        """用户在出错的推理进程的子菜单里选择了“重新启动”。"""
        ...


class OneShotTimer(Protocol):
    """单次计时器。start 重新开始计时，到时调用构造时给定的回调；stop 取消尚未到时的回调。"""

    def start(self, msec: int, /) -> None: ...

    def stop(self) -> None: ...


type TimerFactory = Callable[[Callable[[], None]], OneShotTimer]
"""用到时回调创建一个单次计时器。视图提供基于 QTimer 的实现，测试注入手动触发的假计时器。"""


# ---------------------------------------------------------------- 纯函数：配置、状态文本、菜单模型


def read_hotkeys(config: AppConfig) -> Hotkeys:
    """从 AppConfig 的 hotkeys 节读取两个热键。节或键缺失、类型不对、为空串时用默认值。

    这一节的模型属于 ui.hotkeys，托盘不依赖它，只读需要的两个键。
    """
    section = config.section("hotkeys")
    if not isinstance(section, dict):
        return Hotkeys()
    return Hotkeys(
        capture=_text_or(section.get("capture"), DEFAULT_CAPTURE_HOTKEY),
        bilingual=_text_or(section.get("bilingual"), DEFAULT_BILINGUAL_HOTKEY),
    )


def _text_or(value: JsonValue, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _status_text(name: str, health: EngineHealth) -> str:
    return tr("tray.status.line", name=name, state=tr(f"health.{health.value}"))


def _details(reason: str, suggestion: str) -> tuple[str, ...]:
    return tr("tray.status.reason", text=reason), tr("tray.status.suggestion", text=suggestion)


def worker_display_name(name: str) -> str:
    """推理进程的显示名：已知进程用 worker.* 文案，其他进程直接用进程名。"""
    key = _WORKER_NAME_KEYS.get(name)
    return tr(key) if key is not None else name


def worker_lines(statuses: Mapping[str, WorkerStatus]) -> list[StatusLine]:
    """推理进程的状态行：OCR、NMT 在前，其他进程按映射顺序排在后面。

    出错时的原因按监管器状态区分：CRASHED 正在退避后重启，FAILED 已停止自动重启；两种状态的子菜单里
    都有“重新启动”，立即重启并清零崩溃计数。未启用（STOPPED）的进程是否启动由配置决定，不提供重新启动。
    监管器的 detail 是技术细节，只进日志和诊断，不在菜单里显示（架构文档 10.3 节）。
    """
    names = [name for name in _WORKER_ORDER if name in statuses]
    names += [name for name in statuses if name not in _WORKER_ORDER]
    lines: list[StatusLine] = []
    for name in names:
        state = statuses[name].state
        health = WORKER_HEALTH[state]
        details: tuple[str, ...] = ()
        if state is WorkerState.CRASHED:
            details = _details(tr("tray.worker.crashed.reason"), tr("tray.worker.crashed.suggestion"))
        elif state is WorkerState.FAILED:
            details = _details(tr("tray.worker.failed.reason"), tr("tray.worker.failed.suggestion"))
        restart = name if state in RESTARTABLE_STATES else ""
        lines.append(
            StatusLine(
                f"worker:{name}",
                _status_text(worker_display_name(name), health),
                health,
                details,
                restart=restart,
                restart_text=tr("tray.worker.restart") if restart else "",
            )
        )
    return lines


def engine_lines(engines: EngineStatuses | None) -> list[StatusLine]:
    """引擎实例的状态行，按 EngineStatuses.statuses 的顺序排列，包括未启用的实例。

    出错时用错误码查原因与处理建议；没有错误码时按 engine.internal 处理。
    """
    if engines is None:
        return []
    lines: list[StatusLine] = []
    for engine_id, status in engines.statuses.items():
        name = engines.names.get(engine_id) or engine_id
        details: tuple[str, ...] = ()
        if status.health is EngineHealth.ERROR:
            details = _details(*error_text(status.error_code))
        lines.append(
            StatusLine(f"engine:{engine_id}", _status_text(name, status.health), status.health, details)
        )
    return lines


def overall_status(healths: Sequence[EngineHealth]) -> str:
    """整体状态：有出错的写出错项数，否则有加载中的写“正在加载”，否则写“全部就绪”。

    未启用的组件不参与判断；忙碌算作就绪。没有任何启用的组件时写“没有启用的组件”。
    """
    active = [health for health in healths if health is not EngineHealth.DISABLED]
    errors = sum(1 for health in active if health is EngineHealth.ERROR)
    if errors:
        return tr("tray.overall.error", count=errors)
    if EngineHealth.LOADING in active:
        return tr("tray.overall.loading")
    if active:
        return tr("tray.overall.ready")
    return tr("tray.overall.idle")


def _offers_install(status: UpdateStatus) -> bool:
    """菜单项是否显示“安装更新 x.y.z…”：发现了新版本、能自动安装，并且没有在进行别的操作。

    ERROR 状态下 available 保留上一次检查到的版本，菜单项照样显示“安装更新”，点击即重试下载。
    """
    return (
        status.available is not None
        and status.can_install
        and status.state in (UpdateState.AVAILABLE, UpdateState.READY, UpdateState.ERROR)
    )


def update_entry(status: UpdateStatus | None) -> MenuEntry:
    """“检查更新”菜单项的文字与可用状态。

    - 还没有收到 UpdateStatus（组合根没有装配更新服务）：显示“检查更新”，禁用；
    - 正在检查、下载、启动安装程序：显示进行中的文字，禁用；
    - 发现新版本且能自动安装（包括已下载就绪、上次下载失败）：显示“安装更新 x.y.z…”；
    - 其他情况（没有检查过、已是最新、检查失败、源码运行）：显示“检查更新”。
    """
    if status is None:
        return MenuEntry(TrayAction.UPDATE, tr("tray.action.update"), enabled=False)
    version = status.available.version if status.available is not None else ""
    match status.state:
        case UpdateState.CHECKING:
            return MenuEntry(TrayAction.UPDATE, tr("tray.update.checking"), enabled=False)
        case UpdateState.DOWNLOADING:
            if status.progress is None:
                text = tr("tray.update.downloading_unknown", version=version)
            else:
                percent = int(min(max(status.progress, 0.0), 1.0) * 100)
                text = tr("tray.update.downloading", version=version, percent=percent)
            return MenuEntry(TrayAction.UPDATE, text, enabled=False)
        case UpdateState.INSTALLING:
            return MenuEntry(TrayAction.UPDATE, tr("tray.update.installing"), enabled=False)
        case _:
            pass
    if _offers_install(status):
        return MenuEntry(TrayAction.UPDATE, tr("tray.update.install", version=version))
    return MenuEntry(TrayAction.UPDATE, tr("tray.action.update"))


def update_command(status: UpdateStatus | None) -> Command | None:
    """点击“检查更新”菜单项时发送的命令；菜单项禁用的状态下返回 None。

    READY 发送 InstallUpdate，显示“安装更新”的其余状态发送 DownloadUpdate，其他状态发送手动检查。
    """
    if status is None or status.state in (
        UpdateState.CHECKING,
        UpdateState.DOWNLOADING,
        UpdateState.INSTALLING,
    ):
        return None
    if _offers_install(status):
        return InstallUpdate() if status.state is UpdateState.READY else DownloadUpdate()
    return CheckForUpdates(manual=True)


def update_error_text(code: UpdateErrorCode | None) -> str:
    """更新错误码的说明。缺失的错误码按 update.internal 处理。"""
    resolved = code if code is not None else UpdateErrorCode.INTERNAL
    return tr(f"update.error.{resolved.value.removeprefix('update.')}")


class UpdateOperation(StrEnum):
    """用户从托盘菜单发起、需要告诉用户结果的更新操作。"""

    CHECK = "check"
    DOWNLOAD = "download"
    INSTALL = "install"


UPDATE_PROGRESS_STATE: Final[Mapping[UpdateOperation, UpdateState]] = {
    UpdateOperation.CHECK: UpdateState.CHECKING,
    UpdateOperation.DOWNLOAD: UpdateState.DOWNLOADING,
    UpdateOperation.INSTALL: UpdateState.INSTALLING,
}
"""每种操作被更新服务接受后进入的状态。"""


def update_result_notice(operation: UpdateOperation, status: UpdateStatus) -> Notice | None:
    """用户发起的更新操作结束后的通知；status 不是这种操作的结果时返回 None。"""
    version = status.available.version if status.available is not None else ""
    match operation, status.state:
        case UpdateOperation.CHECK, UpdateState.UP_TO_DATE:
            return Notice(
                tr("tray.notify.update_latest.title"),
                tr("tray.notify.update_latest.message", version=status.current_version),
            )
        case UpdateOperation.CHECK, UpdateState.AVAILABLE:
            if status.can_install:
                key = "update.notify.available.message"
            else:
                key = "update.notify.available_source.message"
            return Notice(tr("update.notify.available.title"), tr(key, version=version))
        case UpdateOperation.DOWNLOAD, UpdateState.READY:
            return Notice(
                tr("tray.notify.update_ready.title"), tr("tray.notify.update_ready.message", version=version)
            )
        case _, UpdateState.ERROR:
            title = f"tray.notify.update_{operation.value}_failed.title"
            return Notice(tr(title), update_error_text(status.error_code), NoticeLevel.WARNING)
        case _:
            return None


def build_menu(
    app_name: str,
    workers: Mapping[str, WorkerStatus],
    engines: EngineStatuses | None,
    hotkeys: Hotkeys,
    update: UpdateStatus | None = None,
) -> TrayMenu:
    """按当前界面语言生成完整的菜单模型（需求 TR-02、TR-03）。update 决定“检查更新”菜单项的文字。"""
    lines = [*worker_lines(workers), *engine_lines(engines)]
    tooltip = tr("tray.tooltip", app=app_name, status=overall_status([line.health for line in lines]))
    if not lines:
        lines = [StatusLine("none", tr("tray.status.none"), EngineHealth.DISABLED)]
    shortcuts = {TrayAction.CAPTURE: hotkeys.capture, TrayAction.BILINGUAL: hotkeys.bilingual}
    entries = tuple(
        None
        if action is None
        else update_entry(update)
        if action is TrayAction.UPDATE
        else MenuEntry(
            action,
            tr(f"tray.action.{action.value}"),
            shortcut=shortcuts.get(action, ""),
            # 导出诊断信息的命令（架构文档 5.3 节的 ExportDiagnostics）还没有定义，先显示为禁用。
            enabled=action is not TrayAction.EXPORT_DIAGNOSTICS,
        )
        for action in MENU_LAYOUT
    )
    return TrayMenu(tuple(lines), entries, tooltip)


def format_location(location: tuple[str | int, ...]) -> str:
    """配置位置的显示文本：各层用 › 连接，列表下标从 1 开始，例如 ``engines › 第 3 项 › priority``。"""
    if not location:
        return tr("tray.location.root")
    parts = [tr("tray.location.index", n=part + 1) if isinstance(part, int) else part for part in location]
    return LOCATION_SEPARATOR.join(parts)


def config_rejected_notice(locations: Sequence[tuple[str | int, ...]]) -> Notice:
    """把一批无效配置的位置合并成一条通知，正文列出前几处位置。

    正文只写位置，不写原因：原因是技术性说明，逐条写进通知会超出通知的篇幅；
    各项的具体原因由设置窗口显示在对应字段旁边（需求 UI-06）。
    """
    count = len(locations)
    if count == 1:
        head = tr("tray.notify.config_rejected.message_one")
    else:
        head = tr("tray.notify.config_rejected.message", count=count)
    lines = [head, *(format_location(location) for location in locations[:CONFIG_REJECT_LISTED])]
    if count > CONFIG_REJECT_LISTED:
        lines.append(tr("tray.notify.config_rejected.more", count=count - CONFIG_REJECT_LISTED))
    return Notice(tr("tray.notify.config_rejected.title"), "\n".join(lines), NoticeLevel.WARNING)


# ---------------------------------------------------------------- 展示器


class TrayPresenter:
    """托盘的展示器，实现 TrayIntents。

    处理函数都是普通函数，在主线程上由总线调用（架构文档 2.3 节），直接调用视图。
    ``close()`` 之后展示器不再响应任何消息和操作，视图可以随即销毁。
    """

    def __init__(
        self,
        ctx: ModuleContext,
        view: TrayView,
        *,
        app_name: str,
        log_dir: Path,
        timer_factory: TimerFactory,
        reject_window_ms: int = CONFIG_REJECT_WINDOW_MS,
    ) -> None:
        self._ctx = ctx
        self._view = view
        self._app_name = app_name
        self._log_dir = log_dir
        self._reject_window_ms = reject_window_ms
        self._workers: Mapping[str, WorkerStatus] = {}
        self._engines: EngineStatuses | None = None
        self._hotkeys = Hotkeys()
        self._update: UpdateStatus | None = None
        # 用户从菜单发起、还没有结果的更新操作；armed 表示已经看到更新服务接受了这次操作。
        self._update_operation: UpdateOperation | None = None
        self._update_armed = False
        self._last_menu: TrayMenu | None = None
        # 聚合窗口内收到的无效配置位置。dict 当作保持顺序的集合用，同一位置只算一处。
        self._rejected: dict[tuple[str | int, ...], None] = {}
        self._reject_timer = timer_factory(self._flush_rejections)
        self._reject_pending = False
        self._subscriptions: list[Subscription] = []
        self._closed = False

    @property
    def logger(self) -> logging.Logger:
        return self._ctx.logger

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        """先按默认值渲染一次菜单，再订阅消息。State 的当前值随后由总线回放。"""
        self.refresh()
        subscribe = self._ctx.subscribe
        self._subscriptions = [
            subscribe(WorkerStatuses, self._on_worker_statuses),
            subscribe(EngineStatuses, self._on_engine_statuses),
            subscribe(AppConfig, self._on_app_config),
            subscribe(NotificationRequested, self._on_notification),
            subscribe(HotkeyRegistrationFailed, self._on_hotkey_failed),
            subscribe(ConfigRejected, self._on_config_rejected),
            subscribe(UpdateStatus, self._on_update_status),
        ]

    def close(self) -> None:
        """取消订阅和尚未到时的聚合通知。重复调用没有效果。

        订阅也由 ModuleContext.close 统一取消，这里提前取消，是为了让 stop 与 ctx.close 之间
        仍在排队的消息不再交给已经销毁的视图。
        """
        if self._closed:
            return
        self._closed = True
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions = []
        self._reject_timer.stop()
        self._reject_pending = False
        self._rejected.clear()

    def refresh(self) -> None:
        """按当前状态和界面语言重新生成菜单；与上次相同时不打扰视图。"""
        if self._closed:
            return
        menu = build_menu(self._app_name, self._workers, self._engines, self._hotkeys, self._update)
        if menu != self._last_menu:
            self._last_menu = menu
            self._view.render(menu)

    # ------------------------------------------------------------ TrayIntents

    def choose(self, action: TrayAction) -> None:
        if self._closed:
            return
        bus = self._ctx.bus
        match action:
            case TrayAction.CAPTURE:
                bus.send(StartCapture())
            case TrayAction.BILINGUAL:
                bus.send(ToggleBilingualWindow())
            case TrayAction.SETTINGS:
                bus.send(OpenSettings())
            case TrayAction.MODELS:
                bus.send(ShowModelDownloads())
            case TrayAction.OPEN_LOGS:
                self._open_logs()
            case TrayAction.EXPORT_DIAGNOSTICS:
                self.logger.info("导出诊断信息的命令尚未定义，菜单项保持禁用")
            case TrayAction.UPDATE:
                self._start_update_operation()
            case TrayAction.QUIT:
                bus.publish(QuitRequested())

    def click_icon(self) -> None:
        if not self._closed:
            self._ctx.bus.send(ToggleBilingualWindow())

    def menu_opening(self) -> None:
        # 界面语言可能在上次渲染之后改变，弹出前按当前语言重新生成一次。
        self.refresh()

    def restart_worker(self, worker: str) -> None:
        if not self._closed:
            self.logger.info("托盘：重新启动推理进程 %s", worker)
            self._ctx.bus.send(RestartWorker(worker))

    # ------------------------------------------------------------ 消息处理

    def _on_worker_statuses(self, message: WorkerStatuses) -> None:
        self._workers = dict(message.statuses)
        self.refresh()

    def _on_engine_statuses(self, message: EngineStatuses) -> None:
        self._engines = message
        self.refresh()

    def _on_app_config(self, message: AppConfig) -> None:
        # 悬停提示随界面语言变化。组合根设置界面语言的时机晚于本处理函数，所以这里先按配置设置。
        apply_config_language(message)
        self._hotkeys = read_hotkeys(message)
        self.refresh()

    def _start_update_operation(self) -> None:
        command = update_command(self._update)
        if command is None:
            self.logger.debug("更新正在进行或更新服务没有启动，忽略菜单操作")
            return
        if isinstance(command, InstallUpdate):
            operation = UpdateOperation.INSTALL
        elif isinstance(command, DownloadUpdate):
            operation = UpdateOperation.DOWNLOAD
        else:
            operation = UpdateOperation.CHECK
        self._update_operation, self._update_armed = operation, False
        self._ctx.bus.send(command)

    def _on_update_status(self, message: UpdateStatus) -> None:
        """刷新菜单项；用户发起的操作结束时弹出结果通知。

        命令发出后、更新服务处理之前到达的状态是旧状态，不算结果：只有先看到操作对应的进行中状态
        （检查中、下载中），之后的状态才是结果。更新服务拒绝操作时直接发布 ERROR，这时也通知用户。
        启动安装程序成功后程序随即退出，不再通知。
        """
        self._update = message
        self.refresh()
        operation = self._update_operation
        if operation is None:
            return
        if message.state is UPDATE_PROGRESS_STATE[operation]:
            self._update_armed = True
            if operation is UpdateOperation.INSTALL:
                self._update_operation = None
            return
        if not self._update_armed and message.state is not UpdateState.ERROR:
            return
        self._update_operation, self._update_armed = None, False
        notice = update_result_notice(operation, message)
        if notice is not None:
            self._view.notify(notice)

    def _on_notification(self, message: NotificationRequested) -> None:
        params = dict(message.params) if message.params is not None else {}
        title = tr(message.title_key, **params)
        text = tr(message.message_key, **params)
        self._view.notify(Notice(title, text, message.level))

    def _on_hotkey_failed(self, message: HotkeyRegistrationFailed) -> None:
        self._view.notify(
            Notice(
                tr("tray.notify.hotkey_failed.title"),
                tr("tray.notify.hotkey_failed.message", hotkey=message.hotkey, reason=message.reason),
                NoticeLevel.WARNING,
            )
        )

    def _on_config_rejected(self, message: ConfigRejected) -> None:
        self._rejected.setdefault(message.location, None)
        if not self._reject_pending:
            # 窗口从第一条开始计时，不随后续消息顺延，通知最晚在第一条到达后 1 秒弹出。
            self._reject_pending = True
            self._reject_timer.start(self._reject_window_ms)

    def _flush_rejections(self) -> None:
        self._reject_pending = False
        if self._closed or not self._rejected:
            return
        locations = list(self._rejected)
        self._rejected.clear()
        self._view.notify(config_rejected_notice(locations))

    def _open_logs(self) -> None:
        if self._view.open_folder(self._log_dir):
            return
        self.logger.warning("打开日志目录失败：%s", self._log_dir)
        self._view.notify(
            Notice(
                tr("tray.notify.open_logs_failed.title"),
                tr("tray.notify.open_logs_failed.message", path=str(self._log_dir)),
                NoticeLevel.WARNING,
            )
        )
