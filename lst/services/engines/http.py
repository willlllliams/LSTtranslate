"""网络引擎共用的 HTTP 工具：错误分类、重试、SSE 解析、限速（架构文档 2.7 节）。

隐私约束（需求 PRV-02、CFG-04）：错误详情里不写 URL 和响应正文，只取 JSON 里的错误消息字段，
因为有些接口会在错误响应里回显原文。
"""

import asyncio
import email.utils
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final, cast

import httpx

from lst.contracts.errors import ErrorCode

DETAIL_LIMIT: Final = 200
"""错误消息字段写进错误详情时的最大长度，防止异常的长响应进入日志。各引擎共用。"""


class EngineError(Exception):
    """引擎请求失败。code 决定界面显示的原因，detail 是写日志用的技术细节。"""

    def __init__(self, code: ErrorCode, detail: str, *, status: int | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


def classify_status(status: int) -> ErrorCode:
    """把 HTTP 错误状态码映射为错误码。"""
    if status in (401, 403):
        return ErrorCode.AUTH
    if status in (402, 456):  # 456 是 DeepL 的“配额耗尽”
        return ErrorCode.QUOTA
    if status == 429:
        return ErrorCode.RATE_LIMITED
    if status >= 500:
        return ErrorCode.SERVER
    return ErrorCode.BAD_REQUEST


def classify_transport_error(exc: httpx.TransportError) -> ErrorCode:
    """把 httpx 的传输层异常映射为错误码。"""
    if isinstance(exc, httpx.TimeoutException):
        return ErrorCode.TIMEOUT
    return ErrorCode.NETWORK


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """重试策略，默认值对应架构文档 2.7.3 节：网络类引擎最多重试 1 次。

    Retry-After 超过 max_delay_s 时直接放弃重试：在竞速中长时间等待只会推迟竞速结束。
    """

    max_retries: int = 1
    base_delay_s: float = 0.5
    max_delay_s: float = 4.0
    retry_statuses: frozenset[int] = field(default_factory=lambda: frozenset({429, 500, 502, 503, 504}))
    retry_transport_errors: bool = True


def backoff_delay(
    retry_index: int, policy: RetryPolicy, retry_after: str | None, rand: Callable[[], float]
) -> float | None:
    """计算第 retry_index 次重试（从 0 开始）前的等待秒数；返回 None 表示不应重试。

    有 Retry-After 时以它为准；否则按 base_delay_s × 2^retry_index × [0.5, 1.5) 的随机抖动计算，
    结果不超过 max_delay_s。
    """
    if retry_after is not None:
        seconds = parse_retry_after(retry_after)
        if seconds is not None:
            return seconds if seconds <= policy.max_delay_s else None
    delay = policy.base_delay_s * (2**retry_index) * (0.5 + rand())
    return min(delay, policy.max_delay_s)


def parse_retry_after(value: str, *, now: float | None = None) -> float | None:
    """解析 Retry-After 头：整数秒，或 HTTP 日期。无法解析时返回 None。"""
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    current = time.time() if now is None else now
    return max(0.0, when.timestamp() - current)


async def error_detail(response: httpx.Response) -> str:
    """从错误响应中提取可以写日志的细节：状态码，加上 JSON 中的错误消息字段（如果有）。"""
    detail = f"HTTP {response.status_code}"
    try:
        body = await response.aread()
        payload = json.loads(body)
    except (ValueError, httpx.HTTPError):
        return detail
    message = _find_message(payload)
    if message:
        detail += f": {message[:DETAIL_LIMIT]}"
    return detail


def _find_message(payload: object) -> str | None:
    """在常见的错误响应结构里找消息字段：error.message、message、error_msg、error（字符串）。"""
    if not isinstance(payload, dict):
        return None
    fields = cast(dict[str, object], payload)
    error = fields.get("error")
    if isinstance(error, dict):
        message = cast(dict[str, object], error).get("message")
        if isinstance(message, str):
            return message
    if isinstance(error, str):
        return error
    for key in ("message", "error_msg"):
        value = fields.get(key)
        if isinstance(value, str):
            return value
    return None


Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ErrorVerdict:
    """一个错误响应的分类结果：错误码、是否值得重试、写日志用的技术细节。"""

    code: ErrorCode
    retryable: bool
    detail: str


ClassifyError = Callable[[int, bytes, ErrorVerdict], ErrorVerdict]
"""错误分类钩子：(状态码, 响应体, 按状态码得到的默认分类) → 最终分类。

有些接口要看响应体才能分类，例如 Google 的 403 既可能是限流也可能是 key 无权限。钩子只能把
响应体里的结构化字段（错误原因等）写进 detail，不能写入正文（隐私约束见模块说明）。
"""


async def send_with_retry(
    client: httpx.AsyncClient,
    build: Callable[[], httpx.Request],
    *,
    policy: RetryPolicy | None = None,
    stream: bool = False,
    sleep: Sleep = asyncio.sleep,
    rand: Callable[[], float] = random.random,
    classify: ClassifyError | None = None,
    send_timeout: float | None = None,
) -> httpx.Response:
    """发送请求，按策略重试，返回状态码小于 400 的响应；失败时抛出 EngineError。

    - build 每次重试都会被调用，生成一个新的请求对象；它在每次发送前一刻调用，调用方可以据此记下发送时刻。
    - stream=True 时返回未读取的流式响应，调用方负责关闭（用 `async with` 或 `aclose()`）。
      错误响应在这里读取错误详情后关闭，读取期间被取消或出错也会关闭。
    - classify 不为 None 时，错误响应的分类交给它调整，见 ClassifyError。
    - send_timeout 是单次发送（发出请求到收到响应头）的时限，不含重试前的退避等待。
      超出时抛出 TimeoutError，不重试，由调用方决定如何报告（例如 LLM 引擎报“首 token 超时”）。

    总请求次数不超过 policy.max_retries + 1。
    """
    policy = policy or RetryPolicy()
    for retry_index in range(policy.max_retries + 1):
        last_try = retry_index == policy.max_retries
        try:
            async with asyncio.timeout(send_timeout):
                response = await client.send(build(), stream=stream)
        except httpx.TransportError as exc:
            code = classify_transport_error(exc)
            if last_try or not policy.retry_transport_errors:
                raise EngineError(code, f"{type(exc).__name__}: {exc}") from exc
            delay = backoff_delay(retry_index, policy, None, rand)
            assert delay is not None
            await sleep(delay)
            continue

        if response.status_code < 400:
            return response

        try:
            detail = await error_detail(response)
        finally:
            await response.aclose()
        status = response.status_code
        verdict = ErrorVerdict(classify_status(status), status in policy.retry_statuses, detail)
        if classify is not None:
            verdict = classify(status, _read_body(response), verdict)
        if last_try or not verdict.retryable:
            raise EngineError(verdict.code, verdict.detail, status=status)
        delay = backoff_delay(retry_index, policy, response.headers.get("retry-after"), rand)
        if delay is None:
            raise EngineError(verdict.code, f"{verdict.detail}（Retry-After 过长，放弃重试）", status=status)
        await sleep(delay)

    raise AssertionError("unreachable")  # 循环的最后一次一定返回或抛出


def _read_body(response: httpx.Response) -> bytes:
    """error_detail 已经读过的响应体；读取失败时为空。"""
    try:
        return response.content
    except httpx.ResponseNotRead:
        return b""


async def aiter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """按 SSE（Server-Sent Events）格式逐个产出事件的 data 内容。

    - 以冒号开头的注释行被忽略，例如 DeepSeek 排队时发送的 `: keep-alive`；
    - 一个事件的多行 data 用换行连接；
    - event、id、retry 字段被忽略；
    - 收到 `data: [DONE]` 时结束（OpenAI 兼容接口的约定）。
    """
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                if data == "[DONE]":
                    return
                yield data
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            data_lines.append(value)
    if data_lines:
        data = "\n".join(data_lines)
        if data != "[DONE]":
            yield data


class RateLimiter:
    """令牌桶限速器：平均每秒不超过 rate 次，允许 burst 次突发（百度翻译按 QPS 限速）。

    acquire() 在主线程的事件循环上排队等待，不阻塞线程。
    """

    def __init__(
        self,
        rate: float,
        *,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate 必须为正数，burst 至少为 1")
        self._rate = rate
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self._rate)
