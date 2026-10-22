"""词典包格式：key 归一化、包头与词条的解析、查词规则（词条优先，否则经变形查原形）。"""

import gzip
from pathlib import Path

import pytest

from lst.services.engines.dictionary.pack import (
    MAX_UNSPACED_KEY_CHARS,
    PackFormatError,
    base_language,
    decode_pack,
    normalize_key,
    parse_header,
    parse_pack_text,
    read_pack_file,
)
from tests.services.engines.dictionary.helpers import make_pack, pack_bytes, pack_text, write_pack

# ---------------------------------------------------------------- 归一化


@pytest.mark.parametrize(
    ("text", "lang", "expected"),
    [
        # NFC：e 加组合锐音符合成为 é。
        ("Café", "fr", "café"),
        # 首尾空白去掉，内部连续空白（含制表符、换行）合并为一个空格。
        ("  In \t front\n of  ", "en", "in front of"),
        # 土耳其语：I → ı，İ → i；其他语言 I → i。
        ("Istanbul İzmir", "tr", "ıstanbul izmir"),
        ("Istanbul", "en", "istanbul"),
        # 俄语去掉重音符号，ё、й 保留。
        ("Приве́т", "ru", "привет"),
        ("Ёлка й", "ru", "ёлка й"),
        # 其他语言的组合锐音符不去掉。
        ("Pé", "es", "pé"),
        # 汉字没有大小写，只有夹带的拉丁字母变小写；语言码带脚本子标签时按主语言处理。
        ("T恤", "zh-Hans", "t恤"),
        ("ＡＢＣ", "ja", "ａｂｃ"),
    ],
)
def test_normalize_key(text: str, lang: str, expected: str) -> None:
    assert normalize_key(text, lang) == expected


def test_base_language_takes_the_primary_subtag() -> None:
    assert base_language("zh-Hans") == "zh"
    assert base_language("TR") == "tr"


# ---------------------------------------------------------------- 解析


def test_parse_header_reads_six_fields() -> None:
    header = parse_header("#dict\ten\tzh-Hans\t20261011\tECDICT\tMIT\r")
    assert (header.source, header.target, header.version, header.source_name, header.license) == (
        "en",
        "zh-Hans",
        "20261011",
        "ECDICT",
        "MIT",
    )


@pytest.mark.parametrize(
    "line",
    ["w\tcat\t猫", "#dict\ten\tde", "#dict\t\tde\t1\tx\tMIT", "#DICT\ten\tde\t1\tx\tMIT"],
)
def test_parse_header_rejects_other_first_lines(line: str) -> None:
    with pytest.raises(PackFormatError):
        parse_header(line)


def test_parse_pack_reads_entries_and_forms_and_skips_broken_lines() -> None:
    text = pack_text("en", "de", {"go": "gehen", "in front of": "vor"}, {"went": "go"})
    text += "x\tunknown\ttag\nw\tonly-two-fields\n\nw\t\tempty key\nw\tcrlf\tzeile\r\n"
    pack = parse_pack_text(text)
    assert pack.entries == {"go": "gehen", "in front of": "vor", "crlf": "zeile"}
    assert pack.forms == {"went": "go"}
    assert (pack.source, pack.target) == ("en", "de")


def test_longest_from_records_the_longest_key_per_first_character() -> None:
    long_key = "中" * (MAX_UNSPACED_KEY_CHARS + 4)
    pack = make_pack("zh-Hans", "en", {"中": "middle", "中华": "China", long_key: "x", "人民": "people"})
    # 超过上限的 key 按上限计：最长匹配从这么长的片段试起，耗时有上界。
    assert pack.longest_from == {"中": MAX_UNSPACED_KEY_CHARS, "人": 2}


def test_decode_pack_round_trip() -> None:
    pack = decode_pack(pack_bytes("de", "en", {"hund": "dog"}))
    assert pack.lookup("Hund") == "dog"


@pytest.mark.parametrize(
    "data",
    [
        b"not gzip at all",
        gzip.compress(b"#dict\ten\tde\t1\tx\tMIT\nw\t\xff\xfe\tbad\n"),
        gzip.compress(b"w\tcat\tKatze\n"),
        gzip.compress(b"#dict\ten\tde")[:-6],  # 截断的 gzip
    ],
)
def test_decode_pack_rejects_files_that_are_not_packs(data: bytes) -> None:
    with pytest.raises(PackFormatError):
        decode_pack(data)


def test_read_pack_file(tmp_path: Path) -> None:
    path = write_pack(tmp_path, "en", "zh-Hans", {"cat": "猫"})
    assert read_pack_file(path).lookup("CAT") == "猫"


# ---------------------------------------------------------------- 查词


def test_lookup_prefers_the_entry_then_follows_a_form_to_its_lemma() -> None:
    pack = make_pack(
        "en",
        "de",
        {"go": "gehen", "went": "ging", "be": "sein"},
        {"went": "go", "is": "be", "gone": "missing", "been": "is"},
    )
    assert pack.lookup("went") == "ging"  # 词条优先于变形
    assert pack.lookup("Is") == "sein"  # 没有词条时经变形查原形
    assert pack.lookup("gone") is None  # 原形不在包里
    assert pack.lookup("been") is None  # 变形不做链式解析
    assert pack.lookup("unknown") is None


def test_lookup_normalizes_by_the_source_language() -> None:
    pack = make_pack("tr", "en", {"ıstanbul": "Istanbul"})
    assert pack.lookup("ISTANBUL") == "Istanbul"
    ru = make_pack("ru", "en", {"привет": "hello"})
    assert ru.lookup("Приве́т") == "hello"
