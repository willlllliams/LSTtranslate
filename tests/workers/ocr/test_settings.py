"""Configure 设置、请求 options 与识别语言的校验。"""

import logging
from pathlib import Path

import pytest

from lst.contracts.worker_protocol import JsonValue
from lst.workers._runtime import ConfigError
from lst.workers.ocr_worker.languages import recognition_model
from lst.workers.ocr_worker.settings import (
    DEFAULT_MODEL_CACHE_DIR,
    BadOptions,
    OcrSettings,
    RequestOptions,
)

# 主进程 lst/services/ocr/config.py 的 worker_settings 在默认配置下给出的内容。
MAIN_DEFAULTS: dict[str, JsonValue] = {
    "mode": "structure",
    "language": "auto",
    "table": True,
    "formula": False,
    "chart": False,
    "doc_orientation": False,
    "doc_unwarping": False,
    "textline_orientation": False,
    "grayscale": False,
    "resize_max_side": 4000,
    "device": "auto",
}


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADDLE_PDX_CACHE_HOME", raising=False)
    settings = OcrSettings.parse({})
    assert settings.mode == "structure"
    assert settings.device == "auto"
    assert settings.language == "auto"
    assert settings.features == frozenset({"table"})
    assert (settings.grayscale, settings.resize_max_side) == (False, 0)
    assert settings.model_cache_dir == DEFAULT_MODEL_CACHE_DIR.absolute()
    assert (settings.layout_model, settings.det_model, settings.rec_model) == (
        "PP-DocLayout_plus-L",
        "PP-OCRv5_mobile_det",
        "PP-OCRv5_mobile_rec",
    )
    assert (settings.cpu_threads, settings.warmup) == (0, True)


def test_main_process_settings_are_accepted(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        settings = OcrSettings.parse(MAIN_DEFAULTS)
    assert not caplog.records, "主进程给出的每个键都应被识别"
    assert settings.features == frozenset({"table"})
    assert settings.resize_max_side == 4000


def test_unknown_keys_are_warned(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        OcrSettings.parse({"no_such_key": 1})
    assert any("no_such_key" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "raw",
    [
        {"mode": "layout"},
        {"device": "cuda"},
        {"table": "yes"},
        {"grayscale": 1},
        {"resize_max_side": -1},
        {"resize_max_side": 1.5},
        {"resize_max_side": True},
        {"cpu_threads": -2},
        {"warmup": None},
        {"language": ""},
        {"language": "xx"},
        {"rec_model": ""},
        {"layout_model": 3},
        {"model_cache_dir": ""},
    ],
)
def test_invalid_settings(raw: dict[str, JsonValue]) -> None:
    with pytest.raises(ConfigError):
        OcrSettings.parse(raw)


def test_text_mode_drops_structure_only_features() -> None:
    settings = OcrSettings.parse(
        {"mode": "text", "table": True, "formula": True, "textline_orientation": True}
    )
    assert settings.features == frozenset({"textline_orientation"})


def test_model_cache_dir_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADDLE_PDX_CACHE_HOME", r"C:\from-env")
    assert OcrSettings.parse({}).model_cache_dir == Path(r"C:\from-env")
    assert OcrSettings.parse({"model_cache_dir": r"D:\from-settings"}).model_cache_dir == Path(
        r"D:\from-settings"
    )


# ---------------------------------------------------------------- 识别语言


@pytest.mark.parametrize("language", ["auto", "zh-Hans", "zh-Hant", "en", "ja"])
def test_general_languages_use_general_model(language: str) -> None:
    assert OcrSettings.parse({"language": language}).rec_model == "PP-OCRv5_mobile_rec"


@pytest.mark.parametrize(
    ("language", "model"),
    [
        ("ko", "korean_PP-OCRv5_mobile_rec"),
        ("fr", "latin_PP-OCRv5_mobile_rec"),
        ("de", "latin_PP-OCRv5_mobile_rec"),
        ("ru", "eslav_PP-OCRv5_mobile_rec"),
        ("bg", "cyrillic_PP-OCRv5_mobile_rec"),
        ("ar", "arabic_PP-OCRv5_mobile_rec"),
        ("hi", "devanagari_PP-OCRv5_mobile_rec"),
        ("th", "th_PP-OCRv5_mobile_rec"),
    ],
)
def test_script_specific_models(language: str, model: str) -> None:
    assert recognition_model(language, "PP-OCRv5_mobile_rec") == model


def test_explicit_rec_model_wins() -> None:
    assert (
        OcrSettings.parse({"language": "ko", "rec_model": "PP-OCRv5_server_rec"}).rec_model
        == "PP-OCRv5_server_rec"
    )


# ---------------------------------------------------------------- 请求 options


def test_options_default_to_settings() -> None:
    settings = OcrSettings.parse({**MAIN_DEFAULTS, "grayscale": True, "resize_max_side": 2000})
    options = RequestOptions.parse({}, settings)
    assert options == RequestOptions(frozenset({"table"}), True, 2000)


def test_options_override_settings() -> None:
    settings = OcrSettings.parse(MAIN_DEFAULTS)
    raw: dict[str, JsonValue] = {
        **MAIN_DEFAULTS,
        "table": False,
        "formula": True,
        "grayscale": True,
        "resize_max_side": 0,
    }
    options = RequestOptions.parse(raw, settings)
    assert options == RequestOptions(frozenset({"formula"}), True, 0)


def test_text_mode_ignores_structure_options() -> None:
    settings = OcrSettings.parse({**MAIN_DEFAULTS, "mode": "text"})
    options = RequestOptions.parse(
        {**MAIN_DEFAULTS, "mode": "text", "chart": True, "doc_orientation": True}, settings
    )
    assert options.features == frozenset({"doc_orientation"})


@pytest.mark.parametrize(
    "raw",
    [
        {"table": "true"},
        {"grayscale": 0},
        {"resize_max_side": -5},
        {"resize_max_side": "640"},
        {"formula": None},
    ],
)
def test_bad_options(raw: dict[str, JsonValue]) -> None:
    with pytest.raises(BadOptions):
        RequestOptions.parse(raw, OcrSettings.parse({}))


def test_mode_or_language_mismatch_is_only_warned(caplog: pytest.LogCaptureFixture) -> None:
    settings = OcrSettings.parse(MAIN_DEFAULTS)
    with caplog.at_level(logging.WARNING):
        options = RequestOptions.parse({"mode": "text", "language": "ja"}, settings)
    assert options.features == frozenset({"table"})
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "mode" in messages and "language" in messages
