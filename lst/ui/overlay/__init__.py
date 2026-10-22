"""截屏翻译覆盖窗口（需求 OV-01 至 OV-07，架构文档 11.5 节）。

- geometry：截图坐标、窗口逻辑坐标与设备像素之间的换算，缩放与单击判断，纯函数；
- fitting：字号适配的二分查找与估算，纯函数；
- window：覆盖窗口（视图），自己绘制底图、版面块与译文；
- presenter：订阅总线消息、维护当前会话、驱动窗口；
- module：OverlayModule，实现 kernel 的 Module 协议；
- strings：中英文文案，导入时登记到 ui.i18n。
"""

from lst.ui.overlay import strings as strings  # 导入时登记文案
from lst.ui.overlay.fitting import MIN_FONT_PX, char_units, estimate_font_size, fit_font_size
from lst.ui.overlay.module import OverlayModule, system_clipboard
from lst.ui.overlay.presenter import OverlayPresenter, overlay_markers
from lst.ui.overlay.window import OverlayWindow, Phase, SegmentItem, chart_cell_bbox, tooltip_text

__all__ = [
    "MIN_FONT_PX",
    "OverlayModule",
    "OverlayPresenter",
    "OverlayWindow",
    "Phase",
    "SegmentItem",
    "char_units",
    "chart_cell_bbox",
    "estimate_font_size",
    "fit_font_size",
    "overlay_markers",
    "system_clipboard",
    "tooltip_text",
]
