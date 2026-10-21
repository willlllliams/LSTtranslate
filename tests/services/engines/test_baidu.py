"""百度翻译引擎的契约测试：表单请求与签名、换行替换、按字节拆分、error_code 映射与重试、限速、隐私与取消。"""

import asyncio
import itertools
from collections.abc import AsyncIterator, Callable

import httpx
import pydantic
import pytest
from hypothesis import given
from hypothesis import strategies as st

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.translation import Engine, EngineKind
from lst.services.engines.baidu import BaiduSettings, create_baidu_engine
from lst.services.engines.baidu.engine import (
    ERROR_RULES,
    MAX_QUERY_BYTES,
    TRANSLATE_URL,
    flatten,
    make_sign,
    split_by_bytes,
)
from lst.services.engines.baidu.languages import CODES, source_code, target_code
from tests.services.engines.mt_helpers import (
    FakeClock,
    FakeSecrets,
    Recorder,
    Sleeps,
    assert_all_failed,
    assert_private,
    cancel_while_pending,
    capture_all,
    collect,
    form_body,
    hanging,
    make_job,
    texts_by_id,
)

APPID = "20240101000123"
SECRET = "s3cr3tBaiduKey"
SALT = "42"


def settings(**overrides: object) -> BaiduSettings:
    fields: dict[str, object] = {
        "id": "baidu",
        "name": "百度",
        "priority": 3,
        "secret": "baidu",
        "appid": APPID,
    }
    return BaiduSettings.model_validate({**fields, **overrides})


def ok(*texts: str) -> httpx.Response:
    return httpx.Response(
        200, json={"from": "en", "to": "zh", "trans_result": [{"src": "x", "dst": text} for text in texts]}
    )


def echo(request: httpx.Request) -> httpx.Response:
    """把 q 的每一行加上“译:”前缀返回，用来核对按行回填与分组。"""
    lines = form_body(request)["q"].split("\n")
    return ok(*(f"译:{line}" for line in lines))


def api_error(code: str | int, message: str = "error") -> httpx.Response:
    return httpx.Response(200, json={"error_code": code, "error_msg": message})


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeps(clock: FakeClock) -> Sleeps:
    return Sleeps(clock)


@pytest.fixture
async def client(recorder: Recorder) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        yield client


def engine_for(
    client: httpx.AsyncClient,
    sleeps: Sleeps,
    clock: FakeClock,
    *,
    secret: str | None = SECRET,
    salt: Callable[[], str] = lambda: SALT,
    **overrides: object,
) -> Engine:
    secrets = FakeSecrets({"baidu": secret} if secret is not None else {})
    return create_baidu_engine(
        settings(**overrides), secrets, client, sleep=sleeps, clock=clock, rand=lambda: 0.5, salt=salt
    )


class TestRequestShape:
    async def test_form_post(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(ok("你好", "世界"))
        await collect(engine_for(client, sleeps, clock), make_job("Hello", "World"))

        request = recorder.last
        assert request.method == "POST"
        assert str(request.url) == "https://fanyi-api.baidu.com/api/trans/vip/translate"
        assert request.url.query == b""
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
        form = form_body(request)
        assert set(form) == {"q", "from", "to", "appid", "salt", "sign"}
        assert form["q"] == "Hello\nWorld"
        assert (form["from"], form["to"], form["appid"], form["salt"]) == ("en", "zh", APPID, SALT)
        assert form["sign"] == make_sign(APPID, "Hello\nWorld", SALT, SECRET)

    async def test_text_and_secret_never_in_url(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(ok("机密"))
        await collect(engine_for(client, sleeps, clock), make_job("Confidential text"))
        request = recorder.last
        url = str(request.url)
        assert "Confidential" not in url
        assert SECRET not in url
        # 密钥只参与签名，不出现在请求体里。
        assert SECRET not in request.content.decode("ascii")

    async def test_request_timeout(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(ok("你好"))
        await collect(engine_for(client, sleeps, clock), make_job("Hello"))
        assert recorder.last.extensions["timeout"] == {"connect": 3.0, "read": 8.0, "write": 8.0, "pool": 8.0}

    def test_constant_url(self) -> None:
        assert TRANSLATE_URL == "https://fanyi-api.baidu.com/api/trans/vip/translate"


class TestSign:
    def test_official_example(self) -> None:
        # 百度接入文档的示例：appid=2015063000000001，q=apple，salt=1435660288，密钥=12345678。
        assert make_sign("2015063000000001", "apple", "1435660288", "12345678") == (
            "f89f9594663708c1605f3d736d01d2d4"
        )

    async def test_sign_uses_raw_utf8_query(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(ok("Hello", "World"))
        engine = engine_for(client, sleeps, clock, secret="s3cr3t")
        await collect(engine, make_job("你好", "世界", source="zh-Hans", target="en"))
        form = form_body(recorder.last)
        assert form["q"] == "你好\n世界"
        # 预先算好的 MD5("20240101000123" + "你好\n世界" + "42" + "s3cr3t")，q 未经 URL 编码。
        assert form["sign"] == "dbf9ee4f03c197e2c51a91bb5ed69f43"

    async def test_salt_is_fresh_for_each_attempt(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        salts = iter(["100", "200"])
        recorder.queue(httpx.Response(503), ok("你好"))
        await collect(engine_for(client, sleeps, clock, salt=lambda: next(salts)), make_job("Hello"))
        first, second = (form_body(request) for request in recorder.requests)
        assert (first["salt"], second["salt"]) == ("100", "200")
        assert second["sign"] == make_sign(APPID, "Hello", "200", SECRET)


class TestNewlines:
    def test_flatten(self) -> None:
        assert flatten("a\r\nb\nc\rd") == "a b c d"
        assert flatten("a\n\nb") == "a  b"
        assert flatten("no break") == "no break"
        assert flatten("tab\tstays") == "tab\tstays"
        # str.splitlines 认作换行的其他字符也替换：行分隔符 U+2028、段分隔符 U+2029、NEL U+0085、垂直制表符。
        others = [chr(0x2028), chr(0x2029), chr(0x85), chr(0x0B)]
        assert flatten("x".join(others)) == "x".join(" " * len(others))

    async def test_inner_newlines_become_spaces(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(echo)
        job = make_job("first\r\nline", "second\nline\rend")
        outcomes = await collect(engine_for(client, sleeps, clock), job)
        assert form_body(recorder.last)["q"] == "first line\nsecond line end"
        assert texts_by_id(outcomes) == {"s1": "译:first line", "s2": "译:second line end"}

    async def test_segments_blank_after_flattening_are_not_sent(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(echo)
        outcomes = await collect(engine_for(client, sleeps, clock), make_job("A", "\r\n", "B", " "))
        assert form_body(recorder.last)["q"] == "A\nB"
        assert texts_by_id(outcomes) == {"s1": "译:A", "s2": "\r\n", "s3": "译:B", "s4": " "}


class TestSplitByBytes:
    def test_boundary(self) -> None:
        # 2999 + 1（换行）+ 3000 = 6000 字节，正好放下。
        assert split_by_bytes(["a" * 2999, "b" * 3000]) == [range(0, 2)]
        assert split_by_bytes(["a" * 3000, "b" * 3000]) == [range(0, 1), range(1, 2)]

    def test_counts_utf8_bytes_not_chars(self) -> None:
        # 每个汉字 3 字节：1000 字 = 3000 字节，两段加换行是 6001 字节。
        assert split_by_bytes(["汉" * 1000, "字" * 1000]) == [range(0, 1), range(1, 2)]
        assert split_by_bytes(["汉" * 999, "字" * 1000]) == [range(0, 2)]

    def test_oversized_segment_stands_alone(self) -> None:
        assert split_by_bytes(["a", "b" * 7000, "c"]) == [range(0, 1), range(1, 2), range(2, 3)]

    def test_empty(self) -> None:
        assert split_by_bytes([]) == []

    @given(st.lists(st.text(min_size=1, max_size=50), max_size=40), st.integers(min_value=1, max_value=200))
    def test_groups_cover_input_in_order_within_limit(self, texts: list[str], limit: int) -> None:
        groups = split_by_bytes(texts, limit)
        assert [index for group in groups for index in group] == list(range(len(texts)))
        for group in groups:
            assert len(group) > 0
            size = len("\n".join(texts[index] for index in group).encode("utf-8"))
            assert size <= limit or len(group) == 1
        # 贪心：相邻两组合并后一定超限。
        for left, right in itertools.pairwise(groups):
            merged = "\n".join(texts[index] for index in (*left, right[0]))
            assert len(merged.encode("utf-8")) > limit

    async def test_job_over_6000_bytes_is_split_into_ordered_requests(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        texts = ["汉" * 900, "字" * 900, "文" * 900, "本" * 10]  # 2700 字节一段
        recorder.queue(echo, echo)
        outcomes = await collect(
            engine_for(client, sleeps, clock), make_job(*texts, source="zh-Hans", target="en")
        )
        assert recorder.calls == 2
        queries = [form_body(request)["q"] for request in recorder.requests]
        assert queries == ["\n".join(texts[:2]), "\n".join(texts[2:])]
        assert all(len(q.encode("utf-8")) <= MAX_QUERY_BYTES for q in queries)
        assert [o.segment_id for o in outcomes] == ["s1", "s2", "s3", "s4"]
        assert [o.text for o in outcomes] == [f"译:{text}" for text in texts]

    async def test_failure_in_later_group_fails_the_rest_without_more_requests(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        texts = ["a" * 4000, "b" * 4000, "c" * 4000]
        recorder.queue(echo, api_error("54004", "Insufficient balance"))
        outcomes = await collect(engine_for(client, sleeps, clock), make_job(*texts))
        assert recorder.calls == 2
        assert outcomes[0].text == "译:" + "a" * 4000
        assert [(o.segment_id, o.code) for o in outcomes[1:]] == [
            ("s2", ErrorCode.QUOTA),
            ("s3", ErrorCode.QUOTA),
        ]


class TestErrorCodes:
    @pytest.mark.parametrize(
        ("error_code", "code"),
        [
            ("52003", ErrorCode.AUTH),
            ("54000", ErrorCode.BAD_REQUEST),
            ("54001", ErrorCode.AUTH),
            ("54004", ErrorCode.QUOTA),
            ("58000", ErrorCode.AUTH),
            ("58001", ErrorCode.UNSUPPORTED_LANGUAGE),
            ("58002", ErrorCode.SERVER),
            ("58003", ErrorCode.RATE_LIMITED),
            ("90107", ErrorCode.BAD_RESPONSE),
            (54001, ErrorCode.AUTH),  # 有的响应里 error_code 是整数
        ],
    )
    async def test_mapping_without_retry(
        self,
        client: httpx.AsyncClient,
        recorder: Recorder,
        sleeps: Sleeps,
        clock: FakeClock,
        error_code: str | int,
        code: ErrorCode,
    ) -> None:
        recorder.queue(api_error(error_code, "Some Message"))
        job = make_job("Hello", "World")
        outcomes = await collect(engine_for(client, sleeps, clock), job)
        assert_all_failed(outcomes, job, code)
        assert recorder.calls == 1
        assert sleeps.delays == []
        assert outcomes[0].error == f"百度错误码 {error_code}: Some Message"

    async def test_null_error_message(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(httpx.Response(200, json={"error_code": "54001", "error_msg": None}))
        job = make_job("Hello")
        outcomes = await collect(engine_for(client, sleeps, clock), job)
        assert_all_failed(outcomes, job, ErrorCode.AUTH)
        assert outcomes[0].error == "百度错误码 54001: "

    def test_rule_table(self) -> None:
        assert ERROR_RULES["52001"] == (ErrorCode.TIMEOUT, 1.0)
        assert ERROR_RULES["52002"] == (ErrorCode.SERVER, 1.0)
        assert ERROR_RULES["54003"] == (ErrorCode.RATE_LIMITED, 1.0)
        assert ERROR_RULES["54005"] == (ErrorCode.RATE_LIMITED, 3.0)
        retried = {code for code, (_, wait) in ERROR_RULES.items() if wait is not None}
        assert retried == {"52001", "52002", "54003", "54005"}

    @pytest.mark.parametrize(
        ("error_code", "wait"), [("52001", 1.0), ("52002", 1.0), ("54003", 1.0), ("54005", 3.0)]
    )
    async def test_retryable_codes_wait_then_retry_once(
        self,
        client: httpx.AsyncClient,
        recorder: Recorder,
        sleeps: Sleeps,
        clock: FakeClock,
        error_code: str,
        wait: float,
    ) -> None:
        recorder.queue(api_error(error_code), ok("你好"))
        outcomes = await collect(engine_for(client, sleeps, clock), make_job("Hello"))
        assert texts_by_id(outcomes) == {"s1": "你好"}
        assert recorder.calls == 2
        # 等待之后令牌桶已经补满，限速器不再额外等待。
        assert sleeps.delays == [wait]

    @pytest.mark.parametrize(
        ("error_code", "code"),
        [
            ("52001", ErrorCode.TIMEOUT),
            ("52002", ErrorCode.SERVER),
            ("54003", ErrorCode.RATE_LIMITED),
            ("54005", ErrorCode.RATE_LIMITED),
        ],
    )
    async def test_retryable_codes_twice_give_up(
        self,
        client: httpx.AsyncClient,
        recorder: Recorder,
        sleeps: Sleeps,
        clock: FakeClock,
        error_code: str,
        code: ErrorCode,
    ) -> None:
        recorder.queue(api_error(error_code), api_error(error_code))
        job = make_job("Hello", "World")
        assert_all_failed(await collect(engine_for(client, sleeps, clock), job), job, code)
        assert recorder.calls == 2

    async def test_retry_budget_is_shared_with_http_layer(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(httpx.Response(503), api_error("54003"))
        job = make_job("Hello")
        assert_all_failed(await collect(engine_for(client, sleeps, clock), job), job, ErrorCode.RATE_LIMITED)
        assert recorder.calls == 2

    async def test_http_retry_after_business_retry_is_not_allowed(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(api_error("54003"), httpx.Response(503))
        job = make_job("Hello")
        assert_all_failed(await collect(engine_for(client, sleeps, clock), job), job, ErrorCode.SERVER)
        assert recorder.calls == 2

    @pytest.mark.parametrize(
        "response",
        [
            ok(),
            ok("一", "二", "三"),
            httpx.Response(200, text="<html>"),
            httpx.Response(200, json={"from": "en", "to": "zh"}),
            httpx.Response(200, json={"trans_result": [{"src": "a"}, {"src": "b"}]}),
        ],
        ids=["too-few", "too-many", "not-json", "missing-result", "missing-dst"],
    )
    async def test_bad_response(
        self,
        client: httpx.AsyncClient,
        recorder: Recorder,
        sleeps: Sleeps,
        clock: FakeClock,
        response: httpx.Response,
    ) -> None:
        recorder.queue(response)
        job = make_job("Hello", "World")
        assert_all_failed(await collect(engine_for(client, sleeps, clock), job), job, ErrorCode.BAD_RESPONSE)

    async def test_success_code_52000_is_accepted(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(httpx.Response(200, json={"error_code": "52000", "trans_result": [{"dst": "你好"}]}))
        assert texts_by_id(await collect(engine_for(client, sleeps, clock), make_job("Hello"))) == {
            "s1": "你好"
        }


class TestMaxRetries:
    """max_retries 同时约束 HTTP 层与业务层的重试，两层合计的尝试次数不超过 max_retries + 1。"""

    async def test_zero_disables_http_retry(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(httpx.Response(503))
        job = make_job("Hello")
        outcomes = await collect(engine_for(client, sleeps, clock, max_retries=0), job)
        assert_all_failed(outcomes, job, ErrorCode.SERVER)
        assert (recorder.calls, sleeps.delays) == (1, [])

    @pytest.mark.parametrize(
        ("error_code", "code"),
        [
            ("52001", ErrorCode.TIMEOUT),
            ("52002", ErrorCode.SERVER),
            ("54003", ErrorCode.RATE_LIMITED),
            ("54005", ErrorCode.RATE_LIMITED),
        ],
    )
    async def test_zero_disables_business_retry(
        self,
        client: httpx.AsyncClient,
        recorder: Recorder,
        sleeps: Sleeps,
        clock: FakeClock,
        error_code: str,
        code: ErrorCode,
    ) -> None:
        recorder.queue(api_error(error_code))
        job = make_job("Hello")
        outcomes = await collect(engine_for(client, sleeps, clock, max_retries=0), job)
        assert_all_failed(outcomes, job, code)
        assert (recorder.calls, sleeps.delays) == (1, [])

    async def test_two_allows_two_business_retries(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(api_error("54003"), api_error("52002"), ok("你好"))
        outcomes = await collect(engine_for(client, sleeps, clock, max_retries=2), make_job("Hello"))
        assert texts_by_id(outcomes) == {"s1": "你好"}
        assert (recorder.calls, sleeps.delays) == (3, [1.0, 1.0])

    async def test_two_business_retries_then_give_up(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(*(api_error("54003") for _ in range(3)))
        job = make_job("Hello")
        outcomes = await collect(engine_for(client, sleeps, clock, max_retries=2), job)
        assert_all_failed(outcomes, job, ErrorCode.RATE_LIMITED)
        assert recorder.calls == 3

    async def test_two_allows_two_http_retries(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(httpx.Response(503), httpx.Response(502), httpx.Response(500))
        job = make_job("Hello")
        outcomes = await collect(engine_for(client, sleeps, clock, max_retries=2), job)
        assert_all_failed(outcomes, job, ErrorCode.SERVER)
        assert recorder.calls == 3

    @pytest.mark.parametrize(
        ("replies", "code"),
        [
            # HTTP 层先用掉两次重试，业务层不能再重试。
            ((httpx.Response(503), httpx.Response(503), api_error("54003")), ErrorCode.RATE_LIMITED),
            # 业务层先用掉一次，HTTP 层只剩一次。
            ((api_error("54005"), httpx.Response(503), httpx.Response(503)), ErrorCode.SERVER),
            # 两层交替，各用一次。
            ((httpx.Response(503), api_error("52001"), api_error("52001")), ErrorCode.TIMEOUT),
        ],
        ids=["http-then-business", "business-then-http", "alternating"],
    )
    async def test_two_is_shared_across_layers(
        self,
        client: httpx.AsyncClient,
        recorder: Recorder,
        sleeps: Sleeps,
        clock: FakeClock,
        replies: tuple[httpx.Response, ...],
        code: ErrorCode,
    ) -> None:
        recorder.queue(*replies)
        job = make_job("Hello")
        outcomes = await collect(engine_for(client, sleeps, clock, max_retries=2), job)
        assert_all_failed(outcomes, job, code)
        assert recorder.calls == 3

    async def test_two_succeeds_on_third_attempt_across_layers(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(httpx.Response(503), api_error("54005"), ok("你好"))
        outcomes = await collect(engine_for(client, sleeps, clock, max_retries=2), make_job("Hello"))
        assert texts_by_id(outcomes) == {"s1": "你好"}
        assert recorder.calls == 3


class TestHttpErrors:
    @pytest.mark.parametrize(
        ("status", "code", "calls"),
        [
            (401, ErrorCode.AUTH, 1),
            (403, ErrorCode.AUTH, 1),
            (456, ErrorCode.QUOTA, 1),
            (429, ErrorCode.RATE_LIMITED, 2),
            (500, ErrorCode.SERVER, 2),
            (502, ErrorCode.SERVER, 2),
        ],
    )
    async def test_status_mapping_and_retry(
        self,
        client: httpx.AsyncClient,
        recorder: Recorder,
        sleeps: Sleeps,
        clock: FakeClock,
        status: int,
        code: ErrorCode,
        calls: int,
    ) -> None:
        recorder.queue(*(httpx.Response(status) for _ in range(calls)))
        job = make_job("Hello")
        assert_all_failed(await collect(engine_for(client, sleeps, clock), job), job, code)
        assert recorder.calls == calls

    async def test_network_error(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(httpx.ConnectError("refused"), httpx.ConnectError("refused"))
        job = make_job("Hello")
        assert_all_failed(await collect(engine_for(client, sleeps, clock), job), job, ErrorCode.NETWORK)


class TestRateLimit:
    @staticmethod
    def stamping(clock: FakeClock, times: list[float]) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            times.append(clock.now)
            return echo(request)

        return handler

    @pytest.mark.parametrize(("tier", "interval"), [("standard", 1.0), ("advanced", 0.1), ("premium", 0.01)])
    async def test_requests_are_spaced_by_tier_qps(
        self,
        client: httpx.AsyncClient,
        recorder: Recorder,
        sleeps: Sleeps,
        clock: FakeClock,
        tier: str,
        interval: float,
    ) -> None:
        times: list[float] = []
        recorder.queue(*(self.stamping(clock, times) for _ in range(3)))
        engine = engine_for(client, sleeps, clock, tier=tier)
        await collect(engine, make_job("a" * 4000, "b" * 4000, "c" * 4000))
        assert times == [pytest.approx(0.0), pytest.approx(interval), pytest.approx(2 * interval)]

    async def test_limiter_is_shared_across_jobs_of_one_instance(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        times: list[float] = []
        recorder.queue(*(self.stamping(clock, times) for _ in range(2)))
        engine = engine_for(client, sleeps, clock)
        await asyncio.gather(collect(engine, make_job("first")), collect(engine, make_job("second")))
        assert times == [pytest.approx(0.0), pytest.approx(1.0)]

    async def test_instances_have_independent_limiters(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        times: list[float] = []
        recorder.queue(*(self.stamping(clock, times) for _ in range(2)))
        await collect(engine_for(client, sleeps, clock), make_job("first"))
        await collect(engine_for(client, sleeps, clock), make_job("second"))
        assert times == [0.0, 0.0]

    async def test_http_retry_also_waits_for_a_token(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        times: list[float] = []

        def failing(request: httpx.Request) -> httpx.Response:
            times.append(clock.now)
            return httpx.Response(503)

        recorder.queue(failing, self.stamping(clock, times))
        await collect(engine_for(client, sleeps, clock), make_job("Hello"))
        # 退避 0.5 秒后令牌只补了一半，限速器再等 0.5 秒，两次请求间隔 1 秒。
        assert sleeps.delays == [pytest.approx(0.5), pytest.approx(0.5)]
        assert times == [pytest.approx(0.0), pytest.approx(1.0)]

    def test_tier_is_validated(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            settings(tier="gold")


class TestLanguages:
    @pytest.mark.parametrize(
        ("lang", "code"),
        [
            ("zh-Hans", "zh"),
            ("zh-Hant", "cht"),
            ("en", "en"),
            ("ja", "jp"),
            ("ko", "kor"),
            ("fr", "fra"),
            ("es", "spa"),
            ("de", "de"),
            ("ru", "ru"),
            ("pt", "pt"),
            ("it", "it"),
            ("ar", "ara"),
            ("vi", "vie"),
            ("th", "th"),
        ],
    )
    def test_codes(self, lang: str, code: str) -> None:
        assert target_code(lang) == code
        assert source_code(lang) == code

    def test_unknown_source_is_auto_but_unknown_target_is_none(self) -> None:
        assert source_code("xx") == "auto"
        assert source_code("") == "auto"
        assert target_code("xx") is None
        assert "auto" not in CODES.values()

    @pytest.mark.parametrize(
        ("source", "target", "expected"),
        [
            ("en", "zh-Hans", True),
            ("zh-Hans", "zh-Hant", True),
            ("ja", "ko", True),
            ("en", "en", False),
            ("xx", "en", False),
            ("en", "xx", False),
        ],
    )
    async def test_supports(
        self,
        client: httpx.AsyncClient,
        sleeps: Sleeps,
        clock: FakeClock,
        source: str,
        target: str,
        expected: bool,
    ) -> None:
        assert engine_for(client, sleeps, clock).supports(Lang(source), Lang(target)) is expected

    async def test_request_uses_mapped_codes(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        recorder.queue(ok("안녕"), ok("你好"))
        engine = engine_for(client, sleeps, clock)
        await collect(engine, make_job("こんにちは", source="ja", target="ko"))
        await collect(engine, make_job("Hello", source="xx", target="zh-Hant"))
        first, second = (form_body(request) for request in recorder.requests)
        assert (first["from"], first["to"]) == ("jp", "kor")
        assert (second["from"], second["to"]) == ("auto", "cht")

    async def test_unsupported_target_fails_without_request(
        self, client: httpx.AsyncClient, recorder: Recorder, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        job = make_job("Hello", target="xx")
        outcomes = await collect(engine_for(client, sleeps, clock), job)
        assert_all_failed(outcomes, job, ErrorCode.UNSUPPORTED_LANGUAGE)
        assert recorder.calls == 0


class TestMissingSecret:
    @pytest.mark.parametrize(
        "secrets", [FakeSecrets({}), FakeSecrets({"baidu": ""})], ids=["absent", "empty"]
    )
    async def test_missing_secret_fails_without_request(
        self, client: httpx.AsyncClient, recorder: Recorder, secrets: FakeSecrets
    ) -> None:
        engine = create_baidu_engine(settings(), secrets, client)
        job = make_job("Hello", "World")
        assert_all_failed(await collect(engine, job), job, ErrorCode.AUTH)
        assert recorder.calls == 0

    def test_appid_is_required(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            settings(appid="")


class TestInfo:
    async def test_info_fields(self, client: httpx.AsyncClient, sleeps: Sleeps, clock: FakeClock) -> None:
        info = engine_for(client, sleeps, clock).info
        assert info.kind is EngineKind.ONLINE_MT
        assert info.billing is True
        assert (info.max_batch_segments, info.max_batch_chars) == (100, 1800)
        assert info.timeout_s == 8.0

    async def test_fingerprint_ignores_secret_appid_and_tier(
        self, client: httpx.AsyncClient, sleeps: Sleeps, clock: FakeClock
    ) -> None:
        base = engine_for(client, sleeps, clock).info
        variants = [
            engine_for(client, sleeps, clock, secret="other").info,
            engine_for(client, sleeps, clock, appid="999").info,
            engine_for(client, sleeps, clock, tier="premium").info,
        ]
        for info in variants:
            assert info.fingerprint == base.fingerprint
            assert info.revision != base.revision
        assert SECRET not in repr(base)


class TestPrivacy:
    async def test_logs_contain_no_secret_or_text(
        self,
        client: httpx.AsyncClient,
        recorder: Recorder,
        sleeps: Sleeps,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        capture_all(caplog)
        recorder.queue(
            ok("机密译文甲"), api_error("54003", "Access Limit"), api_error("54001", "Invalid Sign")
        )
        engine = engine_for(client, sleeps, clock)
        await collect(engine, make_job("Secret source alpha"))
        await collect(engine, make_job("Secret source beta"))
        assert_private(caplog, SECRET, "Secret source", "机密译文")
        assert any(record.name == "httpx" for record in caplog.records)
        assert any(record.name.startswith("lst.services.engines.baidu") for record in caplog.records)


class TestCancellation:
    async def test_cancel_propagates_during_request(self, sleeps: Sleeps, clock: FakeClock) -> None:
        started = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.MockTransport(hanging(started))) as client:
            engine = engine_for(client, sleeps, clock)
            assert await cancel_while_pending(engine, make_job("Hello"), started) == []

    async def test_cancel_propagates_during_rate_limit_wait(
        self, client: httpx.AsyncClient, recorder: Recorder
    ) -> None:
        started = asyncio.Event()

        async def blocking_sleep(delay: float) -> None:
            started.set()
            await asyncio.Event().wait()

        recorder.queue(api_error("54005"))
        engine = create_baidu_engine(
            settings(), FakeSecrets({"baidu": SECRET}), client, sleep=blocking_sleep, clock=FakeClock()
        )
        assert await cancel_while_pending(engine, make_job("Hello"), started) == []
        assert recorder.calls == 1

    async def test_cancel_between_groups_keeps_earlier_results(
        self, client: httpx.AsyncClient, recorder: Recorder, clock: FakeClock
    ) -> None:
        started = asyncio.Event()

        async def blocking_sleep(delay: float) -> None:
            started.set()
            await asyncio.Event().wait()

        recorder.queue(echo)
        engine = create_baidu_engine(
            settings(), FakeSecrets({"baidu": SECRET}), client, sleep=blocking_sleep, clock=clock
        )
        produced = await cancel_while_pending(engine, make_job("a" * 4000, "b" * 4000), started)
        # 第一组已经返回；第二组在限速器里等待令牌时被取消。
        assert [o.segment_id for o in produced] == ["s1"]
        assert recorder.calls == 1
