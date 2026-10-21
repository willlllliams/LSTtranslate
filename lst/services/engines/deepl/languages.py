"""DeepL 的语言表，以及规范语言码（架构文档 2.6 节）到 DeepL 语言码的映射。

收录 DeepL 文本翻译的核心语言，依据是 DeepL 开发者文档的支持语言说明与更新日志：
阿拉伯语 2024 年加入，泰语、希伯来语、越南语 2025 年加入。2026 年 1 月正式开放的八十余种扩展语言没有收录。

源语言码只用大写的主语言子标签，中文简繁都写作 ZH；目标语言码区分变体，
英语固定为 EN-US，葡萄牙语固定为 PT-BR，中文为 ZH-HANS 和 ZH-HANT。
"""

from collections.abc import Mapping
from types import MappingProxyType

# 规范写法与 DeepL 主语言码只差大小写的语言。
_PRIMARY = (
    "ar", "bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "he", "hu", "id", "it", "ja",
    "ko", "lt", "lv", "nb", "nl", "pl", "pt", "ro", "ru", "sk", "sl", "sv", "th", "tr", "uk", "vi",
)  # fmt: skip

SOURCE_CODES: Mapping[str, str] = MappingProxyType(
    {**{lang: lang.upper() for lang in _PRIMARY}, "zh-Hans": "ZH", "zh-Hant": "ZH"}
)

TARGET_CODES: Mapping[str, str] = MappingProxyType(
    {
        **{lang: lang.upper() for lang in _PRIMARY},
        "en": "EN-US",
        "pt": "PT-BR",
        "zh-Hans": "ZH-HANS",
        "zh-Hant": "ZH-HANT",
    }
)


def source_code(lang: str) -> str | None:
    """规范写法对应的 DeepL 源语言码；不在语言表里时返回 None。"""
    return SOURCE_CODES.get(lang)


def target_code(lang: str) -> str | None:
    """规范写法对应的 DeepL 目标语言码；不在语言表里时返回 None。"""
    return TARGET_CODES.get(lang)


def supports_pair(source: str, target: str) -> bool:
    """两端都在语言表里，并且主语言不同。

    zh-Hans 与 zh-Hant 在 DeepL 这边的源语言码都是 ZH，和目标语言 ZH-HANT 属于同一种语言，
    文档没有承诺这种请求会做简繁转换，所以按不支持处理。
    """
    src = SOURCE_CODES.get(source)
    tgt = TARGET_CODES.get(target)
    return src is not None and tgt is not None and src != tgt.split("-")[0]
