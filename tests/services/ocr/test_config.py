"""[ocr] 配置节的测试：默认值、校验、处理时限分档、请求 options 与重载判断。"""

import json

import pytest
from pydantic import ValidationError

from lst.services.ocr import (
    CONFIG_SECTIONS,
    OcrConfig,
    OcrDevice,
    OcrMode,
    needs_reload,
    processing_timeout,
    request_options,
    worker_settings,
)


def test_defaults() -> None:
    config = OcrConfig()
    assert config.mode is OcrMode.STRUCTURE
    assert config.device is OcrDevice.AUTO
    assert config.language == "auto"
    assert (config.table, config.formula, config.chart) == (True, False, False)
    assert (config.doc_orientation, config.doc_unwarping, config.textline_orientation) == (
        False,
        False,
        False,
    )
    assert (config.grayscale, config.resize_max_side) == (False, 4000)


def test_config_sections_register_the_ocr_section() -> None:
    assert set(CONFIG_SECTIONS) == {"ocr"}
    assert CONFIG_SECTIONS["ocr"] is OcrConfig


def test_parses_a_toml_like_section() -> None:
    config = OcrConfig.model_validate({"mode": "text", "device": "gpu", "table": False, "grayscale": True})
    assert (config.mode, config.device, config.table, config.grayscale) == (
        OcrMode.TEXT,
        OcrDevice.GPU,
        False,
        True,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("auto", "auto"), ("AUTO", "auto"), ("zh-CN", "zh-Hans"), ("EN", "en"), ("ko", "ko")],
)
def test_language_is_normalised(raw: str, expected: str) -> None:
    assert OcrConfig(language=raw).language == expected


@pytest.mark.parametrize(
    "raw",
    [
        {"language": "not a language"},
        {"language": ""},
        {"mode": "layout"},
        {"device": "tpu"},
        {"resize_max_side": -1},
        {"resize_max_side": 100},
        {"resize_max_side": 20000},
        {"text_timeout_s": 0},
        {"unknown": True},
    ],
)
def test_invalid_values_are_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OcrConfig.model_validate(raw)


@pytest.mark.parametrize("value", [0, 256, 4000, 16384])
def test_resize_limits(value: int) -> None:
    assert OcrConfig(resize_max_side=value).resize_max_side == value


@pytest.mark.parametrize(
    ("fields", "seconds"),
    [
        ({"mode": "text"}, 30),
        ({"mode": "text", "table": True, "formula": True, "chart": True}, 30),
        ({}, 120),  # 版面模式，默认只开表格
        ({"table": False}, 60),
        ({"formula": True}, 180),
        ({"table": True, "formula": True, "chart": True}, 240),
        ({"doc_orientation": True, "doc_unwarping": True, "textline_orientation": True, "table": False}, 60),
        ({"structure_timeout_s": 90, "feature_timeout_s": 30, "chart": True}, 150),
        ({"mode": "text", "text_timeout_s": 12.5}, 12.5),
    ],
)
def test_processing_timeout_by_mode_and_features(fields: dict[str, object], seconds: float) -> None:
    assert processing_timeout(OcrConfig.model_validate(fields)) == seconds


def test_request_options_carry_recognition_and_preprocessing_fields() -> None:
    config = OcrConfig.model_validate(
        {
            "mode": "text",
            "language": "ja",
            "formula": True,
            "grayscale": True,
            "resize_max_side": 2000,
            "device": "cpu",
        }
    )
    options = request_options(config)
    assert options == {
        "mode": "text",
        "language": "ja",
        "table": True,
        "formula": True,
        "chart": False,
        "doc_orientation": False,
        "doc_unwarping": False,
        "textline_orientation": False,
        "grayscale": True,
        "resize_max_side": 2000,
    }
    json.dumps(options)  # 必须能原样放进 JSON 消息


def test_worker_settings_add_the_device() -> None:
    config = OcrConfig(device=OcrDevice.GPU)
    assert worker_settings(config) == {**request_options(config), "device": "gpu"}


@pytest.mark.parametrize(
    ("change", "reload"),
    [
        ({"mode": "text"}, True),
        ({"device": "cpu"}, True),
        ({"language": "ko"}, True),
        ({"table": False}, False),
        ({"chart": True}, False),
        ({"grayscale": True}, False),
        ({"resize_max_side": 0}, False),
        ({"text_timeout_s": 5}, False),
    ],
)
def test_reload_only_for_process_level_fields(change: dict[str, object], reload: bool) -> None:
    assert needs_reload(OcrConfig(), OcrConfig.model_validate(change)) is reload
