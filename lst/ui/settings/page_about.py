"""关于与诊断页：版本号、各推理进程的状态、导出诊断信息（需求 UI-05）。

“导出诊断信息”的命令（架构文档 5.3 节的 ExportDiagnostics）还没有定义，随 services.diagnostics 落地，
所以按钮暂时不可用，旁边说明原因。进程状态用托盘里的五种状态显示，出错时给出原因和处理建议，
不显示技术细节。
"""

from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from lst.contracts.events import EngineHealth, WorkerStatus
from lst.contracts.ports import WorkerState
from lst.ui.i18n import tr
from lst.ui.settings.cards import CardBuilder
from lst.ui.settings.controls import SettingCard, ThemedLabel
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.pages import SettingsPage
from lst.ui.settings.presenter import SettingsPresenter, Topic
from lst.ui.settings.strings import has_text
from lst.ui.theme import TOKENS, Glyph, fluent_icon

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


def worker_state_text(status: WorkerStatus) -> str:
    """一个推理进程的状态文字。出错时附上原因与处理建议。"""
    health = _WORKER_HEALTH[status.state]
    text = tr(f"settings.worker.{'stopped' if health is EngineHealth.DISABLED else health.value}")
    if status.state is WorkerState.CRASHED:
        return f"{text}：{tr('settings.about.worker.crashed')}"
    if status.state is WorkerState.FAILED:
        return f"{text}：{tr('settings.about.worker.failed')}"
    return text


def worker_name(key: str) -> str:
    """推理进程的显示名：ui.i18n 里登记了 worker.<key> 就用它，否则显示进程名。"""
    name_key = f"worker.{key}"
    name = tr(name_key)
    return key if name == name_key else name


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

        self.add_title("settings.about.workers")
        self.workers_box = QWidget()
        self.workers_layout = QVBoxLayout(self.workers_box)
        self.workers_layout.setContentsMargins(0, 0, 0, 0)
        self.workers_layout.setSpacing(TOKENS.spacing_xs)
        self.worker_labels: dict[str, QLabel] = {}
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
        self.finish()

    def retranslate(self) -> None:
        super().retranslate()
        self.name_label.setText(tr("settings.about.name"))
        version = self.presenter.app_version
        self.version_label.setText(tr("settings.about.version", version=version) if version else "")
        self.version_label.setVisible(bool(version))
        self.tagline_label.setText(tr("settings.about.tagline"))
        self.export_button.setText(tr("settings.about.export"))
        self.export_note.setText(tr("settings.about.export.tip"))
        self.export_button.setToolTip(tr("settings.about.export.tip"))
        self.workers_empty.setText(tr("settings.about.workers.none"))
        self._sync_workers()

    def refresh_all(self) -> None:
        super().refresh_all()
        self._sync_workers()

    def refresh(self, topic: Topic) -> None:
        super().refresh(topic)
        if topic is Topic.WORKERS:
            self._sync_workers()

    def _sync_workers(self) -> None:
        statuses = self.presenter.worker_statuses
        items = dict(statuses.statuses) if statuses is not None else {}
        keys = [key for key in _WORKER_ORDER if key in items] + sorted(
            k for k in items if k not in _WORKER_ORDER
        )
        for key in [k for k in self.worker_labels if k not in items]:
            label = self.worker_labels.pop(key)
            self.workers_layout.removeWidget(label)
            label.deleteLater()
        for key in keys:
            label = self.worker_labels.get(key)
            if label is None:
                label = ThemedLabel("primary", TOKENS.title_px)
                label.setWordWrap(True)
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                self.worker_labels[key] = label
                self.workers_layout.addWidget(label)
            label.setText(
                tr("settings.about.worker.line", name=worker_name(key), state=worker_state_text(items[key]))
            )
        self.workers_empty.setVisible(not items)


__all__ = ["AboutPage", "has_text", "worker_name", "worker_state_text"]
