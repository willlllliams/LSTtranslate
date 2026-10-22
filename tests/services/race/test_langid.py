"""语言代码规范化与语言检测的测试。检测用真实的 lingua，检测器在模块级 fixture 里构建一次后复用。"""

import asyncio
import threading
import time
from collections.abc import Sequence

import lingua
import pytest
from hypothesis import given
from hypothesis import strategies as st

from lst.contracts.ids import Lang
from lst.services.race.langid import (
    CONTEXT_FALLBACK,
    DEFAULT_CANDIDATES,
    LONG_TEXT_LETTERS,
    SIMPLIFIED_ONLY,
    TRADITIONAL_ONLY,
    Detection,
    LanguageDetector,
    SupportsLanguageDetection,
    chinese_variant,
    has_text,
    normalize_lang,
    resolve_with_context,
)

# ---------------------------------------------------------------- 规范化


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        # 简体中文
        ("zh", "zh-Hans"),
        ("zh-CN", "zh-Hans"),
        ("zh-cn", "zh-Hans"),
        ("zh_CN", "zh-Hans"),
        ("zh-hans", "zh-Hans"),
        ("ZH-HANS", "zh-Hans"),
        ("zh-Hans", "zh-Hans"),
        ("zh-SG", "zh-Hans"),
        ("zh-Hans-CN", "zh-Hans"),
        ("zh-Hans-TW", "zh-Hans"),  # 书写系统优先于地区
        ("zh-chs", "zh-Hans"),
        ("zho", "zh-Hans"),
        (" zh ", "zh-Hans"),
        # 繁体中文
        ("zh-TW", "zh-Hant"),
        ("zh-HK", "zh-Hant"),
        ("zh-MO", "zh-Hant"),
        ("zh-hant", "zh-Hant"),
        ("zh-Hant", "zh-Hant"),
        ("zh-Hant-TW", "zh-Hant"),
        ("zh-Hant-CN", "zh-Hant"),
        ("zh_TW", "zh-Hant"),
        ("zh-cht", "zh-Hant"),
        # 其他语言只保留主语言子标签
        ("en", "en"),
        ("EN", "en"),
        ("en-US", "en"),
        ("en_GB", "en"),
        ("pt-BR", "pt"),
        ("PT_br", "pt"),
        ("sr-Latn-RS", "sr"),
        ("es-419", "es"),
        ("ja-JP", "ja"),
        ("fil", "fil"),
        # 三字母码换成两字母码，废弃代码换成现行代码
        ("eng", "en"),
        ("jpn", "ja"),
        ("deu", "de"),
        ("iw", "he"),
        ("in", "id"),
        ("no", "nb"),
    ],
)
def test_normalize(code: str, expected: str) -> None:
    assert normalize_lang(code) == expected


@pytest.mark.parametrize(
    "code",
    [
        "",
        "   ",
        "e",  # 主语言子标签太短
        "english",  # 太长
        "auto",
        "中文",
        "en-",
        "-en",
        "en--US",
        "en US",
        "1en",
        "en-toolongsubtag",
        "zh-Latn",  # 中文只有简繁两种书写系统
        "zh-Cyrl-CN",
        "und",
        "mul",
        "zxx",
        "qaa",
        "qtz-x",
        "\u212aa",  # 开尔文符号 U+212A 小写后是 ASCII 的 k，不能当成合法代码
    ],
)
def test_normalize_rejects_invalid(code: str) -> None:
    with pytest.raises(ValueError, match="语言代码"):
        normalize_lang(code)


_subtag = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=1, max_size=8
)


@given(st.lists(_subtag, min_size=1, max_size=4), st.sampled_from(["-", "_"]))
def test_normalize_is_idempotent(subtags: list[str], separator: str) -> None:
    """能规范化的代码，规范化结果本身是合法代码，再规范化一次不变。"""
    try:
        once = normalize_lang(separator.join(subtags))
    except ValueError:
        return
    assert normalize_lang(once) == once


# ---------------------------------------------------------------- 简繁区分


def test_simplified_and_traditional_sets() -> None:
    assert len(SIMPLIFIED_ONLY) >= 40
    assert len(SIMPLIFIED_ONLY) == len(TRADITIONAL_ONLY)
    assert not SIMPLIFIED_ONLY & TRADITIONAL_ONLY
    # 繁体文本里也作为本字出现的字不收
    assert not {"后", "里", "干", "面", "只", "系", "台"} & SIMPLIFIED_ONLY


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("这是简体中文", "zh-Hans"),
        ("這是繁體中文", "zh-Hant"),
        ("你好", "zh-Hans"),  # 没有区分字时取简体
        ("这個", "zh-Hans"),  # 一样多时取简体
        ("這個说法", "zh-Hant"),  # 繁体多
    ],
)
def test_chinese_variant(text: str, expected: str) -> None:
    assert chinese_variant(text) == expected


# ---------------------------------------------------------------- 真实 lingua 检测


@pytest.fixture(scope="module")
def detector() -> LanguageDetector:
    instance = LanguageDetector()
    instance.detect("warm up")
    return instance


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The weather is lovely today, isn't it?", "en"),
        ("Please restart your computer.", "en"),
        ("今天天气很好，我们一起去公园散步吧。", "zh-Hans"),
        ("这个问题需要进一步讨论。", "zh-Hans"),
        ("今日はとても良い天気ですね。", "ja"),
        ("コンピューターを再起動してください。", "ja"),
        ("오늘은 날씨가 정말 좋네요.", "ko"),
        ("회의는 내일 오후 세 시에 시작합니다.", "ko"),
        ("Je voudrais réserver une table pour deux.", "fr"),
        ("Où se trouve la gare ?", "fr"),
        ("Ich möchte heute Abend ins Kino gehen.", "de"),
        ("Wo ist der Bahnhof?", "de"),
        ("Сегодня очень хорошая погода.", "ru"),
        ("Где находится вокзал?", "ru"),
        ("¿Dónde está la estación de tren?", "es"),
    ],
)
def test_detect_short_sentences(detector: LanguageDetector, text: str, expected: str) -> None:
    assert 10 <= len(text) <= 50
    assert detector.detect(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("今天天气很好，我们一起去公园散步吧。", "zh-Hans"),
        ("今天天氣很好，我們一起去公園散步吧。", "zh-Hant"),
        ("电脑软件的设计与开发", "zh-Hans"),
        ("電腦軟體的設計與開發", "zh-Hant"),
        ("這個問題需要進一步討論。", "zh-Hant"),
        ("谢谢", "zh-Hans"),
        ("謝謝", "zh-Hant"),
    ],
)
def test_detect_simplified_and_traditional(detector: LanguageDetector, text: str, expected: str) -> None:
    assert detector.detect(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "   ", "12345", "2024-10-21 12:30", "!!!???", "😀🎉", "$100.00", "(3)", "a", "v1.2.3"],
)
def test_undetectable_returns_none(detector: LanguageDetector, text: str) -> None:
    """没有字母，或只有一个非汉字字母时检测不出。"""
    assert detector.detect(text) is None


def test_single_han_character_is_detected(detector: LanguageDetector) -> None:
    assert detector.detect("中") == "zh-Hans"


def test_script_rules_apply_before_candidates() -> None:
    """假名和谚文按文字系统直接判定，不受候选语言限制。"""
    restricted = LanguageDetector(["en", "fr"])
    assert restricted.detect("今日は") == "ja"
    assert restricted.detect("감사합니다") == "ko"
    assert restricted.detect("我の世界") == "ja"  # 夹带假名的文本按日文处理


def test_candidates_limit_lingua() -> None:
    restricted = LanguageDetector(["en", "fr"])
    assert restricted.languages == {"en", "fr"}
    assert restricted.detect("Ich möchte heute Abend ins Kino gehen.") in {"en", "fr"}


def test_candidates_are_normalized() -> None:
    assert LanguageDetector(["zh-CN", "zh-TW", "en-US"]).languages == {"zh-Hans", "zh-Hant", "en"}
    assert LanguageDetector().languages == set(DEFAULT_CANDIDATES)
    assert len(DEFAULT_CANDIDATES) == 13


@pytest.mark.parametrize("languages", [[], ["english"], ["en", "xx"], ["en", "yue"]])
def test_invalid_candidates(languages: list[str]) -> None:
    with pytest.raises(ValueError):
        LanguageDetector(languages)


async def test_detect_many_keeps_order(detector: LanguageDetector) -> None:
    texts = [
        "Ich möchte heute Abend ins Kino gehen, wenn es nicht regnet.",
        "12345",
        "這是繁體中文的句子",
        "今日はいい天気",
    ]
    assert await detector.detect_many(texts) == ["de", None, "zh-Hant", "ja"]
    assert await detector.detect_many([]) == []


# ---------------------------------------------------------------- 有没有可翻译的文字


@pytest.mark.parametrize(
    "text", ["•", "☆4", "12345", "2024-10-21", "$100.00", "😀🎉", "", "   ", "a", "v1.2.3"]
)
def test_symbols_and_single_letters_have_no_text(text: str) -> None:
    assert not has_text(text)


@pytest.mark.parametrize("text", ["中", "の", "Hi", "Star", "Python ☆4"])
def test_words_and_single_cjk_characters_have_text(text: str) -> None:
    assert has_text(text)


# ---------------------------------------------------------------- 按上下文修正短文本


GITHUB_PAGE = [
    "Star",
    "Code",
    "Actions",
    "Wiki",
    "Sign in",
    "Python ☆4",
    "GitHub is where people build software and work together on projects.",
    "This repository has been archived by the owner and is now read-only.",
]


async def test_short_words_follow_long_text_of_the_same_page(detector: LanguageDetector) -> None:
    """英文页面上的短词单独检测会判成葡、德、法、越语（实测），按同一截屏里长文本的语言改判为英语。"""
    for word, wrong in (("Star", "pt"), ("Code", "de"), ("Actions", "fr"), ("Wiki", "vi")):
        assert detector.detect(word) == wrong  # 单段检测的误判，正是本规则要修正的
    results = await detector.detect_many(GITHUB_PAGE)
    assert results == ["en"] * len(GITHUB_PAGE)


async def test_long_foreign_paragraph_keeps_its_own_language(detector: LanguageDetector) -> None:
    """整段真正的外语长文本按自己的检测结果，不被页面上的多数语言改写。"""
    german = "Die Bundesregierung hat am Mittwoch neue Regeln für den Datenschutz beschlossen."
    results = await detector.detect_many([*GITHUB_PAGE, german])
    assert results[-1] == "de"
    assert results[:4] == ["en"] * 4


async def test_short_words_follow_majority_of_long_texts(detector: LanguageDetector) -> None:
    """长文本里德语多于英语时，短词取德语。"""
    texts = [
        "Wiki",
        "Die Bundesregierung hat am Mittwoch neue Regeln für den Datenschutz beschlossen.",
        "Der Zug nach Berlin fährt heute wegen Bauarbeiten eine Stunde später ab.",
        "GitHub is where people build software and work together on projects.",
    ]
    assert (await detector.detect_many(texts))[0] == "de"


async def test_short_words_without_long_text_fall_back_to_english(detector: LanguageDetector) -> None:
    assert CONTEXT_FALLBACK == "en"
    assert await detector.detect_many(["Star", "Code", "Actions", "Wiki", "Sign in"]) == ["en"] * 5


async def test_chinese_context_does_not_capture_latin_words(detector: LanguageDetector) -> None:
    """投票只在拉丁字母文本之间进行：中文页面上的英文短词仍按英语处理，中文段照常判为中文。"""
    texts = ["今天天气很好，我们一起去公园散步，顺便看看新开的书店。", "Wiki", "设置"]
    assert await detector.detect_many(texts) == ["zh-Hans", "en", "zh-Hans"]


def test_resolve_with_context_rules() -> None:
    """纯函数规则：可信的结果原样保留；不可信的拉丁字母文本取可信拉丁字母文本的众数，没有时取英语。"""
    long_fr = Detection(Lang("fr"), LONG_TEXT_LETTERS, latin=True, confidence=0.9)
    long_de = Detection(Lang("de"), LONG_TEXT_LETTERS + 5, latin=True, confidence=0.95)
    low_conf_long = Detection(Lang("it"), LONG_TEXT_LETTERS + 10, latin=True, confidence=0.3)
    short = Detection(Lang("pt"), 4, latin=True, confidence=0.9)
    russian = Detection(Lang("ru"), 3, latin=False, confidence=0.6)
    none = Detection(None, 0, latin=False, confidence=0.0)
    assert resolve_with_context([short, long_fr, long_de, long_fr, low_conf_long, russian, none]) == [
        "fr",
        "fr",
        "de",
        "fr",
        "fr",
        "ru",
        None,
    ]
    assert resolve_with_context([short, low_conf_long]) == ["en", "en"]
    assert resolve_with_context([long_de, long_fr, short]) == ["de", "fr", "de"]  # 票数相同取先出现的


async def test_detect_many_does_not_block_event_loop(detector: LanguageDetector) -> None:
    """检测期间一个每 5 毫秒醒来一次的计时协程照常运行。

    同步检测会让事件循环在整个批次期间停住，计时协程一次也醒不来。批次大小按本机速度估算，
    让检测持续约 0.3 秒，远大于 Windows 约 15.6 毫秒的定时器粒度。
    """
    sample = [
        "The weather is lovely today, isn't it?",
        "今天天气很好，我们一起去公园散步吧。",
        "Ich möchte heute Abend ins Kino gehen.",
        "Сегодня очень хорошая погода.",
        "Je voudrais réserver une table pour deux.",
    ]
    started = time.perf_counter()
    for _ in range(40):
        for text in sample:
            detector.detect(text)
    per_text = (time.perf_counter() - started) / (40 * len(sample))
    texts = sample * max(1, min(40_000, int(0.3 / per_text / len(sample))))

    loop = asyncio.get_running_loop()
    ticks: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        while not stop.is_set():
            ticks.append(loop.time())
            await asyncio.sleep(0.005)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)
    begin = loop.time()
    results = await detector.detect_many(texts)
    end = loop.time()
    stop.set()
    await task

    assert results[: len(sample)] == ["en", "zh-Hans", "de", "ru", "fr"]
    inside = [t for t in ticks if begin <= t <= end]
    gaps = [b - a for a, b in zip([begin, *inside], [*inside, end], strict=True)]
    assert end - begin > 0.1
    assert len(inside) >= 3
    assert max(gaps) < 0.1


# ---------------------------------------------------------------- 构建与预热


class CountingDetector(LanguageDetector):
    """记录 lingua 检测器的构建次数与构建所在线程；构建时故意停 50 毫秒，放大并发窗口。"""

    def __init__(self, languages: Sequence[str] = ("en", "fr")) -> None:
        super().__init__(languages)
        self.builds: list[int] = []

    def _build(self) -> lingua.LanguageDetector:
        self.builds.append(threading.get_ident())
        time.sleep(0.05)
        return super()._build()


async def test_detector_is_built_lazily_in_worker_thread() -> None:
    detector = CountingDetector()
    assert not detector.ready
    await detector.warm_up()
    assert detector.ready
    assert len(detector.builds) == 1
    assert detector.builds[0] != threading.get_ident()


async def test_concurrent_first_use_builds_once() -> None:
    detector = CountingDetector()
    results = await asyncio.gather(
        detector.warm_up(),
        detector.detect_many(["Where is the station? I would like to buy a ticket."]),
        detector.detect_many(["Où se trouve la gare ? Je voudrais acheter un billet."]),
    )
    assert results[1:] == [["en"], ["fr"]]
    assert len(detector.builds) == 1


def test_detector_satisfies_protocol() -> None:
    instance: SupportsLanguageDetection = LanguageDetector()
    assert isinstance(instance, LanguageDetector)


def test_detect_returns_canonical_codes(detector: LanguageDetector) -> None:
    """lingua 的结果经过规范化：返回值都是规范写法，与 Lang 比较相等。"""
    result = detector.detect("Please restart your computer.")
    assert result == Lang("en")
    assert result is not None
    assert normalize_lang(result) == result
