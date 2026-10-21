"""双语窗口文本的段落切分测试。"""

from lst.contracts.ids import SegmentId
from lst.contracts.translation import Segment
from lst.services.race import split_paragraphs

CR = chr(0x0D)
LF = chr(0x0A)
TAB = chr(0x09)
LINE_SEPARATOR = chr(0x2028)
IDEOGRAPHIC_SPACE = chr(0x3000)


def texts(segments: tuple[Segment, ...]) -> list[tuple[str, str]]:
    return [(s.id, s.text) for s in segments]


def test_each_line_is_a_paragraph() -> None:
    assert texts(split_paragraphs(LF.join(["第一段", "第二段", "第三段"]))) == [
        ("p1", "第一段"),
        ("p2", "第二段"),
        ("p3", "第三段"),
    ]


def test_blank_lines_are_dropped_without_using_ids() -> None:
    text = LF.join(["First.", "", "", "Second.", "   ", "Third."])
    assert texts(split_paragraphs(text)) == [("p1", "First."), ("p2", "Second."), ("p3", "Third.")]


def test_surrounding_whitespace_is_stripped() -> None:
    text = (CR + LF).join(
        ["  Hello world  ", f"{TAB}缩进{TAB}", f"{IDEOGRAPHIC_SPACE}全角空格开头{IDEOGRAPHIC_SPACE}"]
    )
    assert texts(split_paragraphs(text)) == [("p1", "Hello world"), ("p2", "缩进"), ("p3", "全角空格开头")]


def test_inner_whitespace_is_kept() -> None:
    assert texts(split_paragraphs(f"a  b{TAB}c")) == [("p1", f"a  b{TAB}c")]


def test_all_newline_styles_split() -> None:
    text = f"a{CR}b{CR}{LF}c{LF}d{LINE_SEPARATOR}e"
    assert texts(split_paragraphs(text)) == [("p1", "a"), ("p2", "b"), ("p3", "c"), ("p4", "d"), ("p5", "e")]


def test_empty_or_blank_text_has_no_segments() -> None:
    assert split_paragraphs("") == ()
    assert split_paragraphs("   ") == ()
    assert split_paragraphs(f"{LF}{LF} {CR}{LF}{TAB}{IDEOGRAPHIC_SPACE}{LF}") == ()


def test_single_paragraph_without_newline() -> None:
    assert split_paragraphs("Just one line.") == (Segment(SegmentId("p1"), "Just one line."),)
