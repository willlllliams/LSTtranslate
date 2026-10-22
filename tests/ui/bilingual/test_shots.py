"""双语窗口、竞速页统计表与覆盖窗口悬停提示的截图，浅色与深色各一张（手动开启：设置 LST_RACE_SHOTS=1）。

截图存到 data/tmp/race-shots/，用来人工检查界面，不做断言之外的比较。
"""

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPainter, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QWidget

from lst.contracts.events import ToggleBilingualWindow
from lst.contracts.ids import EngineId
from lst.contracts.translation import AttemptState
from lst.ui.theme import apply_app_style, theme_colors
from tests.ui.bilingual.conftest import StartHarness, attempt, segment, statuses
from tests.ui.conftest import on_native_platform

TARGET = Path(__file__).resolve().parents[3] / "data" / "tmp" / "race-shots"
SCHEMES = ("light", "dark")

pytestmark = pytest.mark.skipif(not os.environ.get("LST_RACE_SHOTS"), reason="只在需要截图时运行")


@pytest.fixture
def restore_scheme(qapp: QApplication) -> Iterator[None]:
    TARGET.mkdir(parents=True, exist_ok=True)
    yield
    apply_app_style(qapp, "system")


@pytest.mark.usefixtures("restore_scheme")
async def test_bilingual_window_shots(start_harness: StartHarness, qapp: QApplication) -> None:
    for scheme in SCHEMES:
        apply_app_style(qapp, scheme)
        harness = await start_harness(with_engines=False)
        await harness.publish(
            statuses(
                {"argos": "Argos 本地", "deepseek": "DeepSeek", "dict": "字典翻译"}, billing={"deepseek"}
            )
        )
        window = harness.window
        if on_native_platform():
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        harness.bus.send(ToggleBilingualWindow())
        await harness.flush()
        window.resize(760, 560)
        combo = window.engine_combo
        combo.setCurrentIndex(combo.findText("DeepSeek（计费）"))
        command = await harness.type_and_wait(
            "The quick brown fox jumps over the lazy dog.\nStar this repository"
        )
        await harness.updates(
            command.session_id,
            segment(
                "p1",
                "The quick brown fox jumps over the lazy dog.",
                attempt("deepseek", AttemptState.DONE, "敏捷的棕色狐狸跳过了那只懒狗。", latency=900),
                shown_engine="deepseek",
                finished=True,
            ),
            segment(
                "p2",
                "Star this repository",
                attempt("deepseek", AttemptState.DONE, "给这个仓库加星标", latency=900),
                shown_engine="deepseek",
                finished=True,
            ),
        )
        await asyncio.sleep(0.4)  # 等淡入结束
        for _ in range(30):
            QApplication.processEvents()
        assert harness.presenter.engine == EngineId("deepseek")
        # Mica 背景由系统绘制，控件截图里是透明的；先铺上主题底色再画窗口内容。
        image = QPixmap(window.size())
        image.fill(theme_colors().window)
        painter = QPainter(image)
        window.render(painter, QPoint(0, 0), QRegion(), QWidget.RenderFlag.DrawChildren)
        painter.end()
        image.save(str(TARGET / f"bilingual-{scheme}.png"))
        await harness.stop()
