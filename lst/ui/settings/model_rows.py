"""模型页的列表内容（需求 MOD-01）：把 ``ModelCatalog`` 整理成一行一个模型的显示内容。

不依赖 Qt，可以在没有 QApplication 的测试里检查。状态与进度的判定与首次运行引导相同：
正在排队、下载或校验的模型显示下载状态，其余按“已安装”“下载失败”“已取消”“未安装”显示。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from lst.contracts.models import (
    DownloadProgress,
    DownloadState,
    ModelCatalog,
    ModelErrorCode,
    ModelInfo,
)
from lst.ui.i18n import tr
from lst.ui.settings.strings import format_size, language_name


class RowState(StrEnum):
    MISSING = "missing"
    INSTALLED = "installed"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ACTIVE: Final = frozenset({RowState.QUEUED, RowState.DOWNLOADING, RowState.VERIFYING})


@dataclass(frozen=True, slots=True)
class ModelRow:
    """模型列表的一行。文字都已按当前界面语言填好。

    can_download：可以下载或重试（未安装、失败、已取消，或已安装但有新版本）；
    can_cancel：排队中或下载中；can_delete：已安装且没有正在进行的下载。
    """

    model_id: str
    state: RowState
    title: str
    size: str
    status: str
    can_download: bool
    can_cancel: bool
    can_delete: bool


def error_reason(code: ModelErrorCode | None) -> str:
    """模型错误码的原因说明与处理建议；没有错误码时按内部错误处理。"""
    resolved = code if code is not None else ModelErrorCode.INTERNAL
    return tr(f"settings.error.{resolved.value}")


def row_state(model: ModelInfo, progress: DownloadProgress | None) -> RowState:
    if progress is not None:
        match progress.state:
            case DownloadState.QUEUED:
                return RowState.QUEUED
            case DownloadState.DOWNLOADING:
                return RowState.DOWNLOADING
            case DownloadState.VERIFYING:
                return RowState.VERIFYING
            case _:
                pass
    if model.installed:
        return RowState.INSTALLED
    if progress is not None and progress.state is DownloadState.FAILED:
        return RowState.FAILED
    if progress is not None and progress.state is DownloadState.CANCELLED:
        return RowState.CANCELLED
    return RowState.MISSING


def status_text(model: ModelInfo, state: RowState, progress: DownloadProgress | None) -> str:
    match state:
        case RowState.INSTALLED:
            if model.update_available:
                return tr(
                    "settings.models.status.update", installed=model.installed_version, version=model.version
                )
            return tr("settings.models.status.installed", version=model.installed_version or model.version)
        case RowState.QUEUED:
            return tr("settings.models.status.queued")
        case RowState.DOWNLOADING:
            fraction = progress.fraction if progress is not None else None
            done = format_size(progress.done_bytes if progress is not None else 0)
            if fraction is None:
                return tr("settings.models.status.downloading_unknown", done=done)
            return tr(
                "settings.models.status.downloading",
                percent=int(fraction * 100),
                done=done,
                total=format_size(progress.total_bytes if progress is not None else None),
            )
        case RowState.VERIFYING:
            return tr("settings.models.status.verifying")
        case RowState.FAILED:
            code = progress.error_code if progress is not None else None
            return tr("settings.models.status.failed", reason=error_reason(code))
        case RowState.CANCELLED:
            return tr("settings.models.status.cancelled")
        case RowState.MISSING:
            return tr("settings.models.status.missing")


def build_row(model: ModelInfo, progress: DownloadProgress | None) -> ModelRow:
    state = row_state(model, progress)
    return ModelRow(
        model_id=model.id,
        state=state,
        title=tr(
            "settings.models.title", source=language_name(model.source), target=language_name(model.target)
        ),
        size=format_size(model.size_bytes),
        status=status_text(model, state, progress),
        can_download=state in (RowState.MISSING, RowState.FAILED, RowState.CANCELLED)
        or (state is RowState.INSTALLED and model.update_available),
        can_cancel=state in (RowState.QUEUED, RowState.DOWNLOADING),
        can_delete=state is RowState.INSTALLED,
    )


def _matches(model: ModelInfo, needle: str) -> bool:
    """筛选：关键字出现在源语言或目标语言的代码、中文名或英文名里。"""
    haystack: list[str] = []
    for code in (model.source, model.target):
        haystack.append(code.lower())
        haystack.extend(language_name(code, language).lower() for language in ("zh-Hans", "en"))
    return any(needle in text for text in haystack)


def build_rows(
    catalog: ModelCatalog | None, *, needle: str = "", installed_only: bool = False
) -> list[ModelRow]:
    """按筛选条件整理出要显示的行，顺序与目录相同（按源语言、目标语言排序）。"""
    if catalog is None:
        return []
    downloads: Mapping[str, DownloadProgress] = catalog.downloads
    text = needle.strip().lower()
    rows: list[ModelRow] = []
    for model in catalog.models:
        if text and not _matches(model, text):
            continue
        row = build_row(model, downloads.get(model.id))
        if installed_only and row.state is not RowState.INSTALLED:
            continue
        rows.append(row)
    return rows


def notice(catalog: ModelCatalog | None) -> tuple[str, bool]:
    """模型列表上方的提示和它是否是错误：获取中、获取失败、只有已安装的模型。"""
    if catalog is None:
        return "", False
    if catalog.refreshing:
        return tr("settings.models.index.loading"), False
    if catalog.index_error is not None or catalog.index_error_code is not None:
        return tr("settings.models.index.failed", reason=error_reason(catalog.index_error_code)), True
    if not catalog.index_loaded:
        return tr("settings.models.index.not_loaded"), False
    return "", False
