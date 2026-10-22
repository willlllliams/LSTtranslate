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
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from lst.contracts.ocr_engines import OcrEngineErrorCode, OcrEngineInfo, OcrEngineKind, OcrEngineState
from lst.ui.common.text import clamp_fraction, format_size, language_name
from lst.ui.i18n import error_text, tr
from lst.ui.theme import Glyph

FEATURE_ORDER: Final = ("text", "layout", "table", "formula")
"""能力的显示顺序，与 OcrEngineInfo.features 的取值相同。"""

_PROGRESS_STATES: Final = frozenset({OcrEngineState.DOWNLOADING, OcrEngineState.INSTALLING})
_USABLE_STATES: Final = frozenset({OcrEngineState.INSTALLED, OcrEngineState.LOADING, OcrEngineState.READY})


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
    no_language = state is OcrEngineState.UNAVAILABLE and code is OcrEngineErrorCode.NO_LANGUAGE
    suggestion = ""
    if state in (OcrEngineState.UNAVAILABLE, OcrEngineState.ERROR) and not no_language:
        _, advice = error_text(code, OcrEngineErrorCode.INTERNAL)
        suggestion = tr("settings.ocr_engine.suggestion", suggestion=advice)
    worker_failed = state is OcrEngineState.ERROR and code is OcrEngineErrorCode.WORKER_FAILED
    languages = ""
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
    )


__all__ = ["FEATURE_ORDER", "EngineView", "Tone", "build_view", "description_text", "engine_title"]
