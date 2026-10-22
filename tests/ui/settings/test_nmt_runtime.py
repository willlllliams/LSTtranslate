"""模型页“显卡加速”分组的测试（需求 MOD-06）。

检查推理设备下拉框的选项与提交，显卡运行库卡片随 NmtRuntimeInfo 各状态的文字、进度条与按钮，按钮发出的命令，
删除前的确认，显卡行与实际设备行，以及文案中英文齐全、占位符一致。

设置环境变量 ``LST_SETTINGS_SHOTS=1`` 时，``test_capture_screenshots`` 把模型页的“显卡加速”分组截成浅色、
深色两张图，存到 data/tmp/settings-shots/nmt-runtime-*.png。
"""

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from lst.contracts.events import WorkerStatus, WorkerStatuses
from lst.contracts.nmt_runtime import (
    CancelNmtRuntimeInstall,
    DeleteNmtRuntime,
    InstallNmtRuntime,
    NmtRuntimeErrorCode,
    NmtRuntimeInfo,
    NmtRuntimeState,
)
from lst.contracts.ports import WorkerState
from lst.ui import i18n
from lst.ui.settings import strings_nmt
from lst.ui.settings.controls import ChoiceEditor
from lst.ui.settings.nmt_runtime_card import NmtRuntimeCard
from lst.ui.settings.nmt_runtime_rows import Tone, build_view
from lst.ui.settings.page_models import GpuPage, NmtModelsPage
from lst.ui.theme import apply_app_style
from tests.ui.conftest import on_native_platform
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import FakeDialogs, default_config

DOWNLOAD_BYTES = 579_840_000  # 约 553.0 MB
INSTALLED_BYTES = 734_003_200  # 700.0 MB
VERSION = "12.9.1.4"
GPU = "NVIDIA GeForce RTX 4070"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def runtime(
    state: NmtRuntimeState = NmtRuntimeState.NOT_INSTALLED,
    *,
    installed: bool = False,
    gpus: tuple[str, ...] = (GPU,),
    progress: float | None = None,
    error_code: NmtRuntimeErrorCode | None = None,
    version: str = VERSION,
    installed_version: str = VERSION,
) -> NmtRuntimeInfo:
    return NmtRuntimeInfo(
        state=state,
        version=version,
        installed_version=installed_version if installed else None,
        download_bytes=DOWNLOAD_BYTES,
        installed_bytes=INSTALLED_BYTES if installed else 0,
        gpus=gpus,
        progress=progress,
        error_code=error_code,
        detail="不应显示的技术细节",
    )


def nmt_worker(device: str | None, state: WorkerState = WorkerState.READY) -> WorkerStatuses:
    versions = {"ctranslate2": "4.6.0", "device": device} if device is not None else {}
    return WorkerStatuses({"nmt": WorkerStatus(state, versions=versions)})


def models_page(harness: Harness) -> GpuPage:
    """显卡加速页。"""
    page = harness.window.page("models.gpu")
    assert isinstance(page, GpuPage)
    return page


def card(harness: Harness) -> NmtRuntimeCard:
    return models_page(harness).runtime_card


def shown_buttons(runtime_card: NmtRuntimeCard) -> set[str]:
    buttons = {
        "download": runtime_card.download_button,
        "cancel": runtime_card.cancel_button,
        "retry": runtime_card.retry_button,
        "delete": runtime_card.delete_button,
    }
    return {name for name, button in buttons.items() if not button.isHidden()}


@pytest.fixture(autouse=True)
def chinese() -> Iterator[None]:
    previous = i18n.current_language()
    i18n.set_language("zh-Hans")
    yield
    i18n.set_language(previous)


# ---------------------------------------------------------------------- 纯逻辑


def test_view_for_each_state() -> None:
    view = build_view(runtime(), configured="auto", actual=None)
    assert (view.status, view.tone, view.progress) == ("未安装", Tone.NORMAL, None)
    assert view.can_download and view.download_text == "下载（553.0 MB）"
    assert not (view.can_cancel or view.can_retry or view.can_delete)
    assert "553.0 MB" in view.description and VERSION in view.description
    assert "nvidia-cublas-cu12" in view.description and "NVIDIA 软件许可" in view.description

    view = build_view(runtime(NmtRuntimeState.DOWNLOADING, progress=0.426), configured="auto", actual=None)
    assert (view.status, view.tone, view.progress) == ("正在下载 42%", Tone.ACTIVE, 0.426)
    assert view.can_cancel and not (view.can_download or view.can_retry or view.can_delete)

    view = build_view(runtime(NmtRuntimeState.DOWNLOADING), configured="auto", actual=None)
    assert (view.status, view.progress) == ("正在下载", -1.0)

    view = build_view(runtime(NmtRuntimeState.INSTALLING), configured="auto", actual=None)
    assert (view.status, view.progress) == ("正在校验与解压", -1.0)
    assert not (view.can_download or view.can_cancel or view.can_retry or view.can_delete)

    view = build_view(runtime(NmtRuntimeState.INSTALLED, installed=True), configured="auto", actual=None)
    assert view.status == f"已安装 {VERSION}，占用 700.0 MB"
    assert view.can_delete and not (view.can_download or view.can_cancel or view.can_retry)


def test_error_view_shows_reason_and_suggestion_but_not_the_detail() -> None:
    info = runtime(NmtRuntimeState.ERROR, error_code=NmtRuntimeErrorCode.DISK)
    view = build_view(info, configured="auto", actual=None)
    reason, suggestion = i18n.error_text(NmtRuntimeErrorCode.DISK)
    assert view.status == f"下载失败：{reason}"
    assert view.suggestion == f"处理建议：{suggestion}"
    assert view.tone is Tone.ERROR
    assert view.can_retry and not (view.can_delete or view.can_download)
    assert "技术细节" not in view.status + view.suggestion

    kept = build_view(
        runtime(NmtRuntimeState.ERROR, installed=True, error_code=NmtRuntimeErrorCode.NETWORK),
        configured="auto",
        actual=None,
    )
    assert f"已安装的 {VERSION} 仍然可用" in kept.status
    assert kept.can_retry and kept.can_delete

    unknown = build_view(runtime(NmtRuntimeState.ERROR), configured="auto", actual=None)
    assert unknown.status == f"下载失败：{i18n.error_text(NmtRuntimeErrorCode.INTERNAL)[0]}"


def test_installed_older_version_offers_an_update() -> None:
    info = runtime(NmtRuntimeState.INSTALLED, installed=True, installed_version="12.8.4.1")
    view = build_view(info, configured="auto", actual=None)
    assert view.status == f"已安装 12.8.4.1，占用 700.0 MB；可以更新到 {VERSION}"
    assert view.can_download and view.download_text == "更新（553.0 MB）"
    assert view.can_delete


def test_unknown_sizes_and_missing_info() -> None:
    info = NmtRuntimeInfo(download_bytes=0, version="")
    view = build_view(info, configured="auto", actual=None)
    assert view.download_text == f"下载（{i18n.tr('common.size.unknown')}）"
    assert VERSION not in view.description

    missing = build_view(None, configured="auto", actual=None)
    assert missing.status == "暂时无法管理显卡运行库"
    assert not (missing.can_download or missing.can_cancel or missing.can_retry or missing.can_delete)
    assert missing.gpu == ""


def test_gpu_line() -> None:
    found = build_view(runtime(gpus=(GPU, "NVIDIA RTX A2000")), configured="auto", actual=None)
    assert found.gpu == f"检测到 NVIDIA 显卡：{GPU}、NVIDIA RTX A2000"
    none = build_view(runtime(gpus=()), configured="auto", actual=None)
    assert none.gpu == "没有检测到 NVIDIA 显卡，下载后也用不上"
    assert none.can_download  # 没有显卡也可以下载


def test_device_line() -> None:
    installed = runtime(NmtRuntimeState.INSTALLED, installed=True)
    assert build_view(installed, configured="auto", actual=None).device == ""
    assert build_view(installed, configured="auto", actual="unknown").device == ""

    gpu = build_view(installed, configured="auto", actual="cuda")
    assert (gpu.device, gpu.device_warning) == ("本地翻译当前在显卡上运行", False)

    # 装了运行库、有显卡、没有选“仅 CPU”，实际却在 CPU 上：提示查看日志。
    for configured in ("auto", "cuda"):
        odd = build_view(installed, configured=configured, actual="cpu")
        assert odd.device_warning, configured
        assert odd.device.startswith("本地翻译当前在 CPU 上运行") and "日志" in odd.device

    # 以下几种在 CPU 上运行是预期的，不提示。
    for info, configured in (
        (installed, "cpu"),
        (runtime(), "auto"),
        (runtime(NmtRuntimeState.INSTALLED, installed=True, gpus=()), "auto"),
    ):
        plain = build_view(info, configured=configured, actual="cpu")
        assert (plain.device, plain.device_warning) == ("本地翻译当前在 CPU 上运行", False)


# ---------------------------------------------------------------------- 文案


def test_texts_have_both_languages_with_the_same_placeholders() -> None:
    texts = dict(strings_nmt.CATALOG)
    for code in NmtRuntimeErrorCode:
        for part in ("reason", "suggestion"):
            key = f"error.{code.value}.{part}"
            assert i18n.is_registered(key), key
            texts[key] = {language: i18n_text(key, language) for language in i18n.SUPPORTED}
    for key, pair in texts.items():
        assert set(pair) == set(i18n.SUPPORTED), key
        assert all(pair.values()), key
        assert set(_PLACEHOLDER.findall(pair["zh-Hans"])) == set(_PLACEHOLDER.findall(pair["en"])), key
    for value in ("auto", "cpu", "cuda"):
        assert f"settings.choice.nmt.device.{value}" in strings_nmt.CATALOG


def i18n_text(key: str, language: str) -> str:
    previous = i18n.current_language()
    i18n.set_language(language)
    try:
        return i18n.tr(key)
    finally:
        i18n.set_language(previous)


def test_english_view() -> None:
    i18n.set_language("en")
    view = build_view(runtime(NmtRuntimeState.DOWNLOADING, progress=0.5), configured="auto", actual="cpu")
    assert view.status == "Downloading 50%"
    assert view.gpu == f"NVIDIA graphics card found: {GPU}"
    assert view.device == "Local translation is running on the CPU"


# ---------------------------------------------------------------------- 页面


async def test_device_choice_lists_three_options_and_commits(harness: Harness) -> None:
    page = models_page(harness)
    binding = next(b for b in page.bindings if b.location == ("nmt", "device"))
    editor = binding.editor
    assert isinstance(editor, ChoiceEditor)
    combo = editor.combo
    texts = [combo.itemText(index) for index in range(combo.count())]
    assert texts == ["自动", "仅 CPU", "NVIDIA 显卡（CUDA）"]
    assert binding.card.title_label.text() == "推理设备"
    assert "本地翻译进程会出错" in binding.card.desc_label.text()
    editor.committed.emit("cuda")
    await harness.flush()
    assert harness.recorded.updates[-1].patch == {"nmt": {"device": "cuda"}}


async def test_card_without_runtime_state_has_no_buttons(harness: Harness) -> None:
    runtime_card = card(harness)
    assert runtime_card.title_label.text() == "NVIDIA cuBLAS 12 运行库"
    assert runtime_card.status_label.text() == "暂时无法管理显卡运行库"
    assert shown_buttons(runtime_card) == set()
    assert runtime_card.device_row.isHidden()


async def test_card_follows_states_and_sends_commands(harness: Harness) -> None:
    runtime_card = card(harness)
    await harness.publish(runtime())
    assert runtime_card.status_label.text() == "未安装"
    assert shown_buttons(runtime_card) == {"download"}
    assert runtime_card.download_button.text() == "下载（553.0 MB）"
    assert runtime_card.progress_bar.isHidden()
    assert runtime_card.gpu_label.text() == f"检测到 NVIDIA 显卡：{GPU}"
    runtime_card.download_button.click()
    await harness.flush()
    assert len(harness.recorded.of(InstallNmtRuntime)) == 1

    await harness.publish(runtime(NmtRuntimeState.DOWNLOADING, progress=0.25))
    assert runtime_card.status_label.text() == "正在下载 25%"
    assert shown_buttons(runtime_card) == {"cancel"}
    assert not runtime_card.progress_bar.isHidden()
    assert runtime_card.progress_bar.maximum() > 0
    assert runtime_card.progress_bar.value() == runtime_card.progress_bar.maximum() // 4
    runtime_card.cancel_button.click()
    await harness.flush()
    assert len(harness.recorded.of(CancelNmtRuntimeInstall)) == 1

    await harness.publish(runtime(NmtRuntimeState.INSTALLING))
    assert runtime_card.status_label.text() == "正在校验与解压"
    assert shown_buttons(runtime_card) == set()
    assert runtime_card.progress_bar.maximum() == 0  # 忙碌动画

    await harness.publish(runtime(NmtRuntimeState.ERROR, error_code=NmtRuntimeErrorCode.TIMEOUT))
    assert runtime_card.status_label.text() == "下载失败：下载超时"
    assert runtime_card.suggestion_label.text() == "处理建议：稍后重试"
    assert not runtime_card.suggestion_label.isHidden()
    assert shown_buttons(runtime_card) == {"retry"}
    runtime_card.retry_button.click()
    await harness.flush()
    assert len(harness.recorded.of(InstallNmtRuntime)) == 2

    await harness.publish(runtime(NmtRuntimeState.INSTALLED, installed=True))
    assert runtime_card.status_label.text() == f"已安装 {VERSION}，占用 700.0 MB"
    assert runtime_card.suggestion_label.isHidden()
    assert runtime_card.progress_bar.isHidden()
    assert shown_buttons(runtime_card) == {"delete"}


async def test_delete_asks_first(start_settings: StartSettings) -> None:
    dialogs = FakeDialogs(confirm=False)
    harness = await start_settings(dialogs=dialogs)
    runtime_card = card(harness)
    await harness.publish(runtime(NmtRuntimeState.INSTALLED, installed=True))
    runtime_card.delete_button.click()
    await harness.flush()
    assert harness.recorded.of(DeleteNmtRuntime) == []
    assert len(dialogs.confirms) == 1
    text = dialogs.confirms[0][1]
    assert "700.0 MB" in text and "短暂重启" in text

    dialogs.confirm_answer = True
    runtime_card.delete_button.click()
    await harness.flush()
    assert len(harness.recorded.of(DeleteNmtRuntime)) == 1


async def test_gpu_and_device_rows(start_settings: StartSettings) -> None:
    harness = await start_settings(config=default_config({"nmt": {"device": "auto"}}))
    runtime_card = card(harness)
    await harness.publish(runtime(gpus=()))
    assert runtime_card.gpu_label.text() == "没有检测到 NVIDIA 显卡，下载后也用不上"
    assert shown_buttons(runtime_card) == {"download"}
    assert runtime_card.device_row.isHidden()

    await harness.publish(runtime(NmtRuntimeState.INSTALLED, installed=True), nmt_worker("cuda"))
    assert runtime_card.device_label.text() == "本地翻译当前在显卡上运行"
    assert not runtime_card.device_row.isHidden()

    await harness.publish(nmt_worker("cpu"))
    assert "日志" in runtime_card.device_label.text()
    assert runtime_card.view is not None and runtime_card.view.device_warning

    # 配置改为“仅 CPU”后，在 CPU 上运行是预期的。
    await harness.publish(default_config({"nmt": {"device": "cpu"}}))
    assert runtime_card.device_label.text() == "本地翻译当前在 CPU 上运行"
    assert not runtime_card.view.device_warning

    # 进程没有报告设备时不显示这一行。
    await harness.publish(nmt_worker(None, WorkerState.LOADING))
    assert runtime_card.device_row.isHidden()


async def test_card_switches_language(harness: Harness) -> None:
    runtime_card = card(harness)
    await harness.publish(runtime())
    await harness.publish(default_config({"ui": {"language": "en"}}))
    assert runtime_card.title_label.text() == "NVIDIA cuBLAS 12 runtime"
    assert runtime_card.status_label.text() == "Not installed"
    assert runtime_card.download_button.text() == "Download (553.0 MB)"


async def test_gpu_page_lists_the_device_before_the_runtime_card(harness: Harness) -> None:
    page = models_page(harness)
    layout = page.body
    widgets = [layout.itemAt(index).widget() for index in range(layout.count())]  # pyright: ignore[reportOptionalMemberAccess]
    device_card = next(b.card for b in page.bindings if b.location == ("nmt", "device"))
    order = [widgets.index(widget) for widget in (device_card, page.runtime_card)]
    assert order == sorted(order)
    assert [b.location for b in page.bindings] == [("nmt", "device")]


async def test_models_page_puts_the_location_and_the_list_under_their_steps(harness: Harness) -> None:
    """模型目录在“取模型”步骤的面板里，模型列表在“CTranslate2 推理”步骤的面板里（默认打开）。"""
    page = harness.window.page("models.nmt")
    assert isinstance(page, NmtModelsPage)
    dir_card = next(b.card for b in page.bindings if b.location == ("nmt", "model_dir"))
    assert page.flow.panels["model"].isAncestorOf(dir_card)
    assert page.flow.panels["infer"].isAncestorOf(page.list_card)
    assert page.flow.selected == "infer"


# ---------------------------------------------------------------------- 截图


@pytest.fixture
def restore_scheme(qapp: QApplication) -> Iterator[None]:
    yield
    apply_app_style(qapp, "system")


@pytest.mark.skipif(not os.environ.get("LST_SETTINGS_SHOTS"), reason="只在需要截图时运行")
@pytest.mark.usefixtures("restore_scheme")
async def test_capture_screenshots(start_settings: StartSettings, qapp: QApplication) -> None:
    target = Path(__file__).resolve().parents[3] / "data" / "tmp" / "settings-shots"
    target.mkdir(parents=True, exist_ok=True)
    for scheme in ("light", "dark"):
        apply_app_style(qapp, scheme)
        harness = await start_settings(config=default_config({"nmt": {"device": "auto"}}))
        await harness.publish(
            runtime(NmtRuntimeState.DOWNLOADING, progress=0.42),
            nmt_worker("cpu"),
        )
        window = await harness.open()
        if on_native_platform():
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.resize(1100, 900)
        window.show_page("models.gpu")
        page = models_page(harness)
        QApplication.processEvents()
        content = page.widget()
        assert content is not None
        content.adjustSize()
        content.grab().save(str(target / f"nmt-runtime-{scheme}.png"))
        await harness.publish(
            runtime(NmtRuntimeState.ERROR, installed=True, error_code=NmtRuntimeErrorCode.NETWORK),
            nmt_worker("cuda"),
        )
        QApplication.processEvents()
        content.adjustSize()
        content.grab().save(str(target / f"nmt-runtime-error-{scheme}.png"))
        await harness.stop()
