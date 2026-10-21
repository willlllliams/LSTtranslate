"""竞速页的统计表、模型页、关于页的测试：显示的内容，以及按钮发出的命令。"""

from PySide6.QtCore import Qt

from lst.contracts.events import (
    CacheCleared,
    ClearCache,
    EngineStatsRow,
    ResetStats,
    StatsSnapshot,
    WorkerStatus,
    WorkerStatuses,
)
from lst.contracts.ids import EngineId, Lang
from lst.contracts.models import (
    CancelModelDownload,
    DeleteModel,
    DownloadModels,
    DownloadProgress,
    DownloadState,
    ModelCatalog,
    ModelErrorCode,
    ModelInfo,
    ModelKind,
    RefreshModelCatalog,
    nmt_model_id,
)
from lst.contracts.ports import WorkerState
from lst.ui.settings.page_about import AboutPage
from lst.ui.settings.page_models import ModelsPage
from lst.ui.settings.pages import RacePage
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import default_config

MB = 1024 * 1024


def cells(page: RacePage) -> list[list[str]]:
    table = page.stats.table
    return [
        [table.item(row, column).text() for column in range(table.columnCount())]  # type: ignore[union-attr]
        for row in range(table.rowCount())
    ]


# ---------------------------------------------------------------- 竞速统计


async def test_stats_table_lists_every_instance_even_without_data(harness: Harness) -> None:
    page = harness.window.page("race")
    assert isinstance(page, RacePage)
    # 还没有统计：配置里的 4 个实例各一行，数字为 0。
    assert cells(page) == [
        ["Argos", "0", "0", "—", "—", "—", "—"],
        ["DeepSeek", "0", "0", "—", "—", "—", "—"],
        ["DeepL", "0", "0", "—", "—", "—", "—"],
        ["Ollama", "0", "0", "—", "—", "—", "—"],
    ]
    assert not page.stats.empty_label.isHidden()


async def test_stats_table_shows_the_snapshot(harness: Harness) -> None:
    page = harness.window.page("race")
    assert isinstance(page, RacePage)
    await harness.publish(
        StatsSnapshot(
            (
                EngineStatsRow(EngineId("argos"), 120, 30, 180.0, 420.0, 0.0),
                EngineStatsRow(EngineId("deepseek"), 80, 50, 1500.0, 2600.0, 0.05),
                EngineStatsRow(EngineId("removed"), 4, 0, None, None, 1.0),
            )
        )
    )
    table = cells(page)
    assert [row[0] for row in table] == ["Argos", "DeepSeek", "DeepL", "Ollama", "removed"]
    # 列：实例、执行段数、胜出次数、胜出率（胜出次数 / 执行段数）、耗时中位数、耗时 P95、失败率。
    assert table[0][1:] == ["120", "30", "25%", "180 毫秒", "420 毫秒", "0%"]
    assert table[1][1:] == ["80", "50", "62.5%", "1.50 秒", "2.60 秒", "5%"]
    assert table[2][1:] == ["0", "0", "—", "—", "—", "—"]
    assert table[4][1:] == ["4", "0", "0%", "—", "—", "100%"]  # 已删除的实例显示实例号
    assert page.stats.empty_label.isHidden()
    header = page.stats.table.horizontalHeaderItem(3)
    assert header is not None and header.text() == "胜出率"


async def test_stats_names_follow_engine_statuses(harness: Harness) -> None:
    from lst.contracts.events import EngineHealth, EngineStatus, EngineStatuses

    page = harness.window.page("race")
    assert isinstance(page, RacePage)
    await harness.publish(
        EngineStatuses(
            {EngineId("argos"): EngineStatus(EngineHealth.READY)}, {EngineId("argos"): "本地 Argos"}, {}
        )
    )
    assert cells(page)[0][0] == "本地 Argos"


async def test_stats_table_is_keyboard_reachable(harness: Harness) -> None:
    page = harness.window.page("race")
    assert isinstance(page, RacePage)
    assert page.stats.table.focusPolicy() != Qt.FocusPolicy.NoFocus
    assert page.stats.table.accessibleName() == "各引擎的竞速统计"


async def test_reset_statistics_button_asks_then_sends_the_command(harness: Harness) -> None:
    page = harness.window.page("race")
    assert isinstance(page, RacePage)
    harness.dialogs.confirm_answer = False
    page.stats.reset_button.click()
    await harness.flush()
    assert harness.recorded.of(ResetStats) == []
    assert len(harness.dialogs.confirms) == 1

    harness.dialogs.confirm_answer = True
    page.stats.reset_button.click()
    await harness.flush()
    assert harness.recorded.of(ResetStats) == [ResetStats()]
    await harness.publish(StatsSnapshot(()))  # 统计模块处理后发布的空快照
    assert page.stats.empty_label.isHidden() is False


async def test_clear_cache_button_asks_then_sends_the_command_and_shows_the_result(harness: Harness) -> None:
    page = harness.window.page("race")
    assert isinstance(page, RacePage)
    harness.dialogs.confirm_answer = False
    page.stats.clear_cache_button.click()
    await harness.flush()
    assert harness.recorded.of(ClearCache) == []

    harness.dialogs.confirm_answer = True
    page.stats.clear_cache_button.click()
    await harness.flush()
    assert harness.recorded.of(ClearCache) == [ClearCache()]
    assert page.stats.cache_label.isHidden()
    await harness.publish(CacheCleared(1234))
    assert not page.stats.cache_label.isHidden()
    assert page.stats.cache_label.text() == "已清空译文缓存（1234 条）。"


async def test_stats_buttons_have_mnemonics(harness: Harness) -> None:
    page = harness.window.page("race")
    assert isinstance(page, RacePage)
    assert page.stats.reset_button.text() == "重置统计(&R)"
    assert page.stats.clear_cache_button.text() == "清空译文缓存(&C)"


# ---------------------------------------------------------------- 模型页


def info(
    source: str,
    target: str,
    *,
    installed: bool = False,
    version: str = "1.9",
    installed_version: str | None = None,
    size: int | None = 90 * MB,
) -> ModelInfo:
    return ModelInfo(
        id=nmt_model_id(source, target),
        kind=ModelKind.NMT,
        source=Lang(source),
        target=Lang(target),
        version=version,
        size_bytes=size,
        installed=installed,
        url="https://packages.invalid/m.argosmodel",
        installed_version=(installed_version or version) if installed else None,
    )


EN_ZH = nmt_model_id("en", "zh-Hans")
ZH_EN = nmt_model_id("zh-Hans", "en")
EN_DE = nmt_model_id("en", "de")
DE_EN = nmt_model_id("de", "en")


def catalog(
    *models: ModelInfo, downloads: dict[str, DownloadProgress] | None = None, **kwargs: object
) -> ModelCatalog:
    return ModelCatalog(models=models, downloads=downloads or {}, **kwargs)  # type: ignore[arg-type]


def sample_catalog() -> ModelCatalog:
    return catalog(
        info("en", "zh-Hans", installed=True),
        info("zh-Hans", "en", installed=True, version="1.9", installed_version="1.8"),
        info("en", "de"),
        info("de", "en"),
        index_loaded=True,
    )


async def open_models(harness: Harness) -> ModelsPage:
    window = await harness.open()
    window.show_page("models")
    page = window.page("models")
    assert isinstance(page, ModelsPage)
    return page


def tree_rows(page: ModelsPage) -> list[list[str]]:
    return [
        [page.tree.topLevelItem(row).text(col) for col in range(3)]  # type: ignore[union-attr]
        for row in range(page.tree.topLevelItemCount())
    ]


async def test_models_page_lists_installed_and_downloadable_models(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(sample_catalog())
    assert [row.model_id for row in page.rows] == [EN_ZH, ZH_EN, EN_DE, DE_EN]
    table = tree_rows(page)
    assert table[0] == ["英语 → 简体中文", "90.0 MB", "已安装（版本 1.9）"]
    assert table[1][2] == "已安装版本 1.8，可更新到 1.9"
    assert table[2] == ["英语 → 德语", "90.0 MB", "未安装"]


async def test_models_page_status_texts(harness: Harness) -> None:
    page = await open_models(harness)
    downloads = {
        EN_DE: DownloadProgress(DownloadState.DOWNLOADING, 30 * MB, 90 * MB),
        DE_EN: DownloadProgress(
            DownloadState.FAILED, 0, None, error="HTTP 503 secret-detail", error_code=ModelErrorCode.NETWORK
        ),
    }
    await harness.publish(catalog(*sample_catalog().models, downloads=downloads, index_loaded=True))
    table = {row.model_id: row.status for row in page.rows}
    assert table[EN_DE] == "正在下载 33%（30.0 MB / 90.0 MB）"
    assert table[DE_EN].startswith("下载失败：网络连接失败")
    assert "503" not in table[DE_EN] and "secret-detail" not in table[DE_EN]  # 不显示技术细节


async def test_progress_updates_do_not_rebuild_the_rows(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(sample_catalog())
    page.select(EN_DE)
    first_item = page.tree.topLevelItem(2)
    progress = {EN_DE: DownloadProgress(DownloadState.DOWNLOADING, 10 * MB, 90 * MB)}
    await harness.publish(catalog(*sample_catalog().models, downloads=progress, index_loaded=True))
    progress = {EN_DE: DownloadProgress(DownloadState.DOWNLOADING, 60 * MB, 90 * MB)}
    await harness.publish(catalog(*sample_catalog().models, downloads=progress, index_loaded=True))
    assert page.tree.topLevelItem(2) is first_item  # 同一个条目，选中不丢
    assert page.selected_ids() == [EN_DE]
    assert tree_rows(page)[2][2].startswith("正在下载 66%")


async def test_download_button_sends_only_downloadable_selected_models(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(sample_catalog())
    assert not page.download_button.isEnabled()  # 没有选中
    page.select(EN_ZH, EN_DE, DE_EN)  # 已安装且是最新的 EN_ZH 不会被下载
    assert page.download_button.isEnabled()
    page.download_button.click()
    await harness.flush()
    assert harness.recorded.of(DownloadModels) == [DownloadModels((EN_DE, DE_EN))]


async def test_update_available_model_can_be_downloaded_again(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(sample_catalog())
    page.select(ZH_EN)
    assert page.download_button.isEnabled() and page.delete_button.isEnabled()


async def test_cancel_button_cancels_active_downloads(harness: Harness) -> None:
    page = await open_models(harness)
    progress = {
        EN_DE: DownloadProgress(DownloadState.DOWNLOADING, 1, 10),
        DE_EN: DownloadProgress(DownloadState.QUEUED),
    }
    await harness.publish(catalog(*sample_catalog().models, downloads=progress, index_loaded=True))
    page.select(EN_DE, DE_EN, EN_ZH)
    assert page.cancel_button.isEnabled() and not page.download_button.isEnabled()
    page.cancel_button.click()
    await harness.flush()
    assert harness.recorded.of(CancelModelDownload) == [
        CancelModelDownload(EN_DE),
        CancelModelDownload(DE_EN),
    ]


async def test_delete_button_asks_for_confirmation(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(sample_catalog())
    page.select(EN_ZH)
    harness.dialogs.confirm_answer = False
    page.delete_button.click()
    await harness.flush()
    assert harness.recorded.of(DeleteModel) == []
    assert "英语 → 简体中文" in harness.dialogs.confirms[-1][1]

    harness.dialogs.confirm_answer = True
    page.delete_button.click()
    await harness.flush()
    assert harness.recorded.of(DeleteModel) == [DeleteModel(EN_ZH)]

    page.select(EN_ZH, ZH_EN)
    page.delete_button.click()
    assert "2 个模型" in harness.dialogs.confirms[-1][1]


async def test_delete_is_disabled_while_a_model_is_downloading_or_missing(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(sample_catalog())
    page.select(EN_DE)
    assert not page.delete_button.isEnabled()


async def test_refresh_button_and_first_open_fetch_the_index(harness: Harness) -> None:
    page = await open_models(harness)  # 第一次打开模型页：获取一次索引
    await harness.flush()
    assert harness.recorded.of(RefreshModelCatalog) == [RefreshModelCatalog()]
    harness.window.show_page("about")
    harness.window.show_page("models")
    await harness.flush()
    assert len(harness.recorded.of(RefreshModelCatalog)) == 1  # 再进入不重复获取
    page.refresh_button.click()
    await harness.flush()
    assert len(harness.recorded.of(RefreshModelCatalog)) == 2


async def test_notice_for_loading_failure_and_unloaded_index(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(catalog(info("en", "zh-Hans", installed=True), index_loaded=False))
    assert "只列出了已安装的模型" in page.notice_label.text()
    await harness.publish(catalog(info("en", "zh-Hans", installed=True), index_loaded=False, refreshing=True))
    assert page.notice_label.text() == "正在获取可下载的模型列表…"
    await harness.publish(
        catalog(
            info("en", "zh-Hans", installed=True),
            index_loaded=False,
            index_error="HTTP 500 raw detail",
            index_error_code=ModelErrorCode.TIMEOUT,
        )
    )
    assert "连接超时" in page.notice_label.text() and "raw detail" not in page.notice_label.text()
    await harness.publish(sample_catalog())
    assert page.notice_label.isHidden()


async def test_filter_and_installed_only(harness: Harness) -> None:
    page = await open_models(harness)
    await harness.publish(sample_catalog())
    page.filter_edit.setText("德")
    assert [row.model_id for row in page.rows] == [EN_DE, DE_EN]
    page.filter_edit.setText("german")  # 英文名也能筛选
    assert [row.model_id for row in page.rows] == [EN_DE, DE_EN]
    page.filter_edit.setText("zh-hans")  # 语言码
    assert [row.model_id for row in page.rows] == [EN_ZH, ZH_EN]
    page.filter_edit.setText("")
    page.installed_only.setChecked(True)
    assert [row.model_id for row in page.rows] == [EN_ZH, ZH_EN]
    page.filter_edit.setText("没有这种语言")
    assert page.rows == [] and not page.empty_label.isHidden()


async def test_model_directory_card(start_settings: StartSettings) -> None:
    harness = await start_settings(config=default_config({"nmt": {"model_dir": "D:/models"}}))
    page = await open_models(harness)
    directory = page.bindings[0].editor
    assert directory.json_value() == "D:/models"
    assert page.bindings[0].location == ("nmt", "model_dir")
    harness.dialogs.directory = "E:/other"
    directory.browse_button.click()  # type: ignore[attr-defined]
    await harness.flush()
    assert [c.patch for c in harness.recorded.updates] == [{"nmt": {"model_dir": "E:/other"}}]
    assert harness.dialogs.directories[-1][1] == "D:/models"


# ---------------------------------------------------------------- 关于页


async def test_about_page_shows_version_and_worker_states(start_settings: StartSettings) -> None:
    harness = await start_settings(app_version="1.2.3")
    window = await harness.open()
    window.show_page("about")
    page = window.page("about")
    assert page.version_label.text() == "版本 1.2.3"  # type: ignore[attr-defined]
    assert page.workers_empty.isHidden() is False  # type: ignore[attr-defined]

    await harness.publish(
        WorkerStatuses(
            {
                "nmt": WorkerStatus(WorkerState.CRASHED, "ExitCode 3221225477 traceback"),
                "ocr": WorkerStatus(WorkerState.READY),
            }
        )
    )
    assert isinstance(page, AboutPage)
    labels = page.worker_labels
    assert list(labels) == ["ocr", "nmt"]
    assert labels["ocr"].text() == "OCR：就绪"
    assert labels["nmt"].text().startswith("本地翻译模型：出错：")
    assert "3221225477" not in labels["nmt"].text()  # 不显示技术细节
    await harness.publish(WorkerStatuses({"nmt": WorkerStatus(WorkerState.LOADING)}))
    assert list(page.worker_labels) == ["nmt"]  # type: ignore[attr-defined]
    assert page.worker_labels["nmt"].text() == "本地翻译模型：加载中"  # type: ignore[attr-defined]


async def test_export_diagnostics_button_is_disabled_with_a_reason(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("about")
    page = window.page("about")
    assert not page.export_button.isEnabled()  # type: ignore[attr-defined]
    assert "尚未提供" in page.export_button.toolTip()  # type: ignore[attr-defined]
    assert "尚未提供" in page.export_note.text()  # type: ignore[attr-defined]
