"""关于与诊断页的“自动化接口”开关与构建类型（需求 DEV-01 至 DEV-03），以及存储位置卡片里供设置页与自动化接口
共用的判断（plan_data_dir、plan_models_dir）。"""

import os

from lst.contracts.automation import AutomationStatus, BuildFlavor
from lst.contracts.config import UpdateConfig
from lst.contracts.storage import (
    ChangeDataDir,
    ChangeModelsDir,
    DataDirSource,
    StorageKind,
    StorageLocations,
    StorageProgress,
)
from lst.ui.settings.page_about import AboutPage, automation_status_text, version_text
from lst.ui.settings.page_storage import StorageSection
from lst.ui.settings.pages import GeneralPage
from tests.ui.settings.conftest import Harness, StartSettings

PIPE = r"\\.\pipe\lsttranslate-automation-0123456789ab"


def about(harness: Harness) -> AboutPage:
    page = harness.window.page("about")
    assert isinstance(page, AboutPage)
    return page


def status(**changes: object) -> AutomationStatus:
    fields: dict[str, object] = {
        "flavor": BuildFlavor.DEV,
        "enabled": True,
        "explicit": False,
        "listening": True,
        "pipe": PIPE,
    }
    fields.update(changes)
    return AutomationStatus(**fields)  # pyright: ignore[reportArgumentType]


def test_version_line_names_the_dev_build() -> None:
    assert version_text("0.3.0", BuildFlavor.DEV) == "版本 0.3.0（开发版）"
    assert version_text("0.3.0", BuildFlavor.NORMAL) == "版本 0.3.0"
    assert version_text("", BuildFlavor.DEV) == ""


def test_status_line() -> None:
    assert automation_status_text(None) == ("这次运行没有装配自动化接口。", False)
    assert automation_status_text(status()) == (f"正在监听：{PIPE}", False)
    assert automation_status_text(status(enabled=False, listening=False)) == ("未开启。", False)
    assert automation_status_text(status(listening=False, error="管道已被占用")) == (
        "无法开启：管道已被占用",
        True,
    )


async def test_card_without_status_is_disabled(start_settings: StartSettings) -> None:
    harness = await start_settings(flavor=BuildFlavor.NORMAL)
    card = about(harness).automation_card
    assert not card.switch.isEnabled()
    assert card.status_label.text() == "这次运行没有装配自动化接口。"
    assert "普通版默认关闭" in card.desc_label.text()
    assert about(harness).version_label.text() == "版本 9.9.9"


async def test_card_shows_state_and_writes_the_switch(start_settings: StartSettings) -> None:
    harness = await start_settings(flavor=BuildFlavor.DEV)
    page = about(harness)
    assert page.version_label.text() == "版本 9.9.9（开发版）"
    assert "开发版默认开启" in page.automation_card.desc_label.text()
    assert "截屏" in page.automation_card.desc_label.text()
    await harness.publish(status())
    card = page.automation_card
    assert card.switch.isEnabled() and card.switch.isChecked()
    assert card.status_label.text() == f"正在监听：{PIPE}"
    harness.recorded.clear()
    card.switch.click()
    await harness.flush()
    assert harness.recorded.updates == [UpdateConfig({"automation": {"enabled": False}})]
    await harness.publish(status(enabled=False, explicit=True, listening=False))
    assert not card.switch.isChecked() and card.status_label.text() == "未开启。"


DATA = os.path.normpath("C:/Users/me/AppData/Local/LSTtranslate")
MODELS = os.path.join(DATA, "models")


def section(harness: Harness) -> StorageSection:
    page = harness.window.page("general")
    assert isinstance(page, GeneralPage)
    return page.storage


def locations(**changes: object) -> StorageLocations:
    fields: dict[str, object] = {"data_dir": DATA, "data_source": DataDirSource.DEFAULT, "models_dir": MODELS}
    fields.update(changes)
    return StorageLocations(**fields)  # pyright: ignore[reportArgumentType]


async def test_storage_plans(start_settings: StartSettings) -> None:
    harness = await start_settings()
    storage = section(harness)
    assert storage.plan_data_dir("D:/x") == "unavailable"
    await harness.publish(locations())
    other = os.path.normpath("D:/LST data")
    assert storage.plan_data_dir(other) == "change"
    assert storage.plan_data_dir(DATA) == "same"
    assert storage.plan_models_dir("") == "same"
    assert storage.plan_models_dir(MODELS) == "same"
    assert storage.plan_models_dir("D:/models") == "change"
    await harness.publish(locations(pending_data_dir=other))
    assert storage.plan_data_dir(other) == "same"
    assert storage.plan_data_dir(DATA) == "revert"
    harness.recorded.clear()
    assert storage.submit_data_dir(DATA, copy=True) == "revert"
    await harness.flush()
    assert harness.recorded.of(ChangeDataDir) == [ChangeDataDir(DATA, copy=False)]
    await harness.publish(locations(data_source=DataDirSource.ARGUMENT))
    assert storage.plan_data_dir(other) == "fixed"
    await harness.publish(locations(models_dir="D:/models", models_default=False))
    assert storage.plan_models_dir("") == "change"
    assert storage.submit_models_dir("", move=False) == "change"
    await harness.flush()
    assert harness.recorded.of(ChangeModelsDir) == [ChangeModelsDir("", move=False)]
    await harness.publish(locations(task=StorageProgress(StorageKind.MODELS, "D:/m")))
    assert storage.plan_models_dir("D:/elsewhere") == "busy" and storage.plan_data_dir(other) == "busy"
