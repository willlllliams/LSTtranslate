"""OCR 测试共用的构造函数。"""

from collections.abc import Sequence

from lst.contracts.ocr import BBox, TextLine
from lst.services.ocr import GeometricMerger, MergeParams, Paragraph


def line(x0: int, y0: int, x1: int, y1: int, text: str = "x", score: float = 0.9) -> TextLine:
    return TextLine(BBox(x0, y0, x1, y1), text, score)


def stack(
    x0: int,
    y0: int,
    widths: Sequence[int],
    *,
    height: int = 20,
    gap: int = 6,
    texts: Sequence[str] | None = None,
) -> list[TextLine]:
    """从 (x0, y0) 开始自上而下排列的若干行，行高 height，行间距 gap。"""
    out: list[TextLine] = []
    y = y0
    for k, width in enumerate(widths):
        text = texts[k] if texts is not None else f"w{k}"
        out.append(line(x0, y, x0 + width, y + height, text))
        y += height + gap
    return out


def groups(paragraphs: Sequence[Paragraph]) -> list[list[int]]:
    return [list(p.lines) for p in paragraphs]


def merge(lines: Sequence[TextLine], params: MergeParams | None = None) -> tuple[Paragraph, ...]:
    return GeometricMerger(params).merge(lines)
