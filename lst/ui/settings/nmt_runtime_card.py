"""模型页的显卡运行库卡片（需求 MOD-06）。

版式与截屏与识别页的 OCR 引擎卡片相同：左侧图标，中间标题与说明（作用、下载大小与版本、来源与许可），
右侧是按钮；下方与标题文字左对齐，依次是状态行（状态图标与文字）、进度条、处理建议、显卡行、实际设备行。
显示内容由 ``nmt_runtime_rows.build_view`` 给出，控件不随状态重建，焦点与 Tab 顺序不变。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from lst.ui.common.text import PROGRESS_STEPS, progress_steps
from lst.ui.i18n import tr
from lst.ui.settings import strings_nmt  # noqa: F401  # pyright: ignore[reportUnusedImport]  登记文案
from lst.ui.settings.controls import SettingCard, ThemedLabel
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.nmt_runtime_rows import RuntimeView, build_view, size_text
from lst.ui.settings.ocr_engine_rows import Tone
from lst.ui.settings.presenter import SettingsPresenter
from lst.ui.theme import TOKENS, Glyph, fluent_icon, fluent_icon_font, style_primary_button


class _IconRow(QWidget):
    """一行带图标的小字：图标在左，文字自动换行。"""

    def __init__(self) -> None:
        super().__init__()
        self.icon = ThemedLabel("secondary")
        self.icon.setFont(fluent_icon_font(TOKENS.caption_px + 2))
        self.icon.setFixedWidth(TOKENS.caption_px + 4)
        self.label = ThemedLabel("secondary", TOKENS.caption_px)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(TOKENS.spacing_s)
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.label, 1)

    def set_content(self, glyph: str, text: str, *, icon_role: str, text_role: str) -> None:
        self.icon.setText(glyph)
        self.icon.set_role(icon_role)
        self.label.setText(text)
        self.label.set_role(text_role)
        self.setVisible(bool(text))


class NmtRuntimeCard(SettingCard):
    """NVIDIA cuBLAS 12 运行库：下载、取消、重试、删除（删除前确认）。"""

    def __init__(self, presenter: SettingsPresenter, dialogs: Dialogs) -> None:
        actions = QWidget()
        self.download_button = QPushButton()
        style_primary_button(self.download_button, Glyph.DOWNLOAD)
        self.cancel_button = QPushButton()
        self.cancel_button.setIcon(fluent_icon(Glyph.CANCEL))
        self.retry_button = QPushButton()
        self.retry_button.setIcon(fluent_icon(Glyph.REFRESH))
        self.delete_button = QPushButton()
        self.delete_button.setIcon(fluent_icon(Glyph.DELETE))
        row = QHBoxLayout(actions)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(TOKENS.spacing_s)
        for button in (self.download_button, self.cancel_button, self.retry_button, self.delete_button):
            row.addWidget(button)
        super().__init__(actions, icon=Glyph.CHIP)
        self._presenter = presenter
        self._dialogs = dialogs
        self.view: RuntimeView | None = None

        self.status_row = _IconRow()
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(TOKENS.spacing_xs + 2)
        self.progress_bar.setRange(0, PROGRESS_STEPS)
        self.suggestion_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.suggestion_label.setWordWrap(True)
        self.suggestion_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.gpu_row = _IconRow()
        self.device_row = _IconRow()
        self.details = QWidget()
        details = QVBoxLayout(self.details)
        details.setContentsMargins(self.icon_label.width() + TOKENS.spacing_l, 0, 0, 0)
        details.setSpacing(TOKENS.spacing_xs)
        details.addWidget(self.status_row)
        details.addWidget(self.progress_bar)
        details.addWidget(self.suggestion_label)
        details.addWidget(self.gpu_row)
        details.addWidget(self.device_row)
        root = self.layout()
        if isinstance(root, QVBoxLayout):
            root.insertWidget(1, self.details)

        self.download_button.clicked.connect(presenter.install_nmt_runtime)
        self.retry_button.clicked.connect(presenter.install_nmt_runtime)
        self.cancel_button.clicked.connect(presenter.cancel_nmt_runtime_install)
        self.delete_button.clicked.connect(self._delete)
        self.retranslate()

    # ------------------------------------------------------------------ 查询（测试用）

    @property
    def status_label(self) -> QLabel:
        return self.status_row.label

    @property
    def gpu_label(self) -> QLabel:
        return self.gpu_row.label

    @property
    def device_label(self) -> QLabel:
        return self.device_row.label

    # ------------------------------------------------------------------ 文案与刷新

    def retranslate(self) -> None:
        self.cancel_button.setText(tr("settings.nmt_runtime.cancel"))
        self.retry_button.setText(tr("settings.nmt_runtime.retry"))
        self.delete_button.setText(tr("settings.nmt_runtime.delete"))
        self.progress_bar.setAccessibleName(tr("settings.nmt_runtime.progress"))
        self.refresh()

    def refresh(self) -> None:
        presenter = self._presenter
        view = build_view(
            presenter.nmt_runtime, configured=presenter.nmt_device, actual=presenter.nmt_worker_device
        )
        self.view = view
        self.set_texts(view.title, view.description)
        error = view.tone is Tone.ERROR
        self.status_row.set_content(
            view.glyph, view.status, icon_role=view.tone.value, text_role="critical" if error else "primary"
        )
        self._set_progress(view.progress)
        self.suggestion_label.setText(view.suggestion)
        self.suggestion_label.setVisible(bool(view.suggestion))
        self.gpu_row.set_content(view.gpu_glyph, view.gpu, icon_role="secondary", text_role="secondary")
        device_glyph = Glyph.WARNING if view.device_warning else Glyph.LIGHTNING
        device_role = "critical" if view.device_warning else "secondary"
        self.device_row.set_content(device_glyph, view.device, icon_role=device_role, text_role=device_role)
        self.download_button.setText(view.download_text)
        self.download_button.setVisible(view.can_download)
        self.cancel_button.setVisible(view.can_cancel)
        self.retry_button.setVisible(view.can_retry)
        self.delete_button.setVisible(view.can_delete)
        for button in (self.download_button, self.cancel_button, self.retry_button, self.delete_button):
            button.setAccessibleDescription(view.title)

    def _set_progress(self, progress: float | None) -> None:
        if progress is None:
            self.progress_bar.hide()
            return
        if progress < 0:
            self.progress_bar.setRange(0, 0)  # 进度未知：忙碌动画
        else:
            self.progress_bar.setRange(0, PROGRESS_STEPS)
            self.progress_bar.setValue(progress_steps(progress))
        self.progress_bar.show()

    def _delete(self) -> None:
        info = self._presenter.nmt_runtime
        if info is None or info.installed_version is None:
            return
        text = tr("settings.nmt_runtime.delete.confirm", size=size_text(info.installed_bytes))
        if self._dialogs.confirm(tr("settings.confirm.title"), text):
            self._presenter.delete_nmt_runtime()


__all__ = ["NmtRuntimeCard"]
