"""文本行的段落合并（需求 OCR-05，架构文档第 7 章第 5 步）。

文本模式下 OCR 进程只给出文本行（PaddleOCR 文本模式与 Windows OCR 都不做版面分析），
本模块把它们合并成段落，每个段落成为一个参加竞速的段。合并器做成可替换的组件：
``ParagraphMerger`` 是协议，``GeometricMerger`` 是默认实现。

默认实现参考用户笔记中的“方法 1”（先横向、再纵向逐步合并），分四步：

1. 清洗：丢弃文本为空白或包围盒面积为零的行。
2. 拼行：同一行被 OCR 拆开的碎片合并成一行。两个碎片中线高度差不超过较小字高的
   ``same_line_tolerance`` 倍（用中线代替基线：检测框上下带留白，中线比底边稳定），水平间距
   不超过较小字高的 ``fragment_gap`` 倍，并且两者之间的空隙不是栏间空白，就属于同一行。
   栏间空白的判据：空隙宽于 ``min_column_gap`` 倍字高，上下各 ``gutter_window`` 倍行高的范围内
   既有与左碎片上下对齐（水平上重叠）的行，也有与右碎片上下对齐的行，却没有任何一行跨过空隙的中点。
   这个关系按传递闭包合并，一行可以由多个碎片组成。
3. 成段：相邻两行属于同一段落，要同时满足三条：行高相差不超过较小行高的 ``height_tolerance`` 倍；
   上下间距不超过较小行高的 ``line_gap`` 倍（允许最多 ``max_line_overlap`` 倍的重叠）；水平重叠不少于
   较窄一行宽度的 ``min_horizontal_overlap`` 倍，或者左边缘相差不超过 ``left_align_tolerance`` 倍行高。
   满足条件的“上行 → 下行”关系只在双方互为唯一候选时成立：一行下面同时接着两栏的行，或者一行上面
   同时有两栏的行（跨栏的标题、通栏段落），都不与任何一边合并。多栏版面中两栏在水平上不重叠，
   第三条本身就把它们分开。
4. 排序：段落按递归 XY 切分排阅读顺序。先尝试竖切（找水平投影上宽于 ``min_column_gap`` 倍行高中位数的
   空隙，按栏从左到右），竖切不开再横切（按垂直投影上的空隙从上到下分带）。横切出的相邻各带如果共有
   同一条栏间空隙，就合回一组再竖切，这样两栏中恰好对齐的段间空白不会把栏切碎，通栏标题和通栏段落
   仍然单独成带。各部分递归处理，切不开时按（上边缘，左边缘）排序。

段落文本由成员行按阅读顺序（行从上到下，行内碎片从左到右）用 ``join_lines`` 拼接，规则见
lst/contracts/text.py：两侧有一侧是中日文字符（含注音、笔画与全角标点）时直接相连，否则加一个空格；
韩文词间本来有空格，按西文处理；英文行尾是“字母 + 连字符”且下一行以小写字母开头时视为断词。

阈值默认值的依据：

- ``same_line_tolerance=0.3``：同一行的碎片中线几乎重合；上下相邻两行的中线至少相距约 0.7 倍行高
  （行框最多重叠 0.3 倍行高），0.3 落在两者之间。
- ``fragment_gap=1.0``：西文词间距约为字号的四分之一，OCR 通常不会在这里断开；被拆开的碎片之间多是
  标点后的空白或较宽的字距，在 1 个字高以内。超过 1 倍字高的空白按分栏或独立标签处理。
- ``gutter_window=3.0``：双栏论文的栏间距只有 1 个字号左右（LaTeX 标准文档类的 columnsep 默认 10pt，
  正文 10pt 时正好 1 em），比 fragment_gap 还窄，只看间距会把两栏对齐的行拼在一起，所以另看上下文：
  单栏段落里上下几行都跨过碎片间的空隙，栏间空白则上下几行都没有跨过。看上下各 3 倍行高，
  段间距较大时也能看到相邻的行。表单里上下对齐的“标签 + 取值”同样形成一条空白通道，按分栏处理，
  标签和取值各成一段。
- ``height_tolerance=0.3``：同一字号的行框高度随字形内容（有无上伸、下伸部分）浮动约 10% 至 20%。
  一级、二级标题与正文的字号比在 1.5 倍以上（HTML 默认 h1 为 2 em、h2 为 1.5 em），靠行高就能分开；
  h3 只有 1.17 em，要靠标题上下的间距分开。
- ``line_gap=0.8``：常见行高是字号的 1.2 至 1.8 倍，检测框又带留白，段内行框间距多在 0.2 至 0.6 倍行高；
  段间距通常再加约 1 个字号（HTML 段落默认上下边距 1 em）。
- ``min_horizontal_overlap=0.5``：较窄一行有一半以上落在另一行的水平范围内，就算上下对齐；段落末行很短、
  首行缩进时也满足。
- ``left_align_tolerance=2.0``：只有两行的段落，首行缩进两个字、末行只有两三个字时，两行的水平重叠很小，
  要靠左对齐判断。中文缩进两个字约为 1.5 至 1.8 倍行框高，容差取 2 倍行高把它盖住；
  两栏的左边缘相差一整栏宽，不会因此误合。
- ``min_column_gap=0.25``：排阅读顺序时，两个段落在水平上分开就按分栏处理。窄栏间距的论文里，行框外扩后
  栏间空白可能只剩 0.4 倍行高，阈值要低于它；留 0.25 倍行高只为忽略检测框边缘一两个像素的抖动。

已知局限：段间距与行间距相同、只靠首行缩进分段的段落（小说、LaTeX 默认排版）会合成一段；悬挂缩进的列表项
与首行缩进的段落只看相邻两行无法区分，所以规则不看缩进。回归样本 zh_indent_without_spacing 记录了这一点。

这些默认值由 tests/services/ocr/fixtures 中的回归样本约束，调整后以段落完全匹配率不低于基线为准。
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, fields
from typing import Protocol

from lst.contracts.ocr import BBox, TextLine
from lst.contracts.text import join_lines
from lst.services.ocr.appearance import Appearance, compatible, same_color

# ---------------------------------------------------------------- 结果与协议


@dataclass(frozen=True, slots=True)
class Paragraph:
    """一个段落。

    lines 是成员行在输入序列中的下标，按段内阅读顺序排列；bbox 是成员行包围盒的并集；
    text 是成员行文本按 lines 的顺序用 ``join_lines`` 拼接的结果，不为空。
    """

    lines: tuple[int, ...]
    bbox: BBox
    text: str


class ParagraphMerger(Protocol):
    """段落合并器（需求 OCR-05 要求可替换）。

    输入一组文本行，输出段落，段落按阅读顺序排列。文本为空白的行不进入任何段落；
    其余每一行恰好进入一个段落。
    """

    def merge(self, lines: Sequence[TextLine]) -> tuple[Paragraph, ...]: ...


# ---------------------------------------------------------------- 参数


@dataclass(frozen=True, slots=True)
class MergeParams:
    """段落合并的阈值，全部以行高（字高）为单位。各项含义与默认值依据见模块文档。"""

    same_line_tolerance: float = 0.3
    """同一行碎片的中线高度差上限（× 较小字高）。"""
    fragment_gap: float = 1.0
    """同一行碎片的水平间距上限（× 较小字高）。"""
    gutter_window: float = 3.0
    """判断碎片间的空隙是不是栏间空白时，向上、向下各看多远（× 较大字高）。"""
    height_tolerance: float = 0.3
    """同一段落相邻两行的行高差上限（× 较小行高）。"""
    line_gap: float = 0.8
    """同一段落相邻两行的垂直间距上限（× 较小行高）。"""
    max_line_overlap: float = 0.3
    """同一段落相邻两行允许的垂直重叠上限（× 较小行高）。"""
    min_horizontal_overlap: float = 0.5
    """同一段落相邻两行的水平重叠下限（× 较窄一行的宽度）。"""
    left_align_tolerance: float = 2.0
    """不满足水平重叠时，按左对齐判断：左边缘相差的上限（× 较小行高）。"""
    min_column_gap: float = 0.25
    """阅读顺序中认定为栏间空隙的最小宽度（× 全部行高的中位数）。"""

    def __post_init__(self) -> None:
        for item in fields(self):
            value: object = getattr(self, item.name)
            if not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"MergeParams.{item.name} 必须是非负有限数")


# ---------------------------------------------------------------- 几何工具


def _union(boxes: Iterable[BBox]) -> BBox:
    items = list(boxes)
    return BBox(
        min(b.x0 for b in items),
        min(b.y0 for b in items),
        max(b.x1 for b in items),
        max(b.y1 for b in items),
    )


def _center_y(box: BBox) -> float:
    return (box.y0 + box.y1) / 2


@dataclass(frozen=True, slots=True)
class _Row:
    """拼好的一行：成员下标按从左到右排列，bbox 是成员的并集，height 是成员字高的中位数。

    成段时用 height 而不用 bbox 的高度：同一行的碎片上下错开一两个像素时，并集会比字高高出这么多，
    以它为单位的间距阈值随之放宽，段间空白就可能被当成行间距。
    """

    members: tuple[int, ...]
    bbox: BBox
    height: float
    appearance: Appearance | None = None


class _DisjointSet:
    """并查集，用于按传递闭包合并碎片。"""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, item: int) -> int:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)


# ---------------------------------------------------------------- 默认实现


class GeometricMerger:
    """按几何规则逐步合并文本行的段落合并器（默认实现，规则见模块文档）。"""

    def __init__(
        self,
        params: MergeParams | None = None,
        *,
        appearance: Callable[[TextLine], Appearance | None] | None = None,
    ) -> None:
        self._params = params if params is not None else MergeParams()
        self._appearance = appearance

    @property
    def params(self) -> MergeParams:
        return self._params

    def merge(self, lines: Sequence[TextLine]) -> tuple[Paragraph, ...]:
        valid = [
            index
            for index, line in enumerate(lines)
            if line.text.strip() and line.bbox.width > 0 and line.bbox.height > 0
        ]
        if not valid:
            return ()
        boxes = [line.bbox for line in lines]
        appearances = [self._appearance(line) if self._appearance is not None else None for line in lines]
        rows = self._assemble_rows(boxes, valid, appearances, lines)
        chains = self._link_rows(rows)
        paragraphs: list[Paragraph] = []
        for chain in chains:
            members = tuple(index for row in chain for index in row.members)
            paragraphs.append(
                Paragraph(
                    lines=members,
                    bbox=_union(row.bbox for row in chain),
                    text=join_lines(lines[index].text for index in members),
                )
            )
        unit = _median([row.height for row in rows])
        order = _reading_order([p.bbox for p in paragraphs], self._params.min_column_gap * unit)
        return tuple(paragraphs[index] for index in order)

    # ---- 第 2 步：拼行 ----

    def _same_row(self, a: BBox, b: BBox) -> bool:
        p = self._params
        h = min(a.height, b.height)
        if abs(_center_y(a) - _center_y(b)) > p.same_line_tolerance * h:
            return False
        gap = max(a.x0, b.x0) - min(a.x1, b.x1)  # 水平间距，重叠时为负
        return gap <= p.fragment_gap * h

    def _in_gutter(
        self, ia: int, ib: int, boxes: Sequence[BBox], by_center: Sequence[int], centers: Sequence[float]
    ) -> bool:
        """a、b 之间的空隙是否是栏间空白：附近有与左碎片上下对齐的行、也有与右碎片上下对齐的行，
        却没有一行跨过空隙的中点。

        附近指中线与 a、b 的平均中线相差不超过 gutter_window 倍较大字高、又不在同一行上的行。
        空隙窄于 min_column_gap 倍字高（包括碎片互相重叠）时不算栏间空白，与排阅读顺序时的分栏阈值一致；
        附近没有别的行时证据不足，也返回 False。
        """
        p = self._params
        a, b = boxes[ia], boxes[ib]
        h = max(a.height, b.height)
        left, right = (a, b) if a.x0 <= b.x0 else (b, a)
        if right.x0 - left.x1 <= p.min_column_gap * h:
            return False
        middle = (left.x1 + right.x0) / 2
        center = (_center_y(a) + _center_y(b)) / 2
        lo = bisect.bisect_left(centers, center - p.gutter_window * h)
        hi = bisect.bisect_right(centers, center + p.gutter_window * h)
        on_left = on_right = False
        for k in range(lo, hi):
            index = by_center[k]
            if index in (ia, ib) or abs(centers[k] - center) <= p.same_line_tolerance * h:
                continue
            box = boxes[index]
            if box.x0 < middle < box.x1:
                return False
            # 证据只算与碎片本身在水平上重叠的行，别的栏里的行不算。
            if box.x1 <= middle:
                on_left = on_left or (box.x0 < left.x1 and left.x0 < box.x1)
            else:
                on_right = on_right or (box.x0 < right.x1 and right.x0 < box.x1)
        return on_left and on_right

    def _assemble_rows(
        self,
        boxes: Sequence[BBox],
        valid: Sequence[int],
        appearances: Sequence[Appearance | None],
        lines: Sequence[TextLine],
    ) -> list[_Row]:
        """把同一行的碎片合并成行。按中线排序后只比较中线相近的碎片，复杂度接近线性。"""
        tolerance = self._params.same_line_tolerance
        by_center = sorted(valid, key=lambda i: _center_y(boxes[i]))
        centers = [_center_y(boxes[i]) for i in by_center]
        position = {index: k for k, index in enumerate(by_center)}
        groups = _DisjointSet(len(by_center))
        for k, a in enumerate(by_center):
            reach = centers[k] + tolerance * boxes[a].height
            for b in by_center[k + 1 :]:
                # 中线差的上限是较小字高的若干倍，不超过 a 的字高的若干倍；越过后不会再有满足条件的碎片。
                if _center_y(boxes[b]) > reach:
                    break
                aa, ab = appearances[a], appearances[b]
                styled = aa is not None and ab is not None
                gap = max(boxes[a].x0, boxes[b].x0) - min(boxes[a].x1, boxes[b].x1)
                h = min(boxes[a].height, boxes[b].height)

                def prefix(text: str) -> bool:
                    stripped = text.strip().rstrip(".)、")
                    return not any(c.isalnum() for c in text) or (
                        stripped.isdecimal()
                        and len(stripped) <= 3
                        and text.strip().endswith((".", ")", "、"))
                    )

                accessory = prefix(lines[a].text) or prefix(lines[b].text)
                if styled and (
                    (not same_color(aa, ab) and not accessory)
                    or gap > (1.1 if accessory else 0.55) * h
                    or (
                        aa is not None
                        and ab is not None
                        and abs(aa.ink_height - ab.ink_height)
                        > self._params.height_tolerance * min(aa.ink_height, ab.ink_height)
                        and not accessory
                    )
                ):
                    continue
                overlap = min(boxes[a].y1, boxes[b].y1) - max(boxes[a].y0, boxes[b].y0)
                joined = self._same_row(boxes[a], boxes[b]) or (styled and accessory and overlap > 0.5 * h)
                if joined and (accessory or not self._in_gutter(a, b, boxes, by_center, centers)):
                    groups.union(position[a], position[b])
        members: dict[int, list[int]] = {}
        for k, index in enumerate(by_center):
            members.setdefault(groups.find(k), []).append(index)
        rows: list[_Row] = []
        for group in members.values():
            group.sort(key=lambda i: (boxes[i].x0, boxes[i].y0))
            rows.append(
                _Row(
                    tuple(group),
                    _union(boxes[i] for i in group),
                    _median([boxes[i].height for i in group]),
                    appearances[max(group, key=lambda i: boxes[i].width)],
                )
            )
        return rows

    # ---- 第 3 步：成段 ----

    def _same_paragraph(self, upper_row: _Row, lower_row: _Row) -> bool:
        p = self._params
        upper, lower = upper_row.bbox, lower_row.bbox
        h = min(upper_row.height, lower_row.height)
        if not compatible(upper_row.appearance, lower_row.appearance):
            return False
        if abs(upper_row.height - lower_row.height) > p.height_tolerance * h:
            return False
        gap = lower.y0 - upper.y1
        if gap > self._line_gap(upper_row) * h or gap < -p.max_line_overlap * h:
            return False
        overlap = min(upper.x1, lower.x1) - max(upper.x0, lower.x0)
        if overlap >= p.min_horizontal_overlap * min(upper.width, lower.width):
            return True
        return abs(upper.x0 - lower.x0) <= p.left_align_tolerance * h

    def _line_gap(self, row: _Row) -> float:
        if row.appearance is None:
            return self._params.line_gap
        # 紧贴字形的系统 OCR 框会低估大标题行高；小字控件则按更紧的间距合并。
        return self._params.line_gap * (1.4 if row.height >= 36 else 0.8)

    def _link_rows(self, rows: Sequence[_Row]) -> list[list[_Row]]:
        """把行连成段落：只接受互为唯一候选的“上行 → 下行”关系，结果是若干条从上到下的链。"""
        ordered = sorted(rows, key=lambda r: (r.bbox.y0, r.bbox.x0))
        downs: list[list[int]] = [[] for _ in ordered]
        ups: list[list[int]] = [[] for _ in ordered]
        for a, upper in enumerate(ordered):
            limit = upper.bbox.y1 + self._line_gap(upper) * upper.height
            for b in range(a + 1, len(ordered)):
                lower = ordered[b]
                # 按上边缘排序，后面的行只会更靠下；间距上限不超过上行行高的 line_gap 倍。
                if lower.bbox.y0 > limit:
                    break
                if self._same_paragraph(upper, lower):
                    downs[a].append(b)
        # 去掉跨过中间一行的关系：a → c → b 都成立时，a → b 只是行距较小造成的跳行，不是第二个候选。
        # 碎片上下错开时拼出的行框会变高，隔一行的间距可能落进 line_gap 以内（随机生成的文档实测）；
        # 不去掉的话 a 有两个下方候选，段落从这里断开。两栏的行之间没有关系，扇出到两栏的判断不受影响。
        reachable = [set(candidates) for candidates in downs]
        for a, candidates in enumerate(downs):
            downs[a] = [b for b in candidates if not any(b in reachable[c] for c in candidates if c != b)]
            for b in downs[a]:
                ups[b].append(a)
        nxt: dict[int, int] = {}
        has_prev: set[int] = set()
        for a, candidates in enumerate(downs):
            if len(candidates) == 1 and ups[candidates[0]] == [a]:
                nxt[a] = candidates[0]
                has_prev.add(candidates[0])
        chains: list[list[_Row]] = []
        for start in range(len(ordered)):
            if start in has_prev:
                continue
            chain = [ordered[start]]
            current = start
            while current in nxt:
                current = nxt[current]
                chain.append(ordered[current])
            chains.append(chain)
        return chains


class LineMerger:
    """不合并的“合并器”：每个非空行单独成段，按输入顺序排列。

    结构归一化的聚合步骤关闭时（lst/services/ocr/normalize.py），文本模式按它一行一段切分。
    """

    def merge(self, lines: Sequence[TextLine]) -> tuple[Paragraph, ...]:
        return tuple(
            Paragraph(lines=(index,), bbox=line.bbox, text=line.text.strip())
            for index, line in enumerate(lines)
            if line.text.strip()
        )


# ---------------------------------------------------------------- 第 4 步：阅读顺序


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _split(items: Sequence[int], boxes: Sequence[BBox], *, vertical: bool, min_gap: float) -> list[list[int]]:
    """按投影上宽度大于 min_gap 的空隙切分。vertical 为真时沿 x 方向切成栏，否则沿 y 方向切成带。"""

    def start(i: int) -> int:
        return boxes[i].x0 if vertical else boxes[i].y0

    def stop(i: int) -> int:
        return boxes[i].x1 if vertical else boxes[i].y1

    ordered = sorted(items, key=start)
    groups: list[list[int]] = [[ordered[0]]]
    end = stop(ordered[0])
    for i in ordered[1:]:
        if start(i) - end > min_gap:
            groups.append([i])
            end = stop(i)
        else:
            groups[-1].append(i)
            end = max(end, stop(i))
    return groups


def _column_gaps(items: Sequence[int], boxes: Sequence[BBox], min_gap: float) -> list[tuple[int, int]]:
    """水平投影上宽度大于 min_gap 的空隙（只算两个框之间的空隙，不含两侧页边）。"""
    ordered = sorted(items, key=lambda i: boxes[i].x0)
    gaps: list[tuple[int, int]] = []
    end = boxes[ordered[0]].x1
    for i in ordered[1:]:
        if boxes[i].x0 - end > min_gap:
            gaps.append((end, boxes[i].x0))
        end = max(end, boxes[i].x1)
    return gaps


def _intersect(
    a: Sequence[tuple[int, int]], b: Sequence[tuple[int, int]], min_gap: float
) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a0, a1 in a:
        for b0, b1 in b:
            lo, hi = max(a0, b0), min(a1, b1)
            if hi - lo > min_gap:
                out.append((lo, hi))
    return out


def _group_bands(bands: Sequence[list[int]], boxes: Sequence[BBox], min_gap: float) -> list[list[int]]:
    """把共有同一条栏间空隙的相邻各带合成一组；没有栏间空隙的带（通栏内容）单独成组。"""
    groups: list[list[int]] = []
    common: list[tuple[int, int]] = []
    for band in bands:
        gaps = _column_gaps(band, boxes, min_gap)
        if groups and common and gaps:
            shared = _intersect(common, gaps, min_gap)
            if shared:
                groups[-1].extend(band)
                common = shared
                continue
        groups.append(list(band))
        common = gaps
    return groups


def _reading_order(boxes: Sequence[BBox], min_column_gap: float) -> list[int]:
    """按递归 XY 切分给出阅读顺序（规则见模块文档第 4 步）。"""

    def order(items: list[int]) -> list[int]:
        if len(items) <= 1:
            return items
        columns = _split(items, boxes, vertical=True, min_gap=min_column_gap)
        if len(columns) > 1:
            return [i for column in columns for i in order(column)]
        bands = _split(items, boxes, vertical=False, min_gap=0)
        if len(bands) > 1:
            groups = _group_bands(bands, boxes, min_column_gap)
            if len(groups) > 1:
                return [i for group in groups for i in order(group)]
        return sorted(items, key=lambda i: (boxes[i].y0, boxes[i].x0))

    return order(list(range(len(boxes))))


# ---------------------------------------------------------------- 回归指标


def exact_match_rate(expected: Sequence[Sequence[int]], actual: Sequence[Sequence[int]]) -> float:
    """段落完全匹配率：期望段落中，成员行集合与某个输出段落完全相同的比例（架构文档第 12 章）。

    段内行的先后不影响匹配。期望为空时，输出也为空记 1.0，否则记 0.0。
    """
    if not expected:
        return 1.0 if not actual else 0.0
    produced = {frozenset(group) for group in actual}
    hits = sum(frozenset(group) in produced for group in expected)
    return hits / len(expected)
