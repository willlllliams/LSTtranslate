"""网络引擎共用 HTTP 工具的测试。"""

import httpx
import pytest

from lst.contracts.errors import ErrorCode
from lst.services.engines.http import (
    EngineError,
    RateLimiter,
    RetryPolicy,
    aiter_sse_data,
    backoff_delay,
    classify_status,
    parse_retry_after,
    send_with_retry,
)

URL = "https://api.example.test/v1/translate?key=SECRET"


class Script:
    """按顺序返回预设响应的 MockTransport 处理函数，并记录请求次数。"""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def client_for(script: Script) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(script))


def build() -> httpx.Request:
    return httpx.Request("POST", URL, json={"q": "hello"})


async def send(script: Script, sleeps: Sleeps, policy: RetryPolicy | None = None) -> httpx.Response:
    async with client_for(script) as client:
        return await send_with_retry(client, build, policy=policy, sleep=sleeps, rand=lambda: 0.5)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, ErrorCode.BAD_REQUEST),
        (401, ErrorCode.AUTH),
        (402, ErrorCode.QUOTA),
        (403, ErrorCode.AUTH),
        (404, ErrorCode.BAD_REQUEST),
        (429, ErrorCode.RATE_LIMITED),
        (456, ErrorCode.QUOTA),
        (500, ErrorCode.SERVER),
        (503, ErrorCode.SERVER),
    ],
)
def test_classify_status(status: int, code: ErrorCode) -> None:
    assert classify_status(status) is code


class TestSendWithRetry:
    async def test_success_without_retry(self) -> None:
        script, sleeps = Script(httpx.Response(200, json={"ok": True})), Sleeps()
        response = await send(script, sleeps)
        assert response.json() == {"ok": True}
        assert (script.calls, sleeps.delays) == (1, [])

    async def test_retries_once_on_server_error(self) -> None:
        script, sleeps = Script(httpx.Response(503), httpx.Response(200, text="ok")), Sleeps()
        response = await send(script, sleeps)
        assert response.text == "ok"
        assert script.calls == 2
        assert sleeps.delays == [0.5]  # base 0.5 × 2^0 × (0.5 + 0.5)

    async def test_gives_up_after_one_retry(self) -> None:
        script, sleeps = Script(httpx.Response(503), httpx.Response(502)), Sleeps()
        with pytest.raises(EngineError) as info:
            await send(script, sleeps)
        assert (info.value.code, info.value.status, script.calls) == (ErrorCode.SERVER, 502, 2)

    async def test_auth_error_is_not_retried(self) -> None:
        script, sleeps = Script(httpx.Response(401, json={"error": {"message": "invalid key"}})), Sleeps()
        with pytest.raises(EngineError) as info:
            await send(script, sleeps)
        assert info.value.code is ErrorCode.AUTH
        assert info.value.detail == "HTTP 401: invalid key"
        assert script.calls == 1

    async def test_quota_is_not_retried(self) -> None:
        script, sleeps = Script(httpx.Response(456)), Sleeps()
        with pytest.raises(EngineError) as info:
            await send(script, sleeps)
        assert (info.value.code, script.calls) == (ErrorCode.QUOTA, 1)

    async def test_short_retry_after_is_honored(self) -> None:
        script = Script(httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200))
        sleeps = Sleeps()
        await send(script, sleeps)
        assert sleeps.delays == [2.0]

    async def test_long_retry_after_gives_up_immediately(self) -> None:
        script, sleeps = Script(httpx.Response(429, headers={"Retry-After": "30"})), Sleeps()
        with pytest.raises(EngineError) as info:
            await send(script, sleeps)
        assert info.value.code is ErrorCode.RATE_LIMITED
        assert (script.calls, sleeps.delays) == (1, [])

    async def test_network_error_retried_then_reported(self) -> None:
        script, sleeps = Script(httpx.ConnectError("refused"), httpx.ConnectError("again")), Sleeps()
        with pytest.raises(EngineError) as info:
            await send(script, sleeps)
        assert (info.value.code, script.calls) == (ErrorCode.NETWORK, 2)

    async def test_timeout_maps_to_timeout_code(self) -> None:
        script, sleeps = Script(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")), Sleeps()
        with pytest.raises(EngineError) as info:
            await send(script, sleeps)
        assert info.value.code is ErrorCode.TIMEOUT

    async def test_network_error_without_transport_retry(self) -> None:
        script, sleeps = Script(httpx.ConnectError("x")), Sleeps()
        with pytest.raises(EngineError):
            await send(script, sleeps, RetryPolicy(retry_transport_errors=False))
        assert script.calls == 1

    async def test_error_detail_never_contains_url_or_body(self) -> None:
        body = '{"message": "bad", "echo": "用户原文"}'
        script, sleeps = Script(httpx.Response(400, text=body)), Sleeps()
        with pytest.raises(EngineError) as info:
            await send(script, sleeps)
        detail = info.value.detail
        assert detail == "HTTP 400: bad"
        assert "SECRET" not in detail
        assert "用户原文" not in detail

    async def test_non_json_error_body_gives_status_only(self) -> None:
        script, sleeps = Script(httpx.Response(500, text="<html>oops</html>"), httpx.Response(500)), Sleeps()
        with pytest.raises(EngineError) as info:
            await send(script, sleeps)
        assert info.value.detail == "HTTP 500"


def test_backoff_grows_and_is_capped() -> None:
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=3.0)

    def rand() -> float:
        return 0.5

    assert backoff_delay(0, policy, None, rand) == 1.0
    assert backoff_delay(1, policy, None, rand) == 2.0
    assert backoff_delay(2, policy, None, rand) == 3.0


def test_parse_retry_after() -> None:
    assert parse_retry_after("3") == 3.0
    # 2026-10-21 10:00:05 UTC 对应的时间戳是 1792576805。
    assert parse_retry_after("Wed, 21 Oct 2026 10:00:05 GMT", now=1792576800.0) == pytest.approx(5.0)
    assert parse_retry_after("soon") is None


class TestSse:
    async def collect(self, body: str) -> list[str]:
        script = Script(httpx.Response(200, text=body))
        async with client_for(script) as client, client.stream("GET", URL) as response:
            return [d async for d in aiter_sse_data(response)]

    async def test_events_comments_and_done(self) -> None:
        body = (
            ": keep-alive\n\n"
            'data: {"a":1}\n\n'
            "event: x\nid: 7\n"
            'data: {"b":2}\n\n'
            "data: [DONE]\n\n"
            "data: ignored\n\n"
        )
        assert await self.collect(body) == ['{"a":1}', '{"b":2}']

    async def test_multiline_data_and_no_space_after_colon(self) -> None:
        assert await self.collect("data:line1\ndata: line2\n\n") == ["line1\nline2"]

    async def test_trailing_event_without_blank_line(self) -> None:
        assert await self.collect('data: {"last":true}') == ['{"last":true}']


class TestRateLimiter:
    async def test_spacing_follows_rate(self) -> None:
        now = [0.0]
        waits: list[float] = []

        async def sleep(delay: float) -> None:
            waits.append(delay)
            now[0] += delay

        limiter = RateLimiter(2.0, clock=lambda: now[0], sleep=sleep)
        for _ in range(3):
            await limiter.acquire()
        # 第一次用掉初始令牌，之后每次等 1/rate 秒。
        assert waits == [pytest.approx(0.5), pytest.approx(0.5)]

    def test_rejects_bad_parameters(self) -> None:
        with pytest.raises(ValueError):
            RateLimiter(0)
