"""界面模块共用的文案与格式化：语言名、文件大小、模型错误原因、进度条刻度（需求 CFG-05）。

设置窗口、双语窗口、首次运行引导都要显示语言名和下载大小，这些函数以前各模块各有一份，取值范围也不一致
（设置窗口的语言名表比语言下拉框少七种语言，那七种语言只显示语言码）。现在统一放在这里。
本模块不依赖 Qt，导入时登记文案。
"""

from typing import Final

from lst.contracts.ids import Lang
from lst.contracts.models import ModelErrorCode
from lst.ui.i18n import Catalog, current_language, register_catalog, tr

LANGUAGES: Final[tuple[Lang, ...]] = tuple(
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
"""语言下拉框里的语言，按使用频率排列。代码统一用 BCP 47 规范写法。"""

LANGUAGE_NAMES: Final[dict[str, tuple[str, str]]] = {
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
"""语言码 → (中文名, 英文名)。"""

PROGRESS_STEPS: Final = 1000
"""进度条的刻度数：0 到 1 的进度乘以它取整后交给 QProgressBar。"""

CATALOG: Final[Catalog] = {
    "common.size.unknown": {"zh-Hans": "大小未知", "en": "Size unknown"},
    "common.model.title": {"zh-Hans": "{source} → {target}", "en": "{source} → {target}"},
    # 模型行的状态（设置窗口的模型页与首次运行引导共用）
    "common.model.status.missing": {"zh-Hans": "未安装", "en": "Not installed"},
    "common.model.status.installed": {
        "zh-Hans": "已安装（版本 {version}）",
        "en": "Installed (version {version})",
    },
    "common.model.status.update": {
        "zh-Hans": "已安装版本 {installed}，可更新到 {version}",
        "en": "Version {installed} installed, {version} available",
    },
    "common.model.status.queued": {"zh-Hans": "等待下载", "en": "Waiting to download"},
    "common.model.status.downloading": {
        "zh-Hans": "正在下载 {percent}%（{done} / {total}）",
        "en": "Downloading {percent}% ({done} / {total})",
    },
    "common.model.status.downloading_unknown": {
        "zh-Hans": "正在下载（已下载 {done}）",
        "en": "Downloading ({done} so far)",
    },
    "common.model.status.verifying": {"zh-Hans": "正在校验", "en": "Verifying"},
    "common.model.status.failed": {"zh-Hans": "下载失败：{reason}", "en": "Download failed: {reason}"},
    "common.model.status.cancelled": {"zh-Hans": "已取消", "en": "Cancelled"},
    # 模型错误码的原因与处理建议
    "common.model.error.models.network": {
        "zh-Hans": "网络连接失败，请检查网络或代理设置后重试",
        "en": "Network connection failed. Check your network or proxy settings and try again",
    },
    "common.model.error.models.timeout": {
        "zh-Hans": "连接超时，请稍后重试",
        "en": "The connection timed out. Try again later",
    },
    "common.model.error.models.http": {
        "zh-Hans": "下载服务器返回错误，请稍后重试",
        "en": "The download server returned an error. Try again later",
    },
    "common.model.error.models.bad_index": {
        "zh-Hans": "模型列表的格式无法识别",
        "en": "The model list has an unrecognized format",
    },
    "common.model.error.models.invalid_package": {
        "zh-Hans": "下载的文件不是可用的模型包，请重试",
        "en": "The downloaded file is not a usable model package. Try again",
    },
    "common.model.error.models.pair_mismatch": {
        "zh-Hans": "下载的模型与列表中的语言不符",
        "en": "The downloaded model does not match the listed languages",
    },
    "common.model.error.models.disk": {
        "zh-Hans": "无法写入模型目录，请检查磁盘空间和目录权限",
        "en": "Cannot write to the model folder. Check free disk space and folder permissions",
    },
    "common.model.error.models.unavailable": {
        "zh-Hans": "模型列表里没有这个模型，请先获取模型列表",
        "en": "This model is not in the model list. Fetch the list first",
    },
    "common.model.error.models.internal": {
        "zh-Hans": "内部错误，请导出诊断信息并检查日志",
        "en": "Internal error. Export diagnostics and check the logs",
    },
}

register_catalog(CATALOG)


def language_name(code: str, language: str | None = None) -> str:
    """语言码的显示名，language 缺省时按当前界面语言；表里没有的语言直接显示语言码。"""
    names = LANGUAGE_NAMES.get(code)
    if names is None:
        return code
    return names[0] if (language or current_language()) == "zh-Hans" else names[1]


def format_size(size: int | None) -> str:
    """字节数的显示文本：1 GB 以上用 GB，1 MB 以上用 MB，以下用 KB，保留一位小数；未知时显示“大小未知”。"""
    if size is None or size < 0:
        return tr("common.size.unknown")
    if size >= 1024**3:
        return f"{size / 1024**3:.1f} GB"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / 1024:.1f} KB"


def model_error_reason(code: ModelErrorCode | None) -> str:
    """模型错误码的原因说明与处理建议；没有错误码时按内部错误处理。"""
    resolved = code if code is not None else ModelErrorCode.INTERNAL
    return tr(f"common.model.error.{resolved.value}")


def clamp_fraction(value: float) -> float:
    """把进度限制在 0 到 1 之间。"""
    return min(max(value, 0.0), 1.0)


def progress_steps(fraction: float) -> int:
    """0 到 1 的进度换算成进度条的刻度值（超出范围的先截断）。"""
    return round(clamp_fraction(fraction) * PROGRESS_STEPS)
