"""模型的三个子页：本地翻译模型、显卡加速、词典（需求 MOD-01、MOD-04、MOD-06）。

页面标识依次是 models.nmt、models.gpu、models.dicts。

本地翻译模型页有两组：“存放位置”与“本地翻译模型”列表。存放位置是 [nmt].model_dir，单独指定本地翻译模型的
目录；留空时用模型目录下的 nmt，输入框的占位文字就是这个实际位置，模型目录本身在常规页的“存储位置”里更改。
列表来自 ``ModelCatalog``，一行一个语言对。选中一行或几行后，用按钮下载、取消或删除；
下载进度由模型管理模块持续发布的目录状态驱动，列表只更新文字，不重建，选中与焦点不丢。筛选框与“只显示已安装的”对这个列表起作用。

显卡加速页是 [nmt].device 的推理设备下拉框，以及 NVIDIA cuBLAS 12 运行库卡片（``NmtRuntimeCard``），
后者随 ``NmtRuntimeInfo``、NMT 进程状态与配置刷新。

词典页的每一行显示大小、许可证和状态，它的下一行是这个包的署名全文（CC BY-SA 4.0 要求署名可见）；
列表上方另有一段总署名，许可证名链接到许可证全文。模型管理模块不管理词典包时（目录状态的
dicts_enabled 为假），整个词典列表不显示。

打开本地翻译模型页或词典页时获取一次模型索引（需要联网，需求 PRV-01）。
"""

import os
from collections.abc import Mapping
from typing import Final

from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt
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

from lst.contracts.events import WorkerStatus
from lst.contracts.models import ModelKind
from lst.ui.i18n import tr
from lst.ui.settings import strings_flow  # noqa: F401  # pyright: ignore[reportUnusedImport]  登记文案
from lst.ui.settings.cards import Binding, CardBuilder, FieldDef
from lst.ui.settings.controls import DirectoryEditor, SettingCard, ThemedLabel, make_transparent
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.flow import FlowView, NodeNote, NodeState
from lst.ui.settings.flow_specs import BATCH, INFER, MODEL, NMT_FLOW, NMT_FLOW_KEYS, SUBMIT
from lst.ui.settings.model_rows import ModelRow, build_rows, dict_notice, notice
from lst.ui.settings.nmt_runtime_card import NmtRuntimeCard
from lst.ui.settings.page_about import worker_state_text
from lst.ui.settings.pages import Intro, SettingsPage
from lst.ui.settings.presenter import SettingsPresenter, Topic
from lst.ui.settings.strings_dict import dict_credit
from lst.ui.theme import TOKENS, Glyph, fluent_icon, style_primary_button

ID_ROLE: Final = Qt.ItemDataRole.UserRole

NMT_GLYPHS: Final[dict[str, str]] = {
    "langid": Glyph.TRANSLATE_AA,
    "cache": Glyph.HISTORY,
    "batch": Glyph.GRID,
    "submit": Glyph.SEND,
    "route": Glyph.SWITCH,
    "sentences": Glyph.CUT,
    "direct": Glyph.LINK,
    "pivot": Glyph.GLOBE,
    "model": Glyph.PACKAGE,
    "tokenize": Glyph.PUZZLE,
    "infer": Glyph.CHIP,
    "detokenize": Glyph.PENCIL,
    "join": Glyph.MERGE,
}
"""本地翻译流程各节点的图标。"""

MAX_LOADED_MODELS: Final = 4
"""NMT 进程同时常驻的模型数：ct2_backend.Ct2Settings 的 max_loaded_models 默认值，[nmt] 节不提供这一项。"""

DEFAULT_BATCH: Final = (16, 800)
"""本地翻译实例的批大小默认值（段数、字数），
与 LocalNmtSettings 的 max_batch_segments、max_batch_chars 一致。"""
_CREDIT_PADDING: Final = 8
"""署名行在文字高度之外留的上下空白（像素）。"""


class _CreditFitter(QObject):
    """词典列表的视口宽度变化时，按新宽度重新计算署名行的高度。

    署名行跨全部列，QTreeWidget 按第一列的宽度估算行高，长署名会被截断。这里按视口宽度自己计算
    自动换行后的高度，写进署名行的 sizeHint，署名全文因此总是完整可见。
    """

    def __init__(self, tree: QTreeWidget) -> None:
        super().__init__(tree)
        self._tree = tree
        self._width = -1
        tree.viewport().installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Resize and self._tree.viewport().width() != self._width:
            self.fit()
        return False

    def fit(self) -> None:
        tree = self._tree
        self._width = tree.viewport().width()
        width = max(80, self._width - tree.indentation() - 2 * _CREDIT_PADDING)
        metrics = tree.fontMetrics()
        for index in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(index)
            credit = item.child(0) if item is not None else None
            if credit is None:
                continue
            bounds = metrics.boundingRect(
                QRect(0, 0, width, 1 << 20), int(Qt.TextFlag.TextWordWrap), credit.text(0)
            )
            credit.setSizeHint(0, QSize(width, bounds.height() + _CREDIT_PADDING))


class NmtModelsPage(SettingsPage):
    """本地翻译模型：上面是本地翻译的流程图，下面是被点开的步骤的面板。

    “CTranslate2 推理”步骤的面板是模型列表、推理设备与集束宽度，默认打开；
    “取模型”步骤的面板是本地翻译模型的单独目录；
    “分批”步骤的面板有“转到翻译引擎”按钮（批大小是引擎实例的设置）；其余步骤只有说明。
    """

    page_id = "models.nmt"

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self._rows: list[ModelRow] = []
        self.add_title("settings.flow.nmt.title")
        self.add_intro("settings.flow.nmt.intro")
        self.flow = FlowView(NMT_FLOW_KEYS)
        self.add_card(self.flow)
        self.notes: dict[str, NodeNote] = {}
        self.model_dir: Binding | None = None
        self.panel_labels: list[tuple[QLabel, str]] = []
        self.engines_button = QPushButton()
        self.engines_button.setIcon(fluent_icon(Glyph.SETTINGS))
        self.engines_button.clicked.connect(lambda: self.navigate("engines"))
        for key in NMT_FLOW_KEYS:
            panel = QWidget()
            box = QVBoxLayout(panel)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(TOKENS.card_gap)
            note = NodeNote(NMT_GLYPHS[key], self.engines_button if key == BATCH else None)
            self.notes[key] = note
            box.addWidget(note)
            if key == MODEL:
                directory = self.builder.build(FieldDef("nmt", ("model_dir",), widget="dir"))
                if directory is not None:
                    self.model_dir = directory
                    self.bindings.append(directory)
                    box.addWidget(directory.card)
            elif key == INFER:
                self._build_infer_panel(box)
            self.flow.add_panel(key, panel)
        self.flow.set_items(NMT_FLOW)
        self.flow.open(INFER)

    def _build_infer_panel(self, box: QVBoxLayout) -> None:
        """推理步骤的面板：模型列表、推理设备卡片、集束宽度。"""
        presenter = self.presenter
        self._panel_label(box, "settings.models.list", title=True)
        self._panel_label(box, "settings.models.intro", title=False)
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
        box.addWidget(self.list_card)
        self.gpu_button = QPushButton()
        self.gpu_button.setIcon(fluent_icon(Glyph.CHIP))
        self.gpu_button.clicked.connect(lambda: self.navigate("models.gpu"))
        self.device_card = SettingCard(self.gpu_button, icon=Glyph.CHIP)
        box.addWidget(self.device_card)
        beam = self.builder.build(FieldDef("nmt", ("beam_size",), icon=Glyph.SLIDERS))
        if beam is not None:
            self.bindings.append(beam)
            box.addWidget(beam.card)

        self.filter_edit.textChanged.connect(self._filter_changed)
        self.installed_only.toggled.connect(self._filter_changed)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.download_button.clicked.connect(self._download)
        self.cancel_button.clicked.connect(self._cancel)
        self.delete_button.clicked.connect(self._delete)
        self.refresh_button.clicked.connect(presenter.refresh_models)

    def _panel_label(self, box: QVBoxLayout, key: str, *, title: bool) -> QLabel:
        """面板里的分组标题或说明文字，文案键 key；界面语言切换时随页面一起更新。"""
        if title:
            label = ThemedLabel("primary", TOKENS.title_px, semibold=True)
            label.setContentsMargins(0, TOKENS.spacing_l, 0, TOKENS.spacing_xs)
        else:
            label = ThemedLabel("secondary", TOKENS.caption_px)
            label.setWordWrap(True)
            label.setContentsMargins(0, 0, 0, TOKENS.spacing_xs)
        box.addWidget(label)
        self.panel_labels.append((label, key))
        return label

    # ------------------------------------------------------------------ 流程图

    def node_state(self, key: str) -> NodeState:
        """本地翻译流程里一个节点的显示内容。"""
        title = tr(f"settings.flow.node.{key}.title")
        if key == BATCH:
            segments, chars = self._batch_limits()
            status = tr("settings.flow.status.batch", segments=segments, chars=chars)
        elif key == SUBMIT:
            worker = self._nmt_status()
            status = tr("settings.flow.status.worker", state=worker_state_text(worker)) if worker else ""
        elif key == MODEL:
            status = tr("settings.flow.status.model", count=MAX_LOADED_MODELS)
        elif key == INFER:
            status = self._device_text(short=True)
        else:
            status = tr(f"settings.flow.status.{key}")
        return NodeState(title, status, NMT_GLYPHS[key])

    def _nmt_status(self) -> WorkerStatus | None:
        statuses = self.presenter.worker_statuses
        return statuses.statuses.get("nmt") if statuses is not None else None

    def _batch_limits(self) -> tuple[int, int]:
        """本地翻译实例的批大小上限；配置里没有本地翻译实例时用实例的默认值。"""
        segments, chars = DEFAULT_BATCH
        for entry in self.presenter.engines():
            if entry.kind == "local_nmt":
                raw_segments = entry.data.get("max_batch_segments")
                raw_chars = entry.data.get("max_batch_chars")
                if isinstance(raw_segments, int):
                    segments = raw_segments
                if isinstance(raw_chars, int):
                    chars = raw_chars
                break
        return segments, chars

    def _device_text(self, *, short: bool) -> str:
        """推理设备：进程报告了设备与计算类型时写实际值，否则写设置里选的设备。"""
        worker = self._nmt_status()
        versions: Mapping[str, str] = worker.versions if worker is not None else {}
        device = versions.get("device")
        compute = versions.get("compute_type") or "—"
        if device:
            key = "settings.flow.status.device_gpu" if device == "cuda" else "settings.flow.status.device_cpu"
            name = tr(key)
            if short:
                return tr("settings.flow.status.infer", device=name, compute=compute)
            return tr("settings.flow.device.desc", device=name, compute=compute)
        choice = tr(f"settings.choice.nmt.device.{self.presenter.nmt_device or 'auto'}")
        if short:
            return tr("settings.flow.status.infer_pending", device=choice)
        return tr("settings.flow.device.desc_pending", device=choice)

    def _sync_flow(self) -> None:
        for key in self.flow.chart.order:
            self.flow.chart.set_state(key, self.node_state(key))
        self.device_card.set_texts(tr("settings.flow.device.title"), self._device_text(short=False))

    # ------------------------------------------------------------------ 文案与刷新

    def retranslate(self) -> None:
        super().retranslate()
        self.flow.chart.setAccessibleName(tr("settings.flow.nmt.name"))
        for key, note in self.notes.items():
            note.set_texts(tr(f"settings.flow.node.{key}.title"), tr(f"settings.flow.node.{key}.desc"))
        for label, key in self.panel_labels:
            label.setText(tr(key))
        self.engines_button.setText(tr("settings.flow.goto_engines"))
        self.gpu_button.setText(tr("settings.flow.goto_gpu"))
        self._sync_flow()
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
        self._sync_model_dir()

    def refresh_all(self) -> None:
        super().refresh_all()
        self._sync()
        self._sync_flow()
        self._sync_model_dir()

    def refresh(self, topic: Topic) -> None:
        super().refresh(topic)
        if topic is Topic.MODELS:
            self._sync()
        if topic in (Topic.WORKERS, Topic.CONFIG, Topic.ENGINES):
            self._sync_flow()
        if topic is Topic.STORAGE:
            self._sync_model_dir()

    def _sync_model_dir(self) -> None:
        """单独目录留空时的实际位置（模型目录下的 nmt）写在输入框的占位文字里。"""
        editor = self.model_dir.editor if self.model_dir is not None else None
        if not isinstance(editor, DirectoryEditor):
            return
        storage = self.presenter.storage
        if storage is None:
            editor.edit.setPlaceholderText(tr("settings.dir.placeholder"))
            return
        editor.edit.setPlaceholderText(os.path.join(storage.models_dir, "nmt"))

    def on_shown(self) -> None:
        """第一次打开模型页时获取一次模型索引（需要联网，需求 PRV-01）。"""
        super().on_shown()
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


class GpuPage(SettingsPage):
    """显卡加速：推理设备与 NVIDIA 显卡运行库。"""

    page_id = "models.gpu"

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self.build([Intro("settings.nmt_gpu.intro"), FieldDef("nmt", ("device",), icon=Glyph.CHIP)])
        self.runtime_card = NmtRuntimeCard(presenter, dialogs)
        self.add_card(self.runtime_card)

    def retranslate(self) -> None:
        super().retranslate()
        self.runtime_card.retranslate()

    def refresh_all(self) -> None:
        super().refresh_all()
        self.runtime_card.refresh()

    def refresh(self, topic: Topic) -> None:
        super().refresh(topic)
        if topic in (Topic.NMT_RUNTIME, Topic.WORKERS, Topic.CONFIG):
            self.runtime_card.refresh()


class DictsPage(SettingsPage):
    """词典：总署名、词典列表和下载、取消、删除按钮。"""

    page_id = "models.dicts"

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self._dict_rows: list[ModelRow] = []
        self.dict_intro = self.add_intro("settings.dicts.intro")
        self.dict_attribution = ThemedLabel("secondary", TOKENS.caption_px)
        self.dict_attribution.setWordWrap(True)
        self.dict_attribution.setTextFormat(Qt.TextFormat.RichText)
        self.dict_attribution.setOpenExternalLinks(True)
        self.dict_attribution.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.filter_label = QLabel()
        self.filter_edit = QLineEdit()
        self.filter_edit.setClearButtonEnabled(True)
        self.installed_only = QCheckBox()
        self.dict_notice_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.dict_notice_label.setWordWrap(True)
        self.dict_notice_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.dict_tree = QTreeWidget()
        self.dict_tree.setColumnCount(4)
        self.dict_tree.setRootIsDecorated(False)
        self.dict_tree.setIndentation(TOKENS.spacing_l)
        self.dict_tree.setUniformRowHeights(False)
        self.dict_tree.setWordWrap(True)
        self.dict_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.dict_tree.setMinimumHeight(260)
        self.dict_tree.header().setStretchLastSection(True)
        self.dict_tree.setColumnWidth(0, 240)
        self.dict_tree.setColumnWidth(1, 90)
        self.dict_tree.setColumnWidth(2, 110)
        self.dict_tree.setFrameShape(QFrame.Shape.NoFrame)
        self.dict_tree.viewport().setAutoFillBackground(False)
        make_transparent(self.dict_tree)
        self._credit_fitter = _CreditFitter(self.dict_tree)
        self.dict_empty_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.dict_download_button = QPushButton()
        style_primary_button(self.dict_download_button, Glyph.DOWNLOAD)
        self.dict_cancel_button = QPushButton()
        self.dict_delete_button = QPushButton()
        self.dict_delete_button.setIcon(fluent_icon(Glyph.DELETE))

        filters = QHBoxLayout()
        filters.setSpacing(TOKENS.spacing_s)
        filters.addWidget(self.filter_label)
        filters.addWidget(self.filter_edit, 1)
        filters.addWidget(self.installed_only)
        buttons = QHBoxLayout()
        buttons.setSpacing(TOKENS.spacing_s)
        for button in (self.dict_download_button, self.dict_cancel_button, self.dict_delete_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(TOKENS.spacing_s)
        layout.addWidget(self.dict_attribution)
        layout.addWidget(self.dict_notice_label)
        layout.addLayout(filters)
        layout.addWidget(self.dict_tree)
        layout.addWidget(self.dict_empty_label)
        layout.addLayout(buttons)
        self.dict_card = SettingCard(box, stacked=True)
        self.dict_card.hide_header()
        self.body.addWidget(self.dict_card)

        self.filter_edit.textChanged.connect(self._filter_changed)
        self.installed_only.toggled.connect(self._filter_changed)
        self.dict_tree.itemSelectionChanged.connect(self._update_dict_buttons)
        self.dict_download_button.clicked.connect(self._download_dicts)
        self.dict_cancel_button.clicked.connect(self._cancel_dicts)
        self.dict_delete_button.clicked.connect(self._delete_dicts)

    # ------------------------------------------------------------------ 文案与刷新

    def retranslate(self) -> None:
        super().retranslate()
        self.filter_label.setText(tr("settings.models.filter"))
        self.filter_edit.setPlaceholderText(tr("settings.models.filter.placeholder"))
        self.filter_edit.setAccessibleName(tr("settings.models.filter"))
        self.installed_only.setText(tr("settings.models.installed_only"))
        self.dict_attribution.setText(dict_credit())
        self.dict_tree.setHeaderLabels(
            [
                tr("settings.dicts.col.dict"),
                tr("settings.models.col.size"),
                tr("settings.dicts.col.license"),
                tr("settings.models.col.status"),
            ]
        )
        self.dict_tree.setAccessibleName(tr("settings.dicts.list"))
        self.dict_empty_label.setText(tr("settings.dicts.empty"))
        self.dict_download_button.setText(tr("settings.dicts.download"))
        self.dict_cancel_button.setText(tr("settings.dicts.cancel"))
        self.dict_delete_button.setText(tr("settings.dicts.delete"))
        self._sync_dicts()

    def refresh_all(self) -> None:
        super().refresh_all()
        self._sync_dicts()

    def refresh(self, topic: Topic) -> None:
        super().refresh(topic)
        if topic is Topic.MODELS:
            self._sync_dicts()

    def on_shown(self) -> None:
        """第一次打开词典页时获取一次模型索引（需要联网，需求 PRV-01）。"""
        super().on_shown()
        self.presenter.models_page_opened()

    # ------------------------------------------------------------------ 词典列表

    @property
    def dict_rows(self) -> list[ModelRow]:
        """词典列表里当前显示的行。"""
        return list(self._dict_rows)

    def selected_dict_ids(self) -> list[str]:
        return [
            str(item.data(0, ID_ROLE))
            for item in self.dict_tree.selectedItems()
            if item.parent() is None and item.data(0, ID_ROLE) is not None
        ]

    def select_dicts(self, *ids: str) -> None:
        """选中这些词典。"""
        self.dict_tree.clearSelection()
        for index in range(self.dict_tree.topLevelItemCount()):
            item = self.dict_tree.topLevelItem(index)
            if item is not None and item.data(0, ID_ROLE) in ids:
                item.setSelected(True)

    def _sync_dicts(self) -> None:
        catalog = self.presenter.catalog
        enabled = catalog is not None and catalog.dicts_enabled
        self.dict_card.setVisible(enabled)  # 说明总是显示；模型管理模块不管理词典时只隐藏列表
        rows = (
            build_rows(
                catalog,
                needle=self.filter_edit.text(),
                installed_only=self.installed_only.isChecked(),
                kind=ModelKind.DICT,
            )
            if enabled
            else []
        )
        text, is_error = dict_notice(catalog) if enabled else ("", False)
        self.dict_notice_label.setText(text)
        self.dict_notice_label.setVisible(bool(text))
        self.dict_notice_label.set_role("critical" if is_error else "secondary")
        if [row.model_id for row in rows] != [row.model_id for row in self._dict_rows]:
            selected = set(self.selected_dict_ids())
            self.dict_tree.clear()
            for row in rows:
                item = QTreeWidgetItem(["", "", "", ""])
                item.setData(0, ID_ROLE, row.model_id)
                self.dict_tree.addTopLevelItem(item)
                # 署名单独占一行，跨全部列，不能选中：选择只针对词典本身。
                credit = QTreeWidgetItem([""])
                credit.setFlags(Qt.ItemFlag.ItemIsEnabled)
                item.addChild(credit)
                credit.setFirstColumnSpanned(True)
                item.setExpanded(True)
                item.setSelected(row.model_id in selected)
        self._dict_rows = rows
        for index, row in enumerate(rows):
            item = self.dict_tree.topLevelItem(index)
            if item is None:
                continue
            item.setText(0, row.title)
            item.setText(1, row.size)
            item.setText(2, row.license)
            item.setText(3, row.status)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            credit = item.child(0)
            if credit is not None:
                credit.setText(0, row.attribution)
                credit.setToolTip(0, row.attribution)
        self._credit_fitter.fit()
        self.dict_tree.setVisible(bool(rows))
        self.dict_empty_label.setVisible(enabled and not rows)
        self._update_dict_buttons()

    def dict_attribution_text(self, model_id: str) -> str:
        """某个词典下一行显示的署名，没有这一行时为空串。"""
        for index in range(self.dict_tree.topLevelItemCount()):
            item = self.dict_tree.topLevelItem(index)
            if item is not None and item.data(0, ID_ROLE) == model_id:
                credit = item.child(0)
                return credit.text(0) if credit is not None else ""
        return ""

    def _selected_dict_rows(self) -> list[ModelRow]:
        wanted = set(self.selected_dict_ids())
        return [row for row in self._dict_rows if row.model_id in wanted]

    def _filter_changed(self, *_args: object) -> None:
        self._sync_dicts()

    def _update_dict_buttons(self) -> None:
        selected = self._selected_dict_rows()
        self.dict_download_button.setEnabled(any(row.can_download for row in selected))
        self.dict_cancel_button.setEnabled(any(row.can_cancel for row in selected))
        self.dict_delete_button.setEnabled(any(row.can_delete for row in selected))

    def _download_dicts(self) -> None:
        ids = [row.model_id for row in self._selected_dict_rows() if row.can_download]
        self.presenter.download_models(ids)

    def _cancel_dicts(self) -> None:
        for row in self._selected_dict_rows():
            if row.can_cancel:
                self.presenter.cancel_download(row.model_id)

    def _delete_dicts(self) -> None:
        rows = [row for row in self._selected_dict_rows() if row.can_delete]
        if not rows:
            return
        if len(rows) == 1:
            text = tr("settings.dicts.delete.confirm", name=rows[0].title)
        else:
            text = tr("settings.dicts.delete.confirm_many", count=len(rows))
        if self.dialogs.confirm(tr("settings.confirm.title"), text):
            for row in rows:
                self.presenter.delete_model(row.model_id)
