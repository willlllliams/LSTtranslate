"""规则句子切分与拼回。"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lst.workers.nmt_worker.sentences import SentenceLayout, join_sentences, split_line, split_sentences


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 中文：句末标点出现即断句，标点留在句中
        ("你好。我很好！你呢？", ["你好。", "我很好！", "你呢？"]),
        ("等等……然后呢", ["等等……", "然后呢"]),
        # 引号和括号归入前一句
        ("他说：“走吧。”然后离开了。", ["他说：“走吧。”", "然后离开了。"]),
        ("（这是注释。）下一句。", ["（这是注释。）", "下一句。"]),
        # 英文：句末标点后面要有空白
        ("He left. She stayed.", ["He left.", "She stayed."]),
        ("Really?! Yes!", ["Really?!", "Yes!"]),
        ('"Stop." He did.', ['"Stop."', "He did."]),
        ("Wait... Then go.", ["Wait...", "Then go."]),
        # 小数、网址、版本号里的点不断句
        ("Pi is 3.14 and the site is example.com today.", ["Pi is 3.14 and the site is example.com today."]),
        # 下一个字符是小写字母时不断句
        ("Wait... what happened?", ["Wait... what happened?"]),
        ("The bill, e.g. the budget, passed.", ["The bill, e.g. the budget, passed."]),
        ('"Really?" she asked.', ['"Really?" she asked.']),
        # 称谓和拉丁缩写
        ("Mr. Smith met Dr. Jones. They talked.", ["Mr. Smith met Dr. Jones.", "They talked."]),
        ("Compare A vs. B carefully.", ["Compare A vs. B carefully."]),
        ("It was e.g. Monday. Next.", ["It was e.g. Monday.", "Next."]),
        # 人名首字母
        ("J. K. Rowling wrote it.", ["J. K. Rowling wrote it."]),
        # 只在后面跟数字时不断句的缩写
        ("See Fig. 3 for details.", ["See Fig. 3 for details."]),
        ("I said no. Then I left.", ["I said no.", "Then I left."]),
        ("No. 5 is next.", ["No. 5 is next."]),
        # 缩写后面跟大写开头的新句子时照常断句
        ("We sell apples, pears, etc. The shop opens at 9 a.m. Come early.",
         ["We sell apples, pears, etc.", "The shop opens at 9 a.m.", "Come early."]),
        # 中英混排
        ("这是 OCR 的结果.然后翻译。Hello world. 再见！",
         ["这是 OCR 的结果.", "然后翻译。", "Hello world.", "再见！"]),
        ("版本 3.0 发布了。", ["版本 3.0 发布了。"]),
        # 没有句末标点
        ("no punctuation at all", ["no punctuation at all"]),
        # 空白
        ("   ", []),
        ("  Hello.   World.  ", ["Hello.", "World."]),
        ("。", ["。"]),
    ],
)  # fmt: skip
def test_split_line(text: str, expected: list[str]) -> None:
    assert split_line(text) == expected


def test_newlines_are_kept_as_line_structure() -> None:
    layout = split_sentences("First. Second.\n\n第三句。\r\nFourth")
    assert layout == SentenceLayout(("First.", "Second.", "第三句。", "Fourth"), (2, 0, 1, 1))


def test_outer_whitespace_is_dropped() -> None:
    assert split_sentences("\n\n  Hello.  \n") == SentenceLayout(("Hello.",), (1,))
    assert split_sentences(" \n\t ") == SentenceLayout((), ())


def test_join_uses_no_space_for_chinese_and_japanese() -> None:
    layout = split_sentences("One. Two.\nThree.")
    assert join_sentences(layout, ["甲。", "乙。", "丙。"], "zh-Hans") == "甲。乙。\n丙。"
    assert join_sentences(layout, ["甲。", "乙。", "丙。"], "zh-Hant") == "甲。乙。\n丙。"
    assert join_sentences(layout, ["あ。", "い。", "う。"], "ja") == "あ。い。\nう。"


def test_join_uses_one_space_for_other_languages() -> None:
    layout = split_sentences("甲。乙。\n\n丙。")
    assert join_sentences(layout, ["A.", " B. ", "C."], "en") == "A. B.\n\nC."
    assert join_sentences(layout, ["A.", "", "C."], "de") == "A.\n\nC."


def test_join_rejects_wrong_sentence_count() -> None:
    with pytest.raises(ValueError):
        join_sentences(split_sentences("One. Two."), ["x"], "en")


_text = st.text(alphabet=st.sampled_from(list("ab Z.!?。！？\n…”,3 ")), max_size=40)


@given(_text)
def test_split_keeps_every_non_space_character(text: str) -> None:
    """切分只在句子边界处断开、只去掉空白：把句子按顺序拼起来，非空白字符与原文完全相同。"""
    layout = split_sentences(text)
    assert "".join(layout.sentences).replace(" ", "") == "".join(text.split())
    assert sum(layout.line_sizes) == len(layout.sentences)
    assert all(s and s == s.strip() for s in layout.sentences)


@given(_text)
def test_identity_translation_roundtrips_line_structure(text: str) -> None:
    layout = split_sentences(text)
    joined = join_sentences(layout, list(layout.sentences), "zh-Hans")
    assert joined.count("\n") == max(len(layout.line_sizes) - 1, 0)
