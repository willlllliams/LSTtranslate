"""谷歌免费翻译接口的语言表，以及规范语言码（架构文档 2.6 节）到谷歌语言码的映射。

语言表取自参考项目 thunderbird-translate 的 src/shared/languages.ts：它为界面提供的 20 种语言
逐一写出了 translate.googleapis.com 的 tl 参数取值。这个接口没有公开文档，参考项目的表是经过
实际使用的取值，所以这里照抄，不扩充到 Cloud Translation 文档里的完整列表。

规范写法与谷歌写法不同的只有中文：简体中文 zh-Hans 写作 zh-CN，繁体中文 zh-Hant 写作 zh-TW。
源语言与目标语言用同一张表。
"""

from collections.abc import Mapping
from types import MappingProxyType

# 规范写法与谷歌写法相同的语言，顺序与参考项目的表一致。
_SAME = (
    "en", "ja", "ko", "fr", "de", "es", "pt", "it", "ru", "uk", "pl", "nl", "tr", "vi", "th", "id",
    "ar", "hi",
)  # fmt: skip

CODES: Mapping[str, str] = MappingProxyType(
    {"zh-Hans": "zh-CN", "zh-Hant": "zh-TW", **{lang: lang for lang in _SAME}}
)


def google_code(lang: str) -> str | None:
    """规范写法对应的谷歌语言码；不在语言表里时返回 None。"""
    return CODES.get(lang)


def supports_pair(source: str, target: str) -> bool:
    """两端都在语言表里且不相同。简体与繁体中文之间可以互译。"""
    return source in CODES and target in CODES and source != target
