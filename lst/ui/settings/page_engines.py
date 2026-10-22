"""翻译引擎页：引擎实例列表、拖拽排序、增删、参数编辑、API key 与测试连接（需求 ENG-02、UI-05、CFG-04）。

列表按优先级排列，靠前的优先级更高。拖拽放下后（或点“上移”“下移”后），按新顺序把优先级重新编号为
1、2、3……再发送 ``UpdateConfig``，数组节整体替换。勾选框表示是否启用。选中一个实例后，下方显示该类引擎的参数，
字段、取值范围和默认值都来自注入的 pydantic 模型（``schema.model_field_specs``）。

API key 不经过配置：输入框里的内容在按 Enter 或离开输入框时去掉首尾空白，用 ``UpdateSecret`` 提交，
提交后清空，界面永远不显示已保存的 key，
只显示“已设置”“未设置”。

OpenAI 兼容引擎的“附带截图”（send_image，需求 ENG-07）关闭时，“截图尺寸上限”（image_max_side）的卡片不可用；
开关一拨动就随之变化，不等配置回传。
"""

from itertools import pairwise
from typing import Final, cast

from PySide6.QtCore import QModelIndex, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lst.contracts.events import EngineHealth
from lst.contracts.worker_protocol import JsonValue
from lst.ui.i18n import error_text, tr
from lst.ui.settings.cards import FIELD_GLYPHS, Binding, CardBuilder, make_editor
from lst.ui.settings.controls import SettingCard, TextEditor, ThemedLabel, ValueEditor, make_transparent
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.entries import (
    ADVANCED_FIELDS,
    COMMON_FIELDS,
    HIDDEN_FIELDS,
    KIND_DEFAULT_NAMES,
    PROVIDER_PRESETS,
    EngineEntry,
    preset_for,
)
from lst.ui.settings.pages import SettingsPage
from lst.ui.settings.presenter import (
    API_KEY_FIELD,
    CheckState,
    KeyState,
    Location,
    SettingsPresenter,
    Topic,
)
from lst.ui.settings.schema import FieldSpec, Kind, model_field_specs
from lst.ui.settings.strings_free import text_or
from lst.ui.theme import TOKENS, Glyph, engine_color, fluent_icon, style_primary_button

ID_ROLE: Final = Qt.ItemDataRole.UserRole
DEFAULT_TIMEOUT_S: Final = 60.0
"""没有默认时限的引擎类别（在线大语言模型）新建时使用的任务时限。"""

_MARKER_SIZE: Final = QSize(14, 14)


def status_text(presenter: SettingsPresenter, entry: EngineEntry) -> str:
    """列表里一个实例的状态文字：启用的实例取引擎模块发布的状态，没有启用的显示“未启用”。"""
    health = presenter.engine_health(entry.id)
    if not entry.enabled:
        health = EngineHealth.DISABLED
    return tr(f"health.{health.value}") if health is not None else ""


# ---------------------------------------------------------------- 添加实例的对话框


class AddEngineDialog(QDialog):
    """添加一个引擎实例：选择类别、填写必填的参数，可以同时填写 API key。

    必填的参数是模型里没有默认值的字段（例如接口地址、模型名、应用 ID），由 pydantic 模型决定，
    新增一类引擎时这里不需要改动。参数不合法时“添加”按钮不可用，对话框下方说明原因。
    """

    def __init__(self, presenter: SettingsPresenter, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._presenter = presenter
        self.setModal(True)
        self.kind_combo = QComboBox()
        for kind in presenter.engine_kind_names:
            self.kind_combo.addItem("", kind)
        self.provider_combo = QComboBox()
        for preset in PROVIDER_PRESETS:
            self.provider_combo.addItem("", preset.provider)
        self.name_edit = QLineEdit()
        self.form = QFormLayout()
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._field_edits: dict[str, QLineEdit] = {}
        self._labels: dict[str, QLabel] = {}
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.add_button = QPushButton()
        self.add_button.setDefault(True)
        style_primary_button(self.add_button)
        self.cancel_button = QPushButton()

        self._kind_label = QLabel()
        self._provider_label = QLabel()
        self._name_label = QLabel()
        self._key_label = QLabel()
        self.form.addRow(self._kind_label, self.kind_combo)
        self.form.addRow(self._provider_label, self.provider_combo)
        self.form.addRow(self._name_label, self.name_edit)
        self._fields_anchor = self.form.rowCount()
        self.form.addRow(self._key_label, self.key_edit)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.setSpacing(TOKENS.spacing_m)
        layout.addWidget(self.hint_label)
        layout.addLayout(self.form)
        layout.addWidget(self.problem_label)
        layout.addLayout(buttons)
        self.setMinimumWidth(460)

        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        self.name_edit.textChanged.connect(self._validate)
        self.key_edit.textChanged.connect(self._validate)
        self.add_button.clicked.connect(self._submit)
        self.cancel_button.clicked.connect(self.reject)
        self.retranslate()
        self._kind_changed()

    # ---- 当前选择

    @property
    def kind(self) -> str:
        data = self.kind_combo.currentData()
        return data if isinstance(data, str) else ""

    @property
    def provider(self) -> str:
        data = self.provider_combo.currentData()
        return data if isinstance(data, str) else ""

    def field_edit(self, name: str) -> QLineEdit:
        return self._field_edits[name]

    def values(self) -> dict[str, JsonValue]:
        """把对话框里填的内容整理成一条记录的字段（不含实例号和优先级）。"""
        values: dict[str, JsonValue] = {"name": self.name_edit.text().strip()}
        model = self._presenter.model_for(self.kind)
        specs = model_field_specs(model) if model is not None else {}
        for name, edit in self._field_edits.items():
            values[name] = edit.text().strip()
        if "provider" in specs:
            values["provider"] = self.provider
        if "timeout_s" in specs and specs["timeout_s"].required:
            values["timeout_s"] = (
                preset_for(self.provider).timeout_s if "provider" in specs else DEFAULT_TIMEOUT_S
            )
        return values

    # ---- 文案

    def retranslate(self) -> None:
        self.setWindowTitle(tr("settings.add.title"))
        self.hint_label.setText(tr("settings.add.hint"))
        self._kind_label.setText(tr("settings.add.kind"))
        self._provider_label.setText(tr("settings.add.provider"))
        self._name_label.setText(tr("settings.ef.name.title"))
        self._key_label.setText(tr("settings.engines.apikey.title"))
        self.add_button.setText(tr("settings.add.ok"))
        self.cancel_button.setText(tr("settings.add.cancel"))
        for index in range(self.kind_combo.count()):
            kind = str(self.kind_combo.itemData(index))
            self.kind_combo.setItemText(index, text_or(f"settings.kind.{kind}", kind))
        for index in range(self.provider_combo.count()):
            provider = str(self.provider_combo.itemData(index))
            self.provider_combo.setItemText(
                index, text_or(f"settings.choice.engine.provider.{provider}", provider)
            )
        for name, label in self._labels.items():
            label.setText(text_or(f"settings.ef.{name}.title", name))

    # ---- 内部

    def _kind_changed(self) -> None:
        for name in list(self._field_edits):
            self.form.removeRow(self._labels.pop(name))
            del self._field_edits[name]
        model = self._presenter.model_for(self.kind)
        specs = model_field_specs(model) if model is not None else {}
        # 必填而又没有默认值的字段：服务、接口地址、模型名、应用 ID 等。时限由界面给默认值。
        required = [
            name
            for name, spec in specs.items()
            if spec.required and name not in HIDDEN_FIELDS and name not in ("name", "timeout_s", "provider")
        ]
        for offset, name in enumerate(required):
            label = QLabel(text_or(f"settings.ef.{name}.title", name))
            edit = QLineEdit()
            edit.textChanged.connect(self._validate)
            self.form.insertRow(self._fields_anchor + offset, label, edit)
            self._labels[name] = label
            self._field_edits[name] = edit
        has_provider = "provider" in specs
        self._provider_label.setVisible(has_provider)
        self.provider_combo.setVisible(has_provider)
        keyless = self.kind in self._presenter.keyless_kinds
        self._key_label.setVisible(not keyless)
        self.key_edit.setVisible(not keyless)
        self._fill_defaults()
        self._relink_tab_order()
        self._validate()

    def _provider_changed(self) -> None:
        self._fill_defaults()
        self._validate()

    def _fill_defaults(self) -> None:
        """按类别（和服务）预填名称、接口地址、模型名。"""
        kind = self.kind
        preset = preset_for(self.provider) if kind == "openai_compat" else None
        name = preset.name if preset is not None else KIND_DEFAULT_NAMES.get(kind, "")
        self.name_edit.setText(name)
        if preset is not None:
            for field, text in (("base_url", preset.base_url), ("model", preset.model)):
                if field in self._field_edits:
                    self._field_edits[field].setText(text)

    def _relink_tab_order(self) -> None:
        chain: list[QWidget] = [self.kind_combo]
        if self.provider_combo.isVisibleTo(self):
            chain.append(self.provider_combo)
        chain.append(self.name_edit)
        chain.extend(self._field_edits.values())
        if self.key_edit.isVisibleTo(self):
            chain.append(self.key_edit)
        chain.extend((self.add_button, self.cancel_button))
        for first, second in pairwise(chain):
            QWidget.setTabOrder(first, second)

    def errors(self) -> dict[str, str]:
        return self._presenter.check_new_engine(self.kind, self.values())

    def _validate(self) -> None:
        errors = self.errors()
        self.add_button.setEnabled(not errors)
        if errors:
            field, reason = next(iter(errors.items()))
            title = text_or(f"settings.ef.{field}.title", field)
            self.problem_label.setText(tr("settings.add.invalid", field=title, reason=reason))
        else:
            self.problem_label.setText("")

    def _submit(self) -> None:
        errors = self._presenter.add_engine(self.kind, self.values(), self.key_edit.text() or None)
        if errors:
            self._validate()
            return
        self.key_edit.clear()
        self.accept()


# ---------------------------------------------------------------- API key 卡片


class SecretField(QWidget):
    """API key 输入框、状态文字和“清除”按钮。输入的内容提交后清空，已保存的 key 永远不显示。"""

    def __init__(self, engine_id: str, presenter: SettingsPresenter) -> None:
        super().__init__()
        self.engine_id = engine_id
        self._presenter = presenter
        # 只在按 Enter 或离开输入框时提交：按停顿提交会把输入到一半的 key 写进密钥库。
        self.editor = TextEditor(password=True, finish_only=True)
        self.status_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.clear_button = QPushButton()
        self.clear_button.setIcon(fluent_icon(Glyph.DELETE))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(TOKENS.spacing_s)
        row.addWidget(self.editor)
        row.addWidget(self.clear_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(TOKENS.spacing_xs)
        layout.addLayout(row)
        layout.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignRight)
        self.setFocusProxy(self.editor.edit)
        self.editor.committed.connect(self._submit)
        self.editor.edit.editingFinished.connect(self._finished)
        self.clear_button.clicked.connect(lambda: presenter.clear_api_key(engine_id))
        self.retranslate()
        self.refresh()

    def retranslate(self) -> None:
        self.editor.edit.setPlaceholderText(tr("settings.engines.apikey.placeholder"))
        self.clear_button.setText(tr("settings.engines.apikey.clear"))
        self.refresh()

    def refresh(self) -> None:
        state = self._presenter.key_state(self.engine_id)
        optional = self._presenter.key_optional(self.engine_id)
        key = {
            KeyState.SET: "settings.engines.apikey.set",
            KeyState.UNSET: "settings.engines.apikey.unset",
            KeyState.UNKNOWN: "settings.engines.apikey.unknown",
            KeyState.NOT_NEEDED: "settings.engines.apikey.unset",
        }[state]
        text = tr(key)
        if state is KeyState.UNSET and optional:
            text += tr("settings.engines.apikey.optional")
        self.status_label.setText(text)
        self.clear_button.setEnabled(state in (KeyState.SET, KeyState.UNKNOWN))

    def _submit(self, value: object) -> None:
        # 空白内容不提交：清空的输入框表示“没有输入”，不是删除已保存的 key（删除用“清除”按钮）。
        if isinstance(value, str) and value.strip():
            self._presenter.set_api_key(self.engine_id, value)

    def _finished(self) -> None:
        """离开输入框或按 Enter：提交还没发出的内容，然后清空输入框，界面上不留 key。"""
        self.editor.flush()
        self.editor.reload("")


# ---------------------------------------------------------------- 页面


class EnginesPage(SettingsPage):
    """翻译引擎页。"""

    page_id = "engines"
    icon = Glyph.TRANSLATE

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self._syncing = False
        self._editor_for: tuple[str, str] | None = None
        self._editor_bindings: list[Binding] = []
        self._editor_cards: list[QWidget] = []
        self.secret_field: SecretField | None = None
        self._key_card: SettingCard | None = None
        self.add_dialog: AddEngineDialog | None = None

        self.intro_label = self.add_intro("settings.engines.intro")
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setMinimumHeight(170)
        self.list.setIconSize(_MARKER_SIZE)
        self.add_button = QPushButton()
        style_primary_button(self.add_button, Glyph.ADD)
        self.remove_button = QPushButton()
        self.remove_button.setIcon(fluent_icon(Glyph.DELETE))
        self.up_button = QPushButton()
        self.up_button.setIcon(fluent_icon(Glyph.UP))
        self.down_button = QPushButton()
        self.down_button.setIcon(fluent_icon(Glyph.DOWN))
        self.test_button = QPushButton()
        self.test_button.setIcon(fluent_icon(Glyph.REFRESH))
        self.test_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.test_label.setWordWrap(True)
        self.test_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.empty_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.empty_label.setWordWrap(True)
        self.editor_title = self.add_title_label()
        self.editor_host = QVBoxLayout()
        self.editor_host.setSpacing(TOKENS.spacing_s)
        self.select_hint = ThemedLabel("secondary", TOKENS.caption_px)
        self.select_hint.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.setSpacing(TOKENS.spacing_s)
        for button in (
            self.add_button,
            self.remove_button,
            self.up_button,
            self.down_button,
            self.test_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.viewport().setAutoFillBackground(False)
        make_transparent(self.list)
        list_box = QWidget()
        list_layout = QVBoxLayout(list_box)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(TOKENS.spacing_s)
        list_layout.addWidget(self.list)
        list_layout.addWidget(self.empty_label)
        list_layout.addLayout(buttons)
        list_layout.addWidget(self.test_label)
        self.list_card = SettingCard(list_box, stacked=True)
        self.list_card.hide_header()
        self.body.addWidget(self.list_card)
        self.body.addSpacing(TOKENS.spacing_s)
        self.body.addWidget(self.editor_title)
        self.body.addWidget(self.select_hint)
        self.body.addLayout(self.editor_host)

        self.list.itemChanged.connect(self._item_changed)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        self.list.model().rowsMoved.connect(self._rows_moved)
        self.add_button.clicked.connect(self.open_add_dialog)
        self.remove_button.clicked.connect(self._remove)
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))
        self.test_button.clicked.connect(self._test)

    def add_title_label(self) -> QLabel:
        return ThemedLabel("primary", TOKENS.title_px, semibold=True)

    # ------------------------------------------------------------------ 文案与刷新

    def retranslate(self) -> None:
        super().retranslate()
        self.list.setAccessibleName(tr("settings.engines.list"))
        self.add_button.setText(tr("settings.engines.add"))
        self.remove_button.setText(tr("settings.engines.remove"))
        self.up_button.setText(tr("settings.engines.up"))
        self.down_button.setText(tr("settings.engines.down"))
        self.test_button.setText(tr("settings.engines.test"))
        self.test_button.setToolTip(tr("settings.engines.test.tip"))
        self.empty_label.setText(tr("settings.engines.none"))
        self.select_hint.setText(tr("settings.engines.select"))
        for binding in self._editor_bindings:
            binding.retranslate()
        if self.secret_field is not None:
            self.secret_field.retranslate()
        if self.add_dialog is not None:
            self.add_dialog.retranslate()
        self._sync_items()
        self._update_editor_title()
        self._update_buttons()
        self._update_test_label()

    def refresh_all(self) -> None:
        super().refresh_all()
        self.refresh(Topic.ENGINES)

    def refresh(self, topic: Topic) -> None:
        super().refresh(topic)
        match topic:
            case Topic.CONFIG | Topic.ENGINES:
                self._sync_items()
                self._sync_editor()
                self._update_buttons()
                self._update_test_label()
            case Topic.CONNECTION:
                self._update_test_label()
            case Topic.REJECTIONS:
                for binding in self._editor_bindings:
                    binding.refresh_error(self.presenter)
                self._refresh_key_error()
            case _:
                # LANGUAGE 由基类调用 retranslate（本类重写的版本），这里不再重复。
                pass

    def all_bindings(self) -> list[Binding]:
        return [*self.bindings, *self._editor_bindings]

    def claims(self, location: Location) -> bool:
        if super().claims(location):
            return True
        if len(location) >= 3 and location[0] == "engines" and location[2] == API_KEY_FIELD:
            return self.secret_field is not None and self.secret_field.engine_id == location[1]
        return False

    def flush(self) -> None:
        super().flush()
        if self.secret_field is not None:
            self.secret_field.editor.flush()

    # ------------------------------------------------------------------ 列表

    @property
    def selected_id(self) -> str | None:
        item = cast(QListWidgetItem | None, self.list.currentItem())
        if item is None or not item.isSelected():
            return None
        data = item.data(ID_ROLE)
        return data if isinstance(data, str) else None

    def ids(self) -> list[str]:
        """列表里从上到下的实例号。"""
        return [str(self.list.item(row).data(ID_ROLE)) for row in range(self.list.count())]

    def select(self, engine_id: str) -> None:
        for row in range(self.list.count()):
            if self.list.item(row).data(ID_ROLE) == engine_id:
                self.list.setCurrentRow(row)
                return

    def _row_text(self, rank: int, entry: EngineEntry) -> str:
        name = self.presenter.engine_name(entry.id)
        status = status_text(self.presenter, entry)
        if status:
            return tr("settings.engines.row.status", rank=rank, name=name, status=status)
        return tr("settings.engines.row", rank=rank, name=name)

    def _marker(self, entry: EngineEntry) -> QIcon:
        return fluent_icon(Glyph.CIRCLE_FILL, engine_color(entry.id, entry.color))

    def _tooltip(self, entry: EngineEntry) -> str:
        code = self.presenter.engine_error_code(entry.id)
        if code is None:
            return ""
        reason, suggestion = error_text(code)
        return tr("settings.engines.error.tip", reason=reason, suggestion=suggestion)

    def _sync_items(self) -> None:
        """让列表与配置一致：顺序不变时只更新文字和勾选状态，不重建，焦点与选中都保留。"""
        entries = self.presenter.engines()
        wanted = [entry.id for entry in entries]
        self._syncing = True
        try:
            if wanted != self.ids():
                selected = self.selected_id
                self.list.clear()
                for entry in entries:
                    item = QListWidgetItem()
                    item.setData(ID_ROLE, entry.id)
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsUserCheckable
                        | Qt.ItemFlag.ItemIsDragEnabled
                    )
                    self.list.addItem(item)
                pick = self.presenter.pending_select if self.presenter.pending_select in wanted else selected
                if pick is None and entries:
                    pick = entries[0].id
                if pick is not None:
                    self.presenter.pending_select = None
                    self.select(pick)
            for rank, entry in enumerate(entries, start=1):
                item = self.list.item(rank - 1)
                item.setText(self._row_text(rank, entry))
                item.setIcon(self._marker(entry))
                item.setToolTip(self._tooltip(entry))
                item.setCheckState(Qt.CheckState.Checked if entry.enabled else Qt.CheckState.Unchecked)
        finally:
            self._syncing = False
        self.list.setVisible(bool(entries))
        self.empty_label.setVisible(not entries)

    def _item_changed(self, item: QListWidgetItem) -> None:
        if self._syncing:
            return
        engine_id = item.data(ID_ROLE)
        if isinstance(engine_id, str):
            self.presenter.set_engine_field(engine_id, "enabled", item.checkState() == Qt.CheckState.Checked)

    def _rows_moved(self, *_args: object) -> None:
        """拖拽放下（或点了上移、下移）：按列表里的新顺序重新编号优先级并提交。"""
        if self._syncing:
            return
        self._relabel_rows()
        self.presenter.reorder_engines(self.ids())
        self._update_buttons()

    def _relabel_rows(self) -> None:
        """列表顺序变了：立即按新顺序重写序号，不等配置回传。"""
        by_id = {entry.id: entry for entry in self.presenter.engines()}
        self._syncing = True
        try:
            for row in range(self.list.count()):
                item = self.list.item(row)
                entry = by_id.get(str(item.data(ID_ROLE)))
                if entry is not None:
                    item.setText(self._row_text(row + 1, entry))
        finally:
            self._syncing = False

    def _move(self, delta: int) -> None:
        row = self.list.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.list.count():
            return
        # QAbstractItemModel.moveRow 的目标行是“放到它之前”：向下移一位要放到下一行之后。
        destination = target + 1 if delta > 0 else target
        self.list.model().moveRow(QModelIndex(), row, QModelIndex(), destination)
        self.list.setCurrentRow(target)

    def _selection_changed(self) -> None:
        if self._syncing:
            return
        self._sync_editor()
        self._update_buttons()
        self._update_test_label()

    def _update_buttons(self) -> None:
        row = self.list.currentRow()
        selected = self.selected_id is not None
        self.remove_button.setEnabled(selected)
        self.test_button.setEnabled(selected)
        self.up_button.setEnabled(selected and row > 0)
        self.down_button.setEnabled(selected and 0 <= row < self.list.count() - 1)

    # ------------------------------------------------------------------ 添加、删除、测试

    def open_add_dialog(self) -> None:
        """打开“添加翻译引擎”对话框（非阻塞，窗口级模态）。"""
        dialog = AddEngineDialog(self.presenter, self.window())
        if self.window().testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
            dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)

        def closed(_result: int) -> None:
            self._dialog_closed(dialog)

        dialog.finished.connect(closed)
        self.add_dialog = dialog
        dialog.open()

    def _dialog_closed(self, dialog: AddEngineDialog) -> None:
        if self.add_dialog is dialog:
            self.add_dialog = None
        dialog.deleteLater()

    def _remove(self) -> None:
        engine_id = self.selected_id
        if engine_id is None:
            return
        name = self.presenter.engine_name(engine_id)
        if self.dialogs.confirm(
            tr("settings.confirm.title"), tr("settings.engines.remove.confirm", name=name)
        ):
            self.presenter.remove_engine(engine_id)

    def _test(self) -> None:
        engine_id = self.selected_id
        if engine_id is not None:
            self.presenter.check_connection(engine_id)

    def _update_test_label(self) -> None:
        engine_id = self.selected_id
        result = self.presenter.connection(engine_id) if engine_id is not None else None
        if result is None:
            self.test_label.setText("")
            return
        if result.state is CheckState.CHECKING:
            self.test_label.setText(tr("settings.engines.test.running"))
            self.test_label.set_role("secondary")
        elif result.state is CheckState.OK:
            if result.latency_ms is not None:
                self.test_label.setText(tr("settings.engines.test.ok", ms=f"{result.latency_ms:.0f}"))
            else:
                self.test_label.setText(tr("settings.engines.test.ok_plain"))
            self.test_label.set_role("secondary")
        else:
            reason, suggestion = error_text(result.error_code)
            self.test_label.setText(tr("settings.engines.test.failed", reason=reason, suggestion=suggestion))
            self.test_label.set_role("critical")
        self.test_button.setEnabled(result.state is not CheckState.CHECKING and self.selected_id is not None)

    # ------------------------------------------------------------------ 参数编辑

    def _update_editor_title(self) -> None:
        engine_id = self.selected_id
        if engine_id is None:
            self.editor_title.setText("")
            return
        self.editor_title.setText(tr("settings.engines.editor", name=self.presenter.engine_name(engine_id)))

    def _sync_editor(self) -> None:
        """选中的实例变了（或类别变了）时重建参数表单，否则只刷新里面的值。"""
        engine_id = self.selected_id
        entry = self.presenter.engine(engine_id) if engine_id is not None else None
        wanted = (entry.id, entry.kind) if entry is not None else None
        if wanted != self._editor_for:
            self._clear_editor()
            self._editor_for = wanted
            if entry is not None:
                self._build_editor(entry)
        else:
            for binding in self._editor_bindings:
                binding.refresh(self.presenter)
            if self.secret_field is not None:
                self.secret_field.refresh()
        self._sync_image_fields()
        self.select_hint.setVisible(entry is None)
        self._update_editor_title()

    def _clear_editor(self) -> None:
        """丢弃当前的参数表单。先把还没提交的输入提交出去，切换实例不丢用户刚输入的内容。"""
        for binding in self._editor_bindings:
            binding.editor.flush()
        if self.secret_field is not None:
            self.secret_field.editor.flush()
        for card in self._editor_cards:
            self.editor_host.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._editor_cards.clear()
        self._editor_bindings.clear()
        self.secret_field = None
        self._key_card = None

    def _field_order(self, model_specs: dict[str, FieldSpec]) -> list[str]:
        """参数的显示顺序：名称与启用，（API key），该类引擎特有的参数，最后是高级参数。"""
        visible = [name for name in model_specs if name not in HIDDEN_FIELDS]
        common = [name for name in COMMON_FIELDS if name in visible]
        advanced = [name for name in ADVANCED_FIELDS if name in visible]
        specific = [name for name in visible if name not in common and name not in advanced]
        return [*common, *specific, *advanced]

    def _build_editor(self, entry: EngineEntry) -> None:
        model = self.presenter.model_for(entry.kind)
        if model is None:
            return
        specs = model_field_specs(model)
        order = self._field_order(specs)
        common = [name for name in order if name in COMMON_FIELDS]
        advanced = [name for name in order if name in ADVANCED_FIELDS]
        specific = [name for name in order if name not in common and name not in advanced]
        for name in common:
            self._add_param(entry, name, specs[name])
        if self.presenter.key_state(entry.id) is not KeyState.NOT_NEEDED:
            self._add_key_card(entry)
        for name in specific:
            self._add_param(entry, name, specs[name])
        for name in advanced:
            self._add_param(entry, name, specs[name])

    def _add_widget(self, widget: QWidget) -> None:
        self.editor_host.addWidget(widget)
        self._editor_cards.append(widget)

    def _add_param(self, entry: EngineEntry, name: str, spec: FieldSpec) -> None:
        if spec.kind is Kind.OTHER:
            return
        editor, relabel = make_editor(
            spec,
            choice_prefix=f"settings.choice.engine.{name}",
            dialogs=self.dialogs,
            debounce_ms=self.builder.debounce_ms,
        )
        location: Location = ("engines", entry.id, name)
        stacked = name in ("system_prompt",)
        card = SettingCard(
            editor, location=location, stacked=stacked, icon=FIELD_GLYPHS.get(f"engine.{name}", "")
        )
        engine_id = entry.id

        def commit(value: object) -> None:
            self.presenter.set_engine_field(engine_id, name, cast(JsonValue, value))

        editor.committed.connect(commit)
        editor.edited.connect(lambda: self.presenter.clear_rejection(location))
        if name in IMAGE_FIELDS:
            editor.edited.connect(self._sync_image_fields)

        def load(presenter: SettingsPresenter) -> JsonValue:
            current = presenter.engine(engine_id)
            if current is None:
                return None
            value = current.data.get(name)
            return spec.default if value is None and spec.kind not in _NULLABLE else value

        binding = Binding(location, card, editor, title_key=f"settings.ef.{name}", relabel=relabel, load=load)
        binding.retranslate()
        binding.refresh(self.presenter)
        binding.refresh_error(self.presenter)
        self._editor_bindings.append(binding)
        self._add_widget(card)

    def _add_key_card(self, entry: EngineEntry) -> None:
        field = SecretField(entry.id, self.presenter)
        card = SettingCard(
            field, location=("engines", entry.id, API_KEY_FIELD), icon=FIELD_GLYPHS["engine.api_key"]
        )
        card.set_texts(tr("settings.engines.apikey.title"), tr("settings.engines.apikey.desc"))
        field.editor.edit.setAccessibleName(
            tr("settings.engines.apikey.name", name=self.presenter.engine_name(entry.id))
        )
        field.editor.edited.connect(lambda: self.presenter.clear_rejection(card.location))
        self.secret_field = field
        self._key_card = card
        self._refresh_key_error()
        self._add_widget(card)

    def _refresh_key_error(self) -> None:
        if self.secret_field is not None and self._key_card is not None:
            self._key_card.set_error(self.presenter.rejection_for(self._key_card.location))

    def _sync_image_fields(self) -> None:
        """“截图尺寸上限”只在“附带截图”开启时可用。按开关当前的显示判断，用户刚拨动时立即生效。"""
        switch = self.editor_editor("send_image")
        card = self.editor_card("image_max_side")
        if switch is not None and card is not None:
            card.setEnabled(switch.json_value() is True)

    def editor_card(self, name: str) -> SettingCard | None:
        """参数表单里某个字段的卡片，没有时返回 None。"""
        for binding in self._editor_bindings:
            if binding.location[-1] == name:
                return binding.card
        return None

    def editor_editor(self, name: str) -> ValueEditor | None:
        card = self.editor_card(name)
        return card.editor if isinstance(card, SettingCard) and isinstance(card.editor, ValueEditor) else None


_NULLABLE: Final = frozenset({Kind.OPTIONAL_FLOAT, Kind.OPTIONAL_TEXT, Kind.TEXT_LIST})

IMAGE_FIELDS: Final = frozenset({"send_image"})
"""修改后要立即重新判断“截图尺寸上限”是否可用的字段。"""
