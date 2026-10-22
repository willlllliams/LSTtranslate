"""引擎模块的测试：真实事件总线 → EnginesModule → EngineRoster 与 EngineStatuses。

API key 由内存里的 SecretReader 提供，它记录每次读取；网络引擎共用一个 httpx.AsyncClient，
传输层拒绝任何请求，测试不会访问网络；NMT 进程由假的 WorkerChannel 扮演，state 与 capabilities
可以随时修改。模块发布的每一条名册和状态都由 Published 收集，测试据此检查“是否重复发布”。
"""

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import pytest

from lst.contracts.config import AppConfig
from lst.contracts.errors import ErrorCode
from lst.contracts.events import (
    EngineHealth,
    EngineRoster,
    EngineStatus,
    EngineStatuses,
    WorkerStatus,
    WorkerStatuses,
)
from lst.contracts.ids import EngineId
from lst.contracts.messages import Event, State
from lst.contracts.ports import WorkerRequestHandle, WorkerState
from lst.contracts.translation import Engine, EngineKind
from lst.contracts.worker_protocol import JsonValue, OcrRequest, TranslateRequest
from lst.kernel import EventBus, Module, ModuleContext
from lst.services.engines import ENGINE_KINDS, EngineDeps, EngineListSchema, EnginesModule
from lst.services.engines.deepl import DeepLSettings
from lst.services.engines.registry import define_kind

MODULE_LOGGER = "lst.engines"
SCHEMA = EngineListSchema()
SECRET_VALUES = {"deepseek": "sk-live-DEEPSEEK-0001", "deepl": "deepl-key-0002", "google": "AIza-google-0003"}


def caps(fingerprint: str) -> dict[str, JsonValue]:
    return {"pairs": [["en", "zh-Hans"], ["zh-Hans", "en"]], "pivot": "en", "fingerprint": fingerprint}


# ---------------------------------------------------------------- 配置条目


def item(raw: Mapping[str, Any]) -> dict[str, JsonValue]:
    """经过校验模式的条目，与配置服务发布的规范 JSON 相同。"""
    return SCHEMA.validate_item(raw)


def argos(priority: int = 1, **extra: Any) -> dict[str, JsonValue]:
    return item({"kind": "local_nmt", "id": "argos", "name": "Argos", "priority": priority, **extra})


def deepseek(priority: int = 2, **extra: Any) -> dict[str, JsonValue]:
    return item(
        {
            "kind": "openai_compat",
            "id": "deepseek",
            "name": "DeepSeek",
            "priority": priority,
            "timeout_s": 60,
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-flash",
            "secret": "deepseek",
            **extra,
        }
    )


def ollama(priority: int = 6, **extra: Any) -> dict[str, JsonValue]:
    return item(
        {
            "kind": "openai_compat",
            "id": "ollama",
            "name": "Ollama",
            "priority": priority,
            "timeout_s": 60,
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "hy-mt2",
            **extra,
        }
    )


def deepl(priority: int = 3, **extra: Any) -> dict[str, JsonValue]:
    return item(
        {"kind": "deepl", "id": "deepl", "name": "DeepL", "priority": priority, "secret": "deepl", **extra}
    )


def google(priority: int = 4, **extra: Any) -> dict[str, JsonValue]:
    return item(
        {
            "kind": "google",
            "id": "google",
            "name": "Google",
            "priority": priority,
            "secret": "google",
            **extra,
        }
    )


def baidu(priority: int = 5, **extra: Any) -> dict[str, JsonValue]:
    raw = {"kind": "baidu", "id": "baidu", "name": "百度", "priority": priority, "appid": "2015063000000001"}
    return item({**raw, "secret": "baidu", **extra})


def config(
    *items: JsonValue, revision: int = 1, secrets_revision: int = 0, **sections: JsonValue
) -> AppConfig:
    engines: list[JsonValue] = list(items)
    return AppConfig(
        sections={"engines": engines, **sections}, revision=revision, secrets_revision=secrets_revision
    )


def nmt_status(state: WorkerState, detail: str = "") -> WorkerStatuses:
    return WorkerStatuses({"nmt": WorkerStatus(state, detail), "ocr": WorkerStatus(WorkerState.READY)})


# ---------------------------------------------------------------- 测试替身


class CountingSecrets:
    """内存里的 SecretReader，记录每次读取的条目名。"""

    def __init__(self, entries: Mapping[str, str]) -> None:
        self.entries = dict(entries)
        self.reads: list[str] = []
        self.threads: set[int] = set()

    def get_secret(self, name: str) -> str | None:
        self.reads.append(name)
        self.threads.add(threading.get_ident())
        return self.entries.get(name)


class FakeChannel:
    """假的 NMT 通道：state 与 capabilities 可以随时修改，不接受请求。"""

    def __init__(self, capabilities: Mapping[str, JsonValue] | None) -> None:
        self.state = WorkerState.STOPPED
        self.capabilities = capabilities

    def submit(
        self, request: TranslateRequest | OcrRequest, *, payload: bytes | None = None, timeout_s: float
    ) -> WorkerRequestHandle:
        raise AssertionError("引擎模块测试不应提交推理请求")


class Published:
    """收集总线上的 EngineRoster 与 EngineStatuses。在模块启动之前订阅，只收到模块发布的值。"""

    def __init__(self, bus: EventBus) -> None:
        self.rosters: list[EngineRoster] = []
        self.statuses: list[EngineStatuses] = []
        bus.subscribe(EngineRoster, self.rosters.append)
        bus.subscribe(EngineStatuses, self.statuses.append)

    @property
    def roster(self) -> EngineRoster:
        assert self.rosters, "模块还没有发布 EngineRoster"
        return self.rosters[-1]

    @property
    def status(self) -> EngineStatuses:
        assert self.statuses, "模块还没有发布 EngineStatuses"
        return self.statuses[-1]

    def ids(self) -> list[str]:
        return [engine.info.id for engine in self.roster.engines]

    def engine(self, engine_id: str, index: int = -1) -> Engine:
        found = [engine for engine in self.rosters[index].engines if engine.info.id == engine_id]
        assert len(found) == 1, f"名册里没有 {engine_id}"
        return found[0]

    def health(self, engine_id: str) -> EngineStatus:
        return self.status.statuses[EngineId(engine_id)]


def first_arg(record: logging.LogRecord) -> object:
    args = record.args
    assert isinstance(args, tuple) and args
    return args[0]


def reject(request: httpx.Request) -> httpx.Response:
    raise AssertionError("引擎模块测试不应发出 HTTP 请求")


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    instance = EventBus(asyncio.get_running_loop(), raise_errors=True)
    yield instance
    await instance.aclose()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    instance = httpx.AsyncClient(transport=httpx.MockTransport(reject))
    yield instance
    await instance.aclose()


@pytest.fixture
def secrets() -> CountingSecrets:
    return CountingSecrets(SECRET_VALUES)


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel(caps("models-v1"))


@pytest.fixture
def deps(secrets: CountingSecrets, client: httpx.AsyncClient, channel: FakeChannel) -> EngineDeps:
    return EngineDeps(secrets=secrets, client=client, nmt=channel)


@pytest.fixture
async def published(bus: EventBus) -> Published:
    # 总线只允许在事件循环线程订阅，所以这个夹具是协程。
    return Published(bus)


async def start_module(bus: EventBus, deps: EngineDeps, **kwargs: Any) -> tuple[EnginesModule, ModuleContext]:
    module = EnginesModule(deps, **kwargs)
    ctx = ModuleContext(bus, logging.getLogger(MODULE_LOGGER))
    await module.start(ctx)
    await bus.drain()
    return module, ctx


async def publish(bus: EventBus, *messages: Event | State) -> None:
    for message in messages:
        bus.publish(message)
    await bus.drain()


# ---------------------------------------------------------------- 名册的内容与顺序


async def test_roster_lists_enabled_instances_in_priority_order(
    bus: EventBus, deps: EngineDeps, published: Published, secrets: CountingSecrets
) -> None:
    secrets.entries["baidu"] = "baidu-key-0004"
    module = EnginesModule(deps)
    static_check: Module = module  # 静态检查：满足 Module 协议
    ctx = ModuleContext(bus, logging.getLogger(f"lst.{static_check.name}"))
    await module.start(ctx)

    # 列表顺序与优先级顺序不同，名册按优先级排列。
    await publish(bus, config(deepl(3), argos(1), deepseek(2, color="#4D6BFE"), baidu(5), google(4)))

    assert len(published.rosters) == 1
    assert published.ids() == ["argos", "deepseek", "deepl", "google", "baidu"]
    infos = [engine.info for engine in published.roster.engines]
    assert [info.priority for info in infos] == [1, 2, 3, 4, 5]
    assert [info.kind for info in infos] == [
        EngineKind.LOCAL_NMT,
        EngineKind.ONLINE_LLM,
        EngineKind.ONLINE_MT,
        EngineKind.ONLINE_MT,
        EngineKind.ONLINE_MT,
    ]
    assert [info.billing for info in infos] == [False, True, True, True, True]
    assert all(info.timeout_s is not None for info in infos)

    assert len(published.statuses) == 1
    statuses = published.status
    # 还没收到 WorkerStatuses：本地 NMT 显示加载中；网络引擎都有 key，显示就绪。
    assert statuses.statuses == {
        "argos": EngineStatus(EngineHealth.LOADING),
        "deepseek": EngineStatus(EngineHealth.READY),
        "deepl": EngineStatus(EngineHealth.READY),
        "google": EngineStatus(EngineHealth.READY),
        "baidu": EngineStatus(EngineHealth.READY),
    }
    assert statuses.names == {
        "deepl": "DeepL",
        "argos": "Argos",
        "deepseek": "DeepSeek",
        "baidu": "百度",
        "google": "Google",
    }
    assert statuses.colors == {"deepl": "", "argos": "", "deepseek": "#4D6BFE", "baidu": "", "google": ""}
    assert statuses.priorities == {"deepl": 3, "argos": 1, "deepseek": 2, "baidu": 5, "google": 4}
    # 计费标记只来自已经创建的实例，双语窗口的服务列表据此标出计费服务。
    assert statuses.billing == {
        "argos": False,
        "deepseek": True,
        "deepl": True,
        "google": True,
        "baidu": True,
    }

    await module.stop()
    ctx.close()


async def test_equal_priorities_are_ordered_by_list_position(
    bus: EventBus, deps: EngineDeps, published: Published
) -> None:
    # 配置服务保证优先级互异；万一相同，名册按列表顺序排列，结果仍是确定的。
    await start_module(bus, deps)
    await publish(bus, config(google(2), deepl(2), argos(1)))
    assert published.ids() == ["argos", "google", "deepl"]


async def test_nothing_is_published_before_config(
    bus: EventBus, deps: EngineDeps, published: Published
) -> None:
    await start_module(bus, deps)
    await publish(bus, nmt_status(WorkerState.READY))
    assert published.rosters == []
    assert published.statuses == []


async def test_config_without_engines_publishes_empty_roster(
    bus: EventBus, deps: EngineDeps, published: Published
) -> None:
    await start_module(bus, deps)
    await publish(bus, AppConfig(sections={"race": {"show_source_marker": {"overlay": True}}}, revision=1))
    assert published.roster.engines == ()
    assert published.status == EngineStatuses({}, {}, {}, {}, {})


async def test_disabled_instance_is_not_in_roster(
    bus: EventBus, deps: EngineDeps, published: Published
) -> None:
    await start_module(bus, deps)
    await publish(bus, config(argos(1), deepl(2, enabled=False, color="#0F2B46"), google(3)))

    assert published.ids() == ["argos", "google"]
    assert published.health("deepl") == EngineStatus(EngineHealth.DISABLED)
    # 未启用的实例也出现在名称与颜色里，设置窗口和双语窗口照常显示它。
    assert published.status.names[EngineId("deepl")] == "DeepL"
    assert published.status.colors == {"argos": "", "deepl": "#0F2B46", "google": ""}
    # 未启用的实例没有创建，计费标记里没有它。
    assert published.status.billing == {"argos": False, "google": True}


async def test_disabling_and_enabling_republishes(
    bus: EventBus, deps: EngineDeps, published: Published
) -> None:
    await start_module(bus, deps)
    await publish(bus, config(argos(1), deepl(2)))
    await publish(bus, config(argos(1), deepl(2, enabled=False), revision=2))
    assert published.ids() == ["argos"]
    await publish(bus, config(argos(1), deepl(2), revision=3))
    assert published.ids() == ["argos", "deepl"]
    assert len(published.rosters) == 3
    assert [status.statuses[EngineId("deepl")].health for status in published.statuses] == [
        EngineHealth.READY,
        EngineHealth.DISABLED,
        EngineHealth.READY,
    ]


# ---------------------------------------------------------------- API key


async def test_missing_key_reports_auth_error_without_secret_values(
    bus: EventBus,
    deps: EngineDeps,
    published: Published,
    secrets: CountingSecrets,
    caplog: pytest.LogCaptureFixture,
) -> None:
    del secrets.entries["deepl"]  # 配置了条目名，keyring 里没有
    secrets.entries["google"] = "   "  # 条目存在但只有空白
    with caplog.at_level(logging.DEBUG):
        await start_module(bus, deps)
        await publish(
            bus,
            config(
                deepseek(1),
                deepl(2),
                google(3),
                baidu(4, secret=None),  # 必须有 key，却没有配置条目名
                ollama(5),  # 本地服务不需要 key
                argos(6),
            ),
        )

    assert published.health("deepseek") == EngineStatus(EngineHealth.READY)
    assert published.health("ollama") == EngineStatus(EngineHealth.READY)
    for engine_id in ("deepl", "google", "baidu"):
        status = published.health(engine_id)
        assert status.health is EngineHealth.ERROR
        assert status.error_code is ErrorCode.AUTH
    # detail 指出缺少哪个条目名。
    assert "'deepl'" in published.health("deepl").detail
    assert "'google'" in published.health("google").detail
    assert "条目名" in published.health("baidu").detail

    # 缺 key 的实例照常进名册，它们的任务以 engine.auth 失败，原因在每个尝试上可见。
    assert published.ids() == ["deepseek", "deepl", "google", "baidu", "ollama", "argos"]

    # 状态与日志里都不出现任何 API key。
    details = " ".join(status.detail for status in published.status.statuses.values())
    for value in SECRET_VALUES.values():
        assert value not in details
        assert value not in caplog.text


async def test_secret_name_on_local_service_must_resolve(
    bus: EventBus, deps: EngineDeps, published: Published
) -> None:
    # 本地服务本身不要求 key；配置了条目名却读不到，说明用户想用 key，按缺少 key 报告。
    await start_module(bus, deps)
    await publish(bus, config(ollama(1, secret="llamacpp")))
    status = published.health("ollama")
    assert (status.health, status.error_code) == (EngineHealth.ERROR, ErrorCode.AUTH)
    assert "'llamacpp'" in status.detail


async def test_secrets_revision_change_rebuilds_instances(
    bus: EventBus, deps: EngineDeps, published: Published, secrets: CountingSecrets
) -> None:
    await start_module(bus, deps)
    await publish(bus, config(argos(1), deepl(2), google(3)))
    old_deepl, old_google, old_argos = (published.engine(name) for name in ("deepl", "google", "argos"))

    secrets.entries["deepl"] = "deepl-key-rotated"
    await publish(bus, config(argos(1), deepl(2), google(3), revision=2, secrets_revision=1))

    assert len(published.rosters) == 2
    new_deepl = published.engine("deepl")
    assert new_deepl is not old_deepl
    assert new_deepl.info.revision != old_deepl.info.revision
    # 只换 key 不影响译文，指纹不变，缓存仍然有效（架构文档 6.5 节第 4 条）。
    assert new_deepl.info.fingerprint == old_deepl.info.fingerprint
    # key 没变的网络引擎重新读取了 key，但 EngineInfo 相同，沿用原对象；本地 NMT 不读 key，原样保留。
    assert published.engine("google") is old_google
    assert published.engine("argos") is old_argos
    # 状态没有变化，不重复发布。
    assert len(published.statuses) == 1


async def test_secrets_revision_change_without_key_change_keeps_roster(
    bus: EventBus, deps: EngineDeps, published: Published, secrets: CountingSecrets
) -> None:
    await start_module(bus, deps)
    await publish(bus, config(deepl(1), google(2)))
    reads = len(secrets.reads)
    await publish(bus, config(deepl(1), google(2), revision=2, secrets_revision=1))
    assert len(secrets.reads) > reads  # 重新读取了 key
    assert len(published.rosters) == 1
    assert len(published.statuses) == 1


async def test_each_key_is_read_once_off_the_event_loop(
    bus: EventBus, deps: EngineDeps, published: Published, secrets: CountingSecrets
) -> None:
    """创建网络引擎实例时每个 keyring 条目只读一次，并且在线程池里读，不阻塞事件循环。"""
    await start_module(bus, deps)
    await publish(bus, config(argos(1), deepl(2), google(3), deepseek(4)))
    assert published.ids() == ["argos", "deepl", "google", "deepseek"]
    assert sorted(secrets.reads) == ["deepl", "deepseek", "google"]
    assert threading.get_ident() not in secrets.threads


async def test_added_key_clears_error(
    bus: EventBus, deps: EngineDeps, published: Published, secrets: CountingSecrets
) -> None:
    del secrets.entries["deepl"]
    await start_module(bus, deps)
    await publish(bus, config(deepl(1)))
    assert published.health("deepl").health is EngineHealth.ERROR

    secrets.entries["deepl"] = "deepl-key-new"
    await publish(bus, config(deepl(1), revision=2, secrets_revision=1))
    assert published.health("deepl") == EngineStatus(EngineHealth.READY)
    assert len(published.rosters) == 2
    assert published.engine("deepl", 1).info.revision != published.engine("deepl", 0).info.revision


# ---------------------------------------------------------------- 重复发布与增量重建


async def test_unchanged_config_is_not_republished(
    bus: EventBus, deps: EngineDeps, published: Published, secrets: CountingSecrets
) -> None:
    await start_module(bus, deps)
    await publish(bus, config(argos(1), deepseek(2), deepl(3)))
    reads = len(secrets.reads)
    roster = published.roster

    # 配置修订号变化、其他节变化，engines 节与 secrets_revision 不变。
    await publish(bus, config(argos(1), deepseek(2), deepl(3), revision=2))
    await publish(bus, config(argos(1), deepseek(2), deepl(3), revision=3, race={"mode": "full"}))

    assert published.rosters == [roster]
    assert len(published.statuses) == 1
    # 输入没变的实例不重建，也不重新读取 key。
    assert len(secrets.reads) == reads


async def test_changed_entry_rebuilds_only_that_instance(
    bus: EventBus, deps: EngineDeps, published: Published
) -> None:
    await start_module(bus, deps)
    await publish(bus, config(argos(1), deepseek(2), deepl(3)))
    old = {name: published.engine(name) for name in ("argos", "deepseek", "deepl")}

    # 调换 DeepSeek 与 DeepL 的优先级。
    await publish(bus, config(argos(1), deepseek(3), deepl(2), revision=2))
    assert len(published.rosters) == 2
    assert published.ids() == ["argos", "deepl", "deepseek"]
    assert published.engine("argos") is old["argos"]
    assert published.engine("deepl") is not old["deepl"]
    assert published.engine("deepl").info.priority == 2
    assert published.engine("deepseek").info.priority == 3

    # 删除一个实例：名册与状态里都不再有它。
    await publish(bus, config(argos(1), deepl(2), revision=3))
    assert published.ids() == ["argos", "deepl"]
    assert EngineId("deepseek") not in published.status.statuses
    assert EngineId("deepseek") not in published.status.names


async def test_name_change_republishes_statuses(
    bus: EventBus, deps: EngineDeps, published: Published
) -> None:
    await start_module(bus, deps)
    await publish(bus, config(deepl(1)))
    await publish(bus, config(deepl(1, name="DeepL Pro", color="#123456"), revision=2))
    assert len(published.statuses) == 2
    assert published.status.names == {"deepl": "DeepL Pro"}
    assert published.status.colors == {"deepl": "#123456"}
    # 名称与颜色进入 EngineInfo，实例随之替换。
    assert len(published.rosters) == 2
    assert published.engine("deepl").info.name == "DeepL Pro"


# ---------------------------------------------------------------- 本地 NMT


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (WorkerState.READY, EngineStatus(EngineHealth.READY)),
        (WorkerState.BUSY, EngineStatus(EngineHealth.BUSY)),
        (WorkerState.STARTING, EngineStatus(EngineHealth.LOADING, "说明")),
        (WorkerState.LOADING, EngineStatus(EngineHealth.LOADING, "说明")),
        (WorkerState.STOPPED, EngineStatus(EngineHealth.LOADING, "说明")),
        (WorkerState.CRASHED, EngineStatus(EngineHealth.ERROR, "说明", ErrorCode.WORKER_CRASHED)),
        (WorkerState.FAILED, EngineStatus(EngineHealth.ERROR, "说明", ErrorCode.WORKER_CRASHED)),
    ],
)
async def test_local_nmt_status_follows_worker(
    bus: EventBus, deps: EngineDeps, published: Published, state: WorkerState, expected: EngineStatus
) -> None:
    await start_module(bus, deps)
    await publish(bus, config(argos(1), deepl(2)))
    await publish(bus, nmt_status(state, "说明"))
    assert published.health("argos") == expected
    assert published.health("deepl") == EngineStatus(EngineHealth.READY)
    # NMT 进程的状态变化不影响名册。
    assert len(published.rosters) == 1


@pytest.mark.parametrize("state", [WorkerState.CRASHED, WorkerState.FAILED])
async def test_worker_error_without_detail_gets_default_detail(
    bus: EventBus, deps: EngineDeps, published: Published, state: WorkerState
) -> None:
    await start_module(bus, deps)
    await publish(bus, config(argos(1)), nmt_status(state))
    status = published.health("argos")
    assert (status.health, status.error_code) == (EngineHealth.ERROR, ErrorCode.WORKER_CRASHED)
    assert "NMT 进程" in status.detail


async def test_local_nmt_without_nmt_worker_status_is_loading(
    bus: EventBus, deps: EngineDeps, published: Published
) -> None:
    await start_module(bus, deps)
    await publish(bus, config(argos(1)), nmt_status(WorkerState.READY))
    assert published.health("argos") == EngineStatus(EngineHealth.READY)
    # 新的 WorkerStatuses 里没有 nmt：按加载中处理。
    await publish(bus, WorkerStatuses({"ocr": WorkerStatus(WorkerState.READY)}))
    assert published.health("argos") == EngineStatus(EngineHealth.LOADING)


async def test_worker_status_published_before_start_is_used(
    bus: EventBus, deps: EngineDeps, published: Published
) -> None:
    # 组合根先启动监管器与配置服务，引擎模块启动时两个状态都已存在，只发布一次状态。
    bus.publish(nmt_status(WorkerState.READY))
    bus.publish(config(argos(1), deepl(2)))
    await start_module(bus, deps)
    assert len(published.statuses) == 1
    assert published.health("argos") == EngineStatus(EngineHealth.READY)
    assert len(published.rosters) == 1


async def test_disabled_local_nmt_ignores_worker(
    bus: EventBus, deps: EngineDeps, published: Published
) -> None:
    await start_module(bus, deps)
    await publish(bus, config(argos(1, enabled=False)), nmt_status(WorkerState.CRASHED))
    assert published.health("argos") == EngineStatus(EngineHealth.DISABLED)
    assert published.roster.engines == ()


async def test_fingerprint_change_rebuilds_only_local_nmt(
    bus: EventBus, deps: EngineDeps, published: Published, channel: FakeChannel, secrets: CountingSecrets
) -> None:
    await start_module(bus, deps)
    await publish(bus, config(argos(1), deepseek(2), deepl(3)), nmt_status(WorkerState.READY))
    assert len(published.rosters) == 1  # 指纹与创建时相同，不重建
    old = {name: published.engine(name) for name in ("argos", "deepseek", "deepl")}
    reads = len(secrets.reads)

    # NMT 进程换了模型：重启过程中能力清单已经更新，但只在就绪时比较指纹。
    channel.capabilities = caps("models-v2")
    await publish(bus, nmt_status(WorkerState.LOADING))
    assert len(published.rosters) == 1
    await publish(bus, nmt_status(WorkerState.READY))

    assert len(published.rosters) == 2
    new_argos = published.engine("argos")
    assert new_argos is not old["argos"]
    assert new_argos.info.fingerprint != old["argos"].info.fingerprint
    # 其他实例是同一个对象，也没有重新读取 key。
    assert published.engine("deepseek") is old["deepseek"]
    assert published.engine("deepl") is old["deepl"]
    assert len(secrets.reads) == reads
    assert published.ids() == ["argos", "deepseek", "deepl"]

    # 再次就绪、指纹不变：不重建。
    await publish(bus, nmt_status(WorkerState.BUSY), nmt_status(WorkerState.READY))
    assert len(published.rosters) == 2


async def test_first_ready_after_start_rebuilds_local_nmt(
    bus: EventBus, client: httpx.AsyncClient, secrets: CountingSecrets, published: Published
) -> None:
    # NMT 进程从未就绪时能力清单为 None，实例的模型指纹按空串计算；首次就绪后带上真实指纹重建。
    channel = FakeChannel(None)
    await start_module(bus, EngineDeps(secrets=secrets, client=client, nmt=channel))
    await publish(bus, config(argos(1), deepl(2)), nmt_status(WorkerState.LOADING))
    before = published.engine("argos")
    deepl_before = published.engine("deepl")

    channel.capabilities = caps("models-v1")
    await publish(bus, nmt_status(WorkerState.READY))
    assert len(published.rosters) == 2
    assert published.engine("argos").info.fingerprint != before.info.fingerprint
    assert published.engine("deepl") is deepl_before


async def test_local_nmt_without_channel_is_error_and_not_in_roster(
    bus: EventBus, client: httpx.AsyncClient, secrets: CountingSecrets, published: Published
) -> None:
    await start_module(bus, EngineDeps(secrets=secrets, client=client, nmt=None))
    await publish(bus, config(argos(1), deepl(2)), nmt_status(WorkerState.READY))

    assert published.ids() == ["deepl"]
    status = published.health("argos")
    assert (status.health, status.error_code) == (EngineHealth.ERROR, ErrorCode.WORKER_CRASHED)
    assert "NMT" in status.detail
    assert published.status.names[EngineId("argos")] == "Argos"


# ---------------------------------------------------------------- 异常输入与生命周期


async def test_unparsable_entries_are_skipped_with_error_log(
    bus: EventBus, deps: EngineDeps, published: Published, caplog: pytest.LogCaptureFixture
) -> None:
    await start_module(bus, deps)
    bad_kind: dict[str, JsonValue] = {"kind": "online_llm", "id": "x", "name": "X", "priority": 7}
    bad_field: dict[str, JsonValue] = {**deepl(8), "id": "deepl-2", "priority": 0}
    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        await publish(bus, config(deepl(1), bad_kind, bad_field, "not an object", deepl(9), argos(2)))

    assert published.ids() == ["deepl", "argos"]
    assert published.engine("deepl").info.priority == 1  # 重复的 id 保留第一条
    assert set(published.status.statuses) == {"deepl", "argos"}
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 4
    # 每条日志的第一个参数是条目下标：未知 kind、字段不合法、不是对象、id 重复。
    assert [first_arg(record) for record in errors] == [1, 2, 3, 4]


async def test_engines_section_of_wrong_type_is_treated_as_empty(
    bus: EventBus, deps: EngineDeps, published: Published, caplog: pytest.LogCaptureFixture
) -> None:
    await start_module(bus, deps)
    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        await publish(bus, AppConfig(sections={"engines": {"id": "deepl"}}, revision=1))
    assert published.roster.engines == ()
    assert "engines 节不是数组" in caplog.text


async def test_factory_failure_marks_instance_error(
    bus: EventBus, deps: EngineDeps, published: Published, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(settings: DeepLSettings, deps: EngineDeps) -> Engine:
        raise RuntimeError("工厂内部错误")

    kinds = {
        **ENGINE_KINDS,
        "deepl": define_kind("deepl", DeepLSettings, explode, requires_key=lambda _: True),
    }
    await start_module(bus, deps, kinds=kinds)
    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        await publish(bus, config(argos(1), deepl(2)))

    assert published.ids() == ["argos"]
    status = published.health("deepl")
    assert (status.health, status.error_code) == (EngineHealth.ERROR, ErrorCode.INTERNAL)
    assert "RuntimeError" in status.detail
    assert any(record.exc_info is not None for record in caplog.records)


async def test_explicit_logger_is_used(
    bus: EventBus, deps: EngineDeps, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("tests.engines.explicit")
    await start_module(bus, deps, logger=logger)
    with caplog.at_level(logging.ERROR):
        await publish(bus, AppConfig(sections={"engines": "oops"}, revision=1))
    assert [record.name for record in caplog.records] == ["tests.engines.explicit"]


async def test_stop_stops_responding(
    bus: EventBus,
    deps: EngineDeps,
    published: Published,
    client: httpx.AsyncClient,
    channel: FakeChannel,
) -> None:
    module, ctx = await start_module(bus, deps)
    await publish(bus, config(argos(1), deepl(2)))
    assert len(published.rosters) == 1

    await module.stop()
    # 组合根先 stop 再取消订阅，这段时间里到达的消息不再处理。
    channel.capabilities = caps("models-v2")
    await publish(bus, config(deepl(1), revision=2), nmt_status(WorkerState.READY))
    assert len(published.rosters) == 1
    assert len(published.statuses) == 1
    # HTTP 客户端由组合根管理，模块不关闭它。
    assert not client.is_closed
    ctx.close()

    await publish(bus, config(revision=3))
    assert len(published.rosters) == 1


async def test_restart_after_stop_republishes(bus: EventBus, deps: EngineDeps, published: Published) -> None:
    module, ctx = await start_module(bus, deps)
    await publish(bus, config(argos(1), deepl(2)))
    await module.stop()
    ctx.close()

    module, ctx = await start_module(bus, deps)
    assert len(published.rosters) == 2
    assert published.ids() == ["argos", "deepl"]
    assert len(published.statuses) == 2
