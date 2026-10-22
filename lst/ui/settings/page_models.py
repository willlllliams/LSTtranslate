"""模型页：本地翻译模型的下载、取消、删除，以及模型目录（需求 MOD-01、MOD-04）。

列表来自 ``ModelCatalog``，一行一个语言对。选中一行或几行后，用按钮下载、取消或删除；
下载进度由模型管理模块持续发布的目录状态驱动，列表只更新文字，不重建，选中与焦点不丢。
"""

from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lst.ui.i18n import tr
from lst.ui.settings.cards import CardBuilder, FieldDef
from lst.ui.settings.controls import SettingCard, ThemedLabel, make_transparent
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.model_rows import ModelRow, build_rows, notice
from lst.ui.settings.pages import Intro, SettingsPage, Title
from lst.ui.settings.presenter import SettingsPresenter, Topic
from lst.ui.theme import TOKENS, Glyph, fluent_icon, style_primary_button

ID_ROLE: Final = Qt.ItemDataRole.UserRole


class ModelsPage(SettingsPage):
    """模型页。"""

    page_id = "models"
    icon = Glyph.PACKAGE

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self._rows: list[ModelRow] = []
        self.build(
            [
                Title("settings.section.models_dir"),
                FieldDef("nmt", ("model_dir",), widget="dir"),
                Title("settings.models.list"),
                Intro("settings.models.intro"),
            ]
        )
        self.notice_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.notice_label.setWordWrap(True)
        self.notice_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.filter_label = QLabel()
        self.filter_edit = QLineEdit()
        self.filter_edit.setClearButtonEnabled(True)
        self.installed_only = QCheckBox()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setMinimumHeight(260)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 240)
        self.tree.setColumnWidth(1, 100)
        self.empty_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.refresh_button = QPushButton()
        self.refresh_button.setIcon(fluent_icon(Glyph.REFRESH))
        self.download_button = QPushButton()
        style_primary_button(self.download_button, Glyph.DOWNLOAD)
        self.cancel_button = QPushButton()
        self.delete_button = QPushButton()
        self.delete_button.setIcon(fluent_icon(Glyph.DELETE))

        filters = QHBoxLayout()
        filters.setSpacing(TOKENS.spacing_s)
        filters.addWidget(self.filter_label)
        filters.addWidget(self.filter_edit, 1)
        filters.addWidget(self.installed_only)
        buttons = QHBoxLayout()
        buttons.setSpacing(TOKENS.spacing_s)
        for button in (self.download_button, self.cancel_button, self.delete_button, self.refresh_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.tree.setFrameShape(QFrame.Shape.NoFrame)
        self.tree.viewport().setAutoFillBackground(False)
        make_transparent(self.tree)
        list_box = QWidget()
        list_layout = QVBoxLayout(list_box)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(TOKENS.spacing_s)
        list_layout.addWidget(self.notice_label)
        list_layout.addLayout(filters)
        list_layout.addWidget(self.tree)
        list_layout.addWidget(self.empty_label)
        list_layout.addLayout(buttons)
        self.list_card = SettingCard(list_box, stacked=True)
        self.list_card.hide_header()
        self.body.addWidget(self.list_card)

        self.filter_edit.textChanged.connect(self._filter_changed)
        self.installed_only.toggled.connect(self._filter_changed)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.download_button.clicked.connect(self._download)
        self.cancel_button.clicked.connect(self._cancel)
        self.delete_button.clicked.connect(self._delete)
        self.refresh_button.clicked.connect(presenter.refresh_models)

    # ------------------------------------------------------------------ 文案与刷新

    def retranslate(self) -> None:
        super().retranslate()
        self.filter_label.setText(tr("settings.models.filter"))
        self.filter_edit.setPlaceholderText(tr("settings.models.filter.placeholder"))
        self.filter_edit.setAccessibleName(tr("settings.models.filter"))
        self.installed_only.setText(tr("settings.models.installed_only"))
        self.tree.setHeaderLabels(
            [
                tr("settings.models.col.model"),
                tr("settings.models.col.size"),
                tr("settings.models.col.status"),
            ]
        )
        self.tree.setAccessibleName(tr("settings.models.list"))
        self.empty_label.setText(tr("settings.models.empty"))
        self.download_button.setText(tr("settings.models.download"))
        self.cancel_button.setText(tr("settings.models.cancel"))
        self.delete_button.setText(tr("settings.models.delete"))
        self.refresh_button.setText(tr("settings.models.refresh"))
        self._sync()

    def refresh_all(self) -> None:
        super().refresh_all()
        self._sync()

    def refresh(self, topic: Topic) -> None:
        super().refresh(topic)
        if topic is Topic.MODELS:
            self._sync()

    def on_shown(self) -> None:
        """第一次打开模型页时获取一次模型索引（需要联网，需求 PRV-01）。"""
        self.presenter.models_page_opened()

    # ------------------------------------------------------------------ 列表

    @property
    def rows(self) -> list[ModelRow]:
        """列表里当前显示的行。"""
        return list(self._rows)

    def selected_ids(self) -> list[str]:
        return [str(item.data(0, ID_ROLE)) for item in self.tree.selectedItems()]

    def select(self, *ids: str) -> None:
        """选中这些模型（测试和键盘之外的程序化选择用）。"""
        self.tree.clearSelection()
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if item is not None and item.data(0, ID_ROLE) in ids:
                item.setSelected(True)

    def _sync(self) -> None:
        catalog = self.presenter.catalog
        rows = build_rows(
            catalog, needle=self.filter_edit.text(), installed_only=self.installed_only.isChecked()
        )
        text, is_error = notice(catalog)
        self.notice_label.setText(text)
        self.notice_label.setVisible(bool(text))
        self.notice_label.set_role("critical" if is_error else "secondary")
        if [row.model_id for row in rows] != [row.model_id for row in self._rows]:
            selected = set(self.selected_ids())
            self.tree.clear()
            for row in rows:
                item = QTreeWidgetItem(["", "", ""])
                item.setData(0, ID_ROLE, row.model_id)
                self.tree.addTopLevelItem(item)
                item.setSelected(row.model_id in selected)
        self._rows = rows
        for index, row in enumerate(rows):
            item = self.tree.topLevelItem(index)
            if item is None:
                continue
            item.setText(0, row.title)
            item.setText(1, row.size)
            item.setText(2, row.status)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.tree.setVisible(bool(rows))
        self.empty_label.setVisible(not rows)
        self._update_buttons()

    def _filter_changed(self, *_args: object) -> None:
        self._sync()

    def _selected_rows(self) -> list[ModelRow]:
        wanted = set(self.selected_ids())
        return [row for row in self._rows if row.model_id in wanted]

    def _update_buttons(self) -> None:
        selected = self._selected_rows()
        self.download_button.setEnabled(any(row.can_download for row in selected))
        self.cancel_button.setEnabled(any(row.can_cancel for row in selected))
        self.delete_button.setEnabled(any(row.can_delete for row in selected))

    # ------------------------------------------------------------------ 操作

    def _download(self) -> None:
        ids = [row.model_id for row in self._selected_rows() if row.can_download]
        self.presenter.download_models(ids)

    def _cancel(self) -> None:
        for row in self._selected_rows():
            if row.can_cancel:
                self.presenter.cancel_download(row.model_id)

    def _delete(self) -> None:
        rows = [row for row in self._selected_rows() if row.can_delete]
        if not rows:
            return
        if len(rows) == 1:
            text = tr("settings.models.delete.confirm", name=rows[0].title)
        else:
            text = tr("settings.models.delete.confirm_many", count=len(rows))
        if self.dialogs.confirm(tr("settings.confirm.title"), text):
            for row in rows:
                self.presenter.delete_model(row.model_id)
