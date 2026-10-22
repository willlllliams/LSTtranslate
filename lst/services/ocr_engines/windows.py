"""Windows 自带 OCR 引擎的状态。

可识别的语言来自 ``Windows.Media.Ocr.OcrEngine.available_recognizer_languages``，取决于系统装了哪些语言的
OCR 功能。读取语言的调用封装成 ``LanguageProbe``，测试注入假函数，不调用 winrt。
"""

import logging
from collections.abc import Callable, Sequence

from lst.contracts.langs import normalize_lang
from lst.contracts.ocr_engines import OcrEngineErrorCode, OcrEngineInfo, OcrEngineKind, OcrEngineState

type LanguageProbe = Callable[[], Sequence[str]]
"""返回系统 OCR 可识别语言的 BCP-47 标签，例如 ["en-US", "zh-Hans-CN"]。失败时抛出异常。"""

WINDOWS_FEATURES = ("text",)


def probe_windows_languages() -> list[str]:
    """用 pywinrt 读取系统 OCR 可识别的语言。没有装 pywinrt 或不是 Windows 时抛 ImportError。"""
    from winrt.windows.media.ocr import OcrEngine

    return [language.language_tag for language in OcrEngine.available_recognizer_languages]


def normalize_languages(tags: Sequence[str], logger: logging.Logger) -> tuple[str, ...]:
    """把 BCP-47 标签整理成规范语言码并去重，保持原顺序；无法识别的标签记 DEBUG 后跳过。"""
    result: list[str] = []
    for tag in tags:
        try:
            code = normalize_lang(tag)
        except ValueError:
            logger.debug("忽略无法识别的 Windows OCR 语言标签 %r", tag)
            continue
        if code not in result:
            result.append(code)
    return tuple(result)


def windows_engine_info(probe: LanguageProbe, logger: logging.Logger) -> OcrEngineInfo:
    """Windows OCR 的引擎信息：有语言时 INSTALLED，没有语言时 UNAVAILABLE + NO_LANGUAGE。

    读取失败（没有 pywinrt、系统组件缺失）时也是 UNAVAILABLE，错误码为 INTERNAL，细节写日志。
    """
    try:
        tags = probe()
    except Exception as exc:
        logger.warning("无法读取 Windows OCR 的可识别语言：%s", exc)
        return OcrEngineInfo(
            OcrEngineKind.WINDOWS,
            OcrEngineState.UNAVAILABLE,
            needs_download=False,
            features=WINDOWS_FEATURES,
            error_code=OcrEngineErrorCode.INTERNAL,
            detail=f"{type(exc).__name__}: {exc}",
        )
    languages = normalize_languages(tags, logger)
    if not languages:
        logger.info("系统没有装任何语言的 OCR 功能，Windows OCR 不可用")
        return OcrEngineInfo(
            OcrEngineKind.WINDOWS,
            OcrEngineState.UNAVAILABLE,
            needs_download=False,
            features=WINDOWS_FEATURES,
            error_code=OcrEngineErrorCode.NO_LANGUAGE,
            detail="OcrEngine.available_recognizer_languages 为空",
        )
    return OcrEngineInfo(
        OcrEngineKind.WINDOWS,
        OcrEngineState.INSTALLED,
        needs_download=False,
        languages=languages,
        features=WINDOWS_FEATURES,
    )
