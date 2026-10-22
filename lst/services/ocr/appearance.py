"""从原图提取文字颜色，用于区分紧邻的界面控件。坐标与原始截图一致。"""

import re
from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

from lst.contracts.ocr import BBox, RawImage, TextLine

type Color = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class Appearance:
    color: Color
    ink_height: int
    stroke: float = 0.0
    background: Color | None = None


def same_color(a: Appearance | None, b: Appearance | None) -> bool:
    return a is None or b is None or max(abs(x - y) for x, y in zip(a.color, b.color, strict=True)) <= 48


def compatible(a: Appearance | None, b: Appearance | None) -> bool:
    """缺少像素证据时保留几何规则；有证据时要求前景色接近。"""
    if a is None or b is None:
        return True
    same_weight = min(a.stroke, b.stroke) <= 0 or max(a.stroke, b.stroke) <= 1.5 * min(a.stroke, b.stroke)
    return same_color(a, b) and same_weight


class ImageAppearance:
    def __init__(self, image: RawImage) -> None:
        rows = np.frombuffer(image.data, dtype=np.uint8, count=image.height * image.stride).reshape(
            image.height, image.stride
        )
        self._pixels = rows[:, : image.width * 4].reshape(image.height, image.width, 4)[..., 2::-1]
        self._cache: dict[TextLine, Appearance | None] = {}

    def of(self, line: TextLine) -> Appearance | None:
        if line not in self._cache:
            self._cache[line] = self._sample(line)
        return self._cache[line]

    def split(self, line: TextLine) -> tuple[TextLine, ...]:
        """用字间投影定位 OCR 已给出的词，拆开一行里的不同颜色或宽间距控件。"""
        return tuple(self._refine_horizontal(part) for part in self._split(line))

    def connects(self, a: TextLine, b: TextLine) -> bool:
        """两个行片段之间还有同色的横向文字笔画时，为跨漏检词的拼行提供像素证据。"""
        left, right = (a, b) if a.bbox.x0 <= b.bbox.x0 else (b, a)
        first, second = self.of(left), self.of(right)
        if first is None or second is None or not same_color(first, second):
            return False
        height, width = self._pixels.shape[:2]
        x0, x1 = max(0, left.bbox.x1), min(width, right.bbox.x0)
        y0, y1 = max(0, min(left.bbox.y0, right.bbox.y0)), min(height, max(left.bbox.y1, right.bbox.y1))
        if x1 <= x0 or y1 <= y0:
            return False
        pixels = self._pixels[y0:y1, x0:x1].astype(np.int32)
        background = np.median(pixels.reshape(-1, 3), axis=0)
        if any(
            np.max(np.abs(background - np.array(reference))) > 24
            for reference in (first.background, second.background)
            if reference is not None
        ):
            return False
        color = (np.array(first.color) + np.array(second.color)) / 2
        ink = np.all(np.abs(pixels - color) <= 40, axis=2)
        columns = np.flatnonzero(ink.any(axis=0))
        ys = np.nonzero(ink)[0]
        if len(columns) < 0.35 * (x1 - x0) or not len(ys):
            return False
        ink_height = int(ys.max() - ys.min() + 1)
        span = int(columns[-1] - columns[0] + 1)
        return ink_height >= 0.45 * min(left.bbox.height, right.bbox.height) and span > 1.25 * ink_height

    def gutter(self, a: TextLine, b: TextLine, window: float) -> bool:
        """确认几何候选栏缝在原图中也持续留白，排除词级检测框造成的假栏缝。"""
        left, right = (a, b) if a.bbox.x0 <= b.bbox.x0 else (b, a)
        style = self.of(left)
        if style is None:
            return True
        h = max(left.bbox.height, right.bbox.height)
        center = (left.bbox.y0 + left.bbox.y1 + right.bbox.y0 + right.bbox.y1) / 4
        height, width = self._pixels.shape[:2]
        x0, x1 = max(0, left.bbox.x1), min(width, right.bbox.x0)
        y0, y1 = max(0, int(center - window * h)), min(height, int(center + window * h))
        if x1 <= x0 or y1 <= y0:
            return True
        pixels = self._pixels[y0:y1, x0:x1].astype(np.int32)
        ink = np.all(np.abs(pixels - np.array(style.color)) <= 40, axis=2)
        return bool(np.count_nonzero(ink.any(axis=0)) < max(2, 0.5 * (x1 - x0)))

    def _split(self, line: TextLine) -> tuple[TextLine, ...]:
        words = re.sub(r"(?<=\w)([»》])", r" \1", line.text).split()
        box = line.bbox
        appearance = self.of(line)
        if (
            not 2 <= len(words) <= 8
            or appearance is None
            or box.width < 8 * box.height
            or box.height > 48
            or any(c.isalpha() and not c.isascii() for c in line.text)
            or any(c in line.text for c in ">√✓")
            or re.match(r"^\d+[.)]\S", line.text.strip()) is not None
        ):
            return (line,)
        pixels = self._pixels[box.y0 : box.y1, box.x0 : box.x1].astype(np.int32)
        # 文字框压得很紧或跨过插图时，面积最大的颜色会成为文字色；此时不据此拆词。
        if float(np.all(np.abs(pixels - np.array(appearance.color)) <= 40, axis=2).mean()) >= 0.4:
            return (line,)
        background = cast(NDArray[np.float64], np.median(np.concatenate((pixels[0], pixels[-1])), axis=0))
        distance = cast(NDArray[np.float64], np.max(np.abs(pixels - background), axis=2))
        mask = distance >= 24
        columns = np.flatnonzero(mask.any(axis=0))
        if len(columns) < 2:
            return (line,)
        gaps = [
            (int(columns[i] - columns[i - 1] - 1), int(columns[i - 1] + 1), int(columns[i]))
            for i in range(1, len(columns))
            if columns[i] - columns[i - 1] > 1
        ]
        selected = sorted(gaps, reverse=True)[: len(words) - 1]
        if len(selected) != len(words) - 1 or min(gap[0] for gap in selected) < 0.12 * appearance.ink_height:
            return (line,)
        cuts = sorted(selected, key=lambda gap: gap[1])
        spans = list(
            zip(
                [int(columns[0]), *[gap[2] for gap in cuts]],
                [*[gap[1] for gap in cuts], int(columns[-1]) + 1],
                strict=True,
            )
        )
        parts: list[TextLine] = []
        styles: list[Appearance] = []
        for word, (start, stop) in zip(words, spans, strict=True):
            part = TextLine(BBox(box.x0 + start, box.y0, box.x0 + stop, box.y1), word, line.score)
            style = self._sample(part)
            if style is None:
                return (line,)
            core = np.all(np.abs(pixels[:, start:stop] - np.array(style.color)) <= 40, axis=2)
            ys = np.nonzero(core)[0]
            if len(ys):
                part = TextLine(
                    BBox(part.bbox.x0, box.y0 + int(ys.min()), part.bbox.x1, box.y0 + int(ys.max()) + 1),
                    word,
                    line.score,
                )
            self._cache[part] = style
            parts.append(part)
            styles.append(style)
        boundaries: list[int] = []
        for index, (gap, a, b) in enumerate(zip(cuts, styles, styles[1:], strict=False), 1):
            other_gaps = [item[0] for item in cuts if item is not gap]
            usual_gap = float(np.median(other_gaps)) if other_gaps else appearance.ink_height * 0.3
            different_size = abs(a.ink_height - b.ink_height) > 0.1 * min(a.ink_height, b.ink_height)
            wide = gap[0] > max(0.7 * min(a.ink_height, b.ink_height), usual_gap * 1.8)
            control_gap = gap[0] > max(max(a.ink_height, b.ink_height), usual_gap * 2.5)
            if not same_color(a, b) or (wide and different_size) or control_gap:
                boundaries.append(index)
        if not boundaries:
            return (line,)
        groups: list[TextLine] = []
        for start, stop in zip([0, *boundaries], [*boundaries, len(parts)], strict=True):
            merged = TextLine(
                BBox(
                    parts[start].bbox.x0,
                    min(p.bbox.y0 for p in parts[start:stop]),
                    parts[stop - 1].bbox.x1,
                    max(p.bbox.y1 for p in parts[start:stop]),
                ),
                " ".join(words[start:stop]),
                line.score,
            )
            dominant = max(range(start, stop), key=lambda i: parts[i].bbox.width)
            self._cache[merged] = styles[dominant]
            groups.append(merged)
        return tuple(groups)

    def _refine_horizontal(self, line: TextLine) -> TextLine:
        """排除检测框边缘的暗色图标，按文字前景定位横向边界；保留行高与列表编号。"""
        box = line.bbox
        style = self.of(line)
        if (
            style is None
            or box.height > 48
            or not any(c.isalpha() for c in line.text)
            or re.match(r"^\d+[.)、]", line.text.strip()) is not None
        ):
            return line
        pixels = self._pixels[box.y0 : box.y1, box.x0 : box.x1].astype(np.int32)
        core = np.all(np.abs(pixels - np.array(style.color)) <= 40, axis=2)
        if float(core.mean()) >= 0.4:
            return line
        columns = np.flatnonzero(core.any(axis=0))
        if not len(columns):
            return line
        start, stop = int(columns[0]), int(columns[-1]) + 1
        if max(start, box.width - stop) <= 0.6 * box.height:
            return line
        refined = TextLine(BBox(box.x0 + start, box.y0, box.x0 + stop, box.y1), line.text, line.score)
        self._cache[refined] = style
        return refined

    def _sample(self, line: TextLine) -> Appearance | None:
        box = line.bbox
        height, width = self._pixels.shape[:2]
        pixels = self._pixels[max(0, box.y0) : min(height, box.y1), max(0, box.x0) : min(width, box.x1)]
        if pixels.size == 0:
            return None
        # 颜色判断只需稀疏采样；宽标题与正文行不必逐像素排序。
        sample_step = max(1, pixels.shape[1] // 192)
        pixels = pixels[:, ::sample_step]
        flat = pixels.reshape(-1, 3).astype(np.int32)
        # 背景通常是框内面积最大的颜色。量化可容纳渐变和压缩噪声。
        quantized = flat // 16
        codes = quantized[:, 0] * 256 + quantized[:, 1] * 16 + quantized[:, 2]
        counts = np.bincount(codes, minlength=4096)
        background_bin = int(np.argmax(counts))
        if counts[background_bin] < len(flat) * 0.2:
            return None
        background = cast(NDArray[np.float64], np.median(flat[codes == background_bin][::4], axis=0))
        distance = cast(NDArray[np.float64], np.max(np.abs(flat - background), axis=1))
        peak = float(distance.max())
        if peak < 24:
            return None
        delta = flat - background
        contrast = np.abs(delta @ np.array([0.299, 0.587, 0.114]))
        weights = np.maximum(0, contrast - 8) ** 4
        if float(weights.sum()) == 0:
            return None
        # ClearType 的彩色边缘对比度低；按亮度对比度加权，避免短词被彩色边缘代表。
        color = np.average(flat, axis=0, weights=weights)
        mask = np.all(np.abs(flat - color) <= 40, axis=1)
        ys = np.nonzero(mask.reshape(pixels.shape[:2]))[0]
        if not len(ys):
            return None
        binary = mask.reshape(pixels.shape[:2]).astype(np.int32)
        perimeter = float(np.abs(np.diff(binary, axis=0)).sum() + np.abs(np.diff(binary, axis=1)).sum())
        stroke = 2 * float(binary.sum()) / max(1, perimeter) if sample_step == 1 else 0.0
        return Appearance(
            (int(color[0]), int(color[1]), int(color[2])),
            int(ys.max() - ys.min() + 1),
            stroke,
            (int(background[0]), int(background[1]), int(background[2])),
        )
