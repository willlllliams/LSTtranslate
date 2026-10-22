"""首次运行引导的界面文案（简体中文与英文，需求 CFG-05）。导入本模块时登记文案。

按钮文案带 Qt 助记符 ``&``，按 Alt 加字母即可触发（需求 UI-07）。模型行里的按钮每行都有，不带助记符。
"""

from typing import Final

from lst.ui.i18n import Catalog, register_catalog

CATALOG: Final[Catalog] = {
    "onboarding.window.title": {"zh-Hans": "欢迎使用 LSTtranslate", "en": "Welcome to LSTtranslate"},
    "onboarding.heading": {"zh-Hans": "下载翻译模型", "en": "Download translation models"},
    "onboarding.intro": {
        "zh-Hans": "安装包里不含翻译模型。离线翻译要用到本地翻译模型，需要联网下载一次，"
        "之后翻译时不再联网。下面是当前互译语言需要的模型，确认后点击“下载”。",
        "en": "The installer does not include translation models. Offline translation needs local models, "
        "which are downloaded once; translating with them needs no network afterwards. "
        "The models for your language pair are listed below. Click Download to get them.",
    },
    "onboarding.pair": {"zh-Hans": "互译语言：{first} ↔ {second}", "en": "Language pair: {first} ↔ {second}"},
    "onboarding.models.title": {"zh-Hans": "本地翻译模型", "en": "Local translation models"},
    # 模型行（状态文字与错误原因在 lst.ui.common 里，与设置窗口的模型页共用）
    "onboarding.row.cancel": {"zh-Hans": "取消", "en": "Cancel"},
    "onboarding.row.retry": {"zh-Hans": "重试", "en": "Retry"},
    "onboarding.row.cancel.name": {"zh-Hans": "取消下载 {model}", "en": "Cancel download of {model}"},
    "onboarding.row.retry.name": {"zh-Hans": "重新下载 {model}", "en": "Download {model} again"},
    "onboarding.row.progress.name": {"zh-Hans": "{model} 的下载进度", "en": "Download progress of {model}"},
    # 模型索引
    "onboarding.index.loading": {
        "zh-Hans": "正在获取可下载的模型列表…",
        "en": "Fetching the list of available models…",
    },
    "onboarding.index.failed": {
        "zh-Hans": "无法获取模型列表：{reason}",
        "en": "Could not fetch the model list: {reason}",
    },
    "onboarding.index.empty": {
        "zh-Hans": "没有找到 {first} ↔ {second} 可用的本地翻译模型。"
        "可以在设置中换一个互译语言，或者使用在线翻译引擎。",
        "en": "No local translation models were found for {first} ↔ {second}. "
        "Choose another language pair in Settings, or use an online engine.",
    },
    "onboarding.index.retry": {"zh-Hans": "重试(&R)", "en": "&Retry"},
    # 底部
    "onboarding.download": {"zh-Hans": "下载(&D)", "en": "&Download"},
    "onboarding.download.total": {"zh-Hans": "下载(&D)（共 {size}）", "en": "&Download ({size})"},
    "onboarding.ocr.note": {
        "zh-Hans": "文字识别（OCR）模型在第一次截屏翻译时自动下载，进度显示在托盘通知里。",
        "en": "Text recognition (OCR) models download automatically the first time you use screenshot "
        "translation; progress appears in tray notifications.",
    },
    "onboarding.finish": {"zh-Hans": "完成(&F)", "en": "&Finish"},
    "onboarding.skip": {"zh-Hans": "跳过(&S)", "en": "&Skip"},
    "onboarding.skip.tip": {
        "zh-Hans": "以后可以在设置 → 模型中下载。正在进行的下载会在后台继续。",
        "en": "You can download models later in Settings → Models. "
        "Downloads in progress continue in the background.",
    },
}

register_catalog(CATALOG)
