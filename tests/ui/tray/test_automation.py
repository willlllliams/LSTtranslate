"""托盘的悬停提示在自动化接口开启期间多一行“自动化接口已开启”（需求 DEV-03）。"""

import logging
from pathlib import Path

from lst.contracts.automation import AutomationStatus, BuildFlavor
from lst.contracts.messages import Message
from lst.kernel import EventBus, ModuleContext
from lst.ui.tray.presenter import Hotkeys, TrayPresenter, build_menu
from tests.ui.tray.fakes import FakeTimer, FakeView


def test_tooltip_line_only_when_listening() -> None:
    plain = build_menu("LSTtranslate", {}, None, Hotkeys())
    on = build_menu("LSTtranslate", {}, None, Hotkeys(), automation=True)
    assert plain.tooltip == "LSTtranslate\n没有启用的组件"
    assert on.tooltip == "LSTtranslate\n没有启用的组件\n自动化接口已开启"


async def test_presenter_follows_automation_status(
    bus: EventBus, received: list[Message], tmp_path: Path
) -> None:
    del received
    view = FakeView()
    ctx = ModuleContext(bus, logging.getLogger("tests.ui.tray.automation"))
    presenter = TrayPresenter(ctx, view, app_name="LSTtranslate", log_dir=tmp_path, timer_factory=FakeTimer)
    presenter.start()
    try:
        await bus.drain()
        assert "自动化接口" not in view.menu.tooltip
        bus.publish(AutomationStatus(BuildFlavor.DEV, enabled=True, explicit=False, listening=True, pipe="p"))
        await bus.drain()
        assert view.menu.tooltip.endswith("自动化接口已开启")
        # 开关打开了但管道没建起来（例如同名管道被占用）：不算开启。
        bus.publish(
            AutomationStatus(BuildFlavor.DEV, enabled=True, explicit=True, listening=False, error="x")
        )
        await bus.drain()
        assert "自动化接口" not in view.menu.tooltip
    finally:
        presenter.close()
