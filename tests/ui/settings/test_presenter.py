"""展示器的测试：状态读取、命令发送、ConfigRejected 记录、引擎列表修改、API key 与连接测试。

展示器不依赖 Qt。这里借用 ``harness``（设置窗口模块加命令记录器）取得展示器与总线，
配置由测试直接发布，设置窗口发出的命令由记录器接收，所以每条断言检查的都是“发出了什么命令”。
"""

import logging

import pytest

from lst.contracts.config import ConfigRejected, UpdateConfig, UpdateSecret
from lst.contracts.errors import ErrorCode
from lst.contracts.events import (
    CacheCleared,
    CheckEngineConnection,
    ClearCache,
    EngineConnectionChecked,
    EngineHealth,
    EngineStatus,
    EngineStatuses,
    ResetStats,
)
from lst.contracts.ids import EngineId
from lst.contracts.models import (
    CancelModelDownload,
    DeleteModel,
    DownloadModels,
    ModelCatalog,
    RefreshModelCatalog,
)
from lst.contracts.worker_protocol import JsonValue
from lst.ui import i18n
from lst.ui.settings.presenter import API_KEY_FIELD, CheckState, KeyState, SettingsPresenter, Topic
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import default_config, default_engines, engine

KEY = "sk-live-SECRET-0123456789"


def engines_of(update: UpdateConfig) -> list[dict[str, JsonValue]]:
    value = update.patch["engines"]
    assert isinstance(value, list)
    return [item for item in value if isinstance(item, dict)]


def statuses(**health: EngineHealth | EngineStatus) -> EngineStatuses:
    items = {EngineId(k): v if isinstance(v, EngineStatus) else EngineStatus(v) for k, v in health.items()}
    return EngineStatuses(items, {k: str(k).title() for k in items}, {})


class Topics:
    def __init__(self, presenter: SettingsPresenter) -> None:
        self.seen: list[Topic] = []
        presenter.subscribe(self.seen.append)


async def test_values_come_from_the_config_with_model_defaults(harness: Harness) -> None:
    presenter = harness.presenter
    assert presenter.value("race", ("mode",)) == "saver"  # 默认值
    assert presenter.value("race", ("show_source_marker", "bilingual")) is True
    assert presenter.value("nope", ("x",)) is None  # 没有这一节
    assert presenter.value("race", ("nope",)) is None
    await harness.publish(default_config({"race": {"mode": "full"}}, revision=2))
    assert presenter.value("race", ("mode",)) == "full"


async def test_set_value_sends_a_field_level_patch(harness: Harness) -> None:
    harness.presenter.set_value("race", ("mode",), "full")
    harness.presenter.set_value("race", ("show_source_marker", "bilingual"), False)
    await harness.flush()
    assert [command.patch for command in harness.recorded.updates] == [
        {"race": {"mode": "full"}},
        {"race": {"show_source_marker": {"bilingual": False}}},
    ]


async def test_rejection_is_recorded_per_location_and_cleared_on_edit(harness: Harness) -> None:
    presenter = harness.presenter
    topics = Topics(presenter)
    await harness.publish(ConfigRejected(("race", "mode"), "不是可选值"))
    assert presenter.rejection_for(("race", "mode")) == "不是可选值"
    assert presenter.rejection_for(("race",)) == "不是可选值"  # 更粗的位置也能看到
    assert presenter.rejection_for(("race", "show_source_marker")) is None
    assert topics.seen == [Topic.REJECTIONS]

    presenter.set_value("race", ("mode",), "full")  # 用户换了一个值：提示消失
    assert presenter.rejection_for(("race", "mode")) is None
    assert presenter.rejections == {}


async def test_clear_rejection_of_unrelated_location_does_nothing(harness: Harness) -> None:
    topics = Topics(harness.presenter)
    await harness.publish(ConfigRejected(("race", "mode"), "x"))
    topics.seen.clear()
    harness.presenter.clear_rejection(("ocr", "mode"))
    assert topics.seen == []


async def test_engine_rejection_location_uses_the_instance_id(harness: Harness) -> None:
    presenter = harness.presenter
    # 发送时 deepseek 在下标 1；之后列表重排，下标会变，所以位置要换算成实例号。
    assert presenter.set_engine_field("deepseek", "name", "新名字")
    await harness.flush()
    await harness.publish(ConfigRejected(("engines", 1, "timeout_s"), "太大"))
    assert presenter.rejection_for(("engines", "deepseek", "timeout_s")) == "太大"


async def test_secret_rejection_is_shown_at_the_api_key_card(harness: Harness) -> None:
    await harness.publish(ConfigRejected(("secrets", "deepseek"), "写入密钥失败"))
    assert harness.presenter.rejection_for(("engines", "deepseek", API_KEY_FIELD)) == "写入密钥失败"


async def test_set_engine_field_sends_the_whole_array_with_one_change(harness: Harness) -> None:
    assert harness.presenter.set_engine_field("deepseek", "model", "deepseek-v4-pro")
    await harness.flush()
    await harness.flush()
    (update,) = harness.recorded.updates
    entries = engines_of(update)
    assert [e["id"] for e in entries] == ["argos", "deepseek", "deepl", "ollama"]
    assert [e["priority"] for e in entries] == [1, 2, 3, 4]
    assert entries[1]["model"] == "deepseek-v4-pro"
    assert entries[0]["name"] == "Argos"  # 其他实例原样不动


async def test_invalid_engine_field_is_not_sent_so_the_instance_survives(harness: Harness) -> None:
    """配置服务会把不合法的条目整条丢弃；界面先校验，不合法就不发送。"""
    presenter = harness.presenter
    assert not presenter.set_engine_field("deepseek", "base_url", "ftp://nope")
    await harness.flush()
    await harness.flush()
    assert harness.recorded.updates == []
    assert "http" in (presenter.rejection_for(("engines", "deepseek", "base_url")) or "")
    # 改成合法值：提示消失，修改发出。
    assert presenter.set_engine_field("deepseek", "base_url", "https://api.deepseek.com/v1")
    await harness.flush()
    assert presenter.rejection_for(("engines", "deepseek", "base_url")) is None
    assert len(harness.recorded.updates) == 1


async def test_unchanged_engine_field_sends_nothing(harness: Harness) -> None:
    assert harness.presenter.set_engine_field("deepseek", "model", "deepseek-flash")
    await harness.flush()
    assert not harness.presenter.set_engine_field("nope", "model", "x")
    await harness.flush()
    assert harness.recorded.updates == []


async def test_consecutive_edits_build_on_the_unconfirmed_draft(harness: Harness) -> None:
    presenter = harness.presenter
    presenter.set_engine_field("deepseek", "name", "A")
    await harness.flush()
    presenter.set_engine_field("deepl", "name", "B")  # 配置服务还没回传第一次修改
    await harness.flush()
    first, second = harness.recorded.updates
    assert engines_of(first)[1]["name"] == "A"
    assert engines_of(second)[1]["name"] == "A" and engines_of(second)[2]["name"] == "B"

    # 配置回传了与草稿相同的列表：草稿作废，之后的修改基于配置。
    await harness.publish(default_config(engines=[*map(dict, engines_of(second))]))  # type: ignore[arg-type]
    presenter.set_engine_field("argos", "name", "C")
    await harness.flush()
    third = harness.recorded.updates[-1]
    assert [e["name"] for e in engines_of(third)] == ["C", "A", "B", "Ollama"]


async def test_engine_rejection_discards_the_draft(harness: Harness) -> None:
    presenter = harness.presenter
    presenter.set_engine_field("deepseek", "name", "A")
    await harness.flush()
    await harness.publish(ConfigRejected(("engines", 1, "name"), "被拒绝"))
    presenter.set_engine_field("deepl", "name", "B")
    await harness.flush()
    assert engines_of(harness.recorded.updates[-1])[1]["name"] == "DeepSeek"  # 没有带上被拒绝的修改


async def test_reorder_renumbers_priorities_from_one(harness: Harness) -> None:
    assert harness.presenter.reorder_engines(["ollama", "argos", "deepl", "deepseek"])
    await harness.flush()
    (update,) = harness.recorded.updates
    assert [(e["id"], e["priority"]) for e in engines_of(update)] == [
        ("ollama", 1),
        ("argos", 2),
        ("deepl", 3),
        ("deepseek", 4),
    ]


async def test_reorder_with_same_order_or_wrong_ids_sends_nothing(harness: Harness) -> None:
    topics = Topics(harness.presenter)
    assert not harness.presenter.reorder_engines(["argos", "deepseek", "deepl", "ollama"])
    await harness.flush()
    assert topics.seen == []
    assert not harness.presenter.reorder_engines(["argos", "deepseek"])
    await harness.flush()
    assert topics.seen == [Topic.ENGINES]  # 视图的顺序与配置对不上：让视图回到配置里的顺序
    assert harness.recorded.updates == []


async def test_reorder_numbers_gaps_away(harness: Harness) -> None:
    gaps = [engine("local_nmt", "a", 1), engine("deepl", "b", 5), engine("google", "c", 9)]
    await harness.publish(default_config(engines=gaps, revision=2))
    assert harness.presenter.reorder_engines(["c", "b", "a"])
    await harness.flush()
    assert [e["priority"] for e in engines_of(harness.recorded.updates[-1])] == [1, 2, 3]


async def test_add_engine_appends_with_new_priority_and_secret_name(harness: Harness) -> None:
    errors = harness.presenter.add_engine("baidu", {"name": "百度翻译", "appid": "20150630"}, api_key=KEY)
    await harness.flush()
    assert errors == {}
    assert harness.presenter.pending_select == "engine"  # 中文名生成的实例号
    # 先写密钥再改配置：配置引用密钥时密钥已经存在。
    assert [type(c) for c in harness.recorded.commands] == [UpdateSecret, UpdateConfig]
    secret, update = harness.recorded.commands
    assert isinstance(secret, UpdateSecret) and isinstance(update, UpdateConfig)
    assert secret.name == "engine" and secret.value == KEY
    entries = engines_of(update)
    assert len(entries) == 5 and entries[-1]["priority"] == 5
    assert entries[-1]["secret"] == "engine" and entries[-1]["appid"] == "20150630"


async def test_add_engine_without_key_and_keyless_kind(harness: Harness) -> None:
    assert harness.presenter.add_engine("deepl", {"name": "DeepL B"}) == {}
    await harness.flush()
    assert harness.recorded.secrets == []
    assert engines_of(harness.recorded.updates[-1])[-1]["id"] == "deepl-b"
    harness.recorded.clear()
    assert harness.presenter.add_engine("local_nmt", {"name": "NMT 2"}, api_key=KEY) == {}
    await harness.flush()
    assert harness.recorded.secrets == []  # 本地 NMT 不需要 key，给了也不写
    assert engines_of(harness.recorded.updates[-1])[-1]["secret"] is None


async def test_add_engine_reports_errors_and_sends_nothing(harness: Harness) -> None:
    errors = harness.presenter.add_engine("baidu", {"name": "百度"})
    await harness.flush()
    assert list(errors) == ["appid"]
    assert harness.recorded.commands == []
    assert harness.presenter.check_new_engine("baidu", {"name": "百度", "appid": "1"}) == {}


async def test_remove_engine_renumbers_and_deletes_its_key(harness: Harness) -> None:
    assert harness.presenter.remove_engine("deepseek")
    await harness.flush()
    update, secret = harness.recorded.commands
    assert isinstance(update, UpdateConfig) and isinstance(secret, UpdateSecret)
    assert [(e["id"], e["priority"]) for e in engines_of(update)] == [
        ("argos", 1),
        ("deepl", 2),
        ("ollama", 3),
    ]
    assert (secret.name, secret.value) == ("deepseek", None)
    assert not harness.presenter.remove_engine("nope")
    await harness.flush()


async def test_remove_keeps_a_key_still_used_by_another_instance(harness: Harness) -> None:
    shared = [engine("deepl", "a", 1, secret="shared"), engine("deepl", "b", 2, secret="shared")]
    await harness.publish(default_config(engines=shared, revision=2))
    assert harness.presenter.remove_engine("a")
    await harness.flush()
    assert harness.recorded.secrets == []
    # 草稿里只剩 b，再删 b 时没有别的实例引用这个条目，密钥随之删除。
    assert harness.presenter.remove_engine("b")
    await harness.flush()
    assert [(c.name, c.value) for c in harness.recorded.secrets] == [("shared", None)]


async def test_removing_the_last_engines_sends_an_empty_array(harness: Harness) -> None:
    await harness.publish(default_config(engines=[engine("local_nmt", "argos", 1)], revision=2))
    assert harness.presenter.remove_engine("argos")
    await harness.flush()
    assert harness.recorded.updates[-1].patch == {"engines": []}


async def test_key_state_follows_engine_status(harness: Harness) -> None:
    presenter = harness.presenter
    assert presenter.key_state("argos") is KeyState.NOT_NEEDED
    assert presenter.key_state("nope") is KeyState.NOT_NEEDED
    assert presenter.key_state("ollama") is KeyState.UNSET  # 没有条目名
    assert presenter.key_optional("ollama") and not presenter.key_optional("deepseek")

    no_key = EngineStatus(EngineHealth.ERROR, "", ErrorCode.AUTH)
    await harness.publish(statuses(deepseek=no_key, deepl=EngineHealth.READY))
    assert presenter.key_state("deepseek") is KeyState.UNSET
    assert presenter.key_state("deepl") is KeyState.SET
    await harness.publish(statuses(deepseek=EngineHealth.READY, deepl=EngineHealth.DISABLED))
    assert presenter.key_state("deepseek") is KeyState.SET
    assert presenter.key_state("deepl") is KeyState.UNKNOWN  # 没有启用的实例不读取 key


async def test_set_api_key_goes_through_update_secret_only(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        assert harness.presenter.set_api_key("deepseek", KEY)
        await harness.flush()
        await harness.flush()
    (secret,) = harness.recorded.secrets
    assert (secret.name, secret.value) == ("deepseek", KEY)
    assert harness.recorded.updates == []  # 实例已有条目名，配置不用改
    assert KEY not in caplog.text
    assert KEY not in repr(secret)
    assert harness.presenter.key_state("deepl") is not KeyState.NOT_NEEDED


async def test_set_api_key_assigns_a_secret_name_when_missing(harness: Harness) -> None:
    assert harness.presenter.set_api_key("ollama", KEY)
    await harness.flush()
    (secret,) = harness.recorded.secrets
    assert secret.name == "ollama"
    entries = engines_of(harness.recorded.updates[-1])
    assert entries[3]["secret"] == "ollama"


async def test_set_api_key_ignores_blank_and_keyless(harness: Harness) -> None:
    assert not harness.presenter.set_api_key("deepseek", "   ")
    await harness.flush()
    assert not harness.presenter.set_api_key("argos", KEY)
    await harness.flush()
    assert not harness.presenter.set_api_key("nope", KEY)
    await harness.flush()
    assert harness.recorded.commands == []


async def test_clear_api_key(harness: Harness) -> None:
    presenter = harness.presenter
    await harness.publish(statuses(deepseek=EngineHealth.READY))
    assert presenter.clear_api_key("deepseek")
    await harness.flush()
    (secret,) = harness.recorded.secrets
    assert (secret.name, secret.value) == ("deepseek", None)
    assert presenter.key_state("deepseek") is KeyState.UNSET
    assert not presenter.clear_api_key("ollama")  # 没有条目名


async def test_connection_check_round_trip(harness: Harness) -> None:
    presenter = harness.presenter
    topics = Topics(presenter)
    presenter.check_connection("deepseek")
    await harness.flush()
    await harness.flush()
    assert harness.recorded.of(CheckEngineConnection) == [CheckEngineConnection(EngineId("deepseek"))]
    result = presenter.connection("deepseek")
    assert result is not None and result.state is CheckState.CHECKING

    await harness.publish(EngineConnectionChecked(EngineId("deepseek"), ok=True, latency_ms=123.0))
    result = presenter.connection("deepseek")
    assert result is not None and (result.state, result.latency_ms) == (CheckState.OK, 123.0)
    await harness.publish(EngineConnectionChecked(EngineId("deepseek"), ok=False, error_code=ErrorCode.AUTH))
    result = presenter.connection("deepseek")
    assert result is not None and (result.state, result.error_code) == (CheckState.FAILED, ErrorCode.AUTH)
    assert topics.seen.count(Topic.CONNECTION) == 3


async def test_stats_cache_and_model_commands(harness: Harness) -> None:
    presenter = harness.presenter
    presenter.reset_stats()
    presenter.clear_cache()
    presenter.refresh_models()
    presenter.download_models(["a", "b"])
    presenter.download_models([])
    presenter.cancel_download("a")
    presenter.delete_model("b")
    await harness.flush()
    assert harness.recorded.commands == [
        ResetStats(),
        ClearCache(),
        RefreshModelCatalog(),
        DownloadModels(("a", "b")),
        CancelModelDownload("a"),
        DeleteModel("b"),
    ]
    topics = Topics(presenter)
    await harness.publish(CacheCleared(7))
    assert presenter.last_cache_cleared == 7 and topics.seen == [Topic.CACHE]


async def test_models_page_fetches_the_index_once_and_only_when_needed(harness: Harness) -> None:
    presenter = harness.presenter
    presenter.models_page_opened()
    presenter.models_page_opened()
    await harness.flush()
    assert harness.recorded.of(RefreshModelCatalog) == [RefreshModelCatalog()]


async def test_models_page_does_not_fetch_when_the_index_is_loaded(
    start_settings: StartSettings,
) -> None:
    harness = await start_settings()
    await harness.publish(ModelCatalog(index_loaded=True))
    harness.presenter.models_page_opened()
    await harness.flush()
    assert harness.recorded.of(RefreshModelCatalog) == []


async def test_language_follows_the_config_and_notifies(harness: Harness) -> None:
    topics = Topics(harness.presenter)
    await harness.publish(default_config({"ui": {"language": "en"}}, revision=2))
    assert i18n.current_language() == "en"
    assert Topic.LANGUAGE in topics.seen
    topics.seen.clear()
    await harness.publish(default_config({"ui": {"language": "en"}}, revision=3))
    assert Topic.LANGUAGE not in topics.seen  # 语言没变不通知


async def test_closed_presenter_ignores_everything(harness: Harness) -> None:
    presenter = harness.presenter
    presenter.close()
    presenter.set_value("race", ("mode",), "full")
    presenter.reset_stats()
    assert not presenter.set_engine_field("deepseek", "name", "x")
    await harness.flush()
    assert harness.recorded.commands == []


async def test_default_engines_fixture_is_valid() -> None:
    assert [e["id"] for e in default_engines()] == ["argos", "deepseek", "deepl", "ollama"]
