"""双语互译窗口（需求 BI-01 至 BI-07）：展示器加视图，通过事件总线与竞速模块通信。

导入本包时 strings 模块登记文案。
"""

from lst.ui.bilingual.module import BilingualModule, system_clipboard
from lst.ui.bilingual.options import BilingualOptions, parse_options
from lst.ui.bilingual.presenter import BilingualPresenter, EngineRow, SegmentItem
from lst.ui.bilingual.strings import CATALOG, LANGUAGES
from lst.ui.bilingual.window import BilingualWindow

__all__ = [
    "CATALOG",
    "LANGUAGES",
    "BilingualModule",
    "BilingualOptions",
    "BilingualPresenter",
    "BilingualWindow",
    "EngineRow",
    "SegmentItem",
    "parse_options",
    "system_clipboard",
]
