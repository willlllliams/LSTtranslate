"""Argos 模型包的读取、校验与模型目录的磁盘操作（架构文档 2.5、8.5 节）。

Argos 模型包（.argosmodel）是一个 zip，顶层通常是一个目录，例如 ``translate-en_zh-1_9/``，里面有
``metadata.json``（from_code、to_code、package_version 等）、``sentencepiece.model`` 和 CTranslate2 模型目录
``model/``。判断一个包是否可用的规则与 NMT 进程（lst/workers/nmt_worker/argos.py）相同：
包里只有一个 metadata.json，并且有 sentencepiece.model 与 model/model.bin。NMT 进程跳过的包，
这里也不算作已安装，模型目录显示的状态因此与 NMT 进程实际能用的模型一致。

本模块的函数都是同步的磁盘操作，由 ModelsModule 放进 ``asyncio.to_thread`` 执行。
"""

import json
import logging
import os
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from lst.contracts.ids import Lang
from lst.contracts.langs import ZH_HANS, ZH_HANT, normalize_lang
from lst.contracts.models import ModelErrorCode, version_key

PACKAGE_SUFFIX: Final = ".argosmodel"
PART_SUFFIX: Final = ".part"

type Pair = tuple[Lang, Lang]

# Argos 的私有代码：zh 在 Argos 里指简体中文，zt 指繁体中文。
_ARGOS_SPECIAL: Final[Mapping[str, Lang]] = {"zh": ZH_HANS, "zt": ZH_HANT}
# NMT 进程只能加载 SentencePiece 分词的 CTranslate2 模型包，缺少这两个成员的包它会跳过。
_REQUIRED: Final = ("sentencepiece.model", "model/model.bin")
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


class InvalidPackage(Exception):
    """文件不是可用的模型包，或者与索引不符。code 是原因分类，detail 是给用户看的技术细节。"""

    def __init__(self, code: ModelErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PackageMeta:
    """一个模型包文件的元数据。source、target 是规范语言码，size 是文件字节数。"""

    path: Path
    source: Lang
    target: Lang
    version: str
    size: int

    @property
    def pair(self) -> Pair:
        return (self.source, self.target)


def from_argos_code(code: str) -> Lang:
    """把 Argos 的语言码映射成规范写法：zh → zh-Hans，zt → zh-Hant，其余按 normalize_lang 处理。

    与 NMT 进程不同，这里遇到非法代码抛出 ValueError：索引和模型包里的怪异代码应当被跳过，而不是猜一个语言。
    """
    key = code.strip().lower()
    special = _ARGOS_SPECIAL.get(key)
    return special if special is not None else normalize_lang(key)


def package_stem(from_code: str, to_code: str, version: str) -> str:
    """模型包的文件名主干，沿用 Argos 的命名：``translate-en_zh-1_9``。

    参数来自网上的索引，所以只保留字母、数字、下划线和连字符，文件名不会含有路径分隔符或 ``..``。
    """
    raw = f"translate-{from_code}_{to_code}-{version.replace('.', '_')}"
    return _UNSAFE_NAME_CHARS.sub("_", raw).strip("_") or "translate-model"


def read_package(path: Path, *, check_crc: bool = False) -> PackageMeta:
    """读取一个模型包的元数据；不是可用的模型包时抛出 InvalidPackage。

    check_crc 为真时还逐个成员核对 CRC，能发现下载中途损坏的文件；它要读完整个文件，只在下载后校验时使用。
    """
    try:
        size = path.stat().st_size
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            prefix = _package_prefix(names)
            missing = [item for item in _REQUIRED if prefix + item not in names]
            if missing:
                raise InvalidPackage(
                    ModelErrorCode.INVALID_PACKAGE,
                    f"缺少 {', '.join(missing)}（只支持 SentencePiece 分词的 CTranslate2 模型包）",
                )
            if check_crc:
                broken = archive.testzip()
                if broken is not None:
                    raise InvalidPackage(ModelErrorCode.INVALID_PACKAGE, f"模型包内的 {broken} 已损坏")
            metadata: object = json.loads(archive.read(prefix + "metadata.json").decode("utf-8"))
    except InvalidPackage:
        raise
    except zipfile.BadZipFile as exc:
        raise InvalidPackage(ModelErrorCode.INVALID_PACKAGE, f"不是有效的 zip 文件（{exc}）") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidPackage(ModelErrorCode.INVALID_PACKAGE, "metadata.json 不是合法的 JSON") from exc
    except OSError as exc:
        raise InvalidPackage(ModelErrorCode.DISK, f"读取文件失败（{type(exc).__name__}）") from exc
    if not isinstance(metadata, dict):
        raise InvalidPackage(ModelErrorCode.INVALID_PACKAGE, "metadata.json 不是 JSON 对象")
    meta = cast(dict[str, Any], metadata)
    from_code, to_code = meta.get("from_code"), meta.get("to_code")
    if not isinstance(from_code, str) or not isinstance(to_code, str):
        raise InvalidPackage(ModelErrorCode.INVALID_PACKAGE, "metadata.json 缺少 from_code 或 to_code")
    try:
        source, target = from_argos_code(from_code), from_argos_code(to_code)
    except ValueError as exc:
        raise InvalidPackage(
            ModelErrorCode.INVALID_PACKAGE, f"metadata.json 的语言码无法识别（{exc}）"
        ) from exc
    version = meta.get("package_version")
    return PackageMeta(path, source, target, version if isinstance(version, str) else "", size)


def _package_prefix(names: set[str]) -> str:
    """包内成员的公共前缀：metadata.json 在顶层时为空串，在唯一的顶层目录里时为 ``目录名/``。"""
    if "metadata.json" in names:
        return ""
    roots = {name.split("/", 1)[0] for name in names if name.endswith("/metadata.json")}
    if len(roots) == 1:
        root = next(iter(roots))
        if f"{root}/metadata.json" in names:
            return f"{root}/"
    raise InvalidPackage(ModelErrorCode.INVALID_PACKAGE, "找不到唯一的 metadata.json")


def verify_download(path: Path, expected: Pair) -> PackageMeta:
    """校验下载好的临时文件：是完整的 zip、是可用的模型包、语言对与索引一致。不一致时抛出 InvalidPackage。"""
    meta = read_package(path, check_crc=True)
    if meta.pair != expected:
        raise InvalidPackage(
            ModelErrorCode.PAIR_MISMATCH,
            f"模型包是 {meta.source}→{meta.target}，模型索引中是 {expected[0]}→{expected[1]}",
        )
    return meta


def best_package(packages: Sequence[PackageMeta]) -> PackageMeta:
    """同一语言对的多个包中 NMT 进程会用的那个：版本最高的，版本相同取文件名靠后的。"""
    return max(packages, key=lambda meta: (version_key(meta.version), meta.path.name))


def scan_installed(models_dir: Path, logger: logging.Logger) -> dict[Pair, list[PackageMeta]]:
    """扫描 models_dir 下（不含子目录）的全部 .argosmodel，按语言对分组返回。

    无效的包记一条警告后跳过。目录不存在或无法读取时返回空表：模型管理是辅助功能，不能因此让程序起不来。
    """
    found: dict[Pair, list[PackageMeta]] = {}
    try:
        if not models_dir.is_dir():
            return found
        paths = sorted(path for path in models_dir.glob(f"*{PACKAGE_SUFFIX}") if path.is_file())
    except OSError:
        logger.warning("无法读取模型目录 %s", models_dir, exc_info=True)
        return found
    for path in paths:
        try:
            meta = read_package(path)
        except InvalidPackage as exc:
            logger.warning("跳过无效的模型包 %s：%s", path.name, exc.detail)
            continue
        found.setdefault(meta.pair, []).append(meta)
    return found


def remove_partial_files(models_dir: Path, logger: logging.Logger) -> int:
    """删除上次运行中断时留下的 .part 临时文件，返回删除的个数。"""
    removed = 0
    try:
        if not models_dir.is_dir():
            return 0
        parts = [path for path in models_dir.glob(f"*{PART_SUFFIX}") if path.is_file()]
    except OSError:
        logger.warning("无法读取模型目录 %s", models_dir, exc_info=True)
        return 0
    for path in parts:
        try:
            path.unlink()
            removed += 1
        except OSError:
            logger.warning("无法删除残留的临时文件 %s", path.name, exc_info=True)
    return removed


def install_package(part: Path, final: Path, obsolete: Iterable[Path], logger: logging.Logger) -> None:
    """把校验通过的临时文件原子地改名为正式文件，再删除同一语言对的旧版本包。

    ``os.replace`` 在同一目录内是原子操作：NMT 进程扫描时要么看不到新包，要么看到完整的新包。
    旧版本删不掉（例如被其他程序占用）只记警告，NMT 进程按版本号会选用新包。
    """
    os.replace(part, final)
    for path in obsolete:
        if path == final:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("无法删除旧版本的模型包 %s", path.name, exc_info=True)


def delete_files(paths: Iterable[Path], logger: logging.Logger) -> int:
    """删除给定的模型包文件，返回成功删除（或本来就不存在）的个数。"""
    deleted = 0
    for path in paths:
        try:
            path.unlink(missing_ok=True)
            deleted += 1
        except OSError:
            logger.error("无法删除模型包 %s", path.name, exc_info=True)
    return deleted
