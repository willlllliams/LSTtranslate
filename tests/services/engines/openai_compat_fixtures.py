"""OpenAI 兼容引擎测试用的替身：可编排的 MockTransport 服务端、流式响应体、密钥读取器。"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

import httpx

from lst.contracts.ids import EngineId, Lang, SegmentId, SessionId
from lst.contracts.translation import Job, JobImage, Segment, SegmentOutcome
from lst.services.engines.openai_compat import OpenAICompatSettings
from lst.services.engines.openai_compat.engine import OpenAICompatEngine
from lst.services.engines.openai_compat.prompt import CONTEXT_SEPARATOR

KEY = "sk-test-SECRET-0123456789"
SESSION = SessionId("sess")
EN = Lang("en")
ZH = Lang("zh-Hans")
JA = Lang("ja")


# ---------------------------------------------------------------- 流式响应


def _event(payload: object) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def content_event(content: str) -> bytes:
    """一个只带 delta.content 的 OpenAI 流式 chunk。"""
    return _event({"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})


# 每个流开头的两个事件：DeepSeek 排队时发送的注释行，以及只带 role 的首个 chunk。
_HEAD = (
    b": keep-alive\n\n",
    _event({"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}),
)


def _tail(finish_reason: str) -> tuple[bytes, ...]:
    empty: dict[str, str] = {}
    return (
        _event({"choices": [{"index": 0, "delta": empty, "finish_reason": finish_reason}]}),
        b"data: [DONE]\n\n",
    )


def sse_response(*pieces: str, finish_reason: str = "stop") -> httpx.Response:
    """把一组 content 片段包装成完整的 OpenAI 流式 SSE 响应：每个片段一个 chunk，最后是 [DONE]。"""
    body = b"".join((*_HEAD, *(content_event(piece) for piece in pieces), *_tail(finish_reason)))
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def reply_json(*texts: str) -> str:
    """模型的标准输出：{"r":[{"i":1,"t":texts[0]},...]}。"""
    return json.dumps({"r": [{"i": i, "t": t} for i, t in enumerate(texts, start=1)]}, ensure_ascii=False)


def reply_response(*texts: str) -> httpx.Response:
    """整个输出放在一个 chunk 里的正常响应。"""
    return sse_response(reply_json(*texts))


class ContentStream(httpx.AsyncByteStream):
    """按片段发送 OpenAI 流式 chunk 的响应体，用于控制数据到达的时机。

    - gate_before 中的片段在发送前等待闸门，release(n) 放行；开始等待时 blocked(n) 被置位。
      引擎只有处理完前面所有数据、并且没有可产出的结果时才会继续读取，所以 blocked(n) 被置位
      说明片段 n 之前的数据都已处理完。
    - error 不为 None 时，发完片段后抛出该异常（模拟断流）；stall 为真时发完片段后一直挂起。
      两者都没有时，最后发送 finish_reason 和 [DONE]。
    """

    def __init__(
        self,
        pieces: Sequence[str],
        *,
        gate_before: Iterable[int] = (),
        error: Exception | None = None,
        stall: bool = False,
    ) -> None:
        self._pieces = list(pieces)
        self._gates = {index: asyncio.Event() for index in gate_before}
        self._blocked: dict[int, asyncio.Event] = {}
        self._error = error
        self._stall = stall
        self.sent = 0
        self.closed = False

    def blocked(self, index: int) -> asyncio.Event:
        """在片段 index 前开始等待时置位；index 等于片段数时表示在全部片段之后挂起。"""
        return self._blocked.setdefault(index, asyncio.Event())

    def release(self, index: int) -> None:
        self._gates[index].set()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in _HEAD:
            yield chunk
        for index, piece in enumerate(self._pieces):
            gate = self._gates.get(index)
            if gate is not None:
                self.blocked(index).set()
                await gate.wait()
            self.sent += 1
            yield content_event(piece)
        if self._error is not None:
            raise self._error
        if self._stall:
            self.blocked(len(self._pieces)).set()
            await asyncio.Event().wait()
        for chunk in _tail("stop"):
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def stream_response(stream: httpx.AsyncByteStream) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)


async def wait_until_blocked(stream: ContentStream, index: int) -> None:
    """等到流在片段 index 前挂起；引擎提前产出结果时它不会再读，这里就会超时。"""
    await asyncio.wait_for(stream.blocked(index).wait(), timeout=2.0)
    await asyncio.sleep(0)


# ---------------------------------------------------------------- 服务端与依赖


Reply = httpx.Response | Callable[[httpx.Request], Awaitable[httpx.Response]]


class FakeServer:
    """MockTransport 的处理函数：按顺序返回排好的应答，记录收到的请求。没有应答可用时测试失败。"""

    def __init__(self) -> None:
        self._replies: list[Reply] = []
        self.requests: list[httpx.Request] = []

    def queue(self, *replies: Reply) -> None:
        self._replies.extend(replies)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._replies:
            raise AssertionError("收到了意料之外的请求")
        reply = self._replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        return await reply(request)

    def body(self, index: int) -> dict[str, Any]:
        return json.loads(self.requests[index].content)

    def user_message(self, index: int) -> str:
        """user 消息的文本；附带截图时取 content 数组里的文本部分。"""
        messages = self.body(index)["messages"]
        assert [m["role"] for m in messages] == ["system", "user"]
        content: str | list[dict[str, Any]] = messages[1]["content"]
        if isinstance(content, list):
            return next(str(part["text"]) for part in content if part["type"] == "text")
        return content

    def context_part(self, index: int) -> str:
        """user 消息中分隔标记之前的部分：会话全文，加上固定的开头说明。"""
        head, separator, _ = self.user_message(index).rpartition(CONTEXT_SEPARATOR)
        assert separator, "user 消息里没有分隔标记"
        return head

    def batch_part(self, index: int) -> str:
        """user 消息中分隔标记之后的部分：语言对、输出格式说明和本批段落。"""
        return self.user_message(index).rpartition(CONTEXT_SEPARATOR)[2]

    def segments(self, index: int) -> list[dict[str, Any]]:
        """第 index 个请求里本批段落的 JSON（分隔标记之后 {"segments":...} 的部分）。"""
        tail = self.batch_part(index)
        return json.loads(tail[tail.index('{"segments"') :])["segments"]


async def never_respond(request: httpx.Request) -> httpx.Response:
    """一直不返回响应头的服务端。"""
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


class Secrets:
    """SecretReader 的替身。"""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get_secret(self, name: str) -> str | None:
        return self._values.get(name)


class Sleeps:
    """记录重试等待时长、不真正等待的 sleep。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


_DEFAULTS: dict[str, Any] = {
    "deepseek": {"base_url": "https://api.deepseek.test", "model": "deepseek-flash", "secret": "deepseek"},
    "openai": {"base_url": "https://api.openai.test/v1", "model": "gpt-test", "secret": "openai"},
    "generic": {"base_url": "https://llm.example.test/v1", "model": "some-model", "secret": "generic"},
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "qwen3:8b"},
    "llamacpp": {"base_url": "http://127.0.0.1:8080/v1", "model": "local"},
}


def make_settings(provider: str = "deepseek", **overrides: Any) -> OpenAICompatSettings:
    data: dict[str, Any] = {
        "id": f"{provider}-1",
        "name": provider,
        "priority": 1,
        "timeout_s": 60,
        "provider": provider,
        **_DEFAULTS[provider],
        **overrides,
    }
    return OpenAICompatSettings.model_validate(data)


def make_engine(
    client: httpx.AsyncClient,
    provider: str = "deepseek",
    *,
    sleeps: Sleeps | None = None,
    secrets: Mapping[str, str] | None = None,
    idle_timeout_s: float = 10.0,
    **overrides: Any,
) -> OpenAICompatEngine:
    """创建引擎；默认在 keyring 替身里放好 deepseek、openai、generic 三个条目。"""
    values = {"deepseek": KEY, "openai": KEY, "generic": KEY} if secrets is None else secrets
    return OpenAICompatEngine(
        make_settings(provider, **overrides),
        Secrets(values),
        client,
        sleep=sleeps or Sleeps(),
        idle_timeout_s=idle_timeout_s,
    )


# ---------------------------------------------------------------- 任务与结果


def make_job(
    *texts: str,
    context: str = "",
    source: Lang = EN,
    target: Lang = ZH,
    prefix: str = "s",
    image: JobImage | None = None,
) -> Job:
    """段号为 prefix1、prefix2……；image 是随任务附带的截图。"""
    segments = tuple(Segment(SegmentId(f"{prefix}{i}"), text) for i, text in enumerate(texts, start=1))
    return Job(SESSION, EngineId("e"), segments, source, target, context, image)


async def collect(stream: AsyncIterator[SegmentOutcome]) -> list[SegmentOutcome]:
    """取出全部结果，并检查每个段只产出一次。"""
    outcomes = [outcome async for outcome in stream]
    ids = [outcome.segment_id for outcome in outcomes]
    assert len(ids) == len(set(ids)), f"段重复产出：{ids}"
    return outcomes


def next_task(outcomes: AsyncIterator[SegmentOutcome]) -> asyncio.Task[SegmentOutcome]:
    """在单独的任务里取下一个结果，便于在等待期间检查状态或取消。"""

    async def step() -> SegmentOutcome:
        return await anext(outcomes)

    return asyncio.create_task(step())


def texts_of(outcomes: Sequence[SegmentOutcome]) -> dict[str, str | None]:
    return {outcome.segment_id: outcome.text for outcome in outcomes}


def codes_of(outcomes: Sequence[SegmentOutcome]) -> dict[str, object]:
    return {outcome.segment_id: outcome.code for outcome in outcomes}
