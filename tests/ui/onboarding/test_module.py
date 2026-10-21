"""OnboardingModule 的测试：真实的引导窗口（offscreen 平台）、真实的事件总线和 QSettings 文件。"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from itertools import pairwise
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QWidget

from lst.contracts.config import AppConfig
from lst.contracts.events import ShowModelDownloads
from lst.contracts.models import (
    DownloadModels,
    DownloadState,
    ModelCatalog,
    ModelErrorCode,
    RefreshModelCatalog,
)
from lst.kernel import EventBus, Module, ModuleContext
from lst.ui import i18n
from lst.ui.onboarding import OnboardingModule
from lst.ui.onboarding.store import COMPLETED_KEY
from lst.ui.onboarding.window import ModelRowWidget, OnboardingWindow
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

type StartModule = Callable[[], Awaitable[OnboardingModule]]


@pytest.fixture
async def start_module(
    qapp: QApplication, bus: EventBus, commands: Commands, settings_path: Path
) -> AsyncIterator[StartModule]:
    """返回启动函数，可以多次调用（模拟重新启动程序），结束时全部停止。只在 offscreen 平台运行。

    依赖 commands 是为了让命令记录先于模块订阅：引导启动时就可能发送 RefreshModelCatalog。
    """
    if QApplication.platformName() != "offscreen":
        pytest.skip("引导窗口测试只在 offscreen 平台运行")
    started: list[tuple[OnboardingModule, ModuleContext]] = []

    async def start() -> OnboardingModule:
        module = OnboardingModule(settings_path)
        ctx = ModuleContext(bus, logging.getLogger("lst.ui.onboarding"))
        await module.start(ctx)
        started.append((module, ctx))
        await bus.drain()
        return module

    yield start
    for module, ctx in reversed(started):
        await module.stop()
        ctx.close()


def window_of(module: OnboardingModule) -> OnboardingWindow:
    window = module.window
    assert window is not None
    return window


def row_of(window: OnboardingWindow, model_id: str) -> ModelRowWidget:
    row = window.row(model_id)
    assert row is not None, f"窗口里没有 {model_id} 这一行"
    return row


async def publish(bus: EventBus, message: ModelCatalog | AppConfig) -> None:
    bus.publish(message)
    await bus.drain()


def test_module_satisfies_protocol(settings_path: Path) -> None:
    module: Module = OnboardingModule(settings_path)
    assert module.name == "ui.onboarding"


async def test_first_start_shows_window_and_fetches_index(
    bus: EventBus, commands: Commands, start_module: StartModule
) -> None:
    bus.publish(catalog(index_loaded=False))  # 模型管理模块先启动，已经发布了只含已安装模型的目录
    module = await start_module()
    window = window_of(module)
    assert window.isVisible()
    assert window.windowTitle() == "欢迎使用 LSTtranslate"
    assert commands.refresh == [RefreshModelCatalog()]

    await publish(bus, catalog(index_loaded=False, refreshing=True))
    assert window.notice_label.isVisibleTo(window)
    assert window.notice_label.text() == "正在获取可下载的模型列表…"


async def test_completed_onboarding_stays_hidden_until_show(
    bus: EventBus, commands: Commands, start_module: StartModule, settings_path: Path
) -> None:
    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    settings.setValue(COMPLETED_KEY, True)
    settings.sync()
    bus.publish(catalog(index_loaded=False))
    module = await start_module()
    window = window_of(module)
    assert not window.isVisible()
    assert commands.refresh == []

    module.show()  # 托盘或设置窗口重新打开引导
    await bus.drain()
    assert window.isVisible()
    assert commands.refresh == [RefreshModelCatalog()]


async def test_show_model_downloads_command_reopens_window(
    bus: EventBus, start_module: StartModule, settings_path: Path
) -> None:
    """托盘菜单“下载翻译模型”发送 ShowModelDownloads，引导已经完成过也重新显示窗口。"""
    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    settings.setValue(COMPLETED_KEY, True)
    settings.sync()
    module = await start_module()
    window = window_of(module)
    assert not window.isVisible()

    bus.send(ShowModelDownloads())
    await bus.drain()
    assert window.isVisible()


async def test_rows_follow_catalog(bus: EventBus, start_module: StartModule) -> None:
    module = await start_module()
    window = window_of(module)
    await publish(bus, zh_en_catalog())
    assert [row.model_id for row in window.rows()] == [ZH_EN, EN_ZH]
    first = row_of(window, ZH_EN)
    assert first.title_label.text() == "简体中文 → 英语"
    assert first.size_label.text() == "50.0 MB"
    assert first.status_label.text() == "未安装"
    assert not first.progress_bar.isVisibleTo(window)
    assert window.download_button.isEnabled()
    assert window.download_button.text() == "下载(&D)（共 150.0 MB）"
    assert window.finish_button.text() == "跳过(&S)"

    # 状态变化时原地更新，不重建行控件。
    await publish(bus, catalog(info("en", "zh-Hans", installed=True), info("zh-Hans", "en", installed=True)))
    assert row_of(window, ZH_EN) is first
    assert first.status_label.text() == "已安装（版本 1.9）"
    assert not window.download_button.isEnabled()
    assert window.finish_button.text() == "完成(&F)"

    # 互译对改变时换一组行。
    await publish(bus, catalog(info("ja", "en"), info("en", "ja")))
    await publish(bus, app_config(["ja", "en"]))
    assert [row.title_label.text() for row in window.rows()] == ["日语 → 英语", "英语 → 日语"]


async def test_download_button_sends_command(
    bus: EventBus, commands: Commands, start_module: StartModule
) -> None:
    module = await start_module()
    window = window_of(module)
    await publish(bus, zh_en_catalog())
    window.download_button.click()
    await bus.drain()
    assert commands.download == [DownloadModels((ZH_EN, EN_ZH))]


async def test_progress_cancel_and_retry(
    bus: EventBus, commands: Commands, start_module: StartModule
) -> None:
    module = await start_module()
    window = window_of(module)
    await publish(
        bus,
        zh_en_catalog(
            downloads={
                ZH_EN: progress(D.DOWNLOADING, 20 * MB, 50 * MB),
                EN_ZH: progress(D.FAILED, code=ModelErrorCode.PAIR_MISMATCH, error="模型包是 en→fr"),
            }
        ),
    )
    downloading, failed = row_of(window, ZH_EN), row_of(window, EN_ZH)
    assert downloading.progress_bar.isVisibleTo(window)
    assert (downloading.progress_bar.minimum(), downloading.progress_bar.maximum()) == (0, 100)
    assert downloading.progress_bar.value() == 40
    assert downloading.status_label.text() == "正在下载 40%（20.0 MB / 50.0 MB）"
    assert downloading.cancel_button.isVisibleTo(window)
    assert not downloading.retry_button.isVisibleTo(window)

    assert failed.status_label.text() == "下载失败：下载的模型与列表中的语言不符"
    assert "en→fr" not in failed.status_label.text()  # 技术细节只进日志
    assert failed.retry_button.isVisibleTo(window)
    assert not failed.cancel_button.isVisibleTo(window)
    assert not failed.progress_bar.isVisibleTo(window)

    downloading.cancel_button.click()
    failed.retry_button.click()
    await bus.drain()
    assert [command.id for command in commands.cancel] == [ZH_EN]
    assert commands.download == [DownloadModels((EN_ZH,))]

    # 校验阶段显示没有刻度的忙碌指示，不能取消。
    await publish(bus, zh_en_catalog(downloads={ZH_EN: progress(D.VERIFYING, 50 * MB, 50 * MB)}))
    assert downloading.status_label.text() == "正在校验"
    assert downloading.progress_bar.maximum() == 0
    assert not downloading.cancel_button.isVisibleTo(window)


async def test_index_failure_offers_retry(
    bus: EventBus, commands: Commands, start_module: StartModule
) -> None:
    bus.publish(catalog(index_loaded=False))
    module = await start_module()
    window = window_of(module)
    await publish(bus, catalog(index_loaded=False, error=(ModelErrorCode.NETWORK, "ConnectError: refused")))
    assert window.retry_index_button.isVisibleTo(window)
    # 只有原因和处理建议，技术细节只进日志（架构文档 10.3 节）
    assert window.notice_label.text() == "无法获取模型列表：网络连接失败，请检查网络或代理设置后重试"
    window.retry_index_button.click()
    await bus.drain()
    assert commands.refresh == [RefreshModelCatalog(), RefreshModelCatalog()]

    await publish(bus, zh_en_catalog())
    assert not window.retry_index_button.isVisibleTo(window)
    assert not window.notice_label.isVisibleTo(window)


async def test_finish_records_completion(
    bus: EventBus, start_module: StartModule, settings_path: Path
) -> None:
    module = await start_module()
    window = window_of(module)
    assert window.isVisible()
    window.finish_button.click()
    assert not window.isVisible()
    assert QSettings(str(settings_path), QSettings.Format.IniFormat).value(COMPLETED_KEY) in (True, "true")

    await module.stop()
    again = await start_module()
    assert not window_of(again).isVisible()


async def test_closing_window_postpones_onboarding(
    bus: EventBus, start_module: StartModule, settings_path: Path
) -> None:
    module = await start_module()
    window = window_of(module)
    window.close()  # 标题栏的关闭按钮
    assert not window.isVisible()
    assert QSettings(str(settings_path), QSettings.Format.IniFormat).value(COMPLETED_KEY) is None
    await module.stop()
    again = await start_module()
    assert window_of(again).isVisible()


async def test_english_texts(bus: EventBus, start_module: StartModule) -> None:
    module = await start_module()
    window = window_of(module)
    await publish(bus, zh_en_catalog())
    i18n.set_language("en")
    await publish(bus, app_config(["zh-Hans", "en"]))
    assert window.windowTitle() == "Welcome to LSTtranslate"
    assert window.heading_label.text() == "Download translation models"
    assert window.download_button.text() == "&Download (150.0 MB)"
    assert window.finish_button.text() == "&Skip"
    assert row_of(window, EN_ZH).status_label.text() == "Not installed"
    assert "OCR" in window.ocr_note.text()


async def test_tab_order_follows_visual_order(bus: EventBus, start_module: StartModule) -> None:
    module = await start_module()
    window = window_of(module)
    await publish(bus, zh_en_catalog())
    rows = window.rows()
    chain = [
        rows[0].cancel_button,
        rows[0].retry_button,
        rows[1].cancel_button,
        rows[1].retry_button,
        window.retry_index_button,
        window.download_button,
        window.finish_button,
    ]
    for first, second in pairwise(chain):
        assert _next_tab_stop(first) is second


def _next_tab_stop(widget: QWidget) -> QWidget:
    """按 Tab 时焦点链里下一个接受 Tab 焦点的控件；进度条、标签这类不接受焦点的控件被跳过。"""
    current = widget.nextInFocusChain()
    while current is not None and not current.focusPolicy() & Qt.FocusPolicy.TabFocus:
        current = current.nextInFocusChain()
    assert current is not None
    return current


async def test_stop_closes_window(start_module: StartModule) -> None:
    module = await start_module()
    window = window_of(module)
    await module.stop()
    assert module.window is None and module.presenter is None
    assert not window.isVisible()
    with pytest.raises(RuntimeError):
        module.show()
