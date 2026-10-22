"""常规页的“存储位置”一组：数据目录与模型目录两张卡片（需求 CFG-06、CFG-07、MOD-04，架构文档 11.7 节）。

两张卡片的版式与模型页的显卡运行库卡片相同：左侧图标，中间标题与说明，右侧是“更改…”“打开”按钮；
下方与标题文字左对齐，依次是当前路径、状态行（进度、重启提示或不能更改的原因）、进度条。

- 数据目录：“更改…”先选目录，再确认（对话框里的“把现有数据复制到新位置”默认勾选），然后发 ChangeDataDir。
  更改在重启程序后生效，状态行显示“重启后改用 …”和“立即重启”按钮。数据目录由命令行参数或环境变量指定时，
  “更改…”不可用，状态行说明原因；
- 模型目录：“更改…”先选目录，再确认（“把现有模型移动到新目录”默认勾选），然后发 ChangeModelsDir。
  不在默认位置时多一个“恢复默认位置”。移动期间状态行与进度条显示进度，两张卡片的“更改…”都不可用。

更改失败的原因（StorageLocations.failure）显示在对应卡片的下方。内容全部来自展示器的 ``storage``，
控件不随状态重建，焦点与 Tab 顺序不变。
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QPushButton, QVBoxLayout, QWidget

from lst.contracts.storage import DataDirSource, StorageKind, StorageLocations, StorageProgress
from lst.ui.common.text import PROGRESS_STEPS, format_size, progress_steps
from lst.ui.i18n import tr
from lst.ui.settings import strings_storage  # noqa: F401  # pyright: ignore[reportUnusedImport]  登记文案
from lst.ui.settings.controls import SettingCard, ThemedLabel
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.presenter import SettingsPresenter
from lst.ui.theme import TOKENS, Glyph, fluent_icon


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def progress_text(task: StorageProgress) -> str:
    """进度的文字：已经知道总大小时写“已完成 x / y”，否则只写在做什么。"""
    key = f"settings.storage.{task.kind.value}.working"
    if task.total_bytes <= 0:
        return tr(key)
    done = tr(
        "settings.storage.progress", done=format_size(task.done_bytes), total=format_size(task.total_bytes)
    )
    return f"{tr(key)} {done}"


class StorageCard(SettingCard):
    """一个目录：右侧“更改…”“打开”（模型目录另有“恢复默认位置”），下方是路径、状态行与进度条。"""

    def __init__(self, kind: StorageKind, glyph: str) -> None:
        actions = QWidget()
        self.change_button = QPushButton()
        self.change_button.setIcon(fluent_icon(Glyph.PENCIL))
        self.reset_button = QPushButton()
        self.reset_button.setIcon(fluent_icon(Glyph.REFRESH))
        self.open_button = QPushButton()
        self.open_button.setIcon(fluent_icon(Glyph.FOLDER))
        row = QHBoxLayout(actions)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(TOKENS.spacing_s)
        for button in (self.change_button, self.reset_button, self.open_button):
            row.addWidget(button)
        super().__init__(actions, icon=glyph)
        self.kind = kind
        self.path_label = ThemedLabel("primary", TOKENS.caption_px)
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.restart_button = QPushButton()
        self.restart_button.setIcon(fluent_icon(Glyph.POWER))
        status = QWidget()
        status_row = QHBoxLayout(status)
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(TOKENS.spacing_m)
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.restart_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(TOKENS.spacing_xs + 2)
        self.progress_bar.setRange(0, PROGRESS_STEPS)
        self.progress_bar.hide()
        details = QWidget()
        column = QVBoxLayout(details)
        column.setContentsMargins(self.icon_label.width() + TOKENS.spacing_l, 0, 0, 0)
        column.setSpacing(TOKENS.spacing_xs)
        column.addWidget(self.path_label)
        column.addWidget(status)
        column.addWidget(self.progress_bar)
        root = self.layout()
        if isinstance(root, QVBoxLayout):
            root.insertWidget(1, details)
        self.reset_button.hide()
        self.restart_button.hide()

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    def set_progress(self, task: StorageProgress | None) -> None:
        if task is None:
            self.progress_bar.hide()
            return
        if task.total_bytes <= 0:
            self.progress_bar.setRange(0, 0)  # 还在统计大小，或同一个卷上的改名：忙碌动画
        else:
            self.progress_bar.setRange(0, PROGRESS_STEPS)
            self.progress_bar.setValue(progress_steps(task.done_bytes / task.total_bytes))
        self.progress_bar.show()


class StorageSection:
    """数据目录与模型目录两张卡片，以及它们的操作。页面负责把卡片放进布局。"""

    def __init__(self, presenter: SettingsPresenter, dialogs: Dialogs) -> None:
        self._presenter = presenter
        self._dialogs = dialogs
        self.data_card = StorageCard(StorageKind.DATA, Glyph.FOLDER)
        self.models_card = StorageCard(StorageKind.MODELS, Glyph.PACKAGE)
        self.data_card.change_button.clicked.connect(self.change_data_dir)
        self.data_card.open_button.clicked.connect(lambda: self._open(self.data_dir))
        self.data_card.restart_button.clicked.connect(presenter.restart_app)
        self.models_card.change_button.clicked.connect(self.change_models_dir)
        self.models_card.reset_button.clicked.connect(self.reset_models_dir)
        self.models_card.open_button.clicked.connect(lambda: self._open(self.models_dir))

    @property
    def cards(self) -> tuple[StorageCard, StorageCard]:
        return self.data_card, self.models_card

    @property
    def locations(self) -> StorageLocations | None:
        return self._presenter.storage

    @property
    def data_dir(self) -> str:
        locations = self.locations
        return locations.data_dir if locations is not None else ""

    @property
    def models_dir(self) -> str:
        locations = self.locations
        return locations.models_dir if locations is not None else ""

    # ------------------------------------------------------------------ 文案与刷新

    def retranslate(self) -> None:
        for card in self.cards:
            prefix = f"settings.storage.{card.kind.value}"
            card.change_button.setText(tr("settings.storage.change"))
            card.open_button.setText(tr("settings.storage.open"))
            card.progress_bar.setAccessibleName(tr(f"{prefix}.working"))
            for button in (card.change_button, card.open_button, card.reset_button, card.restart_button):
                button.setAccessibleDescription(tr(f"{prefix}.title"))
        self.models_card.reset_button.setText(tr("settings.storage.models.reset"))
        self.data_card.restart_button.setText(tr("settings.storage.restart"))
        self.refresh()

    def refresh(self) -> None:
        locations = self.locations
        for card in self.cards:
            prefix = f"settings.storage.{card.kind.value}"
            available = locations is not None
            card.set_texts(
                tr(f"{prefix}.title"), tr(f"{prefix}.desc" if available else "settings.storage.unavailable")
            )
            for button in (card.change_button, card.open_button):
                button.setVisible(available)
        if locations is None:
            for card in self.cards:
                card.path_label.hide()
                card.set_status("")
                card.set_progress(None)
                card.reset_button.hide()
                card.restart_button.hide()
                card.set_error(None)
            return
        busy = locations.task is not None
        self._refresh_data(locations, busy)
        self._refresh_models(locations, busy)
        for card in self.cards:
            failure = locations.failure
            if failure is not None and failure.kind is card.kind:
                card.set_error(
                    tr(f"settings.storage.error.{failure.code.value}", detail=failure.detail or "—")
                )
            else:
                card.set_error(None)

    def _refresh_data(self, locations: StorageLocations, busy: bool) -> None:
        card = self.data_card
        card.path_label.setText(locations.data_dir)
        card.path_label.show()
        fixed = locations.data_source.fixed
        card.change_button.setEnabled(not fixed and not busy)
        task = (
            locations.task if locations.task is not None and locations.task.kind is StorageKind.DATA else None
        )
        card.set_progress(task)
        pending = locations.pending_data_dir
        if task is not None:
            card.set_status(progress_text(task))
        elif fixed:
            card.set_status(tr(f"settings.storage.data.fixed.{locations.data_source.value}"))
        elif pending is not None:
            card.set_status(tr("settings.storage.data.pending", path=pending))
        elif locations.data_source is DataDirSource.DEFAULT:
            card.set_status(tr("settings.storage.data.default"))
        else:
            card.set_status("")
        card.restart_button.setVisible(pending is not None and task is None)
        card.restart_button.setEnabled(not busy)  # 模型目录还在移动时不重启

    def _refresh_models(self, locations: StorageLocations, busy: bool) -> None:
        card = self.models_card
        card.path_label.setText(locations.models_dir)
        card.path_label.show()
        card.change_button.setEnabled(not busy)
        card.reset_button.setVisible(not locations.models_default)
        card.reset_button.setEnabled(not busy)
        task = (
            locations.task
            if locations.task is not None and locations.task.kind is StorageKind.MODELS
            else None
        )
        card.set_progress(task)
        if task is not None:
            card.set_status(progress_text(task))
        elif locations.models_default:
            card.set_status(tr("settings.storage.models.default"))
        else:
            card.set_status("")

    # ------------------------------------------------------------------ 操作

    def _open(self, path: str) -> None:
        if path:
            self._dialogs.open_path(path)

    def change_data_dir(self) -> None:
        locations = self.locations
        if locations is None or locations.data_source.fixed or locations.task is not None:
            return
        start = locations.pending_data_dir or locations.data_dir
        chosen = self._dialogs.choose_directory(tr("settings.storage.data.choose"), start)
        if not chosen:
            return
        target = os.path.normpath(chosen)
        if _same(target, locations.data_dir):
            if locations.pending_data_dir is not None:
                self._presenter.change_data_dir(target, copy=False)  # 选回当前目录：撤销还没生效的更改
            return
        if locations.pending_data_dir is not None and _same(target, locations.pending_data_dir):
            return
        copy = self._dialogs.confirm_option(
            tr("settings.storage.data.title"),
            tr("settings.storage.data.confirm", path=target),
            tr("settings.storage.data.copy"),
            True,
        )
        if copy is None:
            return
        self.data_card.set_error(None)
        self._presenter.change_data_dir(target, copy=copy)

    def change_models_dir(self) -> None:
        locations = self.locations
        if locations is None or locations.task is not None:
            return
        chosen = self._dialogs.choose_directory(tr("settings.storage.models.choose"), locations.models_dir)
        if not chosen:
            return
        target = os.path.normpath(chosen)
        if _same(target, locations.models_dir):
            return
        self._confirm_models(target, target)

    def reset_models_dir(self) -> None:
        locations = self.locations
        if locations is None or locations.task is not None or locations.models_default:
            return
        self._confirm_models("", os.path.join(locations.data_dir, "models"))

    def _confirm_models(self, target: str, shown: str) -> None:
        move = self._dialogs.confirm_option(
            tr("settings.storage.models.title"),
            tr("settings.storage.models.confirm", path=shown),
            tr("settings.storage.models.move"),
            True,
        )
        if move is None:
            return
        self.models_card.set_error(None)
        self._presenter.change_models_dir(target, move=move)


__all__ = ["StorageCard", "StorageSection", "progress_text"]
