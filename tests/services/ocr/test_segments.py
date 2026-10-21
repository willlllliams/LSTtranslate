"""版面块到段的转换（blocks_to_segments）的测试：各类别的规则、段号、顺序、空文本、文本模式的段落合并。"""

import logging
from collections.abc import Sequence

import pytest

from lst.contracts.ocr import BBox, BlockCategory, LayoutBlock, OcrSegment, TableCell, TextLine
from lst.services.ocr import MergeParams, OcrMode, Paragraph, block_text, blocks_to_segments, decode_blocks
from lst.services.ocr.paragraphs import GeometricMerger
from tests.services.ocr.helpers import line, stack

STRUCTURE, TEXT = OcrMode.STRUCTURE, OcrMode.TEXT
BOX = BBox(10, 10, 300, 60)


def block(
    block_id: str,
    category: BlockCategory,
    order: int = 0,
    *,
    text: str = "",
    bbox: BBox = BOX,
    lines: Sequence[TextLine] = (),
    cells: Sequence[TableCell] = (),
    latex: str | None = None,
    chart_table: tuple[tuple[str, ...], ...] | None = None,
) -> LayoutBlock:
    return LayoutBlock(
        id=block_id,
        category=category,
        bbox=bbox,
        order=order,
        score=0.9,
        text=text,
        lines=tuple(lines),
        cells=tuple(cells),
        latex=latex,
        chart_table=chart_table,
    )


def cell(row: int, col: int, text: str) -> TableCell:
    return TableCell(row, col, 1, 1, BBox(col * 100, row * 30, col * 100 + 100, row * 30 + 30), text)


def summary(segments: Sequence[OcrSegment]) -> list[tuple[str, str]]:
    return [(s.segment.id, s.segment.text) for s in segments]


# ---------------------------------------------------------------- 版面模式


@pytest.mark.parametrize("category", [BlockCategory.TEXT, BlockCategory.TITLE, BlockCategory.HEADER_FOOTER])
def test_text_like_block_is_one_segment(category: BlockCategory) -> None:
    (segment,) = blocks_to_segments([block("7", category, text="Hello world")], STRUCTURE)
    assert segment == OcrSegment(segment.segment, "7", BOX, None)
    assert (segment.segment.id, segment.segment.text) == ("b7", "Hello world")


def test_line_breaks_inside_a_block_are_joined() -> None:
    blocks = [
        block("0", BlockCategory.TEXT, 0, text="Optical character\nrecognition output"),
        block("1", BlockCategory.TEXT, 1, text="识别结果通常以文本行\n为单位输出。\n"),
    ]
    assert summary(blocks_to_segments(blocks, STRUCTURE)) == [
        ("b0", "Optical character recognition output"),
        ("b1", "识别结果通常以文本行为单位输出。"),
    ]


def test_block_without_text_uses_its_lines() -> None:
    lines = [line(10, 10, 300, 30, "first part"), line(10, 36, 200, 56, "second part")]
    the_block = block("0", BlockCategory.TEXT, lines=lines)
    assert block_text(the_block) == "first part second part"
    assert summary(blocks_to_segments([the_block], STRUCTURE)) == [("b0", "first part second part")]


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "　"])
def test_blank_text_block_has_no_segment(text: str) -> None:
    assert blocks_to_segments([block("0", BlockCategory.TITLE, text=text)], STRUCTURE) == ()


def test_table_cells_become_segments_in_row_major_order() -> None:
    cells = [
        cell(1, 1, "120"),
        cell(0, 1, "Sales"),
        cell(1, 0, "North"),
        cell(0, 0, "Region"),
        cell(2, 0, " "),
    ]
    segments = blocks_to_segments([block("t", BlockCategory.TABLE, text="ignored", cells=cells)], STRUCTURE)
    assert summary(segments) == [
        ("bt-r0c0", "Region"),
        ("bt-r0c1", "Sales"),
        ("bt-r1c0", "North"),
        ("bt-r1c1", "120"),
    ]
    assert [s.cell for s in segments] == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert segments[3].bbox == BBox(100, 30, 200, 60)
    assert {s.block_id for s in segments} == {"t"}


def test_multiline_cell_text_is_joined() -> None:
    (segment,) = blocks_to_segments(
        [block("t", BlockCategory.TABLE, cells=[cell(0, 0, "two\nlines")])], STRUCTURE
    )
    assert segment.segment.text == "two lines"


def test_table_without_cell_grid_falls_back_to_one_segment() -> None:
    segments = blocks_to_segments([block("t", BlockCategory.TABLE, text="a b\nc d")], STRUCTURE)
    assert [(s.segment.id, s.segment.text, s.cell, s.bbox) for s in segments] == [
        ("bt", "a b c d", None, BOX)
    ]


def test_chart_table_cells_use_the_chart_bbox() -> None:
    chart = block(
        "c", BlockCategory.CHART, chart_table=(("year", "sales"), ("2025", ""), ("2026", "15", "extra"))
    )
    segments = blocks_to_segments([chart], STRUCTURE)
    assert summary(segments) == [
        ("bc-r0c0", "year"),
        ("bc-r0c1", "sales"),
        ("bc-r1c0", "2025"),
        ("bc-r2c0", "2026"),
        ("bc-r2c1", "15"),
        ("bc-r2c2", "extra"),
    ]
    assert {s.bbox for s in segments} == {BOX}
    assert [s.cell for s in segments][:2] == [(0, 0), (0, 1)]


def test_chart_without_table_has_no_segments() -> None:
    assert blocks_to_segments([block("c", BlockCategory.CHART, text="chart")], STRUCTURE) == ()


@pytest.mark.parametrize("category", [BlockCategory.FORMULA, BlockCategory.IMAGE])
def test_formula_and_image_are_not_translated(category: BlockCategory) -> None:
    blocks = [
        block("x", category, text="E = mc^2", latex="E = mc^2" if category is BlockCategory.FORMULA else None)
    ]
    assert blocks_to_segments(blocks, STRUCTURE) == ()


def test_segments_follow_block_order() -> None:
    blocks = [
        block("2", BlockCategory.TEXT, 2, text="third"),
        block("0", BlockCategory.TITLE, 0, text="first"),
        block("1", BlockCategory.TABLE, 1, cells=[cell(0, 0, "second")]),
        block("3", BlockCategory.IMAGE, 3),
        block("4", BlockCategory.HEADER_FOOTER, 4, text="fourth"),
    ]
    assert summary(blocks_to_segments(blocks, STRUCTURE)) == [
        ("b0", "first"),
        ("b1-r0c0", "second"),
        ("b2", "third"),
        ("b4", "fourth"),
    ]


def test_segment_ids_are_unique_and_stable() -> None:
    raw: list[object] = [
        {"id": "0", "category": "text", "bbox": [0, 0, 100, 20], "order": 0, "score": 1, "text": "a"},
        {
            "id": "1",
            "category": "table",
            "bbox": [0, 30, 200, 90],
            "order": 1,
            "score": 1,
            "cells": [
                {"row": r, "col": c, "bbox": [0, 30, 10, 40], "text": f"{r}{c}"}
                for r in range(3)
                for c in range(3)
            ],
        },
        {
            "id": "10",
            "category": "chart",
            "bbox": [0, 100, 200, 200],
            "order": 2,
            "score": 1,
            "chart_table": [["x", "y"]],
        },
        {"id": "11", "category": "title", "bbox": [0, 210, 100, 230], "order": 3, "score": 1, "text": "t"},
    ]
    blocks = decode_blocks(raw, width=300, height=300)
    first = blocks_to_segments(blocks, STRUCTURE)
    second = blocks_to_segments(blocks, STRUCTURE)
    ids = [s.segment.id for s in first]
    assert len(ids) == len(set(ids)) == 13
    assert first == second


def test_duplicate_block_ids_keep_the_first_segment(caplog: pytest.LogCaptureFixture) -> None:
    blocks = [
        block("x", BlockCategory.TEXT, 0, text="first"),
        block("x", BlockCategory.TEXT, 1, text="second"),
    ]
    with caplog.at_level(logging.WARNING):
        assert summary(blocks_to_segments(blocks, STRUCTURE)) == [("bx", "first")]
    assert "段号 bx 重复" in caplog.text


# ---------------------------------------------------------------- 文本模式


def test_text_mode_merges_lines_into_paragraphs() -> None:
    first = stack(40, 20, [500, 500, 200], texts=["one", "two", "three"])
    second = stack(40, 120, [500, 300], texts=["four", "five"])
    # OCR 进程把全部文本行放进一个正文块，顺序不限。
    text_block = block("0", BlockCategory.TEXT, bbox=BBox(0, 0, 800, 600), lines=[*second, *first])
    segments = blocks_to_segments([text_block], TEXT)
    assert summary(segments) == [("p1", "one two three"), ("p2", "four five")]
    assert [s.bbox for s in segments] == [BBox(40, 20, 540, 92), BBox(40, 120, 540, 166)]
    assert {s.block_id for s in segments} == {"0"}
    assert all(s.cell is None for s in segments)


def test_text_mode_paragraph_belongs_to_the_block_of_its_first_line() -> None:
    rows = stack(40, 20, [500, 500], texts=["top", "bottom"])
    blocks = [
        block("b", BlockCategory.TEXT, 1, bbox=rows[1].bbox, lines=[rows[1]]),
        block("a", BlockCategory.TEXT, 0, bbox=rows[0].bbox, lines=[rows[0]]),
    ]
    (segment,) = blocks_to_segments(blocks, TEXT)
    assert (segment.segment.text, segment.block_id) == ("top bottom", "a")


def test_text_mode_block_without_lines_counts_as_one_line() -> None:
    blocks = [
        block("a", BlockCategory.TEXT, 0, text="first\nline", bbox=BBox(40, 20, 540, 40)),
        block("b", BlockCategory.TEXT, 1, text="second line", bbox=BBox(40, 46, 300, 66)),
        block("c", BlockCategory.TEXT, 2, text="  ", bbox=BBox(40, 200, 300, 220)),
    ]
    assert summary(blocks_to_segments(blocks, TEXT)) == [("p1", "first line second line")]


def test_text_mode_keeps_layout_rules_for_other_categories() -> None:
    rows = stack(40, 100, [500], texts=["body"])
    blocks = [
        block("t", BlockCategory.TITLE, 0, text="title"),
        block("x", BlockCategory.TEXT, 1, lines=rows),
        block("tab", BlockCategory.TABLE, 2, cells=[cell(0, 0, "cell")]),
        block("img", BlockCategory.IMAGE, 3),
    ]
    assert summary(blocks_to_segments(blocks, TEXT)) == [
        ("bt", "title"),
        ("p1", "body"),
        ("btab-r0c0", "cell"),
    ]


def test_structure_mode_does_not_merge_text_blocks() -> None:
    rows = stack(40, 20, [500, 500], texts=["one", "two"])
    blocks = [
        block("0", BlockCategory.TEXT, 0, lines=[rows[0]]),
        block("1", BlockCategory.TEXT, 1, lines=[rows[1]]),
    ]
    assert summary(blocks_to_segments(blocks, STRUCTURE)) == [("b0", "one"), ("b1", "two")]


def test_text_mode_uses_the_given_merger() -> None:
    class SplitAll:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def merge(self, lines: Sequence[TextLine]) -> tuple[Paragraph, ...]:
            self.calls.append(len(lines))
            return tuple(Paragraph((i,), item.bbox, item.text) for i, item in enumerate(lines))

    merger = SplitAll()
    rows = stack(40, 20, [500, 500], texts=["one", "two"])
    segments = blocks_to_segments([block("0", BlockCategory.TEXT, lines=rows)], TEXT, merger)
    assert merger.calls == [2]
    assert summary(segments) == [("p1", "one"), ("p2", "two")]
    # 参数不同的默认合并器也可以注入。
    strict = GeometricMerger(MergeParams(line_gap=0.1))
    assert summary(blocks_to_segments([block("0", BlockCategory.TEXT, lines=rows)], TEXT, strict)) == [
        ("p1", "one"),
        ("p2", "two"),
    ]


def test_text_mode_skips_invalid_paragraphs_from_a_custom_merger(caplog: pytest.LogCaptureFixture) -> None:
    class Broken:
        def merge(self, lines: Sequence[TextLine]) -> tuple[Paragraph, ...]:
            return (
                Paragraph((5,), BOX, "out of range"),
                Paragraph((), BOX, "no lines"),
                Paragraph((0,), BOX, "  "),
                Paragraph((0,), BOX, "kept"),
            )

    rows = stack(40, 20, [500], texts=["one"])
    with caplog.at_level(logging.WARNING):
        segments = blocks_to_segments([block("0", BlockCategory.TEXT, lines=rows)], TEXT, Broken())
    assert summary(segments) == [("p1", "kept")]
    assert caplog.text.count("引用了不存在的文本行") == 2


def test_text_mode_without_text_blocks() -> None:
    assert blocks_to_segments([], TEXT) == ()
    assert blocks_to_segments([block("i", BlockCategory.IMAGE)], TEXT) == ()
