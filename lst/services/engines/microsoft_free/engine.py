"""微软免费翻译引擎：Edge 浏览器使用的 edge.microsoft.com/translate/translatetext 接口，不需要 API key。

做法照参考项目 thunderbird-translate 的 src/background/translators/microsoft.ts（免费模式）：

- 请求：POST {endpoint}?from=<源>&to=<目标>&isEnterpriseClient=false，JSON 请求体是段落字符串的数组，
  一个任务只发一次请求。
- 这条路由会对文本做 HTML 标签对齐，原文里单独的 “<” 会被当成标签。所以发送前把 &、<、> 转义成
  HTML 实体，收到译文后再解码回来。
- 响应：[{"translations": [{"text": "译文", "to": "zh-Hans"}]}, ...]，与请求数组一一对应，取每项的
  translations[0].text。

旧的免费路由要先从 edge.microsoft.com/translate/auth 取 token，参考项目的文档写明微软在
2026 年 7 月停用了这个 token 接口，所以这里只用直连接口，不取 token。

这个接口没有公开文档，随时可能变动或限流。文本会发送给微软。请求的查询串里只有语言码，
原文只出现在请求体里（需求 PRV-02）。

单次请求的上限照参考项目的设置文档：最多 100 段、5000 个字符。
"""

import asyncio
import html
import json
import logging
import random
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Final
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, RootModel, field_validator

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.translation import Engine, EngineInfo, EngineKind, Job, Segment, SegmentOutcome
from lst.services.engines.common import (
    EngineSettings,
    build_info,
    check_count,
    failures,
    parse_json,
    split_blank,
)
from lst.services.engines.http import (
    EngineError,
    ErrorVerdict,
    RetryPolicy,
    Sleep,
    send_with_retry,
)
from lst.services.engines.microsoft_free.languages import microsoft_code, supports_pair

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT: Final = "https://edge.microsoft.com/translate/translatetext"
REQUEST_TIMEOUT: Final = httpx.Timeout(8.0, connect=3.0)
MAX_SEGMENTS: Final = 100
MAX_CHARS: Final = 5000
# 语言表或请求构造方式变化时加一，让旧译文不再命中缓存。
_FINGERPRINT_VERSION = 1


class MicrosoftFreeSettings(EngineSettings):
    """微软免费翻译实例的配置。不需要 API key，secret 字段不使用。

    endpoint 是翻译接口的完整地址，默认 https://edge.microsoft.com/translate/translatetext；
    语言参数由引擎加在查询串里。微软改动这条路由时可以在这里改地址。
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
        return value


class _Translation(BaseModel):
    text: str


class _Item(BaseModel):
    translations: list[_Translation] = Field(min_length=1)


class _Response(RootModel[list[_Item]]):
    pass


def escape_text(text: str) -> str:
    """发送前转义 &、<、>，先转义 &，避免把后两步产生的实体再转义一次。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def decode_text(text: str) -> str:
    """把译文里的 HTML 实体解码回字符（&lt;、&gt;、&quot;、&#39;、&#数字;、&amp; 等）。"""
    return html.unescape(text)


def encode_body(texts: Sequence[str]) -> bytes:
    """JSON 字符串数组请求体，每段先转义 HTML 特殊字符。"""
    return json.dumps(
        [escape_text(text) for text in texts], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def parse_response(content: bytes, expected: int) -> list[str]:
    """取出与输入同序的译文并解码 HTML 实体；结构不对或条数不符时抛出 BAD_RESPONSE。"""
    items = parse_json(content, _Response).root
    check_count(len(items), expected)
    return [decode_text(item.translations[0].text) for item in items]


def keyless_verdict(status: int, body: bytes, default: ErrorVerdict) -> ErrorVerdict:
    """send_with_retry 的错误分类钩子：这个接口没有 key，401、403 表示请求被拒绝，不是 key 无效。

    默认分类会把 401、403 记为 AUTH，界面上的建议是“检查 API key”，对免费接口没有意义，
    所以改记为 BAD_REQUEST，不重试。其余状态码（429 限流、5xx 服务端错误等）保持默认分类。
    """
    if default.code is ErrorCode.AUTH:
        return ErrorVerdict(ErrorCode.BAD_REQUEST, False, default.detail)
    return default


class MicrosoftFreeEngine:
    """微软免费翻译引擎实例。"""

    def __init__(
        self,
        settings: MicrosoftFreeSettings,
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
            fingerprint_parts=("microsoft_free", "edge", _FINGERPRINT_VERSION, self._endpoint),
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
        target = microsoft_code(job.target_lang)
        if target is None:
            return failures(
                segments, ErrorCode.UNSUPPORTED_LANGUAGE, f"微软免费接口不支持目标语言 {job.target_lang}"
            )
        # 源语言不在语言表里时不传 from，由微软自动检测（参考项目的做法）。
        params: dict[str, str] = {}
        source = microsoft_code(job.source_lang)
        if source is not None:
            params["from"] = source
        params["to"] = target
        params["isEnterpriseClient"] = "false"
        body = encode_body([segment.text for segment in segments])
        headers = {"Content-Type": "application/json"}

        def build() -> httpx.Request:
            return self._client.build_request(
                "POST", self._endpoint, params=params, content=body, headers=headers, timeout=REQUEST_TIMEOUT
            )

        logger.debug("微软免费实例 %s 发送 %d 段，请求体 %d 字节", self._info.id, len(segments), len(body))
        try:
            response = await send_with_retry(
                self._client,
                build,
                policy=self._policy,
                sleep=self._sleep,
                rand=self._rand,
                classify=keyless_verdict,
            )
            texts = parse_response(response.content, len(segments))
        except EngineError as exc:
            logger.info("微软免费实例 %s 请求失败：%s", self._info.id, exc.detail)
            return failures(segments, exc.code, exc.detail)
        return [SegmentOutcome(segment.id, text=text) for segment, text in zip(segments, texts, strict=True)]


def create_microsoft_free_engine(
    settings: MicrosoftFreeSettings,
    client: httpx.AsyncClient,
    *,
    sleep: Sleep = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> Engine:
    """创建微软免费翻译引擎实例。sleep 与 rand 只在测试中替换，用来跳过重试前的等待。"""
    return MicrosoftFreeEngine(settings, client, sleep=sleep, rand=rand)
