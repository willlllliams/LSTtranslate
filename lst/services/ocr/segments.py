"""把版面块转换成参加竞速的段（需求 OCR-05、OV-03，架构文档第 7 章第 5 步）。

各类版面块的规则：

- 正文、标题、页眉页脚（版面模式）：一个块一个段，段号 ``b{块 id}``，范围是块的包围盒。
  PP-StructureV3 的块已经是段落级，块内的换行是排版折行，按 ``join_lines`` 的规则拼成一段
  （中日文直接相连，其他加空格）。块的 text 为空白时改用 lines；两者都为空白时不生成段。
- 正文（文本模式）：全部正文块的文本行一起交给段落合并器，每个段落一个段，段号 ``p{n}``
  （n 从 1 开始按阅读顺序编号），范围是段落的包围盒，所属块是段落首行所在的块。
  没有 lines 的正文块整体当作一行。其他类别的块仍按版面模式的规则处理。
- 表格：每个非空单元格一个段，段号 ``b{块 id}-r{行}c{列}``，范围是单元格的包围盒，cell=(行, 列)。
  表格没有单元格网格时退回为整块一个段，文字不会丢失。
- 图表：图表转换出的数据表中每个非空单元格一个段，段号写法同表格，cell 是数据表的行列号；
  范围用图表块的包围盒，覆盖窗口点开数据表时才渲染这些段。
- 公式、图片：不生成段，覆盖窗口显示原图（需求 OV-03）。

段按阅读顺序排列：块按 order，表格单元格按（行，列），文本模式的段落按合并器给出的顺序。
段号只由块 id、单元格行列号和段落序号决定，同一个识别结果每次转换得到相同的段号；
版面块 id 不含连字符（见 blocks.BLOCK_ID_PATTERN），三种写法互不重叠，段号在一个会话内唯一。
"""

import logging
from collections.abc import Iterator, Sequence
from typing import Final

from lst.contracts.ids import SegmentId
from lst.contracts.ocr import BBox, BlockCategory, LayoutBlock, OcrSegment, TextLine
from lst.contracts.text import join_lines
from lst.contracts.translation import Segment
from lst.services.ocr.config import OcrMode
from lst.services.ocr.paragraphs import GeometricMerger, ParagraphMerger

logger = logging.getLogger(__name__)

_DEFAULT_MERGER: Final = GeometricMerger()
_TEXT_LIKE: Final = frozenset({BlockCategory.TEXT, BlockCategory.TITLE, BlockCategory.HEADER_FOOTER})


def blocks_to_segments(
    blocks: Sequence[LayoutBlock], mode: OcrMode, merger: ParagraphMerger | None = None
) -> tuple[OcrSegment, ...]:
    """按模块文档的规则把版面块转换成段。merger 缺省时用默认参数的 GeometricMerger。"""
    ordered = sorted(blocks, key=lambda b: b.order)
    paragraph_merger = merger if merger is not None else _DEFAULT_MERGER
    out: list[OcrSegment] = []
    seen: set[str] = set()

    def add(segment_id: str, text: str, block_id: str, bbox: BBox, cell: tuple[int, int] | None) -> None:
        if segment_id in seen:
            logger.warning("段号 %s 重复，后出现的段已丢弃", segment_id)
            return
        seen.add(segment_id)
        out.append(OcrSegment(Segment(SegmentId(segment_id), text), block_id, bbox, cell))

    merged = False
    for block in ordered:
        if mode is OcrMode.TEXT and block.category is BlockCategory.TEXT:
            # 文本模式下全部正文块的行一起合并，段落排在第一个正文块的位置。
            if not merged:
                merged = True
                for segment_id, text, block_id, bbox in _paragraphs(ordered, paragraph_merger):
                    add(segment_id, text, block_id, bbox, None)
            continue
        for segment_id, text, bbox, cell in _block_segments(block):
            add(segment_id, text, block.id, bbox, cell)
    return tuple(out)


def block_text(block: LayoutBlock) -> str:
    """文本类块的段文本：text 中的折行按 join_lines 拼接；text 为空白时改用 lines。"""
    text = join_lines(block.text.splitlines())
    if text:
        return text
    return join_lines(line.text for line in block.lines)


def _block_segments(block: LayoutBlock) -> Iterator[tuple[str, str, BBox, tuple[int, int] | None]]:
    """一个块（不含文本模式的正文块）生成的段：（段号，文本，范围，单元格）。"""
    if block.category in _TEXT_LIKE:
        text = block_text(block)
        if text:
            yield f"b{block.id}", text, block.bbox, None
    elif block.category is BlockCategory.TABLE:
        if not block.cells:
            text = block_text(block)
            if text:
                yield f"b{block.id}", text, block.bbox, None
            return
        for cell in sorted(block.cells, key=lambda c: (c.row, c.col)):
            text = join_lines(cell.text.splitlines())
            if text:
                yield f"b{block.id}-r{cell.row}c{cell.col}", text, cell.bbox, (cell.row, cell.col)
    elif block.category is BlockCategory.CHART:
        for r, row in enumerate(block.chart_table or ()):
            for c, value in enumerate(row):
                text = join_lines(value.splitlines())
                if text:
                    yield f"b{block.id}-r{r}c{c}", text, block.bbox, (r, c)
    # 公式、图片不翻译。


def _paragraphs(
    blocks: Sequence[LayoutBlock], merger: ParagraphMerger
) -> Iterator[tuple[str, str, str, BBox]]:
    """文本模式：合并全部正文块的文本行，产出（段号，文本，所属块，范围）。"""
    lines: list[TextLine] = []
    owners: list[str] = []
    for block in blocks:
        if block.category is not BlockCategory.TEXT:
            continue
        if block.lines:
            lines.extend(block.lines)
            owners.extend(block.id for _ in block.lines)
        else:
            text = join_lines(block.text.splitlines())
            if text:
                lines.append(TextLine(block.bbox, text, block.score))
                owners.append(block.id)
    number = 0
    for paragraph in merger.merge(lines):
        if not paragraph.lines or not all(0 <= i < len(lines) for i in paragraph.lines):
            logger.warning("段落合并器返回的段落引用了不存在的文本行，已丢弃")
            continue
        if not paragraph.text.strip():
            continue
        number += 1
        yield f"p{number}", paragraph.text, owners[paragraph.lines[0]], paragraph.bbox
