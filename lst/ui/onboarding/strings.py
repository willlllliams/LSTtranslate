"""首次运行引导的界面文案（简体中文与英文，需求 CFG-05）与显示用的格式化函数。导入本模块时登记文案。

按钮文案带 Qt 助记符 ``&``，按 Alt 加字母即可触发（需求 UI-07）。模型行里的按钮每行都有，不带助记符。
"""

from typing import Final

from lst.contracts.models import ModelErrorCode
from lst.ui.i18n import Catalog, current_language, register_catalog, tr

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
    "onboarding.model.title": {"zh-Hans": "{source} → {target}", "en": "{source} → {target}"},
    "onboarding.size.unknown": {"zh-Hans": "大小未知", "en": "Size unknown"},
    # 模型行的状态
    "onboarding.status.missing": {"zh-Hans": "未安装", "en": "Not installed"},
    "onboarding.status.installed": {
        "zh-Hans": "已安装（版本 {version}）",
        "en": "Installed (version {version})",
    },
    "onboarding.status.update": {
        "zh-Hans": "已安装版本 {installed}，可更新到 {version}",
        "en": "Version {installed} installed, {version} available",
    },
    "onboarding.status.queued": {"zh-Hans": "等待下载", "en": "Waiting to download"},
    "onboarding.status.downloading": {
        "zh-Hans": "正在下载 {percent}%（{done} / {total}）",
        "en": "Downloading {percent}% ({done} / {total})",
    },
    "onboarding.status.downloading_unknown": {
        "zh-Hans": "正在下载（已下载 {done}）",
        "en": "Downloading ({done} so far)",
    },
    "onboarding.status.verifying": {"zh-Hans": "正在校验", "en": "Verifying"},
    "onboarding.status.failed": {"zh-Hans": "下载失败：{reason}", "en": "Download failed: {reason}"},
    "onboarding.status.cancelled": {"zh-Hans": "已取消", "en": "Cancelled"},
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
    # 错误原因（架构文档 10.3 节：业务模块只给错误码，界面按错误码显示原因）
    "onboarding.error.models.network": {
        "zh-Hans": "网络连接失败，请检查网络或代理设置后重试",
        "en": "Network connection failed. Check your network or proxy settings and try again",
    },
    "onboarding.error.models.timeout": {
        "zh-Hans": "连接超时，请稍后重试",
        "en": "The connection timed out. Try again later",
    },
    "onboarding.error.models.http": {
        "zh-Hans": "下载服务器返回错误，请稍后重试",
        "en": "The download server returned an error. Try again later",
    },
    "onboarding.error.models.bad_index": {
        "zh-Hans": "模型列表的格式无法识别",
        "en": "The model list has an unrecognized format",
    },
    "onboarding.error.models.invalid_package": {
        "zh-Hans": "下载的文件不是可用的模型包，请重试",
        "en": "The downloaded file is not a usable model package. Try again",
    },
    "onboarding.error.models.pair_mismatch": {
        "zh-Hans": "下载的模型与列表中的语言不符",
        "en": "The downloaded model does not match the listed languages",
    },
    "onboarding.error.models.disk": {
        "zh-Hans": "无法写入模型目录，请检查磁盘空间和目录权限",
        "en": "Cannot write to the model folder. Check free disk space and folder permissions",
    },
    "onboarding.error.models.unavailable": {
        "zh-Hans": "模型列表里没有这个模型，请先获取模型列表",
        "en": "This model is not in the model list. Fetch the list first",
    },
    "onboarding.error.models.internal": {
        "zh-Hans": "内部错误，请导出诊断信息并检查日志",
        "en": "Internal error. Export diagnostics and check the logs",
    },
}

register_catalog(CATALOG)

# 语言名。界面模块之间不能互相依赖（架构文档 4.2 节），所以这里自带一份；表里没有的语言直接显示语言码。
_LANGUAGE_NAMES: Final[dict[str, tuple[str, str]]] = {
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


def language_name(code: str) -> str:
    """语言码的显示名，按当前界面语言。"""
    names = _LANGUAGE_NAMES.get(code)
    if names is None:
        return code
    return names[0] if current_language() == "zh-Hans" else names[1]


def format_size(size: int | None) -> str:
    """字节数的显示文本：1 MB 以上用 MB，以下用 KB，保留一位小数；未知时显示“大小未知”。"""
    if size is None or size < 0:
        return tr("onboarding.size.unknown")
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / 1024:.1f} KB"


def error_reason(code: ModelErrorCode | None) -> str:
    """模型错误码的原因说明；没有错误码时按内部错误处理。"""
    resolved = code if code is not None else ModelErrorCode.INTERNAL
    return tr(f"onboarding.error.{resolved.value}")
