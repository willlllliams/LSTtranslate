"""竞速模块三节配置（race、screenshot、bilingual）的模型测试。"""

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from lst.services.race import CONFIG_SECTIONS, BilingualConfig, RaceConfig, RaceMode, ScreenshotConfig
from lst.services.race.config import SourceMarkerConfig


def test_defaults_match_architecture_example() -> None:
    """默认值与架构文档第 9 章的配置示例一致。"""
    race = RaceConfig()
    assert race.mode is RaceMode.SAVER
    assert race.show_source_marker == SourceMarkerConfig(overlay=False, bilingual=True)
    assert ScreenshotConfig().target_lang == "zh-Hans"
    bilingual = BilingualConfig()
    assert bilingual.pair == ("zh-Hans", "en")
    assert bilingual.debounce_ms == 400
    assert bilingual.auto_billing is False


def test_config_sections_registry() -> None:
    expected = {"race": RaceConfig, "screenshot": ScreenshotConfig, "bilingual": BilingualConfig}
    assert expected == CONFIG_SECTIONS


def test_parse_toml_like_values() -> None:
    race = RaceConfig.model_validate(
        {"mode": "full", "show_source_marker": {"overlay": True, "bilingual": False}}
    )
    assert race.mode is RaceMode.FULL
    assert race.show_source_marker.overlay is True
    assert race.show_source_marker.bilingual is False
    # 内联表只写一部分字段时，其余字段取默认值
    assert RaceConfig.model_validate({"show_source_marker": {"overlay": True}}).show_source_marker.bilingual


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("zh", "zh-Hans"),
        ("zh-CN", "zh-Hans"),
        ("zh-hans", "zh-Hans"),
        ("zh-TW", "zh-Hant"),
        ("zh-HK", "zh-Hant"),
        ("en-US", "en"),
        ("JA", "ja"),
    ],
)
def test_language_codes_are_normalized(raw: str, expected: str) -> None:
    assert ScreenshotConfig.model_validate({"target_lang": raw}).target_lang == expected


def test_pair_is_normalized() -> None:
    config = BilingualConfig.model_validate({"pair": ["zh-CN", "en-GB"]})
    assert config.pair == ("zh-Hans", "en")


@pytest.mark.parametrize(
    ("model", "raw", "location"),
    [
        (ScreenshotConfig, {"target_lang": "chinese"}, ("target_lang",)),
        (ScreenshotConfig, {"target_lang": "auto"}, ("target_lang",)),
        (ScreenshotConfig, {"target_lang": 7}, ("target_lang",)),
        (BilingualConfig, {"pair": ["zh", "english"]}, ("pair", 1)),
        (BilingualConfig, {"pair": ["zh", "zh-CN"]}, ("pair",)),  # 规范化后相同
        (BilingualConfig, {"pair": ["en", "en-US"]}, ("pair",)),
        (BilingualConfig, {"pair": ["zh-Hans"]}, ("pair", 1)),
        (BilingualConfig, {"pair": ["zh-Hans", "en", "ja"]}, ("pair",)),
        (BilingualConfig, {"debounce_ms": 0}, ("debounce_ms",)),
        (BilingualConfig, {"debounce_ms": 60_000}, ("debounce_ms",)),
        (BilingualConfig, {"auto_billing": "sometimes"}, ("auto_billing",)),
        (RaceConfig, {"mode": "turbo"}, ("mode",)),
        (RaceConfig, {"show_source_marker": {"tray": True}}, ("show_source_marker", "tray")),
        (RaceConfig, {"speed": 1}, ("speed",)),
    ],
)
def test_invalid_values_report_field_location(
    model: type[BaseModel], raw: dict[str, Any], location: tuple[str | int, ...]
) -> None:
    """错误位置精确到字段，配置服务据此只回退出错的字段（CFG-02）。"""
    with pytest.raises(ValidationError) as info:
        model.model_validate(raw)
    assert location in [error["loc"] for error in info.value.errors()]


def test_pair_error_message() -> None:
    with pytest.raises(ValidationError, match="互译对的两个语言必须不同"):
        BilingualConfig.model_validate({"pair": ["zh", "zh-CN"]})


def test_debounce_range_bounds() -> None:
    assert BilingualConfig(debounce_ms=100).debounce_ms == 100
    assert BilingualConfig(debounce_ms=5000).debounce_ms == 5000


@pytest.mark.parametrize("model", [RaceConfig, ScreenshotConfig, BilingualConfig, SourceMarkerConfig])
def test_models_are_frozen(model: type[BaseModel]) -> None:
    instance = model()
    field = next(iter(type(instance).model_fields))
    with pytest.raises(ValidationError):
        setattr(instance, field, getattr(instance, field))


@pytest.mark.parametrize("model", [RaceConfig, ScreenshotConfig, BilingualConfig])
def test_json_round_trip(model: type[BaseModel]) -> None:
    """配置服务发布的是 model_dump(mode="json") 的结果，竞速模块再用同一个模型解析回来。"""
    instance = model()
    dumped = instance.model_dump(mode="json")
    assert model.model_validate(dumped) == instance


def test_dump_uses_canonical_codes() -> None:
    config = BilingualConfig.model_validate({"pair": ["ja-JP", "zh_TW"]})
    assert config.model_dump(mode="json") == {
        "pair": ["ja", "zh-Hant"],
        "debounce_ms": 400,
        "auto_billing": False,
        "layout": "vertical",
    }
