"""词典包：词典索引（dict-index.json）的解析、已安装包的扫描、下载后的校验与安装。

词典包由字典翻译引擎使用，发布在 GitHub Release ``willlllliams/thunderbird-translate`` 的
``dicts-v1`` 标签下：
22 个 ``dict-<src>-<tgt>.tsv.gz``（英语与 11 种语言互译），外加列出全部包的 ``dict-index.json``。
包格式见字典引擎（lst.services.engines.dictionary.pack）；本模块只读包头，不解析词条。

词典目录（模型目录下的 dict）里每个语言对一个文件，文件名固定，所以没有“同一语言对的多个版本”：
安装新版本就是用 ``os.replace`` 原子地替换旧文件。每个包旁边有一个说明文件 ``<包文件名>.json``，
记下安装时索引给出的数据源、许可证、署名和 SHA-256。没有联网获取索引时，模型页仍然要显示署名
（CC BY-SA 4.0 要求署名可见），说明文件就是署名的来源；说明文件缺失时（例如手工放进目录的包），
署名用包头里的数据源名称和许可证代替。

本模块的函数都是同步的磁盘操作，由 ModelsModule 放进线程执行。
"""

import codecs
import gzip
import json
import logging
import os
import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import quote

from lst.contracts.ids import Lang
from lst.contracts.langs import normalize_lang
from lst.contracts.models import ModelErrorCode, dict_pack_file_name, parse_dict_pack_file_name
from lst.services._shared.download import HASH_CHUNK_BYTES, file_sha256

DEFAULT_DICT_INDEX_URL: Final = (
    "https://github.com/willlllliams/thunderbird-translate/releases/download/dicts-v1/dict-index.json"
)
"""词典索引的地址。包文件与索引在同一个发布标签下，地址把索引文件名换成包文件名。"""

INDEX_VERSION: Final = 1
SIDECAR_SUFFIX: Final = ".json"
PART_SUFFIX: Final = ".part"

HEADER_TAG: Final = "#dict"
MAX_HEADER_BYTES: Final = 4096
"""包头一行的长度上限。读包头时最多读这么多字节，找不到换行就不是词典包。"""

_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_STRING_FIELDS: Final = ("src", "tgt", "file", "sha256", "source", "license", "attribution")
_NUMBER_FIELDS: Final = ("bytes", "rawBytes", "entries", "forms")

type Pair = tuple[Lang, Lang]


class DictIndexFormatError(ValueError):
    """词典索引整体不是预期的格式（不是对象、version 不是 1、没有 packs 数组）。"""


class InvalidDictPack(Exception):
    """下载的文件不是可用的词典包，或者与索引不符。code 是原因分类，detail 是给用户看的技术细节。"""

    def __init__(self, code: ModelErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class DictIndexEntry:
    """词典索引里的一个包。

    source、target 是规范语言码；size_bytes 是压缩后的字节数；sha256 是压缩文件的 SHA-256（十六进制小写）；
    source_name、license、attribution 是数据源名称、许可证（SPDX 标识）与署名全文；url 是下载地址。
    """

    source: Lang
    target: Lang
    file: str
    size_bytes: int
    raw_bytes: int
    entries: int
    forms: int
    sha256: str
    source_name: str
    license: str
    attribution: str
    url: str

    @property
    def pair(self) -> Pair:
        return (self.source, self.target)


@dataclass(frozen=True, slots=True)
class PackHeader:
    """包头：语言对、构建日期（YYYYMMDD）、数据源名称与许可证。"""

    source: str
    target: str
    version: str
    source_name: str
    license: str


@dataclass(frozen=True, slots=True)
class InstalledDictPack:
    """词典目录里一个可用的包。attribution 来自说明文件，没有说明文件时由包头拼出。

    sha256 是包文件的 SHA-256：说明文件里有就用它，没有时读文件计算（包不超过 5 MB）。
    """

    path: Path
    source: Lang
    target: Lang
    version: str
    size: int
    source_name: str
    license: str
    attribution: str
    sha256: str

    @property
    def pair(self) -> Pair:
        return (self.source, self.target)


# ---------------------------------------------------------------- 索引


def pack_url(index_url: str, file: str) -> str:
    """包文件的下载地址：与索引在同一个目录下。"""
    base = index_url.split("?", 1)[0].rsplit("/", 1)[0]
    return f"{base}/{quote(file)}"


def _parse_item(
    raw: Mapping[str, Any], position: int, index_url: str, logger: logging.Logger
) -> DictIndexEntry | None:
    missing = [key for key in _STRING_FIELDS if not isinstance(raw.get(key), str)]
    missing += [
        key
        for key in _NUMBER_FIELDS
        if not isinstance(raw.get(key), int) or isinstance(raw.get(key), bool) or raw[key] < 0
    ]
    if missing:
        logger.warning("词典索引第 %d 项缺少字段或字段类型不对：%s，已跳过", position, ", ".join(missing))
        return None
    src, tgt = cast(str, raw["src"]), cast(str, raw["tgt"])
    try:
        source, target = normalize_lang(src), normalize_lang(tgt)
    except ValueError:
        logger.warning("词典索引第 %d 项的语言码 %r→%r 无法识别，已跳过", position, src, tgt)
        return None
    if (source, target) != (src, tgt) or source == target:
        logger.warning("词典索引第 %d 项的语言码 %r→%r 不是规范写法，已跳过", position, src, tgt)
        return None
    file = cast(str, raw["file"])
    # 文件名来自网上的索引：只接受与语言对对应的标准文件名，不会写到词典目录之外。
    if file != dict_pack_file_name(source, target):
        logger.warning("词典索引第 %d 项的文件名与语言对 %s→%s 不符，已跳过", position, source, target)
        return None
    sha256 = cast(str, raw["sha256"]).strip().lower()
    if _SHA256.fullmatch(sha256) is None:
        logger.warning("词典索引第 %d 项的 sha256 不是 64 个十六进制字符，已跳过", position)
        return None
    return DictIndexEntry(
        source=source,
        target=target,
        file=file,
        size_bytes=cast(int, raw["bytes"]),
        raw_bytes=cast(int, raw["rawBytes"]),
        entries=cast(int, raw["entries"]),
        forms=cast(int, raw["forms"]),
        sha256=sha256,
        source_name=cast(str, raw["source"]),
        license=cast(str, raw["license"]),
        attribution=cast(str, raw["attribution"]),
        url=pack_url(index_url, file),
    )


def parse_dict_index(data: object, index_url: str, logger: logging.Logger) -> list[DictIndexEntry]:
    """解析词典索引，返回按 (source, target) 排序的包。

    索引的顶层是 ``{"version": 1, "packs": [...]}``，不是这个形式时抛出 DictIndexFormatError。
    缺字段、语言码不规范、文件名与语言对不符、sha256 不合法的项跳过并记一条警告。
    同一语言对出现多次时取后一项。
    """
    if not isinstance(data, dict):
        raise DictIndexFormatError("词典索引的顶层不是 JSON 对象")
    top = cast(dict[str, Any], data)
    if top.get("version") != INDEX_VERSION:
        raise DictIndexFormatError(f"词典索引的 version 不是 {INDEX_VERSION}")
    packs = top.get("packs")
    if not isinstance(packs, list):
        raise DictIndexFormatError("词典索引没有 packs 数组")
    found: dict[Pair, DictIndexEntry] = {}
    for position, raw in enumerate(cast(list[object], packs), start=1):
        if not isinstance(raw, dict):
            logger.warning("词典索引第 %d 项不是 JSON 对象，已跳过", position)
            continue
        entry = _parse_item(cast(dict[str, Any], raw), position, index_url, logger)
        if entry is not None:
            found[entry.pair] = entry
    return [found[pair] for pair in sorted(found)]


# ---------------------------------------------------------------- 包头与已安装的包


def parse_header_line(line: bytes) -> PackHeader:
    """解析包头行。格式不对时抛出 ValueError。"""
    try:
        text = line.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError as exc:
        raise ValueError("包头不是 UTF-8 文本") from exc
    fields = text.split("\t")
    if len(fields) < 6 or fields[0] != HEADER_TAG or not fields[1] or not fields[2]:
        raise ValueError("包头不是 #dict 开头的 6 个字段")
    return PackHeader(fields[1], fields[2], fields[3], fields[4], fields[5])


def read_header(path: Path) -> PackHeader:
    """读取词典包文件的包头。不是 gzip 或包头不对时抛出 ValueError，读文件失败时抛出 OSError。"""
    try:
        with gzip.open(path, "rb") as stream:
            line = stream.readline(MAX_HEADER_BYTES)
    except (EOFError, zlib.error, gzip.BadGzipFile) as exc:
        raise ValueError(f"不是有效的 gzip 文件（{type(exc).__name__}）") from exc
    return parse_header_line(line)


def sidecar_path(pack_path: Path) -> Path:
    return pack_path.with_name(pack_path.name + SIDECAR_SUFFIX)


def _read_sidecar(path: Path) -> dict[str, Any]:
    try:
        data: object = json.loads(sidecar_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def read_installed(path: Path) -> InstalledDictPack:
    """读取一个已安装的包：文件名给出语言对，包头必须与之一致。不可用时抛出 ValueError 或 OSError。"""
    pair = parse_dict_pack_file_name(path.name)
    if pair is None:
        raise ValueError("文件名不是 dict-<src>-<tgt>.tsv.gz")
    header = read_header(path)
    if (header.source, header.target) != pair:
        raise ValueError(f"包头是 {header.source}→{header.target}，与文件名不符")
    sidecar = _read_sidecar(path)
    license_id = _text(sidecar, "license") or header.license
    attribution = _text(sidecar, "attribution") or f"{header.source_name} ({license_id})"
    return InstalledDictPack(
        path=path,
        source=pair[0],
        target=pair[1],
        version=header.version,
        size=path.stat().st_size,
        source_name=_text(sidecar, "source") or header.source_name,
        license=license_id,
        attribution=attribution,
        sha256=_text(sidecar, "sha256").lower() or file_sha256(path),
    )


def scan_dict_packs(directory: Path, logger: logging.Logger) -> dict[Pair, InstalledDictPack]:
    """扫描词典目录下的全部词典包。不可用的包记一条警告后跳过；目录不存在或无法读取时返回空表。"""
    found: dict[Pair, InstalledDictPack] = {}
    try:
        if not directory.is_dir():
            return found
        paths = sorted(
            path
            for path in directory.iterdir()
            if parse_dict_pack_file_name(path.name) is not None and path.is_file()
        )
    except OSError:
        logger.warning("无法读取词典目录 %s", directory, exc_info=True)
        return found
    for path in paths:
        try:
            pack = read_installed(path)
        except (ValueError, OSError) as exc:
            logger.warning("跳过不可用的词典包 %s：%s", path.name, exc)
            continue
        found[pack.pair] = pack
    return found


def remove_partial_files(directory: Path, logger: logging.Logger) -> int:
    """删除上次运行中断时留下的临时文件，返回删除的个数。"""
    removed = 0
    try:
        if not directory.is_dir():
            return 0
        parts = [path for path in directory.glob(f"*{PART_SUFFIX}") if path.is_file()]
    except OSError:
        logger.warning("无法读取词典目录 %s", directory, exc_info=True)
        return 0
    for path in parts:
        try:
            path.unlink()
            removed += 1
        except OSError:
            logger.warning("无法删除残留的临时文件 %s", path.name, exc_info=True)
    return removed


# ---------------------------------------------------------------- 校验与安装


def verify_download(part: Path, entry: DictIndexEntry, sha256: str) -> PackHeader:
    """校验下载好的临时文件，sha256 是下载时边收边算的摘要。

    1. 摘要与索引一致；
    2. 整个文件能解压（gzip 自带的 CRC 校验能发现损坏），内容是 UTF-8；
    3. 包头的语言对与索引一致。

    不通过时抛出 InvalidDictPack：摘要不符或不是词典包为 INVALID_PACKAGE，语言对不符为 PAIR_MISMATCH。
    """
    if sha256.lower() != entry.sha256:
        raise InvalidDictPack(ModelErrorCode.INVALID_PACKAGE, "SHA-256 与词典索引不符")
    try:
        with gzip.open(part, "rb") as stream:
            first = stream.readline(MAX_HEADER_BYTES)
            decoder = _Utf8Check()
            while block := stream.read(HASH_CHUNK_BYTES):
                decoder.feed(block)
            decoder.finish()
    except (EOFError, zlib.error, gzip.BadGzipFile) as exc:
        raise InvalidDictPack(
            ModelErrorCode.INVALID_PACKAGE, f"不是有效的 gzip 文件（{type(exc).__name__}）"
        ) from exc
    except UnicodeDecodeError as exc:
        raise InvalidDictPack(ModelErrorCode.INVALID_PACKAGE, "内容不是 UTF-8 文本") from exc
    try:
        header = parse_header_line(first)
    except ValueError as exc:
        raise InvalidDictPack(ModelErrorCode.INVALID_PACKAGE, str(exc)) from exc
    if (header.source, header.target) != entry.pair:
        raise InvalidDictPack(
            ModelErrorCode.PAIR_MISMATCH,
            f"词典包是 {header.source}→{header.target}，词典索引中是 {entry.source}→{entry.target}",
        )
    return header


class _Utf8Check:
    """分块检查 UTF-8：块边界可能切在一个多字节字符中间，用增量解码器处理。"""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()

    def feed(self, data: bytes) -> None:
        self._decoder.decode(data)

    def finish(self) -> None:
        self._decoder.decode(b"", final=True)


def install_pack(part: Path, final: Path, entry: DictIndexEntry) -> None:
    """先写说明文件，再把校验通过的临时文件原子地改名为正式文件（替换旧版本）。

    说明文件也先写到临时文件再改名。顺序是先说明文件后包：中途失败时最多留下一个没有包的说明文件，
    扫描只看包文件，不会出现“包是新的、署名是旧的”。
    """
    sidecar = sidecar_path(final)
    temporary = sidecar.with_name(sidecar.name + PART_SUFFIX)
    meta = {
        "source": entry.source_name,
        "license": entry.license,
        "attribution": entry.attribution,
        "sha256": entry.sha256,
        "entries": entry.entries,
        "forms": entry.forms,
    }
    temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, sidecar)
    os.replace(part, final)


def delete_pack(path: Path, logger: logging.Logger) -> bool:
    """删除词典包与它的说明文件，返回包文件是否已经不在了。"""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.error("无法删除词典包 %s", path.name, exc_info=True)
        return False
    try:
        sidecar_path(path).unlink(missing_ok=True)
    except OSError:
        logger.warning("无法删除词典包的说明文件 %s", sidecar_path(path).name, exc_info=True)
    return True
