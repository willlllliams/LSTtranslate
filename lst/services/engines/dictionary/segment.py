"""逐词查词典的翻译：分词、最长匹配与输出拼接。

一段原文先切成三类记号：词（字母、数字和组合附加符号连成的串，中间可以夹一个撇号，例如 don't、l'homme）、
空白和其他字符（标点、符号，每个字符单独一个记号）。之后按源语言是否用空格分词分两种处理。

有空格的语言（英语、法语、德语、俄语、韩语等）以词为单位查词典。在每个词处先找多词短语：
从当前词起向后拼接至多 MAX_PHRASE_WORDS 个词（相邻两词之间只能隔着不含换行的空白），从最长的试起，
查到就整体替换；都查不到时查单个词，单个词也查不到时原样保留。数字（例如 2024、3.14）不查词典。

没有空格的语言（中文、日文）以字为单位做正向最长匹配，词表就是这个词典包的 key：
在位置 i，从 min(包内以这个字开头的最长 key 的字数, 剩余字数) 个字开始，依次缩短一个字，取第一个查得到的
片段作为一个词，
跳到片段之后继续；一个字也查不到时这个字原样保留，从下一个字继续。夹在中文里的拉丁字母和数字串作为整体，
不会被切开，但可以与后面的汉字连成一个 key（例如 ``T恤``）。

输出保留原文的空白与标点。两处例外是为了让译文符合目标语言的书写习惯：

- 目标语言是中文或日文时，两侧都是中日文字符的行内空白去掉（换行保留），``The cat`` 译作 ``那猫``，
  没有查到的英语词两侧的空格保留；
- 源语言是中文或日文、目标语言用空格分词时，两个相邻的词之间至少有一个查到了译文，就补一个空格，
  ``我爱你`` 译作 ``I love you``。

本模块只做纯计算，不读文件，也不依赖事件循环。
"""

import re
from dataclasses import dataclass
from typing import Final

from lst.services.engines.dictionary.pack import DictPack, base_language, normalize_key

MAX_PHRASE_WORDS: Final = 3
"""有空格的语言中，一个短语最多拼接的词数，与参考实现的 MAX_PHRASE_WORDS 相同。"""

UNSPACED_LANGUAGES: Final = frozenset({"zh", "ja"})
"""词与词之间不写空格的语言（主语言子标签）。"""

# 词的字符：字母与数字（\w 去掉下划线），以及常见的组合附加符号（NFD 形式的越南语、俄语重音等）。
_WORD_CHAR: Final = r"(?:[^\W_]|[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f])"
_TOKEN: Final = re.compile(
    rf"(?P<word>{_WORD_CHAR}+(?:['’]{_WORD_CHAR}+)*)|(?P<space>\s+)|(?P<other>.)", re.DOTALL
)
_NUMBER: Final = re.compile(r"\d[\d.,:]*")

# 汉字、假名及其标记（々〆〇ー 等）。最长匹配只在这些字符之间切分。
_CJK_RANGES: Final = (
    (0x3005, 0x3007),  # 々 〆 〇
    (0x3040, 0x30FF),  # 平假名、片假名（含 ー）
    (0x31F0, 0x31FF),  # 片假名语音扩展
    (0x3400, 0x4DBF),  # 中日韩统一表意文字扩展 A
    (0x4E00, 0x9FFF),  # 中日韩统一表意文字
    (0xF900, 0xFAFF),  # 兼容表意文字
    (0xFF66, 0xFF9F),  # 半角片假名
    (0x20000, 0x3134F),  # 扩展 B 至 G
)
# 去掉空白时也算作中日文的字符：再加上中日文标点与全角字符。
_CJK_TEXT_RANGES: Final = (*_CJK_RANGES, (0x3000, 0x303F), (0xFF00, 0xFFEF))


def _in_ranges(char: str, ranges: tuple[tuple[int, int], ...]) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in ranges)


def is_cjk(char: str) -> bool:
    """汉字或假名。"""
    return _in_ranges(char, _CJK_RANGES)


def _is_cjk_text(char: str) -> bool:
    return _in_ranges(char, _CJK_TEXT_RANGES)


def is_unspaced(lang: str) -> bool:
    """语言的词与词之间不写空格（中文、日文）。"""
    return base_language(lang) in UNSPACED_LANGUAGES


@dataclass(frozen=True, slots=True)
class Translation:
    """一段原文的翻译结果。words 是原文中参与查词的词数（数字不算），hits 是其中查到译文的词数。"""

    text: str
    words: int
    hits: int


@dataclass(slots=True)
class _Piece:
    """输出的一个片段。kind 是 word、space 或 other；hit 表示词查到了译文。"""

    text: str
    kind: str
    hit: bool = False


@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    kind: str  # word、number、space、other


def tokenize(text: str) -> list[_Token]:
    """把原文切成词、数字、空白和其他字符。所有记号依次拼接等于原文。"""
    tokens: list[_Token] = []
    for match in _TOKEN.finditer(text):
        kind = match.lastgroup or "other"
        value = match.group()
        if kind == "word" and _NUMBER.fullmatch(value):
            kind = "number"
        tokens.append(_Token(value, kind))
    return tokens


def _inline_space(token: _Token) -> bool:
    return token.kind == "space" and "\n" not in token.text


# ---------------------------------------------------------------- 有空格的源语言


def _phrase_candidates(tokens: list[_Token], start: int) -> list[tuple[list[str], int]]:
    """从 tokens[start] 起的多词短语，按词数从多到少排列；每项是（词列表，短语后一个记号的下标）。"""
    words = [tokens[start].text]
    found: list[tuple[list[str], int]] = []
    last = start
    while len(words) < MAX_PHRASE_WORDS:
        gap, following = last + 1, last + 2
        if following >= len(tokens) or not _inline_space(tokens[gap]) or tokens[following].kind != "word":
            break
        words.append(tokens[following].text)
        last = following
        found.insert(0, (list(words), last + 1))
    return found


def _spaced_pieces(tokens: list[_Token], pack: DictPack) -> tuple[list[_Piece], int, int]:
    pieces: list[_Piece] = []
    words = hits = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind != "word":
            pieces.append(_Piece(token.text, "word" if token.kind == "number" else token.kind))
            index += 1
            continue
        matched = False
        for phrase, end in _phrase_candidates(tokens, index):
            translation = pack.lookup(" ".join(phrase))
            if translation is not None:
                pieces.append(_Piece(translation, "word", hit=True))
                words += len(phrase)
                hits += len(phrase)
                index = end
                matched = True
                break
        if matched:
            continue
        translation = pack.lookup(token.text)
        words += 1
        if translation is None:
            pieces.append(_Piece(token.text, "word"))
        else:
            hits += 1
            pieces.append(_Piece(translation, "word", hit=True))
        index += 1
    return pieces, words, hits


# ---------------------------------------------------------------- 没有空格的源语言


def _segment_run(run: str, pack: DictPack) -> list[_Piece]:
    """对一个由字母、数字连成的串做正向最长匹配，返回切出的词。"""
    source = pack.source
    normalized = normalize_key(run, source)
    # 归一化不改变长度时（绝大多数中日文文本），直接在归一化后的串上取片段，不必对每个片段重复归一化。
    aligned = len(normalized) == len(run)

    def key(start: int, end: int) -> str:
        return normalized[start:end] if aligned else normalize_key(run[start:end], source)

    def splits_latin(end: int) -> bool:
        """在 end 处切开会把一串拉丁字母或数字切成两半。"""
        return 0 < end < len(run) and not is_cjk(run[end - 1]) and not is_cjk(run[end])

    pieces: list[_Piece] = []
    position = 0
    length = len(run)
    while position < length:
        if is_cjk(run[position]):
            shortest = position + 1
        else:
            # 拉丁字母或数字串：作为整体，至少取到串的末尾。
            shortest = position + 1
            while shortest < length and not is_cjk(run[shortest]):
                shortest += 1
        first = normalized[position] if aligned else normalize_key(run[position], source)[:1]
        longest = min(length, max(shortest, position + pack.longest_from.get(first, 0)))
        translation: str | None = None
        end = longest
        while end >= shortest:
            if not splits_latin(end):
                translation = pack.lookup_key(key(position, end))
                if translation is not None:
                    break
            end -= 1
        if translation is not None:
            pieces.append(_Piece(translation, "word", hit=True))
            position = end
        else:
            piece = run[position:shortest]
            kind = "number" if _NUMBER.fullmatch(piece) else "word"
            pieces.append(_Piece(piece, kind))
            position = shortest
    return pieces


def _unspaced_pieces(tokens: list[_Token], pack: DictPack) -> tuple[list[_Piece], int, int]:
    pieces: list[_Piece] = []
    words = hits = 0
    for token in tokens:
        if token.kind not in ("word", "number"):
            pieces.append(_Piece(token.text, token.kind))
            continue
        for piece in _segment_run(token.text, pack):
            if piece.kind == "number":
                piece.kind = "word"
            else:
                words += 1
                hits += piece.hit
            pieces.append(piece)
    return pieces, words, hits


# ---------------------------------------------------------------- 拼接


def _join(pieces: list[_Piece], *, unspaced_source: bool, unspaced_target: bool) -> str:
    out: list[str] = []
    for index, piece in enumerate(pieces):
        previous = pieces[index - 1] if index > 0 else None
        following = pieces[index + 1] if index + 1 < len(pieces) else None
        if (
            piece.kind == "space"
            and unspaced_target
            and "\n" not in piece.text
            and previous is not None
            and following is not None
            and previous.text
            and following.text
            and _is_cjk_text(previous.text[-1])
            and _is_cjk_text(following.text[0])
        ):
            continue
        if (
            unspaced_source
            and not unspaced_target
            and piece.kind == "word"
            and previous is not None
            and previous.kind == "word"
            and (piece.hit or previous.hit)
        ):
            out.append(" ")
        out.append(piece.text)
    return "".join(out)


def translate_text(text: str, pack: DictPack) -> Translation:
    """用一个词典包逐词翻译一段原文。"""
    tokens = tokenize(text)
    unspaced_source = is_unspaced(pack.source)
    if unspaced_source:
        pieces, words, hits = _unspaced_pieces(tokens, pack)
    else:
        pieces, words, hits = _spaced_pieces(tokens, pack)
    joined = _join(pieces, unspaced_source=unspaced_source, unspaced_target=is_unspaced(pack.target))
    return Translation(joined, words, hits)
