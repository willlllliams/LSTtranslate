"""版面块解码（decode_blocks）的测试：合法块、各类坏块、坐标换算与裁剪、排序、日志隐私。"""

import copy
import logging
from typing import Any

import pytest

from lst.contracts.ocr import BBox, BlockCategory, LayoutBlock, TableCell, TextLine
from lst.services.ocr import decode_blocks

LOGGER = "lst.services.ocr.blocks"
W, H = 800, 600
SECRET = "机密原文 secret source text"


def decode(raw: object) -> tuple[LayoutBlock, ...]:
    return decode_blocks(raw, width=W, height=H)


def text_block(block_id: str = "0", order: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "id": block_id,
        "category": "text",
        "bbox": [10, 10, 200, 60],
        "order": order,
        "score": 0.9,
        **extra,
    }


# ---------------------------------------------------------------- 合法块


def test_full_block_set_decodes() -> None:
    raw = [
        {
            "id": "t0",
            "category": "title",
            "bbox": [12, 8, 380, 40],
            "order": 0,
            "score": 0.97,
            "text": "标题",
            "lines": [{"bbox": [12, 8, 380, 40], "text": "标题", "score": 0.99}],
        },
        {
            "id": "tab",
            "category": "table",
            "bbox": [12, 60, 600, 140],
            "order": 1,
            "score": 0.95,
            "text": "A B",
            "cells": [
                {"row": 0, "col": 0, "bbox": [12, 60, 300, 100], "text": "A"},
                {"row": 0, "col": 1, "row_span": 2, "col_span": 1, "bbox": [300, 60, 600, 140], "text": "B"},
            ],
        },
        {
            "id": "f",
            "category": "formula",
            "bbox": [12, 150, 300, 190],
            "order": 2,
            "score": 0.9,
            "latex": "E=mc^2",
        },
        {
            "id": "c",
            "category": "chart",
            "bbox": [12, 200, 400, 400],
            "order": 3,
            "score": 0.8,
            "chart_table": [["year", "sales"], ["2025", "12"]],
        },
        {"id": "img", "category": "image", "bbox": [400, 200, 700, 400], "order": 4, "score": 0.7},
        {
            "id": "hf",
            "category": "header_footer",
            "bbox": [0, 560, 800, 600],
            "order": 5,
            "score": 0.6,
            "text": "3",
        },
    ]
    blocks = decode(raw)
    assert [b.id for b in blocks] == ["t0", "tab", "f", "c", "img", "hf"]
    assert blocks[0] == LayoutBlock(
        id="t0",
        category=BlockCategory.TITLE,
        bbox=BBox(12, 8, 380, 40),
        order=0,
        score=0.97,
        text="标题",
        lines=(TextLine(BBox(12, 8, 380, 40), "标题", 0.99),),
    )
    assert blocks[1].cells == (
        TableCell(0, 0, 1, 1, BBox(12, 60, 300, 100), "A"),
        TableCell(0, 1, 2, 1, BBox(300, 60, 600, 140), "B"),
    )
    assert blocks[2].latex == "E=mc^2"
    assert blocks[3].chart_table == (("year", "sales"), ("2025", "12"))
    assert blocks[4].category is BlockCategory.IMAGE
    assert blocks[4].text == ""
    assert blocks[4].lines == ()
    assert blocks[5].category is BlockCategory.HEADER_FOOTER


def test_optional_fields_take_defaults() -> None:
    (block,) = decode([text_block()])
    assert (block.text, block.lines, block.cells, block.latex, block.chart_table) == ("", (), (), None, None)


def test_null_latex_and_chart_table_are_allowed() -> None:
    raw = [
        {"id": "f", "category": "formula", "bbox": [0, 0, 10, 10], "order": 0, "score": 1, "latex": None},
        {"id": "c", "category": "chart", "bbox": [0, 0, 10, 10], "order": 1, "score": 0, "chart_table": None},
    ]
    blocks = decode(raw)
    assert [(b.latex, b.chart_table) for b in blocks] == [(None, None), (None, None)]


def test_blocks_are_sorted_by_order_and_ties_keep_input_order() -> None:
    raw = [text_block("a", 2), text_block("b", 0), text_block("c", 1), text_block("d", 0)]
    assert [b.id for b in decode(raw)] == ["b", "d", "c", "a"]


def test_fractional_coordinates_round_outwards() -> None:
    raw = [
        text_block(
            bbox=[10.7, 20.2, 99.1, 40.0], lines=[{"bbox": [10.5, 20.5, 50.5, 30.5], "text": "x", "score": 1}]
        )
    ]
    (block,) = decode(raw)
    assert block.bbox == BBox(10, 20, 100, 40)
    assert block.lines[0].bbox == BBox(10, 20, 51, 31)


def test_coordinates_are_clipped_to_the_screenshot() -> None:
    raw = [
        text_block(
            bbox=[-15, -3, 900, 650],
            lines=[{"bbox": [-5, 590, 810, 620], "text": "x", "score": 0.5}],
        ),
        {
            "id": "t",
            "category": "table",
            "bbox": [700, 500, 820, 640],
            "order": 1,
            "score": 0.9,
            "cells": [{"row": 0, "col": 0, "bbox": [790, 590, 830, 610], "text": "edge"}],
        },
    ]
    text, table = decode(raw)
    assert text.bbox == BBox(0, 0, W, H)
    assert text.lines[0].bbox == BBox(0, 590, W, H)
    assert table.bbox == BBox(700, 500, W, H)
    assert table.cells[0].bbox == BBox(790, 590, W, H)


def test_lines_and_cells_outside_the_screenshot_are_dropped() -> None:
    raw = [
        text_block(
            lines=[
                {"bbox": [10, 10, 50, 30], "text": "in", "score": 1},
                {"bbox": [810, 10, 900, 30], "text": "out", "score": 1},
            ]
        ),
        {
            "id": "t",
            "category": "table",
            "bbox": [0, 0, 800, 600],
            "order": 1,
            "score": 1,
            "cells": [
                {"row": 0, "col": 0, "bbox": [0, 0, 10, 10]},
                {"row": 0, "col": 1, "bbox": [0, 610, 10, 620], "text": "out"},
            ],
        },
    ]
    text, table = decode(raw)
    assert [line.text for line in text.lines] == ["in"]
    assert [(c.row, c.col) for c in table.cells] == [(0, 0)]


def test_integral_floats_and_ints_are_both_numbers() -> None:
    raw = [text_block(order=0, score=1, bbox=[0, 0, 10.0, 10])]
    assert decode(raw)[0].score == 1.0


# ---------------------------------------------------------------- 坏块


def _mutations() -> list[tuple[str, dict[str, Any] | object]]:
    good = text_block("bad", lines=[{"bbox": [10, 10, 50, 30], "text": "x", "score": 1}])

    def changed(**fields: Any) -> dict[str, Any]:
        block = copy.deepcopy(good)
        block.update(fields)
        return block

    def without(name: str) -> dict[str, Any]:
        block = copy.deepcopy(good)
        del block[name]
        return block

    def line_changed(**fields: Any) -> dict[str, Any]:
        block = copy.deepcopy(good)
        block["lines"][0].update(fields)
        return block

    table = {"id": "bad", "category": "table", "bbox": [0, 0, 100, 100], "order": 0, "score": 1}
    return [
        ("不是对象", ["id", "bad"]),
        ("字符串", "block"),
        ("缺少 id", without("id")),
        ("缺少 category", without("category")),
        ("缺少 bbox", without("bbox")),
        ("缺少 order", without("order")),
        ("缺少 score", without("score")),
        ("多余字段", changed(label="paragraph_title")),
        ("id 不是字符串", changed(id=3)),
        ("id 为空", changed(id="")),
        ("id 含连字符", changed(id="a-r0c0")),
        ("id 太长", changed(id="x" * 65)),
        ("未知类别", changed(category="seal")),
        ("bbox 不是数组", changed(bbox="0,0,1,1")),
        ("bbox 只有 3 个数", changed(bbox=[0, 0, 1])),
        ("bbox 含字符串", changed(bbox=[0, 0, "1", 1])),
        ("bbox 含布尔值", changed(bbox=[0, 0, True, 1])),
        ("bbox 含 NaN", changed(bbox=[0, 0, float("nan"), 1])),
        ("bbox 含无穷大", changed(bbox=[0, 0, float("inf"), 1])),
        ("bbox 左右颠倒", changed(bbox=[100, 0, 10, 50])),
        ("bbox 宽度为零", changed(bbox=[10, 0, 10, 50])),
        ("bbox 完全在截图外", changed(bbox=[900, 10, 950, 50])),
        ("order 为负", changed(order=-1)),
        ("order 是小数", changed(order=1.5)),
        ("order 是布尔值", changed(order=True)),
        ("score 大于 1", changed(score=1.01)),
        ("score 为负", changed(score=-0.1)),
        ("score 是字符串", changed(score="0.9")),
        ("text 不是字符串", changed(text=None)),
        ("lines 不是数组", changed(lines={"bbox": [0, 0, 1, 1]})),
        ("文本行不是对象", changed(lines=["x"])),
        ("文本行缺少 score", changed(lines=[{"bbox": [0, 0, 1, 1], "text": "x"}])),
        ("文本行多余字段", line_changed(angle=0)),
        ("文本行 bbox 颠倒", line_changed(bbox=[50, 0, 10, 10])),
        ("文本行 text 是数字", line_changed(text=5)),
        ("cells 出现在正文块", changed(cells=[])),
        ("latex 出现在正文块", changed(latex="x")),
        ("chart_table 出现在正文块", changed(chart_table=None)),
        ("latex 不是字符串", {**table, "category": "formula", "latex": 5}),
        ("chart_table 不是数组", {**table, "category": "chart", "chart_table": "a,b"}),
        ("chart_table 的行不是数组", {**table, "category": "chart", "chart_table": ["a"]}),
        ("chart_table 含数字", {**table, "category": "chart", "chart_table": [["a", 1]]}),
        ("cells 不是数组", {**table, "cells": {}}),
        ("单元格缺少 bbox", {**table, "cells": [{"row": 0, "col": 0}]}),
        ("单元格行号为负", {**table, "cells": [{"row": -1, "col": 0, "bbox": [0, 0, 1, 1]}]}),
        ("单元格跨行为 0", {**table, "cells": [{"row": 0, "col": 0, "row_span": 0, "bbox": [0, 0, 1, 1]}]}),
        ("单元格多余字段", {**table, "cells": [{"row": 0, "col": 0, "bbox": [0, 0, 1, 1], "html": "<td>"}]}),
        (
            "单元格行列号重复",
            {
                **table,
                "cells": [
                    {"row": 0, "col": 0, "bbox": [0, 0, 1, 1]},
                    {"row": 0, "col": 0, "bbox": [1, 1, 2, 2]},
                ],
            },
        ),
    ]


@pytest.mark.parametrize(("case", "bad"), _mutations(), ids=[case for case, _ in _mutations()])
def test_bad_block_is_skipped_with_warning(case: str, bad: object, caplog: pytest.LogCaptureFixture) -> None:
    raw = [text_block("before", 0), bad, text_block("after", 1)]
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        blocks = decode(raw)
    assert [b.id for b in blocks] == ["before", "after"], case
    warnings = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 1, case
    assert "第 1 个版面块无效" in warnings[0].getMessage()


def test_duplicate_id_keeps_the_first_block(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        blocks = decode([text_block("x", 1, text="first"), text_block("x", 0, text="second")])
    assert [(b.id, b.text) for b in blocks] == [("x", "first")]
    assert "id 与前面的块重复" in caplog.text


@pytest.mark.parametrize("raw", [None, {}, "[]", 3])
def test_non_array_blocks_give_no_blocks(raw: object, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert decode(raw) == ()
    assert "不是数组" in caplog.text


def test_empty_array_gives_no_blocks() -> None:
    assert decode([]) == ()


def test_warnings_do_not_contain_recognised_text(caplog: pytest.LogCaptureFixture) -> None:
    raw = [
        text_block("a", text=SECRET, score=5),
        text_block("b", lines=[{"bbox": [0, 0, 1, 1], "text": SECRET, "score": "high"}]),
        {**text_block("c"), "category": SECRET},
        {**text_block("d"), "chart_table": [[SECRET]]},
        {**text_block("e"), "extra": SECRET},
        {**text_block("f"), "id": SECRET},
    ]
    with caplog.at_level(logging.DEBUG):
        assert decode(raw) == ()
    assert len(caplog.records) == len(raw)
    for record in caplog.records:
        message = record.getMessage()
        assert "机密" not in message
        assert "secret" not in message
