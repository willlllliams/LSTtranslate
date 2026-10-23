"""句末标点参与续行判断，几何边界仍阻止跨栏、跨控件合并。"""

import pytest
from pydantic import ValidationError

from lst.contracts.ocr import BBox, BlockCategory, LayoutBlock
from lst.services.ocr.appearance import Appearance
from lst.services.ocr.normalize import NormalizeConfig, NormalizeContext, build_pipeline
from lst.services.ocr.paragraphs import GeometricMerger, ParagraphRules, ends_sentence
from tests.services.ocr.helpers import groups, line
from tests.services.ocr.test_normalize import container


@pytest.mark.parametrize(
    "text,finished",
    [
        ("A complete sentence.", True),
        ('Finished!"', True),
        ("结束了吗？』", True),
        ("结束。 ）", True),
        ("Continue after Dr.", False),
        ("For example, e.g.", False),
        ("See U.S.", False),
        ("A README.md file", False),
        ("add a /profile/", False),
        ("Version 1.2", False),
        ("", False),
    ],
)
def test_sentence_boundaries(text: str, finished: bool) -> None:
    assert ends_sentence(text, ".!?。！？") is finished


def test_custom_sentence_endings() -> None:
    assert ends_sentence("First clause;", ";；")
    assert not ends_sentence("First clause.", ";；")


def test_adjacent_block_merge_does_not_undo_sentence_boundaries() -> None:
    blocks = [
        LayoutBlock("a", BlockCategory.TEXT, BBox(10, 10, 500, 30), 0, 1.0, text="First sentence."),
        LayoutBlock("b", BlockCategory.TEXT, BBox(10, 40, 500, 60), 1, 1.0, text="Second sentence."),
    ]
    context = NormalizeContext(800, 100, False)
    sentence = build_pipeline(NormalizeConfig(paragraph_mode="sentence", merge_blocks=True))
    smart = build_pipeline(NormalizeConfig(paragraph_mode="smart", merge_blocks=True))
    assert len(sentence.apply(blocks, context)) == 2
    assert len(smart.apply(blocks, context)) == 1


@pytest.mark.parametrize(
    "mode,first,expected",
    [
        ("geometric", "Create a public repository with this", [[0], [1]]),
        ("smart", "Create a public repository with this", [[0, 1]]),
        ("smart", "Create a public repository with this.", [[0], [1]]),
        ("continuous", "Create a public repository with this.", [[0, 1]]),
        ("sentence", "Create a public repository with this.", [[0], [1]]),
    ],
)
def test_configured_mode_changes_real_pipeline(mode: str, first: str, expected: list[list[int]]) -> None:
    lines = [line(10, 10, 600, 30, first), line(10, 49, 300, 69, "name and description.")]
    config = NormalizeConfig.model_validate({"paragraph_mode": mode})
    blocks = build_pipeline(config).apply([container(lines)], NormalizeContext(800, 100, True))
    assert [len(b.lines) for b in blocks] == [len(members) for members in expected]


def test_smart_mode_keeps_multiple_sentences_in_one_close_paragraph() -> None:
    lines = [line(10, 10, 600, 30, "First sentence."), line(10, 40, 600, 60, "Second sentence.")]
    assert groups(GeometricMerger(rules=ParagraphRules(mode="smart")).merge(lines)) == [[0, 1]]
    assert groups(GeometricMerger(rules=ParagraphRules(mode="sentence")).merge(lines)) == [[0], [1]]


def test_continuation_does_not_merge_short_menu_items() -> None:
    lines = [line(10, 10, 250, 30, "Privacy and Security"), line(10, 49, 80, 69, "Tags")]
    assert groups(GeometricMerger(rules=ParagraphRules(mode="smart")).merge(lines)) == [[0], [1]]


def test_continuation_still_respects_columns_and_blank_paragraph_spacing() -> None:
    lines = [
        line(10, 10, 300, 30, "This is a line with unfinished"),
        line(400, 49, 700, 69, "another column"),
        line(10, 100, 300, 120, "a separate paragraph"),
    ]
    assert all(
        len(p.lines) == 1 for p in GeometricMerger(rules=ParagraphRules(mode="continuous")).merge(lines)
    )


def test_small_punctuation_fragment_does_not_shrink_row_height() -> None:
    lines = [
        line(10, 10, 600, 31, "Create a public repository called .github."),
        line(10, 41, 240, 55, "README.md. The README will"),
        line(250, 51, 251, 54, ":"),
        line(243, 39, 570, 61, "appear on your organization's profile,"),
        line(10, 69, 160, 90, "visible to anyone."),
    ]
    merger = GeometricMerger(
        rules=ParagraphRules(mode="smart"), appearance=lambda _: Appearance((220, 230, 220), 18)
    )
    assert len(merger.merge(lines)) == 1


@pytest.mark.parametrize(
    "patch", [{"paragraph_mode": "unknown"}, {"continuation_gap": -1}, {"sentence_endings": ""}]
)
def test_invalid_paragraph_settings_are_rejected(patch: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        NormalizeConfig.model_validate(patch)
