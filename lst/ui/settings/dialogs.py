"""设置窗口里会弹出的模态对话框：确认删除、选择目录。

视图通过 ``Dialogs`` 协议使用它们，测试换成记录调用、直接给出结果的假实现，不会真的弹出窗口。
"""

from typing import Protocol

from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from lst.ui.i18n import tr


class Dialogs(Protocol):
    """视图需要的对话框。"""

    def confirm(self, title: str, text: str) -> bool:
        """询问用户是否确认一个不可恢复的操作，默认答案是“否”。"""
        ...

    def choose_directory(self, title: str, start: str) -> str | None:
        """选择目录，用户取消时返回 None。"""
        ...


class QtDialogs:
    """用 QMessageBox 与 QFileDialog 实现的对话框，以设置窗口为父窗口。"""

    def __init__(self) -> None:
        self.parent: QWidget | None = None

    def confirm(self, title: str, text: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=self.parent)
        yes = box.addButton(tr("settings.confirm.yes"), QMessageBox.ButtonRole.YesRole)
        no = box.addButton(tr("settings.confirm.no"), QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.exec()
        return box.clickedButton() is yes

    def choose_directory(self, title: str, start: str) -> str | None:
        chosen = QFileDialog.getExistingDirectory(self.parent, title, start)
        return chosen or None
