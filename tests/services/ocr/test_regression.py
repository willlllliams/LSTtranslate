"""段落合并的回归测试（需求 OCR-05、MNT-04，架构文档第 12 章“回归测试”）。

fixtures 目录下每个 JSON 文件是一个样本：

- lines：文本行输入，每行 {"bbox": [x0, y0, x1, y1], "text": ..., "score": ...}，顺序与 OCR 输出相同；
- paragraphs：人工标注的期望段落，按阅读顺序排列，每段是成员行在 lines 中的下标；
- baseline：段落完全匹配率的基线，测试要求不低于它；
- check_order：为真时，所有段落都匹配上的样本还要求输出段落的顺序与标注相同；
- texts（可选）：期望段落的文本，与 paragraphs 一一对应；
- image、source、description：截图尺寸、来源（synthetic 为手工构造，recorded 为真实 OCR 输出）和说明。

指标是段落完全匹配率（lst.services.ocr.exact_match_rate）。修改合并算法或阈值后，某个样本的匹配率超过基线时，
把基线调高到新值；低于基线说明出现了回归。以后用真实 OCR 输出替换或补充这些样本。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from lst.contracts.ocr import BBox, TextLine
from lst.services.ocr import GeometricMerger, exact_match_rate

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(frozen=True, slots=True)
class Sample:
    name: str
    lines: tuple[TextLine, ...]
    paragraphs: tuple[tuple[int, ...], ...]
    baseline: float
    check_order: bool
    texts: tuple[str, ...] | None


def load(path: Path) -> Sample:
    raw = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    lines: list[TextLine] = []
    for item in cast(list[dict[str, object]], raw["lines"]):
        x0, y0, x1, y1 = cast(list[int], item["bbox"])
        lines.append(
            TextLine(BBox(x0, y0, x1, y1), cast(str, item["text"]), float(cast(float, item["score"])))
        )
    paragraphs = tuple(tuple(group) for group in cast(list[list[int]], raw["paragraphs"]))
    texts = raw.get("texts")
    return Sample(
        name=cast(str, raw["name"]),
        lines=tuple(lines),
        paragraphs=paragraphs,
        baseline=float(cast(float, raw["baseline"])),
        check_order=bool(raw.get("check_order", True)),
        texts=None if texts is None else tuple(cast(list[str], texts)),
    )


SAMPLE_FILES = sorted(FIXTURES.glob("*.json"))


def test_fixture_set_is_present() -> None:
    assert len(SAMPLE_FILES) >= 8


@pytest.mark.parametrize("path", SAMPLE_FILES, ids=lambda p: p.stem)
def test_sample_is_well_formed(path: Path) -> None:
    sample = load(path)
    assert sample.name == path.stem
    assert 0.0 <= sample.baseline <= 1.0
    labelled = [i for group in sample.paragraphs for i in group]
    # 标注把每个非空行恰好分到一个段落里。
    assert sorted(labelled) == [i for i, item in enumerate(sample.lines) if item.text.strip()]
    if sample.texts is not None:
        assert len(sample.texts) == len(sample.paragraphs)


@pytest.mark.parametrize("path", SAMPLE_FILES, ids=lambda p: p.stem)
def test_exact_match_rate_does_not_fall_below_baseline(path: Path) -> None:
    sample = load(path)
    output = GeometricMerger().merge(sample.lines)
    actual = [p.lines for p in output]
    rate = exact_match_rate(sample.paragraphs, actual)
    assert rate >= sample.baseline, f"{sample.name}：段落完全匹配率 {rate:.2f} 低于基线 {sample.baseline:.2f}"

    by_members = {frozenset(p.lines): p for p in output}
    if sample.texts is not None:
        for group, text in zip(sample.paragraphs, sample.texts, strict=True):
            paragraph = by_members.get(frozenset(group))
            if paragraph is not None:
                assert paragraph.text == text
    if sample.check_order and rate == 1.0:
        assert [frozenset(group) for group in actual] == [frozenset(group) for group in sample.paragraphs]
