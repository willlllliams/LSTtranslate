"""竞速模块：把 RaceScheduler 接到事件总线上（架构文档 4.3、5.3、6.5、11.6 节）。

模块订阅五类消息：

- AppConfig：解析 screenshot、bilingual 两节（旧 [race] 节仅用于配置迁移）；
- EngineRoster：更新调度器的引擎实例集合；
- TranslateText：双语窗口的翻译请求。按段落切段、检测语言、按互译对确定目标语言后开始会话，
  会话只交给请求里选中的那一个服务（需求 BI-03 至 BI-06，RACE-10）；
- CaptureCompleted：取消上一次截屏翻译的会话（CAP-06），保存最近一次截屏的截图，并为接收截图的实例
  在线程池里开始缩小与编码（需求 ENG-07）；
- OcrCompleted：识别完成，用其中的段以同一会话号调用 start_screenshot_session，开始截屏翻译
  （架构文档第 7 章第 6 步）。会话号不是最近一次截屏的会话时直接丢弃（CAP-06）：OCR 模块已经丢弃旧会话的
  迟到结果，这里再判断一次，旧结果不会取消新截屏的会话。没有段的识别结果不开始会话。

附带截图的编码放在本模块，在 CaptureCompleted 到达时就开始：编码与 OCR 识别同时进行，识别完成时
截图通常已经编好，不增加出译文的延迟；需要的尺寸由引擎名册里各实例的 EngineInfo.image_max_side 决定，
这只有竞速模块知道；没有实例接收截图时完全不编码。截图只保留最近一次截屏的一张，新截屏到达时旧的丢弃。

调度器的三类输出经 _BusSink 发布为 SegmentsUpdated、SessionProgress、AttemptRecorded。

语言检测要在线程池里执行，期间同一入口可能又来了新请求。每个入口有一个代号计数，
请求到达时加一，检测完成后代号已经变化的请求直接放弃：同一入口总是最后到达的请求生效，
较早的请求不会因为检测完成得晚而取消较新的会话（RACE-01）。
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from lst.contracts.config import AppConfig
from lst.contracts.events import (
    AttemptRecorded,
    EngineRoster,
    SegmentsUpdated,
    SessionProgress,
    TranslateText,
)
from lst.contracts.ids import Entry, Lang, SegmentId, SessionId
from lst.contracts.ocr import CaptureCompleted, OcrCompleted, RawImage
from lst.contracts.ports import TranslationCache
from lst.contracts.translation import AttemptView, Engine, JobImage, Segment, SegmentView
from lst.kernel import EventBus, ModuleContext
from lst.services.race.config import BilingualConfig, ScreenshotConfig
from lst.services.race.langid import LanguageDetector, SupportsLanguageDetection, has_text, normalize_lang
from lst.services.race.paragraphs import split_paragraphs
from lst.services.race.scheduler import LangPair, RaceScheduler
from lst.services.race.screenshot import encode_screenshot

ImageEncoder = Callable[[RawImage, int], JobImage]
"""截图编码函数：(截图, 最大边长) → 缩小并编码后的图片。在线程池里调用，可以耗时。"""


def bilingual_langs(
    detected: Lang | None, source: Lang | None, target: Lang | None, pair: tuple[Lang, Lang]
) -> LangPair:
    """双语窗口一个段的（源语言，目标语言）（需求 BI-03、RACE-10）。

    - 源语言：手动指定的 source 优先，否则用检测结果 detected。
    - 目标语言：手动指定的 target 优先；否则按互译对，源语言是 pair[0] 时译成 pair[1]，
      是 pair[1] 时译成 pair[0]，两者都不是时译成 pair[0]。
    - 源语言为自动且检测不出时，源语言取 pair[0]。此时目标语言若也是自动，就取源语言本身，
      该段按“源语言与目标语言相同”跳过，界面显示原文（RACE-09）；手动指定了目标语言时照常翻译。
    """
    resolved = source if source is not None else detected
    if resolved is None:
        fallback = pair[0]
        return fallback, target if target is not None else fallback
    if target is not None:
        return resolved, target
    return resolved, pair[1] if resolved == pair[0] else pair[0]


def screenshot_langs(detected: Lang | None, target: Lang) -> LangPair:
    """截屏翻译一个段的（源语言，目标语言）。检测不出时源语言取目标语言，该段按同语言跳过。"""
    return (detected if detected is not None else target), target


class _BusSink:
    """RaceSink 的实现：把调度器的输出发布到事件总线（架构文档 5.3 节）。"""

    __slots__ = ("_bus",)

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def segments_updated(self, session_id: SessionId, views: tuple[SegmentView, ...]) -> None:
        self._bus.publish(SegmentsUpdated(session_id, views))

    def session_progress(self, session_id: SessionId, finished: int, total: int) -> None:
        self._bus.publish(SessionProgress(session_id, finished, total))

    def attempt_recorded(self, session_id: SessionId, segment_id: SegmentId, attempt: AttemptView) -> None:
        self._bus.publish(AttemptRecorded(session_id, segment_id, attempt))


@dataclass(eq=False, slots=True)
class _Capture:
    """最近一次截屏的截图，以及各个最大边长的编码任务。任务的结果为 None 表示编码失败。"""

    session_id: SessionId
    image: RawImage
    encodings: dict[int, asyncio.Task[JobImage | None]]


class RaceModule:
    """竞速模块（实现 kernel 的 Module 协议）。

    cache 是译文缓存端口（RACE-08），detector 是语言检测器，缺省时用默认候选语言的 LanguageDetector；
    clock 与 frame_interval 原样交给 RaceScheduler，缺省时用调度器的默认值。
    image_encoder 是截图编码函数，缺省时用 screenshot.encode_screenshot（PNG）。
    """

    name = "race"

    def __init__(
        self,
        *,
        cache: TranslationCache | None = None,
        detector: SupportsLanguageDetection | None = None,
        clock: Callable[[], float] | None = None,
        frame_interval: float | None = None,
        image_encoder: ImageEncoder | None = None,
    ) -> None:
        self._cache = cache
        self._encoder: ImageEncoder = image_encoder if image_encoder is not None else encode_screenshot
        self._engines: tuple[Engine, ...] = ()
        self._capture: _Capture | None = None
        self._detector: SupportsLanguageDetection = detector if detector is not None else LanguageDetector()
        self._clock = clock if clock is not None else time.monotonic
        self._frame_interval = frame_interval if frame_interval is not None else 1 / 60
        self._logger = logging.getLogger(__name__)
        self._screenshot = ScreenshotConfig()
        self._bilingual_config = BilingualConfig()
        self._scheduler: RaceScheduler | None = None
        self._running = False
        self._generation: dict[Entry, int] = dict.fromkeys(Entry, 0)
        self._warm_up: asyncio.Task[None] | None = None

    # ---- 查询 ----

    @property
    def screenshot_config(self) -> ScreenshotConfig:
        return self._screenshot

    @property
    def bilingual_config(self) -> BilingualConfig:
        return self._bilingual_config

    # ---- 生命周期 ----

    async def start(self, ctx: ModuleContext) -> None:
        """创建调度器并订阅消息，在后台线程里预热语言检测器。

        总线上已有的 AppConfig 与 EngineRoster 先同步应用一次：组合根按顺序启动模块，
        config、engines 先于 race 启动，start 返回时调度器已经拿到当前配置和名册。
        订阅时总线回放的同一个值再应用一次，没有效果。
        """
        if self._scheduler is not None:
            raise RuntimeError("竞速模块已经启动过")
        self._logger = ctx.logger
        self._scheduler = RaceScheduler(
            (),
            _BusSink(ctx.bus),
            cache=self._cache,
            clock=self._clock,
            frame_interval=self._frame_interval,
        )
        self._running = True
        config = ctx.bus.latest(AppConfig)
        if config is not None:
            self._apply_config(config)
        roster = ctx.bus.latest(EngineRoster)
        if roster is not None:
            self._apply_roster(roster)
        ctx.subscribe(AppConfig, self._on_config)
        ctx.subscribe(EngineRoster, self._on_roster)
        ctx.subscribe(TranslateText, self._on_translate)
        ctx.subscribe(CaptureCompleted, self._on_capture)
        ctx.subscribe(OcrCompleted, self._on_ocr_completed)
        self._warm_up = asyncio.create_task(self._warm_up_detector(), name="race-langid-warm-up")

    async def stop(self) -> None:
        """关闭调度器：取消全部会话。之后到达的消息和截屏请求都被忽略。可以重复调用。"""
        if not self._running:
            return
        self._running = False
        for entry in Entry:
            self._generation[entry] += 1
        capture, self._capture = self._capture, None
        if capture is not None:
            # 模块停止后编码结果没有用处。线程里的编码无法中断，取消只是不再等待它，免得任务在
            # 事件循环关闭时仍未结束；正在等待的 start_screenshot_session 把它按没有截图处理后返回。
            for task in capture.encodings.values():
                task.cancel()
        if self._warm_up is not None and not self._warm_up.done():
            # 线程里的构建无法中断，它结束后结果仍然有效；这里只是不再等待它。
            self._warm_up.cancel()
        if self._scheduler is not None:
            await self._scheduler.aclose()

    async def drain(self) -> None:
        """等到调度器没有正在派发、排队或执行的任务，并且最后一次合帧刷新已经完成。供测试使用。"""
        if self._scheduler is not None:
            await self._scheduler.drain()

    # ---- 截屏入口 ----

    async def start_screenshot_session(self, session_id: SessionId, segments: Sequence[Segment]) -> None:
        """开始一个截屏翻译会话（收到 OcrCompleted 时由 _on_ocr_completed 调用）。

        各段的语言按同一张截屏的上下文检测（langid.resolve_with_context），目标语言为 screenshot.target_lang；
        没有可翻译文字的段按“无需翻译”跳过，检测不出的段按同语言跳过。全部可用实例都参加（RACE-02）。
        同一入口的旧会话被取消（CAP-06、RACE-01）。
        会话号与最近一次 CaptureCompleted 相同时，接收截图的实例随任务收到缩小后的截图（ENG-07）；
        语言检测与等待截图编码同时进行。编码失败时这些实例照常翻译，只是不带图。
        检测期间又开始了新的截屏会话时，这次请求作废。模块停止后调用没有效果；
        段号重复等非法参数抛出 ValueError。
        """
        if self._scheduler is None:
            raise RuntimeError("竞速模块尚未启动")
        if not self._running:
            return
        generation = self._advance(Entry.SCREENSHOT)
        batch = tuple(segments)
        target = self._screenshot.target_lang
        detected, images = await asyncio.gather(self._detect(batch), self._screenshot_images(session_id))
        if not self._is_current(Entry.SCREENSHOT, generation):
            return
        langs: dict[SegmentId, LangPair | None] = {
            s.id: screenshot_langs(lang, target) if has_text(s.text) else None
            for s, lang in zip(batch, detected, strict=True)
        }
        await self._scheduler.start_session(
            Entry.SCREENSHOT,
            session_id,
            batch,
            langs,
            context=_context(batch),
            images=images,
        )

    # ---- 消息处理 ----

    def _on_config(self, config: AppConfig) -> None:
        if self._running:
            self._apply_config(config)

    def _on_roster(self, roster: EngineRoster) -> None:
        if self._running:
            self._apply_roster(roster)

    def _on_capture(self, event: CaptureCompleted) -> None:
        """新的截屏完成：取消上一次截屏翻译的会话（CAP-06），保存截图，为名册里接收截图的实例开始编码。

        之前保存的截图丢弃，但未完成的编码不取消（见 _drop_capture）。上一次截屏的会话此时已经没有用处：
        覆盖窗口只显示最新的截屏，继续翻译只会占用引擎和费用，所以在识别新截图的同时就取消它。
        """
        if not self._running:
            return
        if self._scheduler is not None:
            self._scheduler.cancel_session(Entry.SCREENSHOT)
        self._drop_capture()
        capture = _Capture(event.session_id, event.image, {})
        self._capture = capture
        for side in self._image_sides():
            self._encoding(capture, side)

    async def _on_ocr_completed(self, event: OcrCompleted) -> None:
        """识别完成：以同一会话号开始截屏翻译会话（架构文档第 7 章第 6 步）。

        最近一次截屏不是这个会话时丢弃结果（CAP-06）；没有收到过截屏（例如模块在截屏之后才启动）时照常开始。
        没有段时不开始会话，覆盖窗口显示“没有识别到文字”。参数非法（段号重复）只记日志，不让异常传到总线。
        """
        if not self._running:
            return
        capture = self._capture
        if capture is not None and capture.session_id != event.session_id:
            self._logger.info("会话 %s 的识别结果晚于新的截屏，已丢弃", event.session_id)
            return
        if not event.segments:
            self._logger.info("会话 %s 没有识别到文字，不开始竞速", event.session_id)
            return
        try:
            await self.start_screenshot_session(event.session_id, [s.segment for s in event.segments])
        except ValueError:
            self._logger.exception("会话 %s 的截屏翻译无法开始", event.session_id)

    async def _on_translate(self, command: TranslateText) -> None:
        """双语窗口的翻译请求（架构文档 11.6 节）：开始新会话，只交给请求里选中的服务。"""
        scheduler = self._scheduler
        if scheduler is None or not self._running:
            return
        # 代号必须在第一个 await 之前领取：总线按命令到达的顺序创建处理任务，领取顺序因此与到达顺序一致。
        generation = self._advance(Entry.BILINGUAL)
        segments = split_paragraphs(command.text)
        if not segments:
            scheduler.cancel_session(Entry.BILINGUAL)
            return
        settings = self._bilingual_config
        source = self._manual_lang(command.source, "源语言")
        target = self._manual_lang(command.target, "目标语言")
        detected = await self._detect(segments) if source is None else [None] * len(segments)
        if not self._is_current(Entry.BILINGUAL, generation):
            return
        langs: dict[SegmentId, LangPair | None] = {
            s.id: bilingual_langs(lang, source, target, settings.pair) if has_text(s.text) else None
            for s, lang in zip(segments, detected, strict=True)
        }
        engines = () if command.engine_id is None else (command.engine_id,)
        await scheduler.start_session(
            Entry.BILINGUAL,
            command.session_id,
            segments,
            langs,
            engines=engines,
            context=_context(segments),
        )

    # ---- 内部 ----

    def _apply_config(self, config: AppConfig) -> None:
        self._screenshot = self._parse(config, "screenshot", ScreenshotConfig, self._screenshot)
        self._bilingual_config = self._parse(config, "bilingual", BilingualConfig, self._bilingual_config)

    def _parse[T: BaseModel](self, config: AppConfig, name: str, model: type[T], previous: T) -> T:
        """解析一节配置：缺失时用默认值。配置服务已经校验过，解析失败说明组合根没有注册这一节的模型，
        这时记录错误并沿用当前值。"""
        raw = config.section(name)
        if raw is None:
            return model()
        try:
            return model.model_validate(raw)
        except ValidationError:
            self._logger.exception("配置节 [%s] 无法解析，沿用当前值", name)
            return previous

    def _apply_roster(self, roster: EngineRoster) -> None:
        assert self._scheduler is not None
        try:
            self._scheduler.set_engines(roster.engines)
        except ValueError:
            self._logger.exception("引擎名册无效，调度器沿用原来的实例集合")
            return
        # 调度器接受之后才更新：截图要编码的尺寸必须与调度器实际使用的实例一致。
        self._engines = roster.engines

    def _manual_lang(self, code: Lang | None, what: str) -> Lang | None:
        """手动指定的语言规范化后使用；非法时记录警告，按“自动”处理。"""
        if code is None:
            return None
        try:
            return normalize_lang(code)
        except ValueError:
            self._logger.warning("TranslateText 的%s %r 不是合法的语言代码，按自动处理", what, code)
            return None

    async def _detect(self, segments: Sequence[Segment]) -> list[Lang | None]:
        """检测各段的语言。检测器出错时记录日志，全部按检测不出处理，不让翻译因此失败。"""
        if not segments:
            return []
        try:
            detected = await self._detector.detect_many([s.text for s in segments])
        except Exception:
            self._logger.exception("语言检测失败，%d 个段按检测不出处理", len(segments))
            return [None] * len(segments)
        if len(detected) != len(segments):
            self._logger.error(
                "语言检测返回 %d 个结果，应为 %d 个，全部按检测不出处理", len(detected), len(segments)
            )
            return [None] * len(segments)
        return detected

    # ---- 截图 ----

    def _image_sides(self) -> list[int]:
        """名册里接收截图的实例要求的最大边长，去重后从小到大排列。"""
        sides: set[int] = set()
        for engine in self._engines:
            side = engine.info.image_max_side
            if side is not None:
                sides.add(side)
        return sorted(sides)

    def _encoding(self, capture: _Capture, side: int) -> asyncio.Task[JobImage | None]:
        """取该尺寸的编码任务，没有（或被取消过）时新建一个。"""
        task = capture.encodings.get(side)
        if task is None or task.cancelled():
            task = asyncio.create_task(
                self._encode(capture.session_id, capture.image, side), name=f"race-image-{side}"
            )
            capture.encodings[side] = task
        return task

    async def _encode(self, session_id: SessionId, image: RawImage, side: int) -> JobImage | None:
        """在线程池里缩小并编码截图。失败时记录日志（不含图片内容）并返回 None。"""
        started = time.perf_counter()
        try:
            encoded = await asyncio.to_thread(self._encoder, image, side)
        except Exception:
            self._logger.exception("会话 %s 的截图编码失败（最大边长 %d），本次不附带截图", session_id, side)
            return None
        self._logger.debug(
            "会话 %s 的截图编码完成：%d×%d → %d×%d，%d 字节，用时 %.1f 毫秒",
            session_id,
            image.width,
            image.height,
            encoded.width,
            encoded.height,
            len(encoded.data),
            (time.perf_counter() - started) * 1000,
        )
        return encoded

    async def _screenshot_images(self, session_id: SessionId) -> dict[int, JobImage]:
        """截屏会话要附带的截图，按最大边长索引。会话没有对应的截图或没有实例接收截图时为空。

        用 asyncio.wait 等待：调用方被取消时编码任务不受影响，同一会话再次开始（例如重新识别）时直接复用；
        编码任务本身被取消（只会出自事件循环关闭）时，该尺寸按没有截图处理，取消不传给调用方。
        """
        capture = self._capture
        if capture is None or capture.session_id != session_id:
            return {}
        sides = self._image_sides()
        if not sides:
            return {}
        tasks = [self._encoding(capture, side) for side in sides]
        await asyncio.wait(tasks)
        images: dict[int, JobImage] = {}
        for side, task in zip(sides, tasks, strict=True):
            image = None if task.cancelled() else task.result()
            if image is not None:
                images[side] = image
        return images

    def _drop_capture(self) -> None:
        """新截屏到达时丢弃上一次的截图。

        编码任务不取消：正在等待它的 start_screenshot_session 属于上一次截屏，仍应带上那张截图。
        丢弃引用后，任务结束时结果随之释放；_encode 捕获全部异常，任务不会留下未取回的异常。
        """
        self._capture = None

    def _advance(self, entry: Entry) -> int:
        self._generation[entry] += 1
        return self._generation[entry]

    def _is_current(self, entry: Entry, generation: int) -> bool:
        return self._running and self._generation[entry] == generation

    async def _warm_up_detector(self) -> None:
        started = time.perf_counter()
        try:
            await self._detector.warm_up()
        except Exception:
            self._logger.exception("语言检测器预热失败，首次检测时再构建")
        else:
            self._logger.debug("语言检测器预热完成，用时 %.1f 毫秒", (time.perf_counter() - started) * 1000)


def _context(segments: Sequence[Segment]) -> str:
    """会话原文，供 LLM 引擎保持术语一致（ENG-04）。"""
    return "\n".join(s.text for s in segments)
