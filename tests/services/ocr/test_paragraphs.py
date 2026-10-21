"""段落合并的测试：文本拼接规则、每条合并规则的正反例、阅读顺序、参数，以及 hypothesis 性质测试。

除非特别说明，测试里的行高都是 20 像素，默认阈值下：同一行碎片的中线差上限 6、水平间距上限 20；
同一段落相邻两行的行高差上限 6、垂直间距上限 16、垂直重叠上限 6、左边缘相差上限 40。
"""

import itertools
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lst.contracts.ocr import BBox, TextLine
from lst.services.ocr import (
    GeometricMerger,
    MergeParams,
    Paragraph,
    ParagraphMerger,
    exact_match_rate,
    is_cjk,
    join_lines,
)
from tests.services.ocr.helpers import groups, line, merge, stack

# ---------------------------------------------------------------- 文本拼接


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (["第一行文字", "第二行文字"], "第一行文字第二行文字"),
        (["first line", "second line"], "first line second line"),
        (["句子到这里，", "继续写。"], "句子到这里，继续写。"),
        (["基于", "Transformer 的模型"], "基于Transformer 的模型"),
        (["在 WMT", "数据集上"], "在 WMT数据集上"),
        (["これは", "テストです。"], "これはテストです。"),
        (["안녕하세요", "세계"], "안녕하세요 세계"),
        (["Ｆｕｌｌ", "ｗｉｄｔｈ"], "Ｆｕｌｌｗｉｄｔｈ"),
        (["recog-", "nition"], "recog-nition"),
        (["well-", "Known"], "well- Known"),
        (["page 3-", "4"], "page 3- 4"),
        (["  leading", "trailing  ", "", "   ", "end"], "leading trailing end"),
        ([], ""),
        (["", " "], ""),
    ],
)
def test_join_lines(parts: list[str], expected: str) -> None:
    assert join_lines(parts) == expected


@pytest.mark.parametrize("char", ["中", "。", "，", "あ", "カ", "ｱ", "Ａ", "㐀", "𠀀", "〔"])
def test_cjk_characters(char: str) -> None:
    assert is_cjk(char)


@pytest.mark.parametrize("char", ["a", "Z", "1", ",", ".", "한", "é", "Я", " "])
def test_non_cjk_characters(char: str) -> None:
    assert not is_cjk(char)


# ---------------------------------------------------------------- 成段规则


def test_lines_with_same_height_and_small_gap_form_one_paragraph() -> None:
    lines = stack(40, 20, [600, 600, 300], texts=["first", "second", "third"])
    paragraphs = merge(lines)
    assert groups(paragraphs) == [[0, 1, 2]]
    assert paragraphs[0].text == "first second third"


@pytest.mark.parametrize(("height", "merged"), [(25, True), (26, True), (27, False), (40, False)])
def test_line_height_must_be_similar(height: int, merged: bool) -> None:
    # 较小行高 20，差值上限 6：25、26 合并，27 起分开。
    lines = [line(40, 20, 600, 40), line(40, 46, 600, 46 + height)]
    assert groups(merge(lines)) == ([[0, 1]] if merged else [[0], [1]])


@pytest.mark.parametrize(
    ("gap", "merged"), [(0, True), (16, True), (17, False), (30, False), (-6, True), (-7, False)]
)
def test_vertical_gap_must_be_small(gap: int, merged: bool) -> None:
    lines = [line(40, 20, 600, 40), line(40, 40 + gap, 600, 60 + gap)]
    assert groups(merge(lines)) == ([[0, 1]] if merged else [[0], [1]])


def test_lines_without_horizontal_overlap_are_not_merged() -> None:
    # 下一行整个落在上一行右侧：没有水平重叠，也不左对齐。
    lines = [line(40, 20, 300, 40), line(320, 46, 600, 66)]
    assert groups(merge(lines)) == [[0], [1]]


def test_small_horizontal_overlap_without_left_alignment_is_not_merged() -> None:
    # 重叠 50 像素，不到较窄一行宽度 200 的一半；左边缘相差 150，超过 40。
    lines = [line(40, 20, 240, 40), line(190, 46, 700, 66)]
    assert groups(merge(lines)) == [[0], [1]]


def test_half_overlap_is_enough() -> None:
    # 居中排列的两行：较窄一行全部落在较宽一行的范围内。
    lines = [line(40, 20, 640, 40), line(240, 46, 440, 66)]
    assert groups(merge(lines)) == [[0, 1]]


def test_indented_first_line_and_tiny_last_line_merge_by_left_alignment() -> None:
    # 首行缩进 36 像素，末行只有两个字（x 40–76），两行在水平上不重叠，只能按左对齐（相差 36 ≤ 40）合并。
    lines = [line(76, 20, 640, 40, "首行缩进两个字"), line(40, 46, 76, 66, "末行")]
    assert groups(merge(lines)) == [[0, 1]]
    assert groups(merge(lines, MergeParams(left_align_tolerance=1.0))) == [[0], [1]]


def test_two_columns_are_not_merged() -> None:
    left = stack(30, 20, [360, 360, 200], texts=["L0", "L1", "L2"])
    right = stack(430, 22, [360, 360, 360], texts=["R0", "R1", "R2"])
    # OCR 的输出按行交错。
    lines = [left[0], right[0], left[1], right[1], left[2], right[2]]
    paragraphs = merge(lines)
    assert groups(paragraphs) == [[0, 2, 4], [1, 3, 5]]
    assert [p.text for p in paragraphs] == ["L0 L1 L2", "R0 R1 R2"]


def test_row_spanning_two_columns_joins_neither() -> None:
    left = stack(30, 20, [360, 360])
    right = stack(430, 20, [360, 360])
    below = line(30, 72, 790, 92, "spanning")  # 紧接在两栏下面的通栏行
    above = line(30, -6, 790, 14, "header")  # 紧挨在两栏上面的通栏行
    lines = [*left, *right, below, above]
    result = {frozenset(g) for g in groups(merge(lines))}
    assert result == {frozenset({0, 1}), frozenset({2, 3}), frozenset({4}), frozenset({5})}


# ---------------------------------------------------------------- 同一行的碎片


def test_fragments_on_one_line_are_joined_left_to_right() -> None:
    # 输入顺序打乱；碎片间距 8、12 像素，都小于 1 倍字高。
    lines = [line(260, 21, 400, 41, "again"), line(40, 20, 160, 40, "Hello"), line(168, 20, 248, 40, "world")]
    paragraphs = merge(lines)
    assert groups(paragraphs) == [[1, 2, 0]]
    assert paragraphs[0].text == "Hello world again"


def test_fragment_gap_limit_is_one_character_height() -> None:
    near = [line(40, 20, 160, 40, "a"), line(180, 20, 300, 40, "b")]  # 间距 20
    far = [line(40, 20, 160, 40, "a"), line(181, 20, 300, 40, "b")]  # 间距 21
    assert groups(merge(near)) == [[0, 1]]
    assert groups(merge(far)) == [[0], [1]]


def test_fragments_must_share_the_center_line() -> None:
    # 中线相差 7 像素（> 6）：不是同一行；上下又重叠 13 像素，也不是上下相邻的两行。
    lines = [line(40, 20, 160, 40, "a"), line(170, 27, 300, 47, "b")]
    assert groups(merge(lines)) == [[0], [1]]


def test_fragmented_rows_then_join_into_a_paragraph() -> None:
    lines = [
        line(40, 20, 150, 40, "one"),
        line(158, 20, 300, 40, "two"),
        line(40, 46, 120, 66, "three"),
        line(128, 46, 300, 66, "four"),
        line(40, 72, 100, 92, "five"),
    ]
    paragraphs = merge(lines)
    assert groups(paragraphs) == [[0, 1, 2, 3, 4]]
    assert paragraphs[0].text == "one two three four five"
    assert paragraphs[0].bbox == BBox(40, 20, 300, 92)


def test_narrow_gutter_between_aligned_columns_is_not_bridged() -> None:
    # 栏间空白 14 像素（0.7 倍行高），比碎片间距上限 20 还窄；两栏的行严格对齐。
    left = stack(30, 20, [330, 330, 330], texts=["L0", "L1", "L2"])
    right = stack(374, 20, [330, 330, 330], texts=["R0", "R1", "R2"])
    lines = [left[0], right[0], left[1], right[1], left[2], right[2]]
    assert [p.text for p in merge(lines)] == ["L0 L1 L2", "R0 R1 R2"]
    # 不看上下文时，两栏对齐的行会被当成同一行的碎片。
    assert [p.text for p in merge(lines, MergeParams(gutter_window=0))] == ["L0 R0 L1 R1 L2 R2"]


def test_split_line_inside_a_paragraph_is_joined() -> None:
    # 中间一行被拆开，间距 14 像素；上下两行都跨过这个空隙，所以不是栏间空白。
    lines = [
        line(40, 20, 600, 40, "top"),
        line(314, 46, 600, 66, "right"),
        line(40, 46, 300, 66, "left"),
        line(40, 72, 600, 92, "bottom"),
    ]
    assert [p.text for p in merge(lines)] == ["top left right bottom"]


def test_isolated_split_line_is_joined() -> None:
    # 附近没有别的行，判断不了是否分栏，按碎片间距合并。
    lines = [line(40, 20, 300, 40, "left"), line(314, 20, 600, 40, "right")]
    assert [p.text for p in merge(lines)] == ["left right"]


def test_chinese_fragments_join_without_spaces() -> None:
    lines = [line(40, 20, 256, 44, "碎片常在标点处断开，"), line(262, 20, 390, 44, "例如逗号之后")]
    assert merge(lines)[0].text == "碎片常在标点处断开，例如逗号之后"


# ---------------------------------------------------------------- 阅读顺序


def texts(paragraphs: Sequence[Paragraph]) -> list[str]:
    return [p.text for p in paragraphs]


def column(x0: int, y0: int, paragraphs: Sequence[Sequence[str]], *, width: int = 360) -> list[TextLine]:
    """一栏：各段之间空 30 像素，段内行距 6。"""
    out: list[TextLine] = []
    y = y0
    for rows in paragraphs:
        out.extend(stack(x0, y, [width] * len(rows), texts=list(rows)))
        y += len(rows) * 26 - 6 + 30
    return out


def test_two_columns_read_left_column_first() -> None:
    left = column(30, 20, [["a1", "a2"], ["b1", "b2"]])
    right = column(430, 20, [["c1", "c2"], ["d1", "d2"]])
    lines = sorted([*left, *right], key=lambda item: (item.bbox.y0, item.bbox.x0))
    assert texts(merge(lines)) == ["a1 a2", "b1 b2", "c1 c2", "d1 d2"]


def test_title_and_footer_around_two_columns() -> None:
    title = line(30, 0, 790, 30, "title")
    left = column(30, 60, [["a1", "a2"], ["b1", "b2"]])
    right = column(430, 60, [["c1", "c2"], ["d1", "d2"]])
    footer = line(30, 240, 790, 260, "footer")
    lines = [footer, *right, title, *left]
    assert texts(merge(lines)) == ["title", "a1 a2", "b1 b2", "c1 c2", "d1 d2", "footer"]


def test_two_column_sections_separated_by_a_full_width_paragraph() -> None:
    top = [*column(30, 0, [["a1", "a2"], ["b1"]]), *column(430, 0, [["c1", "c2"], ["d1"]])]
    middle = stack(30, 140, [760, 400], texts=["m1", "m2"])
    bottom = [*column(30, 230, [["e1"]]), *column(430, 230, [["f1"]])]
    assert texts(merge([*bottom, *middle, *top])) == ["a1 a2", "b1", "c1 c2", "d1", "m1 m2", "e1", "f1"]


def test_single_column_reads_top_to_bottom() -> None:
    lines = column(30, 20, [["a"], ["b"], ["c"]])
    assert texts(merge(list(reversed(lines)))) == ["a", "b", "c"]


# ---------------------------------------------------------------- 清洗与边界


def test_blank_and_degenerate_lines_are_dropped() -> None:
    lines = [line(40, 20, 600, 40, "kept"), line(40, 46, 600, 66, "   "), line(40, 72, 40, 92, "zero width")]
    assert groups(merge(lines)) == [[0]]


def test_empty_input() -> None:
    assert merge([]) == ()
    assert merge([line(0, 0, 10, 10, "")]) == ()


def test_paragraph_bbox_is_union_of_members() -> None:
    lines = [line(60, 20, 600, 40), line(40, 46, 620, 66), line(40, 72, 300, 93)]
    assert merge(lines)[0].bbox == BBox(40, 20, 620, 93)


def test_custom_thresholds_change_the_outcome() -> None:
    # 两段之间空 25 像素（1.25 倍行高）：默认分开，line_gap=1.5 时合并。
    lines = [line(40, 20, 600, 40), line(40, 65, 600, 85)]
    assert groups(merge(lines)) == [[0], [1]]
    assert groups(merge(lines, MergeParams(line_gap=1.5))) == [[0, 1]]


@pytest.mark.parametrize("value", [-0.1, math.nan, math.inf])
def test_merge_params_reject_invalid_values(value: float) -> None:
    with pytest.raises(ValueError, match="line_gap"):
        MergeParams(line_gap=value)


def test_default_merger_satisfies_the_protocol() -> None:
    merger: ParagraphMerger = GeometricMerger()
    assert isinstance(merger, GeometricMerger)
    assert merger.params == MergeParams()


# ---------------------------------------------------------------- 回归指标


def test_exact_match_rate() -> None:
    assert exact_match_rate([[0, 1], [2]], [[1, 0], [2]]) == 1.0
    assert exact_match_rate([[0, 1], [2]], [[0], [1, 2]]) == 0.0
    assert exact_match_rate([[0, 1], [2], [3]], [[0, 1], [2, 3]]) == pytest.approx(1 / 3)
    assert exact_match_rate([], []) == 1.0
    assert exact_match_rate([], [[0]]) == 0.0


# ---------------------------------------------------------------- 性质测试：任意输入


@st.composite
def random_lines(draw: st.DrawFn) -> list[TextLine]:
    """任意位置、任意大小的文本行。每行的文本是唯一的记号 <k>，少数行为空白。"""
    count = draw(st.integers(min_value=0, max_value=30))
    out: list[TextLine] = []
    for k in range(count):
        x0 = draw(st.integers(min_value=0, max_value=600))
        y0 = draw(st.integers(min_value=0, max_value=400))
        width = draw(st.integers(min_value=0, max_value=300))
        height = draw(st.integers(min_value=0, max_value=50))
        blank = k % 4 == 0 and draw(st.booleans())
        out.append(line(x0, y0, x0 + width, y0 + height, " " if blank else f"<{k}>"))
    return out


def _union(boxes: Sequence[BBox]) -> BBox:
    return BBox(
        min(b.x0 for b in boxes), min(b.y0 for b in boxes), max(b.x1 for b in boxes), max(b.y1 for b in boxes)
    )


@settings(max_examples=300, deadline=None)
@given(random_lines())
def test_merge_invariants_on_arbitrary_lines(lines: list[TextLine]) -> None:
    paragraphs = merge(lines)
    members = [i for p in paragraphs for i in p.lines]
    eligible = sorted(
        i
        for i, item in enumerate(lines)
        if item.text.strip() and item.bbox.width > 0 and item.bbox.height > 0
    )
    # 每个非空行恰好进入一个段落，空白行和零面积的行不进入任何段落。
    assert sorted(members) == eligible
    for paragraph in paragraphs:
        assert paragraph.lines
        # 段落的包围盒是成员行的并集。
        assert paragraph.bbox == _union([lines[i].bbox for i in paragraph.lines])
        # 段落文本按成员顺序拼接，每个成员的记号在文本中按这个顺序出现。
        assert paragraph.text == join_lines(lines[i].text for i in paragraph.lines)
        positions = [paragraph.text.index(f"<{i}>") for i in paragraph.lines]
        assert positions == sorted(positions)


# ---------------------------------------------------------------- 性质测试：版面已知的文档


@dataclass(frozen=True, slots=True)
class Document:
    """生成的文档：文本行（已打乱），以及按阅读顺序排列的期望段落（每段的成员按段内阅读顺序）。"""

    lines: list[TextLine]
    expected: list[list[int]]


@st.composite
def documents(draw: st.DrawFn) -> Document:
    """一至三栏的文档。每栏若干段，每段若干行，每行由一至三个碎片组成。

    间距按规则留出余量：段内行距 0 至 0.5 倍行高，段间距 1 至 2 倍较大行高，碎片间距 0.1 至 0.6 倍行高，
    栏间距 2 至 4 倍最大行高；首行缩进 0 或 1.5 倍行高，末行宽度为栏宽的 0.3 至 1 倍。
    同一栏里各行的碎片空隙在水平上互相错开至少一个行高：上下对齐的空隙形成一条空白通道，
    合并器按栏间空白处理（规则见 lst.services.ocr.paragraphs 第 2 步），不属于这里生成的版面。
    """
    max_h = 24
    columns = draw(st.integers(min_value=1, max_value=3))
    placed: list[tuple[BBox, str]] = []
    expected: list[list[int]] = []
    x = 20
    token = 0
    for _ in range(columns):
        width = draw(st.integers(min_value=200, max_value=420))
        y = 20
        previous_h: int | None = None
        channels: list[tuple[int, int]] = []
        for _ in range(draw(st.integers(min_value=1, max_value=3))):
            h = draw(st.sampled_from([16, 20, 24]))
            if previous_h is not None:
                y += draw(st.integers(min_value=max(h, previous_h), max_value=2 * max(h, previous_h)))
            members: list[int] = []
            rows = draw(st.integers(min_value=1, max_value=4))
            indent = draw(st.sampled_from([0, round(1.5 * h)]))
            for r in range(rows):
                start = x + (indent if r == 0 else 0)
                full = x + width
                end = (
                    full
                    if r < rows - 1
                    else start + max(3 * h, int(draw(st.floats(0.3, 1.0)) * (full - start)))
                )
                pieces = draw(st.integers(min_value=1, max_value=3))
                bounds = _split_row(draw, start, min(end, full), pieces, h)
                gaps = [(left[1], right[0]) for left, right in itertools.pairwise(bounds)]
                if any(g0 - max_h < c1 and c0 < g1 + max_h for g0, g1 in gaps for c0, c1 in channels):
                    bounds = [(start, min(end, full))]
                else:
                    channels.extend(gaps)
                for x0, x1 in bounds:
                    dy = draw(st.integers(min_value=-1, max_value=1))
                    placed.append((BBox(x0, y + dy, x1, y + h + dy), f"<{token}>"))
                    members.append(len(placed) - 1)
                    token += 1
                y += h
                if r < rows - 1:
                    y += draw(st.integers(min_value=0, max_value=h // 2))
            expected.append(members)
            previous_h = h
        x += width + draw(st.integers(min_value=2 * max_h, max_value=4 * max_h))
    order = draw(st.permutations(range(len(placed))))
    position = {old: new for new, old in enumerate(order)}
    lines = [TextLine(placed[old][0], placed[old][1], 0.9) for old in order]
    return Document(lines, [[position[i] for i in group] for group in expected])


def _split_row(draw: st.DrawFn, start: int, end: int, pieces: int, h: int) -> list[tuple[int, int]]:
    """把一行 [start, end) 切成若干碎片，碎片间距 0.1 至 0.6 倍行高，每个碎片至少一个字高宽。"""
    gaps = [draw(st.integers(min_value=max(1, h // 10), max_value=(6 * h) // 10)) for _ in range(pieces - 1)]
    usable = end - start - sum(gaps)
    if usable < pieces * h:
        return [(start, end)]
    cuts = sorted(draw(st.lists(st.integers(1, 1000), min_size=pieces - 1, max_size=pieces - 1)))
    widths: list[int] = []
    remaining = usable - pieces * h
    previous = 0
    for cut in [*cuts, 1000]:
        widths.append(h + remaining * (cut - previous) // 1000)
        previous = cut
    widths[-1] += usable - sum(widths)
    out: list[tuple[int, int]] = []
    x = start
    for k, width in enumerate(widths):
        out.append((x, x + width))
        x += width + (gaps[k] if k < len(gaps) else 0)
    return out


@settings(max_examples=200, deadline=None)
@given(documents())
def test_merge_recovers_generated_documents(document: Document) -> None:
    paragraphs = merge(document.lines)
    assert groups(paragraphs) == document.expected
    for paragraph in paragraphs:
        assert paragraph.text == " ".join(document.lines[i].text for i in paragraph.lines)


def test_merge_does_not_depend_on_input_order() -> None:
    lines = [
        *column(30, 0, [["a1", "a2", "a3"], ["b1"]]),
        *column(430, 0, [["c1"], ["d1", "d2"]]),
        line(30, 200, 790, 220, "footer"),
    ]
    reference = texts(merge(lines))
    rng = random.Random(7)
    for _ in range(20):
        shuffled = lines[:]
        rng.shuffle(shuffled)
        assert texts(merge(shuffled)) == reference
