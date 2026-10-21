"""引导是否已经完成的记录（需求 UI-08）。

这是界面状态，与用户配置分开存放：用 QSettings 写进 INI 文件，可以与双语窗口共用同一个 ui_state.ini，
键放在 onboarding 组里，互不冲突。INI 不记录值的类型，重新打开文件后读到的是字符串，所以两种都要认。
"""

from PySide6.QtCore import QSettings

GROUP = "onboarding"
COMPLETED_KEY = f"{GROUP}/completed"


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1")
    return False


class SettingsCompletionStore:
    """在 QSettings 的 onboarding 组里读写“引导已完成”。写入后立即落盘。"""

    def __init__(self, settings: QSettings) -> None:
        self._settings = settings

    def completed(self) -> bool:
        return _as_bool(self._settings.value(COMPLETED_KEY))

    def mark_completed(self) -> None:
        self._settings.setValue(COMPLETED_KEY, True)
        self._settings.sync()
