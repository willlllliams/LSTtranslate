"""语义化版本号（Semantic Versioning 2.0.0，简称 SemVer）的解析与比较，以及当前运行版本的读取。

比较规则按 SemVer 第 11 条：

1. 依次比较主版本号、次版本号、修订号，按整数大小；
2. 三者相同时，带预发布后缀的版本低于不带后缀的版本，例如 0.2.0-beta.1 < 0.2.0；
3. 两个版本都带预发布后缀时，逐个比较点号分隔的标识符：纯数字的按整数比较，其余按 ASCII 字典序，
   纯数字标识符低于非数字标识符；前面的标识符都相同时，标识符少的版本较低。

构建元数据（``+`` 之后的部分）不参与比较。GitHub 的 tag 习惯写成 ``v0.1.0``，解析时去掉前导的 v。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Final, cast

PACKAGE_NAME: Final = "lsttranslate"
"""pyproject.toml 里的项目名，源码运行时从包元数据读取版本。"""

UNKNOWN_VERSION: Final = "0.0.0"

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

_PEP440_PRE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)[-_.]?(a|alpha|b|beta|c|rc|pre|preview)[-_.]?(\d+)$", re.IGNORECASE
)
_PEP440_LABELS: Final = {
    "a": "alpha",
    "alpha": "alpha",
    "b": "beta",
    "beta": "beta",
    "c": "rc",
    "rc": "rc",
    "pre": "rc",
    "preview": "rc",
}
"""PEP 440 预发布标签到 SemVer 标识符的对应：alpha < beta < rc 的顺序在两边一致。"""

type _Identifier = tuple[int, int, str]
"""预发布标识符的排序键：(0, 数值, "") 表示纯数字，(1, 0, 文本) 表示其他。"""


def _identifier_key(identifier: str) -> _Identifier:
    if identifier.isdigit():
        return (0, int(identifier), "")
    return (1, 0, identifier)


@dataclass(frozen=True, slots=True, order=True)
class Version:
    """一个语义化版本号。按 SemVer 优先级比较；text 是规范写法（不带前导 v），不参与比较。"""

    _key: tuple[int, int, int, int, tuple[_Identifier, ...]] = field(repr=False)
    text: str = field(compare=False)

    @property
    def major(self) -> int:
        return self._key[0]

    @property
    def minor(self) -> int:
        return self._key[1]

    @property
    def patch(self) -> int:
        return self._key[2]

    @property
    def prerelease(self) -> bool:
        """是否带预发布后缀。"""
        return self._key[3] == 0

    def __str__(self) -> str:
        return self.text


def parse_version(text: str) -> Version | None:
    """解析版本号或 tag，例如 ``0.2.0``、``v0.2.0-beta.1``、``0.2.0+build.5``。不合 SemVer 时返回 None。"""
    value = text.strip()
    if value[:1] in ("v", "V"):
        value = value[1:]
    match = _SEMVER.match(value)
    if match is None:
        return None
    major, minor, patch, pre, _build = match.groups()
    identifiers = tuple(_identifier_key(part) for part in pre.split(".")) if pre else ()
    # 第四项：带预发布后缀为 0，不带为 1，使 0.2.0-beta.1 < 0.2.0。
    key = (int(major), int(minor), int(patch), 0 if pre else 1, identifiers)
    canonical = f"{major}.{minor}.{patch}" + (f"-{pre}" if pre else "")
    return Version(key, canonical)


def semver_text(text: str) -> str:
    """把版本号换算成 SemVer 写法：已是 SemVer 时原样返回；PEP 440 的预发布写法换算，例如
    ``0.2.0b1`` → ``0.2.0-beta.1``、``0.2.0rc2`` → ``0.2.0-rc.2``；其余写法原样返回。"""
    value = text.strip()
    if parse_version(value) is not None:
        return value
    match = _PEP440_PRE.match(value)
    if match is None:
        return value
    major, minor, patch, label, number = match.groups()
    return f"{int(major)}.{int(minor)}.{int(patch)}-{_PEP440_LABELS[label.lower()]}.{int(number)}"


def running_version(install_marker: Path, logger: logging.Logger | None = None) -> str:
    """当前运行的版本号。

    安装版读安装目录 ``app\\installed.json`` 的 version 字段（打包脚本写入）；文件不存在或读不出版本时
    读包元数据（源码运行时由 uv 安装的可编辑包提供）；都没有时返回 ``0.0.0``。
    """
    log = logger or logging.getLogger(__name__)
    if install_marker.is_file():
        try:
            data: object = json.loads(install_marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("无法读取安装版标记 %s，改用包元数据中的版本", install_marker, exc_info=True)
        else:
            if isinstance(data, dict):
                version = cast(dict[str, object], data).get("version")
                if isinstance(version, str) and version.strip():
                    return semver_text(version)
            log.warning("安装版标记 %s 里没有版本号，改用包元数据中的版本", install_marker)
    try:
        return semver_text(metadata.version(PACKAGE_NAME))
    except metadata.PackageNotFoundError:
        log.warning("找不到包 %s 的元数据，当前版本按 %s 处理", PACKAGE_NAME, UNKNOWN_VERSION)
        return UNKNOWN_VERSION
