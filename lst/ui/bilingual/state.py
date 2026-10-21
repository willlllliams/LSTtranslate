"""窗口状态的保存与恢复（需求 BI-01，架构文档 11.6 节）。

位置、大小、置顶状态和手动选择的语言属于界面状态，用 QSettings 存进单独的 INI 文件，与用户配置分开。
INI 不记录值的类型，重新打开文件后读到的都是字符串，所以读取时按字符串和原生类型两种情况解析，
解析不了的项当作没有保存过。
"""

from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QSettings, QSize

from lst.contracts.ids import Lang
from lst.ui.bilingual.strings import LANGUAGES

GROUP = "bilingual_window"

# 判断标题栏是否可见时，探测点在外框顶边下方多少逻辑像素，大致是标题栏的中线。
_TITLE_PROBE = 12


@dataclass(frozen=True, slots=True)
class WindowState:
    """上次保存的窗口状态。position 是外框左上角，size 是客户区尺寸；没有保存过的项为 None。"""

    position: QPoint | None = None
    size: QSize | None = None
    on_top: bool = False
    source: Lang | None = None
    target: Lang | None = None


def _as_int(value: object) -> int | None:
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


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
    return None


def _as_lang(value: object) -> Lang | None:
    """只接受语言列表里的代码；空串、未知代码和其他类型都表示“自动”。"""
    if isinstance(value, str) and value in LANGUAGES:
        return Lang(value)
    return None


class WindowStateStore:
    """在 QSettings 的 bilingual_window 组里读写窗口状态。每次写入后立即落盘。"""

    def __init__(self, settings: QSettings) -> None:
        self._settings = settings

    def _value(self, key: str) -> object:
        return self._settings.value(f"{GROUP}/{key}")

    def _write(self, values: dict[str, object]) -> None:
        for key, value in values.items():
            self._settings.setValue(f"{GROUP}/{key}", value)
        self._settings.sync()

    def load(self) -> WindowState:
        x, y = _as_int(self._value("x")), _as_int(self._value("y"))
        width, height = _as_int(self._value("width")), _as_int(self._value("height"))
        position = QPoint(x, y) if x is not None and y is not None else None
        size = (
            QSize(width, height)
            if width is not None and height is not None and width > 0 and height > 0
            else None
        )
        return WindowState(
            position=position,
            size=size,
            on_top=_as_bool(self._value("on_top")) or False,
            source=_as_lang(self._value("source")),
            target=_as_lang(self._value("target")),
        )

    def save_geometry(self, position: QPoint, size: QSize) -> None:
        self._write({"x": position.x(), "y": position.y(), "width": size.width(), "height": size.height()})

    def save_on_top(self, on_top: bool) -> None:
        self._write({"on_top": on_top})

    def save_languages(self, source: Lang | None, target: Lang | None) -> None:
        self._write({"source": source or "", "target": target or ""})


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
