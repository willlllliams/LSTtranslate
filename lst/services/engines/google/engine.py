"""Google Cloud Translation v2（Basic）引擎（架构文档 2.7.2 节）。

一个任务合并成一次 POST 请求，JSON 请求体的 q 数组与任务中的段一一对应，返回的
data.translations 与输入同序。API key 放在请求头 X-Goog-Api-Key 里，不进入 URL（需求 CFG-04）。

Google 在超出每分钟配额时返回 403，错误原因为 rateLimitExceeded 或 userRateLimitExceeded，
与 key 无权限的 403 只能靠响应里的 reason 区分，所以请求经 send_with_retry 发送时
传入错误分类钩子 classify_response，按 reason 判断错误码和能否重试。
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
from lst.services.engines.common import (
    EngineSettings,
    build_info,
    check_count,
    failures,
    parse_json,
    split_blank,
)
from lst.services.engines.google.languages import google_code, supports_pair
from lst.services.engines.http import (
    EngineError,
    ErrorVerdict,
    RetryPolicy,
    Sleep,
    classify_status,
    send_with_retry,
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


def classify_response(status: int, body: bytes, default: ErrorVerdict, policy: RetryPolicy) -> ErrorVerdict:
    """send_with_retry 的错误分类钩子：按响应里的 reason 调整错误码与能否重试，并把 reason 写进详情。"""
    reasons = error_reasons(body)
    if not reasons:
        return default
    code, retryable = classify_error(status, reasons, policy)
    return ErrorVerdict(code, retryable, f"{default.detail}（reason: {', '.join(sorted(reasons))}）")


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
        blank, pending = split_blank(job.segments)
        for outcome in blank:
            yield outcome
        if not pending:
            return
        for outcome in await self._translate_batch(job, pending):
            yield outcome

    async def _translate_batch(self, job: Job, segments: Sequence[Segment]) -> list[SegmentOutcome]:
        if self._key is None:
            return failures(segments, ErrorCode.AUTH, "未配置 Google API key")
        target = google_code(job.target_lang)
        if target is None:
            return failures(
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
            response = await send_with_retry(
                self._client,
                build,
                policy=self._policy,
                sleep=self._sleep,
                rand=self._rand,
                classify=self._classify,
            )
            translations = parse_json(response.content, _TranslateResponse).data.translations
            check_count(len(translations), len(segments))
        except EngineError as exc:
            logger.info("Google 实例 %s 请求失败：%s", self._info.id, exc.detail)
            return failures(segments, exc.code, exc.detail)
        return [
            SegmentOutcome(segment.id, text=item.translated_text)
            for segment, item in zip(segments, translations, strict=True)
        ]

    def _classify(self, status: int, body: bytes, default: ErrorVerdict) -> ErrorVerdict:
        return classify_response(status, body, default, self._policy)


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
