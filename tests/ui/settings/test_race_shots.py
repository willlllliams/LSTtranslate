"""双语窗口、竞速页统计表与覆盖窗口悬停提示的截图，浅色与深色各一张（手动开启：设置 LST_RACE_SHOTS=1）。

截图存到 data/tmp/race-shots/，用来人工检查界面，不做断言之外的比较。
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from lst.contracts.events import EngineStatsRow, StatsSnapshot
from lst.contracts.ids import EngineId
from lst.ui.settings.pages import RacePage
from lst.ui.theme import apply_app_style
from tests.ui.conftest import on_native_platform
from tests.ui.settings.conftest import StartSettings

TARGET = Path(__file__).resolve().parents[3] / "data" / "tmp" / "race-shots"
SCHEMES = ("light", "dark")

pytestmark = pytest.mark.skipif(not os.environ.get("LST_RACE_SHOTS"), reason="只在需要截图时运行")


@pytest.fixture
def restore_scheme(qapp: QApplication) -> Iterator[None]:
    TARGET.mkdir(parents=True, exist_ok=True)
    yield
    apply_app_style(qapp, "system")


@pytest.mark.usefixtures("restore_scheme")
async def test_race_page_stats_shots(start_settings: StartSettings, qapp: QApplication) -> None:
    for scheme in SCHEMES:
        apply_app_style(qapp, scheme)
        harness = await start_settings()
        window = await harness.open()
        if on_native_platform():
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.resize(1100, 900)
        window.show_page("race")
        await harness.publish(
            StatsSnapshot(
                (
                    EngineStatsRow(EngineId("argos"), 120, 30, 180.0, 420.0, 0.0),
                    EngineStatsRow(EngineId("deepseek"), 80, 50, 1500.0, 2600.0, 0.05),
                )
            )
        )
        page = window.page("race")
        assert isinstance(page, RacePage)
        QApplication.processEvents()
        content = page.widget()
        assert content is not None
        content.adjustSize()
        content.grab().save(str(TARGET / f"race-page-{scheme}.png"))
        await harness.stop()
