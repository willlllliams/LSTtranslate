"""从 pydantic 模型读出设置项的类型、取值范围和默认值（需求 UI-06、架构文档 11.7 节）。

界面模块不能 import ``lst.services``（架构文档 4.2 节），各节配置的 pydantic 模型由组合根注入。
本模块把模型字段翻译成界面要用的 ``FieldSpec``：控件种类、数值范围、可选项和默认值。
这样范围与默认值只在模型里写一遍，界面不再重复，模型改了界面自动跟着变。

本模块不依赖 Qt，可以在没有 QApplication 的测试里使用。
"""

import types
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any, Final, Literal, cast

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import ErrorDetails, PydanticUndefined, to_jsonable_python

from lst.contracts.config import AppConfig
from lst.contracts.worker_protocol import JsonValue
from lst.ui.i18n import tr

type FieldPath = tuple[str, ...]


class Kind(StrEnum):
    """控件种类。"""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    TEXT = "text"
    CHOICE = "choice"
    OPTIONAL_FLOAT = "optional_float"
    OPTIONAL_TEXT = "optional_text"
    TEXT_LIST = "text_list"
    """用逗号分隔的文字列表，空表示 None（例如引擎的语言白名单）。"""
    OTHER = "other"
    """界面没有通用控件的类型，由页面自己决定怎么处理。"""


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """一个设置项的界面描述。

    - minimum、maximum：数值的闭区间；模型里写的是严格不等式（gt、lt）时，整数换算成闭区间，
      小数取开区间的边界并在 ``exclusive_minimum`` 里标出；没有限制时为 None；
    - choices：CHOICE 的可选值（规范 JSON 值，即枚举的 value 或 Literal 的取值）；
    - default：模型里的默认值（规范 JSON）；``required`` 为真时没有默认值，default 为 None；
    - min_length：文字的最小长度，大于 0 表示不能为空；
    - title、description：模型里写的标题与说明（中文），界面没有登记文案时用它们；
    - advanced：字段的 json_schema_extra 带 ``{"advanced": True}``，放进“高级”折叠区；
    - reload：带 ``{"reload": True}``，修改后识别组件会重新加载，说明里注明；
    - ocr_stage：带 ``{"ocr_stage": "<环节>"}``，是 OCR 流水线某个环节的选模型字段，取值为环节名。
    """

    name: str
    kind: Kind
    default: JsonValue = None
    required: bool = False
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    choices: tuple[str, ...] = ()
    min_length: int = 0
    description: str = ""
    title: str = ""
    advanced: bool = False
    reload: bool = False
    ocr_stage: str = ""


# ---------------------------------------------------------------- 模型 → FieldSpec


def _unwrap(annotation: Any) -> Any:
    """去掉 NewType 与 Annotated 的包装。"""
    while True:
        if isinstance(annotation, typing.TypeAliasType):
            annotation = annotation.__value__
        elif isinstance(annotation, typing.NewType):
            annotation = annotation.__supertype__
        elif typing.get_origin(annotation) is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
        else:
            return annotation


def _is_union(origin: Any) -> bool:
    return origin is typing.Union or origin is types.UnionType


def classify(annotation: Any) -> tuple[Kind, tuple[str, ...]]:
    """字段类型标注 → (控件种类, 可选值)。不认识的类型归入 Kind.OTHER。"""
    ann = _unwrap(annotation)
    origin = typing.get_origin(ann)
    if ann is bool:
        return Kind.BOOL, ()
    if ann is int:
        return Kind.INT, ()
    if ann is float:
        return Kind.FLOAT, ()
    if ann is str:
        return Kind.TEXT, ()
    if isinstance(ann, type) and issubclass(ann, Enum):
        return Kind.CHOICE, tuple(str(member.value) for member in ann)
    if origin is Literal:
        values = typing.get_args(ann)
        if all(isinstance(value, str) for value in values):
            return Kind.CHOICE, tuple(cast(str, value) for value in values)
        return Kind.OTHER, ()
    if origin is tuple:
        args = typing.get_args(ann)
        if len(args) == 2 and args[1] is Ellipsis and _unwrap(args[0]) is str:
            return Kind.TEXT_LIST, ()
        return Kind.OTHER, ()
    if _is_union(origin):
        members = [arg for arg in typing.get_args(ann) if arg is not type(None)]
        if len(members) == 1 and len(members) < len(typing.get_args(ann)):
            inner, choices = classify(members[0])
            if inner is Kind.FLOAT:
                return Kind.OPTIONAL_FLOAT, ()
            if inner is Kind.TEXT:
                return Kind.OPTIONAL_TEXT, ()
            if inner is Kind.TEXT_LIST:
                return Kind.TEXT_LIST, ()
            return Kind.OTHER, choices
    return Kind.OTHER, ()


def _bounds(field: FieldInfo, kind: Kind) -> tuple[float | None, float | None, bool, int]:
    """约束：(下界, 上界, 下界是否开区间, 最小长度)。"""
    minimum: float | None = None
    maximum: float | None = None
    exclusive = False
    min_length = 0
    integer = kind is Kind.INT
    for item in field.metadata:
        if (ge := getattr(item, "ge", None)) is not None:
            minimum = float(ge)
        if (gt := getattr(item, "gt", None)) is not None:
            minimum = float(gt) + 1 if integer else float(gt)
            exclusive = not integer
        if (le := getattr(item, "le", None)) is not None:
            maximum = float(le)
        if (lt := getattr(item, "lt", None)) is not None:
            maximum = float(lt) - 1 if integer else float(lt)
        if (length := getattr(item, "min_length", None)) is not None:
            min_length = int(length)
    return minimum, maximum, exclusive, min_length


def _default_of(field: FieldInfo) -> JsonValue:
    if field.is_required():
        return None
    default = field.get_default(call_default_factory=True)
    if default is PydanticUndefined:
        return None
    return cast(JsonValue, to_jsonable_python(default))


def _extra_of(field: FieldInfo) -> Mapping[str, object]:
    """字段的 json_schema_extra。写成函数的（生成 JSON Schema 时才调用）不认，按没有处理。"""
    extra = field.json_schema_extra
    return cast(Mapping[str, object], extra) if isinstance(extra, Mapping) else {}


def _spec_of(name: str, field: FieldInfo) -> FieldSpec:
    kind, choices = classify(field.annotation)
    minimum, maximum, exclusive, min_length = _bounds(field, kind)
    extra = _extra_of(field)
    stage = extra.get("ocr_stage")
    return FieldSpec(
        name=name,
        kind=kind,
        default=_default_of(field),
        required=field.is_required(),
        minimum=minimum,
        maximum=maximum,
        exclusive_minimum=exclusive,
        choices=choices,
        min_length=min_length,
        description=field.description or "",
        title=field.title or "",
        advanced=extra.get("advanced") is True,
        reload=extra.get("reload") is True,
        ocr_stage=stage if isinstance(stage, str) else "",
    )


def field_spec(model: type[BaseModel], path: Sequence[str]) -> FieldSpec:
    """按路径取一个字段的描述；路径可以穿过嵌套的模型，例如 ("options", "enabled")。

    字段不存在时抛出 KeyError，说明页面的声明与模型不符。
    """
    current = model
    for index, name in enumerate(path):
        fields = current.model_fields
        if name not in fields:
            raise KeyError(f"{model.__name__} 里没有字段 {'.'.join(path[: index + 1])}")
        field = fields[name]
        if index == len(path) - 1:
            return _spec_of(name, field)
        inner = _unwrap(field.annotation)
        if not (isinstance(inner, type) and issubclass(inner, BaseModel)):
            raise KeyError(f"{model.__name__} 的字段 {'.'.join(path[: index + 1])} 不是嵌套的模型")
        current = inner
    raise KeyError("字段路径不能为空")


def nested_model(model: type[BaseModel], name: str) -> type[BaseModel] | None:
    """字段的类型是嵌套的 pydantic 模型时返回该模型，否则返回 None（字段不存在也返回 None）。"""
    field = model.model_fields.get(name)
    if field is None:
        return None
    inner = _unwrap(field.annotation)
    return inner if isinstance(inner, type) and issubclass(inner, BaseModel) else None


def model_field_specs(model: type[BaseModel]) -> dict[str, FieldSpec]:
    """模型全部字段的描述，按声明顺序。"""
    return {name: _spec_of(name, field) for name, field in model.model_fields.items()}


# ---------------------------------------------------------------- AppConfig 的读取与修改


def _table(value: JsonValue) -> Mapping[str, JsonValue] | None:
    return value if isinstance(value, dict) else None


def read_path(config: AppConfig | None, section: str, path: Sequence[str]) -> JsonValue | None:
    """取配置里的一个值。节或字段不存在时返回 None。"""
    node: JsonValue = config.section(section) if config is not None else None
    for key in path:
        table = _table(node)
        if table is None or key not in table:
            return None
        node = table[key]
    return node


def read_value(config: AppConfig | None, section: str, path: Sequence[str], spec: FieldSpec) -> JsonValue:
    """取配置里的一个值；配置里没有（还没收到配置，或键缺失）时用模型的默认值。"""
    value = read_path(config, section, path)
    if value is None and spec.kind not in (Kind.OPTIONAL_FLOAT, Kind.OPTIONAL_TEXT, Kind.TEXT_LIST):
        return spec.default
    return value


def nested_patch(path: Sequence[str], value: JsonValue) -> dict[str, JsonValue]:
    """路径加值 → 嵌套的修改，例如 (("a", "b"), 1) → {"a": {"b": 1}}。UpdateConfig 按字段合并嵌套的表。"""
    patch: JsonValue = value
    for key in reversed(path):
        patch = {key: patch}
    return cast(dict[str, JsonValue], patch)


# ---------------------------------------------------------------- 校验错误的说明

_LIMIT_KEYS: Final = {
    "greater_than_equal": ("ge", "settings.validation.min"),
    "greater_than": ("gt", "settings.validation.gt"),
    "less_than_equal": ("le", "settings.validation.max"),
    "less_than": ("lt", "settings.validation.lt"),
}


def _number_text(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def describe_error(error: ErrorDetails) -> str:
    """一条 pydantic 校验错误的说明，按当前界面语言。说明里不包含字段的原值（原值可能是机密）。"""
    kind = error["type"]
    context = error.get("ctx") or {}
    if kind in _LIMIT_KEYS:
        key, text_key = _LIMIT_KEYS[kind]
        return tr(text_key, limit=_number_text(context.get(key)))
    if kind in ("string_too_short", "too_short"):
        return tr("settings.validation.empty")
    if kind == "missing":
        return tr("settings.validation.required")
    if kind in ("int_parsing", "float_parsing", "int_from_float", "int_type", "float_type", "bool_parsing"):
        return tr("settings.validation.number")
    if kind in ("literal_error", "enum"):
        return tr("settings.validation.choice")
    if kind in ("string_type", "string_pattern_mismatch"):
        return tr("settings.validation.text")
    if kind == "value_error":
        message = error["msg"]
        return message.removeprefix("Value error, ")
    return tr("settings.validation.other")


def describe_validation(errors: Sequence[ErrorDetails]) -> dict[str, str]:
    """校验错误 → {字段名: 说明}。字段名取错误位置的第一级；同一字段有多条错误时只保留第一条。"""
    result: dict[str, str] = {}
    for error in errors:
        location = error["loc"]
        name = str(location[0]) if location else ""
        result.setdefault(name, describe_error(error))
    return result
