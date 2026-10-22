"""引擎注册表与 `[[engines]]` 校验模式的测试：逐条校验、跨条目规范化、默认列表与工厂适配。"""

import copy
import json
import math
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Protocol

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from lst.contracts.errors import ErrorCode
from lst.contracts.ports import WorkerRequestHandle, WorkerState
from lst.contracts.translation import EngineKind
from lst.contracts.worker_protocol import JsonValue, OcrRequest, TranslateRequest
from lst.services.engines import (
    ENGINE_KINDS,
    EngineDeps,
    EngineListSchema,
    EngineUnavailableError,
)
from lst.services.engines.baidu import BaiduSettings
from lst.services.engines.common import EngineSettings
from lst.services.engines.deepl import DeepLSettings
from lst.services.engines.dictionary import DictionarySettings
from lst.services.engines.google import GoogleSettings
from lst.services.engines.google_free import GoogleFreeSettings
from lst.services.engines.local_nmt import LocalNmtSettings
from lst.services.engines.microsoft_free import MicrosoftFreeSettings
from lst.services.engines.openai_compat import OpenAICompatSettings
from tests.services.engines.mt_helpers import FakeSecrets

SCHEMA = EngineListSchema()

SETTINGS_MODELS: dict[str, type[EngineSettings]] = {
    "openai_compat": OpenAICompatSettings,
    "deepl": DeepLSettings,
    "google": GoogleSettings,
    "baidu": BaiduSettings,
    "google_free": GoogleFreeSettings,
    "microsoft_free": MicrosoftFreeSettings,
    "local_nmt": LocalNmtSettings,
    "dictionary": DictionarySettings,
}

# 每类引擎一条合法配置，只写必填字段和少量非默认值。
VALID: dict[str, dict[str, Any]] = {
    "openai_compat": {
        "kind": "openai_compat",
        "id": "deepseek",
        "name": "DeepSeek Flash",
        "priority": 1,
        "timeout_s": 60,
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "secret": "deepseek",
        "languages": ["en", "zh-Hans"],
    },
    "deepl": {
        "kind": "deepl",
        "id": "deepl",
        "name": "DeepL",
        "priority": 2,
        "secret": "deepl",
        "formality": "more",
    },
    "google": {"kind": "google", "id": "google", "name": "Google", "priority": 3, "secret": "google"},
    "baidu": {
        "kind": "baidu",
        "id": "baidu",
        "name": "百度",
        "priority": 4,
        "secret": "baidu",
        "appid": "20150630000000001",
    },
    "local_nmt": {"kind": "local_nmt", "id": "argos", "name": "Argos", "priority": 5, "color": "#2E7D32"},
    "dictionary": {"kind": "dictionary", "id": "dictionary", "name": "词典", "priority": 6},
    "google_free": {"kind": "google_free", "id": "google_free", "name": "谷歌翻译（免费）", "priority": 7},
    "microsoft_free": {
        "kind": "microsoft_free",
        "id": "microsoft_free",
        "name": "微软翻译（免费）",
        "priority": 8,
        "endpoint": "https://edge.microsoft.com/translate/translatetext",
    },
}

KINDS = sorted(VALID)


def without_kind(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if key != "kind"}


def priority_of(item: Mapping[str, JsonValue]) -> int:
    value = item["priority"]
    assert isinstance(value, int)
    return value


class ListSectionSchemaLike(Protocol):
    """配置服务的 ListSectionSchema 协议的副本：EngineListSchema 要在结构上满足它。"""

    def validate_item(self, raw: Mapping[str, Any]) -> dict[str, JsonValue]: ...

    def normalize(
        self, items: list[dict[str, JsonValue]]
    ) -> tuple[list[dict[str, JsonValue]], list[tuple[tuple[str | int, ...], str]]]: ...


class FakeChannel:
    """只提供能力清单的 NMT 通道，不接受请求。"""

    def __init__(self) -> None:
        self.state = WorkerState.READY
        self.capabilities: Mapping[str, JsonValue] | None = {"pairs": [["en", "ja"]], "fingerprint": "fp"}

    def submit(
        self, request: TranslateRequest | OcrRequest, *, payload: bytes | None = None, timeout_s: float
    ) -> WorkerRequestHandle:
        raise AssertionError("注册表测试不应提交推理请求")


def reject(request: httpx.Request) -> httpx.Response:
    raise AssertionError("注册表测试不应发出 HTTP 请求")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    instance = httpx.AsyncClient(transport=httpx.MockTransport(reject))
    yield instance
    await instance.aclose()


# ---------------------------------------------------------------- 注册表


def test_registry_has_eight_kinds_with_their_settings_models() -> None:
    assert set(ENGINE_KINDS) == set(SETTINGS_MODELS)
    for kind, spec in ENGINE_KINDS.items():
        assert spec.kind == kind
        assert spec.settings_model is SETTINGS_MODELS[kind]
        assert spec.uses_nmt == (kind == "local_nmt")


def test_schema_satisfies_list_section_protocol() -> None:
    schema: ListSectionSchemaLike = EngineListSchema()  # 静态检查：结构上满足配置服务的协议
    assert schema.validate_item(VALID["google"])["kind"] == "google"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (VALID["openai_compat"], True),
        ({**VALID["openai_compat"], "provider": "ollama", "base_url": "http://127.0.0.1:11434/v1"}, False),
        (VALID["deepl"], True),
        (VALID["google"], True),
        (VALID["baidu"], True),
        (VALID["local_nmt"], False),
        (VALID["dictionary"], False),
        (VALID["google_free"], False),
        (VALID["microsoft_free"], False),
    ],
)
def test_requires_key(raw: dict[str, Any], expected: bool) -> None:
    spec = ENGINE_KINDS[raw["kind"]]
    settings = spec.settings_model.model_validate(without_kind(raw))
    assert spec.requires_key(settings) is expected


async def test_build_creates_each_kind(client: httpx.AsyncClient, tmp_path: Path) -> None:
    secrets = FakeSecrets({"deepseek": "k1", "deepl": "k2", "google": "k3", "baidu": "k4"})
    deps = EngineDeps(secrets=secrets, client=client, nmt=FakeChannel(), dict_dir=tmp_path / "dict")
    expected_kinds = {
        "openai_compat": EngineKind.ONLINE_LLM,
        "deepl": EngineKind.ONLINE_MT,
        "google": EngineKind.ONLINE_MT,
        "baidu": EngineKind.ONLINE_MT,
        "local_nmt": EngineKind.LOCAL_NMT,
        "dictionary": EngineKind.LOCAL_NMT,
        "google_free": EngineKind.ONLINE_MT,
        "microsoft_free": EngineKind.ONLINE_MT,
    }
    for kind, raw in VALID.items():
        spec = ENGINE_KINDS[kind]
        engine = spec.build(spec.settings_model.model_validate(without_kind(raw)), deps)
        assert engine.info.kind is expected_kinds[kind]
        assert engine.info.id == raw["id"]
        assert engine.info.priority == raw["priority"]


async def test_build_local_nmt_without_channel_raises_unavailable(client: httpx.AsyncClient) -> None:
    spec = ENGINE_KINDS["local_nmt"]
    settings = spec.settings_model.model_validate(without_kind(VALID["local_nmt"]))
    with pytest.raises(EngineUnavailableError) as info:
        spec.build(settings, EngineDeps(secrets=FakeSecrets(), client=client, nmt=None))
    assert info.value.code is ErrorCode.WORKER_CRASHED
    assert "NMT" in info.value.detail


async def test_build_dictionary_without_directory_raises_unavailable(client: httpx.AsyncClient) -> None:
    spec = ENGINE_KINDS["dictionary"]
    settings = spec.settings_model.model_validate(without_kind(VALID["dictionary"]))
    with pytest.raises(EngineUnavailableError) as info:
        spec.build(settings, EngineDeps(secrets=FakeSecrets(), client=client))
    assert info.value.code is ErrorCode.MODEL_MISSING
    assert "词典目录" in info.value.detail


async def test_build_rejects_settings_of_another_kind(client: httpx.AsyncClient) -> None:
    google = GoogleSettings.model_validate(without_kind(VALID["google"]))
    with pytest.raises(TypeError, match="DeepLSettings"):
        ENGINE_KINDS["deepl"].build(google, EngineDeps(secrets=FakeSecrets(), client=client))
    with pytest.raises(TypeError):
        ENGINE_KINDS["local_nmt"].requires_key(google)


# ---------------------------------------------------------------- validate_item


@pytest.mark.parametrize("kind", KINDS)
def test_validate_item_accepts_valid_entry(kind: str) -> None:
    raw = VALID[kind]
    snapshot = copy.deepcopy(raw)
    out = SCHEMA.validate_item(raw)

    assert raw == snapshot  # 不修改输入
    assert out["kind"] == kind
    assert next(iter(out)) == "kind"
    # 输出是规范 JSON：能原样经过 JSON 编解码，并且包含默认值。
    assert json.loads(json.dumps(out)) == out
    assert out["enabled"] is True
    assert "max_retries" in out and "timeout_s" in out
    # 去掉 kind 后能用该类的配置模型解析回去，结果与直接解析输入相同。
    model = SETTINGS_MODELS[kind]
    assert model.model_validate(without_kind(out)) == model.model_validate(without_kind(raw))
    # 规范化是幂等的。
    assert SCHEMA.validate_item(out) == out


def test_validate_item_normalizes_values() -> None:
    llm = SCHEMA.validate_item(VALID["openai_compat"])
    assert llm["languages"] == ["en", "zh-Hans"]
    assert llm["timeout_s"] == 60.0
    assert llm["max_concurrency"] == 2

    # 本地 provider 没写 max_concurrency 时默认 1，规范 JSON 里写明，再次校验结果不变。
    ollama = SCHEMA.validate_item(
        {**VALID["openai_compat"], "provider": "ollama", "base_url": "http://127.0.0.1:11434/v1"}
    )
    assert ollama["max_concurrency"] == 1
    assert SCHEMA.validate_item(ollama) == ollama

    nmt = SCHEMA.validate_item(VALID["local_nmt"])
    assert (nmt["timeout_s"], nmt["processing_timeout_s"], nmt["max_retries"]) == (10.0, 5.0, 0)
    assert nmt["secret"] is None
    assert nmt["color"] == "#2E7D32"


def test_validate_item_without_kind_raises_value_error_listing_options() -> None:
    with pytest.raises(ValueError, match="kind") as info:
        SCHEMA.validate_item(without_kind(VALID["deepl"]))
    assert not isinstance(info.value, ValidationError)
    for kind in KINDS:
        assert kind in str(info.value)


@pytest.mark.parametrize("kind", ["online_llm", "DeepL", "", 3, None, ["deepl"]])
def test_validate_item_with_unknown_kind_raises_value_error(kind: object) -> None:
    raw = {**VALID["deepl"], "kind": kind}
    with pytest.raises(ValueError, match="可选值") as info:
        SCHEMA.validate_item(raw)
    assert not isinstance(info.value, ValidationError)
    # 原因说明里不带原值（原值可能是机密，见 ConfigRejected 的约定）。
    if isinstance(kind, str) and kind:
        assert kind not in str(info.value)


@pytest.mark.parametrize(
    ("kind", "patch", "loc"),
    [
        ("deepl", {"priority": 0}, ("priority",)),
        ("deepl", {"priority": "high"}, ("priority",)),
        ("google", {"api_key": "AIza-should-not-be-here"}, ("api_key",)),
        ("google", {"id": ""}, ("id",)),
        ("baidu", {"appid": None}, ("appid",)),
        ("openai_compat", {"base_url": "https://api.example.com/v1?key=1"}, ("base_url",)),
        ("openai_compat", {"provider": "unknown"}, ("provider",)),
        ("openai_compat", {"timeout_s": math.inf}, ("timeout_s",)),
        ("local_nmt", {"timeout_s": math.nan}, ("timeout_s",)),
        ("local_nmt", {"timeout_s": 0}, ("timeout_s",)),
        ("local_nmt", {"max_retries": 1}, ("max_retries",)),
        ("deepl", {"formality": "polite"}, ("formality",)),
    ],
)
def test_validate_item_with_invalid_field_raises_validation_error(
    kind: str, patch: dict[str, Any], loc: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError) as info:
        SCHEMA.validate_item({**VALID[kind], **patch})
    assert loc in [error["loc"] for error in info.value.errors()]


@pytest.mark.parametrize(("kind", "field"), [("deepl", "id"), ("google", "name"), ("baidu", "appid")])
def test_validate_item_with_missing_required_field(kind: str, field: str) -> None:
    raw = {key: value for key, value in VALID[kind].items() if key != field}
    with pytest.raises(ValidationError) as info:
        SCHEMA.validate_item(raw)
    assert (field,) in [error["loc"] for error in info.value.errors()]


def test_validate_item_requires_timeout_for_llm() -> None:
    raw = {key: value for key, value in VALID["openai_compat"].items() if key != "timeout_s"}
    with pytest.raises(ValidationError) as info:
        SCHEMA.validate_item(raw)
    assert ("timeout_s",) in [error["loc"] for error in info.value.errors()]


# ---------------------------------------------------------------- normalize


def entry(engine_id: str, priority: int, kind: str = "google") -> dict[str, JsonValue]:
    return SCHEMA.validate_item(
        {"kind": kind, "id": engine_id, "name": engine_id.upper(), "priority": priority}
    )


def test_normalize_keeps_valid_list_unchanged() -> None:
    items = [entry("a", 5), entry("b", 2, "local_nmt"), entry("c", 9)]
    out, problems = SCHEMA.normalize(items)
    assert out == items
    assert problems == []
    # 输出是新的字典，修改它不影响输入。
    out[0]["priority"] = 1
    assert items[0]["priority"] == 5


def test_normalize_drops_duplicate_ids_keeping_the_first() -> None:
    items = [entry("a", 1), entry("b", 2), entry("a", 3, "local_nmt"), entry("c", 4), entry("b", 5)]
    out, problems = SCHEMA.normalize(items)
    assert out == [items[0], items[1], items[3]]
    assert [location for location, _ in problems] == [(2, "id"), (4, "id")]
    assert "位置 0" in problems[0][1]
    assert "位置 1" in problems[1][1]


def test_normalize_renumbers_duplicate_priorities_once() -> None:
    items = [entry("a", 3), entry("b", 1), entry("c", 3), entry("d", 7), entry("e", 1)]
    snapshot = copy.deepcopy(items)
    out, problems = SCHEMA.normalize(items)
    # 按（原优先级，列表顺序）排序：b(1) e(1) a(3) c(3) d(7)。
    assert [priority_of(item) for item in out] == [3, 1, 4, 5, 2]
    assert [item["id"] for item in out] == ["a", "b", "c", "d", "e"]
    assert len(problems) == 1
    assert problems[0][0] == (2, "priority")
    assert items == snapshot


def test_normalize_reports_positions_of_the_input_list() -> None:
    # 下标 2 因 id 重复被丢弃；剩下的 a、b 优先级相同，b 在输入中的下标是 1。
    items = [entry("a", 1), entry("b", 1), entry("a", 2)]
    out, problems = SCHEMA.normalize(items)
    assert [(item["id"], item["priority"]) for item in out] == [("a", 1), ("b", 2)]
    assert [location for location, _ in problems] == [(2, "id"), (1, "priority")]


def test_normalize_handles_empty_list() -> None:
    assert SCHEMA.normalize([]) == ([], [])


def test_normalize_rejects_unvalidated_items() -> None:
    with pytest.raises(ValueError, match="id"):
        SCHEMA.normalize([{"kind": "google", "priority": 1}])
    with pytest.raises(ValueError, match="priority"):
        SCHEMA.normalize([{"kind": "google", "id": "g", "priority": True}])


@given(
    st.lists(
        st.tuples(
            st.sampled_from(["a", "b", "c", "d", "e"]),
            st.integers(min_value=1, max_value=6),
            st.sampled_from(["google", "local_nmt"]),
        ),
        max_size=8,
    )
)
def test_normalize_properties(specs: list[tuple[str, int, str]]) -> None:
    items = [entry(engine_id, priority, kind) for engine_id, priority, kind in specs]
    snapshot = copy.deepcopy(items)
    out, problems = SCHEMA.normalize(items)
    assert items == snapshot

    # id：保留每个 id 第一次出现的条目，顺序不变；每丢弃一条报告一次。
    kept: list[int] = []
    dropped: list[int] = []
    seen: set[str] = set()
    for index, (engine_id, _, _) in enumerate(specs):
        (dropped if engine_id in seen else kept).append(index)
        seen.add(engine_id)
    assert [item["id"] for item in out] == [items[index]["id"] for index in kept]
    assert [location for location, _ in problems if location[1] == "id"] == [
        (index, "id") for index in dropped
    ]

    # 除优先级外，保留下来的条目原样输出，并且仍是合法的规范 JSON。
    for item, index in zip(out, kept, strict=True):
        assert {**item, "priority": 0} == {**items[index], "priority": 0}
        assert SCHEMA.validate_item(item) == item

    before = [priority_of(items[index]) for index in kept]
    after = [priority_of(item) for item in out]
    assert len(set(after)) == len(after)
    for i in range(len(after)):
        for j in range(i + 1, len(after)):
            # 原优先级严格不同的保持原先后；相同的按列表顺序排列（i 在 j 之前）。
            if before[i] <= before[j]:
                assert after[i] < after[j]
            else:
                assert after[i] > after[j]

    renumbered = len(set(before)) != len(before)
    priority_reports = [location for location, _ in problems if location[1] == "priority"]
    if renumbered:
        assert sorted(after) == list(range(1, len(after) + 1))
        assert len(priority_reports) == 1
    else:
        assert after == before
        assert priority_reports == []


# ---------------------------------------------------------------- default_items


def test_default_items_pass_validation() -> None:
    items = SCHEMA.default_items()
    assert len(items) == 4
    for item in items:
        assert SCHEMA.validate_item(item) == item
    nmt, google_free, microsoft_free, dictionary = items
    assert (nmt["kind"], nmt["id"], nmt["priority"], nmt["enabled"]) == ("local_nmt", "argos", 1, True)
    # 两个免费接口默认列出但不启用（需求 PRV-01），不需要 key，排在本地 NMT 之后、字典翻译之前。
    for item, kind, priority in ((google_free, "google_free", 2), (microsoft_free, "microsoft_free", 3)):
        assert (item["kind"], item["id"], item["priority"], item["enabled"], item["secret"]) == (
            kind,
            kind,
            priority,
            False,
            None,
        )
        spec = ENGINE_KINDS[kind]
        assert not spec.requires_key(spec.settings_model.model_validate(without_kind(item)))
    # 字典翻译启用、不需要 API key，优先级数值最大（最低）：它先出粗译，其他引擎的译文到达后替换它。
    assert (dictionary["kind"], dictionary["id"], dictionary["enabled"], dictionary["secret"]) == (
        "dictionary",
        "dictionary",
        True,
        None,
    )
    assert dictionary["priority"] == max(priority_of(item) for item in items)
    settings = DictionarySettings.model_validate(without_kind(dictionary))
    assert not ENGINE_KINDS["dictionary"].requires_key(settings)
    assert SCHEMA.normalize(items) == (items, [])
    # 每次调用得到新的列表，调用方修改它不影响下一次。
    items[0]["priority"] = 9
    assert SCHEMA.default_items()[0]["priority"] == 1
