"""首次运行引导展示器的测试：画面模型的内容与展示器的命令。视图与完成记录用假对象，不创建任何 Qt 控件。"""

import logging

from lst.contracts.config import AppConfig
from lst.contracts.ids import Lang
from lst.contracts.models import (
    DownloadModels,
    DownloadState,
    ModelCatalog,
    ModelErrorCode,
    RefreshModelCatalog,
)
from lst.kernel import EventBus
from lst.ui import i18n
from lst.ui.onboarding import (
    DEFAULT_PAIR,
    OnboardingPresenter,
    OnboardingScreen,
    RowState,
    build_screen,
    read_pair,
)
from tests.ui.onboarding.conftest import (
    EN_ZH,
    MB,
    ZH_EN,
    Commands,
    app_config,
    catalog,
    info,
    progress,
    zh_en_catalog,
)

D = DownloadState
PAIR = (Lang("zh-Hans"), Lang("en"))


class FakeView:
    def __init__(self) -> None:
        self.screens: list[OnboardingScreen] = []
        self.shown = False
        self.presents = 0

    @property
    def screen(self) -> OnboardingScreen:
        return self.screens[-1]

    def show_screen(self, screen: OnboardingScreen) -> None:
        self.screens.append(screen)

    def present(self) -> None:
        self.shown = True
        self.presents += 1

    def conceal(self) -> None:
        self.shown = False

    def is_shown(self) -> bool:
        return self.shown


class FakeStore:
    def __init__(self, completed: bool = False) -> None:
        self.done = completed

    def completed(self) -> bool:
        return self.done

    def mark_completed(self) -> None:
        self.done = True


def make(bus: EventBus, *, completed: bool = False) -> tuple[OnboardingPresenter, FakeView, FakeStore]:
    view, store = FakeView(), FakeStore(completed)
    presenter = OnboardingPresenter(view, bus=bus, store=store, logger=logging.getLogger("tests.onboarding"))
    return presenter, view, store


# ---------------------------------------------------------------- 画面模型


def test_rows_list_both_directions_of_the_pair() -> None:
    screen = build_screen(zh_en_catalog(), PAIR)
    assert [(row.model_id, row.state) for row in screen.rows] == [
        (ZH_EN, RowState.MISSING),
        (EN_ZH, RowState.MISSING),
    ]
    assert [row.title for row in screen.rows] == ["简体中文 → 英语", "英语 → 简体中文"]
    assert [row.size for row in screen.rows] == ["50.0 MB", "100.0 MB"]
    assert [row.status for row in screen.rows] == ["未安装", "未安装"]
    assert screen.pair == "互译语言：简体中文 ↔ 英语"
    assert screen.download_enabled
    assert screen.download_text == "下载(&D)（共 150.0 MB）"
    assert screen.finish_text == "跳过(&S)"
    assert screen.notice == "" and not screen.show_retry_index
    assert "OCR" in screen.ocr_note


def test_installed_rows_and_finish_button() -> None:
    cat = catalog(info("en", "zh-Hans", installed=True), info("zh-Hans", "en", installed=True, size=None))
    screen = build_screen(cat, PAIR)
    assert [row.status for row in screen.rows] == ["已安装（版本 1.9）", "已安装（版本 1.9）"]
    assert screen.rows[0].size == "大小未知"
    assert not screen.download_enabled
    assert screen.download_text == "下载(&D)"
    assert screen.finish_text == "完成(&F)" and screen.finish_tip == ""


def test_update_available_is_shown_but_not_downloaded() -> None:
    cat = catalog(
        info("en", "zh-Hans", installed=True, version="2.0", installed_version="1.9"),
        info("zh-Hans", "en", installed=True),
    )
    screen = build_screen(cat, PAIR)
    assert screen.rows[1].status == "已安装版本 1.9，可更新到 2.0"
    assert not screen.download_enabled


def test_download_progress_rows() -> None:
    cat = zh_en_catalog(
        downloads={
            ZH_EN: progress(D.DOWNLOADING, 25 * MB, 50 * MB),
            EN_ZH: progress(D.QUEUED, 0, 100 * MB),
        }
    )
    zh_en, en_zh = build_screen(cat, PAIR).rows
    assert zh_en.state is RowState.DOWNLOADING
    assert zh_en.status == "正在下载 50%（25.0 MB / 50.0 MB）"
    assert (zh_en.show_progress, zh_en.progress, zh_en.can_cancel, zh_en.can_retry) == (True, 50, True, False)
    assert en_zh.status == "等待下载"
    assert (en_zh.show_progress, en_zh.progress, en_zh.can_cancel) == (True, 0, True)


def test_unknown_total_and_verifying_use_busy_indicator() -> None:
    cat = zh_en_catalog(
        downloads={ZH_EN: progress(D.DOWNLOADING, 512 * 1024, None), EN_ZH: progress(D.VERIFYING, MB, MB)}
    )
    zh_en, en_zh = build_screen(cat, PAIR).rows
    assert zh_en.status == "正在下载（已下载 512.0 KB）"
    assert (zh_en.show_progress, zh_en.progress) == (True, None)
    assert en_zh.status == "正在校验"
    assert (en_zh.show_progress, en_zh.progress, en_zh.can_cancel) == (True, None, False)


def test_failed_row_shows_reason_without_technical_detail() -> None:
    cat = zh_en_catalog(
        downloads={
            ZH_EN: progress(D.FAILED, code=ModelErrorCode.NETWORK, error="ConnectError: connection reset"),
            EN_ZH: progress(D.CANCELLED),
        }
    )
    screen = build_screen(cat, PAIR)
    failed, cancelled = screen.rows
    assert failed.status == "下载失败：网络连接失败，请检查网络或代理设置后重试"
    assert "ConnectError" not in repr(failed)  # 技术细节只进日志（架构文档 10.3 节）
    assert (failed.can_retry, failed.can_cancel, failed.show_progress) == (True, False, False)
    assert cancelled.status == "已取消" and cancelled.can_retry
    assert screen.download_enabled  # 失败和取消的模型也算“还没有安装”


def test_index_notices() -> None:
    loading = build_screen(catalog(index_loaded=False, refreshing=True), PAIR)
    assert loading.notice == "正在获取可下载的模型列表…" and not loading.show_retry_index

    failed = build_screen(catalog(index_loaded=False, error=(ModelErrorCode.TIMEOUT, "ReadTimeout")), PAIR)
    assert failed.notice_is_error and failed.show_retry_index
    assert failed.notice == "无法获取模型列表：连接超时，请稍后重试"
    assert failed.retry_index_text == "重试(&R)"

    empty = build_screen(catalog(info("en", "de")), PAIR)
    assert empty.rows == ()
    assert empty.notice.startswith("没有找到 简体中文 ↔ 英语 可用的本地翻译模型")
    assert not empty.download_enabled and empty.finish_text == "跳过(&S)"

    nothing_yet = build_screen(None, PAIR)
    assert nothing_yet.rows == () and nothing_yet.notice == ""


def test_english_texts() -> None:
    i18n.set_language("en")
    cat = zh_en_catalog(downloads={ZH_EN: progress(D.FAILED, code=ModelErrorCode.HTTP, error="HTTP 503")})
    screen = build_screen(cat, PAIR)
    assert screen.window_title == "Welcome to LSTtranslate"
    assert screen.pair == "Language pair: Chinese (Simplified) ↔ English"
    assert screen.rows[0].title == "Chinese (Simplified) → English"
    assert screen.rows[0].status == "Download failed: The download server returned an error. Try again later"
    assert screen.rows[1].status == "Not installed"
    assert screen.download_text == "&Download (150.0 MB)"  # 失败的那个也要重新下载
    assert screen.finish_text == "&Skip"


def test_read_pair() -> None:
    assert read_pair(None) == DEFAULT_PAIR
    assert read_pair(app_config()) == DEFAULT_PAIR
    assert read_pair(app_config(["ja", "en"])) == ("ja", "en")
    assert read_pair(app_config(["zh-CN", "en-US"])) == ("zh-Hans", "en")
    for bad in (["en"], ["en", "en"], ["en", 3], ["en", "???"]):
        assert read_pair(app_config(bad)) == DEFAULT_PAIR
    assert read_pair(AppConfig(sections={"bilingual": "oops"}, revision=1)) == DEFAULT_PAIR


# ---------------------------------------------------------------- 展示器


async def test_show_requests_index_once(bus: EventBus, commands: Commands) -> None:
    presenter, view, _store = make(bus)
    presenter.start()
    assert presenter.show_if_needed()
    assert view.shown
    await bus.drain()
    assert commands.refresh == [], "还没有 ModelCatalog 时等目录到达再决定"

    presenter.on_catalog(catalog(index_loaded=False))
    await bus.drain()
    assert commands.refresh == [RefreshModelCatalog()]

    presenter.on_catalog(catalog(index_loaded=False, refreshing=True))
    presenter.on_catalog(catalog(index_loaded=False, error=(ModelErrorCode.NETWORK, "x")))
    await bus.drain()
    assert len(commands.refresh) == 1, "失败后由用户点“重试”再获取，不自动重复请求"


async def test_show_does_not_refetch_loaded_index(bus: EventBus, commands: Commands) -> None:
    bus.publish(zh_en_catalog())
    presenter, view, _store = make(bus)
    presenter.start()
    presenter.show()
    await bus.drain()
    assert view.shown and commands.refresh == []
    assert [row.model_id for row in view.screen.rows] == [ZH_EN, EN_ZH]


async def test_completed_onboarding_is_not_shown(bus: EventBus, commands: Commands) -> None:
    presenter, view, _store = make(bus, completed=True)
    presenter.start()
    assert not presenter.show_if_needed()
    assert not view.shown
    await bus.drain()
    assert commands.refresh == []


async def test_buttons_send_commands(bus: EventBus, commands: Commands) -> None:
    presenter, view, store = make(bus)
    presenter.start()
    presenter.on_catalog(
        catalog(
            info("en", "zh-Hans"),
            info("zh-Hans", "en", installed=True),
            info("en", "ja"),
            downloads={EN_ZH: progress(D.FAILED, code=ModelErrorCode.HTTP)},
        )
    )
    presenter.download()
    presenter.retry(EN_ZH)
    presenter.cancel(ZH_EN)
    presenter.retry_index()
    await bus.drain()
    assert commands.download == [DownloadModels((EN_ZH,)), DownloadModels((EN_ZH,))]
    assert [command.id for command in commands.cancel] == [ZH_EN]
    assert commands.refresh == [RefreshModelCatalog()]

    presenter.show()
    presenter.finish()
    assert store.done and not view.shown


async def test_download_skips_rows_already_in_progress(bus: EventBus, commands: Commands) -> None:
    presenter, _view, _store = make(bus)
    presenter.start()
    presenter.on_catalog(zh_en_catalog(downloads={ZH_EN: progress(D.DOWNLOADING, 1, 10)}))
    presenter.download()
    presenter.on_catalog(zh_en_catalog(downloads={ZH_EN: progress(D.DOWNLOADING), EN_ZH: progress(D.QUEUED)}))
    presenter.download()  # 全部都在下载，没有可发的
    await bus.drain()
    assert commands.download == [DownloadModels((EN_ZH,))]


async def test_config_changes_pair_and_language(bus: EventBus, commands: Commands) -> None:
    presenter, view, _store = make(bus)
    presenter.start()
    presenter.on_catalog(catalog(info("ja", "en"), info("en", "ja"), info("en", "de")))
    assert view.screen.rows == ()  # 默认的 zh-Hans ↔ en 在目录里没有模型
    presenter.on_config(app_config(["ja", "en"]))
    assert [row.title for row in view.screen.rows] == ["日语 → 英语", "英语 → 日语"]

    count = len(view.screens)
    presenter.on_config(app_config(["ja", "en"], revision=2))
    assert len(view.screens) == count, "画面没有变化时不重绘"

    i18n.set_language("en")
    presenter.on_config(app_config(["ja", "en"], revision=3))
    assert [row.title for row in view.screen.rows] == ["Japanese → English", "English → Japanese"]
    await bus.drain()


async def test_closed_presenter_ignores_everything(bus: EventBus, commands: Commands) -> None:
    presenter, view, store = make(bus)
    presenter.start()
    presenter.on_catalog(zh_en_catalog())
    presenter.close()
    count = len(view.screens)
    presenter.on_catalog(ModelCatalog())
    presenter.download()
    presenter.show()
    presenter.finish()
    await bus.drain()
    assert len(view.screens) == count and not view.shown and not store.done
    assert commands.download == [] and commands.refresh == []
