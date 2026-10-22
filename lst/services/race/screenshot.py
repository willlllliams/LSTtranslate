"""截屏翻译附带的截图：缩小与 PNG 编码（需求 ENG-07）。

CaptureCompleted 里的截图是 BGRA 原始像素（contracts.ocr.RawImage）。开启了“附带截图”的 LLM 实例
要的是缩小到 EngineInfo.image_max_side 以内的 PNG。这里的函数是纯计算，由竞速模块放进线程池执行，
不阻塞事件循环（架构文档第 3 章）。services 不能依赖 Qt，所以不用 QImage，缩放与编码用 numpy 与 zlib：

- 缩放用面积平均（box filter）：每个输出像素取它覆盖的源像素区域的平均值，区域边界可以落在像素中间，
  落在边界上的源像素按覆盖比例计权。文字截图缩小时，这样做比最近邻采样保留更多笔画信息，也不产生摩尔纹。
- 编码成 8 位 RGB 的 PNG，每行使用 Up 滤波（与上一行逐字节相减）。截图里大片同色区域相减后为 0，
  zlib 压缩率比不滤波更高；Alpha 通道丢弃，截图本来就不透明。

numpy 的数值数组运算与 zlib.compress 执行期间释放 GIL，在线程里编码时事件循环照常运转。
"""

import struct
import zlib
from typing import Final

import numpy as np
from numpy.typing import NDArray

from lst.contracts.ocr import RawImage
from lst.contracts.translation import PNG_MEDIA_TYPE, JobImage

# 缩放时一次处理的列数。分组让前缀和数组留在 CPU 缓存附近，也限制内存：4K 截图按 64 列一组，
# 一组的前缀和约 1.7 MB。实测 3840×2160 缩到 1568×882 约 0.25 秒，分组 256 时约 0.35 秒。
_CHUNK: Final = 64
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_ZLIB_LEVEL: Final = 6


def scaled_size(width: int, height: int, max_side: int) -> tuple[int, int]:
    """等比缩小到长边不超过 max_side 后的尺寸；本来就不超过时原样返回。每边至少 1 像素。"""
    if max_side < 1:
        raise ValueError("max_side 必须为正数")
    longest = max(width, height)
    if longest <= max_side:
        return width, height
    scale = max_side / longest
    return max(1, round(width * scale)), max(1, round(height * scale))


def encode_screenshot(image: RawImage, max_side: int) -> JobImage:
    """把截图缩小到长边不超过 max_side，编码成 PNG。耗时数十到数百毫秒，必须在线程里调用。"""
    width, height = image.width, image.height
    rows = np.frombuffer(image.data, dtype=np.uint8, count=image.stride * height).reshape(
        height, image.stride
    )
    bgra = rows[:, : width * 4].reshape(height, width, 4)
    rgb = bgra[:, :, 2::-1]  # BGRA → RGB，丢弃 Alpha
    out_width, out_height = scaled_size(width, height, max_side)
    if (out_width, out_height) != (width, height):
        rgb = _area_resize(rgb, out_width, out_height)
    data = encode_png(np.ascontiguousarray(rgb))
    return JobImage.from_encoded(PNG_MEDIA_TYPE, data, out_width, out_height)


def encode_png(rgb: NDArray[np.uint8]) -> bytes:
    """把形状为 (高, 宽, 3) 的 RGB 数组编码成 PNG（8 位真彩色，不隔行，每行 Up 滤波）。"""
    height, width, channels = rgb.shape
    if channels != 3 or height < 1 or width < 1:
        raise ValueError("encode_png 需要形状为 (高, 宽, 3) 的非空数组")
    lines = rgb.reshape(height, width * 3)
    filtered = np.empty((height, width * 3 + 1), dtype=np.uint8)
    filtered[:, 0] = 2  # 滤波类型 2：Up
    filtered[0, 1:] = lines[0]  # 第一行的“上一行”按全 0 处理
    # uint8 减法按 256 取模回绕，正是 Up 滤波要求的结果。
    np.subtract(lines[1:], lines[:-1], out=filtered[1:, 1:])
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join(
        (
            _PNG_SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(filtered.tobytes(), _ZLIB_LEVEL)),
            _chunk(b"IEND", b""),
        )
    )


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(payload, zlib.crc32(kind))
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _area_resize(rgb: NDArray[np.uint8], out_width: int, out_height: int) -> NDArray[np.uint8]:
    """面积平均缩小：先缩高度，再缩宽度。"""
    tall = _area_resize_axis0(rgb, out_height)
    wide = _area_resize_axis0(tall.transpose(1, 0, 2), out_width).transpose(1, 0, 2)
    return np.clip(np.rint(wide), 0, 255).astype(np.uint8)


def _area_resize_axis0(x: NDArray[np.generic], n_out: int) -> NDArray[np.float32]:
    """沿第 0 轴做面积平均，长度从 n_in 变为 n_out（n_out ≤ n_in）。

    把源数据看成分段常数函数 f，第 i 个源像素占据区间 [i, i+1)。它的累加和
    S(t) = ∫₀ᵗ f 在整数点上是前缀和，在整数点之间是线性的。第 j 个输出像素覆盖区间
    [j·r, (j+1)·r)，r = n_in / n_out，它的值是该区间上 f 的平均值 (S((j+1)·r) − S(j·r)) / r。
    S 在非整数点的值由相邻两个前缀和线性插值得到，结果是精确的面积平均。

    前缀和用 float32：4K 截图一列的前缀和不超过 2160 × 255 ≈ 5.5 × 10⁵，float32 在这个量级的
    间隔是 0.0625，相减后误差远小于取整到 8 位时的 0.5。
    """
    n_in = x.shape[0]
    edges = np.arange(n_out + 1, dtype=np.float64) * (n_in / n_out)
    edges[-1] = n_in  # 消除浮点误差，保证最后一个边界恰好是 n_in
    index = np.minimum(edges.astype(np.int64), n_in - 1)
    frac = (edges - index).astype(np.float32).reshape(-1, *([1] * (x.ndim - 1)))
    out = np.empty((n_out, *x.shape[1:]), dtype=np.float32)
    for start in range(0, x.shape[1], _CHUNK):
        part = x[:, start : start + _CHUNK]
        prefix = np.zeros((n_in + 1, *part.shape[1:]), dtype=np.float32)
        np.cumsum(part, axis=0, dtype=np.float32, out=prefix[1:])
        low = prefix[index]
        at = low + frac * (prefix[index + 1] - low)
        out[:, start : start + _CHUNK] = np.diff(at, axis=0) * (n_out / n_in)
    return out
