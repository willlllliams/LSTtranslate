"""识别框与绘制框之间的布局参数和截图像素几何，保留原始 OCR 坐标。"""

import math
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lst.contracts.ocr import BBox


class OverlayLayoutConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    growth: Literal["fixed", "vertical", "both"] = Field(
        default="both",
        title="自动扩框",
        description="固定框；保留横向范围并扩高；或同时扩宽和扩高。优先保留原文字号。",
    )
    scale_x: float = Field(
        default=1.0, ge=1, le=4, title="框宽度倍数", description="绘制框相对原框的宽度倍数。"
    )
    scale_y: float = Field(
        default=1.0, ge=1, le=4, title="框高度倍数", description="绘制框相对原框的高度倍数。"
    )
    box_padding_x: float = Field(
        default=0,
        ge=0,
        le=200,
        title="水平外扩",
        description="绘制框左右各增加的截图像素。",
        json_schema_extra={"advanced": True},
    )
    box_padding_y: float = Field(
        default=0,
        ge=0,
        le=200,
        title="垂直外扩",
        description="绘制框上下各增加的截图像素。",
        json_schema_extra={"advanced": True},
    )
    font_scale: float = Field(
        default=1.1,
        ge=0.5,
        le=3,
        title="目标字号倍数",
        description="相对原文估算字号的倍数；大于 1 放大文字，空间不足时先扩框。",
    )
    horizontal: Literal["left", "center", "right"] = Field(
        default="left", title="水平对齐", description="文字的水平对齐方式，同时决定扩宽时保留的锚点。"
    )
    vertical: Literal["top", "center", "bottom"] = Field(
        default="center", title="垂直对齐", description="顶部向下扩高，居中向两侧扩高，底部向上扩高。"
    )
    text_padding_x: float = Field(
        default=2,
        ge=0,
        le=50,
        title="水平内边距",
        description="文字到绘制框左右边缘的截图像素，可设为 0。",
        json_schema_extra={"advanced": True},
    )
    text_padding_y: float = Field(
        default=1,
        ge=0,
        le=50,
        title="垂直内边距",
        description="文字到绘制框上下边缘的截图像素，可设为 0。",
        json_schema_extra={"advanced": True},
    )
    max_growth: float = Field(
        default=4,
        ge=1,
        le=8,
        title="自动扩框上限",
        description="自动扩宽或扩高相对手动调整后框大小的最大倍数。",
        json_schema_extra={"advanced": True},
    )
    avoid_overlap: bool = Field(
        default=True,
        title="避让相邻内容",
        description="扩框在相邻识别框之间分配空白，避让已识别图片、表格和公式。",
        json_schema_extra={"advanced": True},
    )


CONFIG_SECTIONS: dict[str, type[BaseModel]] = {"overlay_layout": OverlayLayoutConfig}


def sized_box(source: BBox, width: float, height: float, config: OverlayLayoutConfig) -> BBox:
    """在对齐锚点上调整尺寸；左右和上下外扩已计入调用方的目标宽高。"""
    ax = {"left": 0.0, "center": 0.5, "right": 1.0}[config.horizontal]
    ay = {"top": 0.0, "center": 0.5, "bottom": 1.0}[config.vertical]
    x = source.x0 + (source.width - width) * ax
    y = source.y0 + (source.height - height) * ay
    return BBox(math.floor(x), math.floor(y), math.ceil(x + width), math.ceil(y + height))


def overlaps(a: BBox, b: BBox) -> bool:
    return min(a.x1, b.x1) > max(a.x0, b.x0) and min(a.y1, b.y1) > max(a.y0, b.y0)


def confine_box(source: BBox, desired: BBox, size: tuple[int, int], obstacles: Sequence[BBox]) -> BBox:
    """按截图边界裁剪，在间隙或轻微重叠的中线分配空间；较大原始重叠留给版面结构处理。"""
    width, height = size
    box = BBox(max(0, desired.x0), max(0, desired.y0), min(width, desired.x1), min(height, desired.y1))
    for other in obstacles:
        if not overlaps(box, other):
            continue
        if overlaps(source, other):
            # 行框外扩会让相邻行重叠几个像素；在重叠带中间分界，保留原框作为字号依据。
            overlap_y = min(source.y1, other.y1) - max(source.y0, other.y0)
            overlap_x = min(source.x1, other.x1) - max(source.x0, other.x0)
            if 0 < overlap_y <= 0.35 * min(source.height, other.height):
                if source.y0 < other.y0:
                    box = BBox(box.x0, box.y0, box.x1, min(box.y1, math.floor((source.y1 + other.y0) / 2)))
                else:
                    box = BBox(box.x0, max(box.y0, math.ceil((other.y1 + source.y0) / 2)), box.x1, box.y1)
            elif 0 < overlap_x <= 0.2 * min(source.width, other.width):
                if source.x0 < other.x0:
                    box = BBox(box.x0, box.y0, min(box.x1, math.floor((source.x1 + other.x0) / 2)), box.y1)
                else:
                    box = BBox(max(box.x0, math.ceil((other.x1 + source.x0) / 2)), box.y0, box.x1, box.y1)
            continue
        candidates: list[BBox] = []
        if other.x1 <= source.x0:
            candidates.append(
                BBox(max(box.x0, math.ceil((other.x1 + source.x0) / 2)), box.y0, box.x1, box.y1)
            )
        if other.x0 >= source.x1:
            candidates.append(
                BBox(box.x0, box.y0, min(box.x1, math.floor((source.x1 + other.x0) / 2)), box.y1)
            )
        if other.y1 <= source.y0:
            candidates.append(
                BBox(box.x0, max(box.y0, math.ceil((other.y1 + source.y0) / 2)), box.x1, box.y1)
            )
        if other.y0 >= source.y1:
            candidates.append(
                BBox(box.x0, box.y0, box.x1, min(box.y1, math.floor((source.y1 + other.y0) / 2)))
            )
        if candidates:
            box = max(candidates, key=lambda b: b.width * b.height)
    return box
