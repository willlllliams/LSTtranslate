"""配置服务启动：生成默认文件、逐字段回退、数组节、坏文件处理（需求 CFG-02，架构文档 2.9.1 节、第 9 章）。"""

import logging
import tomllib
from pathlib import Path

import pytest
from pydantic import BaseModel

from lst.services.config import ConfigService, MemorySecretStore
from tests.services.config.helpers import (
    ENGINE_MODELS,
    MODULE_LOGGER,
    SECRET_LIKE,
    SECTIONS,
    DefaultedEngineSchema,
    FakeEngineSchema,
    Launcher,
    engine,
    read,
    write,
)

DEFAULTS = {name: model().model_dump(mode="json") for name, model in SECTIONS.items()}
# 默认文件里的内容：TOML 没有空值，值为 None 的字段不写出来。
DEFAULT_FILE = {
    name: {key: value for key, value in section.items() if value is not None}
    for name, section in DEFAULTS.items()
}


def warnings_of(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == MODULE_LOGGER and record.levelno == logging.WARNING
    ]


# ---------------------------------------------------------------------- 文件不存在


async def test_missing_file_generates_commented_default(start: Launcher, config_path: Path) -> None:
    harness = await start(config_path)

    text = read(config_path)
    parsed = tomllib.loads(text)
    # 普通节写出了全部默认值，与发布的配置一致；没有默认条目的数组节不出现。
    assert parsed == DEFAULT_FILE
    assert {name: harness.section(name) for name in SECTIONS} == DEFAULTS
    assert harness.engines() == []
    # 带简短中文注释：文件说明、节说明（模型文档字符串）和字段说明（Field.description）。
    assert text.startswith("# LSTtranslate 配置文件")
    assert "# 竞速翻译。" in text
    assert 'capture = "Alt+Q" # 截屏翻译' in text
    assert "debounce_ms = 400 # 双语窗口的输入防抖，毫秒" in text
    assert harness.config.revision == 1
    assert harness.config.secrets_revision == 0
    assert harness.rejections == []
    # 没有留下临时文件。
    assert sorted(p.name for p in config_path.parent.iterdir()) == ["settings.toml"]


async def test_missing_file_writes_default_list_items(start: Launcher, config_path: Path) -> None:
    harness = await start(config_path, schema=DefaultedEngineSchema())

    expected = [{"id": "argos", "kind": "local", "priority": 1, "timeout_s": 5.0}]
    assert harness.engines() == expected
    assert tomllib.loads(read(config_path))["engines"] == expected


async def test_generated_file_reloads_without_changes(start: Launcher, config_path: Path) -> None:
    first = await start(config_path, schema=DefaultedEngineSchema())
    text = read(config_path)

    second = await start(config_path, schema=DefaultedEngineSchema())

    assert second.config.sections == first.config.sections
    assert second.rejections == []
    assert read(config_path) == text


async def test_valid_file_is_loaded_and_left_untouched(start: Launcher, config_path: Path) -> None:
    text = (
        "# 我的配置\r\n"
        "[race]\r\n"
        'mode = "full"  # 完整模式\r\n'
        "marker = { overlay = true }\r\n"
        "\r\n"
        "[[engines]]\r\n"
        'id = "argos"\r\n'
        'kind = "local"\r\n'
        "priority = 2\r\n"
    )
    write(config_path, text)

    harness = await start(config_path)

    race = harness.section("race")
    assert race["mode"] == "full"
    assert race["marker"] == {"overlay": True, "bilingual": True}
    assert race["debounce_ms"] == 400
    # 文件里没有的节使用默认值。
    assert harness.section("hotkeys") == DEFAULTS["hotkeys"]
    assert harness.engines() == [{"id": "argos", "kind": "local", "priority": 2, "timeout_s": 5.0}]
    assert harness.rejections == []
    # 启动不改写用户的文件，连 CRLF 换行也保持原样。
    assert read(config_path) == text


async def test_utf8_bom_is_accepted(start: Launcher, config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(b'\xef\xbb\xbf[race]\nmode = "full"\n')

    harness = await start(config_path)

    assert harness.section("race")["mode"] == "full"
    assert harness.rejections == []


# ---------------------------------------------------------------------- 逐字段回退

BAD_FIELDS = f"""\
# 用户写的注释
[hotkeys]
capture = "Ctrl+Shift+Q"
bilingual = ""                     # 太短

[race]
mode = "full"
debounce_ms = 99999                # 越界
marker = {{ overlay = "{SECRET_LIKE}", bilingual = false }}   # 嵌套字段类型错
pair = ["ja", 3]                   # 列表元素类型错
note = "{SECRET_LIKE}"             # 自定义校验器把原值写进了错误消息
colour = "blue"                    # 未知字段

[ocr]
device = "tpu"                     # 不在可选范围内
min_box = 2

[telemetry]                        # 未知的节
enabled = true
"""


async def test_each_invalid_field_falls_back_alone(
    start: Launcher, config_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write(config_path, BAD_FIELDS)
    caplog.set_level(logging.WARNING, logger=MODULE_LOGGER)

    harness = await start(config_path)

    assert harness.section("hotkeys") == {"capture": "Ctrl+Shift+Q", "bilingual": "Alt+E"}
    assert harness.section("race") == {
        "mode": "full",
        "debounce_ms": 400,
        "marker": {"overlay": False, "bilingual": False},
        "pair": ["zh-Hans", "en"],
        "note": "",
        "font": None,
    }
    assert harness.section("ocr") == {"device": "gpu", "min_box": 2, "max_box": 4000}
    assert sorted(harness.locations(), key=repr) == sorted(
        [
            ("telemetry",),
            ("hotkeys", "bilingual"),
            ("race", "debounce_ms"),
            ("race", "marker", "overlay"),
            ("race", "pair"),
            ("race", "note"),
            ("race", "colour"),
            ("ocr", "device"),
        ],
        key=repr,
    )
    reasons = {rejection.location: rejection.reason for rejection in harness.rejections}
    assert reasons[("telemetry",)] == "未知的配置节，已忽略"
    assert reasons[("race", "colour")] == "未知字段"
    assert "less than or equal to 5000" in reasons[("race", "debounce_ms")]
    # 列表字段整体回退，原因里写出是哪一项出错。
    assert reasons[("race", "pair")].startswith("[1]：")
    assert reasons[("race", "note")] == "Value error, 这里不能填 API key：***"
    # 每处回退记一条警告；原值（可能是机密）既不进原因说明，也不进日志。
    for reason in reasons.values():
        assert SECRET_LIKE not in reason
    assert len(warnings_of(caplog)) == len(harness.rejections)
    assert all(SECRET_LIKE not in message for message in warnings_of(caplog))
    assert any("race.marker.overlay" in message for message in warnings_of(caplog))
    # 回退只发生在内存里，用户的文件保持原样。
    assert read(config_path) == BAD_FIELDS


async def test_cross_field_violation_reverts_only_the_offending_field(
    start: Launcher, config_path: Path
) -> None:
    # 模型校验器的错误位置是整节，无法对应到某个键：从默认值出发按文件顺序逐个字段加入。
    write(config_path, '[ocr]\ndevice = "cpu"\nmin_box = 10\nmax_box = 5\n')

    harness = await start(config_path)

    assert harness.section("ocr") == {"device": "cpu", "min_box": 10, "max_box": 4000}
    assert harness.locations() == [("ocr", "max_box")]
    assert "min_box 不能大于 max_box" in harness.rejections[0].reason


async def test_section_of_wrong_type_uses_defaults(start: Launcher, config_path: Path) -> None:
    write(config_path, 'race = 5\n[hotkeys]\ncapture = "F1"\n')

    harness = await start(config_path)

    assert harness.section("race") == DEFAULTS["race"]
    assert harness.section("hotkeys")["capture"] == "F1"
    assert harness.locations() == [("race",)]


# ---------------------------------------------------------------------- 数组节


async def test_bad_entries_are_dropped_whole(start: Launcher, config_path: Path) -> None:
    write(
        config_path,
        f"""\
[[engines]]
id = "argos"
kind = "local"
priority = 1

[[engines]]
id = "deepseek"
kind = "online"
priority = 2
secret = "deepseek"
# 缺少必填的 base_url

[[engines]]
id = "mystery"
kind = "{SECRET_LIKE}"
priority = 3

[[engines]]
id = "slow"
kind = "local"
priority = 4
timeout_s = 0
""",
    )

    harness = await start(config_path)

    assert harness.engines() == [{"id": "argos", "kind": "local", "priority": 1, "timeout_s": 5.0}]
    assert harness.locations() == [
        ("engines", 1, "base_url"),
        ("engines", 2),
        ("engines", 3, "timeout_s"),
    ]
    reasons = [rejection.reason for rejection in harness.rejections]
    assert all(reason.endswith("（该条目已整条丢弃）") for reason in reasons)
    # ValueError 的消息里带着原值，配置服务把它换成了 ***。
    assert reasons[1] == "未知的引擎类型 '***'（该条目已整条丢弃）"
    assert all(SECRET_LIKE not in reason for reason in reasons)


async def test_normalize_problems_point_at_original_index(start: Launcher, config_path: Path) -> None:
    # 第 0 条无效被丢弃，normalize 看到的列表少了一条；它报告的下标要换算回文件里的下标。
    write(
        config_path,
        """\
[[engines]]
id = "broken"
kind = "local"
priority = 0

[[engines]]
id = "argos"
kind = "local"
priority = 1

[[engines]]
id = "argos"
kind = "local"
priority = 2
""",
    )

    harness = await start(config_path)

    assert harness.locations() == [("engines", 0, "priority"), ("engines", 2, "id")]
    assert harness.engines() == [{"id": "argos", "kind": "local", "priority": 1, "timeout_s": 5.0}]


async def test_duplicate_priorities_are_renumbered_in_list_order(start: Launcher, config_path: Path) -> None:
    write(
        config_path,
        """\
[[engines]]
id = "a"
kind = "local"
priority = 5

[[engines]]
id = "b"
kind = "local"
priority = 5

[[engines]]
id = "c"
kind = "local"
priority = 9
""",
    )

    harness = await start(config_path)

    assert [(item["id"], item["priority"]) for item in harness.engines() if isinstance(item, dict)] == [
        ("a", 1),
        ("b", 2),
        ("c", 3),
    ]
    assert harness.locations() == [("engines", 1, "priority")]


async def test_non_table_entries_and_wrong_shape(start: Launcher, config_path: Path) -> None:
    write(config_path, 'engines = [1, { id = "a", kind = "local", priority = 1 }]\n')
    harness = await start(config_path)
    assert harness.locations() == [("engines", 0)]
    assert harness.engines() == [{"id": "a", "kind": "local", "priority": 1, "timeout_s": 5.0}]

    other = config_path.with_name("table.toml")
    write(other, '[engines]\nid = "a"\n')
    harness = await start(other, schema=DefaultedEngineSchema())
    # 写成了普通表：整节使用默认条目。
    assert harness.locations() == [("engines",)]
    assert harness.engines() == [{"id": "argos", "kind": "local", "priority": 1, "timeout_s": 5.0}]


async def test_missing_and_empty_list_sections(start: Launcher, config_path: Path) -> None:
    write(config_path, '[race]\nmode = "full"\n')
    harness = await start(config_path, schema=DefaultedEngineSchema())
    # 整节省略：使用默认条目。
    assert [item["id"] for item in harness.engines() if isinstance(item, dict)] == ["argos"]

    empty = config_path.with_name("empty.toml")
    write(empty, "engines = []\n")
    harness = await start(empty, schema=DefaultedEngineSchema())
    # 显式写成空数组：没有条目。
    assert harness.engines() == []
    assert harness.rejections == []


# ---------------------------------------------------------------------- 坏文件


async def test_syntax_error_preserves_file_and_uses_defaults(
    start: Launcher, config_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    broken = '[race\nmode = "full"\n'
    write(config_path, broken)
    caplog.set_level(logging.WARNING, logger=MODULE_LOGGER)

    harness = await start(config_path)

    assert {name: harness.section(name) for name in SECTIONS} == DEFAULTS
    assert harness.config.revision == 1
    # 坏文件改名保留，原路径写入一份默认文件。
    backups = sorted(config_path.parent.glob("settings.toml.broken-*"))
    assert len(backups) == 1
    assert read(backups[0]) == broken
    assert tomllib.loads(read(config_path)) == DEFAULT_FILE
    assert harness.locations() == [()]
    assert backups[0].name in harness.rejections[0].reason
    assert any(r.levelno == logging.ERROR and r.name == MODULE_LOGGER for r in caplog.records)
    assert harness.service.startup_rejections == tuple(harness.rejections)


async def test_repeated_breakage_never_overwrites_backups(start: Launcher, config_path: Path) -> None:
    write(config_path, "a = \n")
    await start(config_path)
    write(config_path, "b = \n")
    await start(config_path)

    backups = sorted(config_path.parent.glob("settings.toml.broken-*"))
    assert sorted(read(path) for path in backups) == ["a = \n", "b = \n"]


async def test_non_utf8_file_is_treated_as_broken(start: Launcher, config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    # 0xFF 不会出现在任何合法的 UTF-8 文本里。
    config_path.write_bytes(b'[race]\nmode = "\xff"\n')

    harness = await start(config_path)

    assert harness.locations() == [()]
    assert "UTF-8" in harness.rejections[0].reason
    assert len(list(config_path.parent.glob("settings.toml.broken-*"))) == 1


async def test_unreadable_file_uses_defaults_without_writing(start: Launcher, tmp_path: Path) -> None:
    # 路径是一个目录：读取失败，但不能当作“文件不存在”去覆盖它。
    path = tmp_path / "settings.toml"
    path.mkdir()

    harness = await start(path)

    assert {name: harness.section(name) for name in SECTIONS} == DEFAULTS
    assert harness.locations() == [()]
    assert path.is_dir()


# ---------------------------------------------------------------------- 规范 JSON 与构造检查


async def test_published_sections_parse_back_with_their_models(start: Launcher, config_path: Path) -> None:
    write(
        config_path, BAD_FIELDS + '\n[[engines]]\nid = "x"\nkind = "online"\npriority = 3\nbase_url = "u"\n'
    )

    harness = await start(config_path)

    for name, model in SECTIONS.items():
        section = harness.section(name)
        assert model.model_validate(section).model_dump(mode="json") == section
    schema = FakeEngineSchema()
    for item in harness.engines():
        assert isinstance(item, dict)
        assert schema.validate_item(item) == item
        kind = item["kind"]
        assert isinstance(kind, str)
        ENGINE_MODELS[kind].model_validate(item)


class NeedsValue(BaseModel):
    value: int


def test_constructor_rejects_models_without_defaults(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="默认值"):
        ConfigService(tmp_path / "s.toml", sections={"x": NeedsValue}, secrets=MemorySecretStore())


def test_constructor_rejects_overlapping_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="engines"):
        ConfigService(
            tmp_path / "s.toml",
            sections={"engines": SECTIONS["race"]},
            list_sections={"engines": FakeEngineSchema()},
            secrets=MemorySecretStore(),
        )


async def test_engine_helper_builds_valid_entries() -> None:
    schema = FakeEngineSchema()
    assert schema.validate_item(engine("a", 1))["timeout_s"] == 5.0
    assert schema.validate_item(engine("b", 2, kind="online", secret="k"))["secret"] == "k"
