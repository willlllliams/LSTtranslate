"""竞速模块：把 RaceScheduler 接到事件总线上（架构文档 4.3、5.3、6.5、11.6 节）。

模块订阅三类消息：

- AppConfig：解析 race、screenshot、bilingual 三节，竞速模式变化时切换调度器的模式；
- EngineRoster：更新调度器的引擎实例集合；
- TranslateText：双语窗口的翻译请求。按段落切段、检测语言、按互译对确定目标语言后开始会话；
  Ctrl+Enter 且文本未变时，让计费实例加入当前会话（需求 BI-03 至 BI-06，RACE-10）。

截屏翻译的入口是 start_screenshot_session，由 M4 在收到 OcrCompleted 时调用。
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
from lst.contracts.ports import TranslationCache
from lst.contracts.translation import AttemptView, Segment, SegmentView
from lst.kernel import EventBus, ModuleContext
from lst.services.race.config import BilingualConfig, RaceConfig, ScreenshotConfig
from lst.services.race.langid import LanguageDetector, SupportsLanguageDetection, normalize_lang
from lst.services.race.paragraphs import split_paragraphs
from lst.services.race.scheduler import LangPair, RaceMode, RaceScheduler


def bilingual_langs(
    detected: Lang | None, source: Lang | None, target: Lang | None, pair: tuple[Lang, Lang]
) -> LangPair:
    """双语窗口一个段的（源语言，目标语言）（需求 BI-03、RACE-10）。

    - 源语言：手动指定的 source 优先，否则用检测结果 detected。
    - 目标语言：手动指定的 target 优先；否则按互译对，源语言是 pair[0] 时译成 pair[1]，
      是 pair[1] 时译成 pair[0]，两者都不是时译成 pair[0]。
    - 源语言为自动且检测不出时，源语言取 pair[0]。此时目标语言若也是自动，就取源语言本身，
      该段按“源语言与目标语言相同”跳过竞速，界面显示原文（RACE-09）；手动指定了目标语言时照常翻译。
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


@dataclass(frozen=True, slots=True)
class _BilingualSession:
    """双语窗口的当前会话：会话号、各段文本与各段语言对，用来判断 Ctrl+Enter 时文本是否未变。"""

    session_id: SessionId
    texts: tuple[str, ...]
    langs: tuple[LangPair, ...]


class RaceModule:
    """竞速模块（实现 kernel 的 Module 协议）。

    cache 是译文缓存端口（RACE-08），detector 是语言检测器，缺省时用默认候选语言的 LanguageDetector；
    clock 与 frame_interval 原样交给 RaceScheduler，缺省时用调度器的默认值。
    """

    name = "race"

    def __init__(
        self,
        *,
        cache: TranslationCache | None = None,
        detector: SupportsLanguageDetection | None = None,
        clock: Callable[[], float] | None = None,
        frame_interval: float | None = None,
    ) -> None:
        self._cache = cache
        self._detector: SupportsLanguageDetection = detector if detector is not None else LanguageDetector()
        self._clock = clock if clock is not None else time.monotonic
        self._frame_interval = frame_interval if frame_interval is not None else 1 / 60
        self._logger = logging.getLogger(__name__)
        self._race = RaceConfig()
        self._screenshot = ScreenshotConfig()
        self._bilingual_config = BilingualConfig()
        self._scheduler: RaceScheduler | None = None
        self._running = False
        self._generation: dict[Entry, int] = dict.fromkeys(Entry, 0)
        self._bilingual: _BilingualSession | None = None
        self._warm_up: asyncio.Task[None] | None = None

    # ---- 查询 ----

    @property
    def mode(self) -> RaceMode:
        """调度器当前的竞速模式；模块启动前为配置中的模式。"""
        return self._scheduler.mode if self._scheduler is not None else self._race.mode

    @property
    def race_config(self) -> RaceConfig:
        return self._race

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
            mode=self._race.mode,
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
        self._warm_up = asyncio.create_task(self._warm_up_detector(), name="race-langid-warm-up")

    async def stop(self) -> None:
        """关闭调度器：取消全部会话。之后到达的消息和截屏请求都被忽略。可以重复调用。"""
        if not self._running:
            return
        self._running = False
        for entry in Entry:
            self._generation[entry] += 1
        self._bilingual = None
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
        """开始一个截屏翻译会话（M4 在收到 OcrCompleted 时调用）。

        每段检测语言，目标语言为 screenshot.target_lang，检测不出的段按同语言跳过；
        计费实例直接参赛。同一入口的旧会话被取消（CAP-06、RACE-01）。
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
        detected = await self._detect(batch)
        if not self._is_current(Entry.SCREENSHOT, generation):
            return
        langs = {s.id: screenshot_langs(lang, target) for s, lang in zip(batch, detected, strict=True)}
        await self._scheduler.start_session(
            Entry.SCREENSHOT, session_id, batch, langs, include_billing=True, context=_context(batch)
        )

    # ---- 消息处理 ----

    def _on_config(self, config: AppConfig) -> None:
        if self._running:
            self._apply_config(config)

    def _on_roster(self, roster: EngineRoster) -> None:
        if self._running:
            self._apply_roster(roster)

    async def _on_translate(self, command: TranslateText) -> None:
        """双语窗口的翻译请求（架构文档 11.6 节）。"""
        scheduler = self._scheduler
        if scheduler is None or not self._running:
            return
        # 代号必须在第一个 await 之前领取：总线按命令到达的顺序创建处理任务，领取顺序因此与到达顺序一致。
        generation = self._advance(Entry.BILINGUAL)
        segments = split_paragraphs(command.text)
        if not segments:
            scheduler.cancel_session(Entry.BILINGUAL)
            self._bilingual = None
            return
        settings = self._bilingual_config
        source = self._manual_lang(command.source, "源语言")
        target = self._manual_lang(command.target, "目标语言")
        detected = await self._detect(segments) if source is None else [None] * len(segments)
        if not self._is_current(Entry.BILINGUAL, generation):
            return
        texts = tuple(s.text for s in segments)
        langs = tuple(bilingual_langs(lang, source, target, settings.pair) for lang in detected)

        current = self._bilingual
        if (
            command.include_billing
            and current is not None
            and command.session_id == current.session_id
            and (current.texts, current.langs) == (texts, langs)
        ):
            # Ctrl+Enter 且窗口沿用了会话号：计费实例加入当前会话（6.3 节“晚加入”）。
            # 会话号相同而切段结果不同只会出自窗口的缺陷，此时按新会话处理，结果至少与输入一致。
            await scheduler.extend_session(current.session_id)
            return

        new = _BilingualSession(command.session_id, texts, langs)
        previous, self._bilingual = self._bilingual, new
        try:
            await scheduler.start_session(
                Entry.BILINGUAL,
                command.session_id,
                segments,
                {s.id: pair for s, pair in zip(segments, langs, strict=True)},
                include_billing=command.include_billing or settings.auto_billing,
                context=_context(segments),
            )
        except ValueError:
            # 参数非法时调度器不改动旧会话（例如会话号与截屏会话重复），当前会话的记录也恢复原样。
            if self._bilingual is new:
                self._bilingual = previous
            raise

    # ---- 内部 ----

    def _apply_config(self, config: AppConfig) -> None:
        self._race = self._parse(config, "race", RaceConfig, self._race)
        self._screenshot = self._parse(config, "screenshot", ScreenshotConfig, self._screenshot)
        self._bilingual_config = self._parse(config, "bilingual", BilingualConfig, self._bilingual_config)
        if self._scheduler is not None and self._scheduler.mode is not self._race.mode:
            self._scheduler.set_mode(self._race.mode)

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
