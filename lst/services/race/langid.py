"""语言代码规范化与语言检测（架构文档 2.6 节，需求 RACE-09、RACE-10、BI-03）。

调度器按字符串比较语言代码，所以全程序统一用 BCP 47 的一个子集：中文区分 zh-Hans 与 zh-Hant，
其他语言只用主语言子标签（en、ja、de）。配置在校验时、检测结果在输出时都经过 normalize_lang。

检测分三步：

1. 文字系统规则：出现假名判为日文，出现谚文判为韩文；
2. 其余文本交给 lingua 高精度模式，候选语言限定为一个可配置的集合；
3. lingua 判为中文时，再按简体专用字与繁体专用字的个数区分 zh-Hans 与 zh-Hant。

没有任何字母（全是数字、符号、表情）或只有一个非汉字字母的文本，检测结果为 None。
"""

import asyncio
import threading
from collections.abc import Iterable, Sequence
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
        """按顺序返回每段文本的语言，检测不出时为 None。不阻塞事件循环。"""
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
        """在线程池里依次检测各段文本，返回与 texts 等长的列表。"""
        batch = list(texts)
        if not batch:
            return []
        return await asyncio.to_thread(self._detect_batch, batch)

    def detect(self, text: str) -> Lang | None:
        """同步检测一段文本的语言。会阻塞调用线程，事件循环里应当用 detect_many。"""
        letters = 0
        has_han = False
        has_hangul = False
        for ch in text:
            cp = ord(ch)
            if _is_kana(cp):
                return JA
            if _is_hangul(cp):
                has_hangul = True
            elif _is_han(cp):
                has_han = True
            if ch.isalpha():
                letters += 1
        if has_hangul:
            return KO
        # 没有字母，或只有一个非汉字字母（例如 “v1.2” 里的 v）：没有可供判断的语言信息。
        if letters == 0 or (letters == 1 and not has_han):
            return None
        language = self._ensure_detector().detect_language_of(text)
        if language is None:
            return None
        if language == lingua.Language.CHINESE:  # PyO3 枚举每次返回新对象，不能用 is 比较
            return chinese_variant(text)
        return normalize_lang(language.iso_code_639_1.name)

    def _detect_batch(self, texts: list[str]) -> list[Lang | None]:
        return [self.detect(text) for text in texts]

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
