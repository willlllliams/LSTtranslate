"""截图缩小与 PNG 编码的测试（需求 ENG-07）。

PNG 用测试里独立实现的解码器还原成像素再比较：解析分块、校验 CRC、解压、撤销逐行滤波。
"""

import hashlib
import struct
import zlib

import numpy as np
import pytest
from numpy.typing import NDArray

from lst.contracts.ocr import RawImage
from lst.contracts.translation import PNG_MEDIA_TYPE
from lst.services.race.screenshot import encode_png, encode_screenshot, scaled_size


def decode_png(data: bytes) -> NDArray[np.uint8]:
    """解码 8 位 RGB、不隔行的 PNG，支持全部五种滤波类型。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, header = 8, b"", b""
    kinds: list[bytes] = []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + payload)
        kinds.append(kind)
        if kind == b"IHDR":
            header = payload
        elif kind == b"IDAT":
            idat += payload
        pos += 12 + length
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND"
    width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", header)
    assert (depth, color, interlace) == (8, 2, 0)
    raw = zlib.decompress(idat)
    stride = width * 3
    assert len(raw) == height * (stride + 1)
    out = np.zeros((height, stride), dtype=np.int32)
    for y in range(height):
        kind = raw[y * (stride + 1)]
        line = np.frombuffer(raw, np.uint8, stride, y * (stride + 1) + 1).astype(np.int32)
        prior = out[y - 1] if y else np.zeros(stride, np.int32)
        if kind == 0:
            out[y] = line
        elif kind == 2:
            out[y] = (line + prior) % 256
        else:
            # Sub、Average、Paeth 需要逐字节依赖左侧像素；本实现只产出 None 与 Up。
            raise AssertionError(f"意外的滤波类型 {kind}")
    return out.astype(np.uint8).reshape(height, width, 3)


def raw_image(rgb: NDArray[np.uint8], *, padding: int = 0) -> RawImage:
    """把 RGB 数组包装成 BGRA 的 RawImage，每行末尾可以补 padding 个字节。"""
    height, width, _ = rgb.shape
    bgra = np.zeros((height, width * 4 + padding), dtype=np.uint8)
    pixels = bgra[:, : width * 4].reshape(height, width, 4)
    pixels[:, :, 0] = rgb[:, :, 2]
    pixels[:, :, 1] = rgb[:, :, 1]
    pixels[:, :, 2] = rgb[:, :, 0]
    pixels[:, :, 3] = 255
    bgra[:, width * 4 :] = 0xEE  # 填充字节不能进入输出
    return RawImage(width, height, width * 4 + padding, bgra.tobytes())


@pytest.mark.parametrize(
    ("size", "max_side", "expected"),
    [
        ((800, 600), 1568, (800, 600)),
        ((1568, 10), 1568, (1568, 10)),
        ((3840, 2160), 1568, (1568, 882)),
        ((1000, 4000), 1000, (250, 1000)),
        ((5000, 2), 100, (100, 1)),  # 每边至少 1 像素
    ],
)
def test_scaled_size(size: tuple[int, int], max_side: int, expected: tuple[int, int]) -> None:
    assert scaled_size(*size, max_side) == expected


def test_scaled_size_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        scaled_size(10, 10, 0)


def test_png_round_trip() -> None:
    rng = np.random.default_rng(1)
    rgb = rng.integers(0, 256, size=(7, 5, 3), dtype=np.uint8)
    assert np.array_equal(decode_png(encode_png(rgb)), rgb)


def test_small_image_keeps_pixels_and_drops_padding() -> None:
    rng = np.random.default_rng(2)
    rgb = rng.integers(0, 256, size=(6, 9, 3), dtype=np.uint8)
    encoded = encode_screenshot(raw_image(rgb, padding=12), 1568)
    assert (encoded.width, encoded.height, encoded.media_type) == (9, 6, PNG_MEDIA_TYPE)
    assert np.array_equal(decode_png(encoded.data), rgb)  # BGRA → RGB 的通道顺序正确
    assert encoded.sha256 == hashlib.sha256(encoded.data).hexdigest()


def test_integer_factor_is_block_average() -> None:
    rng = np.random.default_rng(3)
    rgb = rng.integers(0, 256, size=(8, 12, 3), dtype=np.uint8)
    encoded = encode_screenshot(raw_image(rgb), 6)
    assert (encoded.width, encoded.height) == (6, 4)
    expected = rgb.astype(np.float64).reshape(4, 2, 6, 2, 3).mean(axis=(1, 3))
    decoded = decode_png(encoded.data).astype(np.float64)
    assert np.abs(decoded - expected).max() <= 0.5 + 1e-6


def test_fractional_factor_weights_partial_pixels() -> None:
    # 一行 3 个像素缩成 2 个：第一个输出覆盖 [0, 1.5)，即像素 0 全部加像素 1 的一半。
    rgb = np.array([[[0, 0, 0], [90, 90, 90], [210, 210, 210]]], dtype=np.uint8)
    encoded = encode_screenshot(raw_image(rgb), 2)
    assert (encoded.width, encoded.height) == (2, 1)
    decoded = decode_png(encoded.data)[0, :, 0].tolist()
    assert decoded == [round((0 + 45) / 1.5), round((45 + 210) / 1.5)]


def test_large_screenshot_is_scaled_to_limit() -> None:
    rgb = np.full((1080, 1920, 3), 255, dtype=np.uint8)
    rgb[::9] = 0  # 横线，模拟文字行
    encoded = encode_screenshot(raw_image(rgb), 1568)
    assert (encoded.width, encoded.height) == (1568, 882)
    decoded = decode_png(encoded.data)
    assert decoded.shape == (882, 1568, 3)
    # 面积平均保持平均亮度。
    assert abs(float(decoded.mean()) - float(rgb.mean())) < 1.0


def test_same_pixels_give_same_digest() -> None:
    rgb = np.arange(4 * 4 * 3, dtype=np.uint8).reshape(4, 4, 3)
    first = encode_screenshot(raw_image(rgb), 1568)
    second = encode_screenshot(raw_image(rgb, padding=4), 1568)
    changed = rgb.copy()
    changed[0, 0, 0] ^= 1
    third = encode_screenshot(raw_image(changed), 1568)
    assert first.sha256 == second.sha256
    assert first.sha256 != third.sha256
