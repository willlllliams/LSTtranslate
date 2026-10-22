"""OpenAI 兼容接口的 LLM 翻译引擎（需求 ENG-01、ENG-04，架构文档 2.7.1、2.7.3、6.4 节）。

DeepSeek、OpenAI、其他兼容服务，以及本地的 Ollama、llama.cpp server 共用这一个实现，
区别只在配置：provider 决定引擎类别、计费标记、结构化输出方式、默认的首 token 时限和重试策略。

一个任务按三步执行：

1. 整批发一次流式请求，每确认一项完整就立即产出该段的结果；
2. 流中途断开时，对还没出结果的段整批重发（需求 ENG-05），重试机会用完仍断开就把这些段记为失败；
3. 流正常结束后仍没有结果的段（输出缺段、译文为空或整体无法解析），逐段各发一次请求。

重试次数（架构文档 2.7.3 节“最多重试 max_retries 次”，默认 1）：HTTP 层的重试（429、5xx、连接失败，
由 send_with_retry 执行）与第 2 步的断流重发共用一份额度，_Budget 记录已经发出的请求数。
所以第 1、2 步合计最多发出 max_retries + 1 个请求；第 3 步每个段各有一份同样的额度，
每段最多 max_retries + 1 个请求。记任务中第 1、2 步之后仍缺的段数为 m，一个任务最多发出
(max_retries + 1) × (1 + m) 个请求；默认配置下整批最多 2 个，逐段退回时每段最多再 2 个。

请求失败（send_with_retry 抛出的 EngineError、首 token 超时、流中的 error 对象）时，
还没出结果的段全部记为该错误码的失败，不做逐段退回。

附带截图（需求 ENG-07）：配置开启 send_image、任务带有截图（Job.image）时，每个请求的 user 消息
都附带这张截图（prompt.with_image）。服务以“不支持图片”类错误拒绝请求时，错误码改为
engine.image_unsupported，界面据此提示关闭“附带截图”。判断条件见 _rejects_image。

隐私（需求 PRV-02，架构文档第 10 章）：日志里只出现实例号、段数、错误码和技术细节，
不出现原文、译文、API key 和请求地址。附带截图时还要保证日志和错误详情里不出现图片内容：
请求体从不写日志；服务端在错误消息里回显的 data URL 与长 base64 片段由 _scrub_image_data 替换掉。
"""

import asyncio
import base64
import logging
import re
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import aclosing
from dataclasses import replace
from typing import Final, cast

import httpx

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.ports import SecretReader
from lst.contracts.translation import (
    Engine,
    EngineInfo,
    EngineKind,
    Job,
    JobImage,
    Segment,
    SegmentOutcome,
)
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
    with_image,
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

_MISSING_DETAIL: Final = "模型输出缺少该段、译文为空或无法解析"

# 服务拒绝图片输入时错误消息里常见的词（小写比较）。OpenAI 对不支持图片的模型返回 400，消息提到
# image_url 只被部分模型支持；DeepSeek 返回 400“unknown variant `image_url`”；Ollama 对不带视觉
# 组件的模型返回“missing data required for image input”；llama.cpp server 没有加载 mmproj 时
# 返回“image input is not supported”。
_IMAGE_HINTS: Final = ("image", "vision", "multimodal", "multi-modal", "mmproj")

# 错误消息里可能回显的图片数据：data URL，以及 64 个字符以上的连续 base64 片段。
_IMAGE_DATA_RE: Final = re.compile(r"data:image/[^\s,]*(?:,[A-Za-z0-9+/=]*)?|[A-Za-z0-9+/]{64,}={0,2}")
_IMAGE_DATA_PLACEHOLDER: Final = "<图片数据已省略>"


class _StreamBroken(EngineError):
    """流式响应在中途断开（传输异常或空闲超时）。整批请求遇到它时，对未完成的段重发。"""


class _Budget:
    """一组请求共用的重试额度：最多发出 max_retries + 1 个请求，send_with_retry 每发一个记一次。"""

    __slots__ = ("limit", "sent")

    def __init__(self, max_retries: int) -> None:
        self.limit = max_retries + 1
        self.sent = 0

    @property
    def retries_left(self) -> int:
        """发出下一个请求之后，还能再重试几次；没有额度发出下一个请求时为负数。"""
        return self.limit - self.sent - 1


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
            image_max_side=settings.image_max_side if settings.send_image else None,
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
        # 截图只编码一次 base64，本任务的整批、重发和逐段请求共用。
        image = job.image if self._settings.send_image else None
        data_url = _data_url(image) if image is not None else None

        # 第 1、2 步：整批请求，流中途断开时对未完成的段最多重发 max_retries 次。
        batch = list(job.segments)
        failure: EngineError | None = None
        budget = _Budget(self._settings.max_retries)
        while True:
            try:
                async with aclosing(self._request(job, batch, context, data_url, budget)) as replies:
                    async for segment, text in replies:
                        if pending.pop(segment.id, None) is not None:
                            yield SegmentOutcome(segment.id, text=text)
            except _StreamBroken as exc:
                batch = [segment for segment in batch if segment.id in pending]
                if not batch:  # 所有段都已经有结果，断开不影响任务
                    break
                if budget.retries_left >= 0:
                    logger.warning(
                        "实例 %s 的流式响应中断（%s），重发未完成的 %d 段（已发出 %d/%d 个请求）",
                        self._info.id,
                        exc.detail,
                        len(batch),
                        budget.sent,
                        budget.limit,
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
                    single = self._request(
                        job, [segment], context, data_url, _Budget(self._settings.max_retries)
                    )
                    async with aclosing(single) as replies:
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
        self, job: Job, batch: Sequence[Segment], context: str, data_url: str | None, budget: _Budget
    ) -> AsyncGenerator[tuple[Segment, str]]:
        """发一次流式请求，逐个产出 (段, 译文)。data_url 不为 None 时请求附带这张截图。

        HTTP 层的重试次数取 budget 的剩余额度，发出的每个请求都记入 budget。调用前 budget 必须还能发出请求。

        抛出 EngineError：请求失败、首 token 超时、流中的 error 对象、chunk 无法解析；
        抛出 _StreamBroken：首 token 之后的传输异常或空闲超时，以及首 token 之前的非超时传输异常。
        流正常结束但缺段时正常返回，由调用方核对。

        附带截图的请求失败时，错误详情去掉回显的图片数据；服务表示不支持图片输入时，
        错误码改为 IMAGE_UNSUPPORTED。
        """
        if data_url is None:
            async with aclosing(self._stream(job, batch, context, None, budget)) as replies:
                async for item in replies:
                    yield item
            return
        try:
            async with aclosing(self._stream(job, batch, context, data_url, budget)) as replies:
                async for item in replies:
                    yield item
        except _StreamBroken as exc:
            raise _StreamBroken(exc.code, _scrub_image_data(exc.detail), status=exc.status) from None
        except EngineError as exc:
            detail = _scrub_image_data(exc.detail)
            if _rejects_image(exc, local=self._settings.is_local):
                raise EngineError(
                    ErrorCode.IMAGE_UNSUPPORTED, f"模型不支持图片输入（{detail}）", status=exc.status
                ) from None
            raise EngineError(exc.code, detail, status=exc.status) from None

    async def _stream(
        self, job: Job, batch: Sequence[Segment], context: str, data_url: str | None, budget: _Budget
    ) -> AsyncGenerator[tuple[Segment, str]]:
        """_request 的实际实现：构造请求、发送并解析流式响应。"""
        body = self._request_body(job, batch, context, data_url)
        first_token_s = self.first_token_timeout_s
        # httpx 的 read 时限同时约束等待响应头的时间，所以不能小于首 token 时限。
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_S,
            read=max(self._idle_timeout_s, first_token_s),
            write=WRITE_TIMEOUT_S,
            pool=POOL_TIMEOUT_S,
        )
        loop = asyncio.get_running_loop()
        logger.debug(
            "实例 %s 发送请求：%d 段，首 token 时限 %g 秒%s",
            self._info.id,
            len(batch),
            first_token_s,
            _image_note(job.image) if data_url is not None else "",
        )

        sent_at = loop.time()

        def build() -> httpx.Request:
            # 每次（重）试都从发出请求的这一刻重新计算首 token 时限，退避等待不计入。
            nonlocal sent_at
            sent_at = loop.time()
            budget.sent += 1
            return self._client.build_request(
                "POST", self._endpoint, json=body, headers=self._headers, timeout=timeout
            )

        try:
            # send_timeout 只约束单次发送到收到响应头为止，重试前的退避等待在时限之外。
            response = await send_with_retry(
                self._client,
                build,
                policy=replace(self._policy, max_retries=budget.retries_left),
                stream=True,
                sleep=self._sleep,
                send_timeout=first_token_s,
            )
        except TimeoutError:
            raise self._first_token_timeout(first_token_s) from None
        # 从这里到下面的 try 之间没有 await，取消与超时都不会在此处打断，响应一定由 finally 关闭。
        first_token_deadline = sent_at + first_token_s

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

    def _request_body(
        self, job: Job, batch: Sequence[Segment], context: str, data_url: str | None
    ) -> dict[str, object]:
        messages = build_messages(
            self._system_prompt,
            context,
            [segment.text for segment in batch],
            job.source_lang,
            job.target_lang,
        )
        body: dict[str, object] = {
            "model": self._settings.model,
            "messages": messages if data_url is None else with_image(messages, data_url),
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

        原文非空而译文去掉空白后为空的项不合格，该段按缺段处理；序号不记入 seen，之后同序号的非空项仍可接受。

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
        segment, translated = batch[index - 1], text.strip()
        if not translated and segment.text.strip():
            # 原文非空而译文为空：模型漏译，按缺段处理（逐段退回或记为 BAD_RESPONSE），不当作成功。
            logger.warning("实例 %s 的输出项 %d 的译文为空，按缺段处理", self._info.id, index)
            return None
        seen.add(index)
        return segment, translated

    def _first_token_timeout(self, limit: float) -> EngineError:
        # 本地服务首 token 超时，多半是模型被卸载后需要重新加载（Ollama 默认空闲 5 分钟卸载），
        # 下一个请求恢复使用含模型加载的时限。
        if self._settings.is_local:
            self._warm = False
        return EngineError(ErrorCode.TIMEOUT, f"首 token 超时（{limit:g} 秒）")


def _data_url(image: JobImage) -> str:
    return f"data:{image.media_type};base64,{base64.b64encode(image.data).decode('ascii')}"


def _image_note(image: JobImage | None) -> str:
    """调试日志里描述截图的文字：只有尺寸和字节数。"""
    if image is None:
        return ""
    return f"，附带截图 {image.width}×{image.height}（{len(image.data)} 字节）"


def _scrub_image_data(detail: str) -> str:
    """去掉错误详情里回显的图片数据（data URL、长 base64 片段）。"""
    return _IMAGE_DATA_RE.sub(_IMAGE_DATA_PLACEHOLDER, detail)


def _rejects_image(exc: EngineError, *, local: bool) -> bool:
    """附带截图的请求失败时，判断是不是服务不接受图片输入。

    条件是错误消息含 _IMAGE_HINTS 中的词，并且错误属于“请求被拒绝”一类：

    - HTTP 4xx 中的 BAD_REQUEST，以及流中 error 对象的 BAD_RESPONSE；
      401、403、402、429 各有自己的含义，不改判；
    - 本地服务（Ollama、llama.cpp）的 5xx，它们对不支持的图片输入返回 500；
    - 流中 error 对象按状态码归为 SERVER 的情况（没有 HTTP 状态码）。
    """
    if exc.code is ErrorCode.BAD_REQUEST or exc.code is ErrorCode.BAD_RESPONSE:
        candidate = True
    elif exc.code is ErrorCode.SERVER:
        candidate = local or exc.status is None
    else:
        candidate = False
    detail = exc.detail.lower()
    return candidate and any(hint in detail for hint in _IMAGE_HINTS)


def create_openai_compat_engine(
    settings: OpenAICompatSettings,
    secrets: SecretReader,
    client: httpx.AsyncClient,
    *,
    sleep: Sleep = asyncio.sleep,
) -> Engine:
    """创建一个 OpenAI 兼容 LLM 引擎实例。sleep 是重试前的等待函数，测试时注入以免真实等待。"""
    return OpenAICompatEngine(settings, secrets, client, sleep=sleep)
