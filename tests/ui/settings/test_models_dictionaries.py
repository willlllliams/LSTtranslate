"""模型页的词典组：按种类分组显示、每行的大小、许可证与署名、按钮发出的命令、字典引擎的设置卡片文案，
以及词典文案表和第三方声明文件。"""

import re
from pathlib import Path

from PySide6.QtWidgets import QApplication

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.models import (
    DeleteModel,
    DownloadModels,
    DownloadProgress,
    DownloadState,
    ModelCatalog,
    ModelErrorCode,
    ModelInfo,
    ModelKind,
    RefreshModelCatalog,
    dict_model_id,
    nmt_model_id,
)
from lst.ui import i18n
from lst.ui.settings import strings_dict
from lst.ui.settings.model_rows import build_rows, dict_notice, license_name
from lst.ui.settings.page_engines import AddEngineDialog, EnginesPage
from lst.ui.settings.page_models import DictsPage, NmtModelsPage
from tests.ui.settings.conftest import Harness

MB = 1024 * 1024
WIKTIONARY = (
    "Adapted (extracted, filtered and trimmed) from Wiktionary by Wiktionary contributors "
    "(https://www.wiktionary.org/), extracted by wiktextract and published by kaikki.org "
    "(https://kaikki.org/); CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/)"
)
ECDICT = (
    "Adapted (extracted, filtered and trimmed) from ECDICT (https://github.com/skywind3000/ECDICT), "
    "Copyright (c) 2025 Linwei; MIT License"
)
EN_ZH = dict_model_id("en", "zh-Hans")
DE_EN = dict_model_id("de", "en")
EN_DE = dict_model_id("en", "de")
NMT_EN_ZH = nmt_model_id("en", "zh-Hans")


def dict_info(source: str, target: str, *, installed: bool = False, stale: bool = False) -> ModelInfo:
    is_ecdict = (source, target) == ("en", "zh-Hans")
    return ModelInfo(
        id=dict_model_id(source, target),
        kind=ModelKind.DICT,
        source=Lang(source),
        target=Lang(target),
        version="20261003" if installed else "",
        size_bytes=int(0.7 * MB),
        installed=installed,
        url=f"https://dicts.invalid/dict-{source}-{target}.tsv.gz",
        installed_version="20261003" if installed else None,
        license="MIT" if is_ecdict else "CC-BY-SA-4.0",
        attribution=ECDICT if is_ecdict else WIKTIONARY,
        differs_from_index=stale,
    )


def nmt_info() -> ModelInfo:
    return ModelInfo(
        id=NMT_EN_ZH,
        kind=ModelKind.NMT,
        source=Lang("en"),
        target=Lang("zh-Hans"),
        version="1.9",
        size_bytes=90 * MB,
        installed=True,
        url="https://packages.invalid/m.argosmodel",
        installed_version="1.9",
    )


def sample(**kwargs: object) -> ModelCatalog:
    fields: dict[str, object] = {
        "models": (
            nmt_info(),
            dict_info("de", "en"),
            dict_info("en", "de", installed=True, stale=True),
            dict_info("en", "zh-Hans", installed=True),
        ),
        "index_loaded": True,
        "dicts_enabled": True,
        "dict_index_loaded": True,
    }
    fields.update(kwargs)
    return ModelCatalog(**fields)  # type: ignore[arg-type]


async def open_models(harness: Harness) -> DictsPage:
    """词典页。"""
    window = await harness.open()
    window.show_page("models.dicts")
    page = window.page("models.dicts")
    assert isinstance(page, DictsPage)
    return page


def nmt_page(harness: Harness) -> NmtModelsPage:
    page = harness.window.page("models.nmt")
    assert isinstance(page, NmtModelsPage)
    return page


def dict_cells(page: DictsPage) -> list[list[str]]:
    return [
        [page.dict_tree.topLevelItem(row).text(col) for col in range(4)]  # type: ignore[union-attr]
        for row in range(page.dict_tree.topLevelItemCount())
    ]


# ---------------------------------------------------------------- 行内容（不需要 Qt）


def test_rows_are_split_by_kind() -> None:
    catalog = sample()
    assert [row.model_id for row in build_rows(catalog)] == [NMT_EN_ZH]
    assert [row.model_id for row in build_rows(catalog, kind=ModelKind.DICT)] == [DE_EN, EN_DE, EN_ZH]


def test_dictionary_rows_carry_license_and_attribution() -> None:
    rows = {row.model_id: row for row in build_rows(sample(), kind=ModelKind.DICT)}
    assert (rows[EN_ZH].license, rows[DE_EN].license) == ("MIT", "CC BY-SA 4.0")
    assert rows[DE_EN].attribution == f"署名：{WIKTIONARY}"
    assert rows[EN_ZH].attribution == f"署名：{ECDICT}"
    # NMT 模型没有许可证与署名两项。
    (nmt,) = build_rows(sample())
    assert (nmt.license, nmt.attribution) == ("", "")
    assert license_name("Apache-2.0") == "Apache-2.0"


def test_stale_dictionary_can_be_downloaded_again_and_says_so() -> None:
    rows = {row.model_id: row for row in build_rows(sample(), kind=ModelKind.DICT)}
    assert rows[EN_DE].status == "已安装（20261003），发布页上有更新的版本"
    assert rows[EN_DE].can_download and rows[EN_DE].can_delete
    assert rows[EN_ZH].status == "已安装（版本 20261003）" and not rows[EN_ZH].can_download
    assert rows[DE_EN].status == "未安装" and rows[DE_EN].can_download and not rows[DE_EN].can_delete


def test_dictionary_notice_follows_the_dictionary_index_only() -> None:
    # Argos 索引失败不影响词典的提示，反之亦然。
    failed_nmt = sample(index_loaded=False, index_error="x", index_error_code=ModelErrorCode.NETWORK)
    assert dict_notice(failed_nmt) == ("", False)
    text, is_error = dict_notice(sample(dict_index_loaded=False, dict_index_error_code=ModelErrorCode.HTTP))
    assert is_error and text.startswith("无法获取词典列表：")
    assert dict_notice(sample(dict_index_loaded=False)) == (
        "目前只列出了已安装的词典。点击“刷新列表”获取可下载的词典（需要联网）。",
        False,
    )
    assert dict_notice(sample(dict_refreshing=True))[0] == "正在获取可下载的词典列表…"


# ---------------------------------------------------------------- 模型页


async def test_models_and_dictionaries_are_listed_on_their_own_pages(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(sample())
    assert [row.model_id for row in nmt_page(harness).rows] == [NMT_EN_ZH]
    assert [row.model_id for row in page.dict_rows] == [DE_EN, EN_DE, EN_ZH]
    assert not page.dict_card.isHidden()
    assert page.heading.text() == "模型 › 词典"
    assert dict_cells(page)[0] == ["德语 → 英语", "716.8 KB", "CC BY-SA 4.0", "未安装"]
    assert dict_cells(page)[2][2] == "MIT"


async def test_every_dictionary_shows_its_attribution_on_its_own_line(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(sample())
    window = page.window()
    window.resize(980, 760)
    QApplication.processEvents()
    for model_id, attribution in ((DE_EN, WIKTIONARY), (EN_DE, WIKTIONARY), (EN_ZH, ECDICT)):
        assert page.dict_attribution_text(model_id) == f"署名：{attribution}"
    item = page.dict_tree.topLevelItem(0)
    assert item is not None and item.isExpanded()
    credit = item.child(0)
    assert credit is not None and credit.isFirstColumnSpanned()
    # 署名行不可选中：选择只针对词典本身。
    credit.setSelected(True)
    assert page.selected_dict_ids() == []
    # 总署名里的许可证名是链接。
    text = page.dict_attribution.text()
    assert "https://creativecommons.org/licenses/by-sa/4.0/" in text and "ECDICT" in text
    assert page.dict_attribution.openExternalLinks()


async def test_dictionary_group_is_hidden_when_the_models_module_manages_no_dictionaries(
    harness: Harness,
) -> None:
    page = await open_models(harness)
    await harness.publish(ModelCatalog(models=(nmt_info(),), index_loaded=True))
    assert page.dict_card.isHidden()
    assert page.dict_rows == []


async def test_dictionary_buttons_send_commands_for_the_selected_packs(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(sample())
    assert not page.dict_download_button.isEnabled()
    page.select_dicts(DE_EN, EN_DE, EN_ZH)
    page.dict_download_button.click()
    await harness.flush()
    # 已安装且与索引相同的 en→zh-Hans 不下载；过期的 en→de 重新下载。
    assert harness.recorded.of(DownloadModels) == [DownloadModels((DE_EN, EN_DE))]

    page.select_dicts(EN_ZH)
    page.dict_delete_button.click()
    await harness.flush()
    assert harness.recorded.of(DeleteModel) == [DeleteModel(EN_ZH)]
    assert "删除词典“英语 → 简体中文”" in harness.dialogs.confirms[-1][1]


async def test_dictionary_progress_updates_keep_the_rows(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(sample())
    page.select_dicts(DE_EN)
    first = page.dict_tree.topLevelItem(0)
    downloads = {DE_EN: DownloadProgress(DownloadState.DOWNLOADING, 350_000, 700_000)}
    await harness.publish(sample(downloads=downloads))
    assert page.dict_tree.topLevelItem(0) is first
    assert page.selected_dict_ids() == [DE_EN]
    assert dict_cells(page)[0][3].startswith("正在下载 50%")
    assert page.dict_cancel_button.isEnabled()


async def test_filter_applies_to_dictionaries_too(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(sample())
    page.filter_edit.setText("德语")
    assert [row.model_id for row in page.dict_rows] == [DE_EN, EN_DE]
    page.filter_edit.setText("")
    page.installed_only.setChecked(True)
    assert [row.model_id for row in page.dict_rows] == [EN_DE, EN_ZH]


async def test_opening_the_page_fetches_the_dictionary_index_when_only_it_is_missing(
    harness: Harness,
) -> None:
    await harness.publish(sample(dict_index_loaded=False))
    page = await open_models(harness)
    page.on_shown()
    await harness.flush()
    assert harness.recorded.of(RefreshModelCatalog) == [RefreshModelCatalog()]


async def test_dictionary_buttons_have_their_own_mnemonics(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(sample())
    texts = [page.dict_download_button.text(), page.dict_cancel_button.text(), page.dict_delete_button.text()]
    assert texts == ["下载词典(&L)", "取消词典下载(&N)", "删除词典(&T)"]


# ---------------------------------------------------------------- 字典引擎的设置卡片


async def test_add_dialog_offers_the_dictionary_without_an_api_key(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("engines")
    page = window.page("engines")
    assert isinstance(page, EnginesPage)
    page.open_add_dialog()
    dialog = page.add_dialog
    assert isinstance(dialog, AddEngineDialog)
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("dictionary"))
    assert dialog.kind_combo.currentText() == "词典（逐词粗译）"
    assert dialog.name_edit.text() == "词典"
    assert not dialog.key_edit.isVisibleTo(dialog)
    assert dialog.errors() == {}
    dialog.reject()


# ---------------------------------------------------------------- 词典组的文案与第三方声明

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
ROOT = Path(__file__).resolve().parents[3]


def test_dictionary_texts_have_both_languages_with_the_same_placeholders() -> None:
    for key, texts in strings_dict.CATALOG.items():
        assert set(texts) == set(i18n.SUPPORTED), key
        zh, en = texts["zh-Hans"], texts["en"]
        assert zh and en, key
        assert set(_PLACEHOLDER.findall(zh)) == set(_PLACEHOLDER.findall(en)), key


def test_no_match_error_has_a_reason_and_a_suggestion() -> None:
    for suffix in ("reason", "suggestion"):
        key = f"error.{ErrorCode.NO_MATCH.value}.{suffix}"
        assert i18n.is_registered(key)
        assert i18n.tr(key) != key


def test_license_names() -> None:
    assert license_name("CC-BY-SA-4.0") == "CC BY-SA 4.0"
    assert license_name("MIT") == "MIT"
    assert license_name("GPL-3.0-only") == "GPL-3.0-only"
    assert license_name("") == ""


def test_credit_points_to_the_third_party_notices() -> None:
    assert "THIRD-PARTY-NOTICES" in strings_dict.dict_credit()


def test_third_party_notices_cover_both_data_sources() -> None:
    notices = (ROOT / "THIRD-PARTY-NOTICES").read_text(encoding="utf-8")
    for needle in (
        "ECDICT",
        "Copyright (c) 2025 Linwei",
        "licenses/ECDICT.LICENSE.txt",
        "Wiktionary",
        "kaikki.org",
        "CC BY-SA 4.0",
        "https://creativecommons.org/licenses/by-sa/4.0/",
    ):
        assert needle in notices, needle
    # 署名全文在声明里折行排版：合并空白后与发布页的署名一致。
    flat = " ".join(notices.split())
    assert WIKTIONARY in flat and ECDICT in flat
    license_text = (ROOT / "licenses" / "ECDICT.LICENSE.txt").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License\n\nCopyright (c) 2025 Linwei\n")
    # 声明里收录的许可证全文与 licenses/ECDICT.LICENSE.txt 逐行一致（只多了缩进）。
    for line in license_text.splitlines():
        assert line.strip() in notices, line
