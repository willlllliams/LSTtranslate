"""OcrResult.blocks 的 JSON 输出（格式以主进程 lst/services/ocr/blocks.py 的模块说明为准）。

每个版面块是一个 JSON 对象：

- 必填：id（ASCII 字母、数字或下划线，这里用阅读顺序的十进制字符串）、category、bbox、order、score；
- 总是给出：text（行与行之间用换行分隔）、lines（[{bbox, text, score}]）；
- 只在对应类别出现：cells 只在 table 块，latex 只在 formula 块，chart_table 只在 chart 块。
  主进程把出现在其他类别里的这三个字段视为坏块，所以这里按类别决定是否写出这个键，而不是写 null。

坐标是相对原始截图左上角的整数像素，已经裁剪到截图范围内，并且 x0 < x1、y0 < y1；置信度裁剪到 [0, 1]。
包围盒为空的行和单元格在转换阶段就已丢弃（geometry.clamp_to_pixels 返回 None）。
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from lst.contracts.ocr import BlockCategory
from lst.contracts.worker_protocol import JsonValue
from lst.workers.ocr_worker.geometry import PixelBox


def clamp_score(value: float) -> float:
    """置信度裁剪到 [0, 1]；非有限数记为 0。"""
    score = float(value)
    if not math.isfinite(score):
        return 0.0
    return min(max(score, 0.0), 1.0)


def _bbox(box: PixelBox) -> list[JsonValue]:
    return [int(box[0]), int(box[1]), int(box[2]), int(box[3])]


@dataclass(frozen=True, slots=True)
class OutLine:
    bbox: PixelBox
    text: str
    score: float

    def to_json(self) -> dict[str, JsonValue]:
        return {"bbox": _bbox(self.bbox), "text": self.text, "score": clamp_score(self.score)}


@dataclass(frozen=True, slots=True)
class OutCell:
    row: int
    col: int
    row_span: int
    col_span: int
    bbox: PixelBox
    text: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "row": self.row,
            "col": self.col,
            "row_span": self.row_span,
            "col_span": self.col_span,
            "bbox": _bbox(self.bbox),
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class OutBlock:
    """一个输出版面块。cells、latex、chart_table 只在对应类别写出，见模块说明。"""

    category: BlockCategory
    bbox: PixelBox
    order: int
    score: float
    text: str = ""
    lines: Sequence[OutLine] = ()
    cells: Sequence[OutCell] = ()
    latex: str | None = None
    chart_table: Sequence[Sequence[str]] | None = None

    @property
    def block_id(self) -> str:
        return str(self.order)

    def to_json(self) -> dict[str, JsonValue]:
        body: dict[str, JsonValue] = {
            "id": self.block_id,
            "category": self.category.value,
            "bbox": _bbox(self.bbox),
            "order": self.order,
            "score": clamp_score(self.score),
            "text": self.text,
            "lines": [line.to_json() for line in self.lines],
        }
        if self.category is BlockCategory.TABLE:
            body["cells"] = [cell.to_json() for cell in self.cells]
        elif self.category is BlockCategory.FORMULA:
            body["latex"] = self.latex
        elif self.category is BlockCategory.CHART:
            body["chart_table"] = None if self.chart_table is None else _rows(self.chart_table)
        return body


def _rows(rows: Sequence[Sequence[str]]) -> list[JsonValue]:
    out: list[JsonValue] = []
    for row in rows:
        cells: list[JsonValue] = [str(cell) for cell in row]
        out.append(cells)
    return out


def blocks_to_json(blocks: Sequence[OutBlock]) -> list[JsonValue]:
    """按阅读顺序输出全部版面块。"""
    return [block.to_json() for block in sorted(blocks, key=lambda b: b.order)]
