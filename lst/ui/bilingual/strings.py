"""双语窗口的文案表与语言下拉框的语言列表（需求 CFG-05）。导入本模块时登记文案。"""

from lst.contracts.ids import Lang
from lst.ui.i18n import Catalog, register_catalog, tr

# 语言下拉框里“自动”之后的常用语言，按使用频率排列。代码统一用 BCP 47 规范写法。
LANGUAGES: tuple[Lang, ...] = tuple(
    Lang(code)
    for code in (
        "zh-Hans",
        "zh-Hant",
        "en",
        "ja",
        "ko",
        "fr",
        "de",
        "es",
        "ru",
        "pt",
        "it",
        "ar",
        "vi",
        "th",
        "id",
        "tr",
        "nl",
        "pl",
        "uk",
    )
)

_LANGUAGE_NAMES: dict[str, tuple[str, str]] = {
    "zh-Hans": ("简体中文", "Chinese (Simplified)"),
    "zh-Hant": ("繁体中文", "Chinese (Traditional)"),
    "en": ("英语", "English"),
    "ja": ("日语", "Japanese"),
    "ko": ("韩语", "Korean"),
    "fr": ("法语", "French"),
    "de": ("德语", "German"),
    "es": ("西班牙语", "Spanish"),
    "ru": ("俄语", "Russian"),
    "pt": ("葡萄牙语", "Portuguese"),
    "it": ("意大利语", "Italian"),
    "ar": ("阿拉伯语", "Arabic"),
    "vi": ("越南语", "Vietnamese"),
    "th": ("泰语", "Thai"),
    "id": ("印尼语", "Indonesian"),
    "tr": ("土耳其语", "Turkish"),
    "nl": ("荷兰语", "Dutch"),
    "pl": ("波兰语", "Polish"),
    "uk": ("乌克兰语", "Ukrainian"),
}


def language_name(lang: Lang | None) -> str:
    """语言代码的显示名；None 表示“自动”。"""
    if lang is None:
        return tr("bilingual.lang.auto")
    return tr(f"bilingual.lang.{lang}")


CATALOG: Catalog = {
    "bilingual.window.title": {"zh-Hans": "双语互译", "en": "Bilingual Translator"},
    "bilingual.lang.auto": {"zh-Hans": "自动", "en": "Auto"},
    **{f"bilingual.lang.{code}": {"zh-Hans": zh, "en": en} for code, (zh, en) in _LANGUAGE_NAMES.items()},
    "bilingual.source.label": {"zh-Hans": "源语言", "en": "Source language"},
    "bilingual.target.label": {"zh-Hans": "目标语言", "en": "Target language"},
    "bilingual.swap": {"zh-Hans": "交换", "en": "Swap"},
    "bilingual.swap.tip": {
        "zh-Hans": "交换源语言与目标语言（Ctrl+Shift+S）",
        "en": "Swap source and target languages (Ctrl+Shift+S)",
    },
    "bilingual.pin": {"zh-Hans": "置顶", "en": "Pin"},
    "bilingual.pin.tip": {
        "zh-Hans": "让窗口保持在最前（Ctrl+Shift+T）",
        "en": "Keep the window on top (Ctrl+Shift+T)",
    },
    "bilingual.clear": {"zh-Hans": "清空", "en": "Clear"},
    "bilingual.clear.tip": {
        "zh-Hans": "清空输入和译文（Ctrl+Shift+L）",
        "en": "Clear the input and the translation (Ctrl+Shift+L)",
    },
    "bilingual.copy": {"zh-Hans": "复制译文", "en": "Copy translation"},
    "bilingual.copy.tip": {
        "zh-Hans": "复制全部译文（Ctrl+Shift+C）",
        "en": "Copy the whole translation (Ctrl+Shift+C)",
    },
    "bilingual.input.label": {"zh-Hans": "原文", "en": "Source text"},
    "bilingual.input.placeholder": {
        "zh-Hans": "输入要翻译的文字。停顿后自动翻译，按 Ctrl+Enter 同时调用计费引擎，按 Esc 隐藏窗口。",
        "en": "Type text to translate. Translation starts when you pause; "
        "Ctrl+Enter also runs paid engines; Esc hides the window.",
    },
    "bilingual.output.label": {"zh-Hans": "译文", "en": "Translation"},
    "bilingual.output.placeholder": {"zh-Hans": "译文显示在这里", "en": "The translation appears here"},
    "bilingual.engines.title": {"zh-Hans": "引擎结果", "en": "Engine results"},
    # 引擎结果列表的状态（需求 RACE-06）
    "bilingual.state.queued": {"zh-Hans": "排队", "en": "Queued"},
    "bilingual.state.running": {"zh-Hans": "翻译中", "en": "Translating"},
    "bilingual.state.done": {"zh-Hans": "完成", "en": "Done"},
    "bilingual.state.failed": {"zh-Hans": "失败", "en": "Failed"},
    "bilingual.state.cancelled": {"zh-Hans": "已取消", "en": "Cancelled"},
    "bilingual.latency.ms": {"zh-Hans": "{ms} 毫秒", "en": "{ms} ms"},
    "bilingual.latency.cached": {"zh-Hans": "缓存", "en": "Cached"},
    "bilingual.row.copy": {"zh-Hans": "复制", "en": "Copy"},
    "bilingual.row.copy.name": {"zh-Hans": "复制 {name} 的译文", "en": "Copy the translation from {name}"},
    "bilingual.row.error": {"zh-Hans": "{reason}。{suggestion}", "en": "{reason}. {suggestion}"},
}

register_catalog(CATALOG)
