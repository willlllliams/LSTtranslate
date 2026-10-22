"""本地翻译模型一行的状态与状态文字（需求 MOD-01、UI-08），设置窗口的模型页与首次运行引导共用。

正在排队、下载或校验的模型显示下载状态，其余按“已安装”“下载失败”“已取消”“未安装”显示。
本模块不依赖 Qt。
"""

from enum import StrEnum
from typing import Final

from lst.contracts.models import DownloadProgress, DownloadState, ModelInfo
from lst.ui.common.text import format_size, language_name, model_error_reason
from lst.ui.i18n import tr


class RowState(StrEnum):
    """模型行的状态。"""

    MISSING = "missing"
    INSTALLED = "installed"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATES: Final = frozenset({RowState.QUEUED, RowState.DOWNLOADING, RowState.VERIFYING})
"""下载进行中的状态：显示进度。"""

DOWNLOADABLE_STATES: Final = frozenset({RowState.MISSING, RowState.FAILED, RowState.CANCELLED})
"""可以下载或重试的状态（已安装但有新版本的情况由调用方另外判断）。"""

CANCELLABLE_STATES: Final = frozenset({RowState.QUEUED, RowState.DOWNLOADING})
"""可以取消下载的状态。校验阶段很短，不提供取消。"""

_ACTIVE_DOWNLOADS: Final = {
    DownloadState.QUEUED: RowState.QUEUED,
    DownloadState.DOWNLOADING: RowState.DOWNLOADING,
    DownloadState.VERIFYING: RowState.VERIFYING,
}


def row_state(model: ModelInfo, progress: DownloadProgress | None) -> RowState:
    """模型行的状态：进行中的下载优先，其次是否已安装，最后是上一次下载的结果。"""
    if progress is not None and progress.state in _ACTIVE_DOWNLOADS:
        return _ACTIVE_DOWNLOADS[progress.state]
    if model.installed:
        return RowState.INSTALLED
    if progress is not None and progress.state is DownloadState.FAILED:
        return RowState.FAILED
    if progress is not None and progress.state is DownloadState.CANCELLED:
        return RowState.CANCELLED
    return RowState.MISSING


def model_title(model: ModelInfo) -> str:
    """模型行的标题，例如“英语 → 简体中文”。"""
    return tr("common.model.title", source=language_name(model.source), target=language_name(model.target))


def download_percent(state: RowState, progress: DownloadProgress | None) -> int | None:
    """进度条的百分比：下载中且总大小已知时为 0 到 100，排队中为 0，其余为 None（不显示刻度）。"""
    if state is RowState.DOWNLOADING and progress is not None and progress.fraction is not None:
        return int(progress.fraction * 100)
    if state is RowState.QUEUED:
        return 0
    return None


def model_status_text(model: ModelInfo, state: RowState, progress: DownloadProgress | None) -> str:
    """模型行的状态文字。失败时只写原因和处理建议，技术细节只进日志（架构文档 10.3 节）。"""
    match state:
        case RowState.INSTALLED:
            if model.update_available:
                return tr(
                    "common.model.status.update", installed=model.installed_version, version=model.version
                )
            return tr("common.model.status.installed", version=model.installed_version or model.version)
        case RowState.QUEUED:
            return tr("common.model.status.queued")
        case RowState.DOWNLOADING:
            fraction = progress.fraction if progress is not None else None
            done = format_size(progress.done_bytes if progress is not None else 0)
            if fraction is None:
                return tr("common.model.status.downloading_unknown", done=done)
            return tr(
                "common.model.status.downloading",
                percent=int(fraction * 100),
                done=done,
                total=format_size(progress.total_bytes if progress is not None else None),
            )
        case RowState.VERIFYING:
            return tr("common.model.status.verifying")
        case RowState.FAILED:
            code = progress.error_code if progress is not None else None
            return tr("common.model.status.failed", reason=model_error_reason(code))
        case RowState.CANCELLED:
            return tr("common.model.status.cancelled")
        case RowState.MISSING:
            return tr("common.model.status.missing")
