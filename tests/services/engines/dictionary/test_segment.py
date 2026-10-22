"""逐词翻译：分词、多词最长匹配、中日文按词条最长匹配切分、空白与标点的保留、命中计数。"""

import pytest

from lst.services.engines.dictionary.pack import DictPack
from lst.services.engines.dictionary.segment import MAX_PHRASE_WORDS, tokenize, translate_text
from tests.services.engines.dictionary.helpers import (
    EN_ZH_ENTRIES,
    EN_ZH_FORMS,
    ZH_EN_ENTRIES,
    make_pack,
)


@pytest.fixture
def en_zh() -> DictPack:
    return make_pack("en", "zh-Hans", EN_ZH_ENTRIES, EN_ZH_FORMS)


@pytest.fixture
def zh_en() -> DictPack:
    return make_pack("zh-Hans", "en", ZH_EN_ENTRIES)


@pytest.fixture
def de_en() -> DictPack:
    return make_pack(
        "de",
        "en",
        {"der": "the", "hund": "dog", "laufen": "walk", "schnell": "quick", "straße": "street"},
        {"läuft": "laufen"},
    )


# ---------------------------------------------------------------- 分词


def test_tokens_concatenate_back_to_the_original() -> None:
    text = "Don't stop—l'homme, 3.14 und  Straße!\n\t中文T恤"
    assert "".join(token.text for token in tokenize(text)) == text


def test_tokenize_keeps_apostrophes_inside_words_and_separates_numbers() -> None:
    kinds = [(token.text, token.kind) for token in tokenize("don't 2024 x")]
    assert kinds == [("don't", "word"), (" ", "space"), ("2024", "number"), (" ", "space"), ("x", "word")]


# ---------------------------------------------------------------- 有空格的源语言


def test_english_to_chinese_drops_spaces_between_chinese_words(en_zh: DictPack) -> None:
    result = translate_text("The cat is sleeping.", en_zh)
    # is → be、sleeping → sleep 经变形行查到原形；句末标点原样保留。
    assert result.text == "那猫是睡眠."
    assert (result.words, result.hits) == (4, 4)


def test_unknown_words_stay_as_they_are_with_their_spaces(en_zh: DictPack) -> None:
    result = translate_text("The zork cat", en_zh)
    assert result.text == "那 zork 猫"
    assert (result.words, result.hits) == (3, 2)


def test_longest_phrase_wins(en_zh: DictPack) -> None:
    # in front of（3 个词）胜过 in front（2 个词）和 in（1 个词）。
    assert translate_text("In front of the house", en_zh).text == "在前面那房子"
    assert translate_text("in front", en_zh).text == "前方"
    assert translate_text("New  York", en_zh).text == "纽约"  # 词间多个空格仍算短语


def test_phrases_do_not_cross_line_breaks_or_punctuation(en_zh: DictPack) -> None:
    assert translate_text("in\nfront of", en_zh).text == "在里\nfront of"
    assert translate_text("in, front of", en_zh).text == "在里, front of"


def test_phrases_are_at_most_three_words(en_zh: DictPack) -> None:
    pack = make_pack("en", "de", {"a b c d": "vier", "a": "eins"})
    assert MAX_PHRASE_WORDS == 3
    assert translate_text("a b c d", pack).text == "eins b c d"
    assert translate_text("a b c d", en_zh).hits == 0


def test_whitespace_and_punctuation_are_kept_for_a_spaced_target(de_en: DictPack) -> None:
    result = translate_text("Der Hund,  läuft!\n\tschnell (Straße)", de_en)
    assert result.text == "the dog,  walk!\n\tquick (street)"


def test_numbers_are_kept_and_not_counted_as_words(en_zh: DictPack) -> None:
    result = translate_text("3 cats, 2024-10-03", en_zh)
    assert result.text == "3 猫, 2024-10-03"
    assert (result.words, result.hits) == (1, 1)
    only_numbers = translate_text("12.5 / 3", en_zh)
    assert (only_numbers.text, only_numbers.words) == ("12.5 / 3", 0)


def test_line_breaks_between_chinese_words_are_kept(en_zh: DictPack) -> None:
    assert translate_text("the\ncat", en_zh).text == "那\n猫"


# ---------------------------------------------------------------- 没有空格的源语言


def test_chinese_forward_maximum_matching(zh_en: DictPack) -> None:
    # 中华人民共和国（7 字）胜过 中华（2 字）；切出的相邻译词之间补一个空格。
    result = translate_text("中华人民共和国万岁", zh_en)
    assert result.text == "PRC long live"
    assert (result.words, result.hits) == (2, 2)
    assert translate_text("中华人民", zh_en).text == "China people"


def test_chinese_punctuation_is_kept(zh_en: DictPack) -> None:
    assert translate_text("你好，世界！", zh_en).text == "hello，world！"
    assert translate_text("我爱你。\n你好", zh_en).text == "I love you。\nhello"


def test_unknown_chinese_characters_stay_and_are_not_spaced_apart(zh_en: DictPack) -> None:
    result = translate_text("我龘龖你", zh_en)
    # 未命中的字原样保留：龘龖 两字之间不补空格，与命中的词之间补空格。
    assert result.text == "I 龘龖 you"
    assert (result.words, result.hits) == (4, 2)


def test_latin_letters_inside_chinese_are_not_split(zh_en: DictPack) -> None:
    # 拉丁字母可以与后面的汉字连成一个 key（T恤），但不会被切成两半去查单个字母。
    assert translate_text("T恤很好", zh_en).text == "T-shirt very good"
    pack = make_pack("zh-Hans", "en", {"a": "x", "好": "good"})
    result = translate_text("abc好", pack)
    assert result.text == "abc good"
    assert (result.words, result.hits) == (2, 1)


def test_japanese_mixes_kanji_and_kana() -> None:
    pack = make_pack("ja", "en", {"猫": "cat", "が": "(subject)", "好き": "like", "好": "good"})
    assert translate_text("猫が好き", pack).text == "cat (subject) like"


def test_chinese_to_japanese_keeps_no_spaces() -> None:
    pack = make_pack("zh-Hans", "ja", {"我": "私", "爱": "愛", "你": "あなた"})
    assert translate_text("我爱你", pack).text == "私愛あなた"


def test_zero_hits_are_reported(zh_en: DictPack, en_zh: DictPack) -> None:
    assert translate_text("龘龖", zh_en).hits == 0
    assert translate_text("Lorem ipsum", en_zh).hits == 0
