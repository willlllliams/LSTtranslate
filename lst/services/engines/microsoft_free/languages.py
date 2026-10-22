"""微软免费翻译接口的语言表，以及规范语言码（架构文档 2.6 节）到微软语言码的映射。

语言表取自参考项目 thunderbird-translate 的 src/shared/languages.ts：它为界面提供的 20 种语言
逐一写出了微软翻译 to 参数的取值。Edge 浏览器使用的免费接口没有公开文档，这里照抄参考项目经过
实际使用的取值，不扩充到 Azure Translator 文档里的完整列表。

微软的中文语言码与规范写法相同（zh-Hans、zh-Hant），表里 20 种语言的写法全部与规范写法一致。
源语言与目标语言用同一张表。
"""

from collections.abc import Mapping
from types import MappingProxyType

# 顺序与参考项目的表一致。
_LANGS = (
    "zh-Hans", "zh-Hant", "en", "ja", "ko", "fr", "de", "es", "pt", "it", "ru", "uk", "pl", "nl", "tr",
    "vi", "th", "id", "ar", "hi",
)  # fmt: skip

CODES: Mapping[str, str] = MappingProxyType({lang: lang for lang in _LANGS})


def microsoft_code(lang: str) -> str | None:
    """规范写法对应的微软语言码；不在语言表里时返回 None。"""
    return CODES.get(lang)


def supports_pair(source: str, target: str) -> bool:
    """两端都在语言表里且不相同。简体与繁体中文之间可以互译。"""
    return source in CODES and target in CODES and source != target
