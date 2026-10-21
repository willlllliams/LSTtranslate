"""双语窗口文本的段落切分（需求 BI-06）。"""

from lst.contracts.ids import SegmentId
from lst.contracts.translation import Segment


def split_paragraphs(text: str) -> tuple[Segment, ...]:
    """按换行把文本切成段，段号依次为 p1、p2……

    空行和单个换行都分段；每段去掉首尾空白（包括全角空格），段内的空白原样保留；
    去掉空白后为空的行丢弃，不占段号。换行符按 str.splitlines 的规则识别（\\n、\\r\\n、\\r、
    U+2028 等）。文本里没有非空白内容时返回空元组。
    """
    paragraphs = (line.strip() for line in text.splitlines())
    return tuple(
        Segment(SegmentId(f"p{index}"), paragraph)
        for index, paragraph in enumerate((p for p in paragraphs if p), start=1)
    )
