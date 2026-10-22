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
    UninstallOcrEngine,
)
from lst.ui import i18n
from lst.ui.common.text import LANGUAGES
from lst.ui.settings.ocr_engine_rows import Tone, build_view
from lst.ui.settings.page_capture import PADDLE_ONLY, CapturePage, OcrEngineCard
from lst.ui.theme import Glyph
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import FakeDialogs, default_config
from tests.ui.settings.test_keyboard import assert_visual_order, tab_stops

PADDLE_BYTES = 1_288_490_188  # 约 1.2 GB


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


def catalog(
    windows: OcrEngineInfo | None = None,
    paddle: OcrEngineInfo | None = None,
    active: OcrEngineKind | None = OcrEngineKind.WINDOWS,
) -> OcrEngineCatalog:
    return OcrEngineCatalog((windows or windows_info(), paddle or paddle_info()), active)


def capture_page(harness: Harness) -> CapturePage:
    page = harness.window.page("capture")
    assert isinstance(page, CapturePage)
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
    window.show_page("capture")
    QApplication.processEvents()
    return harness


# ---------------------------------------------------------------- 没有引擎目录


async def test_without_a_catalog_the_cards_are_hidden_and_a_notice_explains(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("capture")
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
    page = capture_page(shown)
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
    page = capture_page(harness)
    mode, formula = page.binding(("ocr", "mode")), page.binding(("ocr", "formula"))
    assert mode is not None and formula is not None
    assert mode.editor.json_value() == "text" and formula.editor.json_value() is True
    assert not mode.card.isEnabled()


# ---------------------------------------------------------------- 识别语言


async def test_language_dropdown_lists_only_windows_ocr_languages(shown: Harness) -> None:
    page = capture_page(shown)
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
    page = capture_page(harness)
    assert page.language_codes() == ["auto", "zh-Hans", "en", "fr"]
    language = page.binding(("ocr", "language"))
    assert language is not None
    editor = language.editor
    assert editor.json_value() == "fr"
    combo = editor.focus_widget()
    assert combo.property("currentText") == "法语（当前引擎不支持）"
    assert harness.recorded.updates == []  # 只是显示，不改配置


async def test_choosing_a_windows_language_commits_it(shown: Harness) -> None:
    page = capture_page(shown)
    language = page.binding(("ocr", "language"))
    assert language is not None
    combo = language.editor.focus_widget()
    combo.setProperty("currentIndex", 2)
    await shown.flush()
    assert shown.recorded.updates[-1].patch == {"ocr": {"language": "en"}}


async def test_language_list_survives_a_language_switch(shown: Harness) -> None:
    await shown.publish(default_config({"ui": {"language": "en"}}, revision=3))
    page = capture_page(shown)
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


async def test_tab_order_reaches_engine_buttons_before_the_ocr_settings(shown: Harness) -> None:
    await shown.publish(catalog(paddle=paddle_info(OcrEngineState.INSTALLED)))
    QApplication.processEvents()
    window, page = shown.window, capture_page(shown)
    stops = tab_stops(window, page)
    paddle = card(shown, OcrEngineKind.PADDLE)
    enabled = page.binding(("ocr", "enabled"))
    assert enabled is not None
    switch = enabled.editor.focus_widget()
    assert stops.index(paddle.uninstall_button) < stops.index(paddle.use_button) < stops.index(switch)
    assert_visual_order(window, stops, "capture")
    mode = page.binding(("ocr", "mode"))
    assert mode is not None and mode.editor.focus_widget() not in stops  # 不可用的卡片不在 Tab 顺序里


def test_view_for_every_state_is_consistent() -> None:
    """每种状态都给出非空的状态文字；“取消”可用时一定显示；当前引擎不提供“设为当前引擎”。"""
    i18n.set_language("zh-Hans")
    for state in OcrEngineState:
        for info in (paddle_info(state), windows_info(state)):
            for current in (False, True):
                view = build_view(info, current=current)
                assert view.status, (state, info.kind)
                assert view.can_cancel or not view.cancel_enabled
                assert not (current and view.can_use)
                assert not (view.can_download and view.can_uninstall)
