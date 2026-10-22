"""竞速翻译：合并器、调度器、语言检测与竞速模块（架构文档 2.6 节、第 6 章）。"""

from lst.services.race.config import (
    CONFIG_SECTIONS,
    REMOVED_FIELDS,
    BilingualConfig,
    LangCode,
    RaceConfig,
    ScreenshotConfig,
    SourceMarkerConfig,
)
from lst.services.race.langid import (
    DEFAULT_CANDIDATES,
    Detection,
    LanguageDetector,
    SupportsLanguageDetection,
    chinese_variant,
    has_text,
    normalize_lang,
    resolve_with_context,
)
from lst.services.race.merger import SegmentRace, SessionRace
from lst.services.race.module import RaceModule, bilingual_langs, screenshot_langs
from lst.services.race.paragraphs import split_paragraphs
from lst.services.race.scheduler import (
    LangPair,
    RaceScheduler,
    RaceSink,
    pack_segments,
)

__all__ = [
    "CONFIG_SECTIONS",
    "DEFAULT_CANDIDATES",
    "REMOVED_FIELDS",
    "BilingualConfig",
    "Detection",
    "LangCode",
    "LangPair",
    "LanguageDetector",
    "RaceConfig",
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
    "has_text",
    "normalize_lang",
    "pack_segments",
    "resolve_with_context",
    "screenshot_langs",
    "split_paragraphs",
]
