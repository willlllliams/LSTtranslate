"""截屏蒙版（需求 CAP-01 至 CAP-07，架构文档 11.4 节）。

- geometry：逻辑坐标与物理像素的换算，纯函数，不依赖 Qt；
- mask：每块屏幕一个的蒙版窗口（视图）；
- controller：冻结屏幕、框选、取消、发布 CaptureCompleted；
- module：CaptureModule，实现 kernel 的 Module 协议；
- strings：中英文文案，导入时登记到 ui.i18n。
"""

from lst.ui.capture import strings as strings  # 导入时登记文案
from lst.ui.capture.controller import (
    CaptureController,
    CaptureTiming,
    FrozenScreen,
    compose_native,
    describe_screen,
    freeze_screens,
    to_raw_image,
)
from lst.ui.capture.geometry import (
    MIN_SELECTION,
    NativePiece,
    Rect,
    ScreenGeometry,
    Selection,
    anchor_piece,
    clamp_to_screen,
    intersect,
    logical_rect,
    logical_to_pixel,
    native_pieces,
    native_rect,
    native_to_logical,
    pixel_to_logical,
    screen_from_native,
    selection_from_drag,
)
from lst.ui.capture.mask import MaskWindow
from lst.ui.capture.module import CaptureModule

__all__ = [
    "MIN_SELECTION",
    "CaptureController",
    "CaptureModule",
    "CaptureTiming",
    "FrozenScreen",
    "MaskWindow",
    "NativePiece",
    "Rect",
    "ScreenGeometry",
    "Selection",
    "anchor_piece",
    "clamp_to_screen",
    "compose_native",
    "describe_screen",
    "freeze_screens",
    "intersect",
    "logical_rect",
    "logical_to_pixel",
    "native_pieces",
    "native_rect",
    "native_to_logical",
    "pixel_to_logical",
    "screen_from_native",
    "selection_from_drag",
    "to_raw_image",
]
