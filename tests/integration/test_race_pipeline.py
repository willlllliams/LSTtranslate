"""M2 端到端集成测试：真实引擎 + 调度器 + 缓存 + 事件总线 + 统计。

四个真实引擎（DeepSeek、DeepL、Google、百度）连接同一个模拟 HTTP 服务端，放进同一场竞速，验证：
- 各引擎都满足调度器使用的 Engine 协议，请求能被正确构造、响应能被正确解析；
- 显示按需求文档 5.5 节的方式逐级升级，最终停在优先级最高的实例；
- 缓存命中让最高优先级实例耗时 0 胜出，其余实例被剪枝、不发请求；
- 调度器的输出经事件总线到达统计模块，统计口径与隐私要求成立。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from lst.contracts.events import AttemptRecorded, SegmentsUpdated, SessionProgress
from lst.contracts.ids import EngineId, Entry, Lang, SegmentId, SessionId
from lst.contracts.translation import AttemptView, Engine, Segment, SegmentView
from lst.kernel import EventBus, ModuleContext
from lst.services.cache import SqliteTranslationCache
from lst.services.engines.baidu import BaiduSettings, create_baidu_engine
from lst.services.engines.deepl import DeepLSettings, create_deepl_engine
from lst.services.engines.google import GoogleSettings, create_google_engine
from lst.services.engines.openai_compat import OpenAICompatSettings, create_openai_compat_engine
from lst.services.engines.openai_compat.prompt import CONTEXT_SEPARATOR
from lst.services.race import RaceScheduler
from lst.services.stats import SqliteStatsStore, StatsModule

EN, ZH = Lang("en"), Lang("zh-Hans")
DEEPSEEK, DEEPL, GOOGLE, BAIDU = (EngineId(x) for x in ("deepseek", "deepl", "google", "baidu"))
SECRETS = {
    "deepseek": "sk-deepseek-SECRET",
    "deepl": "deepl-SECRET:fx",
    "google": "google-SECRET",
    "baidu": "baidu-SECRET",
}
SOURCE_TEXTS = ("The quick brown fox.", "Jumps over the lazy dog.")


class Secrets:
    def get_secret(self, name: str) -> str | None:
        return SECRETS.get(name)


class FakeServers:
    """一个 MockTransport 处理函数，按主机名扮演四个服务。每个服务都有一个闸门，测试放行后才返回。"""

    def __init__(self) -> None:
        self.gates = {host: asyncio.Event() for host in ("deepseek", "deepl", "google", "baidu")}
        self.requests: dict[str, int] = dict.fromkeys(self.gates, 0)
        self.unauthorized: set[str] = set()  # 这些服务对任何请求都返回 401

    def release_all(self) -> None:
        for gate in self.gates.values():
            gate.set()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.deepseek.com":
            return await self._serve("deepseek", lambda: self._deepseek(request))
        if host == "api-free.deepl.com":
            return await self._serve("deepl", lambda: self._deepl(request))
        if host == "translation.googleapis.com":
            return await self._serve("google", lambda: self._google(request))
        if host == "fanyi-api.baidu.com":
            return await self._serve("baidu", lambda: self._baidu(request))
        raise AssertionError(f"unexpected host {host}")

    async def _serve(self, name: str, respond: Callable[[], httpx.Response]) -> httpx.Response:
        self.requests[name] += 1
        if name in self.unauthorized:
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})
        await self.gates[name].wait()
        return respond()

    @staticmethod
    def _deepseek(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        assert body["response_format"] == {"type": "json_object"}
        assert body["thinking"] == {"type": "disabled"}
        user = body["messages"][-1]["content"]
        batch = user.rpartition(CONTEXT_SEPARATOR)[2]
        segments = json.loads(batch[batch.index('{"segments"') :])["segments"]
        reply = json.dumps({"r": [{"i": s["i"], "t": f"DS:{s['t']}"} for s in segments]})
        chunks = [reply[i : i + 7] for i in range(0, len(reply), 7)]  # 切成小块，走增量解析
        events = "".join(
            "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": c}}]}) + "\n\n"
            for c in chunks
        )
        return httpx.Response(
            200, text=events + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )

    @staticmethod
    def _deepl(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "DeepL-Auth-Key deepl-SECRET:fx"
        body = json.loads(request.content)
        assert body["target_lang"] == "ZH-HANS"
        return httpx.Response(
            200,
            json={
                "translations": [{"detected_source_language": "EN", "text": f"DL:{t}"} for t in body["text"]]
            },
        )

    @staticmethod
    def _google(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "google-SECRET"
        assert "key=" not in str(request.url)
        body = json.loads(request.content)
        assert body["target"] == "zh-CN"
        return httpx.Response(
            200, json={"data": {"translations": [{"translatedText": f"GG:{q}"} for q in body["q"]]}}
        )

    @staticmethod
    def _baidu(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert (form["from"], form["to"]) == ("en", "zh")
        lines = form["q"].split("\n")
        return httpx.Response(
            200,
            json={"from": "en", "to": "zh", "trans_result": [{"src": q, "dst": f"BD:{q}"} for q in lines]},
        )


class BusSink:
    """把调度器的输出适配成总线消息，这是 services.race 模块落地后的职责（架构文档 5.3 节）。"""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.views: list[SegmentView] = []
        self.changed = asyncio.Event()

    def segments_updated(self, session_id: SessionId, views: tuple[SegmentView, ...]) -> None:
        self.views.extend(views)
        self.changed.set()
        self.bus.publish(SegmentsUpdated(session_id, views))

    def session_progress(self, session_id: SessionId, finished: int, total: int) -> None:
        self.bus.publish(SessionProgress(session_id, finished, total))

    def attempt_recorded(self, session_id: SessionId, segment_id: SegmentId, attempt: AttemptView) -> None:
        self.bus.publish(AttemptRecorded(session_id, segment_id, attempt))

    def shown_engines(self, segment_id: SegmentId) -> list[EngineId | None]:
        """该段每次显示变化后的胜出者序列（去掉连续重复）。"""
        seq: list[EngineId | None] = []
        for view in self.views:
            if view.segment_id == segment_id and (not seq or seq[-1] != view.shown_engine):
                seq.append(view.shown_engine)
        return seq

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        async with asyncio.timeout(5):
            while not predicate():
                self.changed.clear()
                await self.changed.wait()


def make_engines(client: httpx.AsyncClient) -> list[Engine]:
    secrets = Secrets()

    async def no_sleep(_: float) -> None:
        return None

    return [
        create_openai_compat_engine(
            OpenAICompatSettings(
                id="deepseek",
                name="DeepSeek Flash",
                priority=1,
                timeout_s=10,
                secret="deepseek",
                provider="deepseek",
                base_url="https://api.deepseek.com",
                model="deepseek-flash",
            ),
            secrets,
            client,
        ),
        create_deepl_engine(
            DeepLSettings(id="deepl", name="DeepL", priority=2, secret="deepl"),
            secrets,
            client,
            sleep=no_sleep,
        ),
        create_google_engine(
            GoogleSettings(id="google", name="Google", priority=3, secret="google"),
            secrets,
            client,
            sleep=no_sleep,
        ),
        create_baidu_engine(
            BaiduSettings(id="baidu", name="百度", priority=4, secret="baidu", appid="20260101000001"),
            secrets,
            client,
            sleep=no_sleep,
            salt=lambda: "1435660288",
        ),
    ]


Harness = tuple[FakeServers, BusSink, RaceScheduler, SqliteStatsStore, EventBus]


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    servers = FakeServers()
    client = httpx.AsyncClient(transport=httpx.MockTransport(servers))
    bus = EventBus(asyncio.get_running_loop(), raise_errors=True)
    store = SqliteStatsStore(tmp_path / "stats.sqlite3")
    stats = StatsModule(store)
    ctx = ModuleContext(bus, logging.getLogger("test.stats"))
    await stats.start(ctx)
    cache = SqliteTranslationCache(tmp_path / "cache.sqlite3")
    await cache.open()
    sink = BusSink(bus)
    scheduler = RaceScheduler(make_engines(client), sink, cache=cache, frame_interval=0.005)
    yield servers, sink, scheduler, store, bus
    await scheduler.aclose()
    await bus.drain()
    await stats.stop()
    ctx.close()
    await cache.close()
    await client.aclose()


def segments(prefix: str) -> list[Segment]:
    return [Segment(SegmentId(f"{prefix}{i}"), text) for i, text in enumerate(SOURCE_TEXTS, start=1)]


async def run_session(scheduler: RaceScheduler, name: str) -> list[Segment]:
    segs = segments(name)
    await scheduler.start_session(
        Entry.SCREENSHOT,
        SessionId(name),
        segs,
        {s.id: (EN, ZH) for s in segs},
        context=" ".join(SOURCE_TEXTS),
    )
    return segs


async def test_display_upgrades_by_priority_then_cache_short_circuits(
    harness: Harness, tmp_path: Path
) -> None:
    servers, sink, scheduler, store, bus = harness
    segs = await run_session(scheduler, "first")
    first = segs[0].id

    # 按优先级从低到高依次放行：百度(4) → Google(3) → DeepL(2) → DeepSeek(1)，显示逐级升级。
    order: list[tuple[str, EngineId, str]] = [
        ("baidu", BAIDU, "BD:"),
        ("google", GOOGLE, "GG:"),
        ("deepl", DEEPL, "DL:"),
        ("deepseek", DEEPSEEK, "DS:"),
    ]
    for host, engine_id, _ in order:
        servers.gates[host].set()
        await sink.wait_for(
            lambda e=engine_id: any(v.segment_id == first and v.shown_engine == e for v in sink.views)
        )
    await scheduler.drain()
    await bus.drain()

    assert sink.shown_engines(first) == [None, BAIDU, GOOGLE, DEEPL, DEEPSEEK]
    final = {v.segment_id: v for v in sink.views}
    for seg in segs:
        view = final[seg.id]
        assert (view.shown_engine, view.finished, view.display_text) == (DEEPSEEK, True, f"DS:{seg.text}")
        assert {a.engine_id for a in view.attempts} == {DEEPSEEK, DEEPL, GOOGLE, BAIDU}
    # 每个引擎对两段只发了一次批量请求。
    assert servers.requests == {"deepseek": 1, "deepl": 1, "google": 1, "baidu": 1}

    # 第二个会话翻译同样的文本：DeepSeek 命中缓存、耗时 0 胜出，其余实例被即时剪枝，一个请求都不发。
    servers.release_all()
    second = await run_session(scheduler, "second")
    await scheduler.drain()
    await bus.drain()
    assert servers.requests == {"deepseek": 1, "deepl": 1, "google": 1, "baidu": 1}
    for seg in second:
        view = final_view(sink, seg.id)
        assert (view.shown_engine, view.display_text) == (DEEPSEEK, f"DS:{seg.text}")
        top = next(a for a in view.attempts if a.engine_id == DEEPSEEK)
        assert (top.cached, top.latency_ms) == (True, 0.0)

    # 统计：第一个会话每个实例执行了 2 段；缓存命中与剪枝都不计入；胜出全部归 DeepSeek 的真实执行。
    summary = {s.engine_id: s for s in await store.summary()}
    assert {e: summary[e].executed for e in (DEEPSEEK, DEEPL, GOOGLE, BAIDU)} == {
        DEEPSEEK: 2,
        DEEPL: 2,
        GOOGLE: 2,
        BAIDU: 2,
    }
    assert summary[DEEPSEEK].wins == 2
    assert all(summary[e].failure_rate == 0.0 for e in (DEEPSEEK, DEEPL, GOOGLE, BAIDU))

    # 隐私：统计库文件里没有原文、译文和任何 key。
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("stats.sqlite3*"))
    for needle in (*SOURCE_TEXTS, "DS:", "BD:", *SECRETS.values()):
        assert needle.encode() not in raw


def final_view(sink: BusSink, segment_id: SegmentId) -> SegmentView:
    return [v for v in sink.views if v.segment_id == segment_id][-1]


async def test_failing_top_engine_does_not_block_race(harness: Harness) -> None:
    """最高优先级实例返回 401 时，竞速在次高的实例返回后结束（架构文档 6.3 节“活性”）。"""
    servers, sink, scheduler, _store, bus = harness
    servers.unauthorized.add("deepseek")
    servers.release_all()
    segs = await run_session(scheduler, "auth")
    await scheduler.drain()
    await bus.drain()

    for seg in segs:
        view = final_view(sink, seg.id)
        assert (view.shown_engine, view.finished) == (DEEPL, True)
        top = next(a for a in view.attempts if a.engine_id == DEEPSEEK)
        assert top.error_code is not None and top.error_code.value == "engine.auth"
        assert top.error is not None and "SECRET" not in top.error
    assert servers.requests["deepseek"] == 1  # 401 不重试
