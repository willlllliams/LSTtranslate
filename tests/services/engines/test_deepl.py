"""DeepL 引擎的契约测试：请求构造、语言映射、批量回填、错误映射、隐私与取消。"""

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pydantic
import pytest

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.translation import Engine, EngineKind
from lst.services.engines.deepl import DeepLSettings, create_deepl_engine
from lst.services.engines.deepl.engine import MAX_BODY_BYTES, encode_json, fit_context
from lst.services.engines.deepl.languages import SOURCE_CODES, TARGET_CODES, source_code, target_code
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

PRO_KEY = "0f1e2d3c-pro-key-4b5a"
FREE_KEY = "9a8b7c6d-free-key:fx"


def settings(**overrides: object) -> DeepLSettings:
    fields: dict[str, object] = {"id": "deepl", "name": "DeepL", "priority": 1, "secret": "deepl"}
    return DeepLSettings.model_validate({**fields, **overrides})


def ok(*texts: str) -> httpx.Response:
    return httpx.Response(
        200, json={"translations": [{"detected_source_language": "EN", "text": text} for text in texts]}
    )


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
    client: httpx.AsyncClient, sleeps: Sleeps, *, key: str | None = PRO_KEY, **overrides: object
) -> Engine:
    secrets = FakeSecrets({"deepl": key} if key is not None else {})
    return create_deepl_engine(settings(**overrides), secrets, client, sleep=sleeps, rand=lambda: 0.5)


class TestRequestShape:
    async def test_pro_key_uses_pro_endpoint_and_header_auth(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好", "世界"))
        await collect(engine_for(client, sleeps), make_job("Hello", "World"))

        request = recorder.last
        assert request.method == "POST"
        assert str(request.url) == "https://api.deepl.com/v2/translate"
        assert request.headers["Authorization"] == f"DeepL-Auth-Key {PRO_KEY}"
        assert request.headers["Content-Type"] == "application/json"
        assert json_body(request) == {
            "text": ["Hello", "World"],
            "target_lang": "ZH-HANS",
            "source_lang": "EN",
        }

    async def test_free_key_uses_free_endpoint(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps, key=FREE_KEY), make_job("Hello"))
        assert str(recorder.last.url) == "https://api-free.deepl.com/v2/translate"
        assert recorder.last.headers["Authorization"] == f"DeepL-Auth-Key {FREE_KEY}"

    @pytest.mark.parametrize("key", [PRO_KEY, FREE_KEY])
    async def test_key_never_in_url_or_body(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, key: str
    ) -> None:
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps, key=key), make_job("Hello"))
        request = recorder.last
        assert key not in str(request.url)
        assert request.url.query == b""
        assert key not in request.content.decode("utf-8")
        assert "auth_key" not in json_body(request)

    async def test_request_timeout(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps), make_job("Hello"))
        timeout = recorder.last.extensions["timeout"]
        assert timeout == {"connect": 3.0, "read": 8.0, "write": 8.0, "pool": 8.0}

    @pytest.mark.parametrize(
        ("formality", "sent"),
        [
            ("default", None),
            ("more", "prefer_more"),
            ("prefer_more", "prefer_more"),
            ("less", "prefer_less"),
            ("prefer_less", "prefer_less"),
        ],
    )
    async def test_formality(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, formality: str, sent: str | None
    ) -> None:
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps, formality=formality), make_job("Hello"))
        assert json_body(recorder.last).get("formality") == sent

    async def test_unknown_source_is_left_to_auto_detection(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps), make_job("Hello", source="xx"))
        assert "source_lang" not in json_body(recorder.last)


class TestContext:
    async def test_context_is_sent(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps), make_job("Hello", context="Hello there. General Kenobi."))
        assert json_body(recorder.last)["context"] == "Hello there. General Kenobi."

    async def test_empty_context_is_omitted(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps), make_job("Hello"))
        assert "context" not in json_body(recorder.last)

    async def test_oversized_context_is_truncated_to_fit(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        context = "上下文" * 60000  # 540000 字节，远超 128 KiB
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps), make_job("Hello", context=context))
        request = recorder.last
        sent = json_body(request)["context"]
        assert isinstance(sent, str)
        assert context.startswith(sent)
        assert len(request.content) <= MAX_BODY_BYTES
        # 再多一个字符（3 字节）就会超限，说明保留的是能放下的最长前缀。
        assert len(request.content) + 3 > MAX_BODY_BYTES

    def test_fit_context_exact_boundary(self) -> None:
        payload: dict[str, object] = {"text": ["a"], "target_lang": "DE"}
        full = encode_json({**payload, "context": "abcdef"})
        assert fit_context(payload, "abcdef", limit=len(full)) == "abcdef"
        assert fit_context(payload, "abcdef", limit=len(full) - 1) == "abcde"
        # 按编码后的长度计算：换行编码为两个字节，所以同样的上限只能放下 5 个字符。
        assert fit_context(payload, "abc\ndef", limit=len(full)) == "abc\nd"
        assert fit_context(payload, "abcdef", limit=len(encode_json(payload))) == ""


class TestLanguages:
    @pytest.mark.parametrize(
        ("lang", "code"),
        [
            ("zh-Hans", "ZH-HANS"),
            ("zh-Hant", "ZH-HANT"),
            ("en", "EN-US"),
            ("pt", "PT-BR"),
            ("ja", "JA"),
            ("ko", "KO"),
            ("fr", "FR"),
            ("de", "DE"),
            ("es", "ES"),
            ("ru", "RU"),
            ("it", "IT"),
            ("nb", "NB"),
            ("he", "HE"),
        ],
    )
    def test_target_codes(self, lang: str, code: str) -> None:
        assert target_code(lang) == code

    @pytest.mark.parametrize(
        ("lang", "code"),
        [("zh-Hans", "ZH"), ("zh-Hant", "ZH"), ("en", "EN"), ("pt", "PT"), ("ja", "JA"), ("ko", "KO")],
    )
    def test_source_codes_use_primary_subtag(self, lang: str, code: str) -> None:
        assert source_code(lang) == code

    def test_tables_cover_same_languages_and_use_uppercase(self) -> None:
        assert set(SOURCE_CODES) == set(TARGET_CODES)
        for lang in SOURCE_CODES:
            assert TARGET_CODES[lang].split("-")[0] == SOURCE_CODES[lang]
            assert SOURCE_CODES[lang].isupper()
        assert {"zh-Hans", "zh-Hant", "en", "ja", "ko", "fr", "de", "es", "ru", "pt", "it"} <= set(
            TARGET_CODES
        )

    def test_non_canonical_codes_are_unknown(self) -> None:
        for lang in ("zh", "zh-CN", "EN", "en-US", "pt-BR", ""):
            assert target_code(lang) is None
            assert source_code(lang) is None

    @pytest.mark.parametrize(
        ("source", "target", "expected"),
        [
            ("en", "zh-Hans", True),
            ("zh-Hans", "en", True),
            ("ja", "zh-Hant", True),
            ("en", "en", False),
            ("zh-Hans", "zh-Hant", False),  # 源语言码都是 ZH，按同一种语言处理
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
        outcomes = await collect(engine_for(client, sleeps), job)
        assert_all_failed(outcomes, job, ErrorCode.UNSUPPORTED_LANGUAGE)
        assert recorder.calls == 0


class TestBatching:
    async def test_one_request_per_job_and_results_in_order(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        texts = [f"line {i}" for i in range(50)]
        recorder.queue(ok(*(f"第 {i} 行" for i in range(50))))
        outcomes = await collect(engine_for(client, sleeps), make_job(*texts))
        assert recorder.calls == 1
        assert json_body(recorder.last)["text"] == texts
        assert [o.segment_id for o in outcomes] == [f"s{i}" for i in range(1, 51)]
        assert [o.text for o in outcomes] == [f"第 {i} 行" for i in range(50)]

    async def test_blank_segments_are_not_sent(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(ok("你好", "世界"))
        outcomes = await collect(engine_for(client, sleeps), make_job("Hello", "  ", "World"))
        assert json_body(recorder.last)["text"] == ["Hello", "World"]
        assert texts_by_id(outcomes) == {"s1": "你好", "s2": "  ", "s3": "世界"}

    async def test_all_blank_job_sends_nothing(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        outcomes = await collect(engine_for(client, sleeps), make_job("", " \n"))
        assert texts_by_id(outcomes) == {"s1": "", "s2": " \n"}
        assert recorder.calls == 0


class TestBadResponse:
    @pytest.mark.parametrize(
        "response",
        [
            ok("只有一条"),
            ok("一", "二", "三"),
            httpx.Response(200, text="not json"),
            httpx.Response(200, json={"translations": [{"text": 1}, {"text": "二"}]}),
            httpx.Response(200, json={"result": []}),
            httpx.Response(200, json=[]),
        ],
        ids=["too-few", "too-many", "not-json", "wrong-type", "missing-key", "not-object"],
    )
    async def test_bad_response(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, response: httpx.Response
    ) -> None:
        recorder.queue(response)
        job = make_job("Hello", "World")
        outcomes = await collect(engine_for(client, sleeps), job)
        assert_all_failed(outcomes, job, ErrorCode.BAD_RESPONSE)

    async def test_bad_response_detail_does_not_echo_body(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(httpx.Response(200, json={"translations": [{"text": "机密译文"}]}))
        outcomes = await collect(engine_for(client, sleeps), make_job("Hello", "World"))
        for outcome in outcomes:
            assert outcome.error is not None
            assert "机密译文" not in outcome.error


class TestHttpErrors:
    @pytest.mark.parametrize(
        ("status", "code", "calls"),
        [
            (400, ErrorCode.BAD_REQUEST, 1),
            (401, ErrorCode.AUTH, 1),
            (403, ErrorCode.AUTH, 1),
            (413, ErrorCode.BAD_REQUEST, 1),
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
        recorder.queue(*(httpx.Response(status, json={"message": "boom"}) for _ in range(calls)))
        job = make_job("Hello", "World")
        outcomes = await collect(engine_for(client, sleeps), job)
        assert_all_failed(outcomes, job, code)
        assert recorder.calls == calls
        assert all(o.error == f"HTTP {status}: boom" for o in outcomes)

    async def test_rate_limit_retried_once_then_succeeds(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(httpx.Response(429), ok("你好"))
        outcomes = await collect(engine_for(client, sleeps), make_job("Hello"))
        assert texts_by_id(outcomes) == {"s1": "你好"}
        assert recorder.calls == 2
        assert sleeps.delays == [0.5]

    async def test_network_error(self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps) -> None:
        recorder.queue(httpx.ConnectError("refused"), httpx.ConnectError("refused"))
        job = make_job("Hello")
        assert_all_failed(await collect(engine_for(client, sleeps), job), job, ErrorCode.NETWORK)


class TestMaxRetries:
    @pytest.mark.parametrize(
        ("reply", "code"),
        [
            (httpx.Response(503), ErrorCode.SERVER),
            (httpx.Response(429), ErrorCode.RATE_LIMITED),
            (httpx.ConnectError("refused"), ErrorCode.NETWORK),
        ],
        ids=["503", "429", "network"],
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
        recorder.queue(httpx.Response(503), httpx.Response(429), ok("你好"))
        outcomes = await collect(engine_for(client, sleeps, max_retries=2), make_job("Hello"))
        assert texts_by_id(outcomes) == {"s1": "你好"}
        # 退避 0.5 × 2^i 秒（抖动固定为 1 倍）。
        assert (recorder.calls, sleeps.delays) == (3, [0.5, 1.0])

    async def test_two_retries_then_give_up(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps
    ) -> None:
        recorder.queue(*(httpx.Response(503) for _ in range(3)))
        job = make_job("Hello")
        assert_all_failed(
            await collect(engine_for(client, sleeps, max_retries=2), job), job, ErrorCode.SERVER
        )
        assert recorder.calls == 3


class TestMissingKey:
    @pytest.mark.parametrize(
        "secrets",
        [FakeSecrets({}), FakeSecrets({"deepl": ""}), FakeSecrets({"deepl": "   "})],
        ids=["absent", "empty", "blank"],
    )
    async def test_missing_key_fails_without_request(
        self, client: httpx.AsyncClient, recorder: Recorder, secrets: FakeSecrets
    ) -> None:
        engine = create_deepl_engine(settings(), secrets, client)
        job = make_job("Hello", "World")
        assert_all_failed(await collect(engine, job), job, ErrorCode.AUTH)
        assert recorder.calls == 0

    async def test_no_secret_configured(self, client: httpx.AsyncClient, recorder: Recorder) -> None:
        engine = create_deepl_engine(settings(secret=None), FakeSecrets({"deepl": PRO_KEY}), client)
        job = make_job("Hello")
        assert_all_failed(await collect(engine, job), job, ErrorCode.AUTH)
        assert recorder.calls == 0


class TestInfo:
    async def test_info_fields(self, client: httpx.AsyncClient, sleeps: Sleeps) -> None:
        info = engine_for(client, sleeps).info
        assert info.kind is EngineKind.ONLINE_MT
        assert info.billing is True
        assert (info.max_batch_segments, info.max_batch_chars) == (50, 30000)
        assert info.timeout_s == 8.0
        assert info.max_concurrency == 2

    async def test_batch_limits_are_configurable_within_api_limits(
        self, client: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        info = engine_for(client, sleeps, max_batch_segments=10, max_batch_chars=2000).info
        assert (info.max_batch_segments, info.max_batch_chars) == (10, 2000)
        with pytest.raises(pydantic.ValidationError):
            settings(max_batch_segments=51)
        with pytest.raises(pydantic.ValidationError):
            settings(formality="formal")

    async def test_fingerprint_tracks_translation_settings_not_key(
        self, client: httpx.AsyncClient, sleeps: Sleeps
    ) -> None:
        base = engine_for(client, sleeps).info
        other_key = engine_for(client, sleeps, key="another-pro-key").info
        assert other_key.fingerprint == base.fingerprint
        assert other_key.revision != base.revision
        assert engine_for(client, sleeps, formality="less").info.fingerprint != base.fingerprint
        assert engine_for(client, sleeps, key=FREE_KEY).info.fingerprint != base.fingerprint
        # more 与 prefer_more 发出的请求相同，共用缓存。
        more = engine_for(client, sleeps, formality="more").info.fingerprint
        assert more == engine_for(client, sleeps, formality="prefer_more").info.fingerprint
        for info in (base, other_key):
            assert PRO_KEY not in repr(info)
            assert "another-pro-key" not in repr(info)


class TestPrivacy:
    async def test_logs_contain_no_key_or_text(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, caplog: pytest.LogCaptureFixture
    ) -> None:
        capture_all(caplog)
        recorder.queue(
            ok("机密译文甲"),
            httpx.Response(403, json={"message": "Wrong key"}),
            httpx.Response(200, text="garbage 机密译文乙"),
        )
        engine = engine_for(client, sleeps)
        await collect(engine, make_job("Secret source alpha", context="Secret context"))
        await collect(engine, make_job("Secret source beta"))
        await collect(engine, make_job("Secret source gamma"))
        assert_private(
            caplog,
            PRO_KEY,
            "Secret source",
            "Secret context",
            "机密译文",
        )
        assert any(record.name == "httpx" for record in caplog.records)
        assert any(record.name.startswith("lst.services.engines.deepl") for record in caplog.records)


class TestCancellation:
    async def test_cancel_propagates_during_request(self, sleeps: Sleeps) -> None:
        started = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.MockTransport(hanging(started))) as client:
            engine = engine_for(client, sleeps)
            produced = await cancel_while_pending(engine, make_job("Hello", "World"), started)
        assert produced == []

    async def test_cancel_propagates_during_retry_wait(
        self, client: httpx.AsyncClient, recorder: Recorder
    ) -> None:
        started = asyncio.Event()

        async def blocking_sleep(delay: float) -> None:
            started.set()
            await asyncio.Event().wait()

        recorder.queue(httpx.Response(503))
        engine = create_deepl_engine(
            settings(), FakeSecrets({"deepl": PRO_KEY}), client, sleep=blocking_sleep
        )
        produced = await cancel_while_pending(engine, make_job("Hello"), started)
        assert produced == []
        assert recorder.calls == 1


def test_encode_json_matches_httpx() -> None:
    """自行编码的请求体与 httpx 的 json= 参数逐字节相同，大小计算才准确。"""
    payload: dict[str, object] = {"text": ['你好\n"x"'], "target_lang": "EN-US", "context": "上下文"}
    request = httpx.Request("POST", "https://example.test", json=payload)
    assert encode_json(payload) == request.content
    assert json.loads(encode_json(payload)) == payload
