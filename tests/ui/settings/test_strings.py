"""设置窗口文案表的测试：每条文案都有中英文，占位符一致，格式化函数的输出。"""

import re

from lst.ui import i18n
from lst.ui.common.text import format_size
from lst.ui.settings.strings import CATALOG, format_timestamp

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_every_settings_text_has_both_languages_with_the_same_placeholders() -> None:
    for key, texts in CATALOG.items():
        assert set(texts) == set(i18n.SUPPORTED), key
        assert all(text for text in texts.values()), key
        zh, en = texts["zh-Hans"], texts["en"]
        assert set(_PLACEHOLDER.findall(zh)) == set(_PLACEHOLDER.findall(en)), key


def test_new_sections_are_registered() -> None:
    prefixes = ("settings.ocr_engine.", "settings.update.", "settings.f.updates.", "settings.ef.send_image.")
    for prefix in prefixes:
        assert any(key.startswith(prefix) for key in CATALOG), prefix
    for key in (
        "settings.ef.image_max_side.title",
        "settings.ocr.paddle_only",
        "settings.ocr_engine.no_language.help",
        "settings.suffix.hours",
    ):
        assert key in CATALOG, key


def test_format_size_uses_gigabytes_for_large_downloads() -> None:
    assert format_size(1_288_490_188) == "1.2 GB"
    assert format_size(90 * 1024 * 1024) == "90.0 MB"


def test_format_timestamp_converts_utc_and_keeps_unparsable_text() -> None:
    assert format_timestamp(None) == ""
    assert format_timestamp("not a time") == "not a time"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", format_timestamp("2026-10-20T08:00:00Z", with_time=False))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", format_timestamp("2026-10-20T08:00:00"))
