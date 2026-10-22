"""截屏与识别页的 OCR 引擎卡片测试（需求 OCRE-01 至 OCRE-06、UI-05、UI-07）。

检查引擎卡片随 OcrEngineCatalog 各状态的文字、按钮与进度条，按钮发出的命令，“设为当前引擎”的 UpdateConfig，
当前引擎为 Windows 自带 OCR 时 PaddleOCR 专属设置项的禁用，以及识别语言下拉框随可识别语言变化。
"""

from dataclasses import dataclass

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from lst.contracts.config import ConfigRejected
from lst.contracts.ocr_engines import (
    CancelOcrEngineInstall,
    InstallOcrEngine,
    OcrEngineCatalog,
    OcrEngineErrorCode,
    OcrEngineInfo,
    OcrEngineKind,
    OcrEngineState,
    OcrStage,
    UninstallOcrEngine,
)
from lst.ui import i18n
from lst.ui.common.text import LANGUAGES
from lst.ui.settings.ocr_engine_rows import Tone, build_view
from lst.ui.settings.page_capture import (
    ENGINE_PAGES,
    PADDLE_ONLY,
    CaptureEnginesPage,
    CaptureOptionsPage,
    OcrEngineCard,
)
from lst.ui.settings.page_ocr_engine import OcrEnginePage
from lst.ui.theme import Glyph
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import FakeDialogs, default_config
from tests.ui.settings.test_keyboard import assert_visual_order, tab_stops

PADDLE_BYTES = 1_288_490_188  # 约 1.2 GB
RAPID_BYTES = 111_164_637  # 约 106.0 MB
RAPID_LANGUAGES = ("zh-Hans", "zh-Hant", "en", "ja", "de", "fr")


def windows_info(
    state: OcrEngineState = OcrEngineState.INSTALLED,
    languages: tuple[str, ...] = ("zh-Hans", "en"),
    error_code: OcrEngineErrorCode | None = None,
) -> OcrEngineInfo:
    return OcrEngineInfo(
        OcrEngineKind.WINDOWS,
        state,
        needs_download=False,
        languages=languages,
        features=("text",),
        error_code=error_code,
    )


def paddle_info(
    state: OcrEngineState = OcrEngineState.NOT_INSTALLED,
    progress: float | None = None,
    error_code: OcrEngineErrorCode | None = None,
) -> OcrEngineInfo:
    return OcrEngineInfo(
        OcrEngineKind.PADDLE,
        state,
        needs_download=True,
        download_bytes=PADDLE_BYTES,
        progress=progress,
        languages=("zh-Hans", "en", "ja"),
        features=("text", "layout", "table", "formula"),
        error_code=error_code,
        detail="不应显示的技术细节",
    )


def rapid_info(
    state: OcrEngineState = OcrEngineState.NOT_INSTALLED,
    progress: float | None = None,
    error_code: OcrEngineErrorCode | None = None,
) -> OcrEngineInfo:
    return OcrEngineInfo(
        OcrEngineKind.RAPID,
        state,
        needs_download=True,
        download_bytes=RAPID_BYTES,
        progress=progress,
        languages=RAPID_LANGUAGES,
        features=("text",),
        error_code=error_code,
    )


def catalog(
    windows: OcrEngineInfo | None = None,
    paddle: OcrEngineInfo | None = None,
    active: OcrEngineKind | None = OcrEngineKind.WINDOWS,
    rapid: OcrEngineInfo | None = None,
) -> OcrEngineCatalog:
    return OcrEngineCatalog(
        (windows or windows_info(), rapid or rapid_info(), paddle or paddle_info()), active
    )


def capture_page(harness: Harness) -> CaptureEnginesPage:
    """OCR 引擎页。"""
    page = harness.window.page("capture.engines")
    assert isinstance(page, CaptureEnginesPage)
    return page


def options_page(harness: Harness) -> CaptureOptionsPage:
    """截图与识别选项页。"""
    page = harness.window.page("capture.options")
    assert isinstance(page, CaptureOptionsPage)
    return page


def card(harness: Harness, kind: OcrEngineKind) -> OcrEngineCard:
    return capture_page(harness).engine_cards[kind]


def visible_buttons(engine_card: OcrEngineCard) -> list[str]:
    """卡片上显示出来的按钮的名字，按从左到右的顺序。"""
    names = {
        id(engine_card.download_button): "download",
        id(engine_card.cancel_button): "cancel",
        id(engine_card.retry_button): "retry",
        id(engine_card.uninstall_button): "uninstall",
        id(engine_card.use_button): "use",
    }
    buttons = sorted(
        (b for b in engine_card.findChildren(QPushButton) if id(b) in names),
        key=lambda b: b.mapTo(engine_card, b.rect().topLeft()).x(),
    )
    return [names[id(b)] for b in buttons if not b.isHidden()]


@pytest.fixture
async def shown(harness: Harness) -> Harness:
    await harness.publish(catalog())
    window = await harness.open()
    window.resize(1000, 760)
    window.show_page("capture.engines")
    QApplication.processEvents()
    return harness


# ---------------------------------------------------------------- 没有引擎目录


async def test_without_a_catalog_the_cards_are_hidden_and_a_notice_explains(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("capture.engines")
    page = capture_page(harness)
    assert page.engines_missing.isVisibleTo(page)
    assert "OCR 引擎的状态" in page.engines_missing.text()
    assert all(card.isHidden() for card in page.engine_cards.values())
    await harness.publish(catalog())
    assert not page.engines_missing.isVisibleTo(page)
    assert all(not card.isHidden() for card in page.engine_cards.values())


# ---------------------------------------------------------------- 各状态的文字、按钮与进度条


@dataclass(frozen=True)
class Case:
    info: OcrEngineInfo
    status: str
    buttons: list[str]
    progress: int | None  # None：不显示进度条；-1：忙碌动画；否则是进度条的取值（0 至 1000）
    tone: Tone


PADDLE_CASES = {
    "not_installed": Case(paddle_info(), "未安装", ["download"], None, Tone.NORMAL),
    "queued": Case(paddle_info(OcrEngineState.QUEUED), "等待安装", ["cancel"], None, Tone.NORMAL),
    "downloading": Case(
        paddle_info(OcrEngineState.DOWNLOADING, 0.375), "正在下载 37%", ["cancel"], 375, Tone.ACTIVE
    ),
    "downloading_unknown": Case(
        paddle_info(OcrEngineState.DOWNLOADING), "正在下载…", ["cancel"], -1, Tone.ACTIVE
    ),
    "installing": Case(
        paddle_info(OcrEngineState.INSTALLING, 0.9), "正在安装和校验…", ["cancel"], 900, Tone.ACTIVE
    ),
    "installed": Case(
        paddle_info(OcrEngineState.INSTALLED), "已安装", ["uninstall", "use"], None, Tone.NORMAL
    ),
    "error_network": Case(
        paddle_info(OcrEngineState.ERROR, error_code=OcrEngineErrorCode.NETWORK),
        "出错：网络连接失败",
        ["retry", "uninstall"],
        None,
        Tone.ERROR,
    ),
    "error_worker": Case(
        paddle_info(OcrEngineState.ERROR, error_code=OcrEngineErrorCode.WORKER_FAILED),
        "出错：识别进程反复退出",
        ["uninstall", "use"],
        None,
        Tone.ERROR,
    ),
}


@pytest.mark.parametrize("name", list(PADDLE_CASES))
async def test_paddle_card_follows_every_install_state(shown: Harness, name: str) -> None:
    case = PADDLE_CASES[name]
    await shown.publish(catalog(paddle=case.info))
    paddle = card(shown, OcrEngineKind.PADDLE)
    assert paddle.status_label.text() == case.status
    assert visible_buttons(paddle) == case.buttons
    assert paddle.view is not None and paddle.view.tone is case.tone
    if case.progress is None:
        assert paddle.progress_bar.isHidden()
    elif case.progress < 0:
        assert not paddle.progress_bar.isHidden() and paddle.progress_bar.maximum() == 0
    else:
        assert not paddle.progress_bar.isHidden() and paddle.progress_bar.value() == case.progress
    assert "不应显示" not in paddle.status_label.text()  # 技术细节只写日志


async def test_cancel_is_shown_but_disabled_while_installing(shown: Harness) -> None:
    await shown.publish(catalog(paddle=paddle_info(OcrEngineState.INSTALLING)))
    paddle = card(shown, OcrEngineKind.PADDLE)
    assert not paddle.cancel_button.isHidden() and not paddle.cancel_button.isEnabled()
    await shown.publish(catalog(paddle=paddle_info(OcrEngineState.DOWNLOADING, 0.1)))
    assert paddle.cancel_button.isEnabled()


async def test_error_shows_reason_and_suggestion(shown: Harness) -> None:
    await shown.publish(catalog(paddle=paddle_info(OcrEngineState.ERROR, error_code=OcrEngineErrorCode.DISK)))
    paddle = card(shown, OcrEngineKind.PADDLE)
    assert paddle.status_label.text() == "出错：无法写入安装目录，或磁盘空间不足"
    assert paddle.suggestion_label.text() == "处理建议：检查磁盘空间和目录权限后重试。"
    assert paddle.status_icon.text() == Glyph.ERROR


async def test_card_texts_describe_capabilities_and_download_size(shown: Harness) -> None:
    windows, paddle = card(shown, OcrEngineKind.WINDOWS), card(shown, OcrEngineKind.PADDLE)
    assert windows.title_label.text() == "Windows 自带 OCR"
    assert "能力：文本；不需要下载" in windows.desc_label.text()
    assert paddle.title_label.text() == "PaddleOCR"
    assert "能力：文本、版面、表格、公式；需要下载约 1.2 GB" in paddle.desc_label.text()
    assert windows.languages_label.text() == "可识别的语言：简体中文、英语"
    assert paddle.languages_label.isHidden()


async def test_windows_without_ocr_languages_explains_how_to_add_one(shown: Harness) -> None:
    info = windows_info(OcrEngineState.UNAVAILABLE, (), OcrEngineErrorCode.NO_LANGUAGE)
    await shown.publish(catalog(windows=info))
    windows = card(shown, OcrEngineKind.WINDOWS)
    assert windows.status_label.text() == "不可用：Windows 没有安装任何语言的 OCR 功能"
    assert windows.view is not None and windows.view.tone is Tone.ERROR
    assert not windows.help_label.isHidden()
    for step in ("时间和语言", "语言和区域", "语言选项", "光学字符识别", "重新启动"):
        assert step in windows.help_label.text()
    assert windows.suggestion_label.isHidden()  # 步骤已经写在说明里，不重复处理建议
    assert visible_buttons(windows) == []


async def test_windows_unavailable_for_other_reasons_shows_the_reason(shown: Harness) -> None:
    info = windows_info(OcrEngineState.UNAVAILABLE, (), OcrEngineErrorCode.INTERNAL)
    await shown.publish(catalog(windows=info))
    windows = card(shown, OcrEngineKind.WINDOWS)
    assert windows.status_label.text() == "不可用：内部错误"
    assert windows.help_label.isHidden() and not windows.suggestion_label.isHidden()


async def test_loading_and_ready_states_of_the_current_engine(shown: Harness) -> None:
    await shown.publish(catalog(windows=windows_info(OcrEngineState.LOADING)))
    windows = card(shown, OcrEngineKind.WINDOWS)
    assert windows.status_label.text() == "正在加载…"
    await shown.publish(catalog(windows=windows_info(OcrEngineState.READY)))
    assert windows.status_label.text() == "就绪"
    assert windows.status_icon.text() == Glyph.COMPLETED
    assert visible_buttons(windows) == []  # 不需要下载的引擎没有卸载；它是当前引擎，没有“设为当前”


# ---------------------------------------------------------------- 当前引擎


async def test_current_engine_has_a_badge_and_others_offer_set_as_current(shown: Harness) -> None:
    await shown.publish(catalog(paddle=paddle_info(OcrEngineState.INSTALLED)))
    windows, paddle = card(shown, OcrEngineKind.WINDOWS), card(shown, OcrEngineKind.PADDLE)
    assert windows.current and not windows.current_badge.isHidden()
    assert windows.badge_label.text() == "当前引擎"
    assert windows.title_label.accessibleName() == "Windows 自带 OCR，当前引擎"
    assert not paddle.current and paddle.current_badge.isHidden()
    paddle.use_button.click()
    await shown.flush()
    assert [c.patch for c in shown.recorded.updates] == [{"ocr": {"engine": "paddle"}}]

    await shown.publish(default_config({"ocr": {"engine": "paddle"}}, revision=2))
    assert paddle.current and not paddle.current_badge.isHidden() and paddle.use_button.isHidden()
    assert not windows.current and not windows.use_button.isHidden()
    windows.use_button.click()
    await shown.flush()
    assert shown.recorded.updates[-1].patch == {"ocr": {"engine": "windows"}}


async def test_an_engine_that_is_not_installed_cannot_be_made_current(shown: Harness) -> None:
    paddle = card(shown, OcrEngineKind.PADDLE)
    assert paddle.use_button.isHidden()
    shown.presenter.set_ocr_engine(OcrEngineKind.WINDOWS)  # 已经是当前引擎：不发送
    await shown.flush()
    assert shown.recorded.updates == []


async def test_rejected_engine_change_is_shown_under_the_engine_cards(shown: Harness) -> None:
    await shown.publish(ConfigRejected(("ocr", "engine"), "取值不在可选范围内"))
    page = capture_page(shown)
    assert page.engine_error.text() == "未保存：取值不在可选范围内"
    assert not page.engine_error.isHidden()
    assert shown.window.unclaimed_rejections() == []


# ---------------------------------------------------------------- 按钮发出的命令


async def test_buttons_send_install_cancel_and_retry_commands(shown: Harness) -> None:
    paddle = card(shown, OcrEngineKind.PADDLE)
    paddle.download_button.click()
    await shown.publish(catalog(paddle=paddle_info(OcrEngineState.DOWNLOADING, 0.2)))
    paddle.cancel_button.click()
    await shown.publish(catalog(paddle=paddle_info(OcrEngineState.ERROR, error_code=OcrEngineErrorCode.HTTP)))
    paddle.retry_button.click()
    await shown.flush()
    assert shown.recorded.commands == [
        InstallOcrEngine(OcrEngineKind.PADDLE),
        CancelOcrEngineInstall(OcrEngineKind.PADDLE),
        InstallOcrEngine(OcrEngineKind.PADDLE),
    ]


async def test_uninstall_asks_for_confirmation(start_settings: StartSettings) -> None:
    dialogs = FakeDialogs(confirm=False)
    harness = await start_settings(dialogs=dialogs)
    await harness.publish(catalog(paddle=paddle_info(OcrEngineState.INSTALLED)))
    paddle = card(harness, OcrEngineKind.PADDLE)
    paddle.uninstall_button.click()
    await harness.flush()
    assert harness.recorded.of(UninstallOcrEngine) == []
    assert "卸载 PaddleOCR？" in dialogs.confirms[0][1]
    assert "当前引擎" not in dialogs.confirms[0][1]

    dialogs.confirm_answer = True
    await harness.publish(default_config({"ocr": {"engine": "paddle"}}, revision=2))
    paddle.uninstall_button.click()
    await harness.flush()
    assert harness.recorded.of(UninstallOcrEngine) == [UninstallOcrEngine(OcrEngineKind.PADDLE)]
    assert "它是当前引擎" in dialogs.confirms[1][1]  # 卸载当前引擎时说明截屏翻译会暂时不可用


# ---------------------------------------------------------------- PaddleOCR 专属的设置项


async def test_paddle_only_cards_are_disabled_while_windows_is_current(shown: Harness) -> None:
    page = options_page(shown)
    paddle_only = page.paddle_only_bindings()
    assert {b.location[-1] for b in paddle_only} == PADDLE_ONLY
    for binding in paddle_only:
        assert not binding.card.isEnabled(), binding.location
        assert binding.card.desc_label.text().endswith("。仅 PaddleOCR 可用。"), binding.location
    for location in (
        ("ocr", "enabled"),
        ("ocr", "language"),
        ("ocr", "grayscale"),
        ("ocr", "resize_max_side"),
    ):
        found = page.binding(location)
        assert found is not None and found.card.isEnabled(), location

    await shown.publish(default_config({"ocr": {"engine": "paddle"}}, revision=2))
    for binding in paddle_only:
        assert binding.card.isEnabled(), binding.location
        assert "仅 PaddleOCR" not in binding.card.desc_label.text()
    mode = page.binding(("ocr", "mode"))
    assert mode is not None and mode.card.desc_label.text().startswith("版面模式分析")


async def test_disabled_cards_keep_the_configured_values(start_settings: StartSettings) -> None:
    harness = await start_settings(config=default_config({"ocr": {"mode": "text", "formula": True}}))
    page = options_page(harness)
    mode = page.binding(("ocr", "mode"))
    assert mode is not None
    assert mode.editor.json_value() == "text"
    assert not mode.card.isEnabled()
    paddle = harness.window.page("capture.paddle")
    assert isinstance(paddle, OcrEnginePage)
    assert paddle.pipeline.toggles[OcrStage.FORMULA].editor.json_value() is True


# ---------------------------------------------------------------- 识别语言


async def test_language_dropdown_lists_only_windows_ocr_languages(shown: Harness) -> None:
    page = options_page(shown)
    assert page.language_codes() == ["auto", "zh-Hans", "en"]
    language = page.binding(("ocr", "language"))
    assert language is not None and language.card.desc_label.text().startswith(
        "Windows 自带 OCR 只能识别系统已安装 OCR 功能的语言"
    )

    await shown.publish(catalog(windows=windows_info(languages=("ja", "ko", "en"))))
    assert page.language_codes() == ["auto", "ja", "ko", "en"]

    await shown.publish(default_config({"ocr": {"engine": "paddle"}}, revision=2))
    assert page.language_codes() == ["auto", *LANGUAGES]
    assert "只列出" not in language.card.desc_label.text()


async def test_configured_language_missing_from_windows_is_kept_and_marked(
    start_settings: StartSettings,
) -> None:
    harness = await start_settings(config=default_config({"ocr": {"language": "fr"}}))
    await harness.publish(catalog())
    page = options_page(harness)
    assert page.language_codes() == ["auto", "zh-Hans", "en", "fr"]
    language = page.binding(("ocr", "language"))
    assert language is not None
    editor = language.editor
    assert editor.json_value() == "fr"
    combo = editor.focus_widget()
    assert combo.property("currentText") == "法语（当前引擎不支持）"
    assert harness.recorded.updates == []  # 只是显示，不改配置


async def test_choosing_a_windows_language_commits_it(shown: Harness) -> None:
    page = options_page(shown)
    language = page.binding(("ocr", "language"))
    assert language is not None
    combo = language.editor.focus_widget()
    combo.setProperty("currentIndex", 2)
    await shown.flush()
    assert shown.recorded.updates[-1].patch == {"ocr": {"language": "en"}}


async def test_language_list_survives_a_language_switch(shown: Harness) -> None:
    await shown.publish(default_config({"ui": {"language": "en"}}, revision=3))
    page = options_page(shown)
    assert page.language_codes() == ["auto", "zh-Hans", "en"]
    windows = card(shown, OcrEngineKind.WINDOWS)
    assert windows.title_label.text() == "Windows built-in OCR"
    assert windows.badge_label.text() == "Current engine"
    assert windows.languages_label.text() == "Recognized languages: Chinese (Simplified), English"
    mode = page.binding(("ocr", "mode"))
    assert mode is not None and mode.card.desc_label.text().endswith("Only available with PaddleOCR.")
    paddle = card(shown, OcrEngineKind.PADDLE)
    assert paddle.status_label.text() == "Not installed"
    assert paddle.download_button.text() == "&Download"


# ---------------------------------------------------------------- 键盘


async def test_tab_order_reaches_the_engine_buttons_in_order(shown: Harness) -> None:
    await shown.publish(catalog(paddle=paddle_info(OcrEngineState.INSTALLED)))
    QApplication.processEvents()
    window, page = shown.window, capture_page(shown)
    stops = tab_stops(window, page)
    paddle = card(shown, OcrEngineKind.PADDLE)
    assert (
        stops.index(paddle.uninstall_button)
        < stops.index(paddle.use_button)
        < stops.index(paddle.manage_button)
    )
    assert_visual_order(window, stops, "capture.engines")


async def test_tab_order_on_the_options_page_skips_disabled_cards(shown: Harness) -> None:
    window = shown.window
    window.show_page("capture.options")
    QApplication.processEvents()
    page = options_page(shown)
    stops = tab_stops(window, page)
    enabled = page.binding(("ocr", "enabled"))
    assert enabled is not None and enabled.editor.focus_widget() in stops
    assert_visual_order(window, stops, "capture.options")
    mode = page.binding(("ocr", "mode"))
    assert mode is not None and mode.editor.focus_widget() not in stops  # 不可用的卡片不在 Tab 顺序里


def test_view_for_every_state_is_consistent() -> None:
    """每种状态都给出非空的状态文字；“取消”可用时一定显示；当前引擎不提供“设为当前引擎”。"""
    i18n.set_language("zh-Hans")
    for state in OcrEngineState:
        for info in (paddle_info(state), windows_info(state), rapid_info(state)):
            for current in (False, True):
                view = build_view(info, current=current)
                assert view.status, (state, info.kind)
                assert view.can_cancel or not view.cancel_enabled
                assert not (current and view.can_use)
                assert not (view.can_download and view.can_uninstall)


# ---------------------------------------------------------------- RapidOCR


async def test_rapid_card_describes_the_engine(shown: Harness) -> None:
    rapid = card(shown, OcrEngineKind.RAPID)
    assert not rapid.isHidden()
    assert rapid.title_label.text() == "RapidOCR"
    assert "轻量，按行给出位置，只有文本模式。" in rapid.desc_label.text()
    assert "能力：文本；需要下载约 106.0 MB" in rapid.desc_label.text()
    assert rapid.icon_label.text() == Glyph.LIGHTNING
    assert rapid.status_label.text() == "未安装"
    assert visible_buttons(rapid) == ["download"]
    assert rapid.languages_label.isHidden()  # 语言很多，列在识别语言的下拉框里，卡片上不重复


async def test_rapid_card_follows_install_states_and_sends_commands(shown: Harness) -> None:
    rapid = card(shown, OcrEngineKind.RAPID)
    rapid.download_button.click()
    await shown.publish(catalog(rapid=rapid_info(OcrEngineState.DOWNLOADING, 0.5)))
    assert rapid.status_label.text() == "正在下载 50%"
    assert visible_buttons(rapid) == ["cancel"] and rapid.progress_bar.value() == 500
    rapid.cancel_button.click()
    await shown.publish(
        catalog(rapid=rapid_info(OcrEngineState.ERROR, error_code=OcrEngineErrorCode.INSTALL_FAILED))
    )
    assert visible_buttons(rapid) == ["retry", "uninstall"]
    rapid.retry_button.click()
    await shown.publish(catalog(rapid=rapid_info(OcrEngineState.INSTALLED)))
    assert rapid.status_label.text() == "已安装"
    assert visible_buttons(rapid) == ["uninstall", "use"]
    await shown.flush()
    assert shown.recorded.commands == [
        InstallOcrEngine(OcrEngineKind.RAPID),
        CancelOcrEngineInstall(OcrEngineKind.RAPID),
        InstallOcrEngine(OcrEngineKind.RAPID),
    ]
    rapid.use_button.click()
    await shown.flush()
    assert shown.recorded.updates[-1].patch == {"ocr": {"engine": "rapid"}}


async def test_rapid_current_disables_paddle_only_cards_and_lists_its_languages(shown: Harness) -> None:
    await shown.publish(catalog(rapid=rapid_info(OcrEngineState.READY), active=OcrEngineKind.RAPID))
    await shown.publish(default_config({"ocr": {"engine": "rapid"}}, revision=2))
    page = options_page(shown)
    rapid = card(shown, OcrEngineKind.RAPID)
    assert rapid.current and rapid.status_label.text() == "就绪"
    for binding in page.paddle_only_bindings():
        assert not binding.card.isEnabled(), binding.location
        assert binding.card.desc_label.text().endswith("。仅 PaddleOCR 可用。"), binding.location
    assert page.language_codes() == ["auto", *RAPID_LANGUAGES]
    language = page.binding(("ocr", "language"))
    assert language is not None
    assert language.card.desc_label.text().startswith("RapidOCR 的识别模型覆盖中文、日文、英文")
    assert language.card.isEnabled()

    await shown.publish(default_config({"ocr": {"engine": "rapid", "language": "ko"}}, revision=3))
    assert page.language_codes() == ["auto", *RAPID_LANGUAGES, "ko"]
    combo = language.editor.focus_widget()
    assert combo.property("currentText") == "韩语（当前引擎不支持）"

    await shown.publish(default_config({"ocr": {"engine": "paddle"}}, revision=4))
    assert page.language_codes() == ["auto", *LANGUAGES]
    for binding in page.paddle_only_bindings():
        assert binding.card.isEnabled(), binding.location


async def test_rapid_texts_in_english(shown: Harness) -> None:
    await shown.publish(default_config({"ui": {"language": "en"}, "ocr": {"engine": "rapid"}}, revision=3))
    rapid = card(shown, OcrEngineKind.RAPID)
    assert "Lightweight. Gives the position of each line; text mode only." in rapid.desc_label.text()
    assert "about 106.0 MB to download" in rapid.desc_label.text()
    language = options_page(shown).binding(("ocr", "language"))
    assert language is not None
    assert language.card.desc_label.text().startswith("RapidOCR's recognition model covers")


async def test_catalog_without_rapid_hides_its_card(shown: Harness) -> None:
    await shown.publish(OcrEngineCatalog((windows_info(), paddle_info()), OcrEngineKind.WINDOWS))
    assert card(shown, OcrEngineKind.RAPID).isHidden()
    assert not card(shown, OcrEngineKind.PADDLE).isHidden()


# ---------------------------------------------------------------- 引擎卡片与子页的跳转


async def test_manage_button_is_shown_for_installed_engines_and_opens_the_engine_page(shown: Harness) -> None:
    await shown.publish(
        catalog(paddle=paddle_info(OcrEngineState.INSTALLED), rapid=rapid_info(OcrEngineState.NOT_INSTALLED))
    )
    window = shown.window
    windows, rapid, paddle = (card(shown, kind) for kind in OcrEngineKind)
    assert not windows.manage_button.isHidden() and not paddle.manage_button.isHidden()
    assert rapid.manage_button.isHidden()  # 没安装的引擎没有“管理”
    paddle.manage_button.click()
    assert window.current_page.page_id == "capture.paddle"
    window.show_page("capture.engines")
    windows.manage_button.click()
    assert window.current_page.page_id == "capture.windows"
    assert ENGINE_PAGES == {
        OcrEngineKind.WINDOWS: "capture.windows",
        OcrEngineKind.RAPID: "capture.rapid",
        OcrEngineKind.PADDLE: "capture.paddle",
    }


async def test_engine_page_of_an_uninstalled_engine_explains_and_jumps_back(shown: Harness) -> None:
    window = shown.window
    window.show_page("capture.paddle")
    page = window.page("capture.paddle")
    assert isinstance(page, OcrEnginePage)
    QApplication.processEvents()
    assert not page.install_card.isHidden()
    assert page.install_card.title_label.text() == "PaddleOCR 尚未安装"
    assert "请先在“OCR 引擎”页下载 PaddleOCR" in page.install_card.desc_label.text()
    assert not page.goto_button.isHidden()
    page.goto_button.click()
    assert window.current_page.page_id == "capture.engines"

    # 装好以后，说明和按钮消失。
    await shown.publish(catalog(paddle=paddle_info(OcrEngineState.INSTALLED)))
    window.show_page("capture.paddle")
    assert page.install_card.isHidden()
    assert page.runtime_card.title_label.text() == "PaddleOCR"
    assert page.runtime_card.desc_label.text() == "已安装"


async def test_windows_page_shows_only_what_the_engine_has(shown: Harness) -> None:
    window = shown.window
    window.show_page("capture.windows")
    page = window.page("capture.windows")
    assert isinstance(page, OcrEnginePage)
    QApplication.processEvents()
    assert page.install_card.isHidden()  # 不需要下载
    assert "可识别的语言：简体中文、英语" in page.runtime_card.desc_label.text()
    # 流程图照常显示 Windows OCR 的步骤，下面一句说明它没有可选的模型和参数。
    assert not page.pipeline.isHidden() and not page.empty_label.isHidden()
    assert page.pipeline.visible_stages() == []
    assert page.pipeline.toggles == {}
    assert page.heading.text() == "截屏与识别 › Windows OCR"


async def test_engine_page_notes_that_the_engine_is_not_current(shown: Harness) -> None:
    await shown.publish(catalog(paddle=paddle_info(OcrEngineState.INSTALLED)))
    shown.window.show_page("capture.paddle")
    page = shown.window.page("capture.paddle")
    assert isinstance(page, OcrEnginePage)
    assert not page.note_label.isHidden() and "不是当前引擎" in page.note_label.text()
    await shown.publish(default_config({"ocr": {"engine": "paddle"}}, revision=2))
    assert page.note_label.isHidden()
