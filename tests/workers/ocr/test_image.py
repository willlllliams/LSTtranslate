"""BGRA 像素帧解包（含行尾填充）与灰度化。"""

import numpy as np
import pytest
from numpy.typing import NDArray

from lst.workers.ocr_worker.image import BadImage, bgra_to_bgr, to_grayscale
from tests.workers.ocr.harness import pad_rows


def make_bgra(width: int, height: int) -> NDArray[np.uint8]:
    """每个像素的 B、G、R、A 都由坐标决定，便于逐像素核对。"""
    ys, xs = np.mgrid[0:height, 0:width]
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[:, :, 0] = (xs * 7 + ys) % 256
    pixels[:, :, 1] = (ys * 5 + 3) % 256
    pixels[:, :, 2] = (xs + ys * 11) % 256
    pixels[:, :, 3] = 200
    return pixels


@pytest.mark.parametrize("padding", [0, 4, 12, 60])
def test_stride_padding_is_dropped(padding: int) -> None:
    width, height = 37, 23
    bgra = make_bgra(width, height)
    payload, stride = pad_rows(bgra.tobytes(), width, height, padding)
    # 填充字节写成 0xEE，若被当成像素会立刻暴露。
    frame = bytearray(payload)
    for y in range(height):
        frame[y * stride + width * 4 : (y + 1) * stride] = b"\xee" * padding
    bgr = bgra_to_bgr(bytes(frame), width, height, stride)
    assert bgr.shape == (height, width, 3)
    assert bgr.dtype == np.uint8
    assert bgr.flags["C_CONTIGUOUS"]
    np.testing.assert_array_equal(bgr, bgra[:, :, :3])


@pytest.mark.parametrize(
    ("width", "height", "stride", "length"),
    [
        (10, 10, 40, 399),  # 少一个字节
        (10, 10, 40, 401),  # 多一个字节
        (10, 10, 44, 400),  # 声明了填充但帧里没有
        (10, 10, 36, 360),  # stride 小于 width × 4
        (0, 10, 0, 0),
        (10, -1, 40, 0),
    ],
)
def test_frame_size_mismatch(width: int, height: int, stride: int, length: int) -> None:
    with pytest.raises(BadImage):
        bgra_to_bgr(b"\0" * length, width, height, stride)


def test_grayscale_matches_bt601_and_keeps_three_channels() -> None:
    bgr = make_bgra(64, 48)[:, :, :3].copy()
    gray = to_grayscale(bgr)
    assert gray.shape == bgr.shape
    assert gray.flags["C_CONTIGUOUS"]
    np.testing.assert_array_equal(gray[:, :, 0], gray[:, :, 1])
    np.testing.assert_array_equal(gray[:, :, 0], gray[:, :, 2])
    b, g, r = (bgr[:, :, i].astype(float) for i in range(3))
    expected = 0.114 * b + 0.587 * g + 0.299 * r
    # 定点系数与浮点系数之差最多带来约 0.013 的偏差，再加上四舍五入的 0.5。
    assert np.abs(gray[:, :, 0].astype(float) - expected).max() <= 0.52


def test_grayscale_extremes() -> None:
    white = np.full((2, 2, 3), 255, dtype=np.uint8)
    black = np.zeros((2, 2, 3), dtype=np.uint8)
    assert (to_grayscale(white) == 255).all()
    assert (to_grayscale(black) == 0).all()
