"""DeepL 文本翻译引擎（架构文档 2.7.2 节）。

一个任务合并成一次 POST /v2/translate 请求，JSON 请求体里的 text 数组与任务中的段一一对应，
返回的 translations 与输入同序。认证只用请求头 `Authorization: DeepL-Auth-Key <key>`，
key 以 `:fx` 结尾的是免费版，走 api-free.deepl.com。
"""

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Literal

import httpx
from pydantic import BaseModel, Field

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
from lst.services.engines.deepl.languages import source_code, supports_pair, target_code
from lst.services.engines.http import EngineError, RetryPolicy, Sleep, send_with_retry

logger = logging.getLogger(__name__)

FREE_BASE_URL = "https://api-free.deepl.com"
PRO_BASE_URL = "https://api.deepl.com"
TRANSLATE_PATH = "/v2/translate"
REQUEST_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
# DeepL 对整个请求的大小限制为 128 KiB。
MAX_BODY_BYTES = 128 * 1024
# 语言表或请求构造方式变化时加一，让旧译文不再命中缓存。
_FINGERPRINT_VERSION = 1

Formality = Literal["default", "more", "less", "prefer_more", "prefer_less"]


class DeepLSettings(EngineSettings):
    """DeepL 实例的配置。

    formality 取 more 或 less 时，请求里发送 prefer_more 或 prefer_less：DeepL 只有部分目标语言支持
    正式程度，对不支持的语言传 more/less 会让整个请求失败，而 prefer_* 在这些语言上退回默认语气，
    在支持的语言上与 more/less 效果相同。
    """

    timeout_s: float = Field(default=8.0, gt=0, le=600)
    formality: Formality = "default"
    max_batch_segments: int = Field(default=50, ge=1, le=50)
    max_batch_chars: int = Field(default=30000, ge=1, le=30000)


class _Translation(BaseModel):
    text: str


class _TranslateResponse(BaseModel):
    translations: list[_Translation]


def base_url_for(key: str | None) -> str:
    """按 key 的后缀选择端点：`:fx` 结尾为免费版，其余为专业版。"""
    return FREE_BASE_URL if key is not None and key.endswith(":fx") else PRO_BASE_URL


def request_formality(formality: Formality) -> str | None:
    """配置值对应的请求参数；None 表示不发送 formality。"""
    if formality == "default":
        return None
    if formality in ("more", "prefer_more"):
        return "prefer_more"
    return "prefer_less"


def encode_json(payload: dict[str, object]) -> bytes:
    """与 httpx 的 json= 参数相同的编码方式：紧凑分隔符，非 ASCII 字符不转义。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def fit_context(payload: dict[str, object], context: str, limit: int = MAX_BODY_BYTES) -> str:
    """返回能放进请求体的最长 context 前缀。

    payload 是不含 context 的请求体。加入 context 等于在末尾的 `}` 前插入 `,"context":<字符串>`，
    所以总长可以精确算出；放不下全文时二分查找最长前缀，一点也放不下时返回空串。
    """
    base = len(encode_json(payload)) + len(b',"context":')

    def size(prefix: str) -> int:
        return base + len(json.dumps(prefix, ensure_ascii=False).encode("utf-8"))

    if size(context) <= limit:
        return context
    low, high = 0, len(context)
    while low < high:
        middle = (low + high + 1) // 2
        if size(context[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return context[:low]


def _parse(response: httpx.Response, expected: int) -> list[str]:
    """取出与输入同序的译文；结构不对或条数不符时抛出 BAD_RESPONSE。错误信息不含响应正文。"""
    translations = parse_json(response.content, _TranslateResponse).translations
    check_count(len(translations), expected)
    return [item.text for item in translations]


class DeepLEngine:
    """DeepL 引擎实例。key 在创建时读取一次，key 变化时由配置服务重建实例（EngineInfo.revision 随之改变）。"""

    def __init__(
        self,
        settings: DeepLSettings,
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
        base_url = base_url_for(key)
        self._url = base_url + TRANSLATE_PATH
        self._formality = request_formality(settings.formality)
        endpoint = "free" if base_url == FREE_BASE_URL else "pro"
        self._info = build_info(
            settings,
            kind=EngineKind.ONLINE_MT,
            billing=True,
            max_batch_segments=settings.max_batch_segments,
            max_batch_chars=settings.max_batch_chars,
            fingerprint_parts=("deepl", "v2", _FINGERPRINT_VERSION, endpoint, self._formality),
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
            return failures(segments, ErrorCode.AUTH, "未配置 DeepL API key")
        target = target_code(job.target_lang)
        if target is None:
            return failures(
                segments, ErrorCode.UNSUPPORTED_LANGUAGE, f"DeepL 不支持目标语言 {job.target_lang}"
            )

        body = self._encode(job, segments, target)
        headers = {"Authorization": f"DeepL-Auth-Key {self._key}", "Content-Type": "application/json"}

        def build() -> httpx.Request:
            return self._client.build_request(
                "POST", self._url, content=body, headers=headers, timeout=REQUEST_TIMEOUT
            )

        logger.debug("DeepL 实例 %s 发送 %d 段，请求体 %d 字节", self._info.id, len(segments), len(body))
        try:
            response = await send_with_retry(
                self._client, build, policy=self._policy, sleep=self._sleep, rand=self._rand
            )
            texts = _parse(response, len(segments))
        except EngineError as exc:
            logger.info("DeepL 实例 %s 请求失败：%s", self._info.id, exc.detail)
            return failures(segments, exc.code, exc.detail)
        return [SegmentOutcome(segment.id, text=text) for segment, text in zip(segments, texts, strict=True)]

    def _encode(self, job: Job, segments: Sequence[Segment], target: str) -> bytes:
        """构造请求体。源语言不在语言表里时不传 source_lang，由 DeepL 自动检测。"""
        payload: dict[str, object] = {"text": [segment.text for segment in segments], "target_lang": target}
        source = source_code(job.source_lang)
        if source is not None:
            payload["source_lang"] = source
        if self._formality is not None:
            payload["formality"] = self._formality
        if job.context:
            context = fit_context(payload, job.context)
            if len(context) < len(job.context):
                logger.debug(
                    "DeepL 实例 %s 的 context 超出请求大小限制，保留前 %d / %d 字符",
                    self._info.id,
                    len(context),
                    len(job.context),
                )
            if context:
                payload["context"] = context
        return encode_json(payload)


def create_deepl_engine(
    settings: DeepLSettings,
    secrets: SecretReader,
    client: httpx.AsyncClient,
    *,
    sleep: Sleep = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> Engine:
    """创建 DeepL 引擎实例。sleep 与 rand 只在测试中替换，用来跳过重试前的等待。"""
    key = secrets.get_secret(settings.secret) if settings.secret else None
    key = key.strip() if key else None
    return DeepLEngine(settings, key or None, client, sleep=sleep, rand=rand)
