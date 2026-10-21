"""UpdateConfig：按字段合并、保留注释写回、非法字段单独拒绝、原子写入（需求 CFG-02、CFG-03、UI-06）。"""

import asyncio
import os
import tomllib
from pathlib import Path

import pytest

from lst.contracts.config import UpdateConfig
from lst.contracts.worker_protocol import JsonValue
from tests.services.config.helpers import (
    DefaultedEngineSchema,
    Harness,
    Launcher,
    engine,
    read,
    write,
)

BASE = """\
# 我的配置：手工写的注释要保留
[hotkeys]
capture = "Ctrl+Q"   # 改过的热键

[race]
# 竞速设置
mode = "saver"       # 先用节省模式
marker = { overlay = false, bilingual = true }
debounce_ms = 300

[[engines]]
id = "argos"         # 本地引擎
kind = "local"
priority = 1

[ocr]
device = "cpu"
"""


async def started(start: Launcher, path: Path, text: str = BASE) -> Harness:
    write(path, text)
    harness = await start(path)
    assert harness.rejections == []
    return harness


async def update(harness: Harness, patch: dict[str, JsonValue]) -> None:
    await harness.send(UpdateConfig(patch))


# ---------------------------------------------------------------------- 合并与写回


async def test_field_merge_keeps_comments_and_order(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)

    await update(harness, {"race": {"mode": "full"}, "hotkeys": {"bilingual": "F2"}})

    # 只改动涉及的行：行尾注释、独立注释、空行和节的顺序都保留；新字段加在节的末尾。
    expected = BASE.replace('mode = "saver"       # 先用', 'mode = "full"       # 先用').replace(
        'capture = "Ctrl+Q"   # 改过的热键\n', 'capture = "Ctrl+Q"   # 改过的热键\nbilingual = "F2"\n'
    )
    assert read(config_path) == expected
    assert harness.section("race")["mode"] == "full"
    assert harness.section("race")["debounce_ms"] == 300
    assert harness.section("hotkeys") == {"capture": "Ctrl+Q", "bilingual": "F2"}
    assert harness.config.revision == 2
    assert harness.rejections == []


async def test_nested_object_merges_recursively(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)

    await update(harness, {"race": {"marker": {"overlay": True}}})

    assert harness.section("race")["marker"] == {"overlay": True, "bilingual": True}
    assert "marker = { overlay = true, bilingual = true }" in read(config_path)


async def test_nested_object_missing_from_file_is_created(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path, '[race]\nmode = "full"\n')

    await update(harness, {"race": {"marker": {"overlay": True}}, "ocr": {"device": "cpu"}})

    parsed = tomllib.loads(read(config_path))
    assert parsed["race"] == {"mode": "full", "marker": {"overlay": True}}
    assert parsed["ocr"] == {"device": "cpu"}
    reloaded = await start(config_path)
    assert reloaded.section("race")["marker"] == {"overlay": True, "bilingual": True}
    assert reloaded.section("ocr")["device"] == "cpu"


async def test_list_section_is_replaced_whole(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)

    await update(
        harness,
        {"engines": [engine("deepseek", 1, kind="online", secret="deepseek"), engine("argos", 2)]},
    )

    assert harness.engines() == [
        {
            "id": "deepseek",
            "kind": "online",
            "priority": 1,
            "timeout_s": 60.0,
            "base_url": "https://api.example.com",
            "secret": "deepseek",
        },
        {"id": "argos", "kind": "local", "priority": 2, "timeout_s": 5.0},
    ]
    text = read(config_path)
    # 表数组在原位置替换，后面的 [ocr] 和文件开头的注释不受影响。
    assert text.startswith("# 我的配置：手工写的注释要保留\n")
    assert text.index('id = "deepseek"') < text.index('id = "argos"') < text.index("[ocr]")
    assert '[ocr]\ndevice = "cpu"\n' in text
    reloaded = await start(config_path)
    assert reloaded.engines() == harness.engines()
    assert reloaded.rejections == []


async def test_list_entries_are_validated_and_normalized(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)

    await update(
        harness,
        {"engines": [engine("a", 1), {"id": "b", "kind": "local", "priority": 0}, engine("c", 1)]},
    )

    # 第 1 条非法被丢弃；a 与 c 优先级重复，按列表顺序重新编号，问题位置换算回提交的列表中的下标。
    assert harness.locations() == [("engines", 1, "priority"), ("engines", 2, "priority")]
    assert [(e["id"], e["priority"]) for e in harness.engines() if isinstance(e, dict)] == [
        ("a", 1),
        ("c", 2),
    ]
    # 写进文件的是规范化之后的条目。
    assert [(e["id"], e["priority"]) for e in tomllib.loads(read(config_path))["engines"]] == [
        ("a", 1),
        ("c", 2),
    ]


async def test_empty_list_is_written_explicitly(start: Launcher, config_path: Path) -> None:
    write(config_path, BASE)
    harness = await start(config_path, schema=DefaultedEngineSchema())

    await update(harness, {"engines": []})

    assert harness.engines() == []
    assert tomllib.loads(read(config_path))["engines"] == []
    # 重新启动后仍然是空数组，没有被默认条目补回来。
    reloaded = await start(config_path, schema=DefaultedEngineSchema())
    assert reloaded.engines() == []


async def test_none_removes_the_key(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)

    await update(harness, {"race": {"font": "Noto Sans"}})
    assert 'font = "Noto Sans"' in read(config_path)

    await update(harness, {"race": {"font": None}})
    assert "font" not in read(config_path)
    assert harness.section("race")["font"] is None
    assert harness.config.revision == 3


# ---------------------------------------------------------------------- 校验与拒绝


async def test_invalid_field_is_rejected_others_written(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)

    await update(harness, {"race": {"mode": "bogus", "debounce_ms": 100, "marker": {"overlay": "x"}}})

    assert harness.locations() == [("race", "mode"), ("race", "marker", "overlay")]
    race = harness.section("race")
    assert (race["mode"], race["debounce_ms"], race["marker"]) == (
        "saver",
        100,
        {"overlay": False, "bilingual": True},
    )
    parsed = tomllib.loads(read(config_path))
    assert parsed["race"]["mode"] == "saver"
    assert parsed["race"]["debounce_ms"] == 100
    assert harness.config.revision == 2


async def test_rejection_reason_hides_the_value(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)
    secret = "sk-proj-0123456789abcdefghij"

    await update(harness, {"race": {"note": secret}})

    assert harness.locations() == [("race", "note")]
    assert secret not in harness.rejections[0].reason
    assert secret not in read(config_path)


async def test_cross_field_constraints(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)

    # 两个字段一起改才满足 min_box <= max_box：整体校验通过，两个都生效。
    await update(harness, {"ocr": {"min_box": 5000, "max_box": 6000}})
    assert (harness.section("ocr")["min_box"], harness.section("ocr")["max_box"]) == (5000, 6000)

    # 合起来违反约束：逐个字段尝试，min_box 单独加入也违反约束被拒绝，max_box 生效。
    await update(harness, {"ocr": {"min_box": 9000, "max_box": 7000}})
    assert harness.locations() == [("ocr", "min_box")]
    assert "min_box 不能大于 max_box" in harness.rejections[0].reason
    assert (harness.section("ocr")["min_box"], harness.section("ocr")["max_box"]) == (5000, 7000)


async def test_unknown_names_and_wrong_shapes_are_rejected(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)

    await update(
        harness,
        {
            "telemetry": {"enabled": True},
            "race": {"colour": "red", "debounce_ms": 10},
            "hotkeys": 5,
            "engines": {"id": "a"},
        },
    )

    assert harness.locations() == [("telemetry",), ("race", "colour"), ("hotkeys",), ("engines",)]
    assert harness.section("race")["debounce_ms"] == 10
    assert "colour" not in read(config_path)


async def test_no_effective_change_neither_writes_nor_publishes(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)
    before = config_path.stat().st_mtime_ns

    await update(harness, {"race": {"mode": "bogus"}})  # 全部非法
    await update(harness, {"race": {"mode": "saver", "marker": {}}})  # 与当前值相同
    await update(harness, {"engines": [engine("argos", 1)]})  # 规范化后与当前列表相同
    await update(harness, {})

    assert len(harness.configs) == 1
    assert harness.locations() == [("race", "mode")]
    assert read(config_path) == BASE
    assert config_path.stat().st_mtime_ns == before


async def test_revision_increments_per_effective_update(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)

    await update(harness, {"race": {"debounce_ms": 1}})
    await update(harness, {"race": {"debounce_ms": 1}})
    await update(harness, {"race": {"debounce_ms": 2}})

    assert [config.revision for config in harness.configs] == [1, 2, 3]
    assert all(config.secrets_revision == 0 for config in harness.configs)


async def test_commands_are_applied_in_order(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)

    # 不等分发完就连发三条：两个协程处理函数并发运行，锁保证按到达顺序依次生效。
    harness.bus.send(UpdateConfig({"race": {"debounce_ms": 1}}))
    harness.bus.send(UpdateConfig({"race": {"debounce_ms": 2}}))
    harness.bus.send(UpdateConfig({"hotkeys": {"capture": "F9"}}))
    await harness.bus.drain()

    assert [config.revision for config in harness.configs] == [1, 2, 3, 4]
    assert harness.section("race")["debounce_ms"] == 2
    parsed = tomllib.loads(read(config_path))
    assert (parsed["race"]["debounce_ms"], parsed["hotkeys"]["capture"]) == (2, "F9")


async def test_section_changes_are_reflected_in_new_config_only(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)
    first = harness.config

    await update(harness, {"race": {"mode": "full"}})

    # 已发布的 AppConfig 不被修改；订阅者改动自己拿到的数据也影响不到服务内部。
    assert first.section("race") == {**harness.section("race"), "mode": "saver"}
    race = harness.section("race")
    race["mode"] = "tampered"
    await update(harness, {"race": {"debounce_ms": 1}})
    assert harness.section("race")["mode"] == "full"


# ---------------------------------------------------------------------- 原子写入与磁盘上的变化


async def test_failed_write_leaves_file_and_config_unchanged(
    start: Launcher, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = await started(start, config_path)

    def refuse(src: object, dst: object) -> None:
        raise PermissionError(13, "文件被占用")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", refuse)
        await update(harness, {"race": {"mode": "full"}})

    assert read(config_path) == BASE
    # 临时文件已删除。
    assert sorted(path.name for path in config_path.parent.iterdir()) == ["settings.toml"]
    assert len(harness.configs) == 1
    assert harness.section("race")["mode"] == "saver"
    assert harness.locations() == [("race", "mode")]
    assert "写入配置文件失败" in harness.rejections[0].reason

    # 恢复之后同样的修改照常生效。
    await update(harness, {"race": {"mode": "full"}})
    assert harness.section("race")["mode"] == "full"
    assert harness.config.revision == 2


async def test_external_edits_survive_an_update(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)
    edited = BASE.replace('capture = "Ctrl+Q"', 'capture = "Ctrl+W"') + "# 运行期间手工加的注释\n"
    write(config_path, edited)

    await update(harness, {"race": {"mode": "full"}})

    text = read(config_path)
    assert 'capture = "Ctrl+W"' in text
    assert "# 运行期间手工加的注释" in text
    assert 'mode = "full"' in text
    # 手工修改要到下次启动才生效。
    assert harness.section("hotkeys")["capture"] == "Ctrl+Q"


async def test_deleted_file_is_recreated(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)
    config_path.unlink()

    await update(harness, {"race": {"mode": "full"}})

    assert read(config_path) == BASE.replace('mode = "saver"', 'mode = "full"')


async def test_file_broken_while_running_is_preserved(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)
    write(config_path, "[race\n")

    await update(harness, {"race": {"mode": "full"}})

    backups = list(config_path.parent.glob("settings.toml.broken-*"))
    assert [read(path) for path in backups] == ["[race\n"]
    assert read(config_path) == BASE.replace('mode = "saver"', 'mode = "full"')


async def test_commands_after_stop_are_ignored(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)

    await harness.service.stop()
    await update(harness, {"race": {"mode": "full"}})

    assert len(harness.configs) == 1
    assert read(config_path) == BASE


async def test_update_waits_for_lock_holder(start: Launcher, config_path: Path) -> None:
    harness = await started(start, config_path)

    # stop 在修改写完之后才返回：先发命令，再立刻 stop，修改仍然完整落盘。
    harness.bus.send(UpdateConfig({"race": {"mode": "full"}}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await harness.service.stop()
    await harness.bus.drain()

    assert 'mode = "full"' in read(config_path)
