"""GitHub Releases 版本列表与校验文件的解析，以及可用版本的选择。

版本列表来自 ``GET /repos/willlllliams/LSTtranslate/releases``。``/releases/latest`` 不返回预发布版本，
而目前发布的版本都是预发布版本，所以取完整列表后自己挑选。

一个版本可以安装，要同时满足：

1. 不是草稿（draft）；tag 去掉前导 v 后是合法的语义化版本号；
2. 附件里有本构建类型的安装包和它的校验文件，两者都有 ``browser_download_url``。同一个版本同时放两种
   安装包：普通版是 ``LSTtranslate-<版本>-setup.exe``，开发版是 ``LSTtranslate-<版本>-dev-setup.exe``，
   校验文件是安装包名加 ``.sha256``。普通版只认不带 ``-dev`` 的那一个，开发版只认 ``-dev-setup.exe``，
   按文件名完全相等匹配，不会把另一种安装包当成自己的（需求 DEV-08）；
3. include_prerelease 为假时，GitHub 没有把它标为预发布，版本号也不带预发布后缀。

校验文件是 sha256sum 的输出格式：``<64 位十六进制>  <文件名>``，文件名前可能有表示二进制模式的 ``*``。
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

from lst.contracts.automation import BuildFlavor
from lst.contracts.updates import ReleaseInfo
from lst.services.updates.version import Version, parse_version

DEFAULT_RELEASES_URL: Final = "https://api.github.com/repos/willlllliams/LSTtranslate/releases"

CHECKSUM_SUFFIX: Final = ".sha256"

_SHA256_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$")


class ReleaseFormatError(ValueError):
    """版本列表或校验文件不是预期的格式。"""


DEV_SUFFIX: Final = "-dev"
"""开发版安装包在版本号与 ``-setup.exe`` 之间多出的一段。"""

_INSTALLER_FILE = re.compile(r"^LSTtranslate-(.+?)(-dev)?-setup\.exe$")


def installer_name(version: str, flavor: BuildFlavor = BuildFlavor.NORMAL) -> str:
    """安装包的文件名，与打包脚本的输出一致：开发版在版本号后面多一段 ``-dev``。"""
    suffix = DEV_SUFFIX if flavor is BuildFlavor.DEV else ""
    return f"LSTtranslate-{version}{suffix}-setup.exe"


def parse_installer_name(name: str) -> tuple[str, BuildFlavor] | None:
    """安装包文件名 → (版本号, 构建类型)；不是安装包的文件名时返回 None。

    版本号本身不会以 ``-dev`` 结尾（打包脚本拒绝这样的版本号），所以 ``-dev-setup.exe`` 一定是开发版。
    """
    match = _INSTALLER_FILE.match(name)
    if match is None:
        return None
    return match.group(1), BuildFlavor.DEV if match.group(2) else BuildFlavor.NORMAL


@dataclass(frozen=True, slots=True)
class Candidate:
    """一个可以安装的版本：对外的版本信息加上两个附件的下载地址。"""

    version: Version
    info: ReleaseInfo
    installer_name: str
    installer_url: str
    checksum_url: str


def _text(item: Mapping[str, object], key: str) -> str:
    value = item.get(key)
    return value if isinstance(value, str) else ""


def _assets(item: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    """附件名 → 附件对象。"""
    raw = item.get("assets")
    if not isinstance(raw, list):
        return {}
    assets: dict[str, Mapping[str, object]] = {}
    for asset in cast(list[object], raw):
        if isinstance(asset, dict):
            entry = cast(dict[str, object], asset)
            name = _text(entry, "name")
            if name:
                assets[name] = entry
    return assets


def _candidate(item: Mapping[str, object], logger: logging.Logger, flavor: BuildFlavor) -> Candidate | None:
    tag = _text(item, "tag_name")
    if item.get("draft") is True:
        return None
    version = parse_version(tag)
    if version is None:
        logger.debug("跳过 tag 不是语义化版本号的版本 %r", tag)
        return None
    name = installer_name(version.text, flavor)
    assets = _assets(item)
    installer = assets.get(name)
    checksum = assets.get(name + CHECKSUM_SUFFIX)
    installer_url = _text(installer, "browser_download_url") if installer is not None else ""
    checksum_url = _text(checksum, "browser_download_url") if checksum is not None else ""
    if not installer_url or not checksum_url:
        logger.info("版本 %s 缺少 %s 版的安装包或校验文件附件，跳过", version, flavor.value)
        return None
    size = installer.get("size") if installer is not None else None
    info = ReleaseInfo(
        version=version.text,
        prerelease=item.get("prerelease") is True or version.prerelease,
        published_at=_text(item, "published_at"),
        notes=_text(item, "body"),
        page_url=_text(item, "html_url"),
        installer_bytes=size if isinstance(size, int) and size >= 0 else 0,
    )
    return Candidate(version, info, name, installer_url, checksum_url)


def parse_releases(
    data: object, logger: logging.Logger, flavor: BuildFlavor = BuildFlavor.NORMAL
) -> list[Candidate]:
    """解析版本列表，返回 flavor 这种构建类型可以安装的版本（不含草稿和缺附件的版本），顺序与列表相同。

    data 不是列表时抛出 ReleaseFormatError；列表里个别项格式不对时跳过这一项。
    """
    if not isinstance(data, list):
        raise ReleaseFormatError("版本列表不是 JSON 数组")
    candidates: list[Candidate] = []
    for item in cast(list[object], data):
        if not isinstance(item, dict):
            logger.debug("跳过版本列表里不是对象的一项")
            continue
        candidate = _candidate(cast(dict[str, object], item), logger, flavor)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def select_update(
    candidates: Sequence[Candidate], current: Version, *, include_prerelease: bool
) -> Candidate | None:
    """在高于当前版本的候选里取版本号最高的一个；没有时返回 None。"""
    eligible = [
        candidate
        for candidate in candidates
        if candidate.version > current and (include_prerelease or not candidate.info.prerelease)
    ]
    return max(eligible, key=lambda candidate: candidate.version, default=None)


def parse_checksum(text: str, expected_name: str) -> str:
    """从 sha256sum 格式的校验文件里取出 expected_name 的 SHA-256（小写十六进制）。

    文件可以有多行；只认文件名与 expected_name 完全相同的一行。找不到时抛出 ReleaseFormatError。
    """
    names: list[str] = []
    for line in text.splitlines():
        match = _SHA256_LINE.match(line.strip())
        if match is None:
            continue
        digest, name = match.groups()
        if name == expected_name:
            return digest.lower()
        names.append(name)
    if names:
        raise ReleaseFormatError(f"校验文件里的文件名是 {names[0]!r}，应当是 {expected_name!r}")
    raise ReleaseFormatError("校验文件不是 sha256sum 格式")
