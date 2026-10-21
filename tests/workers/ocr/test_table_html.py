"""pred_html 解析与 cell_box_list 对齐（架构文档 8.4 节）。"""

import logging

import pytest

from lst.workers.ocr_worker.geometry import Box
from lst.workers.ocr_worker.table_html import MAX_SPAN, align_cells, parse_table_html

# 本机 PP-StructureV3 对两行三列表格的真实输出。
REAL_HTML = (
    "<html><body><table><tr><td>名称</td><td>数量</td><td>价格</td></tr>"
    "<tr><td>苹果</td><td>12</td><td>3.50</td></tr></table></body></html>"
)
REAL_CELL_BOXES: list[Box] = [
    (79.0, 380.4, 338.6, 439.6),
    (338.7, 380.4, 598.8, 439.6),
    (598.7, 380.3, 856.9, 439.4),
    (79.1, 439.5, 338.8, 498.1),
    (338.9, 439.5, 599.1, 498.0),
    (598.8, 439.4, 857.0, 498.0),
]


def cells_of(html: str) -> list[tuple[int, int, int, int, str]]:
    return [(c.row, c.col, c.row_span, c.col_span, c.text) for c in parse_table_html(html).cells]


# ---------------------------------------------------------------- 网格


def test_real_two_by_three_table() -> None:
    grid = parse_table_html(REAL_HTML)
    assert (grid.n_rows, grid.n_cols) == (2, 3)
    assert [(c.row, c.col, c.text) for c in grid.cells] == [
        (0, 0, "名称"),
        (0, 1, "数量"),
        (0, 2, "价格"),
        (1, 0, "苹果"),
        (1, 1, "12"),
        (1, 2, "3.50"),
    ]
    assert all(c.row_span == 1 and c.col_span == 1 for c in grid.cells)


def test_rowspan_and_colspan_occupy_positions() -> None:
    html = (
        "<table><tr><td rowspan='2'>A</td><td colspan=\"2\">B</td></tr>"
        "<tr><td>C</td><td>D</td></tr>"
        "<tr><td>E</td><td colspan='2'>F</td></tr></table>"
    )
    grid = parse_table_html(html)
    assert cells_of(html) == [
        (0, 0, 2, 1, "A"),
        (0, 1, 1, 2, "B"),
        (1, 1, 1, 1, "C"),
        (1, 2, 1, 1, "D"),
        (2, 0, 1, 1, "E"),
        (2, 1, 1, 2, "F"),
    ]
    assert (grid.n_rows, grid.n_cols) == (3, 3)


def test_rowspan_in_middle_column_shifts_later_rows() -> None:
    html = (
        "<table><tr><td>a</td><td rowspan=3>b</td><td>c</td></tr>"
        "<tr><td>d</td><td>e</td></tr><tr><td>f</td><td>g</td></tr></table>"
    )
    assert cells_of(html) == [
        (0, 0, 1, 1, "a"),
        (0, 1, 3, 1, "b"),
        (0, 2, 1, 1, "c"),
        (1, 0, 1, 1, "d"),
        (1, 2, 1, 1, "e"),
        (2, 0, 1, 1, "f"),
        (2, 2, 1, 1, "g"),
    ]


def test_irregular_rows() -> None:
    html = (
        "<table><tr><td>1</td></tr><tr><td>2</td><td>3</td><td>4</td></tr>"
        "<tr></tr><tr><td>5</td><td>6</td></tr></table>"
    )
    grid = parse_table_html(html)
    assert (grid.n_rows, grid.n_cols) == (4, 3)
    assert [(c.row, c.col) for c in grid.cells] == [(0, 0), (1, 0), (1, 1), (1, 2), (3, 0), (3, 1)]


def test_rowspan_past_last_row_is_clipped() -> None:
    html = "<table><tr><td rowspan='5'>x</td><td>y</td></tr><tr><td>z</td></tr></table>"
    assert cells_of(html) == [(0, 0, 2, 1, "x"), (0, 1, 1, 1, "y"), (1, 1, 1, 1, "z")]


@pytest.mark.parametrize(
    ("attribute", "span"), [("x", 1), ("0", 1), ("-3", 1), ("", 1), (" 2 ", 2), ("99999", MAX_SPAN)]
)
def test_invalid_or_huge_spans(attribute: str, span: int) -> None:
    html = f"<table><tr><td colspan='{attribute}'>a</td></tr></table>"
    assert parse_table_html(html).cells[0].col_span == span


def test_text_extraction_rules() -> None:
    html = (
        "<table><thead><tr><th> <b>Name</b> </th><th>A &amp; B</th></tr></thead>"
        "<tbody><tr><td>line1<br>line2</td><td>  many   spaces\n here </td></tr></tbody></table>"
    )
    assert [c.text for c in parse_table_html(html).cells] == [
        "Name",
        "A & B",
        "line1 line2",
        "many spaces here",
    ]


def test_formula_and_image_markup_inside_cells() -> None:
    html = (
        "<table><tr><td>$x^2$</td><td>"
        '<div style="text-align: center;"><img src="imgs/a.jpg" alt="Image" /></div>'
        "</td></tr></table>"
    )
    assert [c.text for c in parse_table_html(html).cells] == ["$x^2$", ""]


def test_td_without_tr_starts_a_row() -> None:
    assert cells_of("<table><td>a</td><td>b</td></table>") == [(0, 0, 1, 1, "a"), (0, 1, 1, 1, "b")]


def test_nested_table_text_joins_outer_cell() -> None:
    html = "<table><tr><td>outer<table><tr><td>inner</td></tr></table></td><td>next</td></tr></table>"
    assert cells_of(html) == [(0, 0, 1, 1, "outerinner"), (0, 1, 1, 1, "next")]


@pytest.mark.parametrize("html", ["", "plain text", "<table></table>", "<html><body></body></html>"])
def test_no_cells(html: str) -> None:
    grid = parse_table_html(html)
    assert grid.cells == ()
    assert (grid.n_rows, grid.n_cols) == (0, 0)


# ---------------------------------------------------------------- 对齐


def test_equal_counts_align_in_order() -> None:
    grid = parse_table_html(REAL_HTML)
    assert align_cells(grid, REAL_CELL_BOXES, (78, 378, 859, 499)) == REAL_CELL_BOXES


def test_missing_box_falls_back_to_grid_lines(caplog: pytest.LogCaptureFixture) -> None:
    grid = parse_table_html(REAL_HTML)
    boxes = REAL_CELL_BOXES[:4] + REAL_CELL_BOXES[5:]  # 少了第二行第二格
    with caplog.at_level(logging.WARNING, logger="lst.workers.ocr_worker.table_html"):
        aligned = align_cells(grid, boxes, (78, 378, 859, 499))
    assert any("不一致" in r.getMessage() for r in caplog.records)
    # 竖线由各框的左右边缘聚类得到：约 79、338.8、598.8、857；横线约 380.4、439.5、498。
    expected = [(79, 380), (339, 380), (599, 380), (79, 439), (339, 439), (599, 439)]
    for (x0, y0, x1, y1), (ex, ey) in zip(aligned, expected, strict=True):
        assert abs(x0 - ex) < 2 and abs(y0 - ey) < 2
        assert x1 - x0 > 250 and y1 - y0 > 55
    assert abs(aligned[4][2] - 599) < 2  # 缺框的那一格仍然拿到了正确的右边线


def test_spanning_cell_gets_union_of_grid_lines() -> None:
    html = (
        "<table><tr><td colspan='2'>head</td></tr>"
        "<tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"
    )
    grid = parse_table_html(html)
    boxes: list[Box] = [(0, 0, 200, 50), (0, 50, 100, 100), (100, 50, 200, 100), (0, 100, 100, 150)]
    aligned = align_cells(grid, boxes, (0, 0, 200, 150))  # 4 个框、5 个单元格：按网格线
    assert aligned[0] == pytest.approx((0, 0, 200, 50))
    assert aligned[4] == pytest.approx((100, 100, 200, 150))


def test_unclusterable_axis_splits_evenly() -> None:
    grid = parse_table_html("<table><tr><td>a</td><td>b</td></tr></table>")
    # 只有一个框，竖线只聚出 2 条（需要 3 条）：横向在框的范围内等分；横线 2 条正好。
    aligned = align_cells(grid, [(10, 20, 110, 60)], (0, 0, 500, 500))
    assert aligned == [pytest.approx((10, 20, 60, 60)), pytest.approx((60, 20, 110, 60))]


def test_no_boxes_split_table_box() -> None:
    grid = parse_table_html("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>")
    aligned = align_cells(grid, [], (0, 0, 100, 40))
    assert aligned == [
        pytest.approx((0, 0, 50, 20)),
        pytest.approx((50, 0, 100, 20)),
        pytest.approx((0, 20, 50, 40)),
        pytest.approx((50, 20, 100, 40)),
    ]


def test_empty_grid_has_no_boxes() -> None:
    assert align_cells(parse_table_html(""), REAL_CELL_BOXES, (0, 0, 1, 1)) == []
