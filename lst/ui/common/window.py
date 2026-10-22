"""常驻托盘程序的次要窗口共用的行为（需求 UI-01、UI-04、BI-01）：窗口状态存储、放到屏幕上、
Mica 背景、显示与关闭。

设置窗口、双语窗口、首次运行引导以前各有一份这些代码，回退逻辑也不一致：设置窗口在 Mica 失败时用
主题底色自己绘制背景，引导窗口额外撤掉一次透明属性，双语窗口在深浅色切换后不更新 Mica 的深浅色属性。
现在三个窗口都继承 ``SecondaryWindow``，同一套规则：

- Mica 只在 Windows 平台、首次显示前尝试一次；``enable_mica`` 失败时已经撤掉透明属性，窗口回到不透明背景，
  ``mica_active`` 为假，子类据此决定要不要自己绘制底色；
- 系统或设置里的深浅色变化时，Mica 生效的窗口更新深浅色属性并重绘；
- 显示时位置不在任何屏幕上就移到主屏中央，带 WA_DontShowOnScreen 的窗口（原生平台上的测试）不抢前台；
- 标题栏的关闭按钮与 Alt+F4 只隐藏窗口，模块停止时 ``close_for_good`` 才真正关闭。

窗口状态（位置、大小等）属于界面状态，用 QSettings 写进 ui_state.ini，每个窗口一个组。INI 不记录值的类型，
重新打开文件后读到的都是字符串，所以读取时按字符串和原生类型两种情况解析，解析不了的项当作没有保存过。
"""

from collections.abc import Mapping, Sequence
from typing import Final, cast

from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt
from PySide6.QtGui import QCloseEvent, QGuiApplication, QScreen
from PySide6.QtWidgets import QApplication, QWidget

from lst.ui.theme import enable_mica, is_dark

# 判断标题栏是否可见时，探测点在外框顶边下方多少逻辑像素，大致是标题栏的中线。
_TITLE_PROBE: Final = 12


# ---------------------------------------------------------------- 窗口状态存储


def as_int(value: object) -> int | None:
    """INI 里读到的整数：整数原样返回，字符串按十进制解析，其余（包括布尔值）返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def as_bool(value: object) -> bool | None:
    """INI 里读到的布尔值：接受 true/false/1/0（不分大小写），其余返回 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
    return None


class GroupStore:
    """在 QSettings 的一个组里读写窗口状态。每次写入后立即落盘。子类加上各自窗口特有的项。"""

    def __init__(self, settings: QSettings, group: str) -> None:
        self._settings = settings
        self._group = group

    def value(self, key: str) -> object:
        return self._settings.value(f"{self._group}/{key}")

    def write(self, values: Mapping[str, object]) -> None:
        for key, value in values.items():
            self._settings.setValue(f"{self._group}/{key}", value)
        self._settings.sync()

    def load_geometry(self) -> tuple[QPoint | None, QSize | None]:
        """上次保存的外框左上角与客户区尺寸；没有保存过或解析不了的项为 None。"""
        x, y = as_int(self.value("x")), as_int(self.value("y"))
        width, height = as_int(self.value("width")), as_int(self.value("height"))
        position = QPoint(x, y) if x is not None and y is not None else None
        size = (
            QSize(width, height)
            if width is not None and height is not None and width > 0 and height > 0
            else None
        )
        return position, size

    def save_geometry(self, position: QPoint, size: QSize) -> None:
        self.write({"x": position.x(), "y": position.y(), "width": size.width(), "height": size.height()})


# ---------------------------------------------------------------- 屏幕


def fit_on_screens(frame: QRect, screens: Sequence[QRect], primary: QRect) -> QRect:
    """返回窗口外框应该放在哪里。

    screens 是各块屏幕的可用区域，primary 是主屏的可用区域。判据是标题栏：外框顶边中点下方
    _TITLE_PROBE 像素处落在某块屏幕的可用区域内，用户就能看到并拖动窗口，位置原样保留。
    否则（例如保存位置时所在的显示器已经拔掉）把窗口移到主屏中央；窗口比主屏还大时先缩小到主屏的尺寸。
    """
    probe = QPoint(frame.x() + frame.width() // 2, frame.y() + min(_TITLE_PROBE, frame.height() // 2))
    if any(screen.contains(probe) for screen in screens):
        return QRect(frame)
    size = frame.size().boundedTo(primary.size())
    x = primary.x() + (primary.width() - size.width()) // 2
    y = primary.y() + (primary.height() - size.height()) // 2
    return QRect(QPoint(x, y), size)


def primary_screen() -> QScreen | None:
    """主屏。类型桩把 primaryScreen 标成非空，没有屏幕时（例如显示器全部断开）它返回 None。"""
    return cast(QScreen | None, QGuiApplication.primaryScreen())


def _app_is_dark() -> bool:
    app = QApplication.instance()
    return is_dark(app) if isinstance(app, QApplication) else False


# ---------------------------------------------------------------- 窗口


class SecondaryWindow(QWidget):
    """常驻托盘程序的次要顶层窗口。规则见模块说明。"""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Window)
        # 关闭它不应让程序退出。
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self._allow_close = False
        self._backdrop_tried = False
        self._mica = False
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._scheme_changed)

    @property
    def mica_active(self) -> bool:
        """Mica 背景是否生效。为假时窗口是不透明背景。"""
        return self._mica

    def is_shown(self) -> bool:
        return self.isVisible() and not self.isMinimized()

    def window_geometry(self) -> tuple[QPoint, QSize]:
        """外框左上角与客户区尺寸。"""
        return (self.pos(), self.size())

    def center_on_primary(self) -> None:
        """把窗口移到主屏可用区域的中央。没有屏幕时不动。"""
        primary = primary_screen()
        if primary is not None:
            area = primary.availableGeometry()
            frame = self.frameGeometry()
            self.move(area.center() - frame.center() + frame.topLeft())

    def restore_geometry(self, position: QPoint | None, size: QSize | None) -> None:
        """恢复保存的位置与大小；没有保存位置时放到主屏中央。"""
        if size is not None:
            self.resize(size.expandedTo(self.minimumSize()))
        if position is not None:
            self.move(position)
        else:
            self.center_on_primary()

    def ensure_on_screen(self) -> None:
        """位置不在任何屏幕上时移到主屏中央。"""
        primary = primary_screen()
        if primary is None:
            return
        screens = [screen.availableGeometry() for screen in QGuiApplication.screens()]
        frame = self.frameGeometry()
        target = fit_on_screens(frame, screens, primary.availableGeometry())
        if target.size() != frame.size():
            # 外框与客户区的尺寸差就是边框和标题栏，缩小时保持不变。
            self.resize(self.size() - (frame.size() - target.size()))
        if target.topLeft() != frame.topLeft():
            self.move(target.topLeft())

    def apply_backdrop(self) -> None:
        """首次显示前尝试 Mica 背景（需求 UI-01）。失败时 enable_mica 已经退回不透明背景。"""
        if self._backdrop_tried:
            return
        self._backdrop_tried = True
        if QGuiApplication.platformName() != "windows":
            return
        self._mica = enable_mica(self, dark=_app_is_dark())

    def present_window(self, focus: QWidget | None) -> None:
        """显示并激活窗口，把焦点交给 focus。窗口已经显示时只是带到前面。"""
        self.ensure_on_screen()
        self.apply_backdrop()
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        # 不显示在屏幕上的窗口（WA_DontShowOnScreen，例如原生平台上的测试）不去抢前台。
        if not self.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
            self.raise_()
            self.activateWindow()
        if focus is not None:
            focus.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def conceal(self) -> None:
        self.hide()

    def close_for_good(self) -> None:
        """模块停止时真正关闭窗口；其他情况下关闭按钮只隐藏窗口。"""
        self._allow_close = True
        self.close()

    def close_requested(self) -> None:
        """用户点了标题栏的关闭按钮或按了 Alt+F4。默认只隐藏窗口。"""
        self.hide()

    def _scheme_changed(self, *_args: object) -> None:
        """深浅色变了：Mica 的深浅色属性随之更新，窗口重绘（需求 UI-02）。"""
        if self._mica:
            enable_mica(self, dark=_app_is_dark())
        self.update()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        event.ignore()
        self.close_requested()
