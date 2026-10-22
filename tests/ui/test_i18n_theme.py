"""ui.i18n 与 ui.theme 的测试。"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from lst.contracts.errors import ErrorCode
from lst.contracts.nmt_runtime import NmtRuntimeErrorCode
from lst.contracts.ocr_engines import OcrEngineErrorCode
from lst.contracts.updates import UpdateErrorCode
from lst.ui import i18n
from lst.ui.theme import TOKENS, UiConfig, apply_app_style, engine_color


@pytest.fixture(autouse=True)
def restore_language() -> object:
    previous = i18n.current_language()
    yield
    i18n.set_language(previous)


def test_every_error_code_has_reason_and_suggestion_in_both_languages() -> None:
    """翻译引擎、OCR 引擎、应用更新、显卡运行库四类错误码都有中英文的原因与处理建议，
    且不退回到内部错误的文案。"""
    families = (ErrorCode, OcrEngineErrorCode, UpdateErrorCode, NmtRuntimeErrorCode)
    assert families == i18n.ERROR_CODE_FAMILIES
    for language in i18n.SUPPORTED:
        i18n.set_language(language)
        for family in families:
            for code in family:
                reason, suggestion = i18n.error_text(code)
                assert reason and not reason.startswith("error."), (language, code)
                assert suggestion and not suggestion.startswith("error."), (language, code)
                assert i18n.error_text(code.value) == (reason, suggestion)
        assert i18n.error_text(ErrorCode.IMAGE_UNSUPPORTED) != i18n.error_text(ErrorCode.INTERNAL)


def test_error_text_uses_the_family_default_for_unknown_codes() -> None:
    i18n.set_language("zh-Hans")
    internal = i18n.error_text(OcrEngineErrorCode.INTERNAL)
    assert i18n.error_text(None, OcrEngineErrorCode.INTERNAL) == internal
    assert i18n.error_text("ocr_engine.nonsense", OcrEngineErrorCode.INTERNAL) == internal
    assert i18n.error_text(None, UpdateErrorCode.INTERNAL) == i18n.error_text(UpdateErrorCode.INTERNAL)


def test_unknown_error_code_falls_back_to_internal() -> None:
    i18n.set_language("en")
    assert i18n.error_text("nonsense") == i18n.error_text(ErrorCode.INTERNAL)
    assert i18n.error_text(None) == i18n.error_text(ErrorCode.INTERNAL)


def test_tr_params_fallback_and_missing_key() -> None:
    i18n.register_catalog({"test.greet": {"zh-Hans": "你好，{name}", "en": "Hello, {name}"}})
    i18n.register_catalog({"test.only_en": {"en": "English only"}})
    i18n.set_language("zh-Hans")
    assert i18n.tr("test.greet", name="甲") == "你好，甲"
    assert i18n.tr("test.only_en") == "English only"
    assert i18n.tr("test.no_such_key") == "test.no_such_key"
    assert i18n.tr("test.greet") == "你好，{name}"  # 没给参数时原样返回


def test_set_language_auto_uses_system_language() -> None:
    assert i18n.set_language("auto") == i18n.system_language()
    assert i18n.set_language("en") == "en"


def test_ui_config_defaults_and_validation() -> None:
    assert UiConfig() == UiConfig(language="auto", theme="system")
    with pytest.raises(ValueError):
        UiConfig.model_validate({"theme": "blue"})


def test_engine_color_is_stable_and_respects_configuration(qapp: QApplication) -> None:
    assert engine_color("deepseek") == engine_color("deepseek")
    assert engine_color("deepseek", "#123456").name() == "#123456"
    assert engine_color("deepseek", "not a color") == engine_color("deepseek")


def test_apply_app_style_sets_scheme(qapp: QApplication) -> None:
    apply_app_style(qapp, "dark")
    # offscreen 平台不支持设置深浅色，只在原生平台上检查。
    if QApplication.platformName() != "offscreen":
        assert qapp.styleHints().colorScheme() == Qt.ColorScheme.Dark
    apply_app_style(qapp, "system")
    assert TOKENS.fade_ms == 150
