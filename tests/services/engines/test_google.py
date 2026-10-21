"""Google Cloud Translation v2 引擎的契约测试。

覆盖请求构造、语言映射、批量回填、403 限流与无权限的区分、隐私与取消。
"""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pydantic
import pytest

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.translation import Engine, EngineKind
from lst.services.engines.google import GoogleSettings, create_google_engine
from lst.services.engines.google.engine import TRANSLATE_URL, classify_error, error_reasons
from lst.services.engines.google.languages import CODES, google_code
from lst.services.engines.http import RetryPolicy
from tests.services.engines.mt_helpers import (
    FakeSecrets,
    Recorder,
    Sleeps,
    assert_all_failed,
    assert_private,
    cancel_while_pending,
    capture_all,
    collect,
    hanging,
    json_body,
    make_job,
    texts_by_id,
)

KEY = "AIzaSy-test-google-key-0123"


def settings(**overrides: object) -> GoogleSettings:
    fields: dict[str, object] = {"id": "google", "name": "Google", "priority": 2, "secret": "google"}
    return GoogleSettings.model_validate({**fields, **overrides})


def ok(*texts: str) -> httpx.Response:
    translations = [{"translatedText": text, "detectedSourceLanguage": "en"} for text in texts]
    return httpx.Response(200, json={"data": {"translations": translations}})


def legacy_error(status: int, reason: str, message: str = "error") -> httpx.Response:
    """旧式错误结构：error.errors[].reason。"""
    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}
    return httpx.Response(status, json=body)


def rpc_error(status: int, reason: str, message: str = "error") -> httpx.Response:
    """google.rpc 错误结构：error.details[] 里的 ErrorInfo。"""
    detail = {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": reason,
        "domain": "googleapis.com",
    }
    body = {"error": {"code": status, "message": message, "status": "X", "details": [detail]}}
    return httpx.Response(status, json=body)


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


def engine_for(
    client: httpx.AsyncClient, sleeps: Sleeps, *, key: str | None = KEY, **overrides: object
) -> Engine:
    secrets = FakeSecrets({"google": key} if key is not None else {})
    return create_google_engine(settings(**overrides), secrets, client, sleep=sleeps, rand=lambda: 0.5)


class TestRequestShape:
    async def test_request(self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps) -> None:
        recorder.queue(ok("你好", "世界"))
        await collect(engine_for(client, sleeps), make_job("Hello", "World"))

        request = recorder.last
        assert request.method == "POST"
        assert str(request.url) == "https://translation.googleapis.com/language/translate/v2"
        assert request.headers["X-Goog-Api-Key"] == KEY
        assert request.headers["Content-Type"] == "application/json"
        assert json_body(request) == {
            "q": ["Hello", "World"],
            "target": "zh-CN",
            "source": "en",
            "format": "text",
        }

    async def test_key_never_in_url_or_body(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps), make_job("Hello"))
        request = recorder.last
        assert request.url.query == b""
        assert KEY not in str(request.url)
        assert KEY not in request.content.decode("utf-8")

    async def test_request_timeout(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps), make_job("Hello"))
        assert recorder.last.extensions["timeout"] == {"connect": 3.0, "read": 8.0, "write": 8.0, "pool": 8.0}

    async def test_unknown_source_is_left_to_auto_detection(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps), make_job("Hello", source="xx"))
        assert "source" not in json_body(recorder.last)

    async def test_constant_url(self) -> None:
        assert TRANSLATE_URL == "https://translation.googleapis.com/language/translate/v2"


class TestLanguages:
    @pytest.mark.parametrize(
        ("lang", "code"),
        [
            ("zh-Hans", "zh-CN"),
            ("zh-Hant", "zh-TW"),
            ("he", "iw"),
            ("nb", "no"),
            ("en", "en"),
            ("ja", "ja"),
            ("ko", "ko"),
            ("fr", "fr"),
            ("de", "de"),
            ("es", "es"),
            ("ru", "ru"),
            ("pt", "pt"),
            ("it", "it"),
        ],
    )
    def test_codes(self, lang: str, code: str) -> None:
        assert google_code(lang) == code

    def test_table_covers_common_languages(self) -> None:
        assert {"zh-Hans", "zh-Hant", "en", "ja", "ko", "fr", "de", "es", "ru", "pt", "it"} <= set(CODES)

    def test_non_canonical_codes_are_unknown(self) -> None:
        for lang in ("zh", "zh-CN", "iw", "EN", "en-US", ""):
            assert google_code(lang) is None

    async def test_request_uses_mapped_codes(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("שלום"))
        await collect(engine_for(client, sleeps), make_job("你好", source="zh-Hant", target="he"))
        body = json_body(recorder.last)
        assert (body["source"], body["target"]) == ("zh-TW", "iw")

    @pytest.mark.parametrize(
        ("source", "target", "expected"),
        [
            ("en", "zh-Hans", True),
            ("zh-Hans", "zh-Hant", True),
            ("he", "en", True),
            ("en", "en", False),
            ("xx", "en", False),
            ("en", "xx", False),
        ],
    )
    async def test_supports(
        self, client: httpx.AsyncClient, sleeps: Sleeps, source: str, target: str, expected: bool
    ) -> None:
        assert engine_for(client, sleeps).supports(Lang(source), Lang(target)) is expected

    async def test_unsupported_target_fails_without_request(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        job = make_job("Hello", target="xx")
        assert_all_failed(await collect(engine_for(client, sleeps), job), job, ErrorCode.UNSUPPORTED_LANGUAGE)
        assert recorder.calls == 0


class TestBatching:
    async def test_one_request_per_job_and_results_in_order(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        texts = [f"line {i}" for i in range(128)]
        recorder.queue(ok(*(f"第 {i} 行" for i in range(128))))
        outcomes = await collect(engine_for(client, sleeps), make_job(*texts))
        assert recorder.calls == 1
        assert json_body(recorder.last)["q"] == texts
        assert [o.segment_id for o in outcomes] == [f"s{i}" for i in range(1, 129)]
        assert [o.text for o in outcomes] == [f"第 {i} 行" for i in range(128)]

    async def test_blank_segments_are_not_sent(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好"))
        outcomes = await collect(engine_for(client, sleeps), make_job("\t", "Hello"))
        assert json_body(recorder.last)["q"] == ["Hello"]
        assert texts_by_id(outcomes) == {"s1": "\t", "s2": "你好"}


class TestBadResponse:
    @pytest.mark.parametrize(
        "response",
        [
            ok("只有一条"),
            ok("一", "二", "三"),
            httpx.Response(200, text="<html>"),
            httpx.Response(200, json={"data": {"translations": [{"text": "一"}, {"text": "二"}]}}),
            httpx.Response(200, json={"translations": []}),
        ],
        ids=["too-few", "too-many", "not-json", "wrong-field", "missing-data"],
    )
    async def test_bad_response(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, response: httpx.Response
    ) -> None:
        recorder.queue(response)
        job = make_job("Hello", "World")
        assert_all_failed(await collect(engine_for(client, sleeps), job), job, ErrorCode.BAD_RESPONSE)


class TestHttpErrors:
    @pytest.mark.parametrize(
        ("status", "code", "calls"),
        [
            (400, ErrorCode.BAD_REQUEST, 1),
            (401, ErrorCode.AUTH, 1),
            (403, ErrorCode.AUTH, 1),
            (456, ErrorCode.QUOTA, 1),
            (429, ErrorCode.RATE_LIMITED, 2),
            (500, ErrorCode.SERVER, 2),
            (503, ErrorCode.SERVER, 2),
        ],
    )
    async def test_status_mapping_and_retry(
        self,
        client: httpx.AsyncClient,
        recorder: Recorder,
        sleeps: Sleeps,
        status: int,
        code: ErrorCode,
        calls: int,
    ) -> None:
        recorder.queue(*(httpx.Response(status, json={"error": {"message": "boom"}}) for _ in range(calls)))
        job = make_job("Hello")
        outcomes = await collect(engine_for(client, sleeps), job)
        assert_all_failed(outcomes, job, code)
        assert recorder.calls == calls
        assert outcomes[0].error == f"HTTP {status}: boom"

    async def test_429_retried_once_then_succeeds(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(httpx.Response(429), ok("你好"))
        assert texts_by_id(await collect(engine_for(client, sleeps), make_job("Hello"))) == {"s1": "你好"}
        assert sleeps.delays == [0.5]

    async def test_retry_after_is_honored(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(httpx.Response(503, headers={"Retry-After": "2"}), ok("你好"))
        await collect(engine_for(client, sleeps), make_job("Hello"))
        assert sleeps.delays == [2.0]

    async def test_long_retry_after_gives_up(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(httpx.Response(429, headers={"Retry-After": "60"}))
        job = make_job("Hello")
        assert_all_failed(await collect(engine_for(client, sleeps), job), job, ErrorCode.RATE_LIMITED)
        assert (recorder.calls, sleeps.delays) == (1, [])

    async def test_network_error_retried_once(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(httpx.ConnectError("refused"), ok("你好"))
        assert texts_by_id(await collect(engine_for(client, sleeps), make_job("Hello"))) == {"s1": "你好"}
        assert recorder.calls == 2

    async def test_timeout_after_retry(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
        job = make_job("Hello")
        assert_all_failed(await collect(engine_for(client, sleeps), job), job, ErrorCode.TIMEOUT)


class TestForbidden:
    """Google 的 403 既可能是每分钟配额（限流），也可能是 key 无权限，靠 reason 区分。"""

    @pytest.mark.parametrize(
        "response",
        [
            legacy_error(403, "userRateLimitExceeded", "User Rate Limit Exceeded"),
            legacy_error(403, "rateLimitExceeded", "Rate Limit Exceeded"),
            rpc_error(403, "RATE_LIMIT_EXCEEDED"),
        ],
        ids=["userRateLimitExceeded", "rateLimitExceeded", "rpc"],
    )
    async def test_rate_limit_403_is_retried_once(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, response: httpx.Response
    ) -> None:
        recorder.queue(response, ok("你好"))
        outcomes = await collect(engine_for(client, sleeps), make_job("Hello"))
        assert texts_by_id(outcomes) == {"s1": "你好"}
        assert recorder.calls == 2
        assert sleeps.delays == [0.5]

    async def test_rate_limit_403_twice_is_rate_limited(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(
            legacy_error(403, "userRateLimitExceeded", "User Rate Limit Exceeded"),
            legacy_error(403, "userRateLimitExceeded", "User Rate Limit Exceeded"),
        )
        job = make_job("Hello", "World")
        outcomes = await collect(engine_for(client, sleeps), job)
        assert_all_failed(outcomes, job, ErrorCode.RATE_LIMITED)
        assert recorder.calls == 2
        assert outcomes[0].error == "HTTP 403: User Rate Limit Exceeded（reason: userRateLimitExceeded）"

    @pytest.mark.parametrize(
        "response",
        [
            legacy_error(403, "forbidden", "The caller does not have permission"),
            legacy_error(403, "accessNotConfigured", "API not enabled"),
            rpc_error(403, "SERVICE_DISABLED"),
            httpx.Response(403, text="Forbidden"),
            httpx.Response(403),
        ],
        ids=["forbidden", "accessNotConfigured", "rpc-service-disabled", "not-json", "empty"],
    )
    async def test_permission_403_is_auth_and_not_retried(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, response: httpx.Response
    ) -> None:
        recorder.queue(response)
        job = make_job("Hello")
        assert_all_failed(await collect(engine_for(client, sleeps), job), job, ErrorCode.AUTH)
        assert (recorder.calls, sleeps.delays) == (1, [])

    async def test_daily_limit_is_quota(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(legacy_error(403, "dailyLimitExceeded", "Daily Limit Exceeded"))
        job = make_job("Hello")
        assert_all_failed(await collect(engine_for(client, sleeps), job), job, ErrorCode.QUOTA)
        assert recorder.calls == 1

    @pytest.mark.parametrize(
        "response",
        [rpc_error(400, "API_KEY_INVALID", "API key not valid."), legacy_error(400, "keyInvalid", "Bad key")],
        ids=["rpc", "legacy"],
    )
    async def test_invalid_key_400_is_auth(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, response: httpx.Response
    ) -> None:
        recorder.queue(response)
        job = make_job("Hello")
        assert_all_failed(await collect(engine_for(client, sleeps), job), job, ErrorCode.AUTH)
        assert recorder.calls == 1

    def test_error_reasons(self) -> None:
        both = b'{"error":{"errors":[{"reason":"a"}],"details":[{"@type":"x","reason":"B"},{"@type":"y"}]}}'
        assert error_reasons(both) == {"a", "B"}
        assert error_reasons(b"not json") == frozenset()
        assert error_reasons(b'{"error":"plain string"}') == frozenset()
        assert error_reasons(b"[]") == frozenset()

    @pytest.mark.parametrize(
        ("status", "reasons", "code", "retryable"),
        [
            (403, {"userRateLimitExceeded"}, ErrorCode.RATE_LIMITED, True),
            (403, {"dailyLimitExceeded"}, ErrorCode.QUOTA, False),
            (403, {"forbidden"}, ErrorCode.AUTH, False),
            (403, set[str](), ErrorCode.AUTH, False),
            (400, {"API_KEY_INVALID"}, ErrorCode.AUTH, False),
            (400, {"badRequest"}, ErrorCode.BAD_REQUEST, False),
            (429, set[str](), ErrorCode.RATE_LIMITED, True),
            (500, {"backendError"}, ErrorCode.SERVER, True),
            (401, set[str](), ErrorCode.AUTH, False),
        ],
    )
    def test_classify_error(self, status: int, reasons: set[str], code: ErrorCode, retryable: bool) -> None:
        assert classify_error(status, frozenset(reasons), RetryPolicy()) == (code, retryable)


class TestMaxRetries:
    @pytest.mark.parametrize(
        ("reply", "code"),
        [
            (httpx.Response(503), ErrorCode.SERVER),
            (httpx.Response(429), ErrorCode.RATE_LIMITED),
            (legacy_error(403, "userRateLimitExceeded"), ErrorCode.RATE_LIMITED),
            (httpx.ConnectError("refused"), ErrorCode.NETWORK),
        ],
        ids=["503", "429", "403-rate-limit", "network"],
    )
    async def test_zero_disables_retry(
        self,
        client: httpx.AsyncClient,
        recorder: Recorder,
        sleeps: Sleeps,
        reply: httpx.Response | Exception,
        code: ErrorCode,
    ) -> None:
        recorder.queue(reply)
        job = make_job("Hello")
        assert_all_failed(await collect(engine_for(client, sleeps, max_retries=0), job), job, code)
        assert (recorder.calls, sleeps.delays) == (1, [])

    async def test_two_allows_two_retries(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(legacy_error(403, "rateLimitExceeded"), httpx.Response(503), ok("你好"))
        outcomes = await collect(engine_for(client, sleeps, max_retries=2), make_job("Hello"))
        assert texts_by_id(outcomes) == {"s1": "你好"}
        # 退避 0.5 × 2^i 秒（抖动固定为 1 倍）。
        assert (recorder.calls, sleeps.delays) == (3, [0.5, 1.0])

    async def test_two_retries_then_give_up(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(*(legacy_error(403, "userRateLimitExceeded") for _ in range(3)))
        job = make_job("Hello")
        outcomes = await collect(engine_for(client, sleeps, max_retries=2), job)
        assert_all_failed(outcomes, job, ErrorCode.RATE_LIMITED)
        assert recorder.calls == 3

    async def test_permission_403_is_never_retried(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(legacy_error(403, "forbidden"))
        job = make_job("Hello")
        assert_all_failed(await collect(engine_for(client, sleeps, max_retries=3), job), job, ErrorCode.AUTH)
        assert recorder.calls == 1


class TestMissingKey:
    @pytest.mark.parametrize(
        "secrets", [FakeSecrets({}), FakeSecrets({"google": ""})], ids=["absent", "empty"]
    )
    async def test_missing_key_fails_without_request(
        self, client: httpx.AsyncClient, recorder: Recorder, secrets: FakeSecrets
    ) -> None:
        engine = create_google_engine(settings(), secrets, client)
        job = make_job("Hello", "World")
        assert_all_failed(await collect(engine, job), job, ErrorCode.AUTH)
        assert recorder.calls == 0

    async def test_no_secret_configured(self, client: httpx.AsyncClient, recorder: Recorder) -> None:
        engine = create_google_engine(settings(secret=None), FakeSecrets({"google": KEY}), client)
        job = make_job("Hello")
        assert_all_failed(await collect(engine, job), job, ErrorCode.AUTH)
        assert recorder.calls == 0


class TestInfo:
    async def test_info_fields(self, client: httpx.AsyncClient, sleeps: Sleeps) -> None:
        info = engine_for(client, sleeps).info
        assert info.kind is EngineKind.ONLINE_MT
        assert info.billing is True
        assert (info.max_batch_segments, info.max_batch_chars) == (128, 5000)
        assert info.timeout_s == 8.0

    async def test_limits_configurable_within_api_limits(
        self, client: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        info = engine_for(client, sleeps, max_batch_segments=20, max_batch_chars=20000).info
        assert (info.max_batch_segments, info.max_batch_chars) == (20, 20000)
        with pytest.raises(pydantic.ValidationError):
            settings(max_batch_segments=129)

    async def test_fingerprint_excludes_key(self, client: httpx.AsyncClient, sleeps: Sleeps) -> None:
        a = engine_for(client, sleeps).info
        b = engine_for(client, sleeps, key="other-key").info
        assert a.fingerprint == b.fingerprint
        assert a.revision != b.revision
        assert KEY not in repr(a)


class TestPrivacy:
    async def test_logs_contain_no_key_or_text(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, caplog: pytest.LogCaptureFixture
    ) -> None:
        capture_all(caplog)
        recorder.queue(
            ok("机密译文甲"),
            legacy_error(403, "userRateLimitExceeded", "User Rate Limit Exceeded"),
            legacy_error(403, "forbidden", "Forbidden"),
            httpx.Response(200, text="机密译文乙"),
        )
        engine = engine_for(client, sleeps)
        await collect(engine, make_job("Secret source alpha"))
        await collect(engine, make_job("Secret source beta"))
        await collect(engine, make_job("Secret source gamma"))
        assert_private(caplog, KEY, "Secret source", "机密译文")
        assert any(record.name == "httpx" for record in caplog.records)
        assert any(record.name.startswith("lst.services.engines.google") for record in caplog.records)


class TestCancellation:
    async def test_cancel_propagates_during_request(self, sleeps: Sleeps) -> None:
        started = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.MockTransport(hanging(started))) as client:
            produced = await cancel_while_pending(engine_for(client, sleeps), make_job("Hello"), started)
        assert produced == []

    async def test_cancel_propagates_during_retry_wait(
        self, client: httpx.AsyncClient, recorder: Recorder
    ) -> None:
        started = asyncio.Event()

        async def blocking_sleep(delay: float) -> None:
            started.set()
            await asyncio.Event().wait()

        recorder.queue(legacy_error(403, "rateLimitExceeded"))
        engine = create_google_engine(settings(), FakeSecrets({"google": KEY}), client, sleep=blocking_sleep)
        assert await cancel_while_pending(engine, make_job("Hello"), started) == []
        assert recorder.calls == 1
