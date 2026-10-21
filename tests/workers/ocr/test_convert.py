"""产线输出到版面块的转换（架构文档 8.4 节）：extract（取字段）、convert（转换）、blocks（JSON 输出）。

三类输入：

- 手工构造的 RawPage，覆盖每条转换规则；
- 仿照 PaddleX 结果对象构造的 dict 与简单对象（含 numpy 数组和 np.float32 坐标），检查 extract；
- fixtures/ 下录制的本机 PP-StructureV3 真实输出（两行三列表格文档、公式文档），作为回归样本。

所有输出都经 validation.validate_blocks 严格校验。
"""

import dataclasses
import json
import logging
from pathlib import Path
from typing import cast

import numpy as np
import pytest

from lst.contracts.ocr import BlockCategory
from lst.workers.ocr_worker.blocks import OutBlock, OutLine, blocks_to_json
from lst.workers.ocr_worker.convert import convert_structure, convert_text, parse_chart_table, reading_order
from lst.workers.ocr_worker.extract import (
    RawBlock,
    RawFormula,
    RawLine,
    RawPage,
    RawRegion,
    RawTable,
    extract_structure,
    extract_text,
)
from lst.workers.ocr_worker.geometry import Box, CoordinateMap
from tests.workers.ocr.validation import validate_blocks

FIXTURES = Path(__file__).with_name("fixtures")
W, H = 1000, 800


def line(box: Box, text: str, score: float = 0.9, is_text: bool = True) -> RawLine:
    return RawLine(box, text, score, is_text)


def convert(page: RawPage, cmap: CoordinateMap | None = None) -> list[dict[str, object]]:
    blocks = convert_structure(page, cmap or CoordinateMap.identity(W, H))
    return validate_blocks(blocks_to_json(blocks), W, H)


TABLE_HTML = "<table><tr><td>名称</td><td>数量</td></tr><tr><td>苹果</td><td>12</td></tr></table>"


def sample_page() -> RawPage:
    """一个覆盖各类块的页面：标题、两行正文、表格、公式、图表、图片，外加一行不属于任何块的文字。"""
    return RawPage(
        output_size=(W, H),
        angle=None,
        lines=(
            line((50, 20, 400, 60), "Title", 0.99),
            # 同一块内两行故意倒序给出，碎片也乱序：转换后应按阅读顺序排列。
            line((52, 130, 300, 160), "second line", 0.8),
            line((50, 90, 200, 120), "first", 0.9),
            line((210, 92, 420, 118), "line", 0.7),
            line((60, 210, 160, 240), "名称", 0.95),  # 表格内的行：不输出
            line((600, 600, 700, 630), "orphan", 0.85),  # 不在任何块内
            line((300, 400, 500, 440), "E=mc^2", 1.0, is_text=False),  # PaddleX 并入的公式结果
            line((720, 420, 800, 440), "2021", 0.6),  # 图表内的轴标签
            line((50, 700, 300, 730), "   ", 0.5),  # 空白行丢弃
        ),
        blocks=(
            RawBlock("paragraph_title", (48, 18, 402, 62), "Title"),
            RawBlock("text", (48, 88, 425, 162), "firstline second line"),
            RawBlock("table", (50, 200, 450, 300), TABLE_HTML),
            RawBlock("formula", (298, 398, 502, 442), "E=mc^2"),
            RawBlock("chart", (700, 300, 950, 500), "Year | Sales\n--- | ---\n2021 | 120\n2022 | 200"),
            RawBlock("image", (50, 500, 300, 690), ""),
        ),
        regions=(
            RawRegion("table", (50, 201, 450, 300), 0.97),
            RawRegion("paragraph_title", (48, 18, 402, 62), 0.83),  # 标签后来被改写也照样按位置配对
            RawRegion("text", (48, 88, 425, 162), 0.88),
            RawRegion("formula", (298, 398, 502, 442), 0.91),
            RawRegion("chart", (700, 300, 950, 500), 0.77),
            RawRegion("image", (50, 500, 300, 690), 0.66),
        ),
        tables=(
            RawTable(
                TABLE_HTML,
                ((50, 200, 250, 250), (250, 200, 450, 250), (50, 250, 250, 300), (250, 250, 450, 300)),
            ),
        ),
        formulas=(RawFormula((299, 399, 501, 441), "E=mc^{2}"),),
        chart_recognized=True,
    )


def test_categories_order_and_scores() -> None:
    blocks = convert(sample_page())
    assert [(b["category"], b["order"], b["id"]) for b in blocks] == [
        ("title", 0, "0"),
        ("text", 1, "1"),
        ("table", 2, "2"),
        ("formula", 3, "3"),
        ("chart", 4, "4"),
        ("image", 5, "5"),
        ("text", 6, "6"),  # 不属于任何块的行排在最后
    ]
    assert [b["score"] for b in blocks[:6]] == [0.83, 0.88, 0.97, 0.91, 0.77, 0.66]
    assert blocks[6]["score"] == 0.85


def test_text_block_lines_in_reading_order() -> None:
    text_block = convert(sample_page())[1]
    lines = cast(list[dict[str, object]], text_block["lines"])
    assert [entry["text"] for entry in lines] == ["first", "line", "second line"]
    assert text_block["text"] == "first\nline\nsecond line"
    assert lines[0]["bbox"] == [50, 90, 200, 120]


def test_table_cells_and_text() -> None:
    table = convert(sample_page())[2]
    assert table["lines"] == []
    assert table["text"] == "名称 数量\n苹果 12"
    assert table["cells"] == [
        {"row": 0, "col": 0, "row_span": 1, "col_span": 1, "bbox": [50, 200, 250, 250], "text": "名称"},
        {"row": 0, "col": 1, "row_span": 1, "col_span": 1, "bbox": [250, 200, 450, 250], "text": "数量"},
        {"row": 1, "col": 0, "row_span": 1, "col_span": 1, "bbox": [50, 250, 250, 300], "text": "苹果"},
        {"row": 1, "col": 1, "row_span": 1, "col_span": 1, "bbox": [250, 250, 450, 300], "text": "12"},
    ]


def test_formula_chart_image_content() -> None:
    blocks = convert(sample_page())
    formula, chart, image = blocks[3], blocks[4], blocks[5]
    assert formula["latex"] == "E=mc^{2}"
    assert formula["lines"] == []  # 公式结果并入整页 OCR 的那一行不是文本行
    assert chart["chart_table"] == [["Year", "Sales"], ["2021", "120"], ["2022", "200"]]
    assert [entry["text"] for entry in cast(list[dict[str, object]], chart["lines"])] == ["2021"]
    assert image["lines"] == [] and image["text"] == ""
    # 内容字段只出现在对应类别里。
    for block in blocks:
        assert ("cells" in block) == (block["category"] == "table")
        assert ("latex" in block) == (block["category"] == "formula")
        assert ("chart_table" in block) == (block["category"] == "chart")


def test_orphan_line_becomes_text_block() -> None:
    orphan = convert(sample_page())[6]
    assert orphan["text"] == "orphan"
    assert orphan["bbox"] == [600, 600, 700, 630]
    assert len(cast(list[object], orphan["lines"])) == 1


def test_chart_without_recognition_has_no_table() -> None:
    page = dataclasses.replace(sample_page(), chart_recognized=False)
    assert convert(page)[4]["chart_table"] is None


def test_formula_without_recognition_has_null_latex() -> None:
    page = dataclasses.replace(sample_page(), formulas=())
    assert convert(page)[3]["latex"] is None


def test_table_without_recognition_falls_back_to_text() -> None:
    page = dataclasses.replace(
        sample_page(),
        blocks=(RawBlock("table", (50, 200, 450, 300), "名称"),),
        tables=(),
    )
    table = convert(page)[0]
    assert table["category"] == "table"
    assert table["cells"] == []
    assert table["text"] == "名称"
    assert [entry["text"] for entry in cast(list[dict[str, object]], table["lines"])] == ["名称"]


def test_unmatched_block_scores_from_lines() -> None:
    page = dataclasses.replace(sample_page(), regions=())
    blocks = convert(page)
    assert blocks[1]["score"] == pytest.approx((0.9 + 0.7 + 0.8) / 3)
    assert blocks[5]["score"] == 0.0  # 图片块没有文本行


def test_unknown_label_with_text_is_text() -> None:
    page = RawPage(
        (W, H), None, (line((10, 10, 100, 40), "hello"),), blocks=(RawBlock("sidebar", (5, 5, 105, 45), ""),)
    )
    assert convert(page)[0]["category"] == "text"


def test_coordinates_are_mapped_back_to_original() -> None:
    # 原图 2000×1600 缩成 1000×800 后识别：输出坐标全部乘 2。
    page = sample_page()
    blocks = convert_structure(page, CoordinateMap((2000, 1600), (1000, 800)))
    out = validate_blocks(blocks_to_json(blocks), 2000, 1600)
    assert out[2]["bbox"] == [100, 400, 900, 600]
    cells = cast(list[dict[str, object]], out[2]["cells"])
    assert cells[0]["bbox"] == [100, 400, 500, 500]
    assert cast(list[dict[str, object]], out[1]["lines"])[0]["bbox"] == [100, 180, 400, 240]


def test_blocks_outside_image_are_dropped() -> None:
    page = RawPage(
        (W, H),
        None,
        (line((1200, 10, 1300, 40), "outside"), line((10, 10, 100, 40), "inside")),
        blocks=(
            RawBlock("text", (1100, 0, 1400, 50), "outside"),
            RawBlock("text", (5, 5, 105, 45), "inside"),
        ),
    )
    blocks = convert(page)
    assert [(b["order"], b["text"]) for b in blocks] == [(0, "inside")]


def test_empty_page() -> None:
    assert convert(RawPage((W, H), None, ())) == []


# ---------------------------------------------------------------- 文本模式


def test_text_mode_single_block_covers_image() -> None:
    page = RawPage(
        (W, H),
        None,
        (
            line((10, 10, 100, 40), "a", 0.8),
            line((10, 50, 100, 80), "", 0.1),
            line((10, 90, 100, 120), "b", 0.6),
        ),
    )
    blocks = validate_blocks(blocks_to_json(convert_text(page, CoordinateMap.identity(W, H))), W, H)
    assert len(blocks) == 1
    block = blocks[0]
    assert (block["category"], block["bbox"], block["order"], block["text"]) == ("text", [0, 0, W, H], 0, "")
    assert block["score"] == pytest.approx(0.7)
    assert [entry["text"] for entry in cast(list[dict[str, object]], block["lines"])] == ["a", "b"]


def test_text_mode_without_lines_returns_nothing() -> None:
    assert convert_text(RawPage((W, H), None, ()), CoordinateMap.identity(W, H)) == []


def test_text_mode_rotation_and_resize() -> None:
    # 原图 800×400 缩成 400×200，PaddleX 再旋转 180 度：旋转后坐标 (0, 0, 100, 50) 对应原图右下角。
    page = RawPage((400, 200), 180, (line((0, 0, 100, 50), "corner"),))
    blocks = blocks_to_json(
        convert_text(page, CoordinateMap((800, 400), (400, 200), page.angle, page.output_size))
    )
    out = validate_blocks(blocks, 800, 400)
    assert cast(list[dict[str, object]], out[0]["lines"])[0]["bbox"] == [600, 300, 800, 400]


# ---------------------------------------------------------------- 图表数据表与阅读顺序


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Year | Sales\n2021 | 120", [["Year", "Sales"], ["2021", "120"]]),
        ("| a | b |\n|---|:-:|\n| 1 | 2 |\r\n", [["a", "b"], ["1", "2"]]),
        ("x | y\n\n\nz | w | v", [["x", "y"], ["z", "w", "v"]]),
        ("", None),
        ("just words without separators", None),
        ("���� Z ���" + " | ", None),  # 本机 CPU 上 PP-Chart2Table 的乱码
    ],
)
def test_parse_chart_table(text: str, expected: list[list[str]] | None) -> None:
    assert parse_chart_table(text) == expected


def test_garbage_chart_output_is_warned(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="lst.workers.ocr_worker.convert"):
        assert parse_chart_table("garbage") is None
    assert any("不是数据表" in record.getMessage() for record in caplog.records)
    assert all("garbage" not in record.getMessage() for record in caplog.records)  # 日志不写内容


def test_reading_order_groups_rows() -> None:
    lines = [
        line((300, 12, 400, 38), "c"),
        line((0, 100, 100, 130), "d"),
        line((0, 10, 100, 40), "a"),
        line((150, 15, 250, 35), "b"),
    ]
    assert [item.text for item in reading_order(lines)] == ["a", "b", "c", "d"]


# ---------------------------------------------------------------- JSON 输出


def test_out_block_json_shape() -> None:
    block = OutBlock(BlockCategory.TEXT, (1, 2, 3, 4), 7, 1.5, "t", (OutLine((1, 2, 3, 4), "t", -0.2),))
    assert block.to_json() == {
        "id": "7",
        "category": "text",
        "bbox": [1, 2, 3, 4],
        "order": 7,
        "score": 1.0,  # 置信度裁剪到 [0, 1]
        "text": "t",
        "lines": [{"bbox": [1, 2, 3, 4], "text": "t", "score": 0.0}],
    }
    assert OutBlock(BlockCategory.FORMULA, (1, 2, 3, 4), 0, float("nan")).to_json()["score"] == 0.0


def test_main_process_decoder_accepts_output() -> None:
    """主进程的 decode_blocks 应原样接受全部块，不跳过任何一个（格式约定的另一侧）。"""
    blocks_module = pytest.importorskip("lst.services.ocr.blocks")
    out = blocks_to_json(convert_structure(sample_page(), CoordinateMap.identity(W, H)))
    decoded = blocks_module.decode_blocks(out, width=W, height=H)
    assert len(decoded) == len(out)


# ---------------------------------------------------------------- extract：仿 PaddleX 结果对象


class FakeLayoutBlock:
    """PaddleX 的 LayoutBlock 只用到 label、bbox、content 三个属性。"""

    def __init__(self, label: str, bbox: list[int], content: str) -> None:
        self.label, self.bbox, self.content = label, bbox, content


def fake_structure_result() -> dict[str, object]:
    image = np.zeros((H, W, 3), dtype=np.uint8)
    return {
        "width": W,
        "height": H,
        "doc_preprocessor_res": {"output_img": image},
        "parsing_res_list": [FakeLayoutBlock("text", [48, 88, 425, 162], "first line")],
        "layout_det_res": {
            "boxes": [
                {
                    "label": "text",
                    "coordinate": [np.float32(48.2), np.float32(88.1), 425.0, 162.0],
                    "score": 0.88,
                }
            ]
        },
        "overall_ocr_res": {
            "rec_boxes": np.array([[50, 90, 200, 120], [300, 400, 500, 440]], dtype=np.int16),
            "rec_texts": ["first line", "x^2"],
            "rec_scores": [np.float64(0.9), 1],
            "rec_labels": ["text", "formula"],
        },
        "table_res_list": [{"pred_html": TABLE_HTML, "cell_box_list": [np.array([1.0, 2.0, 3.0, 4.0])]}],
        "formula_res_list": [{"dt_polys": [np.float32(300.5), 400, 500, 440], "rec_formula": "x^2"}],
        "model_settings": {"use_chart_recognition": False},
    }


def test_extract_structure_from_paddlex_like_objects() -> None:
    page = extract_structure(fake_structure_result())
    assert page.output_size == (W, H)
    assert page.angle is None
    assert page.blocks == (RawBlock("text", (48.0, 88.0, 425.0, 162.0), "first line"),)
    assert len(page.regions) == 1  # np.float32 坐标也能取到
    assert page.regions[0].box == pytest.approx((48.2, 88.1, 425.0, 162.0))
    assert page.lines[0] == RawLine((50.0, 90.0, 200.0, 120.0), "first line", 0.9, True)
    assert page.lines[1].is_text is False
    assert page.tables == (RawTable(TABLE_HTML, ((1.0, 2.0, 3.0, 4.0),)),)
    assert page.formulas[0].latex == "x^2" and page.formulas[0].box[0] == pytest.approx(300.5)
    assert page.chart_recognized is False


def test_extract_reads_rotation_angle() -> None:
    result = fake_structure_result()
    result["doc_preprocessor_res"] = {"output_img": np.zeros((W, H, 3), dtype=np.uint8), "angle": 90}
    result["width"], result["height"] = H, W
    page = extract_structure(result)
    assert (page.angle, page.output_size) == (90, (H, W))


def test_extract_text_without_preprocessor_uses_input_size() -> None:
    result: dict[str, object] = {
        "rec_boxes": np.array([[1, 2, 3, 4]], dtype=np.int16),
        "rec_texts": ["a"],
        "rec_scores": np.array([0.5]),
        "doc_preprocessor_res": {},
    }
    page = extract_text(result, (640, 480))
    assert page.output_size == (640, 480)
    assert page.lines == (RawLine((1.0, 2.0, 3.0, 4.0), "a", 0.5, True),)


def test_extract_skips_malformed_entries() -> None:
    result = fake_structure_result()
    result["parsing_res_list"] = [FakeLayoutBlock("text", [1, 2, 3], "bad"), object()]
    result["overall_ocr_res"] = {"rec_boxes": [[1, 2, 3]], "rec_texts": ["x"], "rec_scores": []}
    page = extract_structure(result)
    assert page.blocks == () and page.lines == ()


# ---------------------------------------------------------------- 录制的真实输出


def load_fixture(name: str) -> RawPage:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def box(value: list[float]) -> Box:
        return (value[0], value[1], value[2], value[3])

    return RawPage(
        output_size=(data["output_size"][0], data["output_size"][1]),
        angle=data["angle"],
        lines=tuple(RawLine(box(i["box"]), i["text"], i["score"], i["is_text"]) for i in data["lines"]),
        blocks=tuple(RawBlock(i["label"], box(i["box"]), i["content"]) for i in data["blocks"]),
        regions=tuple(RawRegion(i["label"], box(i["box"]), i["score"]) for i in data["regions"]),
        tables=tuple(RawTable(i["html"], tuple(box(b) for b in i["cell_boxes"])) for i in data["tables"]),
        formulas=tuple(RawFormula(box(i["box"]), i["latex"]) for i in data["formulas"]),
        chart_recognized=data["chart_recognized"],
    )


def test_recorded_small_document() -> None:
    page = load_fixture("small_document_structure.json")
    width, height = page.output_size
    blocks = validate_blocks(
        blocks_to_json(convert_structure(page, CoordinateMap.identity(width, height))), width, height
    )
    assert [b["category"] for b in blocks] == ["title", "text", "text", "table"]
    assert blocks[0]["text"] == "屏幕翻译测试文档"
    assert "机器翻译" in cast(str, blocks[1]["text"])
    assert "Machine translation" in cast(str, blocks[2]["text"])
    assert blocks[0]["score"] == pytest.approx(0.8307710289955139)  # 取自版面检测框
    cells = cast(list[dict[str, object]], blocks[3]["cells"])
    assert [(c["row"], c["col"], c["text"]) for c in cells] == [
        (0, 0, "名称"),
        (0, 1, "数量"),
        (0, 2, "价格"),
        (1, 0, "苹果"),
        (1, 1, "12"),
        (1, 2, "3.50"),
    ]


def test_recorded_formula_document() -> None:
    page = load_fixture("formula_structure.json")
    width, height = page.output_size
    blocks = validate_blocks(
        blocks_to_json(convert_structure(page, CoordinateMap.identity(width, height))), width, height
    )
    assert [b["category"] for b in blocks] == ["title", "text", "formula", "formula"]
    assert blocks[2]["latex"] == "\\mathrm{E}=\\mathrm{m c}^{2}"
    assert blocks[3]["latex"] == "\\mathrm{f}(\\mathrm{x})=\\mathrm{ax}+\\mathrm{b}"
    # 标题块的标签被 PaddleX 改写为 doc_title，版面检测框仍是 paragraph_title：按位置配对拿到置信度。
    assert blocks[0]["score"] == pytest.approx(0.844, abs=1e-3)
