"""截屏与识别页的 OCR 引擎卡片内容（需求 OCRE-01 至 OCRE-06）：把 ``OcrEngineInfo`` 整理成显示内容。

不依赖 Qt，可以在没有 QApplication 的测试里检查。每种状态对应的文字、图标、语气和可用的按钮：

| 状态 | 文字 | 按钮 |
| --- | --- | --- |
| 不可用 | 不可用及原因；没有 OCR 语言时附上添加语言的步骤 | 无 |
| 未安装 | 未安装 | 下载 |
| 排队、下载中 | 等待安装、下载进度 | 取消 |
| 安装中 | 正在安装和校验 | 取消（不可用：此时已不能取消） |
| 已安装、加载中、就绪 | 对应状态 | 卸载（需要下载的引擎）、设为当前引擎 |
| 出错 | 原因与处理建议 | 重试（安装失败时）、卸载（需要下载的引擎） |

当前引擎不显示“设为当前引擎”，改为“当前引擎”标识。

运行环境的版本（需求 OCRE-07，``build_variant_view``）：引擎有两个以上可装版本时，卡片上有一个版本下拉框，
推荐版本在选项里标出，下拉框下面写出推荐理由，例如“推荐：CUDA（检测到 NVIDIA 显卡）”。

- 未安装或安装失败：下拉框选的是要安装的版本，默认是推荐版本，“下载”“重试”按它安装；
- 排队、下载、安装中：下拉框不可用；
- 已安装：状态文字后面写出已装版本，下拉框默认选中已装版本；选了别的版本后“更换版本”可用。

已安装的引擎可以“管理”：在卡片下方的流水线区域显示它的环节、模型与参数（``can_manage``）。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from lst.contracts.ocr_engines import (
    OcrEngineErrorCode,
    OcrEngineInfo,
    OcrEngineKind,
    OcrEngineState,
    OcrRuntimeVariant,
)
from lst.ui.common.text import clamp_fraction, format_size, language_name
from lst.ui.i18n import error_text, tr
from lst.ui.theme import Glyph

FEATURE_ORDER: Final = ("text", "layout", "table", "formula")
"""能力的显示顺序，与 OcrEngineInfo.features 的取值相同。"""

_PROGRESS_STATES: Final = frozenset({OcrEngineState.DOWNLOADING, OcrEngineState.INSTALLING})
_USABLE_STATES: Final = frozenset({OcrEngineState.INSTALLED, OcrEngineState.LOADING, OcrEngineState.READY})
_BUSY_STATES: Final = frozenset(
    {OcrEngineState.QUEUED, OcrEngineState.DOWNLOADING, OcrEngineState.INSTALLING}
)


class Tone(StrEnum):
    """状态文字与图标的语气：普通（次要色）、进行中或就绪（强调色）、出错（错误色）。"""

    NORMAL = "secondary"
    ACTIVE = "accent"
    ERROR = "critical"


@dataclass(frozen=True, slots=True)
class EngineView:
    """一张 OCR 引擎卡片的显示内容。文字都已按当前界面语言填好。

    progress 是进度条的取值：None 表示不显示进度条，-1 表示进度未知（忙碌动画），否则在 0 到 1 之间。
    can_cancel 决定“取消”是否显示，cancel_enabled 决定它是否可用（安装阶段显示但不可用）。
    """

    kind: OcrEngineKind
    title: str
    description: str
    status: str
    tone: Tone
    glyph: str
    suggestion: str
    help: str
    languages: str
    progress: float | None
    current: bool
    can_download: bool
    can_cancel: bool
    cancel_enabled: bool
    can_retry: bool
    can_uninstall: bool
    can_use: bool
    can_manage: bool = False
    """引擎已安装（或不需要安装），可以在流水线区域管理它的模型与参数。"""


@dataclass(frozen=True, slots=True)
class VariantView:
    """引擎卡片上的运行环境版本一行。

    - visible：引擎有两个以上可装版本时才显示这一行；
    - options：(版本, 显示名)，推荐版本的显示名带“（推荐）”；
    - chosen：下拉框选中的版本；enabled：下拉框是否可用（安装进行中不可用）；
    - recommendation：推荐的版本与理由，没有推荐时为空串；
    - can_change：已安装且选中的版本不同于已装版本，“更换版本”可用；show_change：显示“更换版本”按钮。
    """

    visible: bool
    options: tuple[tuple[OcrRuntimeVariant, str], ...]
    chosen: OcrRuntimeVariant | None
    enabled: bool
    recommendation: str
    show_change: bool
    can_change: bool


def variant_name(variant: OcrRuntimeVariant) -> str:
    return tr(f"settings.ocr_variant.{variant.value}")


def option_text(variant: OcrRuntimeVariant) -> str:
    """下拉框里一个版本的显示名，例如“CUDA · NVIDIA 显卡”。"""
    return tr(
        "settings.ocr_variant.option",
        variant=variant_name(variant),
        hint=tr(f"settings.ocr_variant.hint.{variant.value}"),
    )


def recommendation_text(info: OcrEngineInfo) -> str:
    """推荐版本与理由，例如“推荐：CUDA（检测到 NVIDIA 显卡）”。没有推荐时为空串。"""
    variant = info.recommended_variant
    if variant is None:
        return ""
    reason = tr(f"settings.ocr_variant.reason.{variant.value}")
    return tr("settings.ocr_variant.recommended", variant=variant_name(variant), reason=reason)


def default_variant(info: OcrEngineInfo) -> OcrRuntimeVariant | None:
    """下拉框默认选中的版本：已装版本，其次推荐版本，再次第一个可装版本。"""
    for variant in (info.installed_variant, info.recommended_variant):
        if variant is not None and variant in info.variants:
            return variant
    return info.variants[0] if info.variants else None


def build_variant_view(info: OcrEngineInfo, chosen: OcrRuntimeVariant | None) -> VariantView:
    """chosen 是用户在下拉框里选的版本，没有选过（或选的版本已不可装）时用 default_variant。"""
    if chosen not in info.variants:
        chosen = default_variant(info)
    options = tuple(
        (
            variant,
            tr("settings.ocr_variant.option_recommended", option=option_text(variant))
            if variant is info.recommended_variant
            else option_text(variant),
        )
        for variant in info.variants
    )
    installed = info.state in _USABLE_STATES and info.installed_variant is not None
    return VariantView(
        visible=info.needs_download and len(info.variants) > 1,
        options=options,
        chosen=chosen,
        enabled=info.state not in _BUSY_STATES and info.state is not OcrEngineState.UNAVAILABLE,
        recommendation=recommendation_text(info),
        show_change=installed,
        can_change=installed and chosen is not None and chosen is not info.installed_variant,
    )


def engine_title(kind: OcrEngineKind) -> str:
    return tr(f"settings.ocr_engine.{kind.value}.title")


def features_text(features: tuple[str, ...]) -> str:
    """能力列表的显示文字，例如“文本、版面、表格、公式”，按 FEATURE_ORDER 排列，未知的能力跟在后面。"""
    ordered = [name for name in FEATURE_ORDER if name in features]
    ordered.extend(name for name in features if name not in FEATURE_ORDER)
    names = [tr(f"settings.ocr_engine.feature.{name}") if name in FEATURE_ORDER else name for name in ordered]
    return tr("settings.ocr_engine.feature.separator").join(names)


def download_text(info: OcrEngineInfo) -> str:
    if not info.needs_download:
        return tr("settings.ocr_engine.no_download")
    if info.download_bytes is None:
        return tr("settings.ocr_engine.download_unknown")
    return tr("settings.ocr_engine.download_size", size=format_size(info.download_bytes))


def description_text(info: OcrEngineInfo) -> str:
    """卡片的说明：引擎简介，下一行是能力与下载大小。"""
    summary = tr(
        "settings.ocr_engine.summary",
        features=tr("settings.ocr_engine.features", features=features_text(info.features)),
        download=download_text(info),
    )
    return f"{tr(f'settings.ocr_engine.{info.kind.value}.desc')}\n{summary}"


def _status(info: OcrEngineInfo) -> tuple[str, Tone, str]:
    """状态文字、语气与图标。"""
    match info.state:
        case OcrEngineState.UNAVAILABLE:
            reason, _ = error_text(info.error_code, OcrEngineErrorCode.INTERNAL)
            return tr("settings.ocr_engine.status.unavailable", reason=reason), Tone.ERROR, Glyph.BLOCKED
        case OcrEngineState.NOT_INSTALLED:
            return tr("settings.ocr_engine.status.not_installed"), Tone.NORMAL, Glyph.DOWNLOAD
        case OcrEngineState.QUEUED:
            return tr("settings.ocr_engine.status.queued"), Tone.NORMAL, Glyph.RECENT
        case OcrEngineState.DOWNLOADING:
            if info.progress is None:
                return tr("settings.ocr_engine.status.downloading_unknown"), Tone.ACTIVE, Glyph.DOWNLOAD
            percent = int(clamp_fraction(info.progress) * 100)
            return tr("settings.ocr_engine.status.downloading", percent=percent), Tone.ACTIVE, Glyph.DOWNLOAD
        case OcrEngineState.INSTALLING:
            return tr("settings.ocr_engine.status.installing"), Tone.ACTIVE, Glyph.SYNC
        case OcrEngineState.INSTALLED:
            key = "installed" if info.needs_download else "builtin"
            return tr(f"settings.ocr_engine.status.{key}"), Tone.NORMAL, Glyph.COMPLETED
        case OcrEngineState.LOADING:
            return tr("settings.ocr_engine.status.loading"), Tone.ACTIVE, Glyph.SYNC
        case OcrEngineState.READY:
            return tr("settings.ocr_engine.status.ready"), Tone.ACTIVE, Glyph.COMPLETED
        case OcrEngineState.ERROR:
            reason, _ = error_text(info.error_code, OcrEngineErrorCode.INTERNAL)
            return tr("settings.ocr_engine.status.error", reason=reason), Tone.ERROR, Glyph.ERROR


def _progress(info: OcrEngineInfo) -> float | None:
    if info.state not in _PROGRESS_STATES:
        return None
    return -1.0 if info.progress is None else clamp_fraction(info.progress)


def build_view(info: OcrEngineInfo, *, current: bool) -> EngineView:
    """一个引擎的卡片内容。current 表示它是配置里当前使用的引擎。"""
    status, tone, glyph = _status(info)
    state, code = info.state, info.error_code
    if state in _USABLE_STATES and info.installed_variant is not None and len(info.variants) > 1:
        status = tr(
            "settings.ocr_variant.installed", status=status, variant=variant_name(info.installed_variant)
        )
    no_language = state is OcrEngineState.UNAVAILABLE and code is OcrEngineErrorCode.NO_LANGUAGE
    suggestion = ""
    if state in (OcrEngineState.UNAVAILABLE, OcrEngineState.ERROR) and not no_language:
        _, advice = error_text(code, OcrEngineErrorCode.INTERNAL)
        suggestion = tr("settings.ocr_engine.suggestion", suggestion=advice)
    worker_failed = state is OcrEngineState.ERROR and code is OcrEngineErrorCode.WORKER_FAILED
    languages = ""
    # 只有 Windows 的语言因机器而异，值得列在卡片上；RapidOCR 的约 50 种语言固定不变，只列在识别语言下拉框里。
    if info.kind is OcrEngineKind.WINDOWS and info.languages:
        separator = tr("settings.ocr_engine.feature.separator")
        languages = tr(
            "settings.ocr_engine.languages",
            languages=separator.join(language_name(code) for code in info.languages),
        )
    return EngineView(
        kind=info.kind,
        title=engine_title(info.kind),
        description=description_text(info),
        status=status,
        tone=tone,
        glyph=glyph,
        suggestion=suggestion,
        help=tr("settings.ocr_engine.no_language.help") if no_language else "",
        languages=languages,
        progress=_progress(info),
        current=current,
        can_download=info.needs_download and state is OcrEngineState.NOT_INSTALLED,
        can_cancel=state in (OcrEngineState.QUEUED, OcrEngineState.DOWNLOADING, OcrEngineState.INSTALLING),
        cancel_enabled=state in (OcrEngineState.QUEUED, OcrEngineState.DOWNLOADING),
        can_retry=info.needs_download and state is OcrEngineState.ERROR and not worker_failed,
        can_uninstall=info.needs_download and (state in _USABLE_STATES or state is OcrEngineState.ERROR),
        can_use=not current and (state in _USABLE_STATES or worker_failed),
        can_manage=state in _USABLE_STATES or worker_failed,
    )


__all__ = [
    "FEATURE_ORDER",
    "EngineView",
    "Tone",
    "VariantView",
    "build_variant_view",
    "build_view",
    "default_variant",
    "description_text",
    "engine_title",
    "option_text",
    "recommendation_text",
    "variant_name",
]
