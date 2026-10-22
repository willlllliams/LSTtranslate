"""设置窗口的状态：位置、大小和上次停留的页面（需求 UI-04，架构文档 11.6.4 节的同一套做法）。

与双语窗口、首次运行引导共用同一个 ui_state.ini，键放在自己的 settings_window 组里，互不冲突。
读写规则见 ``lst.ui.common.window``。
"""

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QSettings, QSize

from lst.ui.common.window import GroupStore, as_int

GROUP = "settings_window"


@dataclass(frozen=True, slots=True)
class WindowState:
    """上次保存的窗口状态。position 是外框左上角，size 是客户区尺寸；没有保存过的项为 None。"""

    position: QPoint | None = None
    size: QSize | None = None
    page: int | None = None


class WindowStateStore(GroupStore):
    """在 QSettings 的 settings_window 组里读写窗口状态。每次写入后立即落盘。"""

    def __init__(self, settings: QSettings) -> None:
        super().__init__(settings, GROUP)

    def load(self) -> WindowState:
        position, size = self.load_geometry()
        return WindowState(position=position, size=size, page=as_int(self.value("page")))

    def save_page(self, page: int) -> None:
        self.write({"page": page})
