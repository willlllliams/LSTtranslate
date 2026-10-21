"""包围盒运算与坐标换算（需求 CAP-05，架构文档 8.4 节）。

产线输出的坐标在“产线看到的图像”里，要换算回原始截图（相对截图左上角的物理像素）。两者之间最多隔着三步：

1. 预处理缩放（CAP-05 的 resize_max_side）：原图 W×H 等比缩小成 w×h 后送入产线；
2. 文档方向校正：PaddleX 用 cv2.getRotationMatrix2D 把输入逆时针旋转 angle 度（0、90、180、270），
   画布随之扩展，旋转后的宽高在 90、270 度时互换；
3. 文档去畸变：UVDoc 输出的图像与输入不再是刚体关系，这里只按两者的尺寸比例缩放，结果是近似值。

CoordinateMap.to_original 按 3 → 2 → 1 的顺序逆向换算。旋转的逆变换由 OpenCV 的旋转矩阵推出：
逆时针旋转 θ 并扩展画布后，原图点 (x, y) 落在
θ=90：(y, w − x)；θ=180：(w − x, h − y)；θ=270：(h − y, x)。
其中 w、h 是旋转前的宽高。反解得到旋转后坐标 (xr, yr) 对应的原图坐标：
θ=90：(w − yr, xr)；θ=180：(w − xr, h − yr)；θ=270：(yr, h − xr)。

像素化（to_pixels）：左上角向下取整、右下角向上取整，再裁剪到截图范围内，保证包围盒盖住原来的区域；
裁剪后面积为零（完全落在截图之外）时返回 None，调用方丢弃这一项。
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

type Box = tuple[float, float, float, float]
"""浮点包围盒 (x0, y0, x1, y1)，左上闭、右下开。"""

type PixelBox = tuple[int, int, int, int]
"""整数像素包围盒，含义同 Box，并且 x0 < x1、y0 < y1。"""

# 浮点误差的容差：2.9999999 不应因为向上取整变成 3 之外的下一个像素。
_EPS = 1e-6


def box_from(values: Sequence[float]) -> Box:
    """把 [x0, y0, x1, y1] 或四个角点 [x0, y0, x1, y1, x2, y2, x3, y3] 转成轴对齐包围盒。"""
    if len(values) == 4:
        x0, y0, x1, y1 = (float(v) for v in values)
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    if len(values) == 8:
        xs = [float(v) for v in values[0::2]]
        ys = [float(v) for v in values[1::2]]
        return (min(xs), min(ys), max(xs), max(ys))
    raise ValueError(f"包围盒应有 4 个或 8 个坐标，实际为 {len(values)} 个")


def box_area(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def intersection_area(a: Box, b: Box) -> float:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, width) * max(0.0, height)


def iou(a: Box, b: Box) -> float:
    """交并比；两个框都没有面积时为 0。"""
    inter = intersection_area(a, b)
    union = box_area(a) + box_area(b) - inter
    return inter / union if union > 0 else 0.0


def union_box(boxes: Iterable[Box]) -> Box | None:
    """包住全部框的最小框；没有框时返回 None。"""
    items = list(boxes)
    if not items:
        return None
    return (
        min(b[0] for b in items),
        min(b[1] for b in items),
        max(b[2] for b in items),
        max(b[3] for b in items),
    )


def plan_resize(width: int, height: int, max_side: int) -> tuple[int, int]:
    """预处理缩放后的尺寸：宽或高超过 max_side 时等比缩小，使长边恰好等于 max_side；max_side 为 0 时不缩放。

    短边按比例四舍五入，至少 1 像素。
    """
    if max_side <= 0 or max(width, height) <= max_side:
        return (width, height)
    if width >= height:
        return (max_side, max(1, round(height * max_side / width)))
    return (max(1, round(width * max_side / height)), max_side)


def _normalize_angle(angle: int | None) -> int:
    """PaddleX 没有做方向分类时 angle 为 -1；其余取值是 0、90、180、270。"""
    if angle is None or angle < 0:
        return 0
    normalized = angle % 360
    if normalized not in (0, 90, 180, 270):
        raise ValueError(f"文档方向角只能是 90 的倍数，实际为 {angle}")
    return normalized


@dataclass(frozen=True, slots=True)
class CoordinateMap:
    """产线输出坐标与原始截图坐标之间的关系，见模块说明。

    original_size：原始截图的宽高；input_size：缩放后送入产线的图像宽高；
    angle：文档方向校正时的旋转角（-1 或 None 表示没有旋转）；
    output_size：产线输出坐标所在图像的宽高，None 表示与旋转后的图像相同（没有去畸变）。
    """

    original_size: tuple[int, int]
    input_size: tuple[int, int]
    angle: int | None = None
    output_size: tuple[int, int] | None = None

    @classmethod
    def identity(cls, width: int, height: int) -> "CoordinateMap":
        return cls((width, height), (width, height))

    def to_original(self, box: Box) -> Box:
        """把产线输出的包围盒换算成原始截图上的浮点包围盒（不取整、不裁剪）。"""
        in_w, in_h = self.input_size
        angle = _normalize_angle(self.angle)
        rot_w, rot_h = (in_h, in_w) if angle in (90, 270) else (in_w, in_h)
        x0, y0, x1, y1 = box
        if self.output_size is not None and self.output_size != (rot_w, rot_h):
            out_w, out_h = self.output_size
            sx, sy = rot_w / max(out_w, 1), rot_h / max(out_h, 1)
            x0, x1, y0, y1 = x0 * sx, x1 * sx, y0 * sy, y1 * sy
        if angle == 90:
            x0, y0, x1, y1 = in_w - y1, x0, in_w - y0, x1
        elif angle == 180:
            x0, y0, x1, y1 = in_w - x1, in_h - y1, in_w - x0, in_h - y0
        elif angle == 270:
            x0, y0, x1, y1 = y0, in_h - x1, y1, in_h - x0
        width, height = self.original_size
        sx, sy = width / max(in_w, 1), height / max(in_h, 1)
        return (x0 * sx, y0 * sy, x1 * sx, y1 * sy)

    def to_pixels(self, box: Box) -> PixelBox | None:
        """换算到原始截图并像素化；裁剪后面积为零时返回 None。"""
        return clamp_to_pixels(self.to_original(box), *self.original_size)


def clamp_to_pixels(box: Box, width: int, height: int) -> PixelBox | None:
    """左上角向下取整、右下角向上取整，再裁剪到 [0, width] × [0, height]；面积为零时返回 None。"""
    x0, y0, x1, y1 = box
    if not all(math.isfinite(v) for v in box):
        return None
    px0 = min(max(math.floor(x0 + _EPS), 0), width)
    py0 = min(max(math.floor(y0 + _EPS), 0), height)
    px1 = min(max(math.ceil(x1 - _EPS), 0), width)
    py1 = min(max(math.ceil(y1 - _EPS), 0), height)
    if px1 <= px0 or py1 <= py0:
        return None
    return (px0, py0, px1, py1)
