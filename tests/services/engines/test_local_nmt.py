"""本地 NMT 引擎的测试：能力判断、请求构造、回复转换、取消传播、EngineInfo、与调度器联用和日志隐私。

NMT 进程与监管器由假的 WorkerChannel 扮演。每个请求的回复放在一个 asyncio.Queue 里，
可以在提交时按脚本一次放好，也可以由测试逐条推送，从而控制回复到达的时机。
等待某个状态出现时订阅 Signal，每次变化后重新检查条件，不靠轮询。
"""

import asyncio
import logging
import re
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import pydantic
import pytest

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import EngineId, Entry, Lang, SegmentId, SessionId
from lst.contracts.ports import WorkerState
from lst.contracts.translation import (
    AttemptState,
    AttemptView,
    Engine,
    EngineInfo,
    EngineKind,
    Job,
    Segment,
    SegmentOutcome,
    SegmentView,
    SkipReason,
)
from lst.contracts.worker_protocol import (
    CANCELLED,
    JsonValue,
    OcrRequest,
    OcrResult,
    RequestDone,
    RequestFailed,
    SegmentResult,
    Started,
    TranslateRequest,
    TranslateSegment,
    WorkerReply,
    decode,
    encode,
)
from lst.services.engines.local_nmt import LocalNmtSettings, create_local_nmt_engine
from lst.services.engines.local_nmt import engine as engine_module
from lst.services.engines.local_nmt.capabilities import NmtCapabilities
from lst.services.engines.local_nmt.engine import ERROR_CANCELLED, ERROR_MALFORMED, ERROR_SUBMIT
from lst.services.race import RaceScheduler
from tests.services.engines.mt_helpers import assert_private, capture_all, collect, make_job

LOGGER = "lst.services.engines.local_nmt.engine"

EN, ZH, JA, DE, FR = Lang("en"), Lang("zh-Hans"), Lang("ja"), Lang("de"), Lang("fr")
S1, S2, S3, S4, S5 = (SegmentId(f"s{i}") for i in range(1, 6))
SESSION = SessionId("sess")

# 直连：en↔zh-Hans、en↔ja、de→en；经 en 中转可达 ja↔zh-Hans、de→zh-Hans、de→ja。
CAPS: dict[str, JsonValue] = {
    "pairs": [["en", "zh-Hans"], ["zh-Hans", "en"], ["ja", "en"], ["en", "ja"], ["de", "en"]],
    "pivot": "en",
    "fingerprint": "models-v1",
}


# ---------------------------------------------------------------- 测试替身


class Signal:
    """状态变化通知。wait_until 在每次 notify 后重新检查条件，超时则测试失败。"""

    def __init__(self) -> None:
        self._waiters: list[asyncio.Future[None]] = []

    def notify(self) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._waiters.clear()

    async def wait_until(self, predicate: Callable[[], bool], timeout: float = 2.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                self._waiters.append(waiter)
                await waiter


class StreamEnd:
    """放进回复队列后，回复流不带 RequestDone 直接结束。"""


END = StreamEnd()

type Item = WorkerReply | StreamEnd | Exception
type Script = Callable[[TranslateRequest], Sequence[Item]]


class FakeHandle:
    """一个已提交的请求。

    回复从 queue 中取出：WorkerReply 原样产出，END 让回复流结束，Exception 从回复流里抛出。
    reading 表示引擎正在等下一条回复；closed 表示回复流已经结束（包括被关闭）；
    cancel_calls 记录 cancel 的调用次数，cancel_error 不为 None 时 cancel 在计数后抛出它。
    """

    def __init__(self, request: TranslateRequest, signal: Signal) -> None:
        self.request = request
        self.queue: asyncio.Queue[Item] = asyncio.Queue()
        self.signal = signal
        self.reading = False
        self.closed = False
        self.cancel_calls = 0
        self.cancel_error: Exception | None = None

    @property
    def request_id(self) -> str:
        return self.request.request_id

    def push(self, *items: Item) -> None:
        for item in items:
            self.queue.put_nowait(item)

    async def replies(self) -> AsyncIterator[WorkerReply]:
        try:
            while True:
                if self.queue.empty():
                    self.reading = True
                    self.signal.notify()
                item = await self.queue.get()
                self.reading = False
                if isinstance(item, StreamEnd):
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self.reading = False
            self.closed = True
            self.signal.notify()

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.signal.notify()
        if self.cancel_error is not None:
            raise self.cancel_error


@dataclass(frozen=True, slots=True)
class Submission:
    request: TranslateRequest
    timeout_s: float
    payload: bytes | None
    handle: FakeHandle


class FakeChannel:
    """假的 WorkerChannel。

    script 不为 None 时，每个请求提交后立即按脚本放好全部回复；为 None 时由测试逐条推送。
    fail_submit 为真时 submit 抛出异常，异常信息里带着请求的 repr（其中有原文），用来检查日志。
    """

    def __init__(
        self,
        capabilities: Mapping[str, JsonValue] | None = CAPS,
        *,
        state: WorkerState = WorkerState.READY,
        script: Script | None = None,
    ) -> None:
        self.capabilities: Mapping[str, JsonValue] | None = capabilities
        self.state: WorkerState = state
        self.script = script
        self.fail_submit = False
        self.submissions: list[Submission] = []
        self.signal = Signal()

    def submit(
        self, request: TranslateRequest | OcrRequest, *, payload: bytes | None = None, timeout_s: float
    ) -> FakeHandle:
        assert isinstance(request, TranslateRequest)
        if self.fail_submit:
            raise RuntimeError(f"监管器拒绝了请求 {request!r}")
        handle = FakeHandle(request, self.signal)
        if self.script is not None:
            handle.push(*self.script(request))
        self.submissions.append(Submission(request, timeout_s, payload, handle))
        self.signal.notify()
        return handle

    @property
    def last(self) -> FakeHandle:
        return self.submissions[-1].handle


def translated(text: str) -> str:
    """假译文：与原文不同，也不包含原文（测试里的原文都是小写 ASCII），便于在日志里分别检查。"""
    return f"{text.upper()}（译）"


def translate_all(request: TranslateRequest) -> list[Item]:
    """正常完成的回复序列：Started、每段一条译文、RequestDone。"""
    rid = request.request_id
    results = [SegmentResult(rid, segment.id, text=translated(segment.text)) for segment in request.segments]
    return [Started(rid), *results, RequestDone(rid)]


def settings(**overrides: object) -> LocalNmtSettings:
    fields: dict[str, object] = {"id": "nmt", "name": "Argos", "priority": 3}
    return LocalNmtSettings.model_validate({**fields, **overrides})


def engine_for(channel: FakeChannel, **overrides: object) -> Engine:
    return create_local_nmt_engine(settings(**overrides), channel)


async def wait_reading(channel: FakeChannel, count: int = 1) -> FakeHandle:
    """等到第 count 个请求已经提交，并且引擎正在等它的下一条回复。"""

    def ready() -> bool:
        return len(channel.submissions) >= count and channel.submissions[count - 1].handle.reading

    await channel.signal.wait_until(ready)
    return channel.submissions[count - 1].handle


def generator(stream: AsyncIterator[SegmentOutcome]) -> AsyncGenerator[SegmentOutcome]:
    """translate 的返回值是异步生成器，测试要用到它的 aclose。"""
    assert isinstance(stream, AsyncGenerator)
    return cast(AsyncGenerator[SegmentOutcome], stream)


def engine_warnings(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == logging.WARNING and needle in record.getMessage()
    ]


def failure(segment_id: SegmentId, code: ErrorCode, detail: str) -> SegmentOutcome:
    return SegmentOutcome(segment_id, error=detail, code=code)


# ---------------------------------------------------------------- 配置


class TestSettings:
    def test_defaults(self) -> None:
        value = settings()
        assert value.timeout_s == 10.0
        assert value.processing_timeout_s == 5.0
        assert value.max_concurrency == 1
        assert value.max_batch_segments == 16
        assert value.max_batch_chars == 800
        # 本地 NMT 不重试，max_retries 固定为 0（见 TestReplies.test_never_retries）。
        assert value.max_retries == 0

    def test_max_retries_cannot_be_raised(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            settings(max_retries=1)

    @pytest.mark.parametrize("value", [0, -1, 601])
    def test_processing_timeout_must_be_positive_and_bounded(self, value: float) -> None:
        with pytest.raises(pydantic.ValidationError):
            settings(processing_timeout_s=value)

    def test_unknown_fields_are_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            settings(model_dir="C:/models")


# ---------------------------------------------------------------- supports


class TestSupports:
    @pytest.mark.parametrize(("source", "target"), [(EN, ZH), (ZH, EN), (JA, EN), (EN, JA), (DE, EN)])
    def test_direct_pairs(self, source: Lang, target: Lang) -> None:
        assert engine_for(FakeChannel()).supports(source, target)

    @pytest.mark.parametrize(("source", "target"), [(JA, ZH), (ZH, JA), (DE, ZH), (DE, JA)])
    def test_two_hops_through_pivot(self, source: Lang, target: Lang) -> None:
        assert engine_for(FakeChannel()).supports(source, target)

    @pytest.mark.parametrize(("source", "target"), [(ZH, DE), (JA, DE), (EN, DE), (FR, ZH), (ZH, FR)])
    def test_missing_hop(self, source: Lang, target: Lang) -> None:
        # 没有 en→de 与 fr 的任何模型。
        assert not engine_for(FakeChannel()).supports(source, target)

    @pytest.mark.parametrize("lang", [EN, ZH, FR])
    def test_same_language(self, lang: Lang) -> None:
        caps: dict[str, JsonValue] = {"pairs": [[lang, lang]], "pivot": "en", "fingerprint": ""}
        assert not engine_for(FakeChannel(caps)).supports(lang, lang)

    def test_pivot_language_as_endpoint_needs_direct_model(self) -> None:
        caps: dict[str, JsonValue] = {"pairs": [["fr", "en"], ["en", "de"]], "pivot": "en", "fingerprint": ""}
        engine = engine_for(FakeChannel(caps))
        assert engine.supports(FR, DE)  # fr→en→de
        assert engine.supports(FR, EN)
        assert not engine.supports(EN, FR)
        assert not engine.supports(DE, EN)

    @pytest.mark.parametrize("pivot", [None, "missing"])
    def test_without_pivot_only_direct_pairs(self, pivot: str | None) -> None:
        caps = {key: value for key, value in CAPS.items() if key != "pivot"}
        if pivot is None:
            caps["pivot"] = None
        engine = engine_for(FakeChannel(caps))
        assert engine.supports(EN, ZH)
        assert not engine.supports(JA, ZH)
        assert not engine.supports(DE, ZH)

    def test_no_capabilities(self) -> None:
        engine = engine_for(FakeChannel(None))
        assert not engine.supports(EN, ZH)
        assert not engine.supports(JA, ZH)

    @pytest.mark.parametrize(
        "caps",
        [
            pytest.param({"fingerprint": "x"}, id="no-pairs"),
            pytest.param({"pairs": "en-zh-Hans"}, id="pairs-not-list"),
            pytest.param({"pairs": [["en"]]}, id="pair-too-short"),
            pytest.param({"pairs": [["en", "zh-Hans", "ja"]]}, id="pair-too-long"),
            pytest.param({"pairs": [["en", 1]]}, id="code-not-string"),
            pytest.param({"pairs": [["", "zh-Hans"]]}, id="empty-code"),
            pytest.param({"pairs": ["en"]}, id="pair-not-list"),
            pytest.param({"pairs": [["en", "zh-Hans"]], "pivot": 1}, id="pivot-not-string"),
            pytest.param({"pairs": [["en", "zh-Hans"]], "pivot": ""}, id="pivot-empty"),
        ],
    )
    def test_malformed_capabilities_warn_once(
        self, caps: dict[str, JsonValue], caplog: pytest.LogCaptureFixture
    ) -> None:
        channel = FakeChannel()
        engine = engine_for(channel)
        caplog.set_level(logging.WARNING, logger=LOGGER)
        channel.capabilities = caps
        for _ in range(3):
            assert not engine.supports(EN, ZH)
            assert not engine.supports(JA, ZH)
        assert len(engine_warnings(caplog, "能力清单格式不对")) == 1

    def test_different_malformed_capabilities_warn_again(self, caplog: pytest.LogCaptureFixture) -> None:
        channel = FakeChannel()
        engine = engine_for(channel)
        caplog.set_level(logging.WARNING, logger=LOGGER)
        channel.capabilities = {"pairs": "bad"}
        engine.supports(EN, ZH)
        engine.supports(EN, ZH)
        channel.capabilities = {"pairs": [["en"]]}
        engine.supports(EN, ZH)
        assert len(engine_warnings(caplog, "能力清单格式不对")) == 2

    def test_capabilities_parsed_once_per_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一份能力清单对象只解析一次；通道换上新的清单对象时重新解析。"""
        calls: list[object] = []
        real = engine_module.parse_capabilities

        def counting(raw: Mapping[str, JsonValue]) -> NmtCapabilities:
            calls.append(raw)
            return real(raw)

        monkeypatch.setattr(engine_module, "parse_capabilities", counting)
        channel = FakeChannel()
        engine = engine_for(channel)
        for _ in range(5):
            assert engine.supports(EN, ZH)
            assert engine.supports(JA, ZH)
        assert len(calls) == 1
        channel.capabilities = dict(CAPS)
        assert engine.supports(EN, ZH)
        assert len(calls) == 2

    def test_follows_capability_changes(self) -> None:
        channel = FakeChannel(None)
        engine = engine_for(channel)
        assert not engine.supports(JA, ZH)
        channel.capabilities = CAPS
        assert engine.supports(JA, ZH)
        # NMT 进程重载后少了 en→zh-Hans：直连与中转都不再可用。
        channel.capabilities = {"pairs": [["ja", "en"]], "pivot": "en", "fingerprint": "models-v2"}
        assert not engine.supports(EN, ZH)
        assert not engine.supports(JA, ZH)
        assert engine.supports(JA, EN)
        channel.capabilities = {"pairs": "bad"}
        assert not engine.supports(JA, EN)
        channel.capabilities = CAPS
        assert engine.supports(JA, ZH)


# ---------------------------------------------------------------- 请求


class TestRequest:
    async def test_request_carries_job(self) -> None:
        channel = FakeChannel(script=translate_all)
        job = make_job("gamma", "alpha", "beta", source="ja", target="zh-Hans")
        await collect(engine_for(channel), job)

        [submission] = channel.submissions
        request = submission.request
        assert re.fullmatch(r"[0-9a-f]{32}", request.request_id)
        assert request.session_id == job.session_id
        assert (request.source, request.target) == ("ja", "zh-Hans")
        assert request.segments == (
            TranslateSegment("s1", "gamma"),
            TranslateSegment("s2", "alpha"),
            TranslateSegment("s3", "beta"),
        )
        assert submission.timeout_s == 5.0
        assert submission.payload is None
        # 请求能按协议编解码，说明字段类型都符合协议。
        assert decode(encode(request)) == request

    async def test_processing_timeout_is_separate_from_job_timeout(self) -> None:
        channel = FakeChannel(script=translate_all)
        engine = engine_for(channel, processing_timeout_s=2.5, timeout_s=30)
        await collect(engine, make_job("alpha"))
        assert channel.submissions[0].timeout_s == 2.5
        assert engine.info.timeout_s == 30

    async def test_each_job_gets_new_request_id(self) -> None:
        channel = FakeChannel(script=translate_all)
        engine = engine_for(channel)
        await collect(engine, make_job("alpha"))
        await collect(engine, make_job("alpha"))
        first, second = (s.request.request_id for s in channel.submissions)
        assert first != second

    async def test_empty_job_submits_nothing(self) -> None:
        channel = FakeChannel(script=translate_all)
        assert await collect(engine_for(channel), make_job()) == []
        assert channel.submissions == []


# ---------------------------------------------------------------- 回复


class TestReplies:
    async def test_outcomes_follow_reply_order(self) -> None:
        def script(request: TranslateRequest) -> list[Item]:
            rid = request.request_id
            return [
                Started(rid),
                SegmentResult(rid, "s2", text="BETA"),
                SegmentResult(rid, "s1", text="ALPHA"),
                RequestDone(rid),
            ]

        channel = FakeChannel(script=script)
        outcomes = await collect(engine_for(channel), make_job("alpha", "beta"))
        assert outcomes == [SegmentOutcome(S2, text="BETA"), SegmentOutcome(S1, text="ALPHA")]
        assert channel.last.cancel_calls == 0
        assert channel.last.closed

    async def test_first_segment_is_yielded_before_later_replies(self) -> None:
        channel = FakeChannel()
        stream = engine_for(channel).translate(make_job("alpha", "beta"))
        first = asyncio.ensure_future(anext(stream))
        handle = await wait_reading(channel)
        rid = handle.request_id
        handle.push(Started(rid), SegmentResult(rid, "s1", text="ALPHA"))

        # 第二段的回复还没有推送，第一段已经产出。
        assert await asyncio.wait_for(first, timeout=2) == SegmentOutcome(S1, text="ALPHA")
        assert handle.queue.empty()

        handle.push(SegmentResult(rid, "s2", text="BETA"), RequestDone(rid))
        assert [outcome async for outcome in stream] == [SegmentOutcome(S2, text="BETA")]
        assert handle.cancel_calls == 0

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("engine.model_missing", ErrorCode.MODEL_MISSING),
            ("engine.unsupported_language", ErrorCode.UNSUPPORTED_LANGUAGE),
            ("engine.internal", ErrorCode.INTERNAL),
            (None, ErrorCode.INTERNAL),
        ],
    )
    async def test_error_codes_are_mapped(self, raw: str | None, expected: ErrorCode) -> None:
        def script(request: TranslateRequest) -> list[Item]:
            rid = request.request_id
            return [SegmentResult(rid, "s1", error="未安装 ja→en 模型", code=raw), RequestDone(rid)]

        outcomes = await collect(engine_for(FakeChannel(script=script)), make_job("alpha"))
        assert outcomes == [failure(S1, expected, "未安装 ja→en 模型")]

    async def test_unknown_error_code_becomes_internal(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)

        def script(request: TranslateRequest) -> list[Item]:
            rid = request.request_id
            return [SegmentResult(rid, "s1", error="出错了", code="engine.gpu_on_fire"), RequestDone(rid)]

        [outcome] = await collect(engine_for(FakeChannel(script=script)), make_job("alpha"))
        assert outcome.code is ErrorCode.INTERNAL
        assert outcome.error is not None
        assert "engine.gpu_on_fire" in outcome.error
        assert engine_warnings(caplog, "未知错误码")

    async def test_empty_error_detail_is_filled(self) -> None:
        def script(request: TranslateRequest) -> list[Item]:
            rid = request.request_id
            return [SegmentResult(rid, "s1", error="", code="engine.model_missing"), RequestDone(rid)]

        [outcome] = await collect(engine_for(FakeChannel(script=script)), make_job("alpha"))
        assert outcome.code is ErrorCode.MODEL_MISSING
        assert outcome.error

    @pytest.mark.parametrize(
        ("text", "error"),
        [pytest.param(None, None, id="neither"), pytest.param("ALPHA", "出错了", id="both")],
    )
    async def test_malformed_result_fails_as_internal(
        self, text: str | None, error: str | None, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)

        def script(request: TranslateRequest) -> list[Item]:
            rid = request.request_id
            return [
                SegmentResult(rid, "s1", text=text, error=error, code="engine.model_missing"),
                SegmentResult(rid, "s2", text="BETA"),
                RequestDone(rid),
            ]

        outcomes = await collect(engine_for(FakeChannel(script=script)), make_job("alpha", "beta"))
        assert outcomes == [
            failure(S1, ErrorCode.INTERNAL, ERROR_MALFORMED),
            SegmentOutcome(S2, text="BETA"),
        ]
        assert engine_warnings(caplog, "按内部错误处理")

    async def test_success_ignores_error_code(self) -> None:
        def script(request: TranslateRequest) -> list[Item]:
            rid = request.request_id
            return [SegmentResult(rid, "s1", text="ALPHA", code="engine.internal"), RequestDone(rid)]

        outcomes = await collect(engine_for(FakeChannel(script=script)), make_job("alpha"))
        assert outcomes == [SegmentOutcome(S1, text="ALPHA")]

    async def test_foreign_and_duplicate_segments_are_ignored(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)

        def script(request: TranslateRequest) -> list[Item]:
            rid = request.request_id
            return [
                SegmentResult(rid, "s9", text="NOT MINE"),
                SegmentResult(rid, "s1", text="ALPHA"),
                SegmentResult(rid, "s1", text="ALPHA AGAIN"),
                SegmentResult(rid, "s1", error="迟到的失败", code="engine.internal"),
                SegmentResult(rid, "s2", text="BETA"),
                RequestDone(rid),
            ]

        channel = FakeChannel(script=script)
        outcomes = await collect(engine_for(channel), make_job("alpha", "beta"))
        assert outcomes == [SegmentOutcome(S1, text="ALPHA"), SegmentOutcome(S2, text="BETA")]
        assert len(engine_warnings(caplog, "不属于本任务的段 s9")) == 1
        assert len(engine_warnings(caplog, "重复的段 s1")) == 2
        assert channel.last.cancel_calls == 0

    async def test_replies_of_other_requests_are_ignored(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)

        def script(request: TranslateRequest) -> list[Item]:
            rid = request.request_id
            return [
                SegmentResult("other", "s1", text="NOT MINE"),
                RequestDone("other"),
                OcrResult(rid, blocks=[]),
                SegmentResult(rid, "s1", text="ALPHA"),
                RequestDone(rid),
            ]

        outcomes = await collect(engine_for(FakeChannel(script=script)), make_job("alpha"))
        assert outcomes == [SegmentOutcome(S1, text="ALPHA")]
        assert len(engine_warnings(caplog, "属于请求 other 的回复")) == 2
        assert len(engine_warnings(caplog, "不属于翻译请求的回复 ocr_result")) == 1

    @pytest.mark.parametrize(
        ("raw", "code", "detail"),
        [
            ("engine.worker_crashed", ErrorCode.WORKER_CRASHED, "NMT 进程崩溃"),
            ("engine.timeout", ErrorCode.TIMEOUT, "NMT 进程处理超时"),
            (CANCELLED, ErrorCode.INTERNAL, f"{ERROR_CANCELLED}（NMT 进程丢弃了请求）"),
            ("engine.gpu_on_fire", ErrorCode.INTERNAL, "NMT 进程出错（未知错误码 engine.gpu_on_fire）"),
        ],
    )
    async def test_request_failed_fails_remaining_segments(
        self, raw: str, code: ErrorCode, detail: str
    ) -> None:
        error = {
            "engine.worker_crashed": "NMT 进程崩溃",
            "engine.timeout": "NMT 进程处理超时",
            CANCELLED: "NMT 进程丢弃了请求",
            "engine.gpu_on_fire": "NMT 进程出错",
        }[raw]

        def script(request: TranslateRequest) -> list[Item]:
            rid = request.request_id
            return [
                Started(rid),
                SegmentResult(rid, "s2", text="BETA"),
                RequestFailed(rid, raw, error),
                SegmentResult(rid, "s1", text="TOO LATE"),
            ]

        channel = FakeChannel(script=script)
        outcomes = await collect(engine_for(channel), make_job("alpha", "beta", "gamma"))
        assert outcomes == [
            SegmentOutcome(S2, text="BETA"),
            failure(S1, code, detail),
            failure(S3, code, detail),
        ]
        assert channel.last.cancel_calls == 0

    async def test_cancelled_request_without_detail(self) -> None:
        def script(request: TranslateRequest) -> list[Item]:
            return [RequestFailed(request.request_id, CANCELLED, "")]

        outcomes = await collect(engine_for(FakeChannel(script=script)), make_job("alpha"))
        assert outcomes == [failure(S1, ErrorCode.INTERNAL, ERROR_CANCELLED)]

    async def test_failed_worker_fails_every_segment(self) -> None:
        # 监管器处于 FAILED 时，按端口约定立即以 worker_crashed 结束请求（架构文档 6.5 节第 3 条）。
        def script(request: TranslateRequest) -> list[Item]:
            return [RequestFailed(request.request_id, "engine.worker_crashed", "NMT 进程已停用")]

        channel = FakeChannel(state=WorkerState.FAILED, script=script)
        job = make_job("alpha", "beta")
        outcomes = await collect(engine_for(channel), job)
        assert outcomes == [
            failure(S1, ErrorCode.WORKER_CRASHED, "NMT 进程已停用"),
            failure(S2, ErrorCode.WORKER_CRASHED, "NMT 进程已停用"),
        ]

    async def test_stream_end_with_missing_segments_only_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)

        def script(request: TranslateRequest) -> list[Item]:
            return [SegmentResult(request.request_id, "s1", text="ALPHA"), END]

        channel = FakeChannel(script=script)
        outcomes = await collect(engine_for(channel), make_job("alpha", "beta", "gamma"))
        # 缺的段由调度器记为“引擎未返回该段”，引擎不补失败结果。
        assert outcomes == [SegmentOutcome(S1, text="ALPHA")]
        assert engine_warnings(caplog, "回复流提前结束，2 段没有结果")
        assert channel.last.cancel_calls == 0

    async def test_request_done_with_missing_segments_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)

        def script(request: TranslateRequest) -> list[Item]:
            rid = request.request_id
            return [SegmentResult(rid, "s2", text="BETA"), RequestDone(rid)]

        channel = FakeChannel(script=script)
        outcomes = await collect(engine_for(channel), make_job("alpha", "beta"))
        assert outcomes == [SegmentOutcome(S2, text="BETA")]
        assert engine_warnings(caplog, "已结束，1 段没有结果")
        assert channel.last.cancel_calls == 0

    async def test_stream_error_fails_rest_and_cancels(self) -> None:
        def script(request: TranslateRequest) -> list[Item]:
            return [SegmentResult(request.request_id, "s1", text="ALPHA"), ConnectionResetError("管道断开")]

        channel = FakeChannel(script=script)
        outcomes = await collect(engine_for(channel), make_job("alpha", "beta"))
        assert outcomes == [
            SegmentOutcome(S1, text="ALPHA"),
            failure(S2, ErrorCode.INTERNAL, "读取 NMT 进程回复出错（ConnectionResetError）"),
        ]
        assert channel.last.cancel_calls == 1

    async def test_submit_error_fails_every_segment(self) -> None:
        channel = FakeChannel(script=translate_all)
        channel.fail_submit = True
        outcomes = await collect(engine_for(channel), make_job("alpha", "beta"))
        assert outcomes == [
            failure(S1, ErrorCode.INTERNAL, ERROR_SUBMIT),
            failure(S2, ErrorCode.INTERNAL, ERROR_SUBMIT),
        ]

    async def test_never_retries(self) -> None:
        def script(request: TranslateRequest) -> list[Item]:
            return [RequestFailed(request.request_id, "engine.worker_crashed", "NMT 进程崩溃")]

        channel = FakeChannel(script=script)
        outcomes = await collect(engine_for(channel), make_job("alpha"))
        assert outcomes == [failure(S1, ErrorCode.WORKER_CRASHED, "NMT 进程崩溃")]
        assert len(channel.submissions) == 1


# ---------------------------------------------------------------- 取消


class TestCancellation:
    async def test_task_cancel_while_waiting_cancels_request(self) -> None:
        channel = FakeChannel()
        engine = engine_for(channel)
        produced: list[SegmentOutcome] = []

        async def consume() -> None:
            async for outcome in engine.translate(make_job("alpha", "beta")):
                produced.append(outcome)

        task = asyncio.create_task(consume())
        handle = await wait_reading(channel)
        handle.push(Started(handle.request_id), SegmentResult(handle.request_id, "s1", text="ALPHA"))
        await channel.signal.wait_until(lambda: len(produced) == 1 and handle.reading)
        assert handle.cancel_calls == 0

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert produced == [SegmentOutcome(S1, text="ALPHA")]
        assert handle.cancel_calls == 1
        assert handle.closed

    async def test_cancel_before_any_reply(self) -> None:
        channel = FakeChannel()
        task = asyncio.create_task(collect(engine_for(channel), make_job("alpha")))
        handle = await wait_reading(channel)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert handle.cancel_calls == 1

    async def test_aclose_cancels_request(self) -> None:
        channel = FakeChannel(script=translate_all)
        stream = generator(engine_for(channel).translate(make_job("alpha", "beta")))
        assert await anext(stream) == SegmentOutcome(S1, text=translated("alpha"))
        await stream.aclose()
        assert channel.last.cancel_calls == 1
        assert channel.last.closed

    async def test_normal_completion_does_not_cancel(self) -> None:
        channel = FakeChannel(script=translate_all)
        stream = generator(engine_for(channel).translate(make_job("alpha", "beta")))
        outcomes = [outcome async for outcome in stream]
        await stream.aclose()
        assert outcomes == [
            SegmentOutcome(S1, text=translated("alpha")),
            SegmentOutcome(S2, text=translated("beta")),
        ]
        assert channel.last.cancel_calls == 0
        assert channel.last.closed

    async def test_close_after_request_failed_does_not_cancel(self) -> None:
        def script(request: TranslateRequest) -> list[Item]:
            return [RequestFailed(request.request_id, "engine.worker_crashed", "NMT 进程崩溃")]

        channel = FakeChannel(script=script)
        stream = generator(engine_for(channel).translate(make_job("alpha", "beta")))
        assert await anext(stream) == failure(S1, ErrorCode.WORKER_CRASHED, "NMT 进程崩溃")
        await stream.aclose()
        assert channel.last.cancel_calls == 0

    async def test_cancel_error_does_not_mask_cancelled_error(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)
        channel = FakeChannel()
        task = asyncio.create_task(collect(engine_for(channel), make_job("alpha")))
        handle = await wait_reading(channel)
        handle.cancel_error = RuntimeError("监管器已关闭")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert handle.cancel_calls == 1
        assert engine_warnings(caplog, "时出错（RuntimeError）")


# ---------------------------------------------------------------- EngineInfo


class TestEngineInfo:
    def test_fields(self) -> None:
        info = engine_for(FakeChannel(), color="#0a0").info
        assert info.id == EngineId("nmt")
        assert info.name == "Argos"
        assert info.kind is EngineKind.LOCAL_NMT
        assert info.priority == 3
        assert info.billing is False
        assert info.max_concurrency == 1
        assert info.max_batch_segments == 16
        assert info.max_batch_chars == 800
        assert info.timeout_s == 10.0
        assert info.color == "#0a0"

    def test_batch_settings_are_passed_through(self) -> None:
        info = engine_for(FakeChannel(), max_batch_segments=4, max_batch_chars=600, max_concurrency=2).info
        assert (info.max_batch_segments, info.max_batch_chars, info.max_concurrency) == (4, 600, 2)

    def test_fingerprint_follows_worker_fingerprint(self) -> None:
        def info_for(caps: Mapping[str, JsonValue] | None) -> EngineInfo:
            return engine_for(FakeChannel(caps)).info

        v1 = info_for({**CAPS, "fingerprint": "m1"})
        v1_again = info_for({**CAPS, "fingerprint": "m1", "pairs": [["en", "ja"]]})
        v2 = info_for({**CAPS, "fingerprint": "m2"})
        never_ready = info_for(None)
        empty = info_for({**CAPS, "fingerprint": ""})
        assert v1.fingerprint == v1_again.fingerprint
        assert len({v1.fingerprint, v2.fingerprint, never_ready.fingerprint}) == 3
        assert never_ready.fingerprint == empty.fingerprint
        # 指纹变化让 EngineInfo 不相等，调度器据此替换实例；revision 只随配置变化。
        assert v1 != v2
        assert v1.revision == v2.revision

    def test_missing_worker_fingerprint_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)
        caps = {key: value for key, value in CAPS.items() if key != "fingerprint"}
        info = engine_for(FakeChannel(caps)).info
        assert info.fingerprint == engine_for(FakeChannel(None)).info.fingerprint
        assert engine_warnings(caplog, "模型指纹按空串处理")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("name", "Argos 本地"),
            ("enabled", False),
            ("priority", 7),
            ("color", "#123456"),
            ("timeout_s", 20.0),
            ("processing_timeout_s", 3.0),
            ("max_concurrency", 2),
            ("max_batch_segments", 8),
            ("max_batch_chars", 500),
            ("secret", "unused"),
        ],
    )
    def test_settings_change_revision_but_not_fingerprint(self, field: str, value: object) -> None:
        base = engine_for(FakeChannel()).info
        changed = engine_for(FakeChannel(), **{field: value}).info
        assert changed.fingerprint == base.fingerprint
        assert changed.revision != base.revision


# ---------------------------------------------------------------- 与调度器联用


class Sink:
    """记录调度器输出的 RaceSink。"""

    def __init__(self) -> None:
        self.latest: dict[tuple[SessionId, SegmentId], SegmentView] = {}
        self.records: list[tuple[SessionId, SegmentId, AttemptView]] = []

    def segments_updated(self, session_id: SessionId, views: tuple[SegmentView, ...]) -> None:
        for view in views:
            self.latest[(session_id, view.segment_id)] = view

    def session_progress(self, session_id: SessionId, finished: int, total: int) -> None:
        pass

    def attempt_recorded(self, session_id: SessionId, segment_id: SegmentId, attempt: AttemptView) -> None:
        self.records.append((session_id, segment_id, attempt))

    def attempts(self, session_id: SessionId) -> dict[SegmentId, AttemptView]:
        return {segment_id: attempt for sid, segment_id, attempt in self.records if sid == session_id}


def scheduler_script(request: TranslateRequest) -> list[Item]:
    """按原文决定每段的回复："model missing" 返回模型缺失，"dropped" 不返回，其余正常翻译。"""
    rid = request.request_id
    items: list[Item] = [Started(rid)]
    for segment in request.segments:
        if segment.text == "model missing":
            items.append(SegmentResult(rid, segment.id, error="未安装模型", code="engine.model_missing"))
        elif segment.text != "dropped":
            items.append(SegmentResult(rid, segment.id, text=translated(segment.text)))
    items.append(RequestDone(rid))
    return items


class TestWithScheduler:
    async def test_results_enter_race(self) -> None:
        channel = FakeChannel(script=scheduler_script)
        engine = engine_for(channel, max_batch_segments=2)
        sink = Sink()
        scheduler = RaceScheduler([engine], sink, frame_interval=0)
        segments = (
            Segment(S1, "alpha"),
            Segment(S2, "model missing"),
            Segment(S3, "dropped"),
            Segment(S4, "delta"),
            Segment(S5, "epsilon"),
        )
        langs = {S1: (EN, ZH), S2: (EN, ZH), S3: (EN, ZH), S4: (JA, ZH), S5: (FR, ZH)}
        try:
            await scheduler.start_session(Entry.SCREENSHOT, SESSION, segments, langs)
            await scheduler.drain()
        finally:
            await scheduler.aclose()

        # 同一语言对的段按 2 段一批打包；fr 没有模型，不发请求。
        requests = [submission.request for submission in channel.submissions]
        assert [(r.source, r.target, [s.id for s in r.segments]) for r in requests] == [
            ("en", "zh-Hans", ["s1", "s2"]),
            ("en", "zh-Hans", ["s3"]),
            ("ja", "zh-Hans", ["s4"]),
        ]
        assert all(s.timeout_s == 5.0 and s.handle.cancel_calls == 0 for s in channel.submissions)

        for segment_id, text in ((S1, "alpha"), (S4, "delta")):
            view = sink.latest[(SESSION, segment_id)]
            assert view.shown_engine == engine.info.id
            assert view.display_text == translated(text)
            assert view.finished
        assert sink.latest[(SESSION, S5)].skip_reason is SkipReason.NO_ENGINE

        attempts = sink.attempts(SESSION)
        assert {segment_id: a.state for segment_id, a in attempts.items()} == {
            S1: AttemptState.DONE,
            S2: AttemptState.FAILED,
            S3: AttemptState.FAILED,
            S4: AttemptState.DONE,
        }
        assert attempts[S2].error_code is ErrorCode.MODEL_MISSING
        assert attempts[S3].error_code is ErrorCode.MISSING_SEGMENT

    async def test_cancelled_session_cancels_request(self) -> None:
        channel = FakeChannel()
        engine = engine_for(channel)
        sink = Sink()
        scheduler = RaceScheduler([engine], sink, frame_interval=0)
        old_id, new_id = SessionId("old"), SessionId("new")
        try:
            await scheduler.start_session(Entry.BILINGUAL, old_id, (Segment(S1, "old text"),), {S1: (EN, ZH)})
            old = await wait_reading(channel, 1)
            # 新会话取消同一入口的旧会话：旧请求被取消，新请求随后提交。
            await scheduler.start_session(Entry.BILINGUAL, new_id, (Segment(S1, "new text"),), {S1: (EN, ZH)})
            await channel.signal.wait_until(lambda: old.cancel_calls == 1 and old.closed)
            new = await wait_reading(channel, 2)
            new.push(*translate_all(new.request))
            await scheduler.drain()
        finally:
            await scheduler.aclose()

        assert new.cancel_calls == 0
        assert sink.latest[(new_id, S1)].display_text == translated("new text")
        assert old_id not in {session_id for session_id, _, _ in sink.records}

    async def test_higher_priority_result_cancels_running_request(self) -> None:
        """更高优先级的实例先给出译文：正在推理的 NMT 请求经 cancel 通路取消，尝试记为已取消（RACE-05）。"""

        class Top:
            def __init__(self) -> None:
                self.go = asyncio.Event()
                self._info = EngineInfo(
                    id=EngineId("top"), name="Top", kind=EngineKind.ONLINE_LLM, priority=1, billing=True
                )

            @property
            def info(self) -> EngineInfo:
                return self._info

            def supports(self, source: Lang, target: Lang) -> bool:
                return True

            async def translate(self, job: Job) -> AsyncIterator[SegmentOutcome]:
                await self.go.wait()
                for segment in job.segments:
                    yield SegmentOutcome(segment.id, text="top")

        channel = FakeChannel()
        top = Top()
        engine = engine_for(channel)
        sink = Sink()
        scheduler = RaceScheduler([top, engine], sink, frame_interval=0)
        try:
            await scheduler.start_session(Entry.SCREENSHOT, SESSION, (Segment(S1, "hello"),), {S1: (EN, ZH)})
            handle = await wait_reading(channel, 1)
            top.go.set()
            await channel.signal.wait_until(lambda: handle.cancel_calls == 1)
            await scheduler.drain()
        finally:
            await scheduler.aclose()

        view = sink.latest[(SESSION, S1)]
        assert (view.shown_engine, view.display_text) == (EngineId("top"), "top")
        (nmt,) = [a for _, _, a in sink.records if a.engine_id == engine.info.id]
        assert (nmt.state, nmt.started, nmt.error_code) == (AttemptState.CANCELLED, True, None)

    async def test_job_timeout_cancels_request(self) -> None:
        channel = FakeChannel()
        engine = engine_for(channel, timeout_s=0.05)
        sink = Sink()
        scheduler = RaceScheduler([engine], sink, frame_interval=0)
        try:
            await scheduler.start_session(Entry.SCREENSHOT, SESSION, (Segment(S1, "alpha"),), {S1: (EN, ZH)})
            await scheduler.drain()
        finally:
            await scheduler.aclose()

        assert channel.last.cancel_calls == 1
        attempt = sink.attempts(SESSION)[S1]
        assert attempt.state is AttemptState.FAILED
        assert attempt.error_code is ErrorCode.TIMEOUT


# ---------------------------------------------------------------- 隐私


async def test_logs_contain_no_source_or_translation(caplog: pytest.LogCaptureFixture) -> None:
    """在 DEBUG 级别跑过各条路径，原文和译文都不出现在日志里（PRV-02）。"""
    capture_all(caplog)
    sources = ("alpha secret", "beta secret", "gamma secret")
    job = make_job(*sources)

    def messy(request: TranslateRequest) -> list[Item]:
        rid = request.request_id
        return [
            Started(rid),
            SegmentResult(rid, "s1", text=translated(sources[0]), error="出错了"),  # 畸形结果
            SegmentResult(rid, "s9", text=translated(sources[1])),  # 不属于本任务
            SegmentResult(rid, "s2", text=translated(sources[1])),
            SegmentResult(rid, "s2", text=translated(sources[1])),  # 重复
            SegmentResult("other", "s3", text=translated(sources[2])),  # 别的请求
            SegmentResult(rid, "s3", error="出错了", code="engine.gpu_on_fire"),  # 未知错误码
            RequestDone(rid),
        ]

    def failing(request: TranslateRequest) -> list[Item]:
        rid = request.request_id
        return [SegmentResult(rid, "s1", text=translated(sources[0])), RequestFailed(rid, CANCELLED, "")]

    def broken(request: TranslateRequest) -> list[Item]:
        # 回复流抛出的异常信息里带着原文，引擎只能记异常类型。
        return [
            SegmentResult(request.request_id, "s1", text=translated(sources[0])),
            RuntimeError(sources[1]),
        ]

    def truncated(request: TranslateRequest) -> list[Item]:
        return [SegmentResult(request.request_id, "s1", text=translated(sources[0])), END]

    for script in (translate_all, messy, failing, broken, truncated):
        await collect(engine_for(FakeChannel(script=script)), job)

    rejecting = FakeChannel(script=translate_all)
    rejecting.fail_submit = True
    await collect(engine_for(rejecting), job)

    hanging = FakeChannel()
    task = asyncio.create_task(collect(engine_for(hanging), job))
    handle = await wait_reading(hanging)
    handle.cancel_error = RuntimeError(sources[2])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    sink = Sink()
    scheduler = RaceScheduler([engine_for(FakeChannel(script=scheduler_script))], sink, frame_interval=0)
    segments = tuple(Segment(SegmentId(f"s{i}"), text) for i, text in enumerate(sources, start=1))
    try:
        await scheduler.start_session(Entry.SCREENSHOT, SESSION, segments, {s.id: (EN, ZH) for s in segments})
        await scheduler.drain()
    finally:
        await scheduler.aclose()
    assert sink.latest[(SESSION, S1)].display_text == translated(sources[0])

    # 引擎的 DEBUG 与 WARNING 日志都确实被捕获了，下面的断言才有意义。
    levels = {record.levelno for record in caplog.records if record.name == LOGGER}
    assert {logging.DEBUG, logging.INFO, logging.WARNING} <= levels
    assert_private(caplog, *sources, *(translated(text) for text in sources))
