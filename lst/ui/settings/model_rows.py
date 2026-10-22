"""模型页的列表内容（需求 MOD-01）：把 ``ModelCatalog`` 整理成一行一个模型的显示内容。

不依赖 Qt，可以在没有 QApplication 的测试里检查。行的状态与状态文字由 ``lst.ui.common.models`` 判定，
与首次运行引导相同。
"""

from collections.abc import Mapping
from dataclasses import dataclass

from lst.contracts.models import DownloadProgress, ModelCatalog, ModelInfo
from lst.ui.common.models import (
    CANCELLABLE_STATES,
    DOWNLOADABLE_STATES,
    RowState,
    model_status_text,
    model_title,
    row_state,
)
from lst.ui.common.text import format_size, language_name, model_error_reason
from lst.ui.i18n import tr

__all__ = ["ModelRow", "RowState", "build_row", "build_rows", "notice"]


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


def build_row(model: ModelInfo, progress: DownloadProgress | None) -> ModelRow:
    state = row_state(model, progress)
    return ModelRow(
        model_id=model.id,
        state=state,
        title=model_title(model),
        size=format_size(model.size_bytes),
        status=model_status_text(model, state, progress),
        can_download=state in DOWNLOADABLE_STATES or (state is RowState.INSTALLED and model.update_available),
        can_cancel=state in CANCELLABLE_STATES,
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
        return tr("settings.models.index.failed", reason=model_error_reason(catalog.index_error_code)), True
    if not catalog.index_loaded:
        return tr("settings.models.index.not_loaded"), False
    return "", False
