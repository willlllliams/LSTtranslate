"""结构归一化中间层（需求 OCR-11）：清洗、拆分、聚合各步骤，全部关闭时原样通过，
默认配置与原来的段落合并一致。"""

from pathlib import Path

import pytest

from lst.contracts.ocr import BBox, BlockCategory, LayoutBlock, TableCell, TextLine
from lst.services.ocr import (
    AggregateStep,
    CleanStep,
    GeometricMerger,
    LineMerger,
    NormalizeConfig,
    NormalizeContext,
    NormalizePipeline,
    OcrMode,
    PassThrough,
    SplitStep,
    blocks_to_segments,
    build_pipeline,
    exact_match_rate,
)
from tests.services.ocr.helpers import line, stack
from tests.services.ocr.test_regression import SAMPLE_FILES, load

TEXT = NormalizeContext(1000, 1000, text_mode=True)
STRUCTURE = NormalizeContext(1000, 1000, text_mode=False)


def container(lines: list[TextLine], block_id: str = "0") -> LayoutBlock:
    """文本模式的输出：覆盖整张截图的一个 text 块。"""
    return LayoutBlock(block_id, BlockCategory.TEXT, BBox(0, 0, 1000, 1000), 0, 0.9, lines=tuple(lines))


def block(
    block_id: str,
    box: tuple[int, int, int, int],
    order: int,
    *,
    category: BlockCategory = BlockCategory.TEXT,
    text: str = "text",
    score: float = 0.9,
) -> LayoutBlock:
    return LayoutBlock(block_id, category, BBox(*box), order, score, text=text)


# ---------------------------------------------------------------- 管线


def test_all_steps_off_passes_blocks_through_unchanged() -> None:
    config = NormalizeConfig(clean=False, split=False, aggregate=False, merge_blocks=False)
    pipeline = build_pipeline(config)
    assert pipeline.steps == ()
    assert not pipeline.merges_paragraphs
    blocks = (
        container([line(10, 10, 200, 30, "  "), line(10, 10, 200, 30, "dup")]),
        block("1", (5, 5, 6, 6), 1),
    )
    for context in (TEXT, STRUCTURE):
        assert pipeline.apply(blocks, context) == blocks


def test_pass_through_step_returns_its_input() -> None:
    blocks = (block("0", (0, 0, 10, 10), 0),)
    assert PassThrough().apply(blocks, TEXT) == blocks


def test_default_pipeline_order_is_clean_split_aggregate() -> None:
    pipeline = build_pipeline(NormalizeConfig(split=True))
    assert [step.name for step in pipeline.steps] == ["clean", "split", "aggregate"]
    assert pipeline.merges_paragraphs
    assert [step.name for step in build_pipeline(NormalizeConfig()).steps] == ["clean", "aggregate"]


# ---------------------------------------------------------------- 清洗


def test_clean_drops_empty_small_low_score_and_duplicate_lines() -> None:
    lines = [
        line(10, 10, 300, 30, "keep", 0.95),
        line(10, 40, 300, 60, "   ", 0.99),  # 空文字
        line(10, 70, 12, 71, "dot", 0.99),  # 面积 2
        line(10, 100, 300, 120, "noise", 0.2),  # 低置信度
        line(11, 10, 301, 30, "keep?", 0.90),  # 与第一行几乎重合、置信度更低
    ]
    step = CleanStep(min_area=10, min_score=0.5, duplicate_iou=0.9)
    (out,) = step.apply((container(lines),), TEXT)
    assert [item.text for item in out.lines] == ["keep"]
    assert out.score == pytest.approx(0.95)


def test_duplicate_keeps_the_higher_score() -> None:
    lines = [line(10, 10, 300, 30, "low", 0.6), line(10, 11, 300, 31, "high", 0.9)]
    (out,) = CleanStep().apply((container(lines),), TEXT)
    assert [item.text for item in out.lines] == ["high"]


def test_clean_drops_empty_text_blocks_and_duplicate_blocks_in_structure_mode() -> None:
    blocks = (
        block("0", (0, 0, 500, 40), 0, text="title", category=BlockCategory.TITLE),
        block("1", (0, 50, 500, 200), 1, text="  "),
        block("2", (0, 210, 500, 400), 2, text="body", score=0.8),
        block("3", (1, 211, 500, 401), 3, text="body again", score=0.7),
        LayoutBlock("4", BlockCategory.IMAGE, BBox(0, 500, 300, 800), 4, 0.9),
        block("5", (0, 810, 500, 900), 5, text="weak", score=0.1),
    )
    out = CleanStep(min_score=0.3).apply(blocks, STRUCTURE)
    assert [b.id for b in out] == ["0", "2", "4"]


def test_clean_keeps_every_block_with_recognized_text_in_structure_mode() -> None:
    # 截屏测试页的版面：正文块只有 text 没有 lines（PaddleX 给的 content），按钮与表格是一个 image 块，
    # 文字在它的 lines 里。默认清洗只去掉空白行，有文字的块与图片块里的文字行都留下。
    words = [
        line(49, 374, 144, 414, "Public"),
        line(202, 374, 274, 415, "Star"),
        line(33, 519, 143, 558, " "),
    ]
    image = LayoutBlock("3", BlockCategory.IMAGE, BBox(19, 361, 897, 688), 3, 0.61, lines=tuple(words))
    blocks = (
        block("0", (15, 12, 588, 49), 0, text="Screenshot translation test", category=BlockCategory.TITLE),
        block("1", (13, 102, 876, 187), 1, text="The file could not be found."),
        block("2", (13, 235, 851, 320), 2, text="Your changes have been saved."),
        image,
    )
    out = CleanStep().apply(blocks, STRUCTURE)
    assert [b.id for b in out] == ["0", "1", "2", "3"]
    assert [entry.text for entry in out[3].lines] == ["Public", "Star"]


def test_clean_with_defaults_keeps_everything_meaningful() -> None:
    lines = stack(10, 10, [300, 280, 120], texts=["a", "b", "c"])
    (out,) = CleanStep().apply((container(lines),), TEXT)
    assert out.lines == tuple(lines)


def test_clean_keeps_table_cells_and_drops_a_container_without_lines() -> None:
    cell = TableCell(0, 0, 1, 1, BBox(0, 0, 100, 40), "x")
    table = LayoutBlock("t", BlockCategory.TABLE, BBox(0, 0, 200, 80), 1, 0.9, cells=(cell,))
    empty = container([line(0, 0, 50, 20, " ")])
    out = CleanStep().apply((empty, table), TEXT)
    assert out == (table,)


# ---------------------------------------------------------------- 拆分


def two_columns_with_bridge(text: str) -> list[TextLine]:
    """两栏版面，中间那一行被 OCR 识别成横跨两栏的一个长行。栏宽 400，栏间空白 40。"""
    left = stack(10, 10, [400, 400], texts=["left one", "left two"])
    right = stack(450, 10, [400, 400], texts=["right one", "right two"])
    bridge = line(10, 62, 850, 82, text)
    below_left = stack(10, 88, [400], texts=["left three"])
    below_right = stack(450, 88, [400], texts=["right three"])
    return [*left, *right, bridge, *below_left, *below_right]


def test_split_cuts_a_line_that_spans_the_gutter() -> None:
    lines = two_columns_with_bridge("Lorem ipsum dolor sit amet sed    Ut enim ad minim veniam quis")
    (out,) = SplitStep(min_gap=1.5).apply((container(lines),), TEXT)
    texts = [item.text for item in out.lines]
    assert "Lorem ipsum dolor sit amet sed" in texts
    assert "Ut enim ad minim veniam quis" in texts
    pieces = [item for item in out.lines if item.text.startswith(("Lorem", "Ut enim"))]
    assert pieces[0].bbox.x1 <= 412 and pieces[1].bbox.x0 >= 448
    assert len(out.lines) == len(lines) + 1


def test_split_leaves_text_without_spaces_alone() -> None:
    lines = two_columns_with_bridge("这一行是中文没有空格所以无法可靠地找到切分点而保持原样不变动")
    (out,) = SplitStep().apply((container(lines),), TEXT)
    assert out.lines == tuple(lines)


def test_split_needs_lines_on_both_sides_of_the_gap() -> None:
    # 单栏段落：上下行都跨过同一位置，没有栏间空白。
    lines = stack(
        10, 10, [840, 840, 840], texts=["first full line", "second   full   line", "third full line"]
    )
    (out,) = SplitStep().apply((container(lines),), TEXT)
    assert out.lines == tuple(lines)


def test_split_only_works_in_text_mode() -> None:
    blocks = (container(two_columns_with_bridge("aaaa bbbb    cccc dddd")),)
    assert SplitStep().apply(blocks, STRUCTURE) == blocks


def test_split_then_aggregate_keeps_columns_apart() -> None:
    lines = two_columns_with_bridge("left bridge words here    right bridge words here")
    pipeline = build_pipeline(NormalizeConfig(split=True))
    blocks = pipeline.apply((container(lines),), TEXT)
    for paragraph in blocks:
        xs = {item.bbox.x0 < 425 for item in paragraph.lines}
        assert len(xs) == 1, "段落不应同时包含两栏的行"


# ---------------------------------------------------------------- 聚合


def test_aggregate_builds_paragraph_blocks_matching_the_merger() -> None:
    lines = [*stack(10, 10, [300, 300, 120]), *stack(10, 140, [300, 200])]
    (source,) = (container(lines),)
    out = AggregateStep().apply((source,), TEXT)
    expected = GeometricMerger().merge(lines)
    assert [b.text for b in out] == [p.text for p in expected]
    assert [b.bbox for b in out] == [p.bbox for p in expected]
    assert [b.order for b in out] == list(range(len(out)))
    assert all(b.category is BlockCategory.TEXT and b.lines for b in out)
    assert [b.id for b in out] == ["p1", "p2"]


def test_aggregate_keeps_other_blocks_and_their_order() -> None:
    image = LayoutBlock("9", BlockCategory.IMAGE, BBox(0, 600, 100, 700), 1, 0.9)
    out = AggregateStep().apply((container(stack(10, 10, [300])), image), TEXT)
    assert [b.category for b in out] == [BlockCategory.TEXT, BlockCategory.IMAGE]
    assert [b.order for b in out] == [0, 1]


def test_aggregate_ids_avoid_existing_block_ids() -> None:
    other = LayoutBlock("p1", BlockCategory.IMAGE, BBox(0, 600, 100, 700), 1, 0.9)
    out = AggregateStep().apply((container(stack(10, 10, [300])), other), TEXT)
    assert len({b.id for b in out}) == 2


def test_merge_blocks_joins_adjacent_text_blocks_in_structure_mode() -> None:
    blocks = (
        block("0", (0, 0, 500, 40), 0, text="first part"),
        block("1", (0, 46, 500, 86), 1, text="second part"),
        block("2", (0, 300, 500, 340), 2, text="far away"),
        block("3", (0, 346, 500, 386), 3, text="a title", category=BlockCategory.TITLE),
    )
    out = AggregateStep(paragraphs=False, merge_blocks=True, block_gap=0.8).apply(blocks, STRUCTURE)
    assert [b.text for b in out] == ["first part\nsecond part", "far away", "a title"]
    assert out[0].bbox == BBox(0, 0, 500, 86)
    assert AggregateStep(merge_blocks=False).apply(blocks, STRUCTURE) == blocks


def test_structure_mode_is_untouched_by_default() -> None:
    blocks = (block("0", (0, 0, 500, 40), 0), block("1", (0, 46, 500, 86), 1))
    assert build_pipeline(NormalizeConfig()).apply(blocks, STRUCTURE) == blocks


# ---------------------------------------------------------------- 与原来的段落合并一致


def segments_without_layer(lines: list[TextLine]) -> list[tuple[str, str, BBox]]:
    return [
        (s.segment.id, s.segment.text, s.bbox) for s in blocks_to_segments((container(lines),), OcrMode.TEXT)
    ]


def segments_with_layer(lines: list[TextLine], config: NormalizeConfig) -> list[tuple[str, str, BBox]]:
    pipeline = build_pipeline(config)
    blocks = pipeline.apply((container(lines),), TEXT)
    if pipeline.merges_paragraphs:
        segments = blocks_to_segments(blocks, OcrMode.TEXT, premerged=True)
    else:
        segments = blocks_to_segments(blocks, OcrMode.TEXT, LineMerger())
    return [(s.segment.id, s.segment.text, s.bbox) for s in segments]


@pytest.mark.parametrize("path", SAMPLE_FILES, ids=lambda p: p.stem)
def test_default_layer_gives_the_same_segments_as_before(path: Path) -> None:
    sample = load(path)
    lines = list(sample.lines)
    assert segments_with_layer(lines, NormalizeConfig()) == segments_without_layer(lines)


@pytest.mark.parametrize("path", SAMPLE_FILES, ids=lambda p: p.stem)
def test_default_layer_does_not_regress_the_match_rate(path: Path) -> None:
    sample = load(path)
    pipeline = build_pipeline(NormalizeConfig())
    blocks = pipeline.apply((container(list(sample.lines)),), TEXT)
    index = {id(item): k for k, item in enumerate(sample.lines)}
    groups = [[index[id(item)] for item in b.lines] for b in blocks]
    assert exact_match_rate(sample.paragraphs, groups) >= sample.baseline


def test_aggregate_off_gives_one_segment_per_line() -> None:
    lines = stack(10, 10, [300, 300, 120], texts=["a", "b", "c"])
    result = segments_with_layer(lines, NormalizeConfig(aggregate=False))
    assert [(seg_id, text) for seg_id, text, _ in result] == [("p1", "a"), ("p2", "b"), ("p3", "c")]
    assert [box for *_, box in result] == [item.bbox for item in lines]


def test_merge_params_come_from_the_config() -> None:
    pipeline = build_pipeline(NormalizeConfig(line_gap=0.1))
    (step,) = [s for s in pipeline.steps if isinstance(s, AggregateStep)]
    assert isinstance(step.merger, GeometricMerger)
    assert step.merger.params.line_gap == 0.1
    custom = LineMerger()
    (step,) = [s for s in build_pipeline(NormalizeConfig(), custom).steps if isinstance(s, AggregateStep)]
    assert step.merger is custom


def test_pipeline_sorts_by_order_before_running() -> None:
    blocks = (block("b", (0, 100, 10, 110), 1), block("a", (0, 0, 10, 10), 0))
    assert [b.id for b in NormalizePipeline().apply(blocks, STRUCTURE)] == ["a", "b"]
