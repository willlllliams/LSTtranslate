"""无位置引擎的输出（plain_text.plain_text_blocks）与按文字系统拼接文本的规则（lst.contracts.text）。"""

import pytest

from lst.contracts.text import is_cjk, join_lines
from lst.services.ocr.blocks import decode_blocks
from lst.workers.ocr_worker.blocks import blocks_to_json
from lst.workers.ocr_worker.plain_text import plain_text_blocks
from tests.workers.ocr.validation import validate_blocks


@pytest.mark.parametrize(
    ("char", "unspaced"),
    [("中", True), ("あ", True), ("カ", True), ("，", True), ("。", True), ("Ａ", True),
     ("ㄅ", True), ("ㆠ", True), ("㇀", True),
     ("a", False), ("1", False), ("한", False), ("Я", False), (",", False)],
)  # fmt: skip
def test_unspaced_characters(char: str, unspaced: bool) -> None:
    assert is_cjk(char) is unspaced


@pytest.mark.parametrize(
    ("pieces", "joined"),
    [
        (["屏幕", "翻译"], "屏幕翻译"),
        (["Hello", "world"], "Hello world"),
        (["第三行", "mixed", "English", "文本"], "第三行mixed English文本"),
        ([" 你好 ", "", "  ", "世界"], "你好世界"),
        (["안녕", "하세요"], "안녕 하세요"),
        ([], ""),
    ],
)
def test_join_lines(pieces: list[str], joined: str) -> None:
    assert join_lines(pieces) == joined


def test_plain_text_is_one_block_covering_the_screenshot() -> None:
    blocks = blocks_to_json(plain_text_blocks("第一行\r\nsecond line\n\n第三行", 640, 480))
    (block,) = validate_blocks(blocks, 640, 480)
    assert block["category"] == "text"
    assert block["bbox"] == [0, 0, 640, 480]
    assert block["order"] == 0
    assert block["score"] == 1.0
    assert block["text"] == "第一行\nsecond line\n第三行"
    assert block["lines"] == [{"bbox": [0, 0, 640, 480], "text": "第一行second line第三行", "score": 1.0}]
    (decoded,) = decode_blocks(blocks, width=640, height=480)
    assert decoded.lines[0].bbox == decoded.bbox


def test_score_is_kept() -> None:
    (block,) = blocks_to_json(plain_text_blocks("x", 10, 10, score=0.42))
    assert isinstance(block, dict)
    assert block["score"] == 0.42


@pytest.mark.parametrize("text", ["", "   ", "\n\r\n\t"])
def test_blank_text_gives_no_blocks(text: str) -> None:
    assert plain_text_blocks(text, 100, 100) == []
