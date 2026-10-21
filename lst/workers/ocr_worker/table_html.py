"""表格结果的解析：pred_html → 单元格网格，再与 cell_box_list 对齐出每格的包围盒（架构文档 8.4 节）。

网格
----

用标准库 html.parser 读 pred_html，按 HTML 表格的排布规则给每个 td/th 分配行列号：逐行从左到右放置，
跳过被上方单元格 rowspan 占住的位置，单元格占据 rowspan × colspan 个格位。每行单元格数不同的不规则表格
照常处理，列数取各行占到的最大列号。单元格的文本是 td 内全部文字（<b> 等内联标签去掉），首尾空白去掉；
td 外的文字、嵌套表格的结构都不参与网格。

对齐
----

PP-StructureV3 给出的 cell_box_list 与 HTML 的 td 不保证一一对应：端到端的无线表格模型逐个 td 输出框，
有线表格则来自单元格检测模型，按“先行后列”排好序，个数可能多也可能少。规则如下：

1. 框数与单元格数相等：按顺序一一对应。PaddleX 填写 HTML 文字时也是按这个顺序把框与 td 对应起来的。
2. 不相等：从框的边缘估计网格线。把全部框的左右边缘聚类成竖线、上下边缘聚类成横线，竖线数等于列数 + 1、
   横线数等于行数 + 1 时采用；某个方向对不上时，在表格范围内等分。单元格的框取它跨过的网格线围成的矩形。
   这种回退记一条 WARNING。
3. 没有任何框：在表格块的包围盒内等分。

聚类的容差是框高中位数的 0.25 倍（至少 4 像素）：相邻单元格共用的边框线，两侧框的边缘只差几个像素，
而真正的两条网格线之间至少隔一个字高。
"""

import logging
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

from lst.workers.ocr_worker.geometry import Box, union_box

log = logging.getLogger(__name__)

# rowspan、colspan 的上限。模型偶尔给出离谱的跨度，限制住它，网格不会因此膨胀到无法处理。
MAX_SPAN = 1000


@dataclass(frozen=True, slots=True)
class GridCell:
    """网格中的一个单元格，行列号从 0 开始。"""

    row: int
    col: int
    row_span: int
    col_span: int
    text: str


@dataclass(frozen=True, slots=True)
class TableGrid:
    cells: tuple[GridCell, ...]
    n_rows: int
    n_cols: int


def _span(value: str | None) -> int:
    try:
        span = int((value or "1").strip())
    except ValueError:
        return 1
    return min(max(span, 1), MAX_SPAN)


class _TableParser(HTMLParser):
    """收集最外层表格的行与单元格：每个单元格是 (rowspan, colspan, 文本)。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[int, int, str]]] = []
        self._table_depth = 0
        self._in_row = False
        self._cell: tuple[int, int] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table_depth += 1
            return
        if self._table_depth != 1:
            return
        if tag == "tr":
            self._finish_cell()
            self.rows.append([])
            self._in_row = True
        elif tag in ("td", "th"):
            self._finish_cell()
            if not self._in_row:
                # td 前缺了 <tr>：按新起一行处理。
                self.rows.append([])
                self._in_row = True
            attributes = dict(attrs)
            self._cell = (_span(attributes.get("rowspan")), _span(attributes.get("colspan")))
            self._text = []
        elif tag == "br" and self._cell is not None:
            self._text.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._table_depth == 1:
                self._finish_cell()
                self._in_row = False
            self._table_depth = max(0, self._table_depth - 1)
            return
        if self._table_depth != 1:
            return
        if tag in ("td", "th"):
            self._finish_cell()
        elif tag == "tr":
            self._finish_cell()
            self._in_row = False

    def handle_data(self, data: str) -> None:
        # 嵌套表格里的文字算作外层单元格的文字。
        if self._cell is not None and self._table_depth >= 1:
            self._text.append(data)

    def _finish_cell(self) -> None:
        if self._cell is not None:
            row_span, col_span = self._cell
            text = " ".join("".join(self._text).split())
            self.rows[-1].append((row_span, col_span, text))
            self._cell = None
            self._text = []

    def close(self) -> None:
        super().close()
        self._finish_cell()


def parse_table_html(html: str) -> TableGrid:
    """把 pred_html 解析成单元格网格，见模块说明。没有单元格时返回空网格。"""
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    n_rows = len(parser.rows)
    occupied: set[tuple[int, int]] = set()
    cells: list[GridCell] = []
    n_cols = 0
    for r, row in enumerate(parser.rows):
        c = 0
        for row_span, col_span, text in row:
            while (r, c) in occupied:
                c += 1
            # 跨出表格末行的 rowspan 按 HTML 的规则截到末行。
            span = min(row_span, n_rows - r)
            cells.append(GridCell(r, c, span, col_span, text))
            for dr in range(span):
                for dc in range(col_span):
                    occupied.add((r + dr, c + dc))
            c += col_span
            n_cols = max(n_cols, c)
    return TableGrid(tuple(cells), n_rows if cells else 0, n_cols)


# ---------------------------------------------------------------- 与 cell_box_list 对齐


def _cluster(values: Sequence[float], tolerance: float) -> list[float]:
    """一维聚类：排序后相邻值相差不超过 tolerance 的归为一组，返回各组的平均值（升序）。"""
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def _uniform(start: float, end: float, parts: int) -> list[float]:
    step = (end - start) / parts
    return [start + step * i for i in range(parts + 1)]


def _grid_lines(
    boxes: Sequence[Box], extent: Box, n_rows: int, n_cols: int
) -> tuple[list[float], list[float]]:
    """从框的边缘估计竖线与横线；某个方向的线数对不上时在 extent 内等分。"""
    heights = [b[3] - b[1] for b in boxes if b[3] > b[1]]
    tolerance = max(4.0, 0.25 * statistics.median(heights)) if heights else 4.0
    xs = _cluster([v for b in boxes for v in (b[0], b[2])], tolerance) if boxes else []
    ys = _cluster([v for b in boxes for v in (b[1], b[3])], tolerance) if boxes else []
    if len(xs) != n_cols + 1:
        xs = _uniform(extent[0], extent[2], n_cols)
    if len(ys) != n_rows + 1:
        ys = _uniform(extent[1], extent[3], n_rows)
    return xs, ys


def align_cells(grid: TableGrid, cell_boxes: Sequence[Box], table_box: Box) -> list[Box]:
    """给网格中的每个单元格配一个包围盒（与 grid.cells 同序），见模块说明。

    cell_boxes 与 table_box 都是产线输出坐标。
    """
    cells = grid.cells
    if not cells:
        return []
    if len(cell_boxes) == len(cells):
        return list(cell_boxes)
    if cell_boxes:
        log.warning(
            "表格单元格框数（%d）与 HTML 单元格数（%d）不一致，按框边缘估计网格线",
            len(cell_boxes),
            len(cells),
        )
    extent = union_box(cell_boxes) or table_box
    xs, ys = _grid_lines(cell_boxes, extent, grid.n_rows, grid.n_cols)
    return [
        (xs[cell.col], ys[cell.row], xs[min(cell.col + cell.col_span, grid.n_cols)],
         ys[min(cell.row + cell.row_span, grid.n_rows)])
        for cell in cells
    ]  # fmt: skip
