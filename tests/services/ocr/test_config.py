"""[ocr] 配置节的测试：默认值、校验、OCR 引擎与实际生效的模式、处理时限分档、请求 options
与决定重载的 process_key。"""

import json

import pytest
from pydantic import ValidationError

from lst.contracts.ocr_engines import OcrEngineKind
from lst.services.ocr import (
    CONFIG_SECTIONS,
    OcrConfig,
    OcrDevice,
    OcrMode,
    effective_mode,
    process_key,
    processing_timeout,
    request_options,
    worker_settings,
)


def test_defaults() -> None:
    config = OcrConfig()
    assert config.engine is OcrEngineKind.WINDOWS
    assert config.mode is OcrMode.STRUCTURE
    assert effective_mode(config) is OcrMode.TEXT
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
    config = OcrConfig.model_validate(
        {"engine": "paddle", "mode": "text", "device": "gpu", "table": False, "grayscale": True}
    )
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
        {"engine": "tesseract"},
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
    ("raw", "mode"),
    [
        ({}, OcrMode.TEXT),
        ({"mode": "structure"}, OcrMode.TEXT),
        ({"engine": "windows", "mode": "text"}, OcrMode.TEXT),
        ({"engine": "paddle"}, OcrMode.STRUCTURE),
        ({"engine": "paddle", "mode": "text"}, OcrMode.TEXT),
    ],
)
def test_windows_engine_always_runs_in_text_mode(raw: dict[str, object], mode: OcrMode) -> None:
    config = OcrConfig.model_validate(raw)
    assert effective_mode(config) is mode
    assert request_options(config)["mode"] == mode.value


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
    """PaddleOCR：时限按模式和子能力分档。"""
    assert processing_timeout(OcrConfig.model_validate({"engine": "paddle", **fields})) == seconds


@pytest.mark.parametrize(
    ("fields", "seconds"),
    [
        ({}, 30),
        ({"mode": "structure", "table": True, "formula": True, "chart": True}, 30),
        ({"text_timeout_s": 12.5, "structure_timeout_s": 90}, 12.5),
    ],
)
def test_windows_engine_uses_the_text_timeout(fields: dict[str, object], seconds: float) -> None:
    assert processing_timeout(OcrConfig.model_validate({"engine": "windows", **fields})) == seconds


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


def test_worker_settings_add_the_device_and_engine() -> None:
    config = OcrConfig(engine=OcrEngineKind.PADDLE, device=OcrDevice.GPU)
    assert worker_settings(config) == {**request_options(config), "device": "gpu", "engine": "paddle"}
    assert worker_settings(config)["mode"] == "structure"


def test_worker_settings_for_windows_force_text_mode() -> None:
    settings = worker_settings(OcrConfig(mode=OcrMode.STRUCTURE))
    assert (settings["engine"], settings["mode"]) == ("windows", "text")
    json.dumps(settings)


@pytest.mark.parametrize(
    ("change", "reload"),
    [
        ({"engine": "windows"}, True),
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
    """PaddleOCR：引擎、识别模式、推理设备、识别语言变化时重载。"""
    base = OcrConfig(engine=OcrEngineKind.PADDLE)
    changed = OcrConfig.model_validate({"engine": "paddle", **change})
    assert (process_key(base) != process_key(changed)) is reload


@pytest.mark.parametrize(
    ("change", "reload"),
    [
        ({"engine": "paddle"}, True),
        ({"language": "en"}, True),
        ({"mode": "text"}, False),
        ({"device": "gpu"}, False),
        ({"table": False}, False),
        ({"grayscale": True}, False),
    ],
)
def test_windows_engine_reloads_only_for_engine_and_language(change: dict[str, object], reload: bool) -> None:
    """Windows OCR 不看识别模式和推理设备，改它们不重载。"""
    assert (process_key(OcrConfig()) != process_key(OcrConfig.model_validate(change))) is reload
