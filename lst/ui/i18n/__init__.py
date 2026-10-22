"""界面文案的本地化（需求 CFG-05，架构文档 10.3 节）。

每个界面模块在自己的包里定义文案表，启动时用 register_catalog 登记；本模块只保存共用文案：
引擎与推理进程的状态、错误码的原因与处理建议、通知。业务模块只传错误码和文案键，不含界面文案。

本模块不依赖 Qt，可以在没有 QApplication 的测试里使用。
"""

import ctypes
import logging
import sys
from collections.abc import Mapping
from typing import Final

from lst.contracts.config import AppConfig
from lst.contracts.errors import ErrorCode
from lst.contracts.nmt_runtime import NmtRuntimeErrorCode
from lst.contracts.ocr_engines import OcrEngineErrorCode
from lst.contracts.updates import UpdateErrorCode

logger = logging.getLogger(__name__)

SUPPORTED = ("zh-Hans", "en")
FALLBACK = "en"

type Catalog = Mapping[str, Mapping[str, str]]
"""文案键 → {界面语言 → 文案}。文案里的 {name} 由 tr 的关键字参数填充。"""

_catalog: dict[str, dict[str, str]] = {}
_language = "zh-Hans"


def register_catalog(catalog: Catalog) -> None:
    """登记一组文案。同一个键重复登记时以后登记的为准，并记一条警告。"""
    for key, texts in catalog.items():
        if key in _catalog and _catalog[key] != dict(texts):
            logger.warning("文案键 %s 被重复登记，以后登记的为准", key)
        _catalog[key] = dict(texts)


def is_registered(key: str) -> bool:
    """文案键是否已由某个界面模块登记。"""
    return key in _catalog


def set_language(language: str) -> str:
    """设置界面语言，返回实际生效的语言。"auto" 或不支持的取值按系统界面语言决定。"""
    global _language
    _language = language if language in SUPPORTED else system_language()
    return _language


def current_language() -> str:
    return _language


def apply_config_language(config: AppConfig | None) -> str:
    """按配置的 [ui].language 设置界面语言，返回当前生效的语言。

    组合根也设置界面语言，但它与各界面模块的 AppConfig 处理函数谁先执行没有保证（组合根的处理函数是协程，
    下一轮循环才执行）。每个按配置刷新文案的界面模块先调用它，刷新时用到的就是新语言。
    配置里没有这一项或类型不对时不改动当前语言。
    """
    section = config.section("ui") if config is not None else None
    raw = section.get("language") if isinstance(section, dict) else None
    if isinstance(raw, str):
        return set_language(raw)
    return _language


def system_language() -> str:
    """Windows 的用户界面语言为中文（任何地区）时返回 zh-Hans，否则返回 en。"""
    if sys.platform != "win32":
        return FALLBACK
    try:
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except (AttributeError, OSError):
        return FALLBACK
    return "zh-Hans" if langid & 0x3FF == 0x04 else FALLBACK  # 0x04 是 LANG_CHINESE 主语言号


def tr(key: str, **params: object) -> str:
    """取当前语言的文案；缺少当前语言时退回英文，键不存在时返回键本身。"""
    texts = _catalog.get(key)
    if texts is None:
        logger.warning("缺少文案键 %s", key)
        return key
    text = texts.get(_language) or texts.get(FALLBACK) or key
    if not params:
        return text
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):
        logger.warning("文案 %s 的占位符与参数不符", key)
        return text


type AnyErrorCode = ErrorCode | OcrEngineErrorCode | UpdateErrorCode | NmtRuntimeErrorCode
"""界面按错误码显示原因与处理建议的四类错误码：翻译引擎与竞速、OCR 引擎的安装与运行、应用更新、
本地翻译的显卡运行库。"""

ERROR_CODE_FAMILIES: Final[tuple[type[AnyErrorCode], ...]] = (
    ErrorCode,
    OcrEngineErrorCode,
    UpdateErrorCode,
    NmtRuntimeErrorCode,
)

_KNOWN_CODES: Final = frozenset(code.value for family in ERROR_CODE_FAMILIES for code in family)


def error_text(
    code: AnyErrorCode | str | None, default: AnyErrorCode = ErrorCode.INTERNAL
) -> tuple[str, str]:
    """错误码 → (原因, 处理建议)。

    code 可以是四类错误码中的任何一个，也可以是它们的字符串值。未知或缺失的错误码按 default 处理，
    default 缺省为 engine.internal；OCR 引擎、更新与显卡运行库的调用方传入各自的 internal。
    """
    value = str(code) if code is not None else ""
    if value not in _KNOWN_CODES:
        value = default.value
    return tr(f"error.{value}.reason"), tr(f"error.{value}.suggestion")


_COMMON: Catalog = {
    # 引擎状态（需求 TR-03）
    "health.disabled": {"zh-Hans": "未启用", "en": "Disabled"},
    "health.loading": {"zh-Hans": "加载中", "en": "Loading"},
    "health.ready": {"zh-Hans": "就绪", "en": "Ready"},
    "health.busy": {"zh-Hans": "忙碌", "en": "Busy"},
    "health.error": {"zh-Hans": "出错", "en": "Error"},
    # 推理进程名
    "worker.ocr": {"zh-Hans": "OCR", "en": "OCR"},
    "worker.nmt": {"zh-Hans": "本地翻译模型", "en": "Local translation models"},
    # 错误码（需求 OBS-04）
    "error.engine.network.reason": {"zh-Hans": "网络连接失败", "en": "Network connection failed"},
    "error.engine.network.suggestion": {
        "zh-Hans": "检查网络连接或代理设置",
        "en": "Check your network connection or proxy settings",
    },
    "error.engine.timeout.reason": {"zh-Hans": "等待超时", "en": "Timed out"},
    "error.engine.timeout.suggestion": {
        "zh-Hans": "服务响应过慢，可在设置 → 翻译引擎中调大时限",
        "en": "The service is slow; increase the time limit in Settings → Engines",
    },
    "error.engine.auth.reason": {
        "zh-Hans": "API key 无效或没有权限",
        "en": "Invalid API key or no permission",
    },
    "error.engine.auth.suggestion": {
        "zh-Hans": "在设置 → 翻译引擎中检查 API key",
        "en": "Check the API key in Settings → Engines",
    },
    "error.engine.quota.reason": {"zh-Hans": "配额或余额已用完", "en": "Quota or balance exhausted"},
    "error.engine.quota.suggestion": {
        "zh-Hans": "到服务商控制台查看用量，或暂时停用该实例",
        "en": "Check usage in the provider console, or disable this engine for now",
    },
    "error.engine.rate_limited.reason": {"zh-Hans": "请求过于频繁", "en": "Rate limited"},
    "error.engine.rate_limited.suggestion": {
        "zh-Hans": "稍后再试，或在设置中降低该实例的并发数",
        "en": "Try again later, or lower this engine's concurrency in Settings",
    },
    "error.engine.server.reason": {"zh-Hans": "服务端出错", "en": "Server error"},
    "error.engine.server.suggestion": {"zh-Hans": "稍后再试", "en": "Try again later"},
    "error.engine.bad_request.reason": {"zh-Hans": "请求被服务拒绝", "en": "Request rejected by the service"},
    "error.engine.bad_request.suggestion": {
        "zh-Hans": "检查该实例的模型名、接口地址等设置",
        "en": "Check this engine's model name, endpoint and other settings",
    },
    "error.engine.bad_response.reason": {"zh-Hans": "返回内容无法解析", "en": "Unreadable response"},
    "error.engine.bad_response.suggestion": {
        "zh-Hans": "换一个模型，或调小批量字符数",
        "en": "Try another model, or lower the batch size",
    },
    "error.engine.unsupported_language.reason": {
        "zh-Hans": "不支持这个语言对",
        "en": "Language pair not supported",
    },
    "error.engine.unsupported_language.suggestion": {
        "zh-Hans": "为该语言对启用其他引擎",
        "en": "Enable another engine for this language pair",
    },
    "error.engine.image_unsupported.reason": {
        "zh-Hans": "模型不支持图片输入",
        "en": "The model does not accept images",
    },
    "error.engine.image_unsupported.suggestion": {
        "zh-Hans": "在设置 → 翻译引擎中关闭该实例的“附带截图”，或换用支持图片输入的模型",
        "en": "Turn off “Send screenshot” for this engine in Settings → Engines, "
        "or use a model that accepts images",
    },
    "error.engine.model_missing.reason": {
        "zh-Hans": "没有安装对应的本地模型",
        "en": "Local model not installed",
    },
    "error.engine.model_missing.suggestion": {
        "zh-Hans": "在设置 → 模型中下载该语言对的模型",
        "en": "Download a model for this language pair in Settings → Models",
    },
    "error.engine.no_match.reason": {
        "zh-Hans": "词典里查不到这段文字中的任何词",
        "en": "None of the words are in the dictionary",
    },
    "error.engine.no_match.suggestion": {
        "zh-Hans": "其他引擎的译文会替换它；确认源语言设置正确",
        "en": "Other engines' translations take its place; check the source language",
    },
    "error.engine.worker_crashed.reason": {
        "zh-Hans": "推理进程出错，已重启",
        "en": "Inference process crashed and restarted",
    },
    "error.engine.worker_crashed.suggestion": {
        "zh-Hans": "反复出现时导出诊断信息并检查日志",
        "en": "If it keeps happening, export diagnostics and check the logs",
    },
    "error.engine.internal.reason": {"zh-Hans": "内部错误", "en": "Internal error"},
    "error.engine.internal.suggestion": {
        "zh-Hans": "导出诊断信息并检查日志",
        "en": "Export diagnostics and check the logs",
    },
    "error.race.missing_segment.reason": {
        "zh-Hans": "引擎没有返回这一段",
        "en": "The engine did not return this segment",
    },
    "error.race.missing_segment.suggestion": {
        "zh-Hans": "换一个模型，或调小批量字符数",
        "en": "Try another model, or lower the batch size",
    },
    "error.race.engine_removed.reason": {
        "zh-Hans": "配置已变更，该实例已替换",
        "en": "Engine replaced after a settings change",
    },
    "error.race.engine_removed.suggestion": {"zh-Hans": "重新翻译即可", "en": "Translate again"},
    # OCR 引擎的安装与运行（需求 OCRE-01 至 OCRE-06）
    "error.ocr_engine.no_language.reason": {
        "zh-Hans": "Windows 没有安装任何语言的 OCR 功能",
        "en": "Windows has no OCR language installed",
    },
    "error.ocr_engine.no_language.suggestion": {
        "zh-Hans": "在 Windows 设置 → 时间和语言 → 语言和区域中，打开要识别的语言的“语言选项”，"
        "安装“光学字符识别”，然后重新启动 LSTtranslate",
        "en": "In Windows Settings → Time & language → Language & region, open the language options of the "
        "language to recognize, install “Optical character recognition”, then restart LSTtranslate",
    },
    "error.ocr_engine.network.reason": {"zh-Hans": "网络连接失败", "en": "Network connection failed"},
    "error.ocr_engine.network.suggestion": {
        "zh-Hans": "检查网络连接或代理设置后重试",
        "en": "Check your network connection or proxy settings and try again",
    },
    "error.ocr_engine.timeout.reason": {"zh-Hans": "下载超时", "en": "The download timed out"},
    "error.ocr_engine.timeout.suggestion": {"zh-Hans": "稍后重试", "en": "Try again later"},
    "error.ocr_engine.http.reason": {
        "zh-Hans": "下载服务器返回错误",
        "en": "The download server returned an error",
    },
    "error.ocr_engine.http.suggestion": {"zh-Hans": "稍后重试", "en": "Try again later"},
    "error.ocr_engine.disk.reason": {
        "zh-Hans": "无法写入安装目录，或磁盘空间不足",
        "en": "Cannot write to the install folder, or the disk is full",
    },
    "error.ocr_engine.disk.suggestion": {
        "zh-Hans": "检查磁盘空间和目录权限后重试",
        "en": "Check free disk space and folder permissions, then try again",
    },
    "error.ocr_engine.invalid_package.reason": {
        "zh-Hans": "下载的文件没有通过校验",
        "en": "The downloaded files failed verification",
    },
    "error.ocr_engine.invalid_package.suggestion": {"zh-Hans": "重新下载", "en": "Download again"},
    "error.ocr_engine.install_failed.reason": {
        "zh-Hans": "运行环境安装失败",
        "en": "The runtime could not be installed",
    },
    "error.ocr_engine.install_failed.suggestion": {
        "zh-Hans": "重试；仍然失败时导出诊断信息并检查日志",
        "en": "Try again; if it still fails, export diagnostics and check the logs",
    },
    "error.ocr_engine.worker_failed.reason": {
        "zh-Hans": "识别进程反复退出",
        "en": "The recognition process keeps exiting",
    },
    "error.ocr_engine.worker_failed.suggestion": {
        "zh-Hans": "卸载后重新下载该引擎，或改用其他 OCR 引擎",
        "en": "Uninstall and download this engine again, or switch to another OCR engine",
    },
    "error.ocr_engine.runtime_mismatch.reason": {
        "zh-Hans": "推理设备选了显卡，但运行环境是 CPU 版，或显卡驱动不支持所需的 CUDA 版本",
        "en": "The GPU was chosen as the inference device, but the runtime is the CPU build "
        "or the graphics driver does not support the required CUDA version",
    },
    "error.ocr_engine.runtime_mismatch.suggestion": {
        "zh-Hans": "把推理设备改为“自动”或“CPU”，或在引擎卡片上换成显卡版运行环境",
        "en": "Set the inference device to Auto or CPU, or switch the engine to a GPU runtime",
    },
    "error.ocr_engine.bad_config.reason": {
        "zh-Hans": "所选模型不支持识别语言，或所选模型还没有下载",
        "en": "The selected model does not support the recognition language, or it has not been downloaded",
    },
    "error.ocr_engine.bad_config.suggestion": {
        "zh-Hans": "在识别流水线里下载所选模型，或改选支持该语言的模型",
        "en": "Download the selected model in the recognition pipeline, "
        "or pick one that supports the language",
    },
    "error.ocr_engine.internal.reason": {"zh-Hans": "内部错误", "en": "Internal error"},
    "error.ocr_engine.internal.suggestion": {
        "zh-Hans": "导出诊断信息并检查日志",
        "en": "Export diagnostics and check the logs",
    },
    # 本地翻译的显卡运行库（需求 MOD-06）
    "error.nmt_runtime.network.reason": {"zh-Hans": "网络连接失败", "en": "Network connection failed"},
    "error.nmt_runtime.network.suggestion": {
        "zh-Hans": "检查网络连接或代理设置后重试；下载来自 PyPI（pypi.org）",
        "en": "Check your network connection or proxy settings and try again; the download comes from PyPI "
        "(pypi.org)",
    },
    "error.nmt_runtime.timeout.reason": {"zh-Hans": "下载超时", "en": "The download timed out"},
    "error.nmt_runtime.timeout.suggestion": {"zh-Hans": "稍后重试", "en": "Try again later"},
    "error.nmt_runtime.http.reason": {
        "zh-Hans": "PyPI 下载服务器返回错误",
        "en": "The PyPI download server returned an error",
    },
    "error.nmt_runtime.http.suggestion": {"zh-Hans": "稍后重试", "en": "Try again later"},
    "error.nmt_runtime.disk.reason": {
        "zh-Hans": "无法写入数据目录，或磁盘空间不足",
        "en": "Cannot write to the data folder, or the disk is full",
    },
    "error.nmt_runtime.disk.suggestion": {
        "zh-Hans": "检查磁盘空间和数据目录的权限后重试",
        "en": "Check free disk space and the data folder's permissions, then try again",
    },
    "error.nmt_runtime.invalid_package.reason": {
        "zh-Hans": "下载的文件没有通过校验，或缺少所需的 DLL",
        "en": "The downloaded file failed verification or lacks the required DLLs",
    },
    "error.nmt_runtime.invalid_package.suggestion": {"zh-Hans": "重新下载", "en": "Download it again"},
    "error.nmt_runtime.internal.reason": {"zh-Hans": "内部错误", "en": "Internal error"},
    "error.nmt_runtime.internal.suggestion": {
        "zh-Hans": "导出诊断信息并检查日志",
        "en": "Export diagnostics and check the logs",
    },
    # 应用更新（需求 UPD-01 至 UPD-06）
    "error.update.network.reason": {"zh-Hans": "网络连接失败", "en": "Network connection failed"},
    "error.update.network.suggestion": {
        "zh-Hans": "检查网络连接或代理设置后重试",
        "en": "Check your network connection or proxy settings and try again",
    },
    "error.update.timeout.reason": {"zh-Hans": "连接 GitHub 超时", "en": "Connecting to GitHub timed out"},
    "error.update.timeout.suggestion": {"zh-Hans": "稍后重试", "en": "Try again later"},
    "error.update.http.reason": {
        "zh-Hans": "GitHub 拒绝了请求，可能是访问过于频繁",
        "en": "GitHub rejected the request, possibly because of rate limiting",
    },
    "error.update.http.suggestion": {"zh-Hans": "稍后重试", "en": "Try again later"},
    "error.update.bad_release.reason": {
        "zh-Hans": "发布信息不完整或格式不对",
        "en": "The release information is incomplete or malformed",
    },
    "error.update.bad_release.suggestion": {
        "zh-Hans": "稍后重试，或到发布页面手动下载",
        "en": "Try again later, or download it manually from the release page",
    },
    "error.update.checksum.reason": {
        "zh-Hans": "安装包没有通过 SHA-256 校验，已经删除",
        "en": "The installer failed the SHA-256 check and was deleted",
    },
    "error.update.checksum.suggestion": {"zh-Hans": "重新下载", "en": "Download it again"},
    "error.update.disk.reason": {"zh-Hans": "无法写入下载目录", "en": "Cannot write to the download folder"},
    "error.update.disk.suggestion": {
        "zh-Hans": "检查磁盘空间和目录权限",
        "en": "Check free disk space and folder permissions",
    },
    "error.update.not_installed.reason": {
        "zh-Hans": "源码运行不能自动安装",
        "en": "Running from source cannot install updates",
    },
    "error.update.not_installed.suggestion": {
        "zh-Hans": "更新源码后重新运行",
        "en": "Update the source code and run again",
    },
    "error.update.launch_failed.reason": {
        "zh-Hans": "无法启动安装程序",
        "en": "The installer could not be started",
    },
    "error.update.launch_failed.suggestion": {
        "zh-Hans": "到发布页面手动下载安装",
        "en": "Download and install it manually from the release page",
    },
    "error.update.internal.reason": {"zh-Hans": "内部错误", "en": "Internal error"},
    "error.update.internal.suggestion": {
        "zh-Hans": "导出诊断信息并检查日志",
        "en": "Export diagnostics and check the logs",
    },
}

register_catalog(_COMMON)
