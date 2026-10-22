"""谷歌免费翻译引擎：translate.googleapis.com 的 client=gtx 接口，不需要 API key。

做法照参考项目 thunderbird-translate 的 src/background/translators/google.ts：

- 批量请求：POST {endpoint}/translate_a/t?client=gtx&sl=<源>&tl=<目标>&format=text，
  请求体是 application/x-www-form-urlencoded，每段一个 q 参数。一个任务只发一次请求，
  返回的数组与 q 一一对应，不需要分隔符。
- 批量响应有三种形状：显式给出 sl 时是 ["译文1", "译文2"]；sl=auto 时是 [["译文1", "en"], ...]；
  只有一个 q 时有时直接是字符串 "译文"。三种都按同一规则取出译文。
- 批量响应是合法 JSON、但形状或条数对不上时，改为逐段请求 translate_a/single
  （dt=t、dj=0），它的响应是 [[["句1", "原句1", ...], ["句2", ...]], null, "en", ...]，
  把第一层各分句的译文拼起来就是整段译文。逐段请求各自失败，一段失败不影响其他段。
  响应不是 JSON（例如被拦截时返回的 HTML 页面）时整批记为 BAD_RESPONSE，不再逐段请求，
  免得对同一个拒绝服务的地址再发几十次请求。

这个接口没有公开文档，随时可能变动或限流；在中国大陆无法访问。文本会发送给谷歌。
请求的查询串里只有语言码，原文只出现在请求体里（需求 PRV-02）。

单次请求的上限照参考项目的设置文档：最多 100 段、5000 个字符。
"""

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Final, cast
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import Field, field_validator

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.translation import Engine, EngineInfo, EngineKind, Job, Segment, SegmentOutcome
from lst.services.engines.common import EngineSettings, build_info, check_count, failures, split_blank
from lst.services.engines.google_free.languages import google_code, supports_pair
from lst.services.engines.http import (
    EngineError,
    ErrorVerdict,
    RetryPolicy,
    Sleep,
    send_with_retry,
)

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT: Final = "https://translate.googleapis.com"
BATCH_PATH: Final = "/translate_a/t"
SINGLE_PATH: Final = "/translate_a/single"
FORM_CONTENT_TYPE: Final = "application/x-www-form-urlencoded;charset=UTF-8"
REQUEST_TIMEOUT: Final = httpx.Timeout(8.0, connect=3.0)
MAX_SEGMENTS: Final = 100
MAX_CHARS: Final = 5000
FALLBACK_CONCURRENCY: Final = 4
"""逐段请求时同时进行的请求数，与参考项目每个服务的请求队列默认并发数相同。"""
# 语言表或请求构造方式变化时加一，让旧译文不再命中缓存。
_FINGERPRINT_VERSION = 1


class GoogleFreeSettings(EngineSettings):
    """谷歌免费翻译实例的配置。不需要 API key，secret 字段不使用。

    endpoint 是接口的基地址，默认 https://translate.googleapis.com；请求地址是它加上
    /translate_a/t 或 /translate_a/single。可以改成转发同一接口的镜像地址。
    """

    timeout_s: float = Field(default=8.0, gt=0, le=600)
    endpoint: str = DEFAULT_ENDPOINT
    max_batch_segments: int = Field(default=MAX_SEGMENTS, ge=1, le=MAX_SEGMENTS)
    max_batch_chars: int = Field(default=MAX_CHARS, ge=1, le=MAX_CHARS)

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        # 地址会出现在 httpx 的日志里，所以不允许带查询参数和用户名密码（架构文档第 10 章）。
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("endpoint 必须是 http 或 https 地址")
        if parts.query or parts.fragment:
            raise ValueError("endpoint 不能带查询参数或片段")
        if parts.username is not None or parts.password is not None:
            raise ValueError("endpoint 不能包含用户名或密码")
        return value.rstrip("/")


class BatchShapeError(EngineError):
    """批量响应是合法 JSON，但形状或条数与请求对不上；调用方据此改为逐段请求。"""

    def __init__(self, detail: str) -> None:
        super().__init__(ErrorCode.BAD_RESPONSE, detail)


def encode_form(texts: Sequence[str]) -> bytes:
    """每段一个 q 参数的表单请求体，按 UTF-8 百分号编码。"""
    return urlencode([("q", text) for text in texts]).encode("ascii")


def load_json(content: bytes) -> object:
    """解析 JSON 响应体；不是 JSON 时抛出 BAD_RESPONSE。错误信息不含响应正文。"""
    try:
        return cast(object, json.loads(content))
    except ValueError:
        raise EngineError(ErrorCode.BAD_RESPONSE, "响应不是 JSON") from None


def parse_batch_response(payload: object, expected: int) -> list[str]:
    """从 translate_a/t 的响应里按顺序取出译文。形状或条数不符时抛出 BatchShapeError。"""
    items: object = [payload] if isinstance(payload, str) else payload
    if not isinstance(items, list):
        raise BatchShapeError("批量响应不是数组")
    entries = cast(list[object], items)
    try:
        check_count(len(entries), expected)
    except EngineError as exc:
        raise BatchShapeError(exc.detail) from None
    texts: list[str] = []
    for index, entry in enumerate(entries, start=1):
        text = entry
        if isinstance(entry, list):
            first = cast(list[object], entry)
            text = first[0] if first else None
        if not isinstance(text, str):
            raise BatchShapeError(f"第 {index} 条译文不是字符串")
        texts.append(text)
    return texts


def parse_single_response(payload: object) -> str:
    """从 translate_a/single 的响应里取出整段译文：第一层各分句的第一个元素依次拼接。

    结构不符，或者拼出来是空串（原文不是空白段，空译文没有意义）时抛出 BAD_RESPONSE。
    """
    if not isinstance(payload, list) or not payload or not isinstance(cast(list[object], payload)[0], list):
        raise EngineError(ErrorCode.BAD_RESPONSE, "逐段响应的结构不符合预期")
    chunks = cast(list[object], cast(list[object], payload)[0])
    parts: list[str] = []
    for chunk in chunks:
        if isinstance(chunk, list) and chunk:
            first = cast(list[object], chunk)[0]
            if isinstance(first, str):
                parts.append(first)
    text = "".join(parts)
    if not text:
        raise EngineError(ErrorCode.BAD_RESPONSE, "逐段响应里没有译文")
    return text


def keyless_verdict(status: int, body: bytes, default: ErrorVerdict) -> ErrorVerdict:
    """send_with_retry 的错误分类钩子：这个接口没有 key，401、403 表示请求被拒绝，不是 key 无效。

    默认分类会把 401、403 记为 AUTH，界面上的建议是“检查 API key”，对免费接口没有意义，
    所以改记为 BAD_REQUEST，不重试。其余状态码（429 限流、5xx 服务端错误等）保持默认分类。
    """
    if default.code is ErrorCode.AUTH:
        return ErrorVerdict(ErrorCode.BAD_REQUEST, False, default.detail)
    return default


class GoogleFreeEngine:
    """谷歌免费翻译引擎实例。"""

    def __init__(
        self,
        settings: GoogleFreeSettings,
        client: httpx.AsyncClient,
        *,
        sleep: Sleep,
        rand: Callable[[], float],
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._rand = rand
        self._endpoint = settings.endpoint
        self._policy = RetryPolicy(max_retries=settings.max_retries)
        self._info = build_info(
            settings,
            kind=EngineKind.ONLINE_MT,
            billing=False,
            max_batch_segments=settings.max_batch_segments,
            max_batch_chars=settings.max_batch_chars,
            fingerprint_parts=("google_free", "gtx", _FINGERPRINT_VERSION, self._endpoint),
            secret_value=None,
        )

    @property
    def info(self) -> EngineInfo:
        return self._info

    def supports(self, source: Lang, target: Lang) -> bool:
        return supports_pair(source, target)

    async def translate(self, job: Job) -> AsyncIterator[SegmentOutcome]:
        blank, pending = split_blank(job.segments)
        for outcome in blank:
            yield outcome
        if not pending:
            return
        for outcome in await self._translate_batch(job, pending):
            yield outcome

    async def _translate_batch(self, job: Job, segments: Sequence[Segment]) -> list[SegmentOutcome]:
        target = google_code(job.target_lang)
        if target is None:
            return failures(
                segments, ErrorCode.UNSUPPORTED_LANGUAGE, f"谷歌免费接口不支持目标语言 {job.target_lang}"
            )
        # 源语言不在语言表里时传 auto，由谷歌自动检测（参考项目的做法）。
        source = google_code(job.source_lang) or "auto"
        params = {"client": "gtx", "sl": source, "tl": target, "format": "text"}
        body = encode_form([segment.text for segment in segments])
        logger.debug("谷歌免费实例 %s 发送 %d 段，请求体 %d 字节", self._info.id, len(segments), len(body))
        try:
            payload = load_json(await self._post(BATCH_PATH, params, body))
            texts = parse_batch_response(payload, len(segments))
        except BatchShapeError as exc:
            logger.info("谷歌免费实例 %s 的批量响应对不上（%s），改为逐段请求", self._info.id, exc.detail)
            return await self._translate_each(segments, source, target)
        except EngineError as exc:
            logger.info("谷歌免费实例 %s 请求失败：%s", self._info.id, exc.detail)
            return failures(segments, exc.code, exc.detail)
        return [SegmentOutcome(segment.id, text=text) for segment, text in zip(segments, texts, strict=True)]

    async def _translate_each(
        self, segments: Sequence[Segment], source: str, target: str
    ) -> list[SegmentOutcome]:
        """逐段请求 translate_a/single，最多 FALLBACK_CONCURRENCY 个同时进行，结果保持原来的顺序。"""
        params = {"client": "gtx", "sl": source, "tl": target, "dt": "t", "dj": "0"}
        gate = asyncio.Semaphore(FALLBACK_CONCURRENCY)

        async def one(segment: Segment) -> SegmentOutcome:
            async with gate:
                try:
                    payload = load_json(await self._post(SINGLE_PATH, params, encode_form([segment.text])))
                    return SegmentOutcome(segment.id, text=parse_single_response(payload))
                except EngineError as exc:
                    logger.info("谷歌免费实例 %s 逐段请求失败：%s", self._info.id, exc.detail)
                    return SegmentOutcome(segment.id, error=exc.detail, code=exc.code)

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(segment)) for segment in segments]
        return [task.result() for task in tasks]

    async def _post(self, path: str, params: dict[str, str], body: bytes) -> bytes:
        url = self._endpoint + path
        headers = {"Content-Type": FORM_CONTENT_TYPE}

        def build() -> httpx.Request:
            return self._client.build_request(
                "POST", url, params=params, content=body, headers=headers, timeout=REQUEST_TIMEOUT
            )

        response = await send_with_retry(
            self._client,
            build,
            policy=self._policy,
            sleep=self._sleep,
            rand=self._rand,
            classify=keyless_verdict,
        )
        return response.content


def create_google_free_engine(
    settings: GoogleFreeSettings,
    client: httpx.AsyncClient,
    *,
    sleep: Sleep = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> Engine:
    """创建谷歌免费翻译引擎实例。sleep 与 rand 只在测试中替换，用来跳过重试前的等待。"""
    return GoogleFreeEngine(settings, client, sleep=sleep, rand=rand)
