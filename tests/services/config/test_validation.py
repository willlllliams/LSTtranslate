"""校验与回退算法的单元测试和性质测试（架构文档 2.9.1 节）。"""

from typing import cast

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict, Field

from lst.contracts.worker_protocol import JsonValue
from lst.services.config.validation import (
    canonical,
    format_location,
    lookup,
    patch_section,
    scrub,
    validate_section,
)
from tests.services.config.helpers import SECRET_LIKE, RaceSettings


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: int = 1
    y: int = 2


class Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point: Point = Field(default_factory=Point)
    label: str = "a"


class Required(BaseModel):
    model_config = ConfigDict(extra="forbid")

    a: int


class Outer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inner: Inner = Field(default_factory=Inner)
    needs: Required = Field(default_factory=lambda: Required(a=7))
    either: int | list[int] = 0


def test_format_location() -> None:
    assert format_location(("engines", 2, "priority")) == "engines[2].priority"
    assert format_location(("race", "marker", "overlay")) == "race.marker.overlay"
    assert format_location((1,)) == "[1]"
    assert format_location(()) == ""


def test_scrub_replaces_nested_strings_but_not_short_ones() -> None:
    value = {"a": [SECRET_LIKE, "ok"], "b": {"c": "longer-value"}}
    text = f"bad {SECRET_LIKE} / longer-value / ok"
    assert scrub(text, value) == "bad *** / *** / ok"


def test_deep_nested_field_reverts_alone() -> None:
    data, issues = validate_section(Outer, {"inner": {"point": {"x": "bad", "y": 5}, "label": "z"}})

    assert data["inner"] == {"point": {"x": 1, "y": 5}, "label": "z"}
    assert [location for location, _ in issues] == [("inner", "point", "x")]


def test_missing_required_nested_field_reverts_parent() -> None:
    # needs.a 缺失：没有可删的键 a，往上删到 needs，整个 needs 落到默认值。
    data, issues = validate_section(Outer, {"needs": {}})

    assert data["needs"] == {"a": 7}
    assert issues == [(("needs",), "a：缺少必填字段")]


def test_union_errors_are_merged_into_one_report() -> None:
    data, issues = validate_section(Outer, {"either": "text"})

    assert data["either"] == 0
    assert len(issues) == 1
    location, reason = issues[0]
    assert location == ("either",)
    assert "int：" in reason
    assert "；" in reason


def test_patch_reports_nested_location() -> None:
    current = canonical(Outer, {})
    result = patch_section(Outer, current, {"inner": {"point": {"x": "bad", "y": 9}}})

    assert result.issues[0][0] == ("inner", "point", "x")
    assert result.changes == [(("inner", "point", "y"), 9)]
    assert lookup(result.data, ("inner", "point")) == {"x": 1, "y": 9}


def test_patch_replaces_non_dict_values_whole() -> None:
    current = canonical(Outer, {})
    result = patch_section(Outer, current, {"either": [1, 2]})

    assert result.changes == [(("either",), [1, 2])]
    result = patch_section(Outer, result.data, {"either": {"not": "allowed"}})
    assert [location for location, _ in result.issues] == [("either",)]
    assert result.changes == []


# ---------------------------------------------------------------------- 性质测试

scalars = (
    st.none()
    | st.booleans()
    | st.integers(-10_000, 10_000)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=6)
)
json_values = st.recursive(
    scalars,
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=5), children, max_size=3)
    ),
    max_leaves=8,
)
plausible = {
    "mode": st.sampled_from(["saver", "full", "fast"]),
    "debounce_ms": st.integers(-10, 6000),
    "marker": st.dictionaries(st.sampled_from(["overlay", "bilingual", "junk"]), st.booleans() | scalars),
    "pair": st.lists(st.text(min_size=1, max_size=4), max_size=3),
    "note": st.sampled_from(["", "hello", SECRET_LIKE]),
    "font": st.none() | st.text(max_size=4),
    "colour": scalars,
}
race_raw = st.fixed_dictionaries(
    {}, optional={key: strategy | json_values for key, strategy in plausible.items()}
)


@settings(max_examples=300, deadline=None)
@given(race_raw)
def test_fallback_always_yields_parseable_section(raw: dict[str, JsonValue]) -> None:
    data, issues = validate_section(RaceSettings, raw)

    # 结果一定能被模型解析，并且就是规范 JSON。
    assert RaceSettings.model_validate(data).model_dump(mode="json") == data
    # 每处回退的位置都指向原始数据里存在的键，原因里不出现像密钥的原值。
    for location, reason in issues:
        assert location
        assert location[0] in raw
        assert SECRET_LIKE not in reason
    # 没被报告的字段保留原值（经模型规范化后的值）。
    reported = {location[0] for location, _ in issues}
    for key, value in raw.items():
        if key not in reported:
            alone = RaceSettings.model_validate({key: value}).model_dump(mode="json")
            assert data[key] == alone[key]


@settings(max_examples=300, deadline=None)
@given(race_raw)
def test_patch_accepts_or_rejects_every_field(patch: dict[str, JsonValue]) -> None:
    current = canonical(RaceSettings, {})
    result = patch_section(RaceSettings, current, patch)

    assert RaceSettings.model_validate(result.data).model_dump(mode="json") == result.data
    rejected = {location for location, _ in result.issues}
    for path, value in result.changes:
        assert path not in rejected
        assert lookup(result.data, path) == value
    for key in patch:
        touched = any(location[0] == key for location in rejected) or any(
            path[0] == key for path, _ in result.changes
        )
        unchanged = result.data.get(key) == current.get(key)
        # 每个字段要么被拒绝，要么生效（值相同的修改不算变化）。
        assert touched or unchanged
    # 被拒绝的字段保持原来的值。
    for location in rejected:
        path = cast(tuple[str, ...], location)
        assert lookup(result.data, path) == lookup(current, path)
