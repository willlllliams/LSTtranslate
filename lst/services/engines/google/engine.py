"""Google Cloud Translation v2（Basic）引擎（架构文档 2.7.2 节）。

一个任务合并成一次 POST 请求，JSON 请求体的 q 数组与任务中的段一一对应，返回的
data.translations 与输入同序。API key 放在请求头 X-Goog-Api-Key 里，不进入 URL（需求 CFG-04）。

Google 在超出每分钟配额时返回 403，错误原因为 rateLimitExceeded 或 userRateLimitExceeded，
与 key 无权限的 403 只能靠响应里的 reason 区分。send_with_retry 不把错误响应交给调用方，
所以这里用 http.py 的分类、退避与错误详情工具实现同样的重试循环，额外按 reason 判断。
"""

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Callable, Sequence

import httpx
from pydantic import BaseModel, Field, ValidationError

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.ports import SecretReader
from lst.contracts.translation import Engine, EngineInfo, EngineKind, Job, Segment, SegmentOutcome
from lst.services.engines.common import EngineSettings, build_info
from lst.services.engines.google.languages import google_code, supports_pair
from lst.services.engines.http import (
    EngineError,
    RetryPolicy,
    Sleep,
    backoff_delay,
    classify_status,
    classify_transport_error,
    error_detail,
)

logger = logging.getLogger(__name__)

TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
REQUEST_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
# 语言表或请求构造方式变化时加一，让旧译文不再命中缓存。
_FINGERPRINT_VERSION = 1

# 403 的错误原因：前两类是每分钟配额（限流），可以等一会儿重试；dailyLimitExceeded 是每日配额耗尽。
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"})
QUOTA_REASONS = frozenset({"dailyLimitExceeded", "quotaExceeded"})
# key 无效时 Google 返回 400（INVALID_ARGUMENT），原因为 API_KEY_INVALID，归为认证错误。
INVALID_KEY_REASONS = frozenset({"API_KEY_INVALID", "keyInvalid"})


class GoogleSettings(EngineSettings):
    """Google 实例的配置。

    max_batch_chars 默认 5000：Cloud Translation Basic 建议单次请求不超过 5000 个码位，
    请求大小的硬上限是 100K 字节，超出时返回 400。
    """

    timeout_s: float = Field(default=8.0, gt=0, le=600)
    max_batch_segments: int = Field(default=128, ge=1, le=128)
    max_batch_chars: int = Field(default=5000, ge=1, le=30000)


class _Translation(BaseModel):
    translated_text: str = Field(alias="translatedText")


class _Data(BaseModel):
    translations: list[_Translation]


class _TranslateResponse(BaseModel):
    data: _Data


class _Reason(BaseModel):
    reason: str | None = None


class _ErrorInfo(BaseModel):
    errors: tuple[_Reason, ...] = ()  # 旧式错误结构 error.errors[].reason
    details: tuple[_Reason, ...] = ()  # google.rpc.ErrorInfo 结构 error.details[].reason


class _ErrorBody(BaseModel):
    error: _ErrorInfo


def error_reasons(body: bytes) -> frozenset[str]:
    """从错误响应里取出全部 reason 字段；无法解析时返回空集。"""
    try:
        parsed = _ErrorBody.model_validate_json(body)
    except ValidationError:
        return frozenset()
    items = (*parsed.error.errors, *parsed.error.details)
    return frozenset(item.reason for item in items if item.reason)


def classify_error(status: int, reasons: frozenset[str], policy: RetryPolicy) -> tuple[ErrorCode, bool]:
    """返回错误码，以及这个错误是否值得重试。"""
    if status == 403 and reasons & RATE_LIMIT_REASONS:
        return ErrorCode.RATE_LIMITED, True
    if status == 403 and reasons & QUOTA_REASONS:
        return ErrorCode.QUOTA, False
    if status == 400 and reasons & INVALID_KEY_REASONS:
        return ErrorCode.AUTH, False
    return classify_status(status), status in policy.retry_statuses


async def send_google(
    client: httpx.AsyncClient,
    build: Callable[[], httpx.Request],
    *,
    policy: RetryPolicy,
    sleep: Sleep,
    rand: Callable[[], float],
) -> httpx.Response:
    """发送请求并按策略重试，返回状态码小于 400 的响应；失败时抛出 EngineError。

    与 send_with_retry 的区别只在错误分类：先读出错误响应的 reason，再由 classify_error 判断
    错误码和能否重试。技术细节仍由 error_detail 生成，不含 URL 和响应正文。
    """
    for retry_index in range(policy.max_retries + 1):
        last_try = retry_index == policy.max_retries
        try:
            response = await client.send(build())
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

        detail = await error_detail(response)
        reasons = error_reasons(response.content)
        await response.aclose()
        if reasons:
            detail += f"（reason: {', '.join(sorted(reasons))}）"
        code, retryable = classify_error(response.status_code, reasons, policy)
        if last_try or not retryable:
            raise EngineError(code, detail, status=response.status_code)
        delay = backoff_delay(retry_index, policy, response.headers.get("retry-after"), rand)
        if delay is None:
            raise EngineError(code, f"{detail}（Retry-After 过长，放弃重试）", status=response.status_code)
        await sleep(delay)

    raise AssertionError("unreachable")  # 循环的最后一次一定返回或抛出


def _failures(segments: Sequence[Segment], code: ErrorCode, detail: str) -> list[SegmentOutcome]:
    return [SegmentOutcome(segment.id, error=detail, code=code) for segment in segments]


def _parse(response: httpx.Response, expected: int) -> list[str]:
    """取出与输入同序的译文；结构不对或条数不符时抛出 BAD_RESPONSE。错误信息不含响应正文。"""
    try:
        parsed = _TranslateResponse.model_validate_json(response.content)
    except ValidationError as exc:
        where = ", ".join(".".join(str(part) for part in error["loc"]) or "<root>" for error in exc.errors())
        raise EngineError(ErrorCode.BAD_RESPONSE, f"响应结构不符合预期（{where}）") from None
    translations = parsed.data.translations
    if len(translations) != expected:
        raise EngineError(ErrorCode.BAD_RESPONSE, f"返回 {len(translations)} 条译文，请求了 {expected} 条")
    return [item.translated_text for item in translations]


class GoogleEngine:
    """Google 引擎实例。key 在创建时读取一次，key 变化时由配置服务重建实例。"""

    def __init__(
        self,
        settings: GoogleSettings,
        key: str | None,
        client: httpx.AsyncClient,
        *,
        sleep: Sleep,
        rand: Callable[[], float],
    ) -> None:
        self._key = key
        self._client = client
        self._sleep = sleep
        self._rand = rand
        self._policy = RetryPolicy(max_retries=settings.max_retries)
        self._info = build_info(
            settings,
            kind=EngineKind.ONLINE_MT,
            billing=True,
            max_batch_segments=settings.max_batch_segments,
            max_batch_chars=settings.max_batch_chars,
            fingerprint_parts=("google", "v2", _FINGERPRINT_VERSION, "text"),
            secret_value=key,
        )

    @property
    def info(self) -> EngineInfo:
        return self._info

    def supports(self, source: Lang, target: Lang) -> bool:
        return supports_pair(source, target)

    async def translate(self, job: Job) -> AsyncIterator[SegmentOutcome]:
        # 空白段不发给接口，原样作为译文返回。
        pending = [segment for segment in job.segments if segment.text.strip()]
        for segment in job.segments:
            if not segment.text.strip():
                yield SegmentOutcome(segment.id, text=segment.text)
        if not pending:
            return
        for outcome in await self._translate_batch(job, pending):
            yield outcome

    async def _translate_batch(self, job: Job, segments: Sequence[Segment]) -> list[SegmentOutcome]:
        if self._key is None:
            return _failures(segments, ErrorCode.AUTH, "未配置 Google API key")
        target = google_code(job.target_lang)
        if target is None:
            return _failures(
                segments, ErrorCode.UNSUPPORTED_LANGUAGE, f"Google 不支持目标语言 {job.target_lang}"
            )

        # 源语言不在语言表里时不传 source，由 Google 自动检测。
        payload: dict[str, object] = {"q": [segment.text for segment in segments], "target": target}
        source = google_code(job.source_lang)
        if source is not None:
            payload["source"] = source
        payload["format"] = "text"
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"X-Goog-Api-Key": self._key, "Content-Type": "application/json"}

        def build() -> httpx.Request:
            return self._client.build_request(
                "POST", TRANSLATE_URL, content=body, headers=headers, timeout=REQUEST_TIMEOUT
            )

        logger.debug("Google 实例 %s 发送 %d 段，请求体 %d 字节", self._info.id, len(segments), len(body))
        try:
            response = await send_google(
                self._client, build, policy=self._policy, sleep=self._sleep, rand=self._rand
            )
            texts = _parse(response, len(segments))
        except EngineError as exc:
            logger.info("Google 实例 %s 请求失败：%s", self._info.id, exc.detail)
            return _failures(segments, exc.code, exc.detail)
        return [SegmentOutcome(segment.id, text=text) for segment, text in zip(segments, texts, strict=True)]


def create_google_engine(
    settings: GoogleSettings,
    secrets: SecretReader,
    client: httpx.AsyncClient,
    *,
    sleep: Sleep = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> Engine:
    """创建 Google 引擎实例。sleep 与 rand 只在测试中替换，用来跳过重试前的等待。"""
    key = secrets.get_secret(settings.secret) if settings.secret else None
    key = key.strip() if key else None
    return GoogleEngine(settings, key or None, client, sleep=sleep, rand=rand)
