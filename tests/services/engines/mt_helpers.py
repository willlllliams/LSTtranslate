"""DeepL、Google、百度三个引擎的测试共用的替身与工具。

所有请求都由 httpx.MockTransport 交给 Recorder，Recorder 按顺序返回预设响应并记录请求，测试不访问网络。
等待用 Sleeps 记录而不真正等待；需要验证时间间隔时，Sleeps 把等待累加到 FakeClock 上。
"""

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine, Iterable
from typing import cast
from urllib.parse import parse_qsl

import httpx
import pytest

from lst.contracts.ids import EngineId, Lang, SegmentId, SessionId
from lst.contracts.translation import Engine, Job, Segment, SegmentOutcome

Reply = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


class FakeSecrets:
    """内存里的 SecretReader。"""

    def __init__(self, entries: dict[str, str] | None = None) -> None:
        self.entries = dict(entries or {})

    def get_secret(self, name: str) -> str | None:
        return self.entries.get(name)


class Recorder:
    """MockTransport 的处理函数：按顺序返回预设响应，并记录收到的请求。

    预设项可以是响应、要抛出的异常，或者根据请求生成响应的函数。预设用完后再收到请求时测试失败。
    """

    def __init__(self, *replies: Reply) -> None:
        self.replies: list[Reply] = list(replies)
        self.requests: list[httpx.Request] = []

    def queue(self, *replies: Reply) -> None:
        self.replies.extend(replies)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("收到了预期之外的请求")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return reply(request)

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Sleeps:
    """记录等待时长的假 sleep。给了 clock 时，每次等待把时钟向前推进相应的秒数。"""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.delays: list[float] = []
        self.clock = clock

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if self.clock is not None:
            self.clock.now += delay


def make_job(*texts: str, source: str = "en", target: str = "zh-Hans", context: str = "") -> Job:
    """按顺序生成段号为 s1、s2……的任务。"""
    segments = tuple(Segment(SegmentId(f"s{index}"), text) for index, text in enumerate(texts, start=1))
    return Job(SessionId("sess"), EngineId("engine"), segments, Lang(source), Lang(target), context)


async def collect(engine: Engine, job: Job) -> list[SegmentOutcome]:
    return [outcome async for outcome in engine.translate(job)]


def texts_by_id(outcomes: Iterable[SegmentOutcome]) -> dict[str, str | None]:
    return {outcome.segment_id: outcome.text for outcome in outcomes}


def assert_all_failed(outcomes: list[SegmentOutcome], job: Job, code: object) -> None:
    """任务的每个段恰好产出一次失败结果，错误码都是 code。"""
    assert sorted(outcome.segment_id for outcome in outcomes) == sorted(s.id for s in job.segments)
    for outcome in outcomes:
        assert outcome.text is None
        assert outcome.error
        assert outcome.code == code


def json_body(request: httpx.Request) -> dict[str, object]:
    return cast(dict[str, object], json.loads(request.content))


def form_body(request: httpx.Request) -> dict[str, str]:
    """解析 application/x-www-form-urlencoded 请求体；同名字段出现多次时测试失败。"""
    pairs = parse_qsl(request.content.decode("ascii"), keep_blank_values=True, strict_parsing=True)
    fields = dict(pairs)
    assert len(fields) == len(pairs)
    return fields


def hanging(started: asyncio.Event) -> Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]:
    """一直不返回的异步处理函数，用来测试取消。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    return handler


async def cancel_while_pending(engine: Engine, job: Job, started: asyncio.Event) -> list[SegmentOutcome]:
    """在 started 置位后取消正在执行的 translate，断言 CancelledError 上抛，返回取消前产出的结果。"""
    produced: list[SegmentOutcome] = []

    async def consume() -> None:
        async for outcome in engine.translate(job):
            produced.append(outcome)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return produced


def assert_private(caplog: pytest.LogCaptureFixture, *secrets: str) -> None:
    """日志里有记录（说明确实捕获到了），并且任何一条都不包含 secrets 中的字符串。"""
    assert caplog.records, "没有捕获到任何日志，隐私断言没有意义"
    text = caplog.text + "\n".join(record.getMessage() for record in caplog.records)
    for secret in secrets:
        assert secret not in text, f"日志中出现了不应出现的内容：{secret!r}"


def capture_all(caplog: pytest.LogCaptureFixture) -> None:
    """在根日志器上捕获 DEBUG 及以上的全部日志，包括 httpx 自己的请求日志。"""
    caplog.set_level(logging.DEBUG)
