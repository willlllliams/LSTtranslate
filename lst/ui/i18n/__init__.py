"""界面文案的本地化（需求 CFG-05，架构文档 10.3 节）。

每个界面模块在自己的包里定义文案表，启动时用 register_catalog 登记；本模块只保存共用文案：
引擎与推理进程的状态、错误码的原因与处理建议、通知。业务模块只传错误码和文案键，不含界面文案。

本模块不依赖 Qt，可以在没有 QApplication 的测试里使用。
"""

import ctypes
import logging
import sys
from collections.abc import Mapping

from lst.contracts.errors import ErrorCode

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


def set_language(language: str) -> str:
    """设置界面语言，返回实际生效的语言。"auto" 或不支持的取值按系统界面语言决定。"""
    global _language
    _language = language if language in SUPPORTED else system_language()
    return _language


def current_language() -> str:
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


def error_text(code: ErrorCode | str | None) -> tuple[str, str]:
    """错误码 → (原因, 处理建议)。未知或缺失的错误码按 engine.internal 处理。"""
    try:
        resolved = ErrorCode(code) if code is not None else ErrorCode.INTERNAL
    except ValueError:
        resolved = ErrorCode.INTERNAL
    return tr(f"error.{resolved.value}.reason"), tr(f"error.{resolved.value}.suggestion")


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
    "error.engine.model_missing.reason": {
        "zh-Hans": "没有安装对应的本地模型",
        "en": "Local model not installed",
    },
    "error.engine.model_missing.suggestion": {
        "zh-Hans": "在设置 → 模型中下载该语言对的模型",
        "en": "Download a model for this language pair in Settings → Models",
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
}

register_catalog(_COMMON)
