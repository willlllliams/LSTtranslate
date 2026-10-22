"""首次运行引导的词典部分：按互译对列出词典包、许可证与署名、“同时下载词典”的勾选，以及窗口的显示。"""

import logging
from itertools import pairwise

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from lst.contracts.ids import Lang
from lst.contracts.models import (
    DownloadModels,
    DownloadState,
    ModelCatalog,
    ModelErrorCode,
    ModelInfo,
    ModelKind,
    RefreshModelCatalog,
    dict_model_id,
)
from lst.kernel import EventBus
from lst.ui.onboarding import OnboardingPresenter, OnboardingScreen, RowState, build_screen
from tests.ui.onboarding.conftest import EN_ZH, MB, ZH_EN, Commands, info, progress
from tests.ui.onboarding.test_module import StartModule, publish, window_of
from tests.ui.onboarding.test_module import start_module as start_module  # 引导窗口的夹具
from tests.ui.onboarding.test_presenter import FakeStore, FakeView

PAIR = (Lang("zh-Hans"), Lang("en"))
DICT_EN_ZH = dict_model_id("en", "zh-Hans")
DICT_ZH_EN = dict_model_id("zh-Hans", "en")
ECDICT = "Adapted (extracted, filtered and trimmed) from ECDICT, Copyright (c) 2025 Linwei; MIT License"
WIKTIONARY = (
    "Adapted (extracted, filtered and trimmed) from Wiktionary by Wiktionary contributors; CC BY-SA 4.0"
)


def dict_info(source: str, target: str, *, installed: bool = False) -> ModelInfo:
    ecdict = (source, target) == ("en", "zh-Hans")
    return ModelInfo(
        id=dict_model_id(source, target),
        kind=ModelKind.DICT,
        source=Lang(source),
        target=Lang(target),
        version="20261003" if installed else "",
        size_bytes=MB if ecdict else MB // 2,
        installed=installed,
        url=f"https://dicts.invalid/dict-{source}-{target}.tsv.gz",
        installed_version="20261003" if installed else None,
        license="MIT" if ecdict else "CC-BY-SA-4.0",
        attribution=ECDICT if ecdict else WIKTIONARY,
    )


def with_dicts(**kwargs: object) -> ModelCatalog:
    fields: dict[str, object] = {
        "models": (
            info("en", "zh-Hans", size=100 * MB),
            info("zh-Hans", "en", size=50 * MB),
            dict_info("en", "zh-Hans"),
            dict_info("zh-Hans", "en"),
            dict_info("en", "de"),
        ),
        "index_loaded": True,
        "dicts_enabled": True,
        "dict_index_loaded": True,
    }
    fields.update(kwargs)
    return ModelCatalog(**fields)  # type: ignore[arg-type]


def make(bus: EventBus) -> tuple[OnboardingPresenter, FakeView]:
    view = FakeView()
    presenter = OnboardingPresenter(
        view, bus=bus, store=FakeStore(), logger=logging.getLogger("tests.onboarding.dicts")
    )
    return presenter, view


# ---------------------------------------------------------------- 画面模型


def test_dictionaries_for_both_directions_are_listed_with_license_and_attribution() -> None:
    screen = build_screen(with_dicts(), PAIR)
    assert screen.show_dicts and screen.dicts_title == "词典（可选）"
    assert [row.model_id for row in screen.dict_rows] == [DICT_ZH_EN, DICT_EN_ZH]
    by_id = {row.model_id: row for row in screen.dict_rows}
    assert by_id[DICT_EN_ZH].size == "1.0 MB · MIT"
    assert by_id[DICT_ZH_EN].size == "512.0 KB · CC BY-SA 4.0"
    assert by_id[DICT_ZH_EN].attribution == f"署名：{WIKTIONARY}"
    assert by_id[DICT_EN_ZH].attribution == f"署名：{ECDICT}"
    # 本地翻译模型的行不带署名，词典不混进模型列表。
    assert [row.model_id for row in screen.rows] == [ZH_EN, EN_ZH]
    assert all(row.attribution == "" for row in screen.rows)


def test_download_total_includes_dictionaries_only_when_ticked() -> None:
    ticked = build_screen(with_dicts(), PAIR)
    assert ticked.include_dicts and ticked.download_text == "下载(&D)（共 151.5 MB）"
    unticked = build_screen(with_dicts(), PAIR, include_dicts=False)
    assert unticked.download_text == "下载(&D)（共 150.0 MB）"


def test_dictionaries_do_not_decide_the_finish_button() -> None:
    models = (info("en", "zh-Hans", installed=True), info("zh-Hans", "en", installed=True))
    screen = build_screen(with_dicts(models=(*models, dict_info("en", "zh-Hans"))), PAIR)
    assert screen.finish_text == "完成(&F)"
    assert screen.dict_rows[0].state is RowState.MISSING


def test_no_dictionary_section_without_dictionary_management() -> None:
    screen = build_screen(with_dicts(dicts_enabled=False), PAIR)
    assert not screen.show_dicts and screen.dict_rows == ()


def test_dictionary_index_failure_is_shown_in_the_dictionary_section() -> None:
    catalog = with_dicts(
        models=(info("en", "zh-Hans"),),
        dict_index_loaded=False,
        dict_index_error="HTTP 404",
        dict_index_error_code=ModelErrorCode.HTTP,
    )
    screen = build_screen(catalog, PAIR)
    assert screen.show_dicts and screen.dict_rows == ()
    assert screen.dict_notice.startswith("无法获取词典列表：")
    assert "404" not in screen.dict_notice
    assert screen.notice == ""  # 模型索引本身没有问题


# ---------------------------------------------------------------- 展示器


async def test_download_sends_models_and_ticked_dictionaries(bus: EventBus, commands: Commands) -> None:
    presenter, _view = make(bus)
    bus.publish(with_dicts())
    await bus.drain()
    presenter.on_catalog(with_dicts())
    presenter.download()
    await bus.drain()
    assert commands.download == [DownloadModels((ZH_EN, EN_ZH, DICT_ZH_EN, DICT_EN_ZH))]

    presenter.set_include_dicts(False)
    presenter.download()
    await bus.drain()
    assert commands.download[-1] == DownloadModels((ZH_EN, EN_ZH))


async def test_show_fetches_the_dictionary_index_when_it_is_missing(
    bus: EventBus, commands: Commands
) -> None:
    presenter, _view = make(bus)
    presenter.on_catalog(with_dicts(dict_index_loaded=False))
    presenter.show()
    await bus.drain()
    assert commands.refresh == [RefreshModelCatalog()]


async def test_progress_of_a_dictionary_download(bus: EventBus, commands: Commands) -> None:
    presenter, view = make(bus)
    downloads = {DICT_EN_ZH: progress(DownloadState.DOWNLOADING, MB // 2, MB)}
    presenter.on_catalog(with_dicts(downloads=downloads))
    screen: OnboardingScreen = view.screen
    row = next(row for row in screen.dict_rows if row.model_id == DICT_EN_ZH)
    assert row.show_progress and row.progress == 50 and row.can_cancel


# ---------------------------------------------------------------- 窗口


async def test_window_shows_dictionary_rows_with_attribution(
    bus: EventBus, start_module: StartModule
) -> None:
    module = await start_module()
    window = window_of(module)
    await publish(bus, with_dicts())
    assert not window.dicts_title.isHidden() and not window.include_dicts_box.isHidden()
    assert window.include_dicts_box.isChecked()
    rows = window.dict_rows()
    assert [row.model_id for row in rows] == [DICT_ZH_EN, DICT_EN_ZH]
    assert rows[1].attribution_label.text() == f"署名：{ECDICT}" and not rows[1].attribution_label.isHidden()
    assert rows[1].size_label.text() == "1.0 MB · MIT"
    # 模型行没有署名一行。
    assert all(row.attribution_label.isHidden() for row in window.rows())


async def test_unticking_the_box_leaves_dictionaries_out(
    bus: EventBus, start_module: StartModule, commands: Commands
) -> None:
    module = await start_module()
    window = window_of(module)
    await publish(bus, with_dicts())
    window.include_dicts_box.setChecked(False)
    await bus.drain()
    assert window.download_button.text() == "下载(&D)（共 150.0 MB）"
    window.download_button.click()
    await bus.drain()
    assert commands.download[-1] == DownloadModels((ZH_EN, EN_ZH))


async def test_window_hides_the_dictionary_section_without_dictionaries(
    bus: EventBus, start_module: StartModule
) -> None:
    module = await start_module()
    window = window_of(module)
    await publish(bus, with_dicts(dicts_enabled=False))
    assert window.dicts_title.isHidden() and window.include_dicts_box.isHidden()
    assert window.dict_rows() == []


async def test_tab_order_runs_through_the_dictionary_section(
    bus: EventBus, start_module: StartModule
) -> None:
    module = await start_module()
    window = window_of(module)
    await publish(bus, with_dicts())
    rows, dicts = window.rows(), window.dict_rows()
    chain: list[QWidget] = [
        rows[1].retry_button,
        window.retry_index_button,
        window.include_dicts_box,
        dicts[0].cancel_button,
        dicts[0].retry_button,
        dicts[1].cancel_button,
        dicts[1].retry_button,
        window.download_button,
        window.finish_button,
    ]
    for first, second in pairwise(chain):
        current = first.nextInFocusChain()
        while current is not None and not current.focusPolicy() & Qt.FocusPolicy.TabFocus:
            current = current.nextInFocusChain()
        assert current is second, (first, second)
