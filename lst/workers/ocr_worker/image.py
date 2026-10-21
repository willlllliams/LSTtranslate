"""请求像素的解包与预处理（需求 CAP-05，架构文档 8.1、8.4 节）。

OcrRequest 后面的原始字节帧是 BGRA 像素：每像素 4 字节，每行 stride 字节（stride ≥ width × 4，
行尾可能有对齐填充），共 height 行，帧长恰好是 stride × height。PaddleX 的产线要 BGR 三通道的 uint8 数组，
所以去掉行尾填充和 alpha 通道，复制成连续数组。

预处理只作用于送入 OCR 的图像：灰度化在这里用 numpy 完成（BT.601 系数，与 OpenCV 的 BGR2GRAY 相同的
定点算法），结果仍是三通道，产线的输入格式不变；缩放需要 OpenCV 的面积插值，由后端调用（geometry.plan_resize
给出目标尺寸）。
"""

import numpy as np
from numpy.typing import NDArray

type Pixels = NDArray[np.uint8]


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
