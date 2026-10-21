"""entries 的测试：引擎实例的读取、排序、新建、校验，以及未确认的草稿列表。"""

from lst.contracts.config import AppConfig
from lst.contracts.worker_protocol import JsonValue
from lst.ui.settings.entries import (
    EngineDraft,
    engine_entries,
    new_entry,
    parse_entry,
    raw_entries,
    renumber,
    reorder,
    secret_name_for,
    unique_id,
    used_secret_names,
    validate_entry,
    with_field,
)
from tests.ui.settings.support import default_config, default_engines, engine, engine_kinds


def ids(entries: list[dict[str, JsonValue]]) -> list[str]:
    return [str(entry["id"]) for entry in entries]


def test_entries_are_ordered_by_priority_then_position() -> None:
    config = default_config(
        engines=[engine("deepl", "b", 2), engine("google", "a", 2), engine("local_nmt", "c", 1)]
    )
    assert [e.id for e in engine_entries(config)] == ["c", "b", "a"]


def test_parse_entry_reads_the_common_fields() -> None:
    entry = parse_entry(engine("openai_compat", "ds", 2, color="#4D6BFE"))
    assert entry is not None
    assert (entry.id, entry.kind, entry.name, entry.priority, entry.color) == (
        "ds",
        "openai_compat",
        "DeepSeek",
        2,
        "#4D6BFE",
    )
    assert entry.secret == "ds" and entry.enabled and entry.provider == "deepseek"
    assert parse_entry(engine("local_nmt", "argos", 1, enabled=False)).enabled is False  # type: ignore[union-attr]


def test_parse_entry_rejects_malformed_records() -> None:
    assert parse_entry("x") is None
    assert parse_entry({"id": "a", "kind": "deepl", "name": "n", "priority": True}) is None
    assert parse_entry({"id": 1, "kind": "deepl", "name": "n", "priority": 1}) is None


def test_missing_engines_section_is_empty() -> None:
    assert engine_entries(None) == []
    assert raw_entries(AppConfig(sections={}, revision=1)) == []
    assert raw_entries(AppConfig(sections={"engines": "bad"}, revision=1)) == []


def test_renumber_and_reorder() -> None:
    entries = default_engines()
    assert [e["priority"] for e in renumber(reversed(entries))] == [1, 2, 3, 4]
    result = reorder(entries, ["deepl", "argos", "ollama", "deepseek"])
    assert result is not None
    assert [(e["id"], e["priority"]) for e in result] == [
        ("deepl", 1),
        ("argos", 2),
        ("ollama", 3),
        ("deepseek", 4),
    ]
    # 输入不被修改。
    assert [e["priority"] for e in entries] == [1, 2, 3, 4]


def test_reorder_requires_the_same_set_of_ids() -> None:
    entries = default_engines()
    assert reorder(entries, ["argos"]) is None
    assert reorder(entries, ["argos", "deepseek", "deepl", "nope"]) is None
    assert reorder(entries, ["argos", "argos", "deepl", "ollama"]) is None


def test_validate_entry_returns_the_canonical_record() -> None:
    raw = {"kind": "deepl", "id": "d", "name": "DeepL", "priority": 1}
    entry, errors = validate_entry(engine_kinds(), raw)
    assert errors == {}
    assert entry is not None
    assert entry["kind"] == "deepl" and entry["timeout_s"] == 8.0 and entry["formality"] == "default"


def test_validate_entry_reports_errors_by_field_without_values() -> None:
    entry, errors = validate_entry(
        engine_kinds(), with_field(engine("openai_compat", "ds", 1), "base_url", "ftp://user:pw123456@h")
    )
    assert entry is None
    assert list(errors) == ["base_url"]
    assert "pw123456" not in errors["base_url"]


def test_validate_entry_unknown_kind() -> None:
    entry, errors = validate_entry(engine_kinds(), {"kind": "nope", "id": "x"})
    assert entry is None and list(errors) == ["kind"]
    assert validate_entry(engine_kinds(), {"id": "x"})[0] is None


def test_unique_id_slugs_and_avoids_collisions() -> None:
    assert unique_id("DeepSeek Flash", set()) == "deepseek-flash"
    assert unique_id("DeepSeek", {"deepseek"}) == "deepseek-2"
    assert unique_id("DeepSeek", {"deepseek", "deepseek-2"}) == "deepseek-3"
    assert unique_id("百度翻译", set()) == "engine"
    assert secret_name_for("deepl", {"deepl"}) == "deepl-2"


def test_new_entry_fills_id_priority_and_defaults() -> None:
    values: dict[str, JsonValue] = {
        "name": "My LLM",
        "provider": "generic",
        "base_url": "https://llm.example/v1",
        "model": "m1",
        "timeout_s": 60.0,
    }
    entry, errors = new_entry(engine_kinds(), "openai_compat", values, taken_ids={"my-llm"}, priority=5)
    assert errors == {} and entry is not None
    assert (entry["id"], entry["priority"], entry["kind"]) == ("my-llm-2", 5, "openai_compat")
    assert entry["max_batch_segments"] == 20  # 模型的默认值


def test_new_entry_reports_missing_required_fields() -> None:
    entry, errors = new_entry(engine_kinds(), "baidu", {"name": "百度"}, taken_ids=set(), priority=1)
    assert entry is None and list(errors) == ["appid"]


def test_used_secret_names() -> None:
    entries = default_engines()
    assert used_secret_names(entries) == {"deepseek", "deepl"}
    assert used_secret_names(entries, except_id="deepl") == {"deepseek"}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_draft_is_the_base_until_the_config_confirms_it() -> None:
    clock = Clock()
    draft = EngineDraft(max_age_s=2.0, clock=clock)
    config = default_config()
    assert draft.working(config) == raw_entries(config)
    assert not draft.pending

    sent = renumber(reversed(raw_entries(config)))
    draft.sent(sent)
    assert draft.pending and draft.working(config) == sent  # 还没确认：后续修改在草稿上进行
    draft.confirm(config)  # AppConfig 还是旧的
    assert draft.pending
    draft.confirm(default_config(engines=sent))  # 配置服务确认了
    assert not draft.pending


def test_draft_expires_and_can_be_discarded() -> None:
    clock = Clock()
    draft = EngineDraft(max_age_s=2.0, clock=clock)
    config = default_config()
    draft.sent([engine("deepl", "x", 1)])
    clock.now = 1.0
    draft.confirm(config)
    assert draft.pending
    clock.now = 3.0  # 配置服务规范化后永远不会相等：过期后丢弃
    draft.confirm(config)
    assert not draft.pending
    draft.sent([engine("deepl", "x", 1)])
    draft.discard()
    assert not draft.pending


def test_draft_working_copy_is_independent() -> None:
    draft = EngineDraft()
    draft.sent([engine("deepl", "x", 1)])
    working = draft.working(None)
    working[0]["name"] = "changed"
    assert draft.working(None)[0]["name"] == "DeepL"
