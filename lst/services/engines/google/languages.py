"""Google Cloud Translation v2 的语言表，以及规范语言码（架构文档 2.6 节）到 Google 语言码的映射。

依据是 Cloud Translation 文档的支持语言列表（NMT 模型）。源语言与目标语言用同一张表。
规范写法与 Google 写法不同的有四处：简体中文 zh-CN、繁体中文 zh-TW、希伯来语 iw、挪威语 no。
Google 对希伯来语同时接受 iw 和 he，这里用 v2 接口一直使用的 iw；挪威语在 NMT 列表里只有 no。
"""

from collections.abc import Mapping
from types import MappingProxyType

# 规范写法与 Google 写法相同的语言。
_SAME = (
    "ar", "bg", "bn", "cs", "da", "de", "el", "en", "es", "et", "fa", "fi", "fr", "hi", "hu", "id",
    "it", "ja", "ko", "lt", "lv", "ms", "nl", "pl", "pt", "ro", "ru", "sk", "sl", "sv", "ta", "th",
    "tr", "uk", "ur", "vi",
)  # fmt: skip

CODES: Mapping[str, str] = MappingProxyType(
    {
        **{lang: lang for lang in _SAME},
        "he": "iw",
        "nb": "no",
        "zh-Hans": "zh-CN",
        "zh-Hant": "zh-TW",
    }
)


def google_code(lang: str) -> str | None:
    """规范写法对应的 Google 语言码；不在语言表里时返回 None。"""
    return CODES.get(lang)


def supports_pair(source: str, target: str) -> bool:
    """两端都在语言表里且不相同。简体与繁体中文之间可以互译。"""
    return source in CODES and target in CODES and source != target
