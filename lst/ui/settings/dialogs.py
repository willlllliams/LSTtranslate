"""设置窗口里会弹出的模态对话框与外部程序：确认删除、带一个选项的确认、选择目录、在浏览器里打开网页、
在资源管理器里打开目录。

视图通过 ``Dialogs`` 协议使用它们，测试换成记录调用、直接给出结果的假实现，不会真的弹出窗口，
也不会打开浏览器或资源管理器。
"""

from typing import Protocol

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QFileDialog, QMessageBox, QWidget

from lst.ui.i18n import tr


class Dialogs(Protocol):
    """视图需要的对话框。"""

    def confirm(self, title: str, text: str) -> bool:
        """询问用户是否确认一个不可恢复的操作，默认答案是“否”。"""
        ...

    def confirm_option(self, title: str, text: str, option: str, checked: bool) -> bool | None:
        """询问是否继续，对话框里带一个复选框（option 是它的文字，checked 是初始状态）。

        用户取消时返回 None，确认时返回复选框的状态。
        """
        ...

    def choose_directory(self, title: str, start: str) -> str | None:
        """选择目录，用户取消时返回 None。"""
        ...

    def open_path(self, path: str) -> bool:
        """在资源管理器里打开一个本地目录，返回是否交给了系统。"""
        ...

    def open_url(self, url: str) -> bool:
        """用系统默认浏览器打开网页（例如版本的发布说明），返回是否成功交给了浏览器。"""
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

    def confirm_option(self, title: str, text: str, option: str, checked: bool) -> bool | None:
        box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=self.parent)
        box.setCheckBox(QCheckBox(option))
        check = box.checkBox()
        check.setChecked(checked)
        yes = box.addButton(tr("settings.confirm.continue"), QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton(tr("settings.confirm.cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.exec()
        return check.isChecked() if box.clickedButton() is yes else None

    def choose_directory(self, title: str, start: str) -> str | None:
        chosen = QFileDialog.getExistingDirectory(self.parent, title, start)
        return chosen or None

    def open_path(self, path: str) -> bool:
        """只打开已经存在的本地目录。"""
        target = QUrl.fromLocalFile(path)
        if not path or not target.isValid():
            return False
        return QDesktopServices.openUrl(target)

    def open_url(self, url: str) -> bool:
        """只打开 https 网址：网址来自发布信息，其他协议（file、ms-settings 等）一律不交给系统。"""
        target = QUrl(url)
        if not target.isValid() or target.scheme() != "https":
            return False
        return QDesktopServices.openUrl(target)
