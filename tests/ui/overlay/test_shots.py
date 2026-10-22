"""双语窗口、竞速页统计表与覆盖窗口悬停提示的截图，浅色与深色各一张（手动开启：设置 LST_RACE_SHOTS=1）。

截图存到 data/tmp/race-shots/，用来人工检查界面，不做断言之外的比较。
"""

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QWidget

from lst.contracts.ids import EngineId, SegmentId
from lst.contracts.translation import AttemptState, AttemptView, SegmentView
from lst.ui.overlay import tooltip_text
from lst.ui.theme import apply_app_style
from tests.ui.conftest import on_native_platform
from tests.ui.overlay.conftest import MakeRig, capture, text_result, view

TARGET = Path(__file__).resolve().parents[3] / "data" / "tmp" / "race-shots"
SCHEMES = ("light", "dark")

pytestmark = pytest.mark.skipif(not os.environ.get("LST_RACE_SHOTS"), reason="只在需要截图时运行")


@pytest.fixture
def restore_scheme(qapp: QApplication) -> Iterator[None]:
    TARGET.mkdir(parents=True, exist_ok=True)
    yield
    apply_app_style(qapp, "system")


@pytest.mark.usefixtures("restore_scheme")
async def test_overlay_tooltip_shots(make_rig: MakeRig, qapp: QApplication) -> None:
    names = {EngineId("deepseek"): "DeepSeek", EngineId("deepl"): "DeepL", EngineId("argos"): "Argos"}
    attempts = (
        AttemptView(EngineId("deepseek"), AttemptState.DONE, "你好，世界", None, 900.0, started=True),
        AttemptView(EngineId("deepl"), AttemptState.CANCELLED, None, None, 950.0, started=True),
        AttemptView(EngineId("argos"), AttemptState.DONE, "你好世界", None, 120.0, started=True),
    )
    for scheme in SCHEMES:
        apply_app_style(qapp, scheme)
        rig = await make_rig()
        await rig.publish(capture("s1", light=scheme == "light"))
        await rig.publish(text_result("s1"))
        window = rig.window
        window.apply_views([view("p1", "Hello world", "你好，世界", engine="deepseek", finished=True)])
        window.fit_all_now()
        item = window.items[SegmentId("p1")]
        item.view = SegmentView(
            SegmentId("p1"), "Hello world", "你好，世界", EngineId("deepseek"), True, attempts
        )
        deadline = time.monotonic() + 0.4  # 等过渡结束
        while time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        # QToolTip 是一个弹出的 QLabel；这里用同样的富文本排一个 QLabel，与覆盖窗口并排截图。
        sheet = QWidget()
        row = QHBoxLayout(sheet)
        shot = QLabel()
        shot.setPixmap(window.grab())
        tip = QLabel(tooltip_text(item, names))
        tip.setStyleSheet(
            "QLabel { border: 1px solid palette(mid); padding: 6px;"
            " background: palette(tooltip-base); color: palette(tooltip-text); }"
        )
        row.addWidget(shot)
        row.addWidget(tip, 0, Qt.AlignmentFlag.AlignTop)
        if on_native_platform():
            sheet.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        sheet.show()
        QApplication.processEvents()
        sheet.grab().save(str(TARGET / f"overlay-tooltip-{scheme}.png"))
        sheet.close()
        window.close()
