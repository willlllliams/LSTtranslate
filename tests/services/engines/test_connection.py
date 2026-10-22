"""引擎连接测试：CheckEngineConnection → EnginesModule → EngineConnectionChecked（设置窗口的“测试连接”）。

用两类实例：真实的 DeepL 引擎，传输层是 httpx.MockTransport，不访问网络；假引擎，行为由测试指定。
API key 由内存里的 SecretReader 提供，测试检查它不出现在日志里。
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import pytest

from lst.contracts.config import AppConfig
from lst.contracts.errors import ErrorCode
from lst.contracts.events import CheckEngineConnection, EngineConnectionChecked
from lst.contracts.ids import EngineId, Lang, SegmentId
from lst.contracts.translation import Engine, EngineInfo, EngineKind, Job, SegmentOutcome
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus, ModuleContext
from lst.services.engines import ENGINE_KINDS, EngineDeps, EngineListSchema, EnginesModule
from lst.services.engines.common import EngineSettings
from lst.services.engines.registry import EngineKindSpec, define_kind

SCHEMA = EngineListSchema()
DEEPL_KEY = "deepl-secret-key-0042:fx"


class FakeSecrets:
    def __init__(self, entries: Mapping[str, str]) -> None:
        self.entries = dict(entries)

    def get_secret(self, name: str) -> str | None:
        return self.entries.get(name)


class FakeSettings(EngineSettings):
    """假引擎的配置：behavior 决定 translate 做什么。"""

    behavior: str = "ok"


class FakeEngine:
    def __init__(self, settings: FakeSettings) -> None:
        self._settings = settings
        self.closed = False
        self.jobs: list[Job] = []
        self._info = EngineInfo(
            id=EngineId(settings.id),
            name=settings.name,
            kind=EngineKind.ONLINE_MT,
            priority=settings.priority,
            billing=True,
            timeout_s=settings.timeout_s,
        )

    @property
    def info(self) -> EngineInfo:
        return self._info

    def supports(self, source: Lang, target: Lang) -> bool:
        if self._settings.behavior == "zh_only":
            return (source, target) == (Lang("zh-Hans"), Lang("en"))
        return self._settings.behavior != "unsupported"

    async def translate(self, job: Job) -> AsyncIterator[SegmentOutcome]:
        self.jobs.append(job)
        segment_id = job.segments[0].id
        try:
            match self._settings.behavior:
                case "fail":
                    yield SegmentOutcome(segment_id, error="HTTP 401: invalid key", code=ErrorCode.AUTH)
                case "raise":
                    raise RuntimeError("boom")
                case "hang":
                    await asyncio.sleep(60)
                case "silent":
                    return
                case _:
                    yield SegmentOutcome(segment_id, text="你好")
        finally:
            self.closed = True


def fake_spec() -> EngineKindSpec:
    built: list[FakeEngine] = []

    def factory(settings: FakeSettings, deps: EngineDeps) -> Engine:
        del deps
        engine = FakeEngine(settings)
        built.append(engine)
        return engine

    spec = define_kind("fake", FakeSettings, factory, requires_key=lambda _: False)
    spec_built[spec.kind] = built
    return spec


spec_built: dict[str, list[FakeEngine]] = {}


def fake(engine_id: str = "fake", behavior: str = "ok", **extra: Any) -> dict[str, JsonValue]:
    return SCHEMA_WITH_FAKE.validate_item(
        {
            "kind": "fake",
            "id": engine_id,
            "name": engine_id,
            "priority": extra.pop("priority", 1),
            "timeout_s": extra.pop("timeout_s", 5),
            "behavior": behavior,
            **extra,
        }
    )


KINDS: Mapping[str, EngineKindSpec] = {**ENGINE_KINDS, "fake": fake_spec()}
SCHEMA_WITH_FAKE = EngineListSchema(KINDS)


def deepl(priority: int = 2, **extra: Any) -> dict[str, JsonValue]:
    return SCHEMA.validate_item(
        {"kind": "deepl", "id": "deepl", "name": "DeepL", "priority": priority, "secret": "deepl", **extra}
    )


def config(*items: JsonValue, secrets_revision: int = 0) -> AppConfig:
    engines: list[JsonValue] = list(items)
    return AppConfig(sections={"engines": engines}, revision=1, secrets_revision=secrets_revision)


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    instance = EventBus(asyncio.get_running_loop(), raise_errors=True)
    yield instance
    await instance.aclose()


class Harness:
    def __init__(self, bus: EventBus, secrets: FakeSecrets, requests: list[httpx.Request]) -> None:
        self.bus = bus
        self.secrets = secrets
        self.requests = requests
        self.results: list[EngineConnectionChecked] = []
        bus.subscribe(EngineConnectionChecked, self.results.append)

    async def check(self, engine_id: str) -> EngineConnectionChecked:
        before = len(self.results)
        self.bus.send(CheckEngineConnection(EngineId(engine_id)))
        await self.bus.drain()
        assert len(self.results) == before + 1
        return self.results[-1]


@pytest.fixture
async def harness(bus: EventBus) -> AsyncIterator[Harness]:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"translations": [{"text": "你好"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    secrets = FakeSecrets({"deepl": DEEPL_KEY})
    module = EnginesModule(EngineDeps(secrets=secrets, client=client), kinds=KINDS)
    ctx = ModuleContext(bus, logging.getLogger("lst.engines"))
    await module.start(ctx)
    result = Harness(bus, secrets, requests)
    yield result
    await module.stop()
    ctx.close()
    await client.aclose()


async def publish_config(harness: Harness, *items: JsonValue) -> None:
    harness.bus.publish(config(*items))
    await harness.bus.drain()


async def test_real_engine_passes_and_reports_latency(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    await publish_config(harness, deepl())
    with caplog.at_level(logging.DEBUG):
        result = await harness.check("deepl")

    assert result.ok
    assert result.error_code is None
    assert result.latency_ms is not None and result.latency_ms >= 0
    # 请求确实发出了：一个很短的固定词，认证用的是配置的 key。
    assert len(harness.requests) == 1
    assert harness.requests[0].headers["Authorization"] == f"DeepL-Auth-Key {DEEPL_KEY}"
    # 日志里没有 API key。
    assert DEEPL_KEY not in caplog.text


async def test_missing_key_fails_without_sending_a_request(harness: Harness) -> None:
    harness.secrets.entries.clear()
    await publish_config(harness, deepl())

    result = await harness.check("deepl")

    assert not result.ok
    assert result.error_code is ErrorCode.AUTH
    assert result.latency_ms is None
    assert harness.requests == []


async def test_disabled_instance_can_be_checked(harness: Harness) -> None:
    await publish_config(harness, deepl(enabled=False))

    result = await harness.check("deepl")

    assert result.ok
    assert len(harness.requests) == 1


async def test_engine_failure_code_is_reported(harness: Harness) -> None:
    await publish_config(harness, fake("fake", "fail"))
    result = await harness.check("fake")
    assert not result.ok
    assert result.error_code is ErrorCode.AUTH
    assert result.latency_ms is not None


async def test_unknown_instance_is_an_internal_error(harness: Harness) -> None:
    await publish_config(harness, fake("fake"))
    result = await harness.check("nope")
    assert (result.ok, result.error_code) == (False, ErrorCode.INTERNAL)


async def test_unexpected_exception_is_an_internal_error(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    await publish_config(harness, fake("fake", "raise"))
    with caplog.at_level(logging.WARNING):
        result = await harness.check("fake")
    assert (result.ok, result.error_code) == (False, ErrorCode.INTERNAL)
    # 日志只有异常类型，没有异常消息。
    assert "RuntimeError" in caplog.text
    assert "boom" not in caplog.text


async def test_missing_result_is_reported(harness: Harness) -> None:
    await publish_config(harness, fake("fake", "silent"))
    result = await harness.check("fake")
    assert (result.ok, result.error_code) == (False, ErrorCode.MISSING_SEGMENT)


async def test_timeout_uses_the_instance_time_limit(harness: Harness) -> None:
    await publish_config(harness, fake("fake", "hang", timeout_s=0.05))
    result = await harness.check("fake")
    assert (result.ok, result.error_code) == (False, ErrorCode.TIMEOUT)
    assert result.latency_ms is not None and result.latency_ms < 2000
    engine = spec_built["fake"][-1]
    assert engine.closed  # 超时后异步生成器被关闭，在线引擎借此中断连接


async def test_unsupported_language_pair(harness: Harness) -> None:
    await publish_config(harness, fake("fake", "unsupported"))
    result = await harness.check("fake")
    assert (result.ok, result.error_code) == (False, ErrorCode.UNSUPPORTED_LANGUAGE)
    assert result.latency_ms is None


async def test_falls_back_to_the_reverse_pair(harness: Harness) -> None:
    await publish_config(harness, fake("fake", "zh_only"))
    result = await harness.check("fake")
    assert result.ok
    job = spec_built["fake"][-1].jobs[-1]
    assert (job.source_lang, job.target_lang) == (Lang("zh-Hans"), Lang("en"))
    assert [segment.id for segment in job.segments] == [SegmentId("0")]


@pytest.mark.parametrize(
    ("engine_id", "reply", "path"),
    [
        ("google_free", ["你好"], "/translate_a/t"),
        (
            "microsoft_free",
            [{"translations": [{"text": "你好", "to": "zh-Hans"}]}],
            "/translate/translatetext",
        ),
    ],
)
async def test_free_endpoints_can_be_checked_while_disabled(
    bus: EventBus, engine_id: str, reply: JsonValue, path: str
) -> None:
    """默认列表里未启用的免费接口实例也能测试连接：不读 key，发一次请求，用固定的短词。"""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=reply)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    module = EnginesModule(EngineDeps(secrets=FakeSecrets({}), client=client), kinds=KINDS)
    ctx = ModuleContext(bus, logging.getLogger("lst.engines"))
    await module.start(ctx)
    try:
        harness = Harness(bus, FakeSecrets({}), requests)
        defaults = SCHEMA.default_items()
        assert all(item["enabled"] is False for item in defaults if item["id"] == engine_id)
        await publish_config(
            harness, *[item for item in defaults if item["kind"] in ("google_free", "microsoft_free")]
        )

        result = await harness.check(engine_id)

        assert result.ok and result.error_code is None
        assert len(requests) == 1 and requests[0].url.path == path
        assert b"Hello" in requests[0].content
    finally:
        await module.stop()
        ctx.close()
        await client.aclose()
