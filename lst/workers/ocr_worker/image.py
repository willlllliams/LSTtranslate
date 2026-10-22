"""请求像素的解包与预处理（需求 CAP-05，架构文档 8.1、8.4 节）。

OcrRequest 后面的原始字节帧是 BGRA 像素：每像素 4 字节，每行 stride 字节（stride ≥ width × 4，
行尾可能有对齐填充），共 height 行，帧长恰好是 stride × height。PaddleX 的产线要 BGR 三通道的 uint8 数组，
所以去掉行尾填充和 alpha 通道，复制成连续数组。

预处理只作用于送入 OCR 的图像：灰度化在这里用 numpy 完成（BT.601 系数，与 OpenCV 的 BGR2GRAY 相同的
定点算法），结果仍是三通道，产线的输入格式不变。缩放由后端调用（geometry.plan_resize 给出目标尺寸）：
PaddleOCR 后端用 OpenCV 的面积插值；Windows OCR 与 RapidOCR 后端用这里的 resize_area，它用 numpy 实现
同一种面积平均，单元测试不需要 OpenCV。
"""

from typing import Final

import numpy as np
from numpy.typing import NDArray

type Pixels = NDArray[np.uint8]

# 面积平均缩放时每次处理的条带宽度（像素），限制中间数组的内存。
_RESIZE_STRIP: Final = 1024


class BadImage(ValueError):
    """像素帧与声明的宽、高、跨度不符。"""


def bgra_to_bgr(payload: bytes, width: int, height: int, stride: int) -> Pixels:
    """把 BGRA 像素帧转成 height × width × 3 的连续 BGR 数组；尺寸与帧长不符时抛 BadImage。"""
    if width <= 0 or height <= 0:
        raise BadImage(f"图像宽高必须为正，实际为 {width}×{height}")
    if stride < width * 4:
        raise BadImage(f"stride（{stride}）小于 width × 4（{width * 4}）")
    if len(payload) != stride * height:
        raise BadImage(f"像素帧长 {len(payload)} 字节，应为 stride × height = {stride * height} 字节")
    rows = np.frombuffer(payload, dtype=np.uint8).reshape(height, stride)
    pixels = rows[:, : width * 4].reshape(height, width, 4)
    return np.ascontiguousarray(pixels[:, :, :3])


def to_grayscale(image: Pixels) -> Pixels:
    """灰度化，仍返回三通道 BGR 数组。

    Y = 0.114 B + 0.587 G + 0.299 R，用 14 位定点系数（1868、9617、4899，和为 2^14）四舍五入，
    与 OpenCV 的 cvtColor(BGR2GRAY) 结果一致。
    """
    channels = image.astype(np.uint32)
    gray = (channels[:, :, 0] * 1868 + channels[:, :, 1] * 9617 + channels[:, :, 2] * 4899 + 8192) >> 14
    gray8 = gray.astype(np.uint8)
    return np.ascontiguousarray(np.repeat(gray8[:, :, np.newaxis], 3, axis=2))


def _area_rows(image: NDArray[np.float32], rows: int) -> NDArray[np.float32]:
    """沿第 0 轴把 n 行面积平均成 rows 行（rows ≤ n）。

    把每一列看成分段常数函数 f，积分 F(x) = Σ_{i<⌊x⌋} f_i + (x − ⌊x⌋)·f_{⌊x⌋}；
    第 k 个输出行是 f 在 [k·s, (k+1)·s) 上的平均值 (F((k+1)·s) − F(k·s)) / s，其中 s = n / rows。
    """
    n = image.shape[0]
    scale = n / rows
    edges = np.arange(rows + 1, dtype=np.float64) * scale
    edges[-1] = n
    index = np.minimum(np.floor(edges).astype(np.int64), n - 1)
    frac = (edges - index).astype(np.float32)
    prefix = np.concatenate([np.zeros((1, *image.shape[1:]), np.float32), np.cumsum(image, axis=0)], axis=0)
    shape = (rows + 1,) + (1,) * (image.ndim - 1)
    integral = prefix[index] + frac.reshape(shape) * image[index]
    return (integral[1:] - integral[:-1]) / np.float32(scale)


def resize_area(image: Pixels, size: tuple[int, int]) -> Pixels:
    """把 height × width × c 的 uint8 图像面积平均缩小到 size = (宽, 高)。尺寸不变时原样返回。

    先缩行再缩列，按条带处理，中间数组最多是一个条带的 float32 副本。
    """
    height, width = image.shape[:2]
    new_width, new_height = size
    if (new_width, new_height) == (width, height):
        return image
    if new_width > width or new_height > height or new_width <= 0 or new_height <= 0:
        raise ValueError(f"只能缩小：{width}×{height} → {new_width}×{new_height}")
    rows = np.empty((new_height, width, *image.shape[2:]), np.float32)
    for start in range(0, width, _RESIZE_STRIP):
        strip = image[:, start : start + _RESIZE_STRIP].astype(np.float32)
        rows[:, start : start + _RESIZE_STRIP] = _area_rows(strip, new_height)
    out = np.empty((new_height, new_width, *image.shape[2:]), np.uint8)
    for start in range(0, new_height, _RESIZE_STRIP):
        strip = np.swapaxes(rows[start : start + _RESIZE_STRIP], 0, 1)
        cols = np.swapaxes(_area_rows(np.ascontiguousarray(strip), new_width), 0, 1)
        out[start : start + _RESIZE_STRIP] = np.clip(np.rint(cols), 0, 255).astype(np.uint8)
    return out
