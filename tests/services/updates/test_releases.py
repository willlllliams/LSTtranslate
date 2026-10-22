"""版本列表与校验文件的解析，以及可用版本的选择。"""

import logging

import pytest

from lst.services.updates.releases import (
    ReleaseFormatError,
    parse_checksum,
    parse_releases,
    select_update,
)
from lst.services.updates.version import Version, parse_version
from tests.services.updates.helpers import DOWNLOAD_HOST, release, setup_name

LOGGER = logging.getLogger("tests.updates.releases")


def _v(text: str) -> Version:
    version = parse_version(text)
    assert version is not None
    return version


def _versions(data: object, current: str = "0.1.0", *, include_prerelease: bool = True) -> str | None:
    best = select_update(parse_releases(data, LOGGER), _v(current), include_prerelease=include_prerelease)
    return best.version.text if best is not None else None


def test_parse_release_fields() -> None:
    [candidate] = parse_releases([release("v0.2.0-beta.1", prerelease=True, size=1234)], LOGGER)
    assert candidate.version == _v("0.2.0-beta.1")
    assert candidate.installer_name == "LSTtranslate-0.2.0-beta.1-setup.exe"
    assert candidate.installer_url == f"{DOWNLOAD_HOST}/v0.2.0-beta.1/LSTtranslate-0.2.0-beta.1-setup.exe"
    assert candidate.checksum_url == candidate.installer_url + ".sha256"
    info = candidate.info
    assert (info.version, info.prerelease, info.installer_bytes) == ("0.2.0-beta.1", True, 1234)
    assert info.notes == "notes for 0.2.0-beta.1"
    assert info.page_url.endswith("/releases/tag/v0.2.0-beta.1")
    assert info.published_at == "2026-10-01T00:00:00Z"


def test_selects_highest_version_regardless_of_list_order() -> None:
    data = [
        release("v0.2.0"),
        release("v0.3.0-beta.2", prerelease=True),
        release("v0.2.5"),
        release("v0.1.0"),
    ]
    assert _versions(data) == "0.3.0-beta.2"


def test_release_beats_its_own_prerelease() -> None:
    data = [release("v0.2.0-rc.1", prerelease=True), release("v0.2.0")]
    assert _versions(data) == "0.2.0"


def test_nothing_newer_than_current() -> None:
    data = [release("v0.1.0", prerelease=True), release("v0.0.9")]
    assert _versions(data, "0.1.0") is None
    # 当前是预发布版本时，同号的正式版更高。
    assert _versions([release("v0.2.0")], "0.2.0-beta.1") == "0.2.0"


def test_include_prerelease_false_skips_prereleases() -> None:
    data = [release("v0.3.0-beta.1"), release("v0.2.5", prerelease=True), release("v0.2.0")]
    # v0.3.0-beta.1 没有被 GitHub 标为预发布，但版本号带预发布后缀，同样跳过；v0.2.5 被 GitHub 标为预发布。
    assert _versions(data, include_prerelease=False) == "0.2.0"
    assert _versions(data, include_prerelease=True) == "0.3.0-beta.1"
    assert _versions([release("v0.2.0-beta.1", prerelease=True)], include_prerelease=False) is None


def test_releases_missing_assets_are_skipped() -> None:
    data = [
        release("v0.4.0", with_checksum=False),
        release("v0.3.0", with_installer=False),
        release("v0.2.0"),
    ]
    assert [candidate.version.text for candidate in parse_releases(data, LOGGER)] == ["0.2.0"]
    assert _versions(data) == "0.2.0"


def test_asset_name_must_match_tag_version() -> None:
    item = release("v0.3.0")
    item["tag_name"] = "v0.3.1"  # 附件仍然是 0.3.0 的文件名
    assert parse_releases([item], LOGGER) == []


def test_drafts_bad_tags_and_bad_items_are_skipped() -> None:
    bad_tag = release("v0.5.0")
    bad_tag["tag_name"] = "nightly"
    no_url = release("v0.6.0")
    no_url["assets"] = [{"name": setup_name("0.6.0"), "size": 1}, {"name": setup_name("0.6.0") + ".sha256"}]
    data: list[object] = [release("v0.9.0", draft=True), bad_tag, no_url, "junk", 42, release("v0.2.0")]
    assert [candidate.version.text for candidate in parse_releases(data, LOGGER)] == ["0.2.0"]


@pytest.mark.parametrize("data", [{"message": "Not Found"}, None, "[]"])
def test_non_list_is_format_error(data: object) -> None:
    with pytest.raises(ReleaseFormatError):
        parse_releases(data, LOGGER)


DIGEST = "a" * 64
NAME = "LSTtranslate-0.2.0-setup.exe"


@pytest.mark.parametrize(
    "text",
    [
        f"{DIGEST}  {NAME}\n",
        f"{DIGEST} *{NAME}\r\n",
        f"{DIGEST.upper()}  {NAME}",
        f"# comment\n{'b' * 64}  other.exe\n{DIGEST}  {NAME}\n",
    ],
)
def test_parse_checksum(text: str) -> None:
    assert parse_checksum(text, NAME) == DIGEST


def test_checksum_file_name_must_match() -> None:
    with pytest.raises(ReleaseFormatError, match="文件名"):
        parse_checksum(f"{DIGEST}  LSTtranslate-0.1.0-setup.exe\n", NAME)


@pytest.mark.parametrize("text", ["", DIGEST, f"{'z' * 64}  {NAME}", f"{'a' * 63}  {NAME}", "<html></html>"])
def test_checksum_garbage_is_format_error(text: str) -> None:
    with pytest.raises(ReleaseFormatError):
        parse_checksum(text, NAME)
