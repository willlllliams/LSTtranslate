"""语义化版本号的解析与比较，以及当前运行版本的读取。"""

import itertools
import json
import logging
from pathlib import Path

import pytest

from lst.services.updates import version as version_module
from lst.services.updates.version import Version, parse_version, running_version, semver_text


@pytest.mark.parametrize(
    ("text", "canonical", "prerelease"),
    [
        ("0.1.0", "0.1.0", False),
        ("v0.1.0", "0.1.0", False),
        ("V1.2.3", "1.2.3", False),
        (" v0.2.0-beta.1 ", "0.2.0-beta.1", True),
        ("1.0.0-rc.1+build.7", "1.0.0-rc.1", True),
        ("1.0.0+20261021", "1.0.0", False),
    ],
)
def test_parse_valid_versions(text: str, canonical: str, prerelease: bool) -> None:
    version = parse_version(text)
    assert version is not None
    assert version.text == canonical
    assert version.prerelease is prerelease


@pytest.mark.parametrize(
    "text",
    ["", "v", "1.0", "1.0.0.0", "01.0.0", "1.0.0-", "1.0.0-01", "latest", "1.0.0-beta..1", "1.0.0 beta"],
)
def test_parse_rejects_invalid_versions(text: str) -> None:
    assert parse_version(text) is None


def _v(text: str) -> Version:
    version = parse_version(text)
    assert version is not None
    return version


def test_semver_precedence_chain() -> None:
    """SemVer 2.0.0 第 11 条给出的示例序列，外加主、次、修订号的整数比较。"""
    chain = [
        "0.9.0",
        "0.10.0",
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.1.0",
        "2.0.0",
    ]
    versions = [_v(text) for text in chain]
    for lower, higher in itertools.pairwise(versions):
        assert lower < higher, (lower, higher)
    assert sorted(reversed(versions)) == versions


def test_prerelease_is_lower_than_release() -> None:
    assert _v("0.2.0-beta.1") < _v("0.2.0")
    assert _v("0.2.0-beta.1") > _v("0.1.0")


def test_build_metadata_and_leading_v_do_not_affect_comparison() -> None:
    assert _v("v1.0.0") == _v("1.0.0+build.5")
    assert not _v("1.0.0+a") < _v("1.0.0+b")


def test_running_version_from_install_marker(tmp_path: Path) -> None:
    marker = tmp_path / "installed.json"
    marker.write_text(
        json.dumps({"version": "0.3.1", "built": "2026-10-21T00:00:00+00:00"}), encoding="utf-8"
    )
    assert running_version(marker) == "0.3.1"


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"built": "x"}), json.dumps(["0.3.1"])])
def test_running_version_falls_back_to_package_metadata(
    tmp_path: Path, content: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    marker = tmp_path / "installed.json"
    if content is not None:
        marker.write_text(content, encoding="utf-8")
    caplog.set_level(logging.WARNING)
    version = running_version(marker, logging.getLogger("tests.updates.version"))
    # 测试环境里 lsttranslate 以可编辑方式安装，包元数据的版本就是 pyproject.toml 的版本。
    assert parse_version(version) is not None
    assert (content is None) == (not caplog.records)


@pytest.mark.parametrize(
    ("text", "semver"),
    [
        ("0.2.0b1", "0.2.0-beta.1"),
        ("0.2.0a3", "0.2.0-alpha.3"),
        ("0.2.0rc2", "0.2.0-rc.2"),
        ("0.2.0.rc2", "0.2.0-rc.2"),
        ("1.0.0", "1.0.0"),
        ("0.2.0-beta.1", "0.2.0-beta.1"),
    ],
)
def test_semver_text_converts_pep440_prereleases(text: str, semver: str) -> None:
    assert semver_text(text) == semver
    # 换算后的写法与 SemVer 写法比较结果一致：0.2.0b1 低于 0.2.0。
    parsed = parse_version(semver_text(text))
    assert parsed is not None and parsed.text == semver


@pytest.mark.parametrize("text", ["0.2.0.dev3", "0.2.0.post1", "1.0", "garbage"])
def test_semver_text_keeps_unconvertible_text(text: str) -> None:
    assert semver_text(text) == text


def test_running_version_converts_pep440_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 源码运行时版本来自包元数据，元数据里是 PEP 440 规范化后的写法。
    def fake_version(_name: str) -> str:
        return "0.2.0b1"

    monkeypatch.setattr(version_module.metadata, "version", fake_version)
    assert running_version(tmp_path / "missing.json") == "0.2.0-beta.1"
    marker = tmp_path / "installed.json"
    marker.write_text(json.dumps({"version": "0.3.0rc1"}), encoding="utf-8")
    assert running_version(marker) == "0.3.0-rc.1"
