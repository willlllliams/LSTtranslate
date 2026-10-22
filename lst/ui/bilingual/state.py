"""双语窗口状态的保存与恢复（需求 BI-01，架构文档 11.6 节）。

位置、大小、置顶状态和手动选择的语言属于界面状态，存进 ui_state.ini 的 bilingual_window 组，与用户配置分开。
读写规则见 ``lst.ui.common.window``。
"""

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QSettings, QSize

from lst.contracts.ids import Lang
from lst.ui.common.text import LANGUAGES
from lst.ui.common.window import GroupStore, as_bool

GROUP = "bilingual_window"


@dataclass(frozen=True, slots=True)
class WindowState:
    """上次保存的窗口状态。position 是外框左上角，size 是客户区尺寸；没有保存过的项为 None。"""

    position: QPoint | None = None
    size: QSize | None = None
    on_top: bool = False
    source: Lang | None = None
    target: Lang | None = None


def _as_lang(value: object) -> Lang | None:
    """只接受语言列表里的代码；空串、未知代码和其他类型都表示“自动”。"""
    if isinstance(value, str) and value in LANGUAGES:
        return Lang(value)
    return None


class WindowStateStore(GroupStore):
    """在 QSettings 的 bilingual_window 组里读写窗口状态。每次写入后立即落盘。"""

    def __init__(self, settings: QSettings) -> None:
        super().__init__(settings, GROUP)

    def load(self) -> WindowState:
        position, size = self.load_geometry()
        return WindowState(
            position=position,
            size=size,
            on_top=as_bool(self.value("on_top")) or False,
            source=_as_lang(self.value("source")),
            target=_as_lang(self.value("target")),
        )

    def save_on_top(self, on_top: bool) -> None:
        self.write({"on_top": on_top})

    def save_languages(self, source: Lang | None, target: Lang | None) -> None:
        self.write({"source": source or "", "target": target or ""})
