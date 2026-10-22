"""常规页“存储位置”一组的测试（需求 CFG-06、CFG-07、MOD-04）：两张卡片显示 StorageLocations 里的路径与状态，
“更改…”经选目录、带选项的确认后发出命令，“打开”交给资源管理器，重启提示与“立即重启”，进度与失败原因，
数据目录由命令行或环境变量指定时不能更改；本地翻译模型页的单独目录在留空时显示实际位置；中英文文案齐全。

设置环境变量 ``LST_SETTINGS_SHOTS=1`` 时，``test_storage_screenshots`` 把常规页截成浅色、深色两张图，
存到 data/tmp/settings-shots/storage-general-*.png。
"""

import os
import re
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from lst.contracts.storage import (
    ChangeDataDir,
    ChangeModelsDir,
    DataDirSource,
    RestartApp,
    StorageErrorCode,
    StorageFailure,
    StorageKind,
    StorageLocations,
    StorageProgress,
)
from lst.ui import i18n
from lst.ui.settings.controls import DirectoryEditor
from lst.ui.settings.page_models import NmtModelsPage
from lst.ui.settings.page_storage import StorageSection
from lst.ui.settings.pages import GeneralPage
from lst.ui.settings.strings_storage import CATALOG
from tests.ui.conftest import on_native_platform
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import FakeDialogs, default_config

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
DATA = os.path.normpath("C:/Users/me/AppData/Local/LSTtranslate")
MODELS = os.path.join(DATA, "models")


def locations(**changes: object) -> StorageLocations:
    base = StorageLocations(data_dir=DATA, data_source=DataDirSource.DEFAULT, models_dir=MODELS)
    fields = {name: getattr(base, name) for name in base.__dataclass_fields__}
    fields.update(changes)
    return StorageLocations(**fields)  # pyright: ignore[reportArgumentType]


def section_of(harness: Harness) -> StorageSection:
    page = harness.window.page("general")
    assert isinstance(page, GeneralPage)
    return page.storage


async def start_with(
    start_settings: StartSettings, state: StorageLocations | None, dialogs: FakeDialogs | None = None
) -> Harness:
    harness = await start_settings(dialogs=dialogs or FakeDialogs())
    if state is not None:
        await harness.publish(state)
    return harness


async def test_without_a_storage_module_the_cards_say_so(start_settings: StartSettings) -> None:
    harness = await start_with(start_settings, None)
    section = section_of(harness)
    for card in section.cards:
        assert card.desc_label.text() == "这个版本没有提供存储位置的设置。"
        assert card.change_button.isHidden() and card.open_button.isHidden()
    section.change_data_dir()  # 没有状态时什么也不做
    assert harness.dialogs.directories == []


async def test_cards_show_the_paths_and_the_default_notes(start_settings: StartSettings) -> None:
    harness = await start_with(start_settings, locations())
    page = harness.window.page("general")
    section = section_of(harness)
    titles = [label.text() for label, _ in page._titles]  # pyright: ignore[reportPrivateUsage]
    assert titles[-1] == "存储位置"
    data, models = section.cards
    assert (data.title_label.text(), models.title_label.text()) == ("数据目录", "模型目录")
    assert data.path_label.text() == DATA and models.path_label.text() == MODELS
    assert data.status_label.text() == "当前是默认位置。"
    assert models.status_label.text() == "当前是默认位置：数据目录下的 models。"
    assert "重启程序才生效" in data.desc_label.text() and "不需要重启程序" in models.desc_label.text()
    assert models.reset_button.isHidden() and data.restart_button.isHidden()
    assert data.change_button.isEnabled() and models.change_button.isEnabled()


async def test_change_data_dir_asks_with_copy_checked_and_sends_the_command(
    start_settings: StartSettings, tmp_path: Path
) -> None:
    dialogs = FakeDialogs(directory=(tmp_path / "new").as_posix())  # 文件夹对话框给出的是正斜杠
    harness = await start_with(start_settings, locations(), dialogs)
    section = section_of(harness)
    section.data_card.change_button.click()
    await harness.flush()
    assert dialogs.directories == [("选择新的数据目录", DATA)]
    [(title, text, option, checked)] = dialogs.options
    assert title == "数据目录" and option == "把现有数据复制到新位置" and checked
    assert str(tmp_path / "new") in text and "重启程序后生效" in text and "凭据管理器" in text
    assert harness.recorded.of(ChangeDataDir) == [ChangeDataDir(str(tmp_path / "new"), True)]

    dialogs.option_answer = False  # 取消勾选“复制现有数据”
    section.data_card.change_button.click()
    await harness.flush()
    assert harness.recorded.of(ChangeDataDir)[-1] == ChangeDataDir(str(tmp_path / "new"), False)

    dialogs.confirm_answer = False  # 对话框里点“取消”
    section.data_card.change_button.click()
    await harness.flush()
    assert len(harness.recorded.of(ChangeDataDir)) == 2


async def test_choosing_the_current_dir_changes_nothing_or_undoes_a_pending_change(
    start_settings: StartSettings,
) -> None:
    dialogs = FakeDialogs(directory=DATA)
    harness = await start_with(start_settings, locations(), dialogs)
    section = section_of(harness)
    section.change_data_dir()
    assert dialogs.options == [] and harness.recorded.of(ChangeDataDir) == []
    await harness.publish(locations(pending_data_dir="D:\\LST"))
    section.change_data_dir()
    await harness.flush()
    assert dialogs.options == []  # 撤销不需要再确认
    assert harness.recorded.of(ChangeDataDir) == [ChangeDataDir(DATA, False)]


async def test_pending_change_offers_restart(start_settings: StartSettings) -> None:
    harness = await start_with(start_settings, locations(pending_data_dir="D:\\LST data"))
    data = section_of(harness).data_card
    assert data.status_label.text() == "重启程序后改用：D:\\LST data"
    assert not data.restart_button.isHidden() and data.restart_button.text() == "立即重启(&R)"
    data.restart_button.click()
    await harness.flush()
    assert harness.recorded.of(RestartApp) == [RestartApp()]


@pytest.mark.parametrize(
    ("source", "reason"),
    [(DataDirSource.ARGUMENT, "--data-dir"), (DataDirSource.ENVIRONMENT, "LST_DATA_DIR")],
)
async def test_data_dir_set_on_the_command_line_cannot_be_changed(
    start_settings: StartSettings, source: DataDirSource, reason: str
) -> None:
    harness = await start_with(start_settings, locations(data_source=source))
    section = section_of(harness)
    assert not section.data_card.change_button.isEnabled()
    assert reason in section.data_card.status_label.text()
    section.change_data_dir()
    assert harness.dialogs.directories == []
    assert section.models_card.change_button.isEnabled()  # 模型目录照常可以改


async def test_change_and_reset_models_dir(start_settings: StartSettings, tmp_path: Path) -> None:
    dialogs = FakeDialogs(directory=str(tmp_path / "big"))
    harness = await start_with(start_settings, locations(), dialogs)
    models = section_of(harness).models_card
    models.change_button.click()
    await harness.flush()
    [(title, text, option, checked)] = dialogs.options
    assert title == "模型目录" and option == "把现有模型移动到新目录" and checked
    assert "进行中的下载会被取消" in text
    assert harness.recorded.of(ChangeModelsDir) == [ChangeModelsDir(str(tmp_path / "big"), True)]

    await harness.publish(locations(models_dir=str(tmp_path / "big"), models_default=False))
    assert not models.reset_button.isHidden() and models.status_label.isHidden()
    dialogs.option_answer = False
    models.reset_button.click()
    await harness.flush()
    assert MODELS in dialogs.options[-1][1]
    assert harness.recorded.of(ChangeModelsDir)[-1] == ChangeModelsDir("", False)


async def test_open_buttons_hand_the_paths_to_explorer(start_settings: StartSettings) -> None:
    harness = await start_with(start_settings, locations())
    section = section_of(harness)
    section.data_card.open_button.click()
    section.models_card.open_button.click()
    assert harness.dialogs.opened == [DATA, MODELS]


async def test_progress_disables_both_change_buttons(start_settings: StartSettings) -> None:
    task = StorageProgress(StorageKind.MODELS, "E:\\models", 512 * 1024 * 1024, 2 * 1024**3)
    harness = await start_with(start_settings, locations(task=task))
    section = section_of(harness)
    models = section.models_card
    assert not models.progress_bar.isHidden() and models.progress_bar.value() == 250
    assert models.status_label.text() == "正在移动模型… 已完成 512.0 MB / 2.0 GB"
    assert not models.change_button.isEnabled() and not section.data_card.change_button.isEnabled()
    await harness.publish(locations(task=task, pending_data_dir="D:\\x"))
    assert not section.data_card.restart_button.isHidden()
    assert not section.data_card.restart_button.isEnabled()  # 模型还在移动：不能重启
    assert section.data_card.progress_bar.isHidden()

    counting = StorageProgress(StorageKind.DATA, "D:\\x")  # 还在统计大小：忙碌动画
    await harness.publish(locations(task=counting))
    assert section.data_card.progress_bar.maximum() == 0
    assert section.data_card.status_label.text() == "正在复制数据…"


async def test_failures_are_shown_under_the_card_they_belong_to(start_settings: StartSettings) -> None:
    failure = StorageFailure(StorageKind.DATA, StorageErrorCode.NOT_WRITABLE, "D:\\x", "拒绝访问。（5）")
    harness = await start_with(start_settings, locations(failure=failure))
    data, models = section_of(harness).cards
    assert "新目录无法写入（拒绝访问。（5））" in data.error_text
    assert models.error_text == ""
    moved = StorageFailure(StorageKind.MODELS, StorageErrorCode.MOVE_FAILED, "E:\\m", "磁盘已满（112）")
    await harness.publish(locations(failure=moved))
    assert data.error_text == "" and "模型已经移回原处" in models.error_text


async def test_nmt_model_dir_placeholder_is_the_real_location(start_settings: StartSettings) -> None:
    harness = await start_with(start_settings, locations(models_dir="E:\\LST models", models_default=False))
    page = harness.window.page("models.nmt")
    assert isinstance(page, NmtModelsPage) and page.model_dir is not None
    editor = page.model_dir.editor
    assert isinstance(editor, DirectoryEditor)
    assert editor.edit.placeholderText() == os.path.join("E:\\LST models", "nmt")
    assert page.model_dir.card.title_label.text() == "本地翻译模型的单独目录"
    await harness.publish(default_config({"ui": {"language": "en"}}, revision=2))
    assert editor.edit.placeholderText() == os.path.join("E:\\LST models", "nmt")  # 换语言后仍是实际位置


def test_texts_have_both_languages_with_the_same_placeholders() -> None:
    for key, texts in CATALOG.items():
        assert set(texts) == set(i18n.SUPPORTED), key
        zh, en = texts["zh-Hans"], texts["en"]
        assert zh and en, key
        assert set(_PLACEHOLDER.findall(zh)) == set(_PLACEHOLDER.findall(en)), key
    for code in StorageErrorCode:
        assert f"settings.storage.error.{code.value}" in CATALOG, code
    for source in (DataDirSource.ARGUMENT, DataDirSource.ENVIRONMENT):
        assert f"settings.storage.data.fixed.{source.value}" in CATALOG


async def test_english_texts(start_settings: StartSettings) -> None:
    harness = await start_with(start_settings, locations(pending_data_dir="D:\\x"))
    await harness.publish(default_config({"ui": {"language": "en"}}, revision=2))
    data, models = section_of(harness).cards
    assert (data.title_label.text(), models.title_label.text()) == ("Data folder", "Model folder")
    assert data.change_button.text() == "C&hange…" and data.restart_button.text() == "&Restart now"
    assert data.status_label.text() == "After a restart the program uses: D:\\x"


# ---------------------------------------------------------------- 截图（手动开启）


@pytest.mark.skipif(not os.environ.get("LST_SETTINGS_SHOTS"), reason="只在需要截图时运行")
async def test_storage_screenshots(start_settings: StartSettings, qapp: QApplication) -> None:
    """常规页在浅色、深色下的截图：数据目录等待重启，模型目录正在移动。"""
    from PySide6.QtGui import QImage, QPainter

    from lst.ui.theme import apply_app_style, theme_colors

    target = Path(__file__).resolve().parents[3] / "data" / "tmp" / "settings-shots"
    target.mkdir(parents=True, exist_ok=True)
    state = locations(
        pending_data_dir="D:\\LSTtranslate\\data",
        models_dir="E:\\LSTtranslate\\models",
        models_default=False,
        task=StorageProgress(StorageKind.MODELS, "E:\\LSTtranslate\\models", 1288490188, 3221225472),
    )
    try:
        for scheme in ("light", "dark"):
            apply_app_style(qapp, scheme)
            harness = await start_with(start_settings, state)
            window = await harness.open()
            if on_native_platform():
                window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            window.resize(1240, 900)
            window.show_page("general")
            QApplication.processEvents()
            grabbed = window.grab()
            image = QImage(grabbed.size(), QImage.Format.Format_ARGB32)
            image.setDevicePixelRatio(grabbed.devicePixelRatio())
            image.fill(theme_colors().window)
            painter = QPainter(image)
            painter.drawPixmap(0, 0, grabbed)
            painter.end()
            image.save(str(target / f"storage-general-{scheme}.png"))
            await harness.stop()
    finally:
        apply_app_style(qapp, "system")
