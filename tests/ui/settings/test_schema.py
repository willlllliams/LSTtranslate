"""schema 的测试：从真实的 pydantic 模型读出设置项的类型、取值范围和默认值，以及校验错误的说明。"""

import pytest
from pydantic import ValidationError

from lst.contracts.worker_protocol import JsonValue
from lst.ui import i18n
from lst.ui.settings.schema import (
    FieldSpec,
    Kind,
    describe_error,
    describe_validation,
    field_spec,
    model_field_specs,
    nested_patch,
    read_path,
    read_value,
)
from tests.ui.settings.support import all_models, default_config, engine_kinds


def spec(section: str, *path: str) -> FieldSpec:
    return field_spec(all_models()[section], path)


def test_choice_from_literal_type_alias() -> None:
    # [ui].theme 的类型是用 type 语句定义的别名，要先展开才能认出是 Literal。
    theme = spec("ui", "theme")
    assert theme.kind is Kind.CHOICE
    assert theme.choices == ("system", "light", "dark")
    assert theme.default == "system"
    assert not theme.required


def test_choice_from_enum_uses_member_values() -> None:
    mode = spec("ocr", "mode")
    assert (mode.kind, mode.choices, mode.default) == (Kind.CHOICE, ("structure", "text"), "structure")
    assert spec("ocr", "device").choices == ("auto", "cpu", "gpu")


def test_integer_range_and_default_come_from_the_model() -> None:
    debounce = spec("bilingual", "debounce_ms")
    assert (debounce.kind, debounce.minimum, debounce.maximum, debounce.default) == (Kind.INT, 100, 5000, 400)
    resize = spec("ocr", "resize_max_side")
    assert (resize.minimum, resize.maximum, resize.default) == (0, 16384, 4000)


def test_nested_model_path() -> None:
    marker = spec("race", "show_source_marker", "overlay")
    assert (marker.kind, marker.default) == (Kind.BOOL, False)


def test_exclusive_float_minimum() -> None:
    timeout = spec("ocr", "text_timeout_s")
    assert timeout.kind is Kind.FLOAT
    assert timeout.minimum == 0 and timeout.exclusive_minimum
    assert timeout.maximum == 3600


def test_new_type_and_plain_text_fields() -> None:
    assert spec("screenshot", "target_lang").kind is Kind.TEXT
    assert spec("ocr", "language").kind is Kind.TEXT
    assert spec("hotkeys", "capture").default == "Alt+Q"


def test_unknown_field_and_bad_nesting() -> None:
    with pytest.raises(KeyError, match="没有字段"):
        spec("bilingual", "nope")
    with pytest.raises(KeyError, match="嵌套"):
        spec("bilingual", "layout", "deeper")
    with pytest.raises(KeyError):
        field_spec(all_models()["bilingual"], ())


def test_engine_model_fields() -> None:
    specs = model_field_specs(engine_kinds()["openai_compat"])
    assert specs["timeout_s"].required and specs["timeout_s"].kind is Kind.FLOAT
    assert specs["base_url"].required and specs["base_url"].kind is Kind.TEXT
    assert specs["provider"].kind is Kind.CHOICE
    assert specs["provider"].choices == ("deepseek", "openai", "ollama", "llamacpp", "generic")
    assert specs["first_token_timeout_s"].kind is Kind.OPTIONAL_FLOAT
    assert specs["languages"].kind is Kind.TEXT_LIST
    assert specs["system_prompt"].kind is Kind.OPTIONAL_TEXT
    assert (specs["temperature"].minimum, specs["temperature"].maximum) == (0, 2)
    assert (specs["max_batch_segments"].minimum, specs["max_batch_segments"].maximum) == (1, 200)
    assert specs["name"].min_length == 1
    assert specs["enabled"].kind is Kind.BOOL and specs["enabled"].default is True


def test_every_registered_engine_field_has_a_generic_control() -> None:
    """新增引擎类别时界面不需要改：字段类型都能映射到通用控件（OTHER 只允许出现在界面隐藏的字段之外没有）。"""
    for kind, model in engine_kinds().items():
        for name, field in model_field_specs(model).items():
            assert field.kind is not Kind.OTHER, (kind, name)


def test_read_value_falls_back_to_the_default() -> None:
    config = default_config({"bilingual": {"debounce_ms": 700}})
    debounce = spec("bilingual", "debounce_ms")
    assert read_value(config, "bilingual", ("debounce_ms",), debounce) == 700
    assert read_value(None, "bilingual", ("debounce_ms",), debounce) == 400
    assert read_path(config, "race", ("show_source_marker", "overlay")) is False
    assert read_path(config, "bilingual", ("nope", "deeper")) is None


def test_optional_fields_read_none_not_the_default() -> None:
    first_token = field_spec(engine_kinds()["openai_compat"], ("first_token_timeout_s",))
    assert read_value(None, "engines", ("first_token_timeout_s",), first_token) is None


def test_nested_patch() -> None:
    value: JsonValue = True
    assert nested_patch(("show_source_marker", "overlay"), value) == {"show_source_marker": {"overlay": True}}
    assert nested_patch(("layout",), "horizontal") == {"layout": "horizontal"}


def errors_of(kind: str, **fields: object) -> dict[str, str]:
    base = {
        "id": "x",
        "name": "x",
        "priority": 1,
        "timeout_s": 60,
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "m",
    }
    with pytest.raises(ValidationError) as info:
        engine_kinds()[kind].model_validate({**base, **fields})
    return describe_validation(info.value.errors(include_url=False, include_input=False))


def test_validation_messages_are_user_text_in_both_languages() -> None:
    i18n.set_language("zh-Hans")
    assert errors_of("openai_compat", max_concurrency=0) == {"max_concurrency": "不能小于 1"}
    assert errors_of("openai_compat", max_concurrency=99) == {"max_concurrency": "不能大于 32"}
    assert errors_of("openai_compat", name="") == {"name": "不能为空"}
    assert errors_of("openai_compat", timeout_s=0) == {"timeout_s": "必须大于 0"}
    assert errors_of("openai_compat", timeout_s="abc") == {"timeout_s": "应为数字"}
    assert errors_of("openai_compat", structured_output="x") == {"structured_output": "取值不在可选范围内"}
    assert "http" in errors_of("openai_compat", base_url="ftp://h")["base_url"]
    i18n.set_language("en")
    assert errors_of("openai_compat", max_concurrency=0) == {"max_concurrency": "Must not be less than 1"}


def test_validation_messages_never_contain_the_input_value() -> None:
    errors = errors_of("openai_compat", base_url="https://user:hunter2-password@host.example/v1")
    assert "hunter2" not in " ".join(errors.values())


def test_missing_field_is_reported_as_required() -> None:
    with pytest.raises(ValidationError) as info:
        engine_kinds()["baidu"].model_validate({"id": "b", "name": "b", "priority": 1})
    messages = describe_validation(info.value.errors(include_url=False, include_input=False))
    assert messages == {"appid": "必须填写"}


def test_describe_error_unknown_type_is_generic() -> None:
    error = {"type": "weird", "loc": ("x",), "msg": "m", "input": 1}
    assert describe_error(error) == "取值无效"  # pyright: ignore[reportArgumentType]
