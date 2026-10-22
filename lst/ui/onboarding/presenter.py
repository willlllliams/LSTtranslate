"""首次运行引导的展示器（需求 UI-08、INS-03，架构文档 11.1、11.7 节）。

展示器订阅 ``ModelCatalog`` 与 ``AppConfig``，按当前界面语言生成一份纯数据的画面模型 ``OnboardingScreen``
交给视图绘制；视图把按钮操作交回展示器，展示器转换成模型管理的命令。本模块不依赖 Qt，
画面模型的内容（列出哪些模型、每行的状态和按钮）都可以在没有 Qt 对象的情况下检查。

联网时机（需求 PRV-01）：引导窗口显示时，如果本次运行还没有获取过模型索引（管理词典包时还有词典索引），
展示器发送一次 ``RefreshModelCatalog``；模型的下载只在用户点击“下载”或“重试”后发生。

词典：模型管理模块管理词典包时，引导在本地翻译模型下面列出互译对两个方向的词典包（``default_dict_ids``），
每行带许可证与署名。“同时下载词典”默认勾选，勾选时“下载”一并下载这些词典包；词典是可选的，
“完成”只看本地翻译模型是否装齐。
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from lst.contracts.config import AppConfig
from lst.contracts.ids import Lang
from lst.contracts.langs import normalize_lang
from lst.contracts.models import (
    CancelModelDownload,
    DownloadModels,
    DownloadProgress,
    ModelCatalog,
    ModelInfo,
    ModelKind,
    RefreshModelCatalog,
    default_dict_ids,
    default_model_ids,
)
from lst.kernel import EventBus
from lst.ui.common.models import (
    ACTIVE_STATES,
    CANCELLABLE_STATES,
    DOWNLOADABLE_STATES,
    RowState,
    download_percent,
    model_status_text,
    model_title,
    row_state,
)
from lst.ui.common.text import format_size, language_name, model_error_reason
from lst.ui.i18n import apply_config_language, tr

DEFAULT_PAIR: Final = (Lang("zh-Hans"), Lang("en"))
"""配置里没有互译对时使用的默认值，与 [bilingual] 节的默认值相同。"""


@dataclass(frozen=True, slots=True)
class ModelRow:
    """模型列表的一行。文字都已按当前界面语言填好。

    progress 是 0 到 100 的进度；为 None 且 show_progress 为真时，视图显示没有刻度的忙碌指示
    （总大小未知或正在校验）。失败时 status 只写原因和处理建议，技术细节只进日志（架构文档 10.3 节）。
    """

    model_id: str
    state: RowState
    title: str
    size: str
    status: str
    show_progress: bool
    progress: int | None
    can_cancel: bool
    can_retry: bool
    attribution: str = ""


@dataclass(frozen=True, slots=True)
class OnboardingScreen:
    """引导窗口的完整内容。notice 是模型列表下方的提示（获取中、获取失败、没有模型），没有时为空串。

    词典部分：show_dicts 为假时整个部分不显示；dict_rows 是互译对需要的词典包，attribution 是每个包的署名；
    include_dicts 是“同时下载词典”的勾选状态；dict_notice 是词典索引获取中或获取失败的提示。
    """

    window_title: str
    heading: str
    intro: str
    pair: str
    models_title: str
    rows: tuple[ModelRow, ...]
    notice: str
    notice_is_error: bool
    show_retry_index: bool
    retry_index_text: str
    download_text: str
    download_enabled: bool
    ocr_note: str
    finish_text: str
    finish_tip: str
    cancel_text: str
    retry_text: str
    show_dicts: bool = False
    dicts_title: str = ""
    dict_rows: tuple[ModelRow, ...] = ()
    dict_notice: str = ""
    include_dicts: bool = True
    include_dicts_text: str = ""
    dict_attribution: str = ""


class OnboardingIntents(Protocol):
    """视图把用户操作交给展示器的接口。"""

    def download(self) -> None:
        """点击“下载”：下载列表中全部尚未安装的模型。"""
        ...

    def cancel(self, model_id: str) -> None: ...

    def retry(self, model_id: str) -> None: ...

    def retry_index(self) -> None:
        """模型列表获取失败后点击“重试”。"""
        ...

    def set_include_dicts(self, include: bool) -> None:
        """勾选或取消“同时下载词典”。"""
        ...

    def finish(self) -> None:
        """点击“完成”或“跳过”。"""
        ...


class OnboardingView(Protocol):
    """展示器驱动视图的接口。"""

    def show_screen(self, screen: OnboardingScreen) -> None:
        """按画面模型更新全部控件。"""
        ...

    def present(self) -> None:
        """显示并激活窗口。"""
        ...

    def conceal(self) -> None: ...

    def is_shown(self) -> bool: ...


class CompletionStore(Protocol):
    """记录引导是否已经完成。"""

    def completed(self) -> bool: ...

    def mark_completed(self) -> None: ...


# ---------------------------------------------------------------- 纯函数


def read_pair(config: AppConfig | None) -> tuple[Lang, Lang]:
    """从 AppConfig 的 bilingual.pair 读互译对。缺失、不是两个合法且不同的语言码时用默认值。

    [bilingual] 节的模型属于竞速模块，界面不能依赖它，所以这里只按键读取。
    """
    section = config.section("bilingual") if config is not None else None
    raw = section.get("pair") if isinstance(section, dict) else None
    if not isinstance(raw, list) or len(raw) != 2:
        return DEFAULT_PAIR
    first, second = raw
    if not isinstance(first, str) or not isinstance(second, str):
        return DEFAULT_PAIR
    try:
        pair = (normalize_lang(first), normalize_lang(second))
    except ValueError:
        return DEFAULT_PAIR
    return pair if pair[0] != pair[1] else DEFAULT_PAIR


def _size_text(model: ModelInfo) -> str:
    """大小一栏：词典包在大小后面写上许可证。"""
    size = format_size(model.size_bytes)
    if model.kind is ModelKind.DICT and model.license:
        return tr(
            "onboarding.dicts.size", size=size, license=_LICENSE_NAMES.get(model.license, model.license)
        )
    return size


_LICENSE_NAMES: Final = {"CC-BY-SA-4.0": "CC BY-SA 4.0"}
"""许可证 SPDX 标识的显示名，表里没有的原样显示。"""


def build_row(model: ModelInfo, progress: DownloadProgress | None) -> ModelRow:
    """一个模型的显示内容。"""
    state = row_state(model, progress)
    is_dict = model.kind is ModelKind.DICT
    return ModelRow(
        model_id=model.id,
        state=state,
        title=model_title(model),
        size=_size_text(model),
        status=model_status_text(model, state, progress),
        show_progress=state in ACTIVE_STATES,
        progress=download_percent(state, progress),
        can_cancel=state in CANCELLABLE_STATES,
        can_retry=state in (RowState.FAILED, RowState.CANCELLED),
        attribution=tr("onboarding.dicts.attribution", attribution=model.attribution)
        if is_dict and model.attribution
        else "",
    )


def pair_models(catalog: ModelCatalog | None, pair: tuple[Lang, Lang]) -> list[ModelInfo]:
    """互译对需要的模型，按 default_model_ids 的规则与顺序。"""
    if catalog is None:
        return []
    models: list[ModelInfo] = []
    for model_id in default_model_ids(catalog, pair):
        model = catalog.model(model_id)
        if model is not None:
            models.append(model)
    return models


def pair_dicts(catalog: ModelCatalog | None, pair: tuple[Lang, Lang]) -> list[ModelInfo]:
    """互译对需要的词典包，按 default_dict_ids 的规则与顺序。"""
    if catalog is None or not catalog.dicts_enabled:
        return []
    found = (catalog.model(model_id) for model_id in default_dict_ids(catalog, pair))
    return [model for model in found if model is not None]


def _dict_notice(catalog: ModelCatalog | None) -> str:
    """词典索引获取中或获取失败的提示。"""
    if catalog is None or not catalog.dicts_enabled:
        return ""
    if catalog.dict_refreshing:
        return tr("onboarding.dicts.loading")
    if catalog.dict_index_error is not None or catalog.dict_index_error_code is not None:
        return tr("onboarding.dicts.failed", reason=model_error_reason(catalog.dict_index_error_code))
    return ""


def _notice(catalog: ModelCatalog | None, pair: tuple[Lang, Lang], has_rows: bool) -> tuple[str, bool]:
    """模型列表下方的提示与它是否是错误。"""
    if catalog is None:
        return "", False
    if catalog.refreshing:
        return tr("onboarding.index.loading"), False
    if catalog.index_error is not None or catalog.index_error_code is not None:
        reason = model_error_reason(catalog.index_error_code)
        return tr("onboarding.index.failed", reason=reason), True
    if catalog.index_loaded and not has_rows:
        first, second = (language_name(code) for code in pair)
        return tr("onboarding.index.empty", first=first, second=second), False
    return "", False


def build_screen(
    catalog: ModelCatalog | None, pair: tuple[Lang, Lang], *, include_dicts: bool = True
) -> OnboardingScreen:
    """按当前界面语言生成引导窗口的内容。include_dicts 是“同时下载词典”的勾选状态。"""
    models = pair_models(catalog, pair)
    downloads: Mapping[str, DownloadProgress] = catalog.downloads if catalog is not None else {}
    rows = tuple(build_row(model, downloads.get(model.id)) for model in models)
    dicts = pair_dicts(catalog, pair)
    dict_rows = tuple(build_row(model, downloads.get(model.id)) for model in dicts)
    dict_notice = _dict_notice(catalog)
    notice, notice_is_error = _notice(catalog, pair, bool(rows))
    pending = [model for model, row in zip(models, rows, strict=True) if row.state in DOWNLOADABLE_STATES]
    if include_dicts:
        pending += [
            model for model, row in zip(dicts, dict_rows, strict=True) if row.state in DOWNLOADABLE_STATES
        ]
    sizes = [model.size_bytes for model in pending]
    if pending and all(size is not None for size in sizes):
        download_text = tr("onboarding.download.total", size=format_size(sum(size or 0 for size in sizes)))
    else:
        download_text = tr("onboarding.download")
    all_installed = bool(rows) and all(row.state is RowState.INSTALLED for row in rows)
    first, second = (language_name(code) for code in pair)
    return OnboardingScreen(
        window_title=tr("onboarding.window.title"),
        heading=tr("onboarding.heading"),
        intro=tr("onboarding.intro"),
        pair=tr("onboarding.pair", first=first, second=second),
        models_title=tr("onboarding.models.title"),
        rows=rows,
        notice=notice,
        notice_is_error=notice_is_error,
        show_retry_index=notice_is_error,
        retry_index_text=tr("onboarding.index.retry"),
        download_text=download_text,
        download_enabled=bool(pending),
        ocr_note=tr("onboarding.ocr.note"),
        finish_text=tr("onboarding.finish") if all_installed else tr("onboarding.skip"),
        finish_tip="" if all_installed else tr("onboarding.skip.tip"),
        cancel_text=tr("onboarding.row.cancel"),
        retry_text=tr("onboarding.row.retry"),
        show_dicts=bool(dict_rows or dict_notice),
        dicts_title=tr("onboarding.dicts.title"),
        dict_rows=dict_rows,
        dict_notice=dict_notice,
        include_dicts=include_dicts,
        include_dicts_text=tr("onboarding.dicts.include"),
        dict_attribution=tr("onboarding.dicts.credit"),
    )


# ---------------------------------------------------------------- 展示器


class OnboardingPresenter:
    """引导窗口的展示器，实现 OnboardingIntents。处理函数都是普通函数，在主线程上由总线调用。"""

    def __init__(
        self, view: OnboardingView, *, bus: EventBus, store: CompletionStore, logger: logging.Logger
    ) -> None:
        self._view = view
        self._bus = bus
        self._store = store
        self._logger = logger
        self._catalog: ModelCatalog | None = None
        self._pair = DEFAULT_PAIR
        self._include_dicts = True
        self._screen: OnboardingScreen | None = None
        # 窗口显示时还没有收到 ModelCatalog：等收到第一份目录后再决定要不要获取索引。
        self._index_wanted = False
        self._closed = False

    @property
    def screen(self) -> OnboardingScreen | None:
        """最近一次交给视图的画面。"""
        return self._screen

    @property
    def completed(self) -> bool:
        return self._store.completed()

    def start(self) -> None:
        """用总线上已有的状态渲染一次。之后的变化由订阅的处理函数送来。"""
        self._catalog = self._bus.latest(ModelCatalog)
        self._pair = read_pair(self._bus.latest(AppConfig))
        self.refresh()

    def close(self) -> None:
        """之后不再响应任何消息和操作。重复调用没有效果。"""
        self._closed = True

    def refresh(self) -> None:
        """按当前状态和界面语言重新生成画面；与上次相同时不打扰视图。"""
        if self._closed:
            return
        screen = build_screen(self._catalog, self._pair, include_dicts=self._include_dicts)
        if screen != self._screen:
            self._screen = screen
            self._view.show_screen(screen)

    def show(self) -> None:
        """显示引导窗口。本次运行还没有获取过模型索引时，请模型管理模块获取一次。"""
        if self._closed:
            return
        self.refresh()
        self._view.present()
        if self._catalog is None:
            self._index_wanted = True
        else:
            self._request_index(self._catalog)

    def show_if_needed(self) -> bool:
        """引导没有完成过时显示窗口，返回是否显示了。"""
        if self._store.completed():
            return False
        self.show()
        return True

    def _request_index(self, catalog: ModelCatalog) -> None:
        self._index_wanted = False
        nmt_missing = not catalog.index_loaded and not catalog.refreshing
        dicts_missing = (
            catalog.dicts_enabled and not catalog.dict_index_loaded and not catalog.dict_refreshing
        )
        if nmt_missing or dicts_missing:
            self._bus.send(RefreshModelCatalog())

    # ------------------------------------------------------------ 消息处理

    def on_catalog(self, catalog: ModelCatalog) -> None:
        if self._closed:
            return
        self._catalog = catalog
        if self._index_wanted:
            self._request_index(catalog)
        self.refresh()

    def on_config(self, config: AppConfig) -> None:
        if self._closed:
            return
        # 界面语言也可能变了。组合根与各模块处理 AppConfig 的先后没有保证，所以这里自己设置界面语言，
        # 再重新生成画面。
        apply_config_language(config)
        self._pair = read_pair(config)
        self.refresh()

    # ------------------------------------------------------------ OnboardingIntents

    def download(self) -> None:
        if self._closed:
            return
        screen = self._screen
        rows = (*screen.rows, *(screen.dict_rows if self._include_dicts else ())) if screen else ()
        ids = tuple(row.model_id for row in rows if row.state in DOWNLOADABLE_STATES)
        if not ids:
            return
        self._logger.info("首次运行引导：下载 %d 个本地翻译模型或词典", len(ids))
        self._bus.send(DownloadModels(ids))

    def set_include_dicts(self, include: bool) -> None:
        if self._closed or include == self._include_dicts:
            return
        self._include_dicts = include
        self.refresh()

    def cancel(self, model_id: str) -> None:
        if not self._closed:
            self._bus.send(CancelModelDownload(model_id))

    def retry(self, model_id: str) -> None:
        if not self._closed:
            self._bus.send(DownloadModels((model_id,)))

    def retry_index(self) -> None:
        if not self._closed:
            self._bus.send(RefreshModelCatalog())

    def finish(self) -> None:
        if self._closed:
            return
        self._store.mark_completed()
        self._logger.info("首次运行引导已完成")
        self._view.conceal()
