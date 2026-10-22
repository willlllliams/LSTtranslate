"""设置窗口里的谷歌、微软免费翻译接口：类别名称、“接口地址”卡片的文案、不需要 key、测试连接。

组合根目前没有把这两类登记为不需要 key 的类别（界面默认的 KEYLESS_KINDS 只含 local_nmt 与 dictionary），
这里按设置窗口模块的构造参数 keyless_kinds 传入，验证传入后的界面行为。
"""

import re

from lst.contracts.events import CheckEngineConnection, EngineConnectionChecked
from lst.contracts.ids import EngineId
from lst.ui import i18n
from lst.ui.settings.page_engines import EnginesPage
from lst.ui.settings.strings_free import CATALOG
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import default_config, engine

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
KEYLESS = frozenset({"local_nmt", "dictionary", "google_free", "microsoft_free"})


def test_free_texts_have_both_languages_and_name_the_limits() -> None:
    for key, texts in CATALOG.items():
        assert set(texts) == set(i18n.SUPPORTED), key
        assert all(texts.values()), key
        assert set(_PLACEHOLDER.findall(texts["zh-Hans"])) == set(_PLACEHOLDER.findall(texts["en"])), key
    zh = CATALOG["settings.ef.endpoint.desc"]["zh-Hans"]
    for phrase in (
        "免费接口",
        "没有公开文档",
        "可能变动或限流",
        "谷歌翻译在中国大陆不可用",
        "文本会发送给该服务",
    ):
        assert phrase in zh, phrase
    en = CATALOG["settings.ef.endpoint.desc"]["en"]
    for phrase in ("undocumented", "rate-limited", "mainland China", "is sent to the service"):
        assert phrase in en, phrase


async def open_free(start_settings: StartSettings, select: str) -> tuple[Harness, EnginesPage]:
    config = default_config(
        engines=[
            engine("local_nmt", "argos", 1),
            engine("google_free", "google_free", 2, enabled=False),
            engine("microsoft_free", "microsoft_free", 3, enabled=False),
        ]
    )
    harness = await start_settings(config=config, keyless_kinds=KEYLESS)
    window = await harness.open()
    window.show_page("engines")
    page = window.page("engines")
    assert isinstance(page, EnginesPage)
    page.select(select)
    return harness, page


async def test_editor_shows_the_endpoint_card_and_no_key(start_settings: StartSettings) -> None:
    _, page = await open_free(start_settings, "google_free")
    for engine_id in ("google_free", "microsoft_free"):
        page.select(engine_id)
        cards = {
            b.location[-1]: b.card for b in page.all_bindings() if b.location[:2] == ("engines", engine_id)
        }
        assert {"name", "enabled", "endpoint", "max_batch_segments", "max_batch_chars", "timeout_s"} <= set(
            cards
        )
        assert page.secret_field is None  # 不需要 API key


async def test_endpoint_card_shows_the_free_endpoint_caveats(start_settings: StartSettings) -> None:
    _, page = await open_free(start_settings, "google_free")
    card = next(b.card for b in page.all_bindings() if b.location == ("engines", "google_free", "endpoint"))
    assert card.title_label.text() == "接口地址"
    assert "没有公开文档" in card.desc_label.text() and "中国大陆" in card.desc_label.text()


async def test_free_instances_are_listed_in_priority_order(start_settings: StartSettings) -> None:
    _, page = await open_free(start_settings, "google_free")
    assert page.ids() == ["argos", "google_free", "microsoft_free"]


async def test_add_dialog_hides_the_key_for_free_kinds(start_settings: StartSettings) -> None:
    _, page = await open_free(start_settings, "google_free")
    page.add_button.click()
    dialog = page.add_dialog
    assert dialog is not None
    for kind, name in (("google_free", "谷歌翻译（免费接口）"), ("microsoft_free", "微软翻译（免费接口）")):
        index = dialog.kind_combo.findData(kind)
        assert dialog.kind_combo.itemText(index) == name
        dialog.kind_combo.setCurrentIndex(index)
        assert not dialog.key_edit.isVisibleTo(dialog)
    dialog.cancel_button.click()


async def test_connection_test_works_for_a_free_instance(start_settings: StartSettings) -> None:
    harness, page = await open_free(start_settings, "microsoft_free")
    page.test_button.click()
    await harness.flush()
    assert harness.recorded.of(CheckEngineConnection) == [CheckEngineConnection(EngineId("microsoft_free"))]
    await harness.publish(EngineConnectionChecked(EngineId("microsoft_free"), ok=True, latency_ms=88.0))
    assert page.test_label.text() == "连接正常（88 毫秒）"
