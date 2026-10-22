"""谷歌、微软免费翻译接口在设置窗口里的文案：引擎类别的名称与“接口地址”参数的标题和说明。

两类引擎共用 endpoint 字段，参数卡片按字段名取文案（``settings.ef.<字段>``），所以说明同时写明
两个接口的限制。模块被导入时登记文案。

strings 模块的 text_or 只查主文案表，查不到本表的键。翻译引擎页改从本模块导入 text_or：
先查本表，再查主文案表，与 strings_ocr 的做法相同。
"""

from typing import Final

from lst.ui.i18n import Catalog, register_catalog, tr
from lst.ui.settings import strings

CATALOG: Final[Catalog] = {
    "settings.kind.google_free": {
        "zh-Hans": "谷歌翻译（免费接口）",
        "en": "Google Translate (free endpoint)",
    },
    "settings.kind.microsoft_free": {
        "zh-Hans": "微软翻译（免费接口）",
        "en": "Microsoft Translator (free endpoint)",
    },
    "settings.ef.endpoint.title": {"zh-Hans": "接口地址", "en": "Endpoint"},
    "settings.ef.endpoint.desc": {
        "zh-Hans": (
            "免费接口的地址，通常保持默认。谷歌与微软的免费接口没有公开文档，可能变动或限流；"
            "谷歌翻译在中国大陆不可用。启用后，要翻译的文本会发送给该服务。"
        ),
        "en": (
            "Address of the free endpoint; the default usually works. The free Google and Microsoft "
            "endpoints are undocumented and may change or be rate-limited; Google Translate is not "
            "available in mainland China. Once enabled, the text to translate is sent to the service."
        ),
    },
}

register_catalog(CATALOG)


def has_text(key: str) -> bool:
    """文案键是否已经登记（本模块或 strings 模块）。"""
    return key in CATALOG or strings.has_text(key)


def text_or(key: str, fallback: str) -> str:
    """有文案时返回当前语言的文案，否则返回 fallback。"""
    return tr(key) if has_text(key) else fallback


__all__ = ["CATALOG", "has_text", "text_or"]
