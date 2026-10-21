"""竞速翻译：合并器、调度器、语言检测与竞速模块（架构文档 2.6 节、第 6 章）。"""

from lst.services.race.config import (
    CONFIG_SECTIONS,
    BilingualConfig,
    LangCode,
    RaceConfig,
    ScreenshotConfig,
    SourceMarkerConfig,
)
from lst.services.race.langid import (
    DEFAULT_CANDIDATES,
    LanguageDetector,
    SupportsLanguageDetection,
    chinese_variant,
    normalize_lang,
)
from lst.services.race.merger import SegmentRace, SessionRace
from lst.services.race.module import RaceModule, bilingual_langs, screenshot_langs
from lst.services.race.paragraphs import split_paragraphs
from lst.services.race.scheduler import (
    LangPair,
    RaceMode,
    RaceScheduler,
    RaceSink,
    pack_segments,
)

__all__ = [
    "CONFIG_SECTIONS",
    "DEFAULT_CANDIDATES",
    "BilingualConfig",
    "LangCode",
    "LangPair",
    "LanguageDetector",
    "RaceConfig",
    "RaceMode",
    "RaceModule",
    "RaceScheduler",
    "RaceSink",
    "ScreenshotConfig",
    "SegmentRace",
    "SessionRace",
    "SourceMarkerConfig",
    "SupportsLanguageDetection",
    "bilingual_langs",
    "chinese_variant",
    "normalize_lang",
    "pack_segments",
    "screenshot_langs",
    "split_paragraphs",
]
