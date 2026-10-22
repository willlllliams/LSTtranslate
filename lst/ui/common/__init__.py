"""界面模块的共用部分（与 ui.theme、ui.i18n 同属共用模块）：文案与格式化、模型行状态、次要窗口的公共行为、
界面模块之间的消息。

本包只依赖 contracts、ui.theme 与 ui.i18n，不依赖任何一个界面模块。__init__ 只导出不依赖 Qt 的部分，
次要窗口的基类在 ``lst.ui.common.window``。
"""

from lst.ui.common.messages import HotkeyRecordingChanged
from lst.ui.common.models import (
    ACTIVE_STATES,
    CANCELLABLE_STATES,
    DOWNLOADABLE_STATES,
    RowState,
    download_percent,
    model_status_text,
    model_title,
    row_state,
)
from lst.ui.common.text import (
    LANGUAGE_NAMES,
    LANGUAGES,
    PROGRESS_STEPS,
    clamp_fraction,
    format_size,
    language_name,
    model_error_reason,
    progress_steps,
)

__all__ = [
    "ACTIVE_STATES",
    "CANCELLABLE_STATES",
    "DOWNLOADABLE_STATES",
    "LANGUAGES",
    "LANGUAGE_NAMES",
    "PROGRESS_STEPS",
    "HotkeyRecordingChanged",
    "RowState",
    "clamp_fraction",
    "download_percent",
    "format_size",
    "language_name",
    "model_error_reason",
    "model_status_text",
    "model_title",
    "progress_steps",
    "row_state",
]
