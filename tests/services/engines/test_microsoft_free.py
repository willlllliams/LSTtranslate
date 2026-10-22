"""微软免费翻译引擎（Edge 浏览器的 translatetext 接口）的契约测试。

覆盖请求构造（JSON 字符串数组、HTML 转义）、响应解析与实体解码、语言码映射、批量上限、
错误映射（429、5xx、401/403、结构不符、网络）、隐私与取消。全部请求走 httpx.MockTransport。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import cast

import httpx
import pydantic
import pytest

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.translation import Engine, EngineKind
from lst.services.engines.http import EngineError
from lst.services.engines.microsoft_free import MicrosoftFreeSettings, create_microsoft_free_engine
from lst.services.engines.microsoft_free.engine import (
    DEFAULT_ENDPOINT,
    decode_text,
    escape_text,
    parse_response,
)
from lst.services.engines.microsoft_free.languages import CODES, microsoft_code
from tests.services.engines.mt_helpers import (
    Recorder,
    Sleeps,
    assert_all_failed,
    assert_private,
    cancel_while_pending,
    capture_all,
    collect,
    hanging,
    make_job,
    texts_by_id,
)

# 结构不符合预期的响应，以及请求的段数。
BAD_SHAPES: list[tuple[object, int]] = [
    ({"error": {"code": 1}}, 1),
    ([], 1),
    ([{"translations": []}], 1),
    ([{"translations": [{"to": "en"}]}], 1),
    ([{"translations": [{"text": "一"}]}], 2),
    ("一", 1),
]


def settings(**overrides: object) -> MicrosoftFreeSettings:
    fields: dict[str, object] = {"id": "microsoft_free", "name": "微软翻译（免费）", "priority": 3}
    return MicrosoftFreeSettings.model_validate({**fields, **overrides})


def ok(*texts: str, to: str = "zh-Hans") -> httpx.Response:
    return httpx.Response(200, json=[{"translations": [{"text": text, "to": to}]} for text in texts])


def array_body(request: httpx.Request) -> list[str]:
    value: object = json.loads(request.content)
    assert isinstance(value, list)
    return [str(item) for item in cast(list[object], value)]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
async def client(recorder: Recorder) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        yield client


def engine_for(client: httpx.AsyncClient, sleeps: Sleeps, **overrides: object) -> Engine:
    return create_microsoft_free_engine(settings(**overrides), client, sleep=sleeps, rand=lambda: 0.5)


class TestRequestShape:
    async def test_one_post_with_an_array_body(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好", "世界"))
        outcomes = await collect(engine_for(client, sleeps), make_job("Hello", "World"))

        assert texts_by_id(outcomes) == {"s1": "你好", "s2": "世界"}
        assert recorder.calls == 1
        request = recorder.last
        assert request.method == "POST"
        assert str(request.url).split("?")[0] == DEFAULT_ENDPOINT
        assert list(request.url.params.multi_items()) == [
            ("from", "en"),
            ("to", "zh-Hans"),
            ("isEnterpriseClient", "false"),
        ]
        assert request.headers["content-type"] == "application/json"
        assert array_body(request) == ["Hello", "World"]
        assert "authorization" not in request.headers  # 不需要 key，也不取 token
        assert "/translate/auth" not in str(request.url)

    async def test_html_characters_are_escaped_and_decoded(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("若 a &lt; b &amp;&amp; c &gt; d，则 &quot;是&quot; &#39;x&#39; &#x4E2D;"))
        outcomes = await collect(engine_for(client, sleeps), make_job("if a < b && c > d"))
        assert array_body(recorder.last) == ["if a &lt; b &amp;&amp; c &gt; d"]
        assert texts_by_id(outcomes) == {"s1": "若 a < b && c > d，则 \"是\" 'x' 中"}

    def test_escape_round_trip(self) -> None:
        for text in ("a<b>c", "&amp; literal", "&lt;", "<<&>>", "plain"):
            assert decode_text(escape_text(text)) == text
        assert escape_text("&<>") == "&amp;&lt;&gt;"

    async def test_text_only_in_body(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("机密"))
        await collect(engine_for(client, sleeps), make_job("TOP-SECRET-TEXT"))
        assert "TOP-SECRET-TEXT" not in str(recorder.last.url)

    async def test_blank_segments_are_not_sent(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好"))
        outcomes = await collect(engine_for(client, sleeps), make_job(" ", "Hello"))
        assert texts_by_id(outcomes) == {"s1": " ", "s2": "你好"}
        assert array_body(recorder.last) == ["Hello"]

    async def test_custom_endpoint(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps, endpoint="https://edge.example/t"), make_job("Hello"))
        assert str(recorder.last.url).startswith("https://edge.example/t?from=en&to=zh-Hans")


class TestResponse:
    def test_parse(self) -> None:
        body = json.dumps([{"translations": [{"text": "一", "to": "zh-Hans"}]}]).encode()
        assert parse_response(body, 1) == ["一"]

    @pytest.mark.parametrize(
        ("payload", "expected"),
        BAD_SHAPES,
    )
    def test_bad_shapes(self, payload: object, expected: int) -> None:
        with pytest.raises(EngineError) as info:
            parse_response(json.dumps(payload).encode(), expected)
        assert info.value.code is ErrorCode.BAD_RESPONSE

    def test_not_json(self) -> None:
        with pytest.raises(EngineError) as info:
            parse_response(b"<html></html>", 1)
        assert info.value.code is ErrorCode.BAD_RESPONSE

    async def test_count_mismatch_fails_the_batch(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("一"))
        job = make_job("A", "B")
        assert_all_failed(await collect(engine_for(client, sleeps), job), job, ErrorCode.BAD_RESPONSE)


class TestErrors:
    async def test_429_is_retried_then_rate_limited(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(httpx.Response(429), httpx.Response(429))
        job = make_job("A")
        assert_all_failed(await collect(engine_for(client, sleeps), job), job, ErrorCode.RATE_LIMITED)
        assert recorder.calls == 2

    @pytest.mark.parametrize("status", [500, 503])
    async def test_5xx_is_server(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, status: int
    ) -> None:
        recorder.queue(httpx.Response(status), ok("好"))
        outcomes = await collect(engine_for(client, sleeps), make_job("A"))
        assert texts_by_id(outcomes) == {"s1": "好"}  # 重试一次后成功
        recorder.queue(httpx.Response(status))
        job = make_job("A")
        assert_all_failed(
            await collect(engine_for(client, sleeps, max_retries=0), job), job, ErrorCode.SERVER
        )

    @pytest.mark.parametrize("status", [401, 403])
    async def test_forbidden_is_bad_request_not_auth(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, status: int
    ) -> None:
        recorder.queue(httpx.Response(status, json={"error": {"code": 401000, "message": "denied"}}))
        job = make_job("A")
        outcomes = await collect(engine_for(client, sleeps), job)
        assert_all_failed(outcomes, job, ErrorCode.BAD_REQUEST)
        assert recorder.calls == 1
        assert outcomes[0].error is not None and "denied" in outcomes[0].error

    async def test_network_error(self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps) -> None:
        recorder.queue(httpx.ConnectError("refused"))
        job = make_job("A")
        assert_all_failed(
            await collect(engine_for(client, sleeps, max_retries=0), job), job, ErrorCode.NETWORK
        )

    async def test_unsupported_target(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        job = make_job("A", target="xx")
        assert_all_failed(await collect(engine_for(client, sleeps), job), job, ErrorCode.UNSUPPORTED_LANGUAGE)
        assert recorder.calls == 0

    async def test_logs_hold_no_text(
        self,
        client: httpx.AsyncClient,
        recorder: Recorder,
        sleeps: Sleeps,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        capture_all(caplog)
        recorder.queue(httpx.Response(502, text="echo PRIVATE-TEXT"))
        await collect(engine_for(client, sleeps, max_retries=0), make_job("PRIVATE-TEXT"))
        assert_private(caplog, "PRIVATE-TEXT")

    async def test_cancel_propagates(self) -> None:
        started = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.MockTransport(hanging(started))) as async_client:
            engine = create_microsoft_free_engine(settings(), async_client)
            assert await cancel_while_pending(engine, make_job("A"), started) == []


class TestLanguagesAndLimits:
    def test_codes(self) -> None:
        assert microsoft_code("zh-Hans") == "zh-Hans"
        assert microsoft_code("zh-Hant") == "zh-Hant"
        assert microsoft_code("ko") == "ko"
        assert microsoft_code("nb") is None
        assert len(CODES) == 20

    def test_supports(self, client: httpx.AsyncClient, sleeps: Sleeps) -> None:
        engine = engine_for(client, sleeps)
        assert engine.supports(Lang("ja"), Lang("zh-Hans"))
        assert engine.supports(Lang("zh-Hant"), Lang("zh-Hans"))
        assert not engine.supports(Lang("ja"), Lang("ja"))
        assert not engine.supports(Lang("en"), Lang("he"))

    async def test_source_code_mapped(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("Hi", to="en"))
        await collect(engine_for(client, sleeps), make_job("你好", source="zh-Hant", target="en"))
        assert recorder.last.url.params["from"] == "zh-Hant" and recorder.last.url.params["to"] == "en"

    def test_info_limits(self, client: httpx.AsyncClient, sleeps: Sleeps) -> None:
        info = engine_for(client, sleeps).info
        assert (info.max_batch_segments, info.max_batch_chars) == (100, 5000)
        assert info.kind is EngineKind.ONLINE_MT and not info.billing

    @pytest.mark.parametrize(
        "overrides",
        [{"max_batch_segments": 101}, {"max_batch_chars": 5001}, {"endpoint": "edge.microsoft.com"}],
    )
    def test_settings_reject_out_of_range(self, overrides: dict[str, object]) -> None:
        with pytest.raises(pydantic.ValidationError):
            settings(**overrides)
