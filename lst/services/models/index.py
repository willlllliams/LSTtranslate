"""Argos 模型索引（argospm-index 仓库的 index.json）的解析（架构文档 8.5 节）。

索引是一个 JSON 数组，每项描述一个模型包。本模块用到的字段：

- ``from_code``、``to_code``：Argos 语言码（zh 是简体中文，zt 是繁体中文）；
- ``package_version``：模型包版本，例如 "1.9"；
- ``links``：下载地址列表，取第一个 http(s) 地址；
- ``type``：包类型，缺省视为 "translate"，其他类型（例如句子切分包）跳过；
- ``size``：下载大小（字节）。这一项是否存在没有核实过，有就用，没有时大小在下载开始后由响应头给出。

其余字段（from_name、to_name、argos_version 等）不使用。必需字段缺失或类型不对的项跳过并记一条警告，
一项出错不影响其他项。同一语言对出现多次时取版本最高的一项。
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, cast
from urllib.parse import urlsplit

from lst.contracts.ids import Lang
from lst.contracts.models import version_key
from lst.services.models.argos import Pair, from_argos_code

DEFAULT_INDEX_URL: Final = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
"""argostranslate 默认使用的模型索引地址。"""

_REQUIRED_TEXT: Final = ("from_code", "to_code", "package_version")


class IndexFormatError(ValueError):
    """索引整体不是预期的格式（例如顶层不是数组）。"""


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """索引里一个可下载的模型包。

    from_code、to_code 是索引里的 Argos 原始代码，用来生成文件名；source、target 是规范语言码。
    """

    from_code: str
    to_code: str
    source: Lang
    target: Lang
    version: str
    url: str
    size_bytes: int | None

    @property
    def pair(self) -> Pair:
        return (self.source, self.target)


def _first_http_link(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    for link in cast(list[object], value):
        if isinstance(link, str) and urlsplit(link.strip()).scheme in ("http", "https"):
            return link.strip()
    return None


def _size(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def parse_entry(raw: Mapping[str, Any], position: int, logger: logging.Logger) -> IndexEntry | None:
    """解析一项。跳过的项返回 None：非翻译包记 DEBUG，字段缺失或非法记 WARNING。

    日志只写项的序号、字段名和语言码，不写下载地址。
    """
    kind = raw.get("type", "translate")
    if kind != "translate":
        logger.debug("模型索引第 %d 项的类型是 %r，不是翻译模型，跳过", position, kind)
        return None
    missing = [key for key in _REQUIRED_TEXT if not isinstance(raw.get(key), str) or not raw[key].strip()]
    url = _first_http_link(raw.get("links"))
    if url is None:
        missing.append("links")
    if missing or url is None:
        logger.warning("模型索引第 %d 项缺少字段或字段类型不对：%s，已跳过", position, ", ".join(missing))
        return None
    from_code = cast(str, raw["from_code"]).strip()
    to_code = cast(str, raw["to_code"]).strip()
    try:
        source, target = from_argos_code(from_code), from_argos_code(to_code)
    except ValueError:
        logger.warning("模型索引第 %d 项的语言码 %r→%r 无法识别，已跳过", position, from_code, to_code)
        return None
    if source == target:
        logger.warning("模型索引第 %d 项的源语言与目标语言相同（%s），已跳过", position, source)
        return None
    return IndexEntry(
        from_code=from_code,
        to_code=to_code,
        source=source,
        target=target,
        version=cast(str, raw["package_version"]).strip(),
        url=url,
        size_bytes=_size(raw.get("size")),
    )


def parse_index(data: object, logger: logging.Logger) -> list[IndexEntry]:
    """解析整个索引，返回按 (source, target) 排序的可下载模型。顶层不是数组时抛出 IndexFormatError。"""
    if not isinstance(data, list):
        raise IndexFormatError("模型索引的顶层不是数组")
    best: dict[Pair, IndexEntry] = {}
    for position, raw in enumerate(cast(list[object], data), start=1):
        if not isinstance(raw, dict):
            logger.warning("模型索引第 %d 项不是 JSON 对象，已跳过", position)
            continue
        entry = parse_entry(cast(dict[str, Any], raw), position, logger)
        if entry is None:
            continue
        current = best.get(entry.pair)
        if current is None or version_key(entry.version) > version_key(current.version):
            best[entry.pair] = entry
    return [best[pair] for pair in sorted(best)]
