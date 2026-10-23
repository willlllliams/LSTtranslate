"""英文与中日文的互补识别：文字完整性、去重、计数和误识别边界。"""

from collections.abc import Sequence

from lst.workers.ocr_worker.winocr_backend import Word, assemble_line, fuse_languages


def text(rows: Sequence[Sequence[Word]]) -> list[str]:
    return [assembled[0] for row in rows if (assembled := assemble_line(row)) is not None]


def word(value: str, x: float, y: float = 10, width: float = 20, height: float = 20) -> Word:
    return Word(value, x, y, width, height)


def test_english_words_do_not_acquire_a_chinese_character_inside_a_word() -> None:
    english = [[word("Hello", 10, width=80)], [word("屏", 10, y=50), word("幕", 32, y=50)]]
    native = [[word("H", 10), word("刨", 32), word("0", 54)], [word("屏", 10, y=50), word("幕", 32, y=50)]]
    assert text(fuse_languages(english, native)) == ["Hello", "屏幕"]


def test_small_dot_misread_as_a_chinese_character_keeps_the_english_number() -> None:
    english = [[word("10.", 10, width=35), word("Translate", 50, width=100)]]
    native = [[word("10", 10), word("庵", 40, y=26, width=4, height=4), word("Translate", 50, width=100)]]
    assert text(fuse_languages(english, native)) == ["10. Translate"]


def test_a_chinese_header_is_kept_on_an_english_page() -> None:
    english = [[word("The team is working hard.", 10, y=50, width=400)]]
    native = [
        [word("屏", 10), word("幕", 32), word("翻", 54), word("译", 76)],
        [word("The team is working hard.", 10, y=50, width=400)],
    ]
    assert text(fuse_languages(english, native)) == ["屏幕翻译", "The team is working hard."]


def test_mixed_line_has_one_copy_of_its_english_part() -> None:
    english = [[word("Screen", 100, width=80), word("translation", 190, width=140)]]
    native = [
        [word("屏", 10), word("幕", 32), word("Screen", 100, width=80), word("translation", 190, width=140)]
    ]
    assert text(fuse_languages(english, native)) == ["屏幕 Screen translation"]


def test_partial_native_line_does_not_delete_other_english_words() -> None:
    english = [[word("Name", 10, width=60), word("RF", 90, width=40), word("Account", 160, width=100)]]
    native = [[word("用", 90), word("户", 112)]]
    assert text(fuse_languages(english, native)) == ["Name 用户 Account"]


def test_punctuation_inside_a_selected_native_line_is_not_duplicated() -> None:
    english = [[word("-", 60, y=25, width=8, height=3)]]
    native = [[word("雲", 10), word("端", 32), word("-", 60, y=25, width=8, height=3)]]
    assert text(fuse_languages(english, native)) == ["雲端 -"]


def test_one_chinese_character_is_preserved_with_script_context_and_word_spacing() -> None:
    english = [[word("Firefox", 10, width=70), word("RF", 90, width=40), word("account", 160, width=80)]]
    native = [
        [word("Firefox", 10, width=70), word("用", 90), word("account", 160, width=80)],
        [word("工", 10, y=50), word("具", 32, y=50)],
    ]
    assert text(fuse_languages(english, native)) == ["Firefox 用 account", "工具"]


def test_isolated_chinese_character_without_an_english_match_is_kept() -> None:
    assert text(fuse_languages([], [[word("中", 10)]])) == ["中"]


def test_repeated_chinese_with_an_adjacent_counter_is_kept() -> None:
    english = [[word("(100)", 72, width=50)]]
    native = [[word("哈", 10), word("哈", 31), word("哈", 52), word("（100）", 73, width=50)]]
    assert text(fuse_languages(english, native)) == ["哈哈哈（100）"]


def test_distinct_chinese_text_with_a_distant_counter_is_kept() -> None:
    english = [[word("(100)", 160, width=50)]]
    native = [[word("用", 10), word("户", 32), word("（100）", 160, width=50)]]
    assert text(fuse_languages(english, native)) == ["用户（100）"]


def test_star_shapes_with_a_distant_counter_keep_the_english_count() -> None:
    english = [[word("(32)", 160, width=50)]]
    native = [[word("女", 10), word("女", 32), word("女", 54), word("（32）", 160, width=50)]]
    assert text(fuse_languages(english, native)) == ["(32)"]


def test_full_width_count_matches_without_losing_digits() -> None:
    english = [[word("(32)", 160, width=50)]]
    native = [[word("女", 10), word("女", 32), word("女", 54), word("（３２）", 160, width=50)]]
    assert text(fuse_languages(english, native)) == ["(32)"]


def test_a_partial_counter_is_not_treated_as_agreement() -> None:
    english = [[word("(3)", 160, width=50)]]
    native = [[word("哈", 10), word("哈", 32), word("哈", 54), word("（23）", 160, width=50)]]
    assert text(fuse_languages(english, native)) == ["哈哈哈（23）"]


def test_a_single_symbol_misread_as_chinese_does_not_replace_an_english_list_number() -> None:
    assert text(fuse_languages([[word("2.", 10)]], [[word("艺", 10)]])) == ["2."]


def test_japanese_kana_and_english_are_kept_once() -> None:
    english = [[word("Settings", 100, width=100)]]
    native = [[word("せ", 10), word("っ", 32), word("Settings", 100, width=100)]]
    assert text(fuse_languages(english, native)) == ["せっ Settings"]


def test_empty_and_whitespace_rows_do_not_change_blank_results() -> None:
    assert fuse_languages([[], [word(" ", 10)]], [[]]) == []
