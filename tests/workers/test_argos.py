"""Argos 模型包的扫描、语言码映射与解压。"""

import zipfile
from pathlib import Path

import pytest

from lst import _fileops
from lst.workers.nmt_worker.argos import (
    InvalidPackage,
    extract_package,
    extracted_dir,
    prune_extracted,
    read_package,
    scan_packages,
)
from tests.workers.packages import REAL_EN_ZH, REAL_ZH_EN, make_package


def test_scan_maps_argos_codes_to_canonical(tmp_path: Path) -> None:
    make_package(tmp_path, "en", "zh")
    make_package(tmp_path, "zt", "en")
    make_package(tmp_path, "en", "ja")
    packages = scan_packages(tmp_path)
    assert sorted(packages) == [("en", "ja"), ("en", "zh-Hans"), ("zh-Hant", "en")]
    package = packages[("en", "zh-Hans")]
    assert package.root == "translate-en_zh-1_0"
    assert package.version == "1.0"
    assert len(package.digest) == 64


def test_scan_skips_invalid_packages(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    make_package(tmp_path, "en", "de")
    make_package(tmp_path, "en", "fr", sentencepiece=False)  # BPE 分词的包不支持
    (tmp_path / "garbage.argosmodel").write_bytes(b"not a zip")
    with zipfile.ZipFile(tmp_path / "no-metadata.argosmodel", "w") as archive:
        archive.writestr("x/model/model.bin", b"")
    (tmp_path / "ignored.zip").write_bytes(b"")
    assert list(scan_packages(tmp_path)) == [("en", "de")]
    assert "garbage.argosmodel" in caplog.text and "sentencepiece.model" in caplog.text


def test_scan_prefers_highest_version_for_same_pair(tmp_path: Path) -> None:
    make_package(tmp_path, "en", "de", version="1.9")
    make_package(tmp_path, "en", "de", version="1.10")
    make_package(tmp_path, "en", "de", version="1.2")
    assert scan_packages(tmp_path)[("en", "de")].version == "1.10"


def test_scan_of_missing_directory_is_empty(tmp_path: Path) -> None:
    assert scan_packages(tmp_path / "nope") == {}


def test_digest_depends_on_content_not_location(tmp_path: Path) -> None:
    a = read_package(make_package(tmp_path / "a", "en", "de"))
    b = read_package(make_package(tmp_path / "b", "en", "de"))
    c = read_package(make_package(tmp_path / "c", "en", "de", model_bytes=b"other weights"))
    assert a.digest == b.digest
    assert a.digest != c.digest


@pytest.mark.skipif(not REAL_EN_ZH.is_file() or not REAL_ZH_EN.is_file(), reason="本机没有 Argos 模型包")
def test_reads_real_argos_packages() -> None:
    en_zh = read_package(REAL_EN_ZH)
    zh_en = read_package(REAL_ZH_EN)
    assert (en_zh.source, en_zh.target, en_zh.version, en_zh.root) == (
        "en",
        "zh-Hans",
        "1.9",
        "translate-en_zh-1_9",
    )
    assert (zh_en.source, zh_en.target) == ("zh-Hans", "en")


def test_extract_keeps_only_needed_files_and_reuses_result(tmp_path: Path) -> None:
    package = read_package(make_package(tmp_path / "models", "en", "de"))
    root = extract_package(package, tmp_path / "extracted")
    assert root == extracted_dir(package, tmp_path / "extracted")
    names = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    assert names == [
        ".complete",
        "metadata.json",
        "model/config.json",
        "model/model.bin",
        "model/shared_vocabulary.json",
        "sentencepiece.model",
    ]
    sentinel = root / "sentinel"
    sentinel.write_text("x")
    assert extract_package(package, tmp_path / "extracted") == root
    assert sentinel.exists(), "已经完整解压过的包不应重新解压"
    assert not list((tmp_path / "extracted").glob(".tmp-*"))


def test_incomplete_extraction_is_redone(tmp_path: Path) -> None:
    package = read_package(make_package(tmp_path / "models", "en", "de"))
    partial = extracted_dir(package, tmp_path / "extracted")
    (partial / "model").mkdir(parents=True)
    (partial / "model" / "model.bin").write_bytes(b"trunc")
    root = extract_package(package, tmp_path / "extracted")
    assert (root / ".complete").is_file()
    assert (root / "model" / "model.bin").read_bytes() == b"model"


def test_changed_package_extracts_to_new_directory(tmp_path: Path) -> None:
    models = tmp_path / "models"
    old = read_package(make_package(models, "en", "de"))
    old_root = extract_package(old, tmp_path / "extracted")
    new = read_package(make_package(models, "en", "de", model_bytes=b"new weights"))
    new_root = extract_package(new, tmp_path / "extracted")
    assert new_root != old_root
    assert (new_root / "model" / "model.bin").read_bytes() == b"new weights"


def test_prune_removes_stale_extractions_only(tmp_path: Path) -> None:
    """升级后旧版本的解压目录、崩溃留下的临时目录被删除；当前包的目录和无关内容保留。"""
    models, extracted = tmp_path / "models", tmp_path / "extracted"
    old = read_package(make_package(models, "en", "de"))
    old_root = extract_package(old, extracted)
    current = read_package(make_package(models, "en", "de", model_bytes=b"new weights"))
    current_root = extract_package(current, extracted)
    crashed = extracted / ".tmp-translate-en_de-1_0-abcdef012345-4242"
    crashed.mkdir()
    unrelated_dir = extracted / "notes"
    unrelated_dir.mkdir()
    unrelated_file = extracted / "readme.txt"
    unrelated_file.write_text("x", encoding="utf-8")

    assert prune_extracted([current], extracted) == 2
    assert not old_root.exists() and not crashed.exists()
    assert current_root.is_dir() and unrelated_dir.is_dir() and unrelated_file.is_file()
    assert prune_extracted([current], extracted) == 0  # 再次调用没有可删的
    assert prune_extracted([], tmp_path / "missing") == 0


def test_unsafe_member_path_is_rejected(tmp_path: Path) -> None:
    path = make_package(tmp_path / "models", "en", "de", extra_members={"model/../../../evil.txt": b"x"})
    package = read_package(path)
    with pytest.raises(InvalidPackage):
        extract_package(package, tmp_path / "extracted")
    assert not (tmp_path / "evil.txt").exists()
    assert not any((tmp_path / "extracted").iterdir())


def test_extract_recovers_from_transient_publish_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = read_package(make_package(tmp_path / "models", "en", "de"))
    replace = _fileops.os.replace
    calls: list[Path] = []
    delays: list[float] = []

    def transient(source: Path, target: Path) -> None:
        calls.append(source)
        assert (source / ".complete").read_text("ascii") == package.digest
        if len(calls) < 3:
            raise PermissionError(13, "temporarily busy", str(source), 5)
        replace(source, target)

    monkeypatch.setattr(_fileops.os, "replace", transient)
    monkeypatch.setattr(_fileops.time, "sleep", delays.append)
    root = extract_package(package, tmp_path / "extracted")
    assert (root / "model" / "model.bin").read_bytes() == b"model"
    assert len(calls) == 3 and delays == [0.025, 0.05]
    assert not list(root.parent.glob(".tmp-*"))


def test_extract_reports_persistent_denial_and_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = read_package(make_package(tmp_path / "models", "en", "de"))

    def denied(source: Path, _target: Path) -> None:
        raise PermissionError(13, "busy", str(source), 5)

    delays: list[float] = []
    monkeypatch.setattr(_fileops.os, "replace", denied)
    monkeypatch.setattr(_fileops.time, "sleep", delays.append)
    with pytest.raises(PermissionError):
        extract_package(package, tmp_path / "extracted")
    assert len(delays) == 6
    assert not extracted_dir(package, tmp_path / "extracted").exists()
    assert not list((tmp_path / "extracted").glob(".tmp-*"))


def test_concurrent_completed_extraction_is_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = read_package(make_package(tmp_path / "models", "en", "de"))

    def another_process(_source: Path, target: Path) -> None:
        target.mkdir()
        (target / ".complete").write_text(package.digest, encoding="ascii")
        (target / "winner").write_bytes(b"other process")
        raise FileExistsError("another process finished first")

    monkeypatch.setattr(_fileops.os, "replace", another_process)
    root = extract_package(package, tmp_path / "extracted")
    assert (root / "winner").read_bytes() == b"other process"
    assert not list(root.parent.glob(".tmp-*"))
