"""百度翻译开放平台通用文本翻译引擎（架构文档 2.7.2 节）。

请求用 POST 表单提交，原文只出现在请求体里，不进入 URL（需求 PRV-02）。一个任务的各段先把段内换行
替换为空格，再用 `\\n` 拼接成一个 q，返回的 trans_result 按行与各段一一对应。

百度把业务错误放在 HTTP 200 的响应体里（error_code），HTTP 层的错误仍交给 send_with_retry 处理。
每个实例有一个令牌桶限速器，按所在版本的 QPS 放行请求，HTTP 层与业务层的重试都要先领令牌。
"""

import asyncio
import hashlib
import logging
import random
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.ports import SecretReader
from lst.contracts.translation import Engine, EngineInfo, EngineKind, Job, SegmentOutcome
from lst.services.engines.baidu.languages import source_code, supports_pair, target_code
from lst.services.engines.common import (
    EngineSettings,
    build_info,
    check_count,
    failures,
    parse_json,
    split_blank,
)
from lst.services.engines.http import EngineError, RateLimiter, RetryPolicy, Sleep, send_with_retry

logger = logging.getLogger(__name__)

TRANSLATE_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"
REQUEST_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
# 百度建议单次请求的 q 不超过 6000 字节（UTF-8，约 2000 个汉字）。
MAX_QUERY_BYTES = 6000
# 语言表或请求构造方式变化时加一，让旧译文不再命中缓存。
_FINGERPRINT_VERSION = 1

Tier = Literal["standard", "advanced", "premium"]

# 标准版、高级版、尊享版的每秒请求数上限。
TIER_QPS: Mapping[Tier, int] = MappingProxyType({"standard": 1, "advanced": 10, "premium": 100})

# error_code → (错误码, 重试前等待的秒数)。等待秒数为 None 的错误不重试。
# 52001（请求超时）、52002（系统错误）对应 HTTP 的超时和 5xx，与它们一样重试；
# 54003 是普通限流；54005 是长 query 请求过于频繁，百度要求 3 秒后再试。
ERROR_RULES: Mapping[str, tuple[ErrorCode, float | None]] = MappingProxyType(
    {
        "52001": (ErrorCode.TIMEOUT, 1.0),
        "52002": (ErrorCode.SERVER, 1.0),
        "52003": (ErrorCode.AUTH, None),
        "54000": (ErrorCode.BAD_REQUEST, None),
        "54001": (ErrorCode.AUTH, None),
        "54003": (ErrorCode.RATE_LIMITED, 1.0),
        "54004": (ErrorCode.QUOTA, None),
        "54005": (ErrorCode.RATE_LIMITED, 3.0),
        "58000": (ErrorCode.AUTH, None),
        "58001": (ErrorCode.UNSUPPORTED_LANGUAGE, None),
        "58002": (ErrorCode.SERVER, None),
        "58003": (ErrorCode.RATE_LIMITED, None),
    }
)
# 有的响应在成功时也带 error_code，取值为 52000。
_SUCCESS_CODES = frozenset({"0", "52000"})
_MESSAGE_LIMIT = 200

# 除 \r\n、\r、\n 之外，也替换 str.splitlines 认作换行的其他字符（\v、\f、\x1c 至 \x1e、\x85、
# \u2028、\u2029），避免它们在服务端被当成分行。
_LINE_BREAK = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


class BaiduSettings(EngineSettings):
    """百度实例的配置。appid 不是机密，写在配置文件里；secret 是 keyring 里密钥条目的名称。

    max_batch_chars 默认 1800：调度器按字符数打包，1800 个汉字是 5400 字节，留出余量；
    发送前还会按 UTF-8 字节数检查，超过 6000 字节时拆成多次请求。
    """

    timeout_s: float = Field(default=8.0, gt=0, le=600)
    appid: str = Field(min_length=1)
    tier: Tier = "standard"
    max_batch_segments: int = Field(default=100, ge=1)
    max_batch_chars: int = Field(default=1800, ge=1, le=6000)


class BaiduApiError(EngineError):
    """响应体里的 error_code 表示的业务错误。retry_after_s 不为 None 时，在重试次数之内等待这么久后重试。"""

    def __init__(self, error_code: str, message: str) -> None:
        code, retry_after_s = ERROR_RULES.get(error_code, (ErrorCode.BAD_RESPONSE, None))
        super().__init__(code, f"百度错误码 {error_code}: {message[:_MESSAGE_LIMIT]}")
        self.error_code = error_code
        self.retry_after_s = retry_after_s


class _Item(BaseModel):
    dst: str


class _Response(BaseModel):
    error_code: str | int | None = None
    error_msg: str | None = None
    trans_result: list[_Item] | None = None


def make_sign(appid: str, query: str, salt: str, secret: str) -> str:
    """签名：MD5(appid + q + salt + 密钥) 的 32 位小写十六进制。q 是 UTF-8 原文，不做 URL 编码。"""
    raw = (appid + query + salt + secret).encode("utf-8")
    return hashlib.md5(raw, usedforsecurity=False).hexdigest()


def flatten(text: str) -> str:
    """把段内的换行（\\r\\n、\\n、\\r 等）替换为空格。q 按行切分结果，段内换行会让返回的段数对不上。"""
    return _LINE_BREAK.sub(" ", text)


def split_by_bytes(texts: Sequence[str], limit: int = MAX_QUERY_BYTES) -> list[range]:
    """把各段按顺序贪心分组，每组用换行拼接后的 UTF-8 字节数不超过 limit，返回各组的下标范围。

    单段本身超过 limit 时独占一组，照常发送，由百度决定是否接受。
    """
    groups: list[range] = []
    start = 0
    size = 0
    for index, text in enumerate(texts):
        length = len(text.encode("utf-8"))
        if index > start and size + 1 + length > limit:
            groups.append(range(start, index))
            start, size = index, length
        else:
            size = length if index == start else size + 1 + length
    if texts:
        groups.append(range(start, len(texts)))
    return groups


def random_salt() -> str:
    return str(random.randint(32768, 65536))


def _parse(response: httpx.Response, expected: int) -> list[str]:
    """取出与各行同序的译文。业务错误抛出 BaiduApiError，结构不对或条数不符抛出 BAD_RESPONSE。"""
    parsed = parse_json(response.content, _Response)
    if parsed.error_code is not None and str(parsed.error_code) not in _SUCCESS_CODES:
        raise BaiduApiError(str(parsed.error_code), parsed.error_msg or "")
    if parsed.trans_result is None:
        raise EngineError(ErrorCode.BAD_RESPONSE, "响应缺少 trans_result")
    check_count(len(parsed.trans_result), expected)
    return [item.dst for item in parsed.trans_result]


class BaiduEngine:
    """百度引擎实例。密钥在创建时读取一次，密钥变化时由配置服务重建实例。"""

    def __init__(
        self,
        settings: BaiduSettings,
        secret: str | None,
        client: httpx.AsyncClient,
        *,
        sleep: Sleep,
        clock: Callable[[], float],
        rand: Callable[[], float],
        salt: Callable[[], str],
    ) -> None:
        self._appid = settings.appid
        self._secret = secret
        self._max_retries = settings.max_retries
        self._client = client
        self._sleep = sleep
        self._rand = rand
        self._salt = salt
        self._limiter = RateLimiter(TIER_QPS[settings.tier], clock=clock, sleep=sleep)
        self._info = build_info(
            settings,
            kind=EngineKind.ONLINE_MT,
            billing=True,
            max_batch_segments=settings.max_batch_segments,
            max_batch_chars=settings.max_batch_chars,
            fingerprint_parts=("baidu", "vip/translate", _FINGERPRINT_VERSION),
            secret_value=secret,
        )

    @property
    def info(self) -> EngineInfo:
        return self._info

    def supports(self, source: Lang, target: Lang) -> bool:
        return supports_pair(source, target)

    async def translate(self, job: Job) -> AsyncIterator[SegmentOutcome]:
        # 换行替换后为空白的段不发给接口（百度会跳过空行，条数就对不上了），原样作为译文返回。
        blank, segments = split_blank(job.segments, [flatten(segment.text) for segment in job.segments])
        for outcome in blank:
            yield outcome
        if not segments:
            return
        lines = [flatten(segment.text) for segment in segments]
        secret = self._secret
        target = target_code(job.target_lang)
        if secret is None:
            for outcome in failures(segments, ErrorCode.AUTH, "未配置百度翻译密钥"):
                yield outcome
            return
        if target is None:
            detail = f"百度不支持目标语言 {job.target_lang}"
            for outcome in failures(segments, ErrorCode.UNSUPPORTED_LANGUAGE, detail):
                yield outcome
            return
        source = source_code(job.source_lang)

        groups = split_by_bytes(lines)
        for number, group in enumerate(groups, start=1):
            query = "\n".join(lines[index] for index in group)
            logger.debug(
                "百度实例 %s 发送第 %d/%d 组：%d 段，q 共 %d 字节",
                self._info.id,
                number,
                len(groups),
                len(group),
                len(query.encode("utf-8")),
            )
            try:
                texts = await self._request(query, source, target, secret, len(group))
            except EngineError as exc:
                error = exc
            else:
                for index, text in zip(group, texts, strict=True):
                    yield SegmentOutcome(segments[index].id, text=text)
                continue
            # 本组及之后各组都记为失败，不再发送：认证、配额、限流类错误下继续发送没有意义。
            logger.info("百度实例 %s 请求失败：%s", self._info.id, error.detail)
            for outcome in failures(segments[group.start :], error.code, error.detail):
                yield outcome
            return

    async def _request(self, query: str, source: str, target: str, secret: str, expected: int) -> list[str]:
        """发送一组段，返回译文。每次发送前领令牌。

        HTTP 层（429、5xx、网络错误）与业务层（52001、52002、54003、54005）的重试共用 max_retries。
        build 每调用一次算一次尝试。已经尝试 attempts 次时，下一轮 send_with_retry 的 HTTP 层最多再重试
        max_retries - attempts 次；业务层只在 attempts 不超过 max_retries 时重试。
        所以总尝试次数不超过 max_retries + 1。
        """
        attempts = 0

        def build() -> httpx.Request:
            nonlocal attempts
            attempts += 1
            salt = self._salt()
            form = {
                "q": query,
                "from": source,
                "to": target,
                "appid": self._appid,
                "salt": salt,
                "sign": make_sign(self._appid, query, salt, secret),
            }
            return self._client.build_request("POST", TRANSLATE_URL, data=form, timeout=REQUEST_TIMEOUT)

        async def wait_then_acquire(delay: float) -> None:
            # send_with_retry 在 HTTP 层重试前调用：退避结束后再领令牌，重试也受 QPS 约束。
            await self._sleep(delay)
            await self._limiter.acquire()

        await self._limiter.acquire()
        while True:
            policy = RetryPolicy(max_retries=self._max_retries - attempts)
            response = await send_with_retry(
                self._client, build, policy=policy, sleep=wait_then_acquire, rand=self._rand
            )
            try:
                return _parse(response, expected)
            except BaiduApiError as exc:
                if exc.retry_after_s is None or attempts > self._max_retries:
                    raise
                logger.debug(
                    "百度实例 %s 收到错误码 %s，%g 秒后重试", self._info.id, exc.error_code, exc.retry_after_s
                )
                await self._sleep(exc.retry_after_s)
                await self._limiter.acquire()


def create_baidu_engine(
    settings: BaiduSettings,
    secrets: SecretReader,
    client: httpx.AsyncClient,
    *,
    sleep: Sleep = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    rand: Callable[[], float] = random.random,
    salt: Callable[[], str] = random_salt,
) -> Engine:
    """创建百度引擎实例。sleep、clock、rand、salt 只在测试中替换：跳过等待、固定签名。"""
    secret = secrets.get_secret(settings.secret) if settings.secret else None
    secret = secret.strip() if secret else None
    return BaiduEngine(settings, secret or None, client, sleep=sleep, clock=clock, rand=rand, salt=salt)
