"""词典包的格式：key 归一化、包头与词条的解析。

格式与构建规则以 thunderbird-translate 仓库的 docs/dictionary-packs.md 为准。

一个包对应一个翻译方向，文件名 ``dict-<src>-<tgt>.tsv.gz``，内容是 gzip 压缩的 UTF-8 文本，
每行以 ``\\n`` 结尾，字段之间用制表符分隔：

- 第 1 行是包头 ``#dict  <src>  <tgt>  <packVersion>  <sourceName>  <license>``；
- 词条行 ``w  <key>  <translation>``：key 的译文；
- 变形行 ``f  <key>  <lemmaKey>``：key 是 lemmaKey 的变形，例如 ``f  went  go``。

查词时词条优先，没有词条时经变形行找到原形再取原形的译文，变形不做链式解析。

本模块只用标准库，函数都是同步的；解压与解析由调用方放进线程执行。
"""

import gzip
import unicodedata
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

HEADER_TAG: Final = "#dict"
ENTRY_TAG: Final = "w"
FORM_TAG: Final = "f"

MAX_UNSPACED_KEY_CHARS: Final = 16
"""无空格语言（中文、日文）最长匹配时尝试的最大字数。词典里更长的 key 极少，限制它让切分的耗时有上界。"""

# 俄语词典的词形带重音符号，正文不带：key 里去掉组合重音符（U+0301 锐音、U+0300 抑音）。
_RUSSIAN_STRESS: Final[dict[int, None]] = {0x0300: None, 0x0301: None}
# 土耳其语（与阿塞拜疆语）的大小写规则：I 的小写是无点的 ı，İ 的小写是 i。
_TURKIC: Final = frozenset({"tr", "az"})
_TURKIC_LOWER: Final = str.maketrans({"I": "ı", "İ": "i"})


class PackFormatError(ValueError):
    """文件不是可用的词典包：不是 gzip、不是 UTF-8，或者包头不对。"""


def base_language(code: str) -> str:
    """语言码的主语言子标签：``zh-Hans`` → ``zh``。"""
    return code.split("-", 1)[0].lower()


def normalize_key(text: str, lang: str) -> str:
    """把一段文字归一化成词典包的 key，与构建脚本的 normalizeKey 相同。

    1. Unicode NFC 归一化；
    2. 去掉首尾空白，内部连续空白合并为一个空格；
    3. 俄语去掉重音符号（U+0301、U+0300），ё、й 的附加符号保留；
    4. 按源语言转小写，等同于 JavaScript 的 ``toLocaleLowerCase(lang)``：土耳其语把 I 转成 ı、İ 转成 i，
       其他语言用 Unicode 默认的小写映射。汉字、假名、谚文没有大小写，只有夹带的拉丁字母会变。
    """
    key = " ".join(unicodedata.normalize("NFC", text).split())
    primary = base_language(lang)
    if primary == "ru":
        key = unicodedata.normalize("NFC", unicodedata.normalize("NFD", key).translate(_RUSSIAN_STRESS))
    if primary in _TURKIC:
        key = key.translate(_TURKIC_LOWER)
    return key.lower()


@dataclass(frozen=True, slots=True)
class PackHeader:
    """包头：语言对、构建日期（YYYYMMDD）、数据源名称与许可证（SPDX 标识）。"""

    source: str
    target: str
    version: str
    source_name: str
    license: str


@dataclass(frozen=True, slots=True, eq=False)
class DictPack:
    """读入内存的一个词典包。

    entries 是 key → 译文，forms 是变形 key → 原形 key。无空格语言做最长匹配时，用 longest_from 确定
    在一个位置从多长的片段试起：它是“首字 → 以这个字开头的 key 的最大字数”（不超过 MAX_UNSPACED_KEY_CHARS），
    首字不在表里说明以它开头的片段都查不到。
    """

    header: PackHeader
    entries: dict[str, str] = field(repr=False)
    forms: dict[str, str] = field(repr=False)
    longest_from: dict[str, int] = field(repr=False, default_factory=dict[str, int])

    @property
    def source(self) -> str:
        return self.header.source

    @property
    def target(self) -> str:
        return self.header.target

    def lookup_key(self, key: str) -> str | None:
        """按已经归一化的 key 查词：词条优先，否则经变形行查原形的译文；查不到时返回 None。"""
        translation = self.entries.get(key)
        if translation is not None:
            return translation
        lemma = self.forms.get(key)
        return None if lemma is None else self.entries.get(lemma)

    def lookup(self, text: str) -> str | None:
        """把原文归一化后查词。"""
        return self.lookup_key(normalize_key(text, self.header.source))


def parse_header(line: str) -> PackHeader:
    """解析包头行（不含换行符）。格式不对时抛出 PackFormatError。"""
    fields = line.rstrip("\r").split("\t")
    if len(fields) < 6 or fields[0] != HEADER_TAG:
        raise PackFormatError("包头不是 #dict 开头的 6 个字段")
    _, source, target, version, source_name, license_id = fields[:6]
    if not source or not target:
        raise PackFormatError("包头缺少语言对")
    return PackHeader(source, target, version, source_name, license_id)


def parse_pack_text(text: str) -> DictPack:
    """解析解压后的包内容。

    包头不对时抛出 PackFormatError。其余各行中，标记不是 w、f 或者字段不足三个的行跳过，与参考实现相同：
    一行坏数据不让整个包不可用。同一个 key 出现多次时后一行生效。
    """
    lines = text.split("\n")
    header = parse_header(lines[0])
    entries: dict[str, str] = {}
    forms: dict[str, str] = {}
    for line in lines[1:]:
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[1]:
            continue
        tag, key, value = parts
        if tag == ENTRY_TAG:
            entries[key] = value.rstrip("\r")
        elif tag == FORM_TAG:
            forms[key] = value.rstrip("\r")
    longest_from: dict[str, int] = {}
    for table in (entries, forms):
        for key in table:
            first, size = key[0], min(len(key), MAX_UNSPACED_KEY_CHARS)
            if size > longest_from.get(first, 0):
                longest_from[first] = size
    return DictPack(header, entries, forms, longest_from)


def decode_pack(data: bytes) -> DictPack:
    """解压并解析一个词典包文件的内容。不是 gzip 或不是 UTF-8 时抛出 PackFormatError。"""
    try:
        raw = gzip.decompress(data)
    except (OSError, EOFError, zlib.error) as exc:
        raise PackFormatError(f"不是有效的 gzip 文件（{type(exc).__name__}）") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackFormatError("内容不是 UTF-8 文本") from exc
    return parse_pack_text(text)


def read_pack_file(path: Path) -> DictPack:
    """读取并解析一个词典包文件。读文件失败时抛出 OSError，格式不对时抛出 PackFormatError。"""
    return decode_pack(path.read_bytes())
