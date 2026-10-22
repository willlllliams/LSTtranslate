"""双语窗口的文案表（需求 CFG-05）。导入本模块时登记文案。语言列表与语言名在 lst.ui.common.text 里。"""

from lst.contracts.ids import Lang
from lst.ui.common.text import LANGUAGES
from lst.ui.common.text import language_name as common_language_name
from lst.ui.i18n import Catalog, register_catalog, tr

__all__ = ["CATALOG", "LANGUAGES", "language_name"]


def language_name(lang: Lang | None) -> str:
    """语言代码的显示名；None 表示“自动”。"""
    if lang is None:
        return tr("bilingual.lang.auto")
    return common_language_name(lang)


CATALOG: Catalog = {
    "bilingual.window.title": {"zh-Hans": "双语互译", "en": "Bilingual Translator"},
    "bilingual.lang.auto": {"zh-Hans": "自动", "en": "Auto"},
    "bilingual.source.label": {"zh-Hans": "源语言", "en": "Source language"},
    "bilingual.target.label": {"zh-Hans": "目标语言", "en": "Target language"},
    "bilingual.swap": {"zh-Hans": "交换", "en": "Swap"},
    "bilingual.swap.tip": {"zh-Hans": "交换源语言与目标语言", "en": "Swap source and target languages"},
    "bilingual.pin": {"zh-Hans": "置顶", "en": "Pin"},
    "bilingual.pin.tip": {"zh-Hans": "让窗口保持在最前", "en": "Keep the window on top"},
    "bilingual.clear": {"zh-Hans": "清空", "en": "Clear"},
    "bilingual.clear.tip": {"zh-Hans": "清空输入和译文", "en": "Clear the input and the translation"},
    "bilingual.copy": {"zh-Hans": "复制译文", "en": "Copy translation"},
    "bilingual.copy.tip": {"zh-Hans": "复制全部译文", "en": "Copy the whole translation"},
    # 按钮提示与占位文字里的键位按当前配置填入（UI-09），没有绑定的操作不提键位。
    # 占位文字由三句拼成，英文的后两句以空格开头。
    "bilingual.tip.keys": {"zh-Hans": "{tip}（{keys}）", "en": "{tip} ({keys})"},
    "bilingual.input.label": {"zh-Hans": "原文", "en": "Source text"},
    "bilingual.input.placeholder": {
        "zh-Hans": "输入要翻译的文字，停顿后自动翻译。",
        "en": "Type text to translate. Translation starts when you pause.",
    },
    "bilingual.input.placeholder.translate": {
        "zh-Hans": "按 {keys} 立即翻译。",
        "en": " {keys} translates now.",
    },
    "bilingual.input.placeholder.close": {
        "zh-Hans": "按 {keys} 隐藏窗口。",
        "en": " {keys} hides the window.",
    },
    "bilingual.output.label": {"zh-Hans": "译文", "en": "Translation"},
    "bilingual.output.placeholder": {"zh-Hans": "译文显示在这里", "en": "The translation appears here"},
    # 翻译服务下拉框与状态行（需求 BI-04）
    "bilingual.engine.label": {"zh-Hans": "翻译服务", "en": "Translation service"},
    "bilingual.engine.billing": {"zh-Hans": "{name}（计费）", "en": "{name} (paid)"},
    "bilingual.engine.error": {"zh-Hans": "{name}（出错）", "en": "{name} (error)"},
    "bilingual.engine.tip": {
        "zh-Hans": "只用选中的服务翻译。计费服务在每次输入停顿时都会产生请求。",
        "en": "Only the selected service translates. A paid service is called at every pause in typing.",
    },
    "bilingual.engine.none": {
        "zh-Hans": "没有可用的翻译服务，请在设置的“翻译引擎”页启用一个。",
        "en": "No translation service is available. Enable one on the Translation engines page in Settings.",
    },
    "bilingual.engine.fallback": {
        "zh-Hans": "“{old}”现在不可用，暂时改用“{new}”。",
        "en": "“{old}” is not available, so “{new}” is used for now.",
    },
    "bilingual.status.failed": {
        "zh-Hans": "翻译失败：{reason}。{suggestion}",
        "en": "Translation failed: {reason}. {suggestion}",
    },
    "bilingual.status.unsupported": {
        "zh-Hans": "“{name}”不支持这段文字的语言对，显示原文。",
        "en": "“{name}” does not support this language pair; the source text is shown.",
    },
}

register_catalog(CATALOG)
