"""OpenAI 兼容接口的 LLM 翻译引擎（需求 ENG-01、ENG-04，架构文档 2.7.1、2.7.3、6.4 节）。

DeepSeek、OpenAI、其他兼容服务，以及本地的 Ollama、llama.cpp server 共用这一个实现，
区别只在配置：provider 决定引擎类别、计费标记、结构化输出方式、默认的首 token 时限和重试策略。

一个任务按三步执行：

1. 整批发一次流式请求，每确认一项完整就立即产出该段的结果；
2. 流中途断开时，对还没出结果的段整批重发，最多重发 max_retries 次（需求 ENG-05），
   次数用完仍断开就把这些段记为失败；max_retries 为 0 时不重发；
3. 流正常结束后仍没有结果的段（输出缺段或整体无法解析），逐段各发一次请求。

HTTP 层的重试（429、5xx、连接失败）由 send_with_retry 按同一个 max_retries 执行。

请求失败（send_with_retry 抛出的 EngineError、首 token 超时、流中的 error 对象）时，
还没出结果的段全部记为该错误码的失败，不做逐段退回。

隐私（需求 PRV-02，架构文档第 10 章）：日志里只出现实例号、段数、错误码和技术细节，
不出现原文、译文、API key 和请求地址。
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import aclosing
from typing import Final, cast

import httpx

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.ports import SecretReader
from lst.contracts.translation import Engine, EngineInfo, EngineKind, Job, Segment, SegmentOutcome
from lst.services.engines.common import build_info
from lst.services.engines.http import (
    EngineError,
    RetryPolicy,
    Sleep,
    aiter_sse_data,
    classify_transport_error,
    send_with_retry,
)
from lst.services.engines.openai_compat.parser import ReplyParser, parse_chunk
from lst.services.engines.openai_compat.prompt import (
    PROMPT_VERSION,
    build_messages,
    effective_system_prompt,
    response_format,
)
from lst.services.engines.openai_compat.settings import OpenAICompatSettings, OutputMode

logger = logging.getLogger(__name__)

# 架构文档 2.7.3 节的时限。首 token 时限从请求发出开始计时，到收到第一个非空 content 为止；
# 空闲时限是收到首 token 之后两次数据之间的最长间隔。
CONNECT_TIMEOUT_S: Final = 3.0
WRITE_TIMEOUT_S: Final = 10.0
POOL_TIMEOUT_S: Final = 3.0
IDLE_TIMEOUT_S: Final = 10.0
ONLINE_FIRST_TOKEN_S: Final = 8.0
LOCAL_FIRST_TOKEN_COLD_S: Final = 60.0  # 含模型加载
LOCAL_FIRST_TOKEN_WARM_S: Final = 15.0

# 本地服务只重试 503（模型加载中），不重试传输错误（架构文档 2.7.3 节）；在线服务用默认的
# 429、5xx 与传输错误。重试次数都取配置的 max_retries。
_LOCAL_RETRY_STATUSES: Final = frozenset({503})

# 逐段退回时，这两类错误只与这一段有关，其余段继续请求；其他错误码说明服务本身出了问题，
# 剩下的段直接记为同样的失败，不再发请求。
_SEGMENT_LEVEL_CODES: Final = frozenset({ErrorCode.BAD_REQUEST, ErrorCode.BAD_RESPONSE})

_MISSING_DETAIL: Final = "模型输出缺少该段或无法解析"


class _StreamBroken(EngineError):
    """流式响应在中途断开（传输异常或空闲超时）。整批请求遇到它时，对未完成的段重发。"""


class OpenAICompatEngine:
    """OpenAI 兼容 LLM 引擎的一个实例。用 create_openai_compat_engine 创建。

    API key 在创建时读取一次；key 变化时配置服务会重建实例（EngineInfo.revision 随之改变）。
    """

    def __init__(
        self,
        settings: OpenAICompatSettings,
        secrets: SecretReader,
        client: httpx.AsyncClient,
        *,
        sleep: Sleep = asyncio.sleep,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
    ) -> None:
        self._settings = settings
        self._client = client
        self._sleep = sleep
        self._idle_timeout_s = idle_timeout_s
        if settings.is_local:
            self._policy = RetryPolicy(
                max_retries=settings.max_retries,
                retry_statuses=_LOCAL_RETRY_STATUSES,
                retry_transport_errors=False,
            )
        else:
            self._policy = RetryPolicy(max_retries=settings.max_retries)
        self._system_prompt = effective_system_prompt(settings.system_prompt)
        self._mode: OutputMode = settings.output_mode
        self._endpoint = settings.endpoint
        # 本地服务成功返回过首 token，说明模型已经加载，之后的请求用较短的首 token 时限。
        self._warm = False

        api_key = secrets.get_secret(settings.secret) if settings.secret else None
        self._api_key = api_key or None
        self._headers = {"Accept": "text/event-stream"}
        if self._api_key is not None:
            self._headers["Authorization"] = f"Bearer {self._api_key}"

        self._info = build_info(
            settings,
            kind=EngineKind.LOCAL_LLM if settings.is_local else EngineKind.ONLINE_LLM,
            billing=not settings.is_local,
            max_batch_segments=settings.max_batch_segments,
            max_batch_chars=settings.max_batch_chars,
            fingerprint_parts=(
                "openai_compat",
                PROMPT_VERSION,
                settings.provider,
                self._endpoint,
                settings.model,
                settings.temperature,
                self._system_prompt,
                self._mode,
            ),
            secret_value=self._api_key,
        )

    @property
    def info(self) -> EngineInfo:
        return self._info

    @property
    def first_token_timeout_s(self) -> float:
        """下一个请求生效的首 token 时限。"""
        override = self._settings.first_token_timeout_s
        if override is not None:
            return override
        if not self._settings.is_local:
            return ONLINE_FIRST_TOKEN_S
        return LOCAL_FIRST_TOKEN_WARM_S if self._warm else LOCAL_FIRST_TOKEN_COLD_S

    def supports(self, source: Lang, target: Lang) -> bool:
        if source == target:
            return False
        languages = self._settings.languages
        return languages is None or (source in languages and target in languages)

    async def translate(self, job: Job) -> AsyncIterator[SegmentOutcome]:
        """按模块说明里的三步翻译一个任务，每个段恰好产出一次结果。"""
        if not job.segments:
            return
        if self._api_key is None and self._settings.needs_api_key:
            logger.warning("实例 %s 没有配置 API key，%d 段全部失败", self._info.id, len(job.segments))
            for segment in job.segments:
                yield SegmentOutcome(segment.id, error="缺少 API key", code=ErrorCode.AUTH)
            return

        pending = {segment.id: segment for segment in job.segments}
        # 会话全文为空时用本批原文拼接；本任务的所有请求共用它，开头因此相同。
        context = job.context or "\n".join(segment.text for segment in job.segments)

        # 第 1、2 步：整批请求，流中途断开时对未完成的段最多重发 max_retries 次。
        batch = list(job.segments)
        failure: EngineError | None = None
        resends = self._settings.max_retries
        for attempt in range(resends + 1):
            try:
                async with aclosing(self._request(job, batch, context)) as replies:
                    async for segment, text in replies:
                        if pending.pop(segment.id, None) is not None:
                            yield SegmentOutcome(segment.id, text=text)
            except _StreamBroken as exc:
                batch = [segment for segment in batch if segment.id in pending]
                if not batch:  # 所有段都已经有结果，断开不影响任务
                    break
                if attempt < resends:
                    logger.warning(
                        "实例 %s 的流式响应中断（%s），重发未完成的 %d 段（第 %d/%d 次）",
                        self._info.id,
                        exc.detail,
                        len(batch),
                        attempt + 1,
                        resends,
                    )
                    continue
                failure = exc
            except EngineError as exc:
                failure = exc
            break

        if failure is not None:
            logger.warning(
                "实例 %s 的请求失败（%s：%s），%d 段记为失败",
                self._info.id,
                failure.code,
                failure.detail,
                len(pending),
            )
            for segment in job.segments:
                if pending.pop(segment.id, None) is not None:
                    yield SegmentOutcome(segment.id, error=failure.detail, code=failure.code)
            return

        missing = [segment for segment in job.segments if segment.id in pending]
        if not missing:
            return
        if len(batch) == 1:
            # 最后一次请求本来就只含这一段，等同于已经逐段请求过。
            for segment in missing:
                del pending[segment.id]
                yield SegmentOutcome(segment.id, error=_MISSING_DETAIL, code=ErrorCode.BAD_RESPONSE)
            return

        # 第 3 步：逐段退回，按顺序执行。
        logger.warning("实例 %s 的输出缺少 %d/%d 段，退回逐段请求", self._info.id, len(missing), len(batch))
        stop: EngineError | None = None  # 服务级错误：之后的段不再发请求
        for segment in missing:
            error = stop
            if error is None:
                try:
                    async with aclosing(self._request(job, [segment], context)) as replies:
                        async for got, text in replies:
                            if pending.pop(got.id, None) is not None:
                                yield SegmentOutcome(got.id, text=text)
                except EngineError as exc:
                    error = exc
                if segment.id not in pending:
                    continue
                if error is None:
                    error = EngineError(ErrorCode.BAD_RESPONSE, _MISSING_DETAIL)
                logger.warning("实例 %s 的逐段请求失败（%s：%s）", self._info.id, error.code, error.detail)
                if error.code not in _SEGMENT_LEVEL_CODES:
                    stop = error
            del pending[segment.id]
            yield SegmentOutcome(segment.id, error=error.detail, code=error.code)

    async def _request(
        self, job: Job, batch: Sequence[Segment], context: str
    ) -> AsyncGenerator[tuple[Segment, str]]:
        """发一次流式请求，逐个产出 (段, 译文)。

        抛出 EngineError：请求失败、首 token 超时、流中的 error 对象、chunk 无法解析；
        抛出 _StreamBroken：首 token 之后的传输异常或空闲超时，以及首 token 之前的非超时传输异常。
        流正常结束但缺段时正常返回，由调用方核对。
        """
        body = self._request_body(job, batch, context)
        first_token_s = self.first_token_timeout_s
        # httpx 的 read 时限同时约束等待响应头的时间，所以不能小于首 token 时限。
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_S,
            read=max(self._idle_timeout_s, first_token_s),
            write=WRITE_TIMEOUT_S,
            pool=POOL_TIMEOUT_S,
        )
        loop = asyncio.get_running_loop()
        logger.debug("实例 %s 发送请求：%d 段，首 token 时限 %g 秒", self._info.id, len(batch), first_token_s)

        try:
            async with asyncio.timeout(None) as scope:

                def build() -> httpx.Request:
                    # 每次（重）试都从发出请求的这一刻重新计算首 token 时限，退避等待不计入。
                    scope.reschedule(loop.time() + first_token_s)
                    return self._client.build_request(
                        "POST", self._endpoint, json=body, headers=self._headers, timeout=timeout
                    )

                response = await send_with_retry(
                    self._client, build, policy=self._policy, stream=True, sleep=self._sleep
                )
        except TimeoutError:
            raise self._first_token_timeout(first_token_s) from None
        first_token_deadline = scope.when()
        assert first_token_deadline is not None

        parser = ReplyParser()
        seen: set[int] = set()
        got_token = False
        finish_reason: str | None = None
        events = aiter_sse_data(response)
        try:
            while True:
                limit = first_token_deadline if not got_token else loop.time() + self._idle_timeout_s
                try:
                    # 时限只包住一次读取，不跨越 yield：yield 时控制权在调用方，
                    # 跨越 yield 的取消作用域会把取消送到调用方的代码里（PEP 789）。
                    async with asyncio.timeout_at(limit):
                        data = await anext(events, None)
                except TimeoutError:
                    if not got_token:
                        raise self._first_token_timeout(first_token_s) from None
                    raise _StreamBroken(
                        ErrorCode.TIMEOUT, f"流式响应空闲超过 {self._idle_timeout_s:g} 秒"
                    ) from None
                except httpx.TransportError as exc:
                    if not got_token and isinstance(exc, httpx.TimeoutException):
                        raise self._first_token_timeout(first_token_s) from exc
                    raise _StreamBroken(
                        classify_transport_error(exc), f"流式响应中断：{type(exc).__name__}: {exc}"
                    ) from exc
                if data is None:
                    break
                content, finish = parse_chunk(data)
                finish_reason = finish or finish_reason
                if not content:
                    continue
                if not got_token:
                    got_token = True
                    self._warm = True
                for raw in parser.feed(content):
                    accepted = self._accept(raw, batch, seen)
                    if accepted is not None:
                        yield accepted
            for raw in parser.finish():
                accepted = self._accept(raw, batch, seen)
                if accepted is not None:
                    yield accepted
        finally:
            if isinstance(events, AsyncGenerator):
                await events.aclose()
            await response.aclose()

        if not parser.closed:
            logger.warning(
                "实例 %s 的模型输出无法完整解析（已确认 %d 项，finish_reason=%s）",
                self._info.id,
                parser.taken,
                finish_reason,
            )
        logger.debug("实例 %s 的请求结束：%d/%d 段有结果", self._info.id, len(seen), len(batch))

    def _request_body(self, job: Job, batch: Sequence[Segment], context: str) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self._settings.model,
            "messages": build_messages(
                self._system_prompt,
                context,
                [segment.text for segment in batch],
                job.source_lang,
                job.target_lang,
            ),
            "temperature": self._settings.temperature,
            "stream": True,
            "response_format": response_format(self._mode),
        }
        if self._settings.provider == "deepseek":
            # DeepSeek 的两个模型默认开启思考模式，会推迟首个 token（架构文档 2.7.1 节）。
            body["thinking"] = {"type": "disabled"}
        return body

    def _accept(self, raw: object, batch: Sequence[Segment], seen: set[int]) -> tuple[Segment, str] | None:
        """校验一项输出，合格时返回对应的段和去掉首尾空白的译文；不合格时记日志并返回 None。

        日志只写序号和类型名，不写内容：模型可能把原文或译文放进任何字段。
        """
        if not isinstance(raw, dict):
            logger.warning("实例 %s 的输出项不是对象（%s），已忽略", self._info.id, type(raw).__name__)
            return None
        fields = cast(dict[str, object], raw)
        index, text = fields.get("i"), fields.get("t")
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(batch):
            shown = index if isinstance(index, int) else type(index).__name__
            logger.warning("实例 %s 的输出项序号无效（%s），已忽略", self._info.id, shown)
            return None
        if not isinstance(text, str):
            logger.warning(
                "实例 %s 的输出项 %d 的译文不是字符串（%s），已忽略",
                self._info.id,
                index,
                type(text).__name__,
            )
            return None
        if index in seen:
            logger.warning("实例 %s 的输出项序号 %d 重复，已忽略", self._info.id, index)
            return None
        seen.add(index)
        return batch[index - 1], text.strip()

    def _first_token_timeout(self, limit: float) -> EngineError:
        # 本地服务首 token 超时，多半是模型被卸载后需要重新加载（Ollama 默认空闲 5 分钟卸载），
        # 下一个请求恢复使用含模型加载的时限。
        if self._settings.is_local:
            self._warm = False
        return EngineError(ErrorCode.TIMEOUT, f"首 token 超时（{limit:g} 秒）")


def create_openai_compat_engine(
    settings: OpenAICompatSettings,
    secrets: SecretReader,
    client: httpx.AsyncClient,
    *,
    sleep: Sleep = asyncio.sleep,
) -> Engine:
    """创建一个 OpenAI 兼容 LLM 引擎实例。sleep 是重试前的等待函数，测试时注入以免真实等待。"""
    return OpenAICompatEngine(settings, secrets, client, sleep=sleep)
