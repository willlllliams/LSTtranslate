"""关于与诊断页：版本号、应用更新、各推理进程的状态、导出诊断信息、自动化接口
（需求 UI-05、UPD-01 至 UPD-06、DEV-01 至 DEV-03）。

版本号后面注明构建类型：开发版写“（开发版）”，普通版不写。

“更新”卡片组：一张更新卡片（``UpdateCard``）显示当前版本、更新状态与操作按钮，下面是 [updates] 节的
三个设置项（自动检查、检查间隔、接收预发布版本）。按钮的显示规则：

- “检查更新”总是显示，检查、下载、启动安装程序期间以及安装包已下载（READY）时不可用；
- “下载”在发现新版本且能自动安装时显示，上次下载失败时也显示，点击即重试；
- “安装并重启”在安装包已下载并通过校验时显示，点击后先确认；
- “查看发布说明”在有可用版本时显示，用系统浏览器打开版本页面。

源码运行（UpdateStatus.can_install 为假）时只检查，卡片里说明这一点。用户点“检查更新”后，检查结束时
状态文字换成“检查完成：……”或“检查失败：……”，无论结果如何都有反馈。

“导出诊断信息”的命令（架构文档 5.3 节的 ExportDiagnostics）还没有定义，随 services.diagnostics 落地，
所以按钮暂时不可用，旁边说明原因。进程状态用托盘里的五种状态显示，出错时给出原因和处理建议，
不显示技术细节；出错的进程旁边有“重新启动”按钮，发送 ``RestartWorker``，由组合根清零崩溃计数后重新启动。

“自动化接口”卡片（``AutomationCard``）：开关、说明与风险、默认值随构建类型的提示，下方一行写当前状态
（正在监听的管道名、未开启、无法开启的原因）。开关显示的是 AutomationStatus 里算好的结果，点击后发
``UpdateConfig {"automation": {"enabled": ...}}``，从此以配置为准。放在本页而不放在常规页：它是给测试脚本与
AI agent 用的诊断功能，与版本号、构建类型放在一起，用户一眼能看出“开发版默认开启”。
"""

from dataclasses import dataclass
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from lst.contracts.automation import AutomationStatus, BuildFlavor
from lst.contracts.events import EngineHealth, WorkerStatus
from lst.contracts.ports import WorkerState
from lst.contracts.updates import UpdateErrorCode, UpdateState, UpdateStatus
from lst.ui.common.text import PROGRESS_STEPS, clamp_fraction, format_size, progress_steps
from lst.ui.i18n import error_text, tr
from lst.ui.settings import strings_automation  # noqa: F401  # pyright: ignore[reportUnusedImport]  登记文案
from lst.ui.settings.cards import Binding, CardBuilder, FieldDef
from lst.ui.settings.controls import SettingCard, ThemedLabel, ToggleSwitch
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.pages import SettingsPage
from lst.ui.settings.presenter import CheckFeedback, SettingsPresenter, Topic
from lst.ui.settings.strings import format_timestamp
from lst.ui.theme import TOKENS, Glyph, fluent_icon, fluent_icon_font, fluent_icon_text, style_primary_button

_WORKER_HEALTH: Final = {
    WorkerState.STOPPED: EngineHealth.DISABLED,
    WorkerState.STARTING: EngineHealth.LOADING,
    WorkerState.LOADING: EngineHealth.LOADING,
    WorkerState.READY: EngineHealth.READY,
    WorkerState.BUSY: EngineHealth.BUSY,
    WorkerState.CRASHED: EngineHealth.ERROR,
    WorkerState.FAILED: EngineHealth.ERROR,
}

_WORKER_ORDER: Final = ("ocr", "nmt")

RESTARTABLE_STATES: Final = frozenset({WorkerState.CRASHED, WorkerState.FAILED})
"""显示“重新启动”的推理进程状态：出错（崩溃后正在退避重启，或已停止自动重启）。"""


def worker_state_text(status: WorkerStatus) -> str:
    """一个推理进程的状态文字。出错时附上原因与处理建议。"""
    health = _WORKER_HEALTH[status.state]
    text = tr(f"settings.worker.{'stopped' if health is EngineHealth.DISABLED else health.value}")
    if status.state is WorkerState.CRASHED:
        return tr("settings.about.worker.detail", state=text, detail=tr("settings.about.worker.crashed"))
    if status.state is WorkerState.FAILED:
        return tr("settings.about.worker.detail", state=text, detail=tr("settings.about.worker.failed"))
    return text


def worker_name(key: str) -> str:
    """推理进程的显示名：ui.i18n 里登记了 worker.<key> 就用它，否则显示进程名。"""
    name_key = f"worker.{key}"
    name = tr(name_key)
    return key if name == name_key else name


# ---------------------------------------------------------------- 应用更新


@dataclass(frozen=True, slots=True)
class UpdateView:
    """更新卡片的显示内容。文字都已按当前界面语言填好。

    progress：None 不显示进度条，-1 表示进度未知（忙碌动画），否则在 0 到 1 之间。
    notes_url 为空时不显示“查看发布说明”；source_note 为真时显示“源码运行只检查不安装”的说明。
    """

    title: str
    details: str
    status: str
    error: bool
    progress: float | None
    can_check: bool
    can_download: bool
    can_install: bool
    notes_url: str
    source_note: bool


_BUSY: Final = frozenset({UpdateState.CHECKING, UpdateState.DOWNLOADING, UpdateState.INSTALLING})


def update_status_text(status: UpdateStatus, feedback: CheckFeedback | None) -> tuple[str, bool]:
    """状态文字与它是否是错误。手动检查结束后用“检查完成”“检查失败”的说法。"""
    release = status.available
    version = release.version if release is not None else ""
    if feedback is CheckFeedback.CHECKING:
        return tr("settings.update.status.checking"), False  # 刚点了“检查更新”，还没有收到结果
    match status.state:
        case UpdateState.IDLE:
            return tr("settings.update.status.idle"), False
        case UpdateState.CHECKING:
            return tr("settings.update.status.checking"), False
        case UpdateState.UP_TO_DATE:
            if feedback is CheckFeedback.UP_TO_DATE:
                return tr("settings.update.feedback.up_to_date", version=status.current_version), False
            return tr("settings.update.status.up_to_date"), False
        case UpdateState.AVAILABLE:
            date = format_timestamp(release.published_at, with_time=False) if release is not None else ""
            key = "available_pre" if release is not None and release.prerelease else "available"
            text = tr(f"settings.update.status.{key}", version=version, date=date)
            if feedback is CheckFeedback.AVAILABLE:
                return tr("settings.update.feedback.done") + text, False
            return text, False
        case UpdateState.DOWNLOADING:
            if status.progress is None:
                return tr("settings.update.status.downloading_unknown", version=version), False
            percent = int(clamp_fraction(status.progress) * 100)
            return tr("settings.update.status.downloading", version=version, percent=percent), False
        case UpdateState.READY:
            return tr("settings.update.status.ready", version=version), False
        case UpdateState.INSTALLING:
            return tr("settings.update.status.installing"), False
        case UpdateState.ERROR:
            reason, suggestion = error_text(status.error_code, UpdateErrorCode.INTERNAL)
            if feedback is CheckFeedback.FAILED:
                advice = tr("settings.ocr_engine.suggestion", suggestion=suggestion)
                return tr("settings.update.feedback.failed", reason=reason) + advice, True
            return tr("settings.update.status.error", reason=reason, suggestion=suggestion), True


def _version_title(version: str) -> str:
    return (
        tr("settings.update.current", version=version) if version else tr("settings.update.current.unknown")
    )


def build_update_view(
    status: UpdateStatus | None, feedback: CheckFeedback | None, app_version: str
) -> UpdateView:
    """更新卡片的内容。status 为 None 表示总线上没有更新状态（组合根没有装配更新服务）。"""
    if status is None:
        return UpdateView(
            title=_version_title(app_version),
            details="",
            status=tr("settings.update.missing"),
            error=False,
            progress=None,
            can_check=False,
            can_download=False,
            can_install=False,
            notes_url="",
            source_note=False,
        )
    details: list[str] = []
    if status.last_checked:
        details.append(tr("settings.update.last_checked", time=format_timestamp(status.last_checked)))
    release = status.available
    if release is not None and status.can_install and release.installer_bytes > 0:
        details.append(tr("settings.update.size", size=format_size(release.installer_bytes)))
    text, error = update_status_text(status, feedback)
    progress: float | None = None
    if status.state is UpdateState.DOWNLOADING:
        progress = -1.0 if status.progress is None else clamp_fraction(status.progress)
    return UpdateView(
        title=_version_title(status.current_version or app_version),
        details=tr("settings.update.details.separator").join(details),
        status=text,
        error=error,
        progress=progress,
        can_check=status.state not in _BUSY and status.state is not UpdateState.READY,
        can_download=status.can_install
        and release is not None
        and status.state in (UpdateState.AVAILABLE, UpdateState.ERROR),
        can_install=status.can_install and status.state is UpdateState.READY,
        notes_url=release.page_url if release is not None else "",
        source_note=not status.can_install,
    )


def _status_glyph(state: UpdateState | None, error: bool) -> tuple[str, str]:
    """状态图标与它的颜色角色。"""
    if error:
        return Glyph.ERROR, "critical"
    if state in _BUSY:
        return Glyph.SYNC, "accent"
    if state in (UpdateState.AVAILABLE, UpdateState.READY):
        return Glyph.DOWNLOAD, "accent"
    if state is UpdateState.UP_TO_DATE:
        return Glyph.COMPLETED, "accent"
    return Glyph.INFO, "secondary"


class UpdateCard(SettingCard):
    """更新卡片：左侧图标，标题是当前版本，说明是上次检查时间与安装包大小，右侧是按钮；下方是状态与进度。"""

    def __init__(self, presenter: SettingsPresenter, dialogs: Dialogs) -> None:
        actions = QWidget()
        self.notes_button = QPushButton()
        self.notes_button.setIcon(fluent_icon(Glyph.OPEN_IN_NEW))
        self.check_button = QPushButton()
        self.check_button.setIcon(fluent_icon(Glyph.REFRESH))
        self.download_button = QPushButton()
        style_primary_button(self.download_button, Glyph.DOWNLOAD)
        self.install_button = QPushButton()
        style_primary_button(self.install_button, Glyph.UPDATE)
        row = QHBoxLayout(actions)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(TOKENS.spacing_s)
        for button in (self.notes_button, self.check_button, self.download_button, self.install_button):
            row.addWidget(button)
        super().__init__(actions, icon=Glyph.UPDATE)
        self._presenter = presenter
        self._dialogs = dialogs
        self.view: UpdateView | None = None

        self.status_icon = ThemedLabel("secondary")
        self.status_icon.setFont(fluent_icon_font(TOKENS.caption_px + 2))
        self.status_icon.setFixedWidth(TOKENS.caption_px + 4)
        self.status_label = ThemedLabel("primary", TOKENS.caption_px)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(TOKENS.spacing_xs + 2)
        self.progress_bar.setRange(0, PROGRESS_STEPS)
        self.source_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.source_label.setWordWrap(True)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(TOKENS.spacing_s)
        status_row.addWidget(self.status_icon, 0, Qt.AlignmentFlag.AlignTop)
        status_row.addWidget(self.status_label, 1)
        self.details = QWidget()
        details = QVBoxLayout(self.details)
        details.setContentsMargins(self.icon_label.width() + TOKENS.spacing_l, 0, 0, 0)
        details.setSpacing(TOKENS.spacing_xs)
        details.addLayout(status_row)
        details.addWidget(self.progress_bar)
        details.addWidget(self.source_label)
        root = self.layout()
        if isinstance(root, QVBoxLayout):
            root.insertWidget(1, self.details)

        self.check_button.clicked.connect(presenter.check_updates)
        self.download_button.clicked.connect(presenter.download_update)
        self.install_button.clicked.connect(self._install)
        self.notes_button.clicked.connect(self._open_notes)
        self.retranslate()

    def retranslate(self) -> None:
        self.check_button.setText(tr("settings.update.check"))
        self.download_button.setText(tr("settings.update.download"))
        self.install_button.setText(tr("settings.update.install"))
        self.notes_button.setText(tr("settings.update.notes"))
        self.source_label.setText(tr("settings.update.source"))
        self.progress_bar.setAccessibleName(tr("settings.update.progress"))
        self.refresh()

    def refresh(self) -> None:
        presenter = self._presenter
        status = presenter.update_status
        view = build_update_view(status, presenter.check_feedback, presenter.app_version)
        self.view = view
        self.set_texts(view.title, view.details)
        glyph, role = _status_glyph(status.state if status is not None else None, view.error)
        self.status_icon.setText(fluent_icon_text(glyph))
        self.status_icon.set_role(role)
        self.status_label.setText(view.status)
        self.status_label.set_role("critical" if view.error else "primary")
        if view.progress is None:
            self.progress_bar.hide()
        else:
            if view.progress < 0:
                self.progress_bar.setRange(0, 0)  # 进度未知：忙碌动画
            else:
                self.progress_bar.setRange(0, PROGRESS_STEPS)
                self.progress_bar.setValue(progress_steps(view.progress))
            self.progress_bar.show()
        self.check_button.setEnabled(view.can_check)
        self.download_button.setVisible(view.can_download)
        self.install_button.setVisible(view.can_install)
        self.notes_button.setVisible(bool(view.notes_url))
        self.source_label.setVisible(view.source_note)

    def _install(self) -> None:
        status = self._presenter.update_status
        release = status.available if status is not None else None
        text = tr("settings.update.install.confirm", version=release.version if release is not None else "")
        if self._dialogs.confirm(tr("settings.confirm.title"), text):
            self._presenter.install_update()

    def _open_notes(self) -> None:
        if self.view is not None and self.view.notes_url:
            self._dialogs.open_url(self.view.notes_url)


# ---------------------------------------------------------------- 自动化接口


def version_text(version: str, flavor: BuildFlavor) -> str:
    """关于页的版本行：开发版在版本号后注明“开发版”。版本号为空时返回空串。"""
    if not version:
        return ""
    if flavor is BuildFlavor.DEV:
        return tr("settings.about.version.flavor", version=version, flavor=tr("settings.flavor.dev"))
    return tr("settings.about.version", version=version)


def automation_status_text(status: AutomationStatus | None) -> tuple[str, bool]:
    """自动化接口卡片下方的状态行与它是否是错误。"""
    if status is None:
        return tr("settings.automation.missing"), False
    if status.enabled and not status.listening:
        return tr("settings.automation.error", reason=status.error or "—"), True
    if status.listening:
        return tr("settings.automation.listening", pipe=status.pipe), False
    return tr("settings.automation.off"), False


class AutomationCard(SettingCard):
    """自动化接口的开关。说明里写清风险，后面注明这个构建类型的默认值；下方一行是当前状态。"""

    def __init__(self, presenter: SettingsPresenter) -> None:
        self.switch = ToggleSwitch()
        super().__init__(self.switch, icon=Glyph.LINK)
        self._presenter = presenter
        self.status_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details = QWidget()
        column = QVBoxLayout(details)
        column.setContentsMargins(self.icon_label.width() + TOKENS.spacing_l, 0, 0, 0)
        column.addWidget(self.status_label)
        root = self.layout()
        if isinstance(root, QVBoxLayout):
            root.insertWidget(1, details)
        self.switch.clicked.connect(self._toggled)
        self.retranslate()

    def retranslate(self) -> None:
        flavor = self._presenter.flavor
        default = tr(f"settings.automation.default.{flavor.value}")
        desc = tr("settings.automation.desc")
        description = tr("settings.automation.desc.full", desc=desc, default=default)
        self.set_texts(tr("settings.automation.title"), description)
        self.switch.setAccessibleName(tr("settings.automation.title"))
        self.refresh()

    def refresh(self) -> None:
        status = self._presenter.automation
        self.switch.setEnabled(status is not None)
        self.switch.setChecked(status is not None and status.enabled)
        text, error = automation_status_text(status)
        self.status_label.setText(text)
        self.status_label.set_role("critical" if error else "secondary")

    def _toggled(self) -> None:
        self._presenter.set_automation_enabled(self.switch.isChecked())


# ---------------------------------------------------------------- 页面


class AboutPage(SettingsPage):
    """关于与诊断页。"""

    page_id = "about"
    icon = Glyph.INFO

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self.body.addSpacing(TOKENS.spacing_m)
        self.name_label = ThemedLabel("primary", TOKENS.page_title_px - 6, semibold=True, display=True)
        self.version_label = ThemedLabel("secondary", TOKENS.title_px)
        self.version_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.tagline_label = ThemedLabel("secondary", TOKENS.title_px)
        about = QWidget()
        about_layout = QVBoxLayout(about)
        about_layout.setContentsMargins(0, 0, 0, 0)
        about_layout.setSpacing(TOKENS.spacing_xs)
        for label in (self.name_label, self.version_label, self.tagline_label):
            about_layout.addWidget(label)
        self.about_card = SettingCard(about, stacked=True)
        self.about_card.hide_header()
        self.add_card(self.about_card)

        self.add_title("settings.section.updates")
        self.update_card = UpdateCard(presenter, dialogs)
        self.add_card(self.update_card)
        self.add_field(FieldDef("updates", ("auto_check",)))
        self.add_field(FieldDef("updates", ("check_interval_hours",), suffix="settings.suffix.hours"))
        self.add_field(FieldDef("updates", ("include_prerelease",)))

        self.add_title("settings.about.workers")
        self.workers_box = QWidget()
        self.workers_layout = QVBoxLayout(self.workers_box)
        self.workers_layout.setContentsMargins(0, 0, 0, 0)
        self.workers_layout.setSpacing(TOKENS.spacing_xs)
        self.worker_labels: dict[str, QLabel] = {}
        self.worker_restart_buttons: dict[str, QPushButton] = {}
        self._worker_rows: dict[str, QWidget] = {}
        self.workers_card = SettingCard(self.workers_box, stacked=True)
        self.workers_card.hide_header()
        self.add_card(self.workers_card)
        self.workers_empty = ThemedLabel("secondary", TOKENS.caption_px)
        self.workers_layout.addWidget(self.workers_empty)

        self.add_title("settings.about.diagnostics")
        self.export_button = QPushButton()
        self.export_button.setIcon(fluent_icon(Glyph.DOWNLOAD))
        self.export_button.setEnabled(False)
        self.export_note = ThemedLabel("secondary", TOKENS.caption_px)
        self.export_note.setWordWrap(True)
        export = QWidget()
        export_layout = QHBoxLayout(export)
        export_layout.setContentsMargins(0, 0, 0, 0)
        export_layout.setSpacing(TOKENS.spacing_m)
        export_layout.addWidget(self.export_button, 0, Qt.AlignmentFlag.AlignTop)
        export_layout.addWidget(self.export_note, 1)
        self.export_card = SettingCard(export, stacked=True)
        self.export_card.hide_header()
        self.add_card(self.export_card)

        self.add_title("settings.section.automation")
        self.automation_card = AutomationCard(presenter)
        self.add_card(self.automation_card)

    def retranslate(self) -> None:
        super().retranslate()
        self.name_label.setText(tr("settings.about.name"))
        version = self.presenter.app_version
        self.version_label.setText(version_text(version, self.presenter.flavor))
        self.version_label.setVisible(bool(version))
        self.tagline_label.setText(tr("settings.about.tagline"))
        self.export_button.setText(tr("settings.about.export"))
        self.export_note.setText(tr("settings.about.export.tip"))
        self.export_button.setToolTip(tr("settings.about.export.tip"))
        self.workers_empty.setText(tr("settings.about.workers.none"))
        self.update_card.retranslate()
        self.automation_card.retranslate()
        self._sync_workers()

    def refresh_all(self) -> None:
        super().refresh_all()
        self._sync_workers()
        self._sync_updates()

    def refresh(self, topic: Topic) -> None:
        super().refresh(topic)
        if topic is Topic.WORKERS:
            self._sync_workers()
        elif topic is Topic.AUTOMATION:
            self.automation_card.refresh()
        elif topic in (Topic.UPDATES, Topic.CONFIG):
            self._sync_updates()

    def binding(self, field: str) -> Binding | None:
        """[updates] 节某个设置项的卡片；组合根没有注入这一节时为 None。"""
        return next((b for b in self.bindings if b.location == ("updates", field)), None)

    def _sync_updates(self) -> None:
        """更新卡片随更新状态刷新；关闭自动检查时“检查间隔”不可用。"""
        self.update_card.refresh()
        interval = self.binding("check_interval_hours")
        if interval is not None:
            interval.card.setEnabled(self.presenter.value("updates", ("auto_check",)) is not False)

    def _sync_workers(self) -> None:
        """每个推理进程一行：状态文字，出错（CRASHED、FAILED）时右侧有“重新启动”按钮。"""
        statuses = self.presenter.worker_statuses
        items = dict(statuses.statuses) if statuses is not None else {}
        keys = [key for key in _WORKER_ORDER if key in items] + sorted(
            k for k in items if k not in _WORKER_ORDER
        )
        for key in [k for k in self._worker_rows if k not in items]:
            row = self._worker_rows.pop(key)
            del self.worker_labels[key], self.worker_restart_buttons[key]
            self.workers_layout.removeWidget(row)
            row.deleteLater()
        for key in keys:
            if key not in self._worker_rows:
                self._add_worker_row(key)
            name = worker_name(key)
            self.worker_labels[key].setText(
                tr("settings.about.worker.line", name=name, state=worker_state_text(items[key]))
            )
            button = self.worker_restart_buttons[key]
            button.setText(tr("settings.about.worker.restart"))
            button.setAccessibleName(tr("settings.about.worker.restart.name", name=name))
            button.setVisible(items[key].state in RESTARTABLE_STATES)
        self.workers_empty.setVisible(not items)

    def _add_worker_row(self, key: str) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(TOKENS.spacing_m)
        label = ThemedLabel("primary", TOKENS.title_px)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        button = QPushButton()
        button.setIcon(fluent_icon(Glyph.REFRESH))
        button.clicked.connect(lambda: self.presenter.restart_worker(key))
        layout.addWidget(label, 1)
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        self._worker_rows[key] = row
        self.worker_labels[key] = label
        self.worker_restart_buttons[key] = button
        self.workers_layout.addWidget(row)


__all__ = [
    "RESTARTABLE_STATES",
    "AboutPage",
    "AutomationCard",
    "UpdateCard",
    "UpdateView",
    "automation_status_text",
    "build_update_view",
    "update_status_text",
    "version_text",
    "worker_name",
    "worker_state_text",
]
