"""自动化接口对窗口的操作：截屏蒙版、覆盖窗口、设置窗口与它们的画面（需求 DEV-05、DEV-06，架构文档 16.4 节）。

这里只读窗口公开的查询（覆盖窗口的 visible_items、selection_ranges 等），操作要么调用窗口自己的方法
（与展示器、测试调用的相同），要么用 ``lst.ui.common.inject`` 合成事件交给窗口的事件处理。
"""

import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import shiboken6
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from lst.app.automation.server import MethodError
from lst.automation.protocol import ErrorCode, Json
from lst.contracts.ids import SegmentId
from lst.ui.capture import MaskWindow, Rect, native_rect, native_to_logical
from lst.ui.overlay import OverlayWindow
from lst.ui.theme import theme_colors

GRAB_NAME: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
"""grab 的 name 参数：只能是字母、数字、下划线与连字符，最终文件名由程序拼出，不接受路径。"""


def alive(widget: QWidget | None) -> bool:
    return widget is not None and shiboken6.isValid(widget)


# ---------------------------------------------------------------- 覆盖窗口


def overlay_state(window: OverlayWindow | None) -> dict[str, Json]:
    """覆盖窗口当前的状态。没有窗口时 open 为假。

    items 里每段给出序号、段号、当前显示的文字与它在窗口里的逻辑坐标矩形 rect（x, y, 宽, 高），
    select_at 与 mouse 的坐标按这个矩形算。
    """
    if window is None or not alive(window):
        return {"open": False}
    scale = window.scale_logical()
    items: list[Json] = []
    for index, item in enumerate(window.visible_items()):
        box = item.bbox
        items.append(
            {
                "index": index,
                "id": item.segment_id,
                "shown": window.shown_text(item),
                "font_px": window.font_px(item),
                "rect": [
                    round(box.x0 * scale, 2),
                    round(box.y0 * scale, 2),
                    round(box.width * scale, 2),
                    round(box.height * scale, 2),
                ],
            }
        )
    ranges = window.selection_ranges()
    geometry = window.geometry()
    progress = window.progress
    return {
        "open": True,
        "visible": window.isVisible(),
        "phase": window.phase.value,
        "status_text": window.status_text,
        "detail_text": window.detail_text,
        "progress": None if progress is None else list(progress),
        "showing": "source" if window.showing_source else "translation",
        "pinned": window.pinned,
        "zoom": round(window.zoom, 4),
        "geometry": [geometry.x(), geometry.y(), geometry.width(), geometry.height()],
        "device_ratio": window.device_ratio(),
        "fitting_pending": window.fitting_pending,
        "items": items,
        "selection": {
            "ranges": {segment: list(span) for segment, span in ranges.items()},
            "text": window.selected_text(),
            "unit": window.selection_unit,
        },
    }


def resolve_position(window: OverlayWindow, value: Json, name: str) -> tuple[SegmentId, int]:
    """[段, 字符位置] → 文字位置。段可以是 visible_items 里的序号，也可以是段号字符串。"""
    if not isinstance(value, list) or len(value) != 2:
        raise MethodError(ErrorCode.INVALID_PARAMS, f"{name} 应当是 [段序号或段号, 字符位置]")
    segment, offset = value
    items = window.visible_items()
    if isinstance(segment, bool) or not isinstance(offset, int) or isinstance(offset, bool):
        raise MethodError(ErrorCode.INVALID_PARAMS, f"{name} 的字符位置应当是整数")
    if isinstance(segment, int):
        if not 0 <= segment < len(items):
            raise MethodError(
                ErrorCode.INVALID_PARAMS, f"{name} 的段序号 {segment} 超出范围 0–{len(items) - 1}"
            )
        item = items[segment]
    elif isinstance(segment, str):
        item = next((i for i in items if i.segment_id == segment), None)
        if item is None:
            raise MethodError(ErrorCode.INVALID_PARAMS, f"{name} 的段号 {segment} 不在当前显示的段里")
    else:
        raise MethodError(ErrorCode.INVALID_PARAMS, f"{name} 的段应当是序号或段号")
    length = len(window.shown_text(item))
    if not 0 <= offset <= length:
        raise MethodError(ErrorCode.INVALID_PARAMS, f"{name} 的字符位置 {offset} 超出范围 0–{length}")
    return item.segment_id, offset


# ---------------------------------------------------------------- 截屏蒙版


def mask_for_point(masks: Sequence[MaskWindow], x: int, y: int) -> MaskWindow | None:
    """原生坐标 (x, y) 所在屏幕的蒙版。"""
    for mask in masks:
        bounds = native_rect(mask.screen_geometry)
        if bounds.x <= x < bounds.right and bounds.y <= y < bounds.bottom:
            return mask
    return None


def mask_local(mask: MaskWindow, x: float, y: float) -> tuple[float, float]:
    """原生坐标 → 蒙版窗口内的逻辑坐标（窗口铺满屏幕，左上角是屏幕的逻辑左上角）。"""
    screen = mask.screen_geometry
    lx, ly = native_to_logical(screen, x, y)
    return lx - screen.logical.x, ly - screen.logical.y


def describe_masks(masks: Sequence[MaskWindow]) -> list[Json]:
    out: list[Json] = []
    for mask in masks:
        screen = mask.screen_geometry
        selection = mask.selection
        out.append(
            {
                "screen": screen.name,
                "native": list(native_rect(screen).as_tuple()),
                "dpr": screen.dpr,
                "selection": None if selection is None else list(selection.pixels.as_tuple()),
                "visible": mask.isVisible(),
            }
        )
    return out


def live_screens() -> list[Json]:
    """真实屏幕的原生矩形与设备像素比，按 Qt 的规则由逻辑矩形换算（左上角不变，宽高乘设备像素比）。"""
    screens: list[Json] = []
    for screen in QGuiApplication.screens():
        rect, dpr = screen.geometry(), screen.devicePixelRatio()
        native = Rect(rect.x(), rect.y(), round(rect.width() * dpr), round(rect.height() * dpr))
        screens.append({"name": screen.name(), "native": list(native.as_tuple()), "dpr": dpr})
    return screens


def parse_rect(value: Json) -> Rect:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    ):
        raise MethodError(ErrorCode.INVALID_PARAMS, "rect 应当是 [x, y, 宽, 高]，原生坐标的整数物理像素")
    x, y, width, height = (int(v) for v in value if isinstance(v, int))
    if width < 1 or height < 1:
        raise MethodError(ErrorCode.INVALID_PARAMS, "rect 的宽高至少为 1")
    return Rect(x, y, width, height)


def parse_point(value: Json, name: str) -> tuple[float, float]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value)
    ):
        raise MethodError(ErrorCode.INVALID_PARAMS, f"{name} 应当是 [x, y]")
    x, y = (float(v) for v in value if isinstance(v, int | float))
    return x, y


# ---------------------------------------------------------------- 画面


def widget_image(widget: QWidget) -> QPixmap:
    """窗口当前的画面。背景透明的窗口（设置窗口的 Mica 背景由系统绘制，不在 grab 的结果里）
    垫上主题的窗口底色，否则深色主题下的白字落在透明背景上，存出来的图片看不清。"""
    pixmap = widget.grab()
    if not widget.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground):
        return pixmap
    base = QPixmap(pixmap.size())
    base.setDevicePixelRatio(pixmap.devicePixelRatio())
    base.fill(theme_colors().window)
    painter = QPainter(base)
    try:
        painter.drawPixmap(QPoint(0, 0), pixmap)
    finally:
        painter.end()
    return base


def grab_widgets(widgets: Mapping[str, QWidget], directory: Path, name: str | None) -> list[Json]:
    """把窗口当前的画面存成 PNG，文件放在 directory 下，返回各文件的路径与像素尺寸。"""
    if name is not None and GRAB_NAME.fullmatch(name) is None:
        raise MethodError(ErrorCode.INVALID_PARAMS, "name 只能包含字母、数字、下划线与连字符，最长 64 个字符")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    saved: list[Json] = []
    for label, widget in widgets.items():
        parts = [label, stamp, *([name] if name else [])]
        path = directory / ("-".join(parts) + ".png")
        pixmap = widget_image(widget)
        if pixmap.isNull() or not pixmap.save(str(path), "PNG"):
            raise MethodError(ErrorCode.INTERNAL, f"{label} 的画面保存失败")
        saved.append({"target": label, "path": str(path), "size": [pixmap.width(), pixmap.height()]})
    return saved


__all__ = [
    "GRAB_NAME",
    "alive",
    "describe_masks",
    "grab_widgets",
    "live_screens",
    "mask_for_point",
    "mask_local",
    "overlay_state",
    "parse_point",
    "parse_rect",
    "resolve_position",
]
