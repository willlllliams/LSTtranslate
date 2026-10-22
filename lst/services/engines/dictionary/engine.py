"""字典翻译引擎：离线逐词查词典，毫秒级给出粗译，是竞速里最快的一档。

译文只是逐词替换的结果，不处理语序、冠词和一词多义，所以默认放在优先级最低的位置：它先出结果，
优先级更高的引擎（本地 NMT、在线引擎）的译文到达后替换它。

- supports：词典目录里装有该方向的词典包（直连）。打开“经英语中转”时，两端都不是英语、并且装有
  源语言→英语和英语→目标语言两个包的方向也支持；简体与繁体中文之间不中转，经英语只会得到按中文语序
  排列的英语释义；
- translate：在线程里读入词典包（首次使用或包更新后）并逐段翻译，翻译算法见 segment 模块。
  空白段原样返回；只含数字和标点、没有任何词的段原样返回；有词却一个都查不到的段以 ErrorCode.NO_MATCH
  失败，让其他引擎胜出，而不是把原文当作译文显示。

日志里只出现实例号、段数、字符数和语言对，不出现原文和译文（PRV-02）。
"""

import logging
import time
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Final

from pydantic import Field

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.langs import ZH_HANS, ZH_HANT
from lst.contracts.translation import EngineInfo, EngineKind, Job, Segment, SegmentOutcome
from lst.services._shared.aio import blocking
from lst.services.engines.common import EngineSettings, build_info
from lst.services.engines.dictionary.library import PackLibrary, PackUnavailableError, shared_library
from lst.services.engines.dictionary.pack import DictPack
from lst.services.engines.dictionary.segment import translate_text

logger = logging.getLogger(__name__)

PIVOT: Final = Lang("en")
"""经英语中转时的中间语言。词典包只有英语与其他语言之间的方向。"""

# 引擎侧影响译文的处理方式（分词、最长匹配、拼接规则）变化时加一，让旧译文不再命中缓存。
_FINGERPRINT_VERSION: Final = 1

ERROR_NO_MATCH: Final = "词典里查不到这一段的任何词"
ERROR_EMPTY: Final = "逐词替换后译文为空"

type Route = tuple[tuple[Lang, Lang], ...]


class DictionarySettings(EngineSettings):
    """字典翻译实例的配置。

    - timeout_s 默认 5 秒：翻译本身是毫秒级，时限主要覆盖第一次使用时读入词典包（全量的包约 0.1 到 1 秒）；
    - max_retries 固定为 0：失败只有“没装词典包”“查不到词”两种，重试结果相同；
    - max_batch_segments、max_batch_chars 是一个任务的段数与字符数上限。查词按字符线性增长，
      实测 1000 字的中英文都在几毫秒内译完，所以一批可以放得很大；
    - pivot_via_english：两端都不是英语时经英语中转（例如日语→英语→德语）。两次逐词替换的误差叠加，
      译文质量更差，默认关闭。
    """

    timeout_s: float = Field(default=5.0, gt=0, le=600)
    max_concurrency: int = Field(default=2, ge=1, le=32)
    max_retries: int = Field(default=0, ge=0, le=0)
    max_batch_segments: int = Field(default=64, ge=1, le=1000)
    max_batch_chars: int = Field(default=20_000, ge=1, le=1_000_000)
    pivot_via_english: bool = False


def _is_chinese_variant_pair(source: str, target: str) -> bool:
    return {source, target} == {ZH_HANS, ZH_HANT}


class DictionaryEngine:
    """字典翻译引擎实例。library 是词典目录，同一目录的多个实例共用一份已读入的包。"""

    def __init__(self, settings: DictionarySettings, library: PackLibrary) -> None:
        self._library = library
        self._pivot = settings.pivot_via_english
        self._info = build_info(
            settings,
            kind=EngineKind.LOCAL_NMT,
            billing=False,
            max_batch_segments=settings.max_batch_segments,
            max_batch_chars=settings.max_batch_chars,
            fingerprint_parts=("dictionary", _FINGERPRINT_VERSION, settings.pivot_via_english),
            secret_value=None,
        )

    @property
    def info(self) -> EngineInfo:
        return self._info

    @property
    def library(self) -> PackLibrary:
        return self._library

    def route(self, source: Lang, target: Lang) -> Route | None:
        """翻译这个方向要依次使用的词典包；没有可用的包时返回 None。"""
        if source == target:
            return None
        pairs = self._library.installed_pairs()
        if (source, target) in pairs:
            return ((source, target),)
        if (
            self._pivot
            and PIVOT not in (source, target)
            and not _is_chinese_variant_pair(source, target)
            and (source, PIVOT) in pairs
            and (PIVOT, target) in pairs
        ):
            return ((source, PIVOT), (PIVOT, target))
        return None

    def supports(self, source: Lang, target: Lang) -> bool:
        """词典目录里有这个方向需要的词典包。每次调用都看目录的当前内容，装好或删掉包后立即生效。"""
        return self.route(source, target) is not None

    async def translate(self, job: Job) -> AsyncGenerator[SegmentOutcome]:
        """在线程里读包并翻译整个任务，然后逐段产出结果。"""
        if not job.segments:
            return
        route = self.route(job.source_lang, job.target_lang)
        if route is None:
            detail = f"没有安装 {job.source_lang}→{job.target_lang} 的词典包"
            for segment in job.segments:
                yield SegmentOutcome(segment.id, error=detail, code=ErrorCode.MODEL_MISSING)
            return
        started = time.perf_counter()
        outcomes = await blocking(self._run, job.segments, route)
        logger.debug(
            "字典实例 %s：%s→%s，%d 段，%d 字符，用时 %.1f 毫秒",
            self._info.id,
            job.source_lang,
            job.target_lang,
            len(job.segments),
            sum(len(segment.text) for segment in job.segments),
            (time.perf_counter() - started) * 1000,
        )
        for outcome in outcomes:
            yield outcome

    def _run(self, segments: Sequence[Segment], route: Route) -> list[SegmentOutcome]:
        """在线程里执行：读入需要的包，逐段翻译。"""
        try:
            packs = [self._library.load(source, target) for source, target in route]
        except PackUnavailableError as exc:
            logger.warning("字典实例 %s 无法使用词典包：%s", self._info.id, exc.detail)
            return [SegmentOutcome(s.id, error=exc.detail, code=ErrorCode.MODEL_MISSING) for s in segments]
        return [translate_segment(segment, packs) for segment in segments]


def translate_segment(segment: Segment, packs: Sequence[DictPack]) -> SegmentOutcome:
    """用一个或依次用几个词典包翻译一段。规则见模块说明。"""
    text = segment.text
    if not text.strip():
        return SegmentOutcome(segment.id, text=text)
    current = text
    for pack in packs:
        result = translate_text(current, pack)
        if result.words == 0:
            # 只有数字、标点和空白：没有可查的词，原样就是译文。
            return SegmentOutcome(segment.id, text=current)
        if result.hits == 0:
            return SegmentOutcome(segment.id, error=ERROR_NO_MATCH, code=ErrorCode.NO_MATCH)
        current = result.text
    if not current.strip():
        return SegmentOutcome(segment.id, error=ERROR_EMPTY, code=ErrorCode.NO_MATCH)
    return SegmentOutcome(segment.id, text=current)


def create_dictionary_engine(
    settings: DictionarySettings, dict_dir: Path, *, library: PackLibrary | None = None
) -> DictionaryEngine:
    """创建字典翻译实例。library 缺省时取词典目录在进程里共用的 PackLibrary。"""
    return DictionaryEngine(settings, library if library is not None else shared_library(dict_dir))
