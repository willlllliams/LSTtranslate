"""OcrResult.blocks 的严格校验，按主进程 lst/services/ocr/blocks.py 的格式约定逐项检查。

比主进程的解码更严：坐标必须是已经裁剪到截图范围内的整数，单元格的六个字段都要给出，order 不能重复。
不合格时抛 AssertionError，消息指出第几个块的哪个字段。
"""

import math
import re
from typing import cast

from lst.contracts.ocr import BlockCategory

BLOCK_ID = re.compile(r"[A-Za-z0-9_]{1,64}")
CATEGORIES = frozenset(category.value for category in BlockCategory)
REQUIRED = frozenset({"id", "category", "bbox", "order", "score"})
OPTIONAL = frozenset({"text", "lines", "cells", "latex", "chart_table"})
LINE_FIELDS = frozenset({"bbox", "text", "score"})
CELL_FIELDS = frozenset({"row", "col", "row_span", "col_span", "bbox", "text"})
OWNER = {"cells": "table", "latex": "formula", "chart_table": "chart"}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_bbox(value: object, width: int, height: int, where: str) -> tuple[int, int, int, int]:
    assert isinstance(value, list), f"{where} 不是数组"
    coords = cast(list[object], value)
    assert len(coords) == 4 and all(_is_int(v) for v in coords), f"{where} 不是 4 个整数：{coords}"
    x0, y0, x1, y1 = cast(list[int], coords)
    assert 0 <= x0 < x1 <= width, f"{where} 的横坐标越界或为空：{coords}"
    assert 0 <= y0 < y1 <= height, f"{where} 的纵坐标越界或为空：{coords}"
    return x0, y0, x1, y1


def _check_score(value: object, where: str) -> None:
    assert isinstance(value, int | float) and not isinstance(value, bool), f"{where} 不是数"
    assert math.isfinite(value) and 0.0 <= value <= 1.0, f"{where} 不在 [0, 1] 内：{value}"


def validate_blocks(blocks: object, width: int, height: int) -> list[dict[str, object]]:
    """校验整个 blocks 数组，返回同一个数组（类型收窄后），便于测试继续检查内容。"""
    assert isinstance(blocks, list), "blocks 不是数组"
    items = cast(list[object], blocks)
    ids: set[str] = set()
    orders: set[int] = set()
    out: list[dict[str, object]] = []
    for index, item in enumerate(items):
        where = f"blocks[{index}]"
        assert isinstance(item, dict), f"{where} 不是对象"
        block = cast(dict[str, object], item)
        keys = set(block)
        assert keys >= REQUIRED, f"{where} 缺少字段 {sorted(REQUIRED - keys)}"
        assert keys <= REQUIRED | OPTIONAL, f"{where} 有未定义的字段 {sorted(keys - REQUIRED - OPTIONAL)}"
        block_id = block["id"]
        assert isinstance(block_id, str) and BLOCK_ID.fullmatch(block_id), f"{where}.id 不合法：{block_id!r}"
        assert block_id not in ids, f"{where}.id 重复"
        ids.add(block_id)
        category = block["category"]
        assert category in CATEGORIES, f"{where}.category 未知：{category!r}"
        for name, owner in OWNER.items():
            assert name not in block or category == owner, f"{where}.{name} 不能出现在 {category} 块"
        check_bbox(block["bbox"], width, height, f"{where}.bbox")
        order = block["order"]
        assert _is_int(order) and cast(int, order) >= 0, f"{where}.order 不是非负整数"
        assert order not in orders, f"{where}.order 重复"
        orders.add(cast(int, order))
        _check_score(block["score"], f"{where}.score")
        assert isinstance(block.get("text", ""), str), f"{where}.text 不是字符串"
        lines = block.get("lines", [])
        assert isinstance(lines, list), f"{where}.lines 不是数组"
        for i, line in enumerate(cast(list[object], lines)):
            lw = f"{where}.lines[{i}]"
            assert isinstance(line, dict) and set(cast(dict[str, object], line)) == LINE_FIELDS, (
                f"{lw} 字段不符"
            )
            entry = cast(dict[str, object], line)
            check_bbox(entry["bbox"], width, height, f"{lw}.bbox")
            assert isinstance(entry["text"], str), f"{lw}.text 不是字符串"
            _check_score(entry["score"], f"{lw}.score")
        if category == "table":
            _check_cells(block.get("cells", []), width, height, where)
        if category == "formula":
            latex = block.get("latex")
            assert latex is None or isinstance(latex, str), f"{where}.latex 不是字符串或 null"
        if category == "chart":
            _check_chart_table(block.get("chart_table"), where)
        out.append(block)
    return out


def _check_cells(cells: object, width: int, height: int, where: str) -> None:
    assert isinstance(cells, list), f"{where}.cells 不是数组"
    seen: set[tuple[int, int]] = set()
    for i, item in enumerate(cast(list[object], cells)):
        cw = f"{where}.cells[{i}]"
        assert isinstance(item, dict) and set(cast(dict[str, object], item)) == CELL_FIELDS, f"{cw} 字段不符"
        cell = cast(dict[str, object], item)
        row, col = cell["row"], cell["col"]
        assert _is_int(row) and _is_int(col) and cast(int, row) >= 0 and cast(int, col) >= 0, (
            f"{cw} 行列号无效"
        )
        key = (cast(int, row), cast(int, col))
        assert key not in seen, f"{cw} 行列号重复"
        seen.add(key)
        for span in ("row_span", "col_span"):
            assert _is_int(cell[span]) and cast(int, cell[span]) >= 1, f"{cw}.{span} 无效"
        check_bbox(cell["bbox"], width, height, f"{cw}.bbox")
        assert isinstance(cell["text"], str), f"{cw}.text 不是字符串"


def _check_chart_table(table: object, where: str) -> None:
    if table is None:
        return
    assert isinstance(table, list), f"{where}.chart_table 不是数组或 null"
    for row in cast(list[object], table):
        assert isinstance(row, list), f"{where}.chart_table 的行不是数组"
        assert all(isinstance(cell, str) for cell in cast(list[object], row)), (
            f"{where}.chart_table 有非字符串"
        )
