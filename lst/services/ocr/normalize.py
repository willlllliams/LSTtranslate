"""结构归一化中间层（需求 OCR-11，OCR-05）。

位置与职责
==========

OCR 进程返回的版面块经 ``decode_blocks`` 校验之后、``blocks_to_segments`` 切分成段之前，先经过本模块的
若干步骤，把不同引擎、不同模式的输出整理成同一种结构：

    decode_blocks → 清洗 → 拆分 → 聚合 → blocks_to_segments

每一步实现 ``NormalizeStep`` 协议：``apply(blocks, context) -> blocks``，输入输出都是按 order 排列的版面块，
步骤之间只通过版面块传递信息。``NormalizePipeline`` 依次执行各步骤；``build_pipeline`` 按 ``[ocr_normalize]``
配置（NormalizeConfig）组装。全部步骤关闭时管线为空，版面块原样通过。

三步的顺序固定为清洗、拆分、聚合，理由是每一步都依赖前一步的结果是干净的：

1. 清洗先去掉空块、过小与低置信度的框和重复框。重复框不先去掉，拆分时会把同一行拆两次，聚合时两份
   同样的文字会被并进同一段；低置信度的噪声框夹在两栏之间时，还会让拆分判断“这里没有栏间空白”。
2. 拆分在聚合之前：一条横跨两栏的长行先拆成左右两行，聚合才能把它们分别并入各自那一栏的段落；
   反过来先聚合，跨栏长行会把两栏的行粘成一段，事后再也拆不开。
3. 聚合最后执行，它的输入是一行一个框、互不重复的文本行。

文本模式与版面模式
==================

文本模式（context.text_mode 为真）下引擎只给出一个覆盖整张截图的 text 块，lines 是全部文本行（见
blocks.py）。清洗与拆分作用于这些文本行；聚合的段落合并把它们合并成段落，每个段落成为一个新的 text 块
（id 为 ``p<序号>``，lines 是成员行，text 是成员行按 ``join_lines`` 拼成的段落文本）。之后
``blocks_to_segments`` 以 premerged=True 把每个段落块转换成一个段，段号仍是 ``p<序号>``，与没有中间层时
完全相同。聚合关闭时文本行保持原样，OcrModule 按“一行一段”切分（paragraphs.LineMerger）。

版面模式下，聚合先结合截图像素拆开 PP-StructureV3 粘连的菜单与控件，连续正文保持段落；
“合并相邻同类小块”是可选的一步，默认关闭。没有像素帧时保留原有版面块。

内置步骤
========

- ``CleanStep``（清洗）：丢弃文字为空白的行；面积小于 min_area 平方像素的行与块；置信度低于 min_score 的
  行与块；与另一框的交并比（IoU）不小于 duplicate_iou 的重复框（保留置信度高的一个，相同时保留先出现的）。
  文本类块（正文、标题、页眉页脚）清洗后既没有文字也没有文本行时整块丢弃；表格、公式、图表、图片块的
  内容不在文字里，只按面积、置信度与重复判断。文本模式的容器块（覆盖整张截图、score 是各行平均值）
  只清洗它的行。
- ``SplitStep``（拆分）：文本模式下，一行在水平方向跨过栏间空白时按空白拆成两行或更多行。做法见该类的文档。
  表格中粘连的单元格没有实现：单元格网格来自 PP-StructureV3 的 pred_html 与 cell_box_list，主进程手里只有
  单元格的框与文字，没有框线的像素信息，按文字推断网格线在合并单元格（跨行跨列）处会把正确的单元格拆坏，
  得不到可靠的结果。
- ``AggregateStep``（聚合）：文本模式下用段落合并器（默认 GeometricMerger，阈值来自配置）把文本行合并成
  段落块；版面模式下可选把阅读顺序上相邻、上下紧挨、左右对齐的两个正文块合并成一块。
- ``PassThrough``（原样通过）：不改变输入，作为占位与测试用。

清洗默认去掉空文字、零面积与 IoU 不小于 0.9 的重复框，置信度阈值为 0；
拆分关闭；聚合开启，段落合并的阈值与 MergeParams 的默认值相同，相邻块合并关闭。
没有像素帧的回归样本保持原有几何合并规则；实际截图另由 tests/fidelity 的人工标注约束。

日志只写数量，不写识别出的文本（需求 PRV-02）。
"""

import logging
import math
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field

from lst.contracts.ocr import BBox, BlockCategory, LayoutBlock, RawImage, TextLine
from lst.contracts.text import join_lines
from lst.services.ocr.appearance import ImageAppearance
from lst.services.ocr.paragraphs import GeometricMerger, MergeParams, ParagraphMerger

logger = logging.getLogger(__name__)

TEXT_LIKE: Final = frozenset({BlockCategory.TEXT, BlockCategory.TITLE, BlockCategory.HEADER_FOOTER})


# ---------------------------------------------------------------- 协议与上下文


@dataclass(frozen=True, slots=True)
class NormalizeContext:
    """步骤可用的上下文：识别结果对应的截图尺寸（物理像素）与是否文本模式。"""

    width: int
    height: int
    text_mode: bool
    image: RawImage | None = None


class NormalizeStep(Protocol):
    """结构归一化的一步。输入输出都是按 order 排列的版面块；不得修改输入（版面块本身不可变）。"""

    name: str

    def apply(self, blocks: Sequence[LayoutBlock], context: NormalizeContext) -> tuple[LayoutBlock, ...]: ...


class PassThrough:
    """原样通过。"""

    name = "pass"

    def apply(self, blocks: Sequence[LayoutBlock], context: NormalizeContext) -> tuple[LayoutBlock, ...]:
        del context
        return tuple(blocks)


# ---------------------------------------------------------------- 几何工具


def area(box: BBox) -> int:
    return max(0, box.width) * max(0, box.height)


def iou(a: BBox, b: BBox) -> float:
    """两个框的交并比。"""
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def union_box(boxes: Sequence[BBox]) -> BBox:
    return BBox(
        min(b.x0 for b in boxes), min(b.y0 for b in boxes), max(b.x1 for b in boxes), max(b.y1 for b in boxes)
    )


def is_container(block: LayoutBlock, context: NormalizeContext) -> bool:
    """文本模式的容器块：正文块且带文本行。它的 score 是各行的平均值，只清洗它的行。"""
    return context.text_mode and block.category is BlockCategory.TEXT and bool(block.lines)


def _mean_score(lines: Sequence[TextLine], fallback: float) -> float:
    return sum(line.score for line in lines) / len(lines) if lines else fallback


# ---------------------------------------------------------------- 清洗


def _dedupe[T](
    items: Sequence[T],
    box_of: Callable[[T], BBox],
    score_of: Callable[[T], float],
    threshold: float,
    same_group: Callable[[T, T], bool],
) -> list[T]:
    """去掉重复框：两项 IoU 不小于 threshold 且 same_group 为真时，保留置信度高的一项（相同时保留先出现的）
    。"""
    kept: list[T] = []
    for item in items:
        duplicate = None
        for index, other in enumerate(kept):
            if same_group(item, other) and iou(box_of(item), box_of(other)) >= threshold:
                duplicate = index
                break
        if duplicate is None:
            kept.append(item)
        elif score_of(item) > score_of(kept[duplicate]):
            kept[duplicate] = item
    return kept


def _line_box(line: TextLine) -> BBox:
    return line.bbox


def _line_score(line: TextLine) -> float:
    return line.score


def _block_box(block: LayoutBlock) -> BBox:
    return block.bbox


def _block_score(block: LayoutBlock) -> float:
    return block.score


def _always(_a: TextLine, _b: TextLine) -> bool:
    return True


class CleanStep:
    """清洗，规则见模块说明。duplicate_iou 大于 1 时不去重。"""

    name = "clean"

    def __init__(self, *, min_area: int = 1, min_score: float = 0.0, duplicate_iou: float = 0.9) -> None:
        self.min_area = min_area
        self.min_score = min_score
        self.duplicate_iou = duplicate_iou

    def _line_ok(self, line: TextLine) -> bool:
        return bool(line.text.strip()) and area(line.bbox) >= self.min_area and line.score >= self.min_score

    def _clean_lines(self, lines: Sequence[TextLine]) -> tuple[TextLine, ...]:
        kept = [line for line in lines if self._line_ok(line)]
        if self.duplicate_iou <= 1.0:
            kept = _dedupe(kept, _line_box, _line_score, self.duplicate_iou, _always)
        return tuple(kept)

    def apply(self, blocks: Sequence[LayoutBlock], context: NormalizeContext) -> tuple[LayoutBlock, ...]:
        out: list[LayoutBlock] = []
        dropped_lines = 0
        for block in blocks:
            lines = self._clean_lines(block.lines)
            dropped_lines += len(block.lines) - len(lines)
            if is_container(block, context):
                if lines:
                    out.append(replace(block, lines=lines, score=_mean_score(lines, block.score)))
                continue
            if area(block.bbox) < self.min_area or block.score < self.min_score:
                continue
            if block.category in TEXT_LIKE and not block.text.strip() and not lines:
                continue
            out.append(replace(block, lines=lines) if len(lines) != len(block.lines) else block)
        if self.duplicate_iou <= 1.0:

            def same_group(a: LayoutBlock, b: LayoutBlock) -> bool:
                return a.category is b.category and not is_container(a, context)

            out = _dedupe(out, _block_box, _block_score, self.duplicate_iou, same_group)
        if dropped_lines or len(out) != len(blocks):
            logger.debug("清洗：丢弃 %d 个版面块、%d 行", len(blocks) - len(out), dropped_lines)
        return tuple(out)


# ---------------------------------------------------------------- 拆分


def char_weight(char: str) -> float:
    """估计一个字符占的宽度（以全角字为 1）：东亚宽字符为 1，空白与其他字符为 0.5。"""
    return 1.0 if unicodedata.east_asian_width(char) in ("W", "F") else 0.5


def _free_intervals(
    span: tuple[float, float], covered: Sequence[tuple[float, float]]
) -> list[tuple[float, float]]:
    """span 里没有被 covered 盖住的区间，按从左到右排列。"""
    free: list[tuple[float, float]] = []
    cursor = span[0]
    for start, stop in sorted(covered):
        if stop <= cursor:
            continue
        if start > cursor:
            free.append((cursor, min(start, span[1])))
        cursor = max(cursor, stop)
        if cursor >= span[1]:
            break
    if cursor < span[1]:
        free.append((cursor, span[1]))
    return [(a, b) for a, b in free if b > a]


class SplitStep:
    """拆分跨越栏间空白的长行（只在文本模式下）。

    判断一行 L（高 h）是不是跨栏长行：

    1. 邻居：中线与 L 相差不超过 window 倍 h、又不与 L 在同一行（中线差大于 0.5 h）的其他行；
    2. 空白：L 的水平范围里没有被任何邻居盖住、宽度不小于 min_gap 倍 h、并且不贴着 L 两端的区间；
    3. 证据：空白左侧至少有一个邻居（右边缘不超过空白左端、与 L 的左段水平重叠），右侧同样至少有一个。
       只有一侧有邻居时，那是缩进或者表格的一列，不是栏间空白。

    找到空白后在文字里找切分点：按 char_weight 估计每个字符在框里的水平位置，取估计位置离空白中心最近的
    一段连续空白，距离不超过空白宽度与 2 h 中较大的一个才切分；文字里没有空白（例如中文）时不拆，
    因为按比例估计的字符位置误差可能有一两个字，切错了比不切更糟。拆出的行沿用原行的上下边与置信度，
    左段的右边缘取空白左端，右段的左边缘取空白右端。
    """

    name = "split"

    def __init__(self, *, min_gap: float = 1.5, window: float = 3.0) -> None:
        self.min_gap = min_gap
        self.window = window

    def apply(self, blocks: Sequence[LayoutBlock], context: NormalizeContext) -> tuple[LayoutBlock, ...]:
        if not context.text_mode:
            return tuple(blocks)
        out: list[LayoutBlock] = []
        for block in blocks:
            if not is_container(block, context):
                out.append(block)
                continue
            lines: list[TextLine] = []
            for index, line in enumerate(block.lines):
                neighbors = [other for k, other in enumerate(block.lines) if k != index]
                lines.extend(self.split_line(line, neighbors))
            if len(lines) != len(block.lines):
                logger.debug("拆分：%d 行拆成 %d 行", len(block.lines), len(lines))
                out.append(replace(block, lines=tuple(lines)))
            else:
                out.append(block)
        return tuple(out)

    def gutters(self, line: TextLine, others: Sequence[TextLine]) -> list[tuple[float, float]]:
        """line 的水平范围里的栏间空白，见类说明第 1 至 3 步。"""
        box = line.bbox
        h = box.height
        if h <= 0:
            return []
        center = (box.y0 + box.y1) / 2
        near = [
            other.bbox
            for other in others
            if 0.5 * h < abs((other.bbox.y0 + other.bbox.y1) / 2 - center) <= self.window * h
        ]
        covered = [(float(b.x0), float(b.x1)) for b in near if b.x1 > box.x0 and b.x0 < box.x1]
        result: list[tuple[float, float]] = []
        for start, stop in _free_intervals((float(box.x0), float(box.x1)), covered):
            if start <= box.x0 or stop >= box.x1 or stop - start < self.min_gap * h:
                continue
            left = any(b.x1 <= start and b.x1 > box.x0 for b in near)
            right = any(b.x0 >= stop and b.x0 < box.x1 for b in near)
            if left and right:
                result.append((start, stop))
        return result

    def split_line(self, line: TextLine, others: Sequence[TextLine]) -> list[TextLine]:
        """按栏间空白拆一行；不需要或不能拆时返回只含原行的列表。"""
        gutters = self.gutters(line, others)
        if not gutters:
            return [line]
        text = line.text
        weights = [char_weight(c) for c in text]
        total = sum(weights)
        if total <= 0:
            return [line]
        box = line.bbox
        h = box.height

        def position(index: int) -> float:
            return box.x0 + box.width * sum(weights[:index]) / total

        runs: list[tuple[int, int]] = []
        start: int | None = None
        for index, char in enumerate(text):
            if char.isspace():
                if start is None:
                    start = index
            elif start is not None:
                runs.append((start, index))
                start = None
        cuts: list[tuple[int, int, float, float]] = []
        for gx0, gx1 in gutters:
            middle = (gx0 + gx1) / 2
            tolerance = max(gx1 - gx0, 2 * h)
            best: tuple[float, tuple[int, int]] | None = None
            for run in runs:
                if run[0] == 0 or run[1] == len(text):
                    continue
                distance = abs((position(run[0]) + position(run[1])) / 2 - middle)
                if distance <= tolerance and (best is None or distance < best[0]):
                    best = (distance, run)
            if best is not None and all(best[1][0] != cut[0] for cut in cuts):
                cuts.append((best[1][0], best[1][1], gx0, gx1))
        if not cuts:
            return [line]
        cuts.sort()
        pieces: list[TextLine] = []
        text_start, x_start = 0, float(box.x0)
        for run_start, run_stop, gx0, gx1 in cuts:
            piece = text[text_start:run_start].strip()
            if piece:
                pieces.append(TextLine(BBox(int(x_start), box.y0, math.ceil(gx0), box.y1), piece, line.score))
            text_start, x_start = run_stop, gx1
        tail = text[text_start:].strip()
        if tail:
            pieces.append(TextLine(BBox(int(x_start), box.y0, box.x1, box.y1), tail, line.score))
        valid = [p for p in pieces if p.bbox.x1 > p.bbox.x0]
        return valid if len(valid) >= 2 else [line]


# ---------------------------------------------------------------- 聚合


def _line_height(block: LayoutBlock) -> float:
    """块内一行的高度：有文本行时取其中位数，否则按文字的行数平分块高。"""
    if block.lines:
        heights = sorted(line.bbox.height for line in block.lines)
        return float(heights[len(heights) // 2])
    rows = max(1, len(block.text.splitlines()))
    return block.bbox.height / rows


class AggregateStep:
    """聚合：文本模式下合并段落（paragraphs 为真时），版面模式下可选合并相邻正文块（merge_blocks 为真时）。"""

    name = "aggregate"

    def __init__(
        self,
        merger: ParagraphMerger | None = None,
        *,
        paragraphs: bool = True,
        merge_blocks: bool = False,
        block_gap: float = 0.8,
        block_overlap: float = 0.5,
        appearance: bool = True,
    ) -> None:
        self.merger: ParagraphMerger = merger if merger is not None else GeometricMerger()
        self.paragraphs = paragraphs
        self.merge_blocks = merge_blocks
        self.block_gap = block_gap
        self.block_overlap = block_overlap
        self.appearance = appearance

    def apply(self, blocks: Sequence[LayoutBlock], context: NormalizeContext) -> tuple[LayoutBlock, ...]:
        merger = self.merger
        if self.appearance and context.image is not None and isinstance(merger, GeometricMerger):
            appearance = ImageAppearance(context.image)
            blocks = tuple(
                replace(block, lines=tuple(part for line in block.lines for part in appearance.split(line)))
                if block.category in TEXT_LIKE and block.lines
                else block
                for block in blocks
            )
            merger = GeometricMerger(merger.params, appearance=appearance.of)
        if context.text_mode:
            return self._paragraph_blocks(blocks, merger) if self.paragraphs else tuple(blocks)
        if self.appearance and context.image is not None:
            blocks = self._structure_paragraphs(blocks, merger)
        return self._merge_adjacent(blocks) if self.merge_blocks else tuple(blocks)

    def _structure_paragraphs(
        self, blocks: Sequence[LayoutBlock], merger: ParagraphMerger
    ) -> tuple[LayoutBlock, ...]:
        """按截图颜色与间距拆开版面模型粘连的控件，连续正文仍共同成段。"""
        result: list[LayoutBlock] = []
        used = {block.id for block in blocks}
        for block in blocks:
            if block.category not in TEXT_LIKE or not block.lines:
                result.append(block)
                continue
            paragraphs = merger.merge(block.lines)
            for index, paragraph in enumerate(paragraphs):
                members = tuple(block.lines[i] for i in paragraph.lines)
                block_id = block.id
                if len(paragraphs) > 1:
                    block_id = f"{block.id}_p{index}"
                    while block_id in used:
                        block_id += "_"
                    used.add(block_id)
                result.append(
                    replace(
                        block,
                        id=block_id,
                        bbox=paragraph.bbox,
                        text=paragraph.text,
                        lines=members,
                        score=_mean_score(members, block.score),
                    )
                )
        return tuple(replace(block, order=index) for index, block in enumerate(result))

    # ---- 文本模式：段落 ----

    def _paragraph_blocks(
        self, blocks: Sequence[LayoutBlock], merger: ParagraphMerger
    ) -> tuple[LayoutBlock, ...]:
        """全部正文块的文本行一起合并成段落（与 segments 原来的做法相同），段落块排在第一个正文块的位置。"""
        ordered = sorted(blocks, key=lambda b: b.order)
        lines: list[TextLine] = []
        for block in ordered:
            if block.category is not BlockCategory.TEXT:
                continue
            if block.lines:
                lines.extend(block.lines)
            else:
                text = join_lines(block.text.splitlines())
                if text:
                    lines.append(TextLine(block.bbox, text, block.score))
        used = {b.id for b in ordered if b.category is not BlockCategory.TEXT}
        paragraphs: list[LayoutBlock] = []
        for paragraph in merger.merge(lines):
            if not paragraph.lines or not all(0 <= i < len(lines) for i in paragraph.lines):
                logger.warning("段落合并器返回的段落引用了不存在的文本行，已丢弃")
                continue
            if not paragraph.text.strip():
                continue
            members = tuple(lines[i] for i in paragraph.lines)
            block_id = f"p{len(paragraphs) + 1}"
            while block_id in used:
                block_id += "_"
            used.add(block_id)
            paragraphs.append(
                LayoutBlock(
                    id=block_id,
                    category=BlockCategory.TEXT,
                    bbox=paragraph.bbox,
                    order=0,
                    score=_mean_score(members, 1.0),
                    text=paragraph.text,
                    lines=members,
                )
            )
        result: list[LayoutBlock] = []
        inserted = False
        for block in ordered:
            if block.category is BlockCategory.TEXT:
                if not inserted:
                    inserted = True
                    result.extend(paragraphs)
                continue
            result.append(block)
        return tuple(replace(block, order=index) for index, block in enumerate(result))

    # ---- 版面模式：相邻块 ----

    def _adjacent(self, upper: LayoutBlock, lower: LayoutBlock) -> bool:
        if upper.category is not BlockCategory.TEXT or lower.category is not BlockCategory.TEXT:
            return False
        h = min(_line_height(upper), _line_height(lower))
        if h <= 0:
            return False
        gap = lower.bbox.y0 - upper.bbox.y1
        if gap < -0.3 * h or gap > self.block_gap * h:
            return False
        overlap = min(upper.bbox.x1, lower.bbox.x1) - max(upper.bbox.x0, lower.bbox.x0)
        return overlap >= self.block_overlap * min(upper.bbox.width, lower.bbox.width)

    def _merge_adjacent(self, blocks: Sequence[LayoutBlock]) -> tuple[LayoutBlock, ...]:
        ordered = sorted(blocks, key=lambda b: b.order)
        result: list[LayoutBlock] = []
        for block in ordered:
            previous = result[-1] if result else None
            if previous is not None and self._adjacent(previous, block):
                text = "\n".join(part for part in (previous.text, block.text) if part)
                result[-1] = replace(
                    previous,
                    bbox=union_box([previous.bbox, block.bbox]),
                    text=text,
                    lines=previous.lines + block.lines,
                    score=min(previous.score, block.score),
                )
                continue
            result.append(block)
        if len(result) != len(ordered):
            logger.debug("聚合：%d 个版面块合并成 %d 个", len(ordered), len(result))
        return tuple(replace(block, order=index) for index, block in enumerate(result))


# ---------------------------------------------------------------- 管线与配置


class NormalizePipeline:
    """依次执行各步骤。没有步骤时版面块原样通过。"""

    def __init__(self, steps: Sequence[NormalizeStep] = ()) -> None:
        self.steps: tuple[NormalizeStep, ...] = tuple(steps)

    @property
    def merges_paragraphs(self) -> bool:
        """文本模式下是否已经合并成段落（有开启段落合并的聚合步骤）。"""
        return any(isinstance(step, AggregateStep) and step.paragraphs for step in self.steps)

    def apply(self, blocks: Sequence[LayoutBlock], context: NormalizeContext) -> tuple[LayoutBlock, ...]:
        current = tuple(sorted(blocks, key=lambda b: b.order))
        for step in self.steps:
            current = step.apply(current, context)
        return current


def _p(
    default: Any,
    title: str,
    description: str,
    *,
    advanced: bool = False,
    ge: float | None = None,
    le: float | None = None,
) -> Any:
    return Field(
        default=default,
        title=title,
        description=description,
        ge=ge,
        le=le,
        json_schema_extra={"advanced": True} if advanced else None,
    )


_MERGE = MergeParams()


class NormalizeConfig(BaseModel):
    """[ocr_normalize] 节：结构归一化各步骤的开关与参数（需求 OCR-11）。

    字段约定与流水线配置相同（lst/services/ocr/config.py 的模块说明）：带 title、description 与取值范围，
    不常用的带 advanced 标记。这一节只在主进程里生效，没有需要重载 OCR 进程的字段，改动从下一次识别起生效。
    步骤顺序固定为清洗 → 拆分 → 聚合，理由见 lst/services/ocr/normalize.py 的模块说明。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    clean: bool = _p(True, "清洗", "去掉空文字、过小、置信度过低的框与重复框。")
    clean_min_score: float = _p(
        0.0, "最低置信度", "置信度低于该值的文本行与版面块丢弃；0 表示不按置信度丢弃。", ge=0.0, le=1.0
    )
    clean_min_area: int = _p(
        1, "最小面积", "面积小于该值（平方像素）的框丢弃。", advanced=True, ge=0, le=100_000
    )
    clean_duplicate_iou: float = _p(
        0.9, "重复框交并比", "两个框的交并比不小于该值时视为重复，只保留置信度高的一个；大于 1 表示不去重。",
        advanced=True, ge=0.1, le=1.01,
    )  # fmt: skip

    split: bool = _p(
        False, "拆分跨栏长行", "一行横跨两栏之间的空白时，按文字里的空格拆成左右两行（只在文本模式下）。"
    )
    split_min_gap: float = _p(
        1.5, "栏间空白宽度", "栏间空白的最小宽度（× 行高）。", advanced=True, ge=0.25, le=20.0
    )
    split_window: float = _p(
        3.0, "上下文范围", "判断栏间空白时向上、向下各看多远（× 行高）。", advanced=True, ge=0.5, le=20.0
    )

    aggregate: bool = _p(
        True,
        "整理文字单元",
        "结合截图颜色和间距整理文字单元；文本模式合并段落，版面模式拆开粘连的控件。"
        "关闭时文本模式每行单独翻译。",
    )
    same_line_tolerance: float = _p(
        _MERGE.same_line_tolerance, "同行中线容差", "同一行碎片的中线高度差上限（× 较小字高）。",
        advanced=True, ge=0.0, le=2.0,
    )  # fmt: skip
    fragment_gap: float = _p(
        _MERGE.fragment_gap, "同行碎片间距", "同一行碎片的水平间距上限（× 较小字高）。",
        advanced=True, ge=0.0, le=10.0,
    )  # fmt: skip
    gutter_window: float = _p(
        _MERGE.gutter_window, "栏间判断范围", "判断碎片间空隙是否栏间空白时，上下各看多远（× 较大字高）。",
        advanced=True, ge=0.0, le=20.0,
    )  # fmt: skip
    height_tolerance: float = _p(
        _MERGE.height_tolerance, "行高容差", "同一段落相邻两行的行高差上限（× 较小行高）。",
        advanced=True, ge=0.0, le=5.0,
    )  # fmt: skip
    line_gap: float = _p(
        _MERGE.line_gap, "行间距上限", "同一段落相邻两行的垂直间距上限（× 较小行高）。",
        advanced=True, ge=0.0, le=10.0,
    )  # fmt: skip
    max_line_overlap: float = _p(
        _MERGE.max_line_overlap, "行重叠上限", "同一段落相邻两行允许的垂直重叠（× 较小行高）。",
        advanced=True, ge=0.0, le=1.0,
    )  # fmt: skip
    min_horizontal_overlap: float = _p(
        _MERGE.min_horizontal_overlap, "水平重叠下限", "同一段落相邻两行的水平重叠下限（× 较窄一行的宽度）。",
        advanced=True, ge=0.0, le=1.0,
    )  # fmt: skip
    left_align_tolerance: float = _p(
        _MERGE.left_align_tolerance, "左对齐容差", "按左对齐判断同段时，左边缘相差的上限（× 较小行高）。",
        advanced=True, ge=0.0, le=20.0,
    )  # fmt: skip
    min_column_gap: float = _p(
        _MERGE.min_column_gap, "分栏空隙下限", "排阅读顺序时认定为栏间空隙的最小宽度（× 行高中位数）。",
        advanced=True, ge=0.0, le=10.0,
    )  # fmt: skip
    merge_blocks: bool = _p(
        False, "合并相邻正文块", "版面模式下把上下紧挨、左右对齐的相邻正文块合并成一块。", advanced=True
    )
    merge_block_gap: float = _p(
        0.8, "相邻块间距上限", "合并相邻正文块时允许的垂直间距（× 行高）。", advanced=True, ge=0.0, le=10.0
    )

    def merge_params(self) -> MergeParams:
        return MergeParams(
            same_line_tolerance=self.same_line_tolerance,
            fragment_gap=self.fragment_gap,
            gutter_window=self.gutter_window,
            height_tolerance=self.height_tolerance,
            line_gap=self.line_gap,
            max_line_overlap=self.max_line_overlap,
            min_horizontal_overlap=self.min_horizontal_overlap,
            left_align_tolerance=self.left_align_tolerance,
            min_column_gap=self.min_column_gap,
        )


def build_pipeline(config: NormalizeConfig, merger: ParagraphMerger | None = None) -> NormalizePipeline:
    """按配置组装管线。merger 不为 None 时段落合并用它（OcrModule 注入的自定义合并器），否则用配置里的阈值
    构造 GeometricMerger。"""
    steps: list[NormalizeStep] = []
    if config.clean:
        steps.append(
            CleanStep(
                min_area=config.clean_min_area,
                min_score=config.clean_min_score,
                duplicate_iou=config.clean_duplicate_iou,
            )
        )
    if config.split:
        steps.append(SplitStep(min_gap=config.split_min_gap, window=config.split_window))
    if config.aggregate or config.merge_blocks:
        steps.append(
            AggregateStep(
                merger if merger is not None else GeometricMerger(config.merge_params()),
                paragraphs=config.aggregate,
                merge_blocks=config.merge_blocks,
                block_gap=config.merge_block_gap,
                appearance=config.aggregate,
            )
        )
    return NormalizePipeline(steps)
