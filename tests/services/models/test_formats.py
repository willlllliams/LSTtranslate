"""Argos 模型包与模型索引的格式处理：语言码映射、文件名、包的读取与校验、目录扫描、索引解析。"""

import logging
import zipfile
from pathlib import Path

import pytest

from lst.contracts.ids import Lang
from lst.contracts.models import ModelErrorCode
from lst.services.models.argos import (
    InvalidPackage,
    best_package,
    from_argos_code,
    package_stem,
    read_package,
    remove_partial_files,
    scan_installed,
    verify_download,
)
from lst.services.models.index import IndexFormatError, parse_index
from tests.services.models.helpers import index_item, package_bytes, package_url, write_package

LOGGER = logging.getLogger("tests.models.formats")


# ---------------------------------------------------------------- 语言码与文件名


@pytest.mark.parametrize(
    ("code", "expected"),
    [("zh", "zh-Hans"), ("zt", "zh-Hant"), ("ZH", "zh-Hans"), ("en", "en"), ("pt_BR", "pt"), ("nb", "nb")],
)
def test_argos_codes_map_to_canonical_codes(code: str, expected: str) -> None:
    assert from_argos_code(code) == expected


@pytest.mark.parametrize("code", ["", "???", "und", "toolongcode"])
def test_invalid_argos_codes_raise(code: str) -> None:
    with pytest.raises(ValueError):
        from_argos_code(code)


def test_package_stem_follows_argos_naming() -> None:
    assert package_stem("en", "zh", "1.9") == "translate-en_zh-1_9"


def test_package_stem_cannot_escape_the_models_directory() -> None:
    stem = package_stem("../../evil", "x/y\\z", "1.0:bad")
    assert "/" not in stem and "\\" not in stem and ".." not in stem and ":" not in stem
    assert stem.startswith("translate-")


# ---------------------------------------------------------------- 模型包


def test_read_package_maps_codes_and_reads_version(tmp_path: Path) -> None:
    path = write_package(tmp_path, "zt", "en", version="1.0")
    meta = read_package(path)
    assert (meta.source, meta.target, meta.version) == ("zh-Hant", "en", "1.0")
    assert meta.size == path.stat().st_size


def test_read_package_accepts_metadata_at_top_level(tmp_path: Path) -> None:
    path = tmp_path / "flat.argosmodel"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("metadata.json", '{"from_code": "de", "to_code": "en", "package_version": "1.2"}')
        archive.writestr("sentencepiece.model", b"spm")
        archive.writestr("model/model.bin", b"model")
    assert read_package(path).pair == ("de", "en")


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        (b"definitely not a zip", "zip"),
        (package_bytes("en", "de", sentencepiece=False), "sentencepiece.model"),
        (package_bytes("en", "de", metadata={"from_code": "en"}), "from_code"),
        (package_bytes("en", "de", metadata={"from_code": "en", "to_code": "???"}), "语言码"),
    ],
)
def test_read_package_rejects_unusable_files(tmp_path: Path, content: bytes, fragment: str) -> None:
    path = tmp_path / "bad.argosmodel"
    path.write_bytes(content)
    with pytest.raises(InvalidPackage) as caught:
        read_package(path)
    assert caught.value.code is ModelErrorCode.INVALID_PACKAGE
    assert fragment in caught.value.detail


def test_read_package_rejects_archive_without_metadata(tmp_path: Path) -> None:
    path = tmp_path / "nometa.argosmodel"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("pkg/sentencepiece.model", b"spm")
        archive.writestr("pkg/model/model.bin", b"model")
    with pytest.raises(InvalidPackage, match=r"metadata\.json"):
        read_package(path)


def test_crc_check_detects_corruption(tmp_path: Path) -> None:
    payload = b"0123456789abcdef" * 64
    content = bytearray(package_bytes("en", "de", payload=payload))
    offset = bytes(content).index(payload) + 100
    content[offset] ^= 0xFF  # 改坏 model.bin 中间的一个字节，zip 的目录仍然完好
    path = tmp_path / "corrupt.argosmodel"
    path.write_bytes(bytes(content))
    read_package(path)  # 只读目录和 metadata.json 时发现不了
    with pytest.raises(InvalidPackage, match="已损坏"):
        read_package(path, check_crc=True)


def test_verify_download_checks_language_pair(tmp_path: Path) -> None:
    path = write_package(tmp_path, "en", "fr")
    assert verify_download(path, (Lang("en"), Lang("fr"))).pair == ("en", "fr")
    with pytest.raises(InvalidPackage) as caught:
        verify_download(path, (Lang("en"), Lang("de")))
    assert caught.value.code is ModelErrorCode.PAIR_MISMATCH
    assert "en→fr" in caught.value.detail and "en→de" in caught.value.detail


def test_best_package_prefers_higher_version(tmp_path: Path) -> None:
    old = read_package(write_package(tmp_path, "en", "de", version="1.9"))
    new = read_package(write_package(tmp_path, "en", "de", version="1.10"))
    assert best_package([new, old]) == new
    assert best_package([old, new]) == new


# ---------------------------------------------------------------- 目录扫描


def test_scan_skips_invalid_packages_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    good = write_package(tmp_path, "en", "zh")
    (tmp_path / "broken.argosmodel").write_bytes(b"PK\x03\x04 truncated")
    write_package(tmp_path, "en", "ja", sentencepiece=False)
    (tmp_path / "notes.txt").write_text("not a model", encoding="utf-8")
    nested = tmp_path / "nested"
    write_package(nested, "en", "ko")  # 子目录不扫描，与 NMT 进程一致
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        found = scan_installed(tmp_path, LOGGER)
    assert list(found) == [("en", "zh-Hans")]
    assert [meta.path for meta in found[(Lang("en"), Lang("zh-Hans"))]] == [good]
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert any("broken.argosmodel" in text for text in warnings)


def test_scan_of_missing_directory_is_empty(tmp_path: Path) -> None:
    assert scan_installed(tmp_path / "missing", LOGGER) == {}


def test_remove_partial_files(tmp_path: Path) -> None:
    (tmp_path / "translate-en_zh-1_9.part").write_bytes(b"half")
    keep = write_package(tmp_path, "en", "zh")
    assert remove_partial_files(tmp_path, LOGGER) == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == [keep.name]


# ---------------------------------------------------------------- 索引


def test_parse_index_reads_entries() -> None:
    entries = parse_index(
        [index_item("zh", "en", "1.9", size=12345), index_item("en", "zt", "1.0")],
        LOGGER,
    )
    assert [(e.source, e.target, e.version) for e in entries] == [
        ("en", "zh-Hant", "1.0"),
        ("zh-Hans", "en", "1.9"),
    ]
    zh_en = entries[1]
    assert (zh_en.from_code, zh_en.to_code) == ("zh", "en")
    assert zh_en.url == package_url("zh", "en", "1.9")
    assert zh_en.size_bytes == 12345
    assert entries[0].size_bytes is None


def test_parse_index_skips_incomplete_items_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    items: list[object] = [
        index_item("en", "de"),
        {"from_code": "en", "to_code": "fr", "links": [package_url("en", "fr")]},  # 缺 package_version
        {"package_version": "1.0", "to_code": "fr", "links": [package_url("en", "fr")]},  # 缺 from_code
        index_item("en", "it", links=[]),  # 没有下载地址
        index_item("en", "es", links=["ipfs://QmSomething"]),  # 只有非 http 地址
        index_item("en", "ru", package_version=19),  # 类型不对
        index_item("en", "???"),  # 语言码无法识别
        index_item("en", "en"),  # 源语言与目标语言相同
        "not an object",
    ]
    with caplog.at_level(logging.DEBUG, logger=LOGGER.name):
        entries = parse_index(items, LOGGER)
    assert [(e.source, e.target) for e in entries] == [("en", "de")]
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 8
    assert "package_version" in warnings[0].getMessage()
    assert "from_code" in warnings[1].getMessage()
    assert "links" in warnings[2].getMessage()


def test_parse_index_ignores_non_translation_packages_quietly(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER.name):
        entries = parse_index([index_item("en", "de", type="sbd"), index_item("en", "fr")], LOGGER)
    assert [(e.source, e.target) for e in entries] == [("en", "fr")]
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_parse_index_keeps_highest_version_per_pair() -> None:
    entries = parse_index(
        [index_item("en", "de", "1.9"), index_item("en", "de", "1.10"), index_item("en", "de", "1.2")], LOGGER
    )
    assert [e.version for e in entries] == ["1.10"]


def test_parse_index_rejects_wrong_top_level() -> None:
    with pytest.raises(IndexFormatError):
        parse_index({"packages": []}, LOGGER)


def test_parse_index_ignores_invalid_sizes() -> None:
    entries = parse_index(
        [
            index_item("en", "de", size=True),
            index_item("en", "fr", size=-5),
            index_item("en", "it", size="9"),
        ],
        LOGGER,
    )
    assert [e.size_bytes for e in entries] == [None, None, None]
