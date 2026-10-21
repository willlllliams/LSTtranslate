"""规则句子切分（架构文档 2.5 节）。

Argos 模型按句子训练，整段输入会降低译文质量，所以每段先切成句子，一段的全部句子作为一个批次翻译，
再按目标语言拼回。段内的换行原样保留：先按行切，再在每行内按句末标点切。

句末标点分两类：

- 中日文句号、感叹号、问号（。！？｡）：出现即断句。
- 英文的 . ! ? 以及省略号 …：后面紧跟中日韩文字或行尾时断句；后面是空白时，再看下一个字符和前一个词，
  下一个字符是小写字母时不断（例如 "e.g. this"），前一个词是称谓或常见缩写时不断（例如 "Mr. Smith"），
  前一个词是单个大写字母时不断（人名首字母，例如 "J. K. Rowling"）；后面是其他字符时不断（例如 "3.14"）。

句末标点后面紧跟的引号和右括号归入前一句，例如 `他说：“走吧。”` 是一句。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from lst.workers.nmt_worker.languages import sentence_joiner

# 出现即断句的句末标点。
_HARD_TERMINATORS = frozenset("。！？｡")
# 要结合上下文判断的句末标点。全角句点 ． 在中文里也常作小数点，所以按英文句点处理。
_SOFT_TERMINATORS = frozenset(".!?…．")
_TERMINATORS = _HARD_TERMINATORS | _SOFT_TERMINATORS
# 句末标点之后仍属于本句的收尾符号。
_CLOSERS = frozenset("\"'”’」』）)】]》〉")

# 后面无论跟什么都不断句的缩写（小写，不含末尾的点）：称谓、拉丁缩写。
_ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "mx", "dr", "prof", "sr", "jr", "st", "mt", "rev", "hon", "fr",
        "gen", "col", "capt", "cmdr", "lt", "sgt", "gov", "sen", "rep", "pres", "messrs", "mme", "mlle",
        "e.g", "i.e", "vs", "cf", "al", "ca", "approx", "viz", "resp", "incl",
        "jan", "feb", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    }
)  # fmt: skip
# 只在后面跟数字时不断句的缩写，例如 "No. 5"、"Fig. 3"；"I said no. Then" 照常断句。
_NUMERIC_ABBREVIATIONS = frozenset(
    {"no", "nos", "fig", "figs", "vol", "vols", "p", "pp", "ch", "sec", "eq", "art"}
)

_WORD_BEFORE = re.compile(r"([A-Za-z][A-Za-z.]*)$")


def _is_cjk(ch: str) -> bool:
    """中日韩文字：汉字、假名、谚文。"""
    code = ord(ch)
    return (
        0x3040 <= code <= 0x30FF  # 平假名、片假名
        or 0x3400 <= code <= 0x4DBF  # 汉字扩展 A
        or 0x4E00 <= code <= 0x9FFF  # 汉字
        or 0xAC00 <= code <= 0xD7AF  # 谚文音节
        or 0xF900 <= code <= 0xFAFF  # 兼容汉字
        or 0x20000 <= code <= 0x3134F  # 汉字扩展 B 至 G
    )


def _is_soft_boundary(line: str, start: int, mark: int, end: int) -> bool:
    """line[mark:end] 是一串以英文句末标点开头的标点（含收尾引号），判断它之后是否断句。"""
    if end >= len(line):
        return True
    following = line[end]
    if _is_cjk(following):
        return True
    if not following.isspace():
        return False
    rest = line[end:].lstrip()
    if rest and rest[0].islower():
        return False
    if line[mark:end].rstrip("".join(_CLOSERS)) not in (".", "．"):
        return True
    match = _WORD_BEFORE.search(line, start, mark)
    if match is None:
        return True
    word = match.group(1)
    lowered = word.lower()
    if lowered in _ABBREVIATIONS:
        return False
    if lowered in _NUMERIC_ABBREVIATIONS and rest[:1].isdigit():
        return False
    return not (len(word) == 1 and word.isupper())


def split_line(line: str) -> list[str]:
    """把一行文字切成句子，保留句末标点，去掉句子首尾的空白。"""
    sentences: list[str] = []
    start = 0
    i = 0
    n = len(line)
    while i < n:
        if line[i] not in _TERMINATORS:
            i += 1
            continue
        mark = i
        end = i + 1
        while end < n and line[end] in _TERMINATORS:
            end += 1
        while end < n and line[end] in _CLOSERS:
            end += 1
        run = line[mark:end]
        if any(c in _HARD_TERMINATORS for c in run) or _is_soft_boundary(line, start, mark, end):
            sentence = line[start:end].strip()
            if sentence:
                sentences.append(sentence)
            start = end
        i = end
    tail = line[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


@dataclass(frozen=True, slots=True)
class SentenceLayout:
    """一段文字切分后的结构。

    sentences 是按顺序排列的全部句子；line_sizes 是每行的句子数，空行为 0。
    译文按同样的结构拼回：同一行的句子用目标语言的分隔符连接，行与行之间用换行连接。
    """

    sentences: tuple[str, ...]
    line_sizes: tuple[int, ...]


def split_sentences(text: str) -> SentenceLayout:
    """把一段文字切成句子。段首段尾的空白去掉，段内的换行和空行保留在 line_sizes 里。"""
    sentences: list[str] = []
    sizes: list[int] = []
    for line in text.strip().splitlines():
        parts = split_line(line)
        sentences.extend(parts)
        sizes.append(len(parts))
    return SentenceLayout(tuple(sentences), tuple(sizes))


def join_sentences(layout: SentenceLayout, translated: Sequence[str], target: str) -> str:
    """把译文句子按 layout 的结构拼回一段。中文、日文句间不加空格，其他语言加一个空格。"""
    if len(translated) != len(layout.sentences):
        raise ValueError(f"译文句数 {len(translated)} 与原文句数 {len(layout.sentences)} 不一致")
    joiner = sentence_joiner(target)
    lines: list[str] = []
    pos = 0
    for size in layout.line_sizes:
        parts = [s.strip() for s in translated[pos : pos + size]]
        lines.append(joiner.join(p for p in parts if p))
        pos += size
    return "\n".join(lines)
