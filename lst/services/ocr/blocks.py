"""OCR 进程返回的版面块的格式与解码（需求 OCR-01、OCR-02，架构文档 8.1、8.4 节）。

OcrResult.blocks 的格式
=======================

OCR 进程完成识别后发送一条 ``ocr_result`` 消息，其中 ``blocks`` 是 JSON 数组，每个元素是一个版面块对象，
字段与 ``lst.contracts.ocr.LayoutBlock`` 一一对应。不允许出现下列之外的字段。

必填字段：

- ``id``：字符串，1 至 64 个 ASCII 字母、数字或下划线，在一个结果内唯一。段号由它派生。
- ``category``：版面类别（需求 OCR-01），取值为 "text"、"title"、"table"、"formula"、"chart"、"image"、
  "header_footer" 之一。PP-StructureV3 的标签按架构文档 8.4 节的表映射。
- ``bbox``：``[x0, y0, x1, y1]``，四个有限数，见下文“坐标”。
- ``order``：非负整数，阅读顺序，从 0 开始。
- ``score``：0 到 1 之间的数，版面检测置信度。文本模式填各行置信度的平均值。

可选字段：

- ``text``：字符串，块的完整文本，行与行之间用换行分隔。默认 ""。
- ``lines``：文本行对象数组，按块内阅读顺序排列。默认 []。
- ``cells``：单元格对象数组，只允许出现在 table 块。默认 []。
- ``latex``：字符串或 null，只允许出现在 formula 块。默认 null。
- ``chart_table``：字符串的二维数组或 null，按行排列，只允许出现在 chart 块。默认 null。

文本行对象：``{"bbox": [x0, y0, x1, y1], "text": "……", "score": 0.98}``，三个字段都必填。

单元格对象：``{"row": 0, "col": 0, "row_span": 1, "col_span": 1, "bbox": [x0, y0, x1, y1], "text": "……"}``。
row、col 是从 0 开始的行列号（必填），同一个表格里 (row, col) 不能重复；row_span、col_span 是不小于 1 的
跨行、跨列数（默认 1）；bbox 必填；text 默认 ""。单元格网格来自 PP-StructureV3 的 ``pred_html``，
包围盒来自 ``cell_box_list``（架构文档 8.4 节）。

坐标：物理像素，相对 **原始** 截图左上角。OCR 进程按 resize_max_side 缩小过图像时，要把坐标换算回原图。
坐标可以是小数：解码时左上角向下取整、右下角向上取整，再裁剪到截图范围内。要求 x0 < x1、y0 < y1。

两种识别模式（需求 OCR-09）的输出：

- 版面模式：PP-StructureV3 的每个版面块对应一个对象，正文、标题、页眉页脚块是段落级的，带 text，
  lines 可以省略。
- 文本模式：PaddleOCR 文本模式与 Windows OCR 都不做版面分析。OCR 进程返回一个 text 块：
  bbox 为整张截图，order 为 0，lines 为全部文本行（顺序不限），text 为空串。
  段落由主进程合并（``lst.services.ocr.paragraphs``）。

示例::

    [
      {"id": "0", "category": "title", "bbox": [12, 8, 380, 40], "order": 0, "score": 0.97,
       "text": "Quarterly report"},
      {"id": "1", "category": "table", "bbox": [12, 60, 600, 200], "order": 1, "score": 0.95,
       "text": "Region Sales\\nNorth 120",
       "cells": [{"row": 0, "col": 0, "bbox": [12, 60, 300, 100], "text": "Region"},
                 {"row": 0, "col": 1, "bbox": [300, 60, 600, 100], "text": "Sales"},
                 {"row": 1, "col": 0, "bbox": [12, 100, 300, 140], "text": "North"},
                 {"row": 1, "col": 1, "bbox": [300, 100, 600, 140], "text": "120"}]},
      {"id": "2", "category": "formula", "bbox": [12, 210, 300, 260], "order": 2, "score": 0.9,
       "latex": "E = mc^2"}
    ]

解码规则
========

``decode_blocks`` 逐块严格校验。一个块不合格（字段缺失或多余、类型不对、取值越界、内容字段与类别不符、
id 或单元格行列号重复、包围盒完全落在截图之外）时跳过这个块并记一条 WARNING，其余块照常返回，
不让整个结果失败。文本行或单元格的包围盒完全落在截图之外时只丢弃这一行或这个单元格。
日志只写块的序号和出错的字段，不写识别出的文本（需求 PRV-02）。
"""

import logging
import math
import re
from collections.abc import Mapping, Sequence
from typing import Final, cast

from lst.contracts.ocr import BBox, BlockCategory, LayoutBlock, TableCell, TextLine

logger = logging.getLogger(__name__)

BLOCK_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9_]{1,64}")
"""版面块 id 的合法写法。不含连字符，段号 b{id} 与表格单元格段号 b{id}-r{row}c{col} 因此不会撞车。"""

_BLOCK_REQUIRED: Final = frozenset({"id", "category", "bbox", "order", "score"})
_BLOCK_OPTIONAL: Final = frozenset({"text", "lines", "cells", "latex", "chart_table"})
_LINE_FIELDS: Final = frozenset({"bbox", "text", "score"})
_CELL_REQUIRED: Final = frozenset({"row", "col", "bbox"})
_CELL_OPTIONAL: Final = frozenset({"row_span", "col_span", "text"})

# 各内容字段只允许出现在对应类别的块里。
_CONTENT_OWNER: Final = {
    "cells": BlockCategory.TABLE,
    "latex": BlockCategory.FORMULA,
    "chart_table": BlockCategory.CHART,
}


class _Invalid(Exception):
    """一个块不合格。消息是不含块内容的原因，例如“lines[2].bbox 不是 4 个数”。"""


def decode_blocks(raw: object, *, width: int, height: int) -> tuple[LayoutBlock, ...]:
    """把 OcrResult.blocks 解码成版面块，按 order 排序（order 相同时保持原顺序）。

    width、height 是原始截图的物理像素尺寸，坐标裁剪到这个范围内。
    raw 不是数组时返回空元组并记 WARNING。
    """
    if not isinstance(raw, list):
        logger.warning("OCR 结果的 blocks 不是数组，按没有版面块处理")
        return ()
    items = cast(list[object], raw)
    blocks: list[LayoutBlock] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        try:
            block = _decode_block(item, width, height)
            if block.id in seen:
                raise _Invalid("id 与前面的块重复")
        except _Invalid as exc:
            logger.warning("OCR 结果中第 %d 个版面块无效，已跳过：%s", index, exc)
            continue
        seen.add(block.id)
        blocks.append(block)
    blocks.sort(key=lambda b: b.order)
    return tuple(blocks)


# ---------------------------------------------------------------- 逐层校验


def _decode_block(item: object, width: int, height: int) -> LayoutBlock:
    obj = _object(item, "", _BLOCK_REQUIRED, _BLOCK_OPTIONAL)
    block_id = _string(obj, "id")
    if BLOCK_ID_PATTERN.fullmatch(block_id) is None:
        raise _Invalid("id 必须是 1 至 64 个 ASCII 字母、数字或下划线")
    try:
        category = BlockCategory(_string(obj, "category"))
    except ValueError:
        raise _Invalid("category 不是已知的版面类别") from None
    for name, owner in _CONTENT_OWNER.items():
        if name in obj and category is not owner:
            raise _Invalid(f"{name} 只能出现在 {owner.value} 块中")
    bbox = _bbox(obj["bbox"], "bbox", width, height)
    if bbox is None:
        raise _Invalid("bbox 完全在截图之外")
    return LayoutBlock(
        id=block_id,
        category=category,
        bbox=bbox,
        order=_non_negative_int(obj, "order"),
        score=_score(obj, "score"),
        text=_string(obj, "text", default=""),
        lines=_lines(obj, width, height),
        cells=_cells(obj, width, height),
        latex=_optional_string(obj, "latex"),
        chart_table=_chart_table(obj),
    )


def _lines(obj: Mapping[str, object], width: int, height: int) -> tuple[TextLine, ...]:
    out: list[TextLine] = []
    for index, item in enumerate(_array(obj, "lines")):
        where = f"lines[{index}]"
        line = _object(item, where, _LINE_FIELDS, frozenset())
        bbox = _bbox(line["bbox"], f"{where}.bbox", width, height)
        text = _string(line, "text", where=where)
        score = _score(line, "score", where=where)
        if bbox is not None:
            out.append(TextLine(bbox, text, score))
    return tuple(out)


def _cells(obj: Mapping[str, object], width: int, height: int) -> tuple[TableCell, ...]:
    out: list[TableCell] = []
    seen: set[tuple[int, int]] = set()
    for index, item in enumerate(_array(obj, "cells")):
        where = f"cells[{index}]"
        cell = _object(item, where, _CELL_REQUIRED, _CELL_OPTIONAL)
        row = _non_negative_int(cell, "row", where=where)
        col = _non_negative_int(cell, "col", where=where)
        if (row, col) in seen:
            raise _Invalid(f"{where} 的行列号与前面的单元格重复")
        seen.add((row, col))
        row_span = _positive_int(cell, "row_span", where=where)
        col_span = _positive_int(cell, "col_span", where=where)
        bbox = _bbox(cell["bbox"], f"{where}.bbox", width, height)
        text = _string(cell, "text", where=where, default="")
        if bbox is not None:
            out.append(TableCell(row, col, row_span, col_span, bbox, text))
    return tuple(out)


def _chart_table(obj: Mapping[str, object]) -> tuple[tuple[str, ...], ...] | None:
    value = obj.get("chart_table")
    if value is None:
        return None
    if not isinstance(value, list):
        raise _Invalid("chart_table 必须是数组或 null")
    rows: list[tuple[str, ...]] = []
    for r, row in enumerate(cast(list[object], value)):
        if not isinstance(row, list):
            raise _Invalid(f"chart_table[{r}] 必须是数组")
        cells = cast(list[object], row)
        if not all(isinstance(cell, str) for cell in cells):
            raise _Invalid(f"chart_table[{r}] 的元素必须是字符串")
        rows.append(tuple(cast(list[str], cells)))
    return tuple(rows)


# ---------------------------------------------------------------- 基本类型


def _object(
    item: object, where: str, required: frozenset[str], optional: frozenset[str]
) -> Mapping[str, object]:
    """校验一个 JSON 对象的字段集合。where 是它在块内的位置，块本身为空串。"""
    label = f"{where} " if where else ""
    if not isinstance(item, dict):
        raise _Invalid(f"{label}不是 JSON 对象")
    obj = cast(dict[object, object], item)
    keys = set(obj)
    if not all(isinstance(key, str) for key in keys):
        raise _Invalid(f"{label}的字段名必须是字符串")
    missing = required - keys
    if missing:
        raise _Invalid(f"{label}缺少字段 {sorted(missing)}")
    extra = cast(set[str], keys) - required - optional
    if extra:
        raise _Invalid(f"{label}有未定义的字段 {sorted(extra)}")
    return cast(Mapping[str, object], obj)


def _field(name: str, where: str | None) -> str:
    return name if where is None else f"{where}.{name}"


def _string(
    obj: Mapping[str, object], name: str, *, where: str | None = None, default: str | None = None
) -> str:
    value = obj.get(name, default)
    if not isinstance(value, str):
        raise _Invalid(f"{_field(name, where)} 必须是字符串")
    return value


def _optional_string(obj: Mapping[str, object], name: str) -> str | None:
    value = obj.get(name)
    if value is not None and not isinstance(value, str):
        raise _Invalid(f"{name} 必须是字符串或 null")
    return value


def _int(obj: Mapping[str, object], name: str, where: str | None, default: int | None) -> int:
    value = obj.get(name, default)
    # bool 是 int 的子类，JSON 的 true/false 不能当整数用。
    if not isinstance(value, int) or isinstance(value, bool):
        raise _Invalid(f"{_field(name, where)} 必须是整数")
    return value


def _non_negative_int(obj: Mapping[str, object], name: str, *, where: str | None = None) -> int:
    value = _int(obj, name, where, None)
    if value < 0:
        raise _Invalid(f"{_field(name, where)} 不能为负")
    return value


def _positive_int(obj: Mapping[str, object], name: str, *, where: str | None = None) -> int:
    value = _int(obj, name, where, 1)
    if value < 1:
        raise _Invalid(f"{_field(name, where)} 必须不小于 1")
    return value


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _score(obj: Mapping[str, object], name: str, *, where: str | None = None) -> float:
    value = obj.get(name)
    if not _is_number(value):
        raise _Invalid(f"{_field(name, where)} 必须是有限数")
    score = float(cast(float, value))
    if not 0.0 <= score <= 1.0:
        raise _Invalid(f"{_field(name, where)} 必须在 0 到 1 之间")
    return score


def _array(obj: Mapping[str, object], name: str) -> Sequence[object]:
    value = obj.get(name, [])
    if not isinstance(value, list):
        raise _Invalid(f"{name} 必须是数组")
    return cast(list[object], value)


def _bbox(value: object, where: str, width: int, height: int) -> BBox | None:
    """解析并裁剪包围盒。格式不对时抛出 _Invalid；裁剪后面积为零（完全在截图之外）时返回 None。"""
    if not isinstance(value, list):
        raise _Invalid(f"{where} 必须是 [x0, y0, x1, y1]")
    coords = cast(list[object], value)
    if len(coords) != 4 or not all(_is_number(v) for v in coords):
        raise _Invalid(f"{where} 必须是 4 个有限数")
    x0, y0, x1, y1 = (float(cast(float, v)) for v in coords)
    if not (x0 < x1 and y0 < y1):
        raise _Invalid(f"{where} 的左上角必须在右下角的左上方")
    box = BBox(
        _clamp(math.floor(x0), width),
        _clamp(math.floor(y0), height),
        _clamp(math.ceil(x1), width),
        _clamp(math.ceil(y1), height),
    )
    if box.width <= 0 or box.height <= 0:
        return None
    return box


def _clamp(value: int, limit: int) -> int:
    return max(0, min(value, limit))
