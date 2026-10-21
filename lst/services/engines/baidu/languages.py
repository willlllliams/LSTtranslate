"""百度翻译开放平台的语言表，以及规范语言码（架构文档 2.6 节）到百度语言码的映射。

收录通用文本翻译 28 种常用语种中能对应到规范写法的 26 种，粤语（yue）与文言文（wyw）没有对应的规范写法，
未收录。语言码取自百度翻译开放平台的语种列表，百度的写法有一部分与 ISO 639-1 不同，例如日语 jp、
韩语 kor、法语 fra、西班牙语 spa、繁体中文 cht。
"""

from collections.abc import Mapping
from types import MappingProxyType

CODES: Mapping[str, str] = MappingProxyType(
    {
        "zh-Hans": "zh",
        "zh-Hant": "cht",
        "en": "en",
        "ja": "jp",
        "ko": "kor",
        "fr": "fra",
        "es": "spa",
        "th": "th",
        "ar": "ara",
        "ru": "ru",
        "pt": "pt",
        "de": "de",
        "it": "it",
        "el": "el",
        "nl": "nl",
        "pl": "pl",
        "bg": "bul",
        "et": "est",
        "da": "dan",
        "fi": "fin",
        "cs": "cs",
        "ro": "rom",
        "sl": "slo",
        "sv": "swe",
        "hu": "hu",
        "vi": "vie",
    }
)

# 百度的源语言可以写 auto，由服务端检测；目标语言不能写 auto。
AUTO = "auto"


def source_code(lang: str) -> str:
    """规范写法对应的百度源语言码。不在语言表里（包括空串）时返回 auto，交给百度检测。"""
    return CODES.get(lang, AUTO)


def target_code(lang: str) -> str | None:
    """规范写法对应的百度目标语言码；不在语言表里时返回 None。"""
    return CODES.get(lang)


def supports_pair(source: str, target: str) -> bool:
    """两端都在语言表里且不相同。简体与繁体中文之间可以互译（zh → cht）。"""
    return source in CODES and target in CODES and source != target
