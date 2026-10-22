"""谷歌免费翻译引擎（client=gtx 接口）的契约测试。

覆盖请求构造（每段一个 q 参数、共用一次请求）、三种批量响应形状、逐段回退、语言码映射、
批量上限、错误映射（429、5xx、401/403、结构不符、网络）、隐私与取消。全部请求走 httpx.MockTransport。
"""

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import parse_qsl

import httpx
import pydantic
import pytest

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.translation import Engine, EngineKind
from lst.services.engines.google_free import GoogleFreeSettings, create_google_free_engine
from lst.services.engines.google_free.engine import (
    DEFAULT_ENDPOINT,
    FALLBACK_CONCURRENCY,
    parse_batch_response,
    parse_single_response,
)
from lst.services.engines.google_free.languages import CODES, google_code
from lst.services.engines.http import EngineError
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

BATCH_URL = f"{DEFAULT_ENDPOINT}/translate_a/t"
# 形状或条数与请求对不上的批量响应，以及请求的段数。
MISMATCHED: list[tuple[object, int]] = [
    ({"x": 1}, 1),
    (["一"], 2),
    ("一", 2),
    ([1, 2], 2),
    ([[]], 1),
    ([[None, "en"]], 1),
]
SINGLE_URL = f"{DEFAULT_ENDPOINT}/translate_a/single"


def settings(**overrides: object) -> GoogleFreeSettings:
    fields: dict[str, object] = {"id": "google_free", "name": "谷歌翻译（免费）", "priority": 2}
    return GoogleFreeSettings.model_validate({**fields, **overrides})


def ok(*texts: str) -> httpx.Response:
    """显式给出 sl 时的批量响应：译文字符串数组。"""
    return httpx.Response(200, json=list(texts))


def single(*sentences: str) -> httpx.Response:
    """translate_a/single 的响应：第一层是各分句 [译文, 原文, ...]。"""
    return httpx.Response(200, json=[[[text, "src", None, None, 10] for text in sentences], None, "en"])


def q_values(request: httpx.Request) -> list[str]:
    """表单请求体里全部 q 参数，按出现顺序。"""
    pairs = parse_qsl(request.content.decode("ascii"), keep_blank_values=True, strict_parsing=True)
    assert all(name == "q" for name, _ in pairs)
    return [value for _, value in pairs]


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
    return create_google_free_engine(settings(**overrides), client, sleep=sleeps, rand=lambda: 0.5)


class TestRequestShape:
    async def test_one_post_with_one_q_per_segment(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好", "世界", "甲&乙"))
        job = make_job("Hello", "World", "a & b = c?")
        outcomes = await collect(engine_for(client, sleeps), job)

        assert texts_by_id(outcomes) == {"s1": "你好", "s2": "世界", "s3": "甲&乙"}
        assert recorder.calls == 1
        request = recorder.last
        assert request.method == "POST"
        assert str(request.url).split("?")[0] == BATCH_URL
        assert list(request.url.params.multi_items()) == [
            ("client", "gtx"),
            ("sl", "en"),
            ("tl", "zh-CN"),
            ("format", "text"),
        ]
        assert request.headers["content-type"] == "application/x-www-form-urlencoded;charset=UTF-8"
        assert q_values(request) == ["Hello", "World", "a & b = c?"]
        assert "authorization" not in request.headers  # 不需要 key

    async def test_text_only_in_body(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("机密"))
        await collect(engine_for(client, sleeps), make_job("TOP-SECRET-TEXT"))
        assert "TOP-SECRET-TEXT" not in str(recorder.last.url)

    async def test_custom_endpoint(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps, endpoint="https://mirror.example/"), make_job("Hello"))
        assert str(recorder.last.url).startswith("https://mirror.example/translate_a/t?")

    async def test_blank_segments_are_not_sent(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好"))
        outcomes = await collect(engine_for(client, sleeps), make_job("  ", "Hello", "\n"))
        assert texts_by_id(outcomes) == {"s1": "  ", "s2": "你好", "s3": "\n"}
        assert q_values(recorder.last) == ["Hello"]

    async def test_all_blank_sends_nothing(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        outcomes = await collect(engine_for(client, sleeps), make_job(" ", ""))
        assert recorder.calls == 0 and len(outcomes) == 2


class TestResponseShapes:
    def test_plain_strings(self) -> None:
        assert parse_batch_response(["一", "二"], 2) == ["一", "二"]

    def test_pairs_with_detected_language(self) -> None:
        assert parse_batch_response([["一", "en"], ["二", "en"]], 2) == ["一", "二"]

    def test_single_string_for_one_q(self) -> None:
        assert parse_batch_response("一", 1) == ["一"]

    @pytest.mark.parametrize(
        ("payload", "expected"),
        MISMATCHED,
    )
    def test_mismatch_raises_bad_response(self, payload: object, expected: int) -> None:
        with pytest.raises(EngineError) as info:
            parse_batch_response(payload, expected)
        assert info.value.code is ErrorCode.BAD_RESPONSE

    def test_single_joins_sentences(self) -> None:
        payload = [[["第一句。", "One."], ["第二句。", "Two."], None], None, "en"]
        assert parse_single_response(payload) == "第一句。第二句。"

    @pytest.mark.parametrize("payload", [None, [], ["x"], [[]], [[[None]]], {"a": 1}])
    def test_single_bad_shape(self, payload: object) -> None:
        with pytest.raises(EngineError) as info:
            parse_single_response(payload)
        assert info.value.code is ErrorCode.BAD_RESPONSE


class TestFallback:
    async def test_shape_mismatch_falls_back_to_single_requests(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        def by_text(request: httpx.Request) -> httpx.Response:
            (text,) = q_values(request)
            return single(f"<{text}>")

        recorder.queue(httpx.Response(200, json=["只有一条"]), by_text, by_text)
        outcomes = await collect(engine_for(client, sleeps), make_job("A", "B"))

        assert texts_by_id(outcomes) == {"s1": "<A>", "s2": "<B>"}
        assert recorder.calls == 3
        for request in recorder.requests[1:]:
            assert str(request.url).split("?")[0] == SINGLE_URL
            assert list(request.url.params.multi_items()) == [
                ("client", "gtx"),
                ("sl", "en"),
                ("tl", "zh-CN"),
                ("dt", "t"),
                ("dj", "0"),
            ]

    async def test_single_failures_stay_with_their_segment(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        def by_text(request: httpx.Request) -> httpx.Response:
            (text,) = q_values(request)
            return single("好") if text == "A" else httpx.Response(400)

        recorder.queue(httpx.Response(200, json={"unexpected": True}), by_text, by_text)
        outcomes = {
            str(o.segment_id): o
            for o in await collect(engine_for(client, sleeps, max_retries=0), make_job("A", "B"))
        }
        assert outcomes["s1"].text == "好"
        assert outcomes["s2"].text is None and outcomes["s2"].code is ErrorCode.BAD_REQUEST

    async def test_fallback_concurrency_is_bounded(
        self, client: httpx.AsyncClient, recorder: Recorder
    ) -> None:
        active = 0
        peak = 0

        async def slow(request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            if request.url.path.endswith("/translate_a/t"):
                return httpx.Response(200, json=[])
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return single("好")

        texts = [f"t{i}" for i in range(FALLBACK_CONCURRENCY * 3)]
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as async_client:
            engine = create_google_free_engine(settings(), async_client)
            outcomes = await collect(engine, make_job(*texts))
        assert all(o.text == "好" for o in outcomes) and len(outcomes) == len(texts)
        assert 1 <= peak <= FALLBACK_CONCURRENCY

    async def test_non_json_fails_the_batch_without_fallback(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(httpx.Response(200, text="<html>unusual traffic</html>"))
        job = make_job("A", "B")
        outcomes = await collect(engine_for(client, sleeps), job)
        assert_all_failed(outcomes, job, ErrorCode.BAD_RESPONSE)
        assert recorder.calls == 1


class TestErrors:
    async def test_429_is_retried_then_rate_limited(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(httpx.Response(429), httpx.Response(429))
        job = make_job("A", "B")
        outcomes = await collect(engine_for(client, sleeps, max_retries=1), job)
        assert_all_failed(outcomes, job, ErrorCode.RATE_LIMITED)
        assert recorder.calls == 2 and len(sleeps.delays) == 1

    async def test_429_then_success(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(httpx.Response(429, headers={"retry-after": "1"}), ok("好"))
        outcomes = await collect(engine_for(client, sleeps), make_job("A"))
        assert texts_by_id(outcomes) == {"s1": "好"}
        assert sleeps.delays == [1.0]

    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_5xx_is_server(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, status: int
    ) -> None:
        recorder.queue(httpx.Response(status))
        job = make_job("A")
        assert_all_failed(
            await collect(engine_for(client, sleeps, max_retries=0), job), job, ErrorCode.SERVER
        )

    @pytest.mark.parametrize("status", [401, 403])
    async def test_forbidden_is_bad_request_not_auth(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, status: int
    ) -> None:
        recorder.queue(httpx.Response(status))
        job = make_job("A")
        assert_all_failed(await collect(engine_for(client, sleeps), job), job, ErrorCode.BAD_REQUEST)
        assert recorder.calls == 1  # 不重试

    async def test_network_error(self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps) -> None:
        recorder.queue(httpx.ConnectError("refused"), httpx.ConnectError("refused"))
        job = make_job("A")
        assert_all_failed(await collect(engine_for(client, sleeps), job), job, ErrorCode.NETWORK)

    async def test_timeout(self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps) -> None:
        recorder.queue(httpx.ReadTimeout("slow"))
        job = make_job("A")
        assert_all_failed(
            await collect(engine_for(client, sleeps, max_retries=0), job), job, ErrorCode.TIMEOUT
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
        recorder.queue(httpx.Response(500, text="echo PRIVATE-TEXT"))
        await collect(engine_for(client, sleeps, max_retries=0), make_job("PRIVATE-TEXT"))
        assert_private(caplog, "PRIVATE-TEXT")

    async def test_cancel_propagates(self, recorder: Recorder) -> None:
        started = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.MockTransport(hanging(started))) as async_client:
            engine = create_google_free_engine(settings(), async_client)
            assert await cancel_while_pending(engine, make_job("A"), started) == []


class TestLanguagesAndLimits:
    def test_codes(self) -> None:
        assert google_code("zh-Hans") == "zh-CN"
        assert google_code("zh-Hant") == "zh-TW"
        assert google_code("ja") == "ja"
        assert google_code("he") is None
        assert len(CODES) == 20

    def test_supports(self, client: httpx.AsyncClient, sleeps: Sleeps) -> None:
        engine = engine_for(client, sleeps)
        assert engine.supports(Lang("en"), Lang("zh-Hans"))
        assert engine.supports(Lang("zh-Hans"), Lang("zh-Hant"))
        assert not engine.supports(Lang("en"), Lang("en"))
        assert not engine.supports(Lang("en"), Lang("sw"))
        assert not engine.supports(Lang("xx"), Lang("en"))

    async def test_source_code_mapped(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("Hi"))
        await collect(engine_for(client, sleeps), make_job("你好", source="zh-Hant", target="en"))
        assert recorder.last.url.params["sl"] == "zh-TW" and recorder.last.url.params["tl"] == "en"

    def test_info_limits(self, client: httpx.AsyncClient, sleeps: Sleeps) -> None:
        info = engine_for(client, sleeps).info
        assert (info.max_batch_segments, info.max_batch_chars) == (100, 5000)
        assert info.kind is EngineKind.ONLINE_MT and not info.billing
        smaller = engine_for(client, sleeps, max_batch_segments=10, max_batch_chars=800).info
        assert (smaller.max_batch_segments, smaller.max_batch_chars) == (10, 800)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"max_batch_segments": 101},
            {"max_batch_chars": 5001},
            {"endpoint": "ftp://x"},
            {"endpoint": "https://x/?a=1"},
            {"endpoint": "https://u:p@x"},
        ],
    )
    def test_settings_reject_out_of_range(self, overrides: dict[str, object]) -> None:
        with pytest.raises(pydantic.ValidationError):
            settings(**overrides)

    def test_fingerprint_follows_endpoint(self, client: httpx.AsyncClient, sleeps: Sleeps) -> None:
        default = engine_for(client, sleeps).info.fingerprint
        assert engine_for(client, sleeps, timeout_s=20).info.fingerprint == default
        assert engine_for(client, sleeps, endpoint="https://mirror.example").info.fingerprint != default
