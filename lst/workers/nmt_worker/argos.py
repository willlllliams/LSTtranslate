"""Argos 模型包的扫描与解压（架构文档 2.5 节）。

Argos 模型包（.argosmodel）是一个 zip，顶层是一个目录，例如 `translate-en_zh-1_9/`，里面有：

- `metadata.json`：from_code、to_code、package_version 等；
- `sentencepiece.model`：源语言的 SentencePiece 分词模型；
- `model/`：CTranslate2 模型目录（model.bin、config.json、词表）；
- `stanza/`：argostranslate 用来切句的 stanza 模型，本项目自己切句，不解压它。

扫描只读 zip 的目录和 metadata.json，不解压。第一次用到某个模型时才解压到解压目录，
目录名带包内容的摘要，解压过的包不重复解压；包文件被替换成新版本后，摘要变化，自动解压到新目录。
"""

import hashlib
import json
import logging
import os
import re
import shutil
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from lst._fileops import publish_directory
from lst.workers.nmt_worker.backend import Pair
from lst.workers.nmt_worker.languages import from_argos_code

log = logging.getLogger(__name__)

PACKAGE_SUFFIX = ".argosmodel"
_COMPLETE_MARKER = ".complete"
# 解压时保留的成员（相对包内顶层目录）。
_WANTED_FILES = ("metadata.json", "sentencepiece.model")
_WANTED_DIR = "model/"
_REQUIRED = ("sentencepiece.model", "model/model.bin")


class InvalidPackage(Exception):
    """文件不是可用的 Argos 模型包。"""


@dataclass(frozen=True, slots=True)
class ArgosPackage:
    """一个已安装的模型包。source、target 是规范语言码。"""

    path: Path
    root: str  # zip 内的顶层目录名；包没有顶层目录时为空串
    source: str
    target: str
    version: str  # metadata.json 的 package_version
    digest: str  # 包内容的摘要：全部成员的名字、CRC32 和大小的 SHA-256，只读 zip 目录就能算出

    @property
    def pair(self) -> Pair:
        return (self.source, self.target)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def read_package(path: Path) -> ArgosPackage:
    """读取一个模型包的元数据；不是可用的模型包时抛 InvalidPackage。"""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = {info.filename for info in infos}
            roots = {name.split("/", 1)[0] for name in names if name.endswith("metadata.json")}
            if "metadata.json" in names:
                root = ""
            elif len(roots) == 1 and f"{next(iter(roots))}/metadata.json" in names:
                root = next(iter(roots))
            else:
                raise InvalidPackage("找不到唯一的 metadata.json")
            prefix = f"{root}/" if root else ""
            missing = [item for item in _REQUIRED if prefix + item not in names]
            if missing:
                raise InvalidPackage(
                    f"缺少 {', '.join(missing)}（只支持 SentencePiece 分词的 CTranslate2 模型包）"
                )
            metadata: object = json.loads(archive.read(prefix + "metadata.json").decode("utf-8"))
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidPackage(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise InvalidPackage("metadata.json 不是 JSON 对象")
    meta = cast(dict[str, Any], metadata)
    from_code, to_code = meta.get("from_code"), meta.get("to_code")
    if not isinstance(from_code, str) or not isinstance(to_code, str) or not from_code or not to_code:
        raise InvalidPackage("metadata.json 缺少 from_code 或 to_code")
    version = meta.get("package_version")
    digest = hashlib.sha256()
    for info in sorted(infos, key=lambda i: i.filename):
        digest.update(f"{info.filename}\0{info.CRC:08x}\0{info.file_size}\n".encode())
    return ArgosPackage(
        path=path,
        root=root,
        source=from_argos_code(from_code),
        target=from_argos_code(to_code),
        version=version if isinstance(version, str) else "",
        digest=digest.hexdigest(),
    )


def scan_packages(model_dir: Path) -> dict[Pair, ArgosPackage]:
    """扫描 model_dir 下（不含子目录）的全部 .argosmodel，按语言对返回。

    无效的包记一条警告后跳过。同一语言对有多个包时取 package_version 最高的，版本相同取文件名靠后的。
    model_dir 不存在时返回空表（还没有安装任何模型）。
    """
    if not model_dir.is_dir():
        log.warning("模型目录不存在：%s", model_dir)
        return {}
    found: dict[Pair, ArgosPackage] = {}
    for path in sorted(model_dir.glob(f"*{PACKAGE_SUFFIX}")):
        if not path.is_file():
            continue
        try:
            package = read_package(path)
        except InvalidPackage as exc:
            log.warning("跳过无效的模型包 %s：%s", path.name, exc)
            continue
        current = found.get(package.pair)
        if current is None or _version_key(package.version) >= _version_key(current.version):
            if current is not None:
                log.info("%s→%s 有多个模型包，使用 %s", package.source, package.target, package.path.name)
            found[package.pair] = package
    return found


def _safe_relative(name: str, prefix: str) -> PurePosixPath | None:
    """把 zip 成员名换成相对顶层目录的安全路径；不需要解压或路径不安全（绝对路径、..）时返回 None。"""
    if not name.startswith(prefix) or name.endswith("/"):
        return None
    relative = name[len(prefix) :]
    if relative not in _WANTED_FILES and not relative.startswith(_WANTED_DIR):
        return None
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or ":" in relative or "\\" in relative:
        raise InvalidPackage(f"模型包含有不安全的路径：{name}")
    return path


def extracted_dir(package: ArgosPackage, extract_root: Path) -> Path:
    """模型包解压后的目录。目录名带摘要，包内容变了就换一个目录。"""
    stem = package.root or package.path.stem
    return extract_root / f"{stem}-{package.digest[:12]}"


def extract_package(package: ArgosPackage, extract_root: Path) -> Path:
    """把模型包解压到 extract_root 下并返回解压目录；已经完整解压过时直接返回。

    先解压到临时目录，写入完成标记后再整体改名，中途崩溃不会留下看似完整的目录。
    """
    target = extracted_dir(package, extract_root)
    if (target / _COMPLETE_MARKER).is_file():
        return target
    if target.exists():
        log.warning("解压目录不完整，重新解压：%s", target)
        shutil.rmtree(target)
    extract_root.mkdir(parents=True, exist_ok=True)
    staging = extract_root / f".tmp-{target.name}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    prefix = f"{package.root}/" if package.root else ""
    try:
        with zipfile.ZipFile(package.path) as archive:
            for info in archive.infolist():
                relative = _safe_relative(info.filename, prefix)
                if relative is None:
                    continue
                destination = staging.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as sink:
                    shutil.copyfileobj(source, sink, 1 << 20)
        (staging / _COMPLETE_MARKER).write_text(package.digest, encoding="ascii")
        try:
            publish_directory(staging, target)
        except OSError:
            # 另一个进程刚好解压完同一个包。
            if not (target / _COMPLETE_MARKER).is_file():
                raise
            shutil.rmtree(staging, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    log.info("已解压模型包 %s", package.path.name)
    return target


_EXTRACTED_NAME = re.compile(r"^(?:.+-[0-9a-f]{12}|\.tmp-.+-\d+)$")


def prune_extracted(packages: Iterable[ArgosPackage], extract_root: Path) -> int:
    """删除 extract_root 下不再对应任何模型包的解压目录，返回删除的个数。

    模型升级或删除后，旧版本的解压目录没有用处；进程中途崩溃会留下 .tmp- 开头的临时目录。
    只删除符合这两种命名规则的目录，用户放在同一目录下的其他内容不受影响。
    在进程加载时、开始解压之前调用：这时没有别的 NMT 进程在解压（单实例，重载先结束旧进程）。
    """
    if not extract_root.is_dir():
        return 0
    keep = {extracted_dir(package, extract_root).name for package in packages}
    removed = 0
    for entry in extract_root.iterdir():
        if not entry.is_dir() or entry.name in keep or not _EXTRACTED_NAME.match(entry.name):
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed += 1
    if removed:
        log.info("已清理 %d 个不再使用的解压目录", removed)
    return removed


def describe(packages: Iterable[ArgosPackage]) -> list[list[str]]:
    """模型包的版本信息，按语言对排序，用于 fingerprint。"""
    return sorted([p.source, p.target, p.version, p.digest] for p in packages)
