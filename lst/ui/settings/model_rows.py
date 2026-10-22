"""模型页的列表内容（需求 MOD-01）：把 ``ModelCatalog`` 整理成一行一个模型的显示内容。

模型页分两个列表：本地翻译模型（NMT）与词典。``build_rows`` 按 kind 取其中一类。

不依赖 Qt，可以在没有 QApplication 的测试里检查。行的状态与状态文字由 ``lst.ui.common.models`` 判定，
与首次运行引导相同。词典包另有许可证与署名两项，署名在模型页的每个词典下面单独占一行
（CC BY-SA 4.0 要求署名可见）。
"""

from collections.abc import Mapping
from dataclasses import dataclass

from lst.contracts.models import DownloadProgress, ModelCatalog, ModelErrorCode, ModelInfo, ModelKind
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
from lst.ui.settings.strings_dict import license_name

__all__ = ["ModelRow", "RowState", "build_row", "build_rows", "dict_notice", "license_name", "notice"]


@dataclass(frozen=True, slots=True)
class ModelRow:
    """模型列表的一行。文字都已按当前界面语言填好。

    can_download：可以下载或重试（未安装、失败、已取消，或已安装但有新版本）；
    can_cancel：排队中或下载中；can_delete：已安装且没有正在进行的下载。
    license、attribution 只有词典包有：许可证的显示名与“署名：……”一行文字。
    """

    model_id: str
    state: RowState
    title: str
    size: str
    status: str
    can_download: bool
    can_cancel: bool
    can_delete: bool
    license: str = ""
    attribution: str = ""


def _status(model: ModelInfo, state: RowState, progress: DownloadProgress | None) -> str:
    if model.kind is ModelKind.DICT and state is RowState.INSTALLED and model.update_available:
        # 词典索引没有版本号，“可更新到哪个版本”无从说起，只说发布页上有更新的包。
        return tr("settings.dicts.status.update", installed=model.installed_version or "")
    return model_status_text(model, state, progress)


def build_row(model: ModelInfo, progress: DownloadProgress | None) -> ModelRow:
    state = row_state(model, progress)
    is_dict = model.kind is ModelKind.DICT
    return ModelRow(
        model_id=model.id,
        state=state,
        title=model_title(model),
        size=format_size(model.size_bytes),
        status=_status(model, state, progress),
        can_download=state in DOWNLOADABLE_STATES or (state is RowState.INSTALLED and model.update_available),
        can_cancel=state in CANCELLABLE_STATES,
        can_delete=state is RowState.INSTALLED,
        license=license_name(model.license) if is_dict else "",
        attribution=tr("settings.dicts.row.attribution", attribution=model.attribution)
        if is_dict and model.attribution
        else "",
    )


def _matches(model: ModelInfo, needle: str) -> bool:
    """筛选：关键字出现在源语言或目标语言的代码、中文名或英文名里。"""
    haystack: list[str] = []
    for code in (model.source, model.target):
        haystack.append(code.lower())
        haystack.extend(language_name(code, language).lower() for language in ("zh-Hans", "en"))
    return any(needle in text for text in haystack)


def build_rows(
    catalog: ModelCatalog | None,
    *,
    needle: str = "",
    installed_only: bool = False,
    kind: ModelKind = ModelKind.NMT,
) -> list[ModelRow]:
    """按筛选条件整理出一类模型要显示的行，顺序与目录相同（按源语言、目标语言排序）。"""
    if catalog is None:
        return []
    downloads: Mapping[str, DownloadProgress] = catalog.downloads
    text = needle.strip().lower()
    rows: list[ModelRow] = []
    for model in catalog.of_kind(kind):
        if text and not _matches(model, text):
            continue
        row = build_row(model, downloads.get(model.id))
        if installed_only and row.state is not RowState.INSTALLED:
            continue
        rows.append(row)
    return rows


def _index_notice(
    prefix: str, refreshing: bool, loaded: bool, error: str | None, code: ModelErrorCode | None
) -> tuple[str, bool]:
    if refreshing:
        return tr(f"{prefix}.index.loading"), False
    if error is not None or code is not None:
        return tr(f"{prefix}.index.failed", reason=model_error_reason(code)), True
    if not loaded:
        return tr(f"{prefix}.index.not_loaded"), False
    return "", False


def notice(catalog: ModelCatalog | None) -> tuple[str, bool]:
    """本地翻译模型列表上方的提示和它是否是错误：获取中、获取失败、只有已安装的模型。"""
    if catalog is None:
        return "", False
    return _index_notice(
        "settings.models",
        catalog.refreshing,
        catalog.index_loaded,
        catalog.index_error,
        catalog.index_error_code,
    )


def dict_notice(catalog: ModelCatalog | None) -> tuple[str, bool]:
    """词典列表上方的提示，规则与本地翻译模型相同，依据的是词典索引的状态。"""
    if catalog is None:
        return "", False
    return _index_notice(
        "settings.dicts",
        catalog.dict_refreshing,
        catalog.dict_index_loaded,
        catalog.dict_index_error,
        catalog.dict_index_error_code,
    )
