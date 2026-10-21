"""把产线输出（extract.RawPage）转换成输出版面块（blocks.OutBlock），架构文档 8.4 节。

版面模式
--------

1. 版面块按 parsing_res_list 的顺序输出，这就是 PP-StructureV3 用 xycut 给出的阅读顺序；order 从 0 连续编号。
2. 类别由 labels.category_for 映射。置信度取与块交并比最大（不小于 0.5）的版面检测框的 score；
   PaddleX 在解析阶段可能改写标签（footnote → text 等），所以匹配只看位置。
   找不到时取块内文本行置信度的平均值。
3. 文本行：整页 OCR 的每一行归给与它重叠面积最大的版面块，重叠面积不到行面积一半的不归给任何块。
   文本类块（正文、标题、页眉页脚，以及公式、图表）带上这些行，块内按阅读顺序排列（先按行从上到下，
   同一行的碎片从左到右）；text 是这些行的文字用换行连接，段落拼接由主进程按语言规则完成。
   块内没有文本行时 text 取 PaddleX 给出的 content。表格（有单元格网格时）、图片块内的行不输出。
4. 表格：content 即 pred_html，与 table_res_list 中 pred_html 相同的那一项配对，拿到单元格框；
   网格解析与对齐见 table_html。text 是各行单元格文字，行内以空格、行间以换行连接。
   没开表格识别时表格块的 content 只是块内文字，这时不输出 cells，按文本类块输出 text 和 lines，
   主进程把整块当作一个段。
5. 公式：与公式块交并比最大（不小于 0.5）的公式识别结果给出 latex；没有时 latex 为 null。
6. 图表：开了图表识别时 content 是 PP-Chart2Table 输出的数据表文本，解析成 chart_table；否则为 null。
7. 不属于任何版面块的文本行，每行单独成为一个正文块，排在全部版面块之后（按从上到下、从左到右）。
   PP-DocLayout 是在文档图像上训练的，界面截图里的按钮、菜单之类的文字常常不在任何版面块里，
   PaddleX 的解析结果会把它们丢掉；这里保留下来，翻译时不会漏字。

文本模式
--------

只输出一个正文块（主进程的约定）：bbox 为整张截图，order 为 0，lines 为全部非空文本行，text 为空串，
score 为各行置信度的平均值。没有任何文本行时不输出块。

包围盒都经 CoordinateMap 换算回原始截图；换算后面积为零的行、单元格和块被丢弃。
"""

import logging
from collections.abc import Sequence

from lst.contracts.ocr import BlockCategory
from lst.workers.ocr_worker.blocks import OutBlock, OutCell, OutLine
from lst.workers.ocr_worker.extract import RawBlock, RawLine, RawPage, RawTable
from lst.workers.ocr_worker.geometry import Box, CoordinateMap, box_area, intersection_area, iou
from lst.workers.ocr_worker.labels import category_for
from lst.workers.ocr_worker.table_html import align_cells, parse_table_html

log = logging.getLogger(__name__)

# 文本行归属版面块的门槛：行面积中落在块内的比例。
LINE_IN_BLOCK = 0.5
# 版面块与检测框、公式结果配对的交并比门槛。
MATCH_IOU = 0.5


def parse_chart_table(text: str) -> list[list[str]] | None:
    """把 PP-Chart2Table 的输出解析成按行排列的二维表；不是数据表时返回 None。

    输出是逐行的文本，单元格用 | 分隔（形如 Markdown 表格，行首行尾的 | 可有可无）。
    只含 -、:、| 和空白的分隔行跳过。整段文字里没有 |，或者含有 U+FFFD（解码失败的字节，本机 CPU 上
    PP-Chart2Table 的输出就是这样的乱码）时，不当作数据表，记一条 WARNING（不写内容）。
    """
    if "|" not in text or chr(0xFFFD) in text:
        if text.strip():
            log.warning("图表识别的输出不是数据表（%d 个字符），已丢弃", len(text))
        return None
    rows: list[list[str]] = []
    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line or set(line) <= set("-:| \t"):
            continue
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        rows.append([cell.strip() for cell in line.split("|")])
    return rows or None


def reading_order(lines: Sequence[RawLine]) -> list[RawLine]:
    """块内文本行的阅读顺序：竖直方向重叠超过较矮一行一半高度的行算同一行，行内从左到右，行间从上到下。"""
    rows: list[list[RawLine]] = []
    for line in sorted(lines, key=lambda item: (item.box[1] + item.box[3]) / 2):
        if rows:
            last = rows[-1]
            top, bottom = min(item.box[1] for item in last), max(item.box[3] for item in last)
            overlap = min(bottom, line.box[3]) - max(top, line.box[1])
            shorter = min(bottom - top, line.box[3] - line.box[1])
            if shorter > 0 and overlap > 0.5 * shorter:
                last.append(line)
                continue
        rows.append([line])
    return [line for row in rows for line in sorted(row, key=lambda item: item.box[0])]


def _assign_lines(
    lines: Sequence[RawLine], blocks: Sequence[RawBlock]
) -> tuple[list[list[RawLine]], list[RawLine]]:
    """把文本行分给版面块；返回（每个块的行，不属于任何块的行）。"""
    owned: list[list[RawLine]] = [[] for _ in blocks]
    orphans: list[RawLine] = []
    for line in lines:
        area = box_area(line.box)
        best, best_ratio = -1, 0.0
        for index, block in enumerate(blocks):
            ratio = intersection_area(line.box, block.box) / area if area > 0 else 0.0
            if ratio > best_ratio:
                best, best_ratio = index, ratio
        if best >= 0 and best_ratio >= LINE_IN_BLOCK:
            owned[best].append(line)
        else:
            orphans.append(line)
    return owned, orphans


def _best_match[T](box: Box, candidates: Sequence[tuple[Box, T]]) -> T | None:
    best: T | None = None
    best_iou = MATCH_IOU
    for candidate_box, value in candidates:
        overlap = iou(box, candidate_box)
        if overlap >= best_iou:
            best, best_iou = value, overlap
    return best


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


class _Builder:
    """逐个追加输出块，order 连续编号。"""

    def __init__(self, cmap: CoordinateMap) -> None:
        self.cmap = cmap
        self.blocks: list[OutBlock] = []

    def out_lines(self, lines: Sequence[RawLine]) -> list[OutLine]:
        out: list[OutLine] = []
        for line in lines:
            box = self.cmap.to_pixels(line.box)
            if box is not None:
                out.append(OutLine(box, line.text, line.score))
        return out

    def add(
        self,
        category: BlockCategory,
        box: Box,
        score: float,
        *,
        text: str = "",
        lines: Sequence[OutLine] = (),
        cells: Sequence[OutCell] = (),
        latex: str | None = None,
        chart_table: Sequence[Sequence[str]] | None = None,
    ) -> None:
        pixels = self.cmap.to_pixels(box)
        if pixels is None:
            log.debug("版面块换算后落在截图之外，已丢弃")
            return
        order = len(self.blocks)
        self.blocks.append(OutBlock(category, pixels, order, score, text, lines, cells, latex, chart_table))


def _table_cells(builder: _Builder, block: RawBlock, table: RawTable | None) -> tuple[list[OutCell], str]:
    html = table.html if table is not None else block.content
    grid = parse_table_html(html)
    boxes = align_cells(grid, table.cell_boxes if table is not None else (), block.box)
    cells: list[OutCell] = []
    for cell, box in zip(grid.cells, boxes, strict=True):
        pixels = builder.cmap.to_pixels(box)
        if pixels is not None:
            cells.append(OutCell(cell.row, cell.col, cell.row_span, cell.col_span, pixels, cell.text))
    rows: dict[int, list[str]] = {}
    for cell in sorted(grid.cells, key=lambda c: (c.row, c.col)):
        if cell.text:
            rows.setdefault(cell.row, []).append(cell.text)
    text = "\n".join(" ".join(rows[row]) for row in sorted(rows))
    return cells, text


def convert_structure(page: RawPage, cmap: CoordinateMap) -> list[OutBlock]:
    """版面模式的转换，见模块说明。"""
    text_lines = [line for line in page.lines if line.is_text and line.text.strip()]
    owned, orphans = _assign_lines(text_lines, page.blocks)
    regions = [(region.box, region.score) for region in page.regions]
    formulas = [(formula.box, formula.latex) for formula in page.formulas]
    unused_tables = list(page.tables)
    builder = _Builder(cmap)

    for block, block_lines in zip(page.blocks, owned, strict=True):
        ordered = reading_order(block_lines)
        has_text = bool(ordered) or bool(block.content.strip())
        category = category_for(block.label, has_text)
        score = _best_match(block.box, regions)
        if score is None:
            score = _mean([line.score for line in ordered])
        if category is BlockCategory.TABLE:
            table = next((t for t in unused_tables if t.html == block.content), None)
            if table is not None:
                unused_tables.remove(table)
            if table is not None or "<table" in block.content.lower():
                cells, text = _table_cells(builder, block, table)
                builder.add(category, block.box, score, text=text, cells=cells)
                continue
            # 没开表格识别：content 是块内文字，没有单元格网格，整块按文字输出。
        elif category is BlockCategory.IMAGE:
            builder.add(category, block.box, score)
            continue
        lines = builder.out_lines(ordered)
        text = "\n".join(line.text for line in lines) if lines else block.content.strip()
        if category is BlockCategory.FORMULA:
            builder.add(
                category, block.box, score, text=text, lines=lines, latex=_best_match(block.box, formulas)
            )
        elif category is BlockCategory.CHART:
            chart_table = parse_chart_table(block.content) if page.chart_recognized else None
            builder.add(category, block.box, score, text=text, lines=lines, chart_table=chart_table)
        else:
            builder.add(category, block.box, score, text=text, lines=lines)

    for line in reading_order(orphans):
        lines = builder.out_lines([line])
        if lines:
            builder.add(BlockCategory.TEXT, line.box, line.score, text=line.text, lines=lines)
    return builder.blocks


def convert_text(page: RawPage, cmap: CoordinateMap) -> list[OutBlock]:
    """文本模式的转换：全部文本行放进一个覆盖整张截图的正文块，见模块说明。"""
    builder = _Builder(cmap)
    lines = builder.out_lines([line for line in page.lines if line.text.strip()])
    if not lines:
        return []
    width, height = cmap.original_size
    return [
        OutBlock(
            BlockCategory.TEXT, (0, 0, width, height), 0, _mean([line.score for line in lines]), lines=lines
        )
    ]
