"""语言代码规范化与语言检测（架构文档 2.6 节，需求 RACE-09、RACE-10、BI-03）。

调度器按字符串比较语言代码，所以全程序统一用 BCP 47 的一个子集：中文区分 zh-Hans 与 zh-Hant，
其他语言只用主语言子标签（en、ja、de）。配置在校验时、检测结果在输出时都经过 normalize_lang。

单段检测（detect）分三步：

1. 文字系统规则：出现假名判为日文，出现谚文判为韩文；
2. 其余文本交给 lingua 高精度模式，候选语言限定为一个可配置的集合；
3. lingua 判为中文时，再按简体专用字与繁体专用字的个数区分 zh-Hans 与 zh-Hant。

没有可翻译文字的文本（has_text 为假：全是数字、符号、表情，或只有一个非汉字字母）检测结果为 None。

一批文本（detect_many，同一张截屏的各段或双语窗口的各段落）在单段检测之后再按上下文修正
拉丁字母文本的结果，规则见 resolve_with_context。
"""

import asyncio
import threading
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

import lingua

from lst.contracts.ids import Lang
from lst.contracts.langs import ZH_HANS, ZH_HANT, normalize_lang

# ---------------------------------------------------------------- 语言代码规范化
# 规则定义在 contracts.langs，主进程与推理进程共用；这里重新导出，保持原有的导入路径可用。

# ---------------------------------------------------------------- 简繁区分

# 高频的简繁对照字，每项左边是简体字、右边是繁体字。只收两边各自专用的字：
# 后（皇后）、里（公里）、干、面、只、系、台等在繁体文本里也作为本字出现，不收。
_HANS_HANT_PAIRS: Final = (
    "这這", "说說", "国國", "们們", "为為", "时時", "会會", "来來", "个個", "对對", "发發", "过過", "还還",
    "么麼", "学學", "开開", "关關", "长長", "门門", "问問", "间間", "见見", "现現", "没沒", "经經", "动動",
    "东東", "车車", "书書", "电電", "话話", "让讓", "认認", "识識", "语語", "请請", "将將", "与與", "进進",
    "实實", "应應", "点點", "无無", "从從", "机機", "种種", "样樣", "头頭", "边邊", "两兩", "气氣", "万萬",
    "业業", "写寫", "听聽", "买買", "卖賣", "爱愛", "钱錢", "线線", "网網", "页頁", "数數", "设設", "计計",
    "图圖", "视視", "处處", "务務", "统統", "华華", "历歷", "题題", "给給", "帮幫", "变變", "员員", "张張",
    "条條", "习習", "级級", "难難", "产產", "报報", "场場", "总總", "结結", "规規", "则則", "选選", "输輸",
    "运運", "软軟", "码碼", "读讀", "译譯", "词詞", "记記", "论論", "该該", "园園", "节節", "谢謝", "吗嗎",
    "欢歡", "脑腦", "饭飯", "馆館", "马馬", "简簡", "汉漢", "声聲", "乐樂", "亲親", "觉覺", "热熱", "风風",
    "区區", "医醫", "师師", "观觀", "闻聞", "尔爾",
)  # fmt: skip

SIMPLIFIED_ONLY: Final = frozenset(pair[0] for pair in _HANS_HANT_PAIRS)
"""简体专用的高频字。"""

TRADITIONAL_ONLY: Final = frozenset(pair[1] for pair in _HANS_HANT_PAIRS)
"""繁体专用的高频字。"""


def chinese_variant(text: str) -> Lang:
    """按简体专用字与繁体专用字的个数区分简繁：繁体字多时为 zh-Hant，否则为 zh-Hans。"""
    simplified = sum(ch in SIMPLIFIED_ONLY for ch in text)
    traditional = sum(ch in TRADITIONAL_ONLY for ch in text)
    return ZH_HANT if traditional > simplified else ZH_HANS


# ---------------------------------------------------------------- 文字系统

JA: Final = Lang("ja")
KO: Final = Lang("ko")


def _is_kana(cp: int) -> bool:
    """平假名、片假名（含片假名音标扩展、半角片假名）。

    不含片假名中点 U+30FB：中文里有时用它分隔外文人名。
    """
    return (
        0x3041 <= cp <= 0x309F
        or 0x30A1 <= cp <= 0x30FA
        or 0x30FC <= cp <= 0x30FF
        or 0x31F0 <= cp <= 0x31FF
        or 0xFF66 <= cp <= 0xFF9F
    )


def _is_hangul(cp: int) -> bool:
    """谚文音节与字母（含兼容字母、扩展 A/B）。"""
    return (
        0xAC00 <= cp <= 0xD7AF
        or 0x1100 <= cp <= 0x11FF
        or 0x3130 <= cp <= 0x318F
        or 0xA960 <= cp <= 0xA97F
        or 0xD7B0 <= cp <= 0xD7FF
    )


def _is_han(cp: int) -> bool:
    """CJK 统一表意文字（含扩展 A 至扩展区 I）与兼容表意文字。"""
    return (
        0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF or 0x20000 <= cp <= 0x323AF
    )


def _letters(text: str) -> tuple[int, bool, bool]:
    """文本里的字母数、是否含汉字、是否含假名或谚文。"""
    letters = 0
    has_han = False
    has_kana_or_hangul = False
    for ch in text:
        cp = ord(ch)
        if _is_kana(cp) or _is_hangul(cp):
            has_kana_or_hangul = True
        elif _is_han(cp):
            has_han = True
        if ch.isalpha():
            letters += 1
    return letters, has_han, has_kana_or_hangul


def has_text(text: str) -> bool:
    """文本里有没有可翻译的文字。

    没有字母（全是数字、符号、表情，例如“•”“☆4”“2024-10-21”），或者只有一个字母且它不是汉字、假名、
    谚文（例如“v1.2”里的 v）时为假：这类段没有语言信息，翻译它只会原样返回或得到无意义的结果（需求 RACE-09）。
    """
    letters, has_han, has_kana_or_hangul = _letters(text)
    return letters >= 2 or (letters == 1 and (has_han or has_kana_or_hangul))


def _latin_only(text: str) -> bool:
    """文本里的字母全是拉丁字母（含带附加符号的拉丁字母，例如 é、ữ）。"""
    found = False
    for ch in text:
        if ch.isalpha():
            if not unicodedata.name(ch, "").startswith("LATIN"):
                return False
            found = True
    return found


# ---------------------------------------------------------------- 上下文修正

LONG_TEXT_LETTERS: Final = 20
"""字母数达到这个值的拉丁字母文本算“长文本”，它自己的检测结果可信（置信度也足够时）。"""

MIN_CONFIDENCE: Final = 0.5
"""lingua 给出的最高置信度低于这个值时，检测结果不可信。"""

CONTEXT_FALLBACK: Final = Lang("en")
"""一批文本里没有可信的拉丁字母长文本时，短的拉丁字母文本按这个语言处理。"""


@dataclass(frozen=True, slots=True)
class Detection:
    """一段文本的单段检测结果。

    lang 是单段检测出的语言，检测不出时为 None；letters 是字母数；latin 表示字母全是拉丁字母；
    confidence 是 lingua 给出的最高置信度（0 到 1），按文字系统直接判定的结果记为 1。
    """

    lang: Lang | None
    letters: int
    latin: bool
    confidence: float

    @property
    def reliable(self) -> bool:
        """单段结果是否可信：不是拉丁字母文本，或者是字母数够多、置信度够高的拉丁字母文本。"""
        if self.lang is None or not self.latin:
            return True
        return self.letters >= LONG_TEXT_LETTERS and self.confidence >= MIN_CONFIDENCE


def resolve_with_context(detections: Sequence[Detection]) -> list[Lang | None]:
    """按同一批文本的上下文修正拉丁字母短文本的语言。

    拉丁字母写成的语言（英、法、德、西、葡、意、越）只凭一两个词分不开：实测 lingua 把 Star 判成葡萄牙语、
    Code 判成德语、Actions 判成法语、Wiki 判成越南语，置信度都在 0.55 以下。同一张截屏里的界面文字
    通常属于同一种语言，所以规则是：

    - 可信的结果（Detection.reliable）原样保留，包括整段真正的外语长文本；
    - 其余拉丁字母文本（字母数少于 LONG_TEXT_LETTERS，或置信度低于 MIN_CONFIDENCE）取这一批里
      可信的拉丁字母文本的检测结果的众数；票数相同时取先出现的语言；
    - 这一批里没有可信的拉丁字母文本时取 CONTEXT_FALLBACK（英语）。

    投票只在拉丁字母文本之间进行：中文页面上的英文单词不会因为周围是中文就被当成中文。
    检测不出的文本（lang 为 None）保持 None。
    """
    votes = Counter(d.lang for d in detections if d.lang is not None and d.latin and d.reliable)
    context = votes.most_common(1)[0][0] if votes else CONTEXT_FALLBACK
    return [d.lang if d.lang is None or d.reliable else context for d in detections]


# ---------------------------------------------------------------- 检测器

DEFAULT_CANDIDATES: Final = tuple(
    Lang(code) for code in ("zh-Hans", "en", "ja", "ko", "fr", "de", "es", "ru", "pt", "it", "ar", "vi", "th")
)
"""默认的候选语言：中、英、日、韩、法、德、西、俄、葡、意、阿、越、泰。"""


class SupportsLanguageDetection(Protocol):
    """竞速模块使用的语言检测接口。LanguageDetector 是它的实现，测试可以注入假的实现。"""

    async def warm_up(self) -> None:
        """预先完成检测器的构建与初始化，之后的检测不再承担这部分耗时。"""
        ...

    async def detect_many(self, texts: Sequence[str]) -> list[Lang | None]:
        """按顺序返回每段文本的语言，检测不出时为 None。同一批文本互为上下文（resolve_with_context）。
        不阻塞事件循环。"""
        ...


class LanguageDetector:
    """文字系统规则加 lingua 高精度模式的语言检测器（架构文档 2.6 节）。

    languages 是候选语言（规范写法或任何可规范化的写法），lingua 只在其中选择；
    zh-Hans、zh-Hant 都对应 lingua 的中文。候选语言不受 lingua 支持时抛出 ValueError。

    lingua 检测器在第一次使用时构建，之后复用。warm_up 与 detect_many 都在线程池里执行，
    构建与检测都不占用事件循环线程；detect 是同步版本，在调用线程上执行。
    多个线程同时触发构建时只构建一次。
    """

    def __init__(self, languages: Iterable[str] = DEFAULT_CANDIDATES) -> None:
        normalized = frozenset(normalize_lang(code) for code in languages)
        if not normalized:
            raise ValueError("候选语言不能为空")
        lingua_languages: set[lingua.Language] = set()
        for lang in normalized:
            primary = lang.split("-")[0]
            try:
                lingua_languages.add(
                    lingua.Language.from_iso_code_639_1(lingua.IsoCode639_1.from_str(primary))
                )
            except ValueError:
                raise ValueError(f"lingua 不支持候选语言 {lang}") from None
        self._languages = normalized
        self._lingua_languages = frozenset(lingua_languages)
        self._detector: lingua.LanguageDetector | None = None
        self._lock = threading.Lock()

    @property
    def languages(self) -> frozenset[Lang]:
        """规范化后的候选语言。"""
        return self._languages

    @property
    def ready(self) -> bool:
        """lingua 检测器已经构建。"""
        return self._detector is not None

    async def warm_up(self) -> None:
        """在线程池里构建 lingua 检测器，并做一次检测，完成 lingua 首次检测时的一次性初始化。"""
        await asyncio.to_thread(self.detect, "warm up")

    async def detect_many(self, texts: Sequence[str]) -> list[Lang | None]:
        """在线程池里依次检测各段文本，再按上下文修正（resolve_with_context），返回与 texts 等长的列表。"""
        batch = list(texts)
        if not batch:
            return []
        return await asyncio.to_thread(self._detect_batch, batch)

    def detect(self, text: str) -> Lang | None:
        """同步检测一段文本的语言，不看上下文。会阻塞调用线程，事件循环里应当用 detect_many。"""
        return self.analyze(text).lang

    def analyze(self, text: str) -> Detection:
        """单段检测，同时给出上下文修正需要的字母数、文字系统与置信度。会阻塞调用线程。"""
        letters, has_han, _ = _letters(text)
        for ch in text:
            cp = ord(ch)
            if _is_kana(cp):
                return Detection(JA, letters, latin=False, confidence=1.0)
        if any(_is_hangul(ord(ch)) for ch in text):
            return Detection(KO, letters, latin=False, confidence=1.0)
        latin = not has_han and _latin_only(text)
        if not has_text(text):
            return Detection(None, letters, latin, 0.0)
        values = self._ensure_detector().compute_language_confidence_values(text)
        if not values or values[0].value <= 0.0:
            return Detection(None, letters, latin, 0.0)
        top = values[0]
        if top.language == lingua.Language.CHINESE:  # PyO3 枚举每次返回新对象，不能用 is 比较
            return Detection(chinese_variant(text), letters, latin=False, confidence=top.value)
        return Detection(normalize_lang(top.language.iso_code_639_1.name), letters, latin, top.value)

    def _detect_batch(self, texts: list[str]) -> list[Lang | None]:
        return resolve_with_context([self.analyze(text) for text in texts])

    def _ensure_detector(self) -> lingua.LanguageDetector:
        detector = self._detector
        if detector is not None:
            return detector
        with self._lock:
            if self._detector is None:
                self._detector = self._build()
            return self._detector

    def _build(self) -> lingua.LanguageDetector:
        """构建 lingua 检测器：高精度模式（lingua 的默认），候选语言限定为 languages。"""
        return (
            lingua.LanguageDetectorBuilder.from_languages(*self._lingua_languages)
            .with_preloaded_language_models()
            .build()
        )
