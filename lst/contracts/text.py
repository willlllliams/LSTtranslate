"""按文字系统拼接文本片段的规则（需求 OCR-05）。

OCR 结果在两处拼接文本：OCR 进程把 Windows OCR 的词拼成行、把无位置引擎的多行拼成一行
（lst/workers/ocr_worker），主进程把文本行拼成段落、把版面块里的折行拼成一段（lst/services/ocr）。
两处用同一套规则，所以放在 contracts 里，只依赖标准库。

规则：两个片段相接处，任一侧是中日文字符时直接相连，其余情况加一个空格。中文、日文不用空格分词；
拉丁文字、西里尔文字、韩文用空格分词，韩文因此不算中日文字符。拼接多行时另有一条断词规则：
前一行以“字母 + 连字符”结尾、后一行以小写字母开头时，视为英文单词在行尾断开，去掉空格、保留连字符。
"""

from collections.abc import Iterable
from typing import Final

CJK_RANGES: Final = (
    (0x2E80, 0x2FDF),  # CJK 部首补充、康熙部首
    (0x3000, 0x303F),  # CJK 符号和标点
    (0x3040, 0x30FF),  # 平假名、片假名
    (0x3100, 0x312F),  # 注音符号
    (0x31A0, 0x31BF),  # 注音符号扩展
    (0x31C0, 0x31EF),  # CJK 笔画
    (0x31F0, 0x31FF),  # 片假名音标扩展
    (0x3400, 0x4DBF),  # 汉字扩展 A
    (0x4E00, 0x9FFF),  # 汉字基本区
    (0xF900, 0xFAFF),  # 兼容汉字
    (0xFE30, 0xFE4F),  # CJK 兼容形式（竖排标点）
    (0xFF00, 0xFFEF),  # 半角及全角形式
    (0x20000, 0x3FFFF),  # 汉字扩展 B 及以后、兼容汉字补充
)
"""中日文字符的码位范围（闭区间）：汉字、假名、注音、笔画、中日文标点与全角形式。"""


def is_cjk(char: str) -> bool:
    """char 是中日文字符，即属于 CJK_RANGES。char 应为单个字符。"""
    code = ord(char)
    return any(start <= code <= end for start, end in CJK_RANGES)


def needs_space(left: str, right: str) -> bool:
    """两个非空片段 left、right 相接时是否加空格：left 的末字或 right 的首字是中日文字符时不加。"""
    return not (is_cjk(left[-1]) or is_cjk(right[0]))


def _hyphenated(left: str, right: str) -> bool:
    return len(left) >= 2 and left[-1] == "-" and left[-2].isalpha() and right[0].islower()


def join_lines(parts: Iterable[str]) -> str:
    """把多行文本拼成一段。

    每行先去掉首尾空白，空行跳过。相邻两行的交界处按 needs_space 决定是否加空格；
    前一行以“字母 + 连字符”结尾且后一行以小写字母开头时直接相连（断词，保留连字符）。
    """
    out = ""
    for raw in parts:
        part = raw.strip()
        if not part:
            continue
        if not out:
            out = part
        elif needs_space(out, part) and not _hyphenated(out, part):
            out += " " + part
        else:
            out += part
    return out
