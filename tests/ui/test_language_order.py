"""切换界面语言后，已经打开的窗口和托盘按新语言刷新，与各模块订阅 AppConfig 的先后无关。

组合根自己的 AppConfig 处理函数是协程，set_language 在下一轮循环才执行；各界面模块的处理函数是同步的，
按订阅顺序执行。组合根的启动顺序是托盘、双语窗口在设置窗口之前，所以每个按配置刷新文案的模块都要
自己按配置设置界面语言，不能依赖别的模块先设置。
"""

import logging
from pathlib import Path

from PySide6.QtCore import QSettings, Qt

from lst.contracts.events import ToggleBilingualWindow
from lst.kernel import EventBus, ModuleContext
from lst.ui import i18n
from lst.ui.bilingual import BilingualModule
from lst.ui.onboarding import OnboardingModule
from lst.ui.onboarding.store import COMPLETED_KEY
from lst.ui.settings import SettingsModule
from lst.ui.tray import TrayModule
from tests.ui.conftest import on_native_platform
from tests.ui.settings.support import FakeDialogs, all_models, default_config, engine_kinds


async def test_language_switch_reaches_modules_started_before_settings(bus: EventBus, tmp_path: Path) -> None:
    state = tmp_path / "ui_state.ini"
    # 托盘图标在原生平台上会出现在任务栏通知区，所以托盘只在 offscreen 平台上参与（与托盘测试相同）。
    tray = (
        TrayModule("LSTtranslate", None, tmp_path, tray_available=False) if not on_native_platform() else None
    )
    bilingual = BilingualModule(state, clipboard=lambda _text: None)
    # 引导标记为已完成：窗口创建后保持隐藏（原生平台上不出现在屏幕上），文案照样随配置刷新。
    marks = QSettings(str(state), QSettings.Format.IniFormat)
    marks.setValue(COMPLETED_KEY, True)
    marks.sync()
    onboarding = OnboardingModule(state)
    settings = SettingsModule(
        state, sections=all_models(), engine_kinds=engine_kinds(), dialogs=FakeDialogs(), app_version="9.9.9"
    )
    bus.subscribe(ToggleBilingualWindow, lambda _command: None)  # 托盘图标单击时发送，这里不用
    bus.publish(default_config({"ui": {"language": "zh-Hans"}}, revision=1))
    # 与组合根相同：托盘、双语窗口、引导先启动，设置窗口最后启动。
    modules = tuple(m for m in (tray, bilingual, onboarding, settings) if m is not None)
    contexts = [ModuleContext(bus, logging.getLogger(module.name)) for module in modules]
    for module, ctx in zip(modules, contexts, strict=True):
        await module.start(ctx)
    await bus.drain()
    window = bilingual.window
    assert window is not None and onboarding.window is not None
    assert not onboarding.window.isVisible()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    assert window.windowTitle() == "双语互译"

    i18n.set_language("zh-Hans")
    bus.publish(default_config({"ui": {"language": "en"}}, revision=2))  # 用户在设置里改成英文
    await bus.drain()
    try:
        assert window.windowTitle() == "Bilingual Translator"
        if tray is not None:
            assert tray.view is not None
            assert tray.view.tray.toolTip().isascii(), tray.view.tray.toolTip()
        assert onboarding.window.windowTitle() == "Welcome to LSTtranslate"
    finally:
        for module, ctx in reversed(list(zip(modules, contexts, strict=True))):
            await module.stop()
            ctx.close()
