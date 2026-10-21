"""版面标签映射（架构文档 8.4 节）：每个已知标签都有映射，未知标签按有无文字回退并告警。"""

import logging

import pytest

from lst.contracts.ocr import BlockCategory
from lst.workers.ocr_worker import labels
from lst.workers.ocr_worker.labels import LABEL_CATEGORIES, category_for
from tests.workers.ocr.harness import OFFICIAL_MODELS

# PaddleOCR 3.7 / PaddleX 3.7 中 PP-StructureV3 可选的版面模型的 label_list（inference.yml），
# 以及解析阶段改写出的标签。模型目录不在时以这份常量为准。
PP_DOCLAYOUT_PLUS_L = [
    "paragraph_title",
    "image",
    "text",
    "number",
    "abstract",
    "content",
    "figure_title",
    "formula",
    "table",
    "reference",
    "doc_title",
    "footnote",
    "header",
    "algorithm",
    "footer",
    "seal",
    "chart",
    "formula_number",
    "aside_text",
    "reference_content",
]
PP_DOCLAYOUT_L = [
    "paragraph_title",
    "image",
    "text",
    "number",
    "abstract",
    "content",
    "figure_title",
    "formula",
    "table",
    "table_title",
    "reference",
    "doc_title",
    "footnote",
    "header",
    "algorithm",
    "footer",
    "seal",
    "chart_title",
    "chart",
    "formula_number",
    "header_image",
    "footer_image",
    "aside_text",
]
PARSING_RELABELS = ["vision_footnote", "doc_title", "text"]
# 实测（本机 PP-StructureV3 输出）出现过的标签。
OBSERVED = ["paragraph_title", "doc_title", "text", "figure_title", "table", "formula", "chart"]


@pytest.mark.parametrize(
    "label", sorted({*PP_DOCLAYOUT_PLUS_L, *PP_DOCLAYOUT_L, *PARSING_RELABELS, *OBSERVED})
)
def test_every_documented_label_is_mapped(label: str) -> None:
    assert label in LABEL_CATEGORIES


@pytest.mark.parametrize("model", ["PP-DocLayout_plus-L", "PP-DocLayout-L", "PP-DocLayout-S"])
def test_label_lists_in_local_models_are_mapped(model: str) -> None:
    config = OFFICIAL_MODELS / model / "inference.yml"
    if not config.is_file():
        pytest.skip(f"本机没有 {model}")
    # label_list 是紧跟在键后面、以 "- " 开头的一串行。
    names: list[str] = []
    for line in config.read_text(encoding="utf-8").split("label_list:", 1)[1].splitlines()[1:]:
        if not line.startswith("- "):
            break
        names.append(line[2:].strip())
    assert len(names) >= 20, "没有从 inference.yml 读到 label_list"
    assert [name for name in names if name not in LABEL_CATEGORIES] == []


@pytest.mark.parametrize(
    ("label", "category"),
    [
        ("doc_title", BlockCategory.TITLE),
        ("paragraph_title", BlockCategory.TITLE),
        ("text", BlockCategory.TEXT),
        ("abstract", BlockCategory.TEXT),
        ("figure_title", BlockCategory.TEXT),
        ("formula_number", BlockCategory.TEXT),
        ("table", BlockCategory.TABLE),
        ("formula", BlockCategory.FORMULA),
        ("chart", BlockCategory.CHART),
        ("image", BlockCategory.IMAGE),
        ("seal", BlockCategory.IMAGE),
        ("header_image", BlockCategory.IMAGE),
        ("header", BlockCategory.HEADER_FOOTER),
        ("footer", BlockCategory.HEADER_FOOTER),
        ("number", BlockCategory.HEADER_FOOTER),
        ("page_number", BlockCategory.HEADER_FOOTER),
    ],
)
def test_architecture_table(label: str, category: BlockCategory) -> None:
    assert category_for(label, has_text=True) is category
    assert category_for(label, has_text=False) is category


def test_labels_are_case_insensitive() -> None:
    assert category_for("Doc_Title", has_text=True) is BlockCategory.TITLE


def test_unknown_label_falls_back_by_text_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    labels._warned.discard("mystery_label")
    with caplog.at_level(logging.WARNING, logger="lst.workers.ocr_worker.labels"):
        assert category_for("mystery_label", has_text=True) is BlockCategory.TEXT
        assert category_for("mystery_label", has_text=False) is BlockCategory.IMAGE
    warnings = [r for r in caplog.records if "mystery_label" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
