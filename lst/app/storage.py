r"""数据目录与模型目录的磁盘操作：写入检查、复制数据、移动模型与回滚、旧布局迁移、卸载时的清理范围
（需求 CFG-06、CFG-07、MOD-04，架构文档 2.9.5 节）。

本模块只做磁盘操作，全部是同步函数，调用方放进线程执行；进度经回调报告，回调在执行线程里调用。

写入检查（``probe_writable``）在目标目录里实际创建、写入、改名、删除一个文件，用的是本进程的实际权限。
源码版的虚拟环境 Python 可能以 Low 完整性运行（2.9.5 节），这时 ACL 上显示“完全控制”的用户目录照样写不进去；
只读介质、受控文件夹访问的拦截也一样。只看 ACL 或 ``os.access`` 判断不出这些情况，实际写一次才可靠。

复制数据目录（``plan_data_copy``、``copy_data``）：除 tmp、锁文件、源码版的位置文件以外的全部内容。
缓存与统计库正被程序使用（WAL 模式），直接复制文件可能拿到不一致的快照，所以用 SQLite 的在线备份接口
（``sqlite3.Connection.backup``）复制，``-wal``、``-shm`` 文件不复制。复制失败时删除已经复制的内容。

移动模型目录（``plan_models_move``、``ModelsMove``）：逐个处理 MODEL_SUBDIRS 里存在的子目录。新旧目录在
同一个卷上时直接改名，瞬间完成；不在同一个卷上时先复制，全部复制成功、配置写好之后（``commit``）才删除
旧目录。任何一步失败都可以 ``rollback``：改过名的改回去，复制出来的删掉，旧目录始终完整。
"""

import contextlib
import errno
import logging
import os
import shutil
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from lst.app.paths import LOCATION_FILE_NAME, MODEL_SUBDIRS, MODELS_DIR_NAME
from lst.contracts.storage import StorageErrorCode

logger = logging.getLogger(__name__)

type Progress = Callable[[int], None]
"""进度回调：参数是这次新完成的字节数。"""

CHUNK_BYTES: Final = 1 << 20

COPY_SKIPPED: Final = frozenset({"tmp", "lsttranslate.lock", LOCATION_FILE_NAME})
"""复制数据目录时跳过的顶层条目：临时文件、运行中的锁文件、源码版的位置文件。"""

SQLITE_SIDE_SUFFIXES: Final = ("-wal", "-shm", "-journal")

DATA_DIRS: Final = ("config", "logs", "tmp", "engines", "runtimes")
"""数据目录里由程序创建的子目录（engines、runtimes 是旧版本放在数据目录里的位置）。"""

DATA_FILE_PATTERNS: Final = (
    "cache.sqlite3*",
    "stats.sqlite3*",
    "ui_state.ini",
    "updates.json",
    "lsttranslate.lock",
)
"""数据目录里由程序创建的文件（含 SQLite 的 -wal、-shm 与损坏后改名保留的 .corrupt-* 文件）。"""

_ERROR_NOT_SAME_DEVICE: Final = 17


class StorageError(Exception):
    """更改目录失败。code 决定界面上的说明，detail 是技术细节。"""

    def __init__(self, code: StorageErrorCode, detail: str = "") -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.detail = detail


def describe_os_error(exc: OSError) -> str:
    """系统错误的简短说明：系统给出的文字加错误码，不含路径（路径另外显示）。"""
    text = exc.strerror or type(exc).__name__
    code = getattr(exc, "winerror", None) or exc.errno
    return f"{text}（{code}）" if code else text


def same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def is_inside(path: Path, parent: Path) -> bool:
    """path 在 parent 里面（不含相等）。比较前统一大小写与分隔符。"""
    child = Path(os.path.normcase(os.path.abspath(path)))
    base = Path(os.path.normcase(os.path.abspath(parent)))
    return child != base and child.is_relative_to(base)


# ---------------------------------------------------------------- 写入检查


@dataclass(frozen=True, slots=True)
class Probe:
    """写入检查的结果。error 为 None 表示可写；created 是检查时新建的目录，从深到浅排列。"""

    error: str | None
    created: tuple[Path, ...] = ()


def _missing_parents(directory: Path) -> list[Path]:
    """directory 及其上级中还不存在的目录，从深到浅。"""
    missing: list[Path] = []
    current = directory
    while not current.exists() and current.parent != current:
        missing.append(current)
        current = current.parent
    return missing


def probe_writable(directory: Path) -> Probe:
    """在 directory 里创建、写入、改名、删除一个文件，检查本进程能否写入。目录不存在时先创建。

    失败时删除检查中新建的目录，返回系统给出的原因。
    """
    created = _missing_parents(directory)
    try:
        if directory.exists() and not directory.is_dir():
            raise NotADirectoryError(errno.ENOTDIR, "不是目录")
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".lst-write-test-{uuid.uuid4().hex}"
        with probe.open("wb") as handle:
            handle.write(b"LSTtranslate")
            handle.flush()
            os.fsync(handle.fileno())
        renamed = probe.with_name(probe.name + ".renamed")
        os.replace(probe, renamed)
        renamed.unlink()
    except OSError as exc:
        remove_created(created)
        return Probe(describe_os_error(exc))
    return Probe(None, tuple(created))


def remove_created(created: Iterable[Path]) -> None:
    """删除写入检查新建的目录（只删空目录，从深到浅）。"""
    for directory in created:
        with contextlib.suppress(OSError):
            directory.rmdir()


# ---------------------------------------------------------------- 大小与空间


def _files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for current, _dirs, names in os.walk(root):
        for name in names:
            yield Path(current) / name


def tree_size(paths: Iterable[Path]) -> int:
    total = 0
    for root in paths:
        for path in _files(root):
            with contextlib.suppress(OSError):
                total += path.stat().st_size
    return total


def free_bytes(directory: Path) -> int:
    return shutil.disk_usage(directory).free


def _check_space(target: Path, needed: int) -> None:
    free = free_bytes(target)
    if free < needed:
        raise StorageError(StorageErrorCode.NO_SPACE, f"需要 {needed} 字节，可用 {free} 字节")


# ---------------------------------------------------------------- 复制文件


def _copy_file(source: Path, target: Path, progress: Progress) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("wb") as writer:
        while chunk := reader.read(CHUNK_BYTES):
            writer.write(chunk)
            progress(len(chunk))
    shutil.copystat(source, target)


def _backup_sqlite(source: Path, target: Path, progress: Progress) -> None:
    """用在线备份接口复制正在使用的 SQLite 库；源文件不是有效的库时按普通文件复制。"""
    size = source.stat().st_size
    try:
        reader = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
        try:
            writer = sqlite3.connect(target)
            try:
                reader.backup(writer)
            finally:
                writer.close()
        finally:
            reader.close()
    except sqlite3.Error as exc:
        logger.warning("在线备份 %s 失败（%s），改为直接复制文件", source.name, exc)
        target.unlink(missing_ok=True)
        _copy_file(source, target, progress)
        return
    progress(size)


def _sqlite_side_file(path: Path) -> bool:
    return any(path.name.endswith(".sqlite3" + suffix) for suffix in SQLITE_SIDE_SUFFIXES)


def _copy_tree(source: Path, target: Path, progress: Progress) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for current, dirs, names in os.walk(source):
        relative = Path(current).relative_to(source)
        for name in dirs:
            (target / relative / name).mkdir(parents=True, exist_ok=True)
        for name in names:
            _copy_file(Path(current) / name, target / relative / name, progress)


# ---------------------------------------------------------------- 数据目录


@dataclass(frozen=True, slots=True)
class CopyPlan:
    """要复制的顶层条目与总字节数。"""

    source: Path
    target: Path
    entries: tuple[Path, ...]
    total_bytes: int


def plan_data_copy(source: Path, target: Path, *, exclude: Iterable[Path] = ()) -> CopyPlan:
    """列出数据目录里要复制的内容，检查目标里没有同名条目、磁盘空间足够。

    exclude 是不复制的路径（例如放在数据目录里的自定义模型目录，配置里记的是它的绝对路径，复制一份没有用）。
    """
    skipped = [Path(path) for path in exclude]
    entries = sorted(
        entry
        for entry in source.iterdir()
        if entry.name not in COPY_SKIPPED
        and not _sqlite_side_file(entry)
        and not any(same_path(entry, path) for path in skipped)
    )
    occupied = [entry.name for entry in entries if (target / entry.name).exists()]
    if occupied:
        raise StorageError(StorageErrorCode.OCCUPIED, "、".join(occupied))
    total = tree_size(entries)
    _check_space(target, total)
    return CopyPlan(source, target, tuple(entries), total)


def copy_data(plan: CopyPlan, progress: Progress) -> None:
    """按计划复制。失败时删除已经复制到目标里的条目，再把异常抛出。"""
    created: list[Path] = []
    try:
        for entry in plan.entries:
            destination = plan.target / entry.name
            created.append(destination)
            if entry.is_dir():
                _copy_tree(entry, destination, progress)
            elif entry.suffix == ".sqlite3":
                _backup_sqlite(entry, destination, progress)
            else:
                _copy_file(entry, destination, progress)
    except BaseException:
        remove_paths(created)
        raise


def remove_paths(paths: Iterable[Path]) -> list[Path]:
    """删除文件或目录树，返回没能删掉的路径。"""
    left: list[Path] = []
    for path in paths:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            left.append(path)
    return left


# ---------------------------------------------------------------- 模型目录


def _same_volume(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _cross_device(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == _ERROR_NOT_SAME_DEVICE


def _is_empty_dir(path: Path) -> bool:
    return path.is_dir() and not any(path.iterdir())


@dataclass
class ModelsMove:
    """把模型目录下的 MODEL_SUBDIRS 从 source 移到 target。用 ``plan_models_move`` 创建。

    run 失败时已经自动回滚；run 成功之后、配置写好之前出错，调用 rollback；配置写好之后调用 commit。
    """

    source: Path
    target: Path
    items: tuple[str, ...]
    same_volume: bool
    total_bytes: int
    renamed: list[str] = field(default_factory=lambda: list[str]())
    copied: list[str] = field(default_factory=lambda: list[str]())

    def run(self, progress: Progress) -> None:
        current: Path | None = None
        try:
            for name in self.items:
                source, destination = self.source / name, self.target / name
                if _is_empty_dir(destination):
                    destination.rmdir()
                if self.same_volume:
                    try:
                        os.replace(source, destination)
                    except OSError as exc:
                        if not _cross_device(exc):
                            raise
                    else:
                        self.renamed.append(name)
                        continue
                current = destination
                _copy_tree(source, destination, progress)
                current = None
                self.copied.append(name)
        except BaseException:
            if current is not None:
                remove_paths([current])
            self.rollback()
            raise

    def rollback(self) -> list[Path]:
        """改名的改回去，复制出来的删掉。返回没能恢复的路径（只记日志，旧目录里的内容仍然完整）。"""
        left: list[Path] = []
        for name in reversed(self.renamed):
            try:
                os.replace(self.target / name, self.source / name)
            except OSError as exc:
                logger.error("模型子目录 %s 没能移回原处：%s", name, describe_os_error(exc))
                left.append(self.target / name)
        left.extend(remove_paths(self.target / name for name in self.copied))
        self.renamed.clear()
        self.copied.clear()
        return left

    def commit(self) -> list[Path]:
        """配置已经指向新目录：删除复制过去的子目录的旧副本。返回没能删掉的路径。"""
        left = remove_paths(self.source / name for name in self.copied)
        self.copied.clear()
        self.renamed.clear()
        return left


def plan_models_move(source: Path, target: Path) -> ModelsMove:
    """检查目标里没有同名的非空子目录、跨卷时磁盘空间足够，给出移动计划。target 必须已经存在。"""
    items = tuple(name for name in MODEL_SUBDIRS if (source / name).exists())
    occupied = [name for name in items if (target / name).exists() and not _is_empty_dir(target / name)]
    if occupied:
        raise StorageError(StorageErrorCode.OCCUPIED, "、".join(occupied))
    same_volume = _same_volume(source, target) if items else True
    total = 0 if same_volume else tree_size(source / name for name in items)
    if total:
        _check_space(target, total)
    return ModelsMove(source, target, items, same_volume, total)


# ---------------------------------------------------------------- 旧布局迁移


def migrate_legacy_layout(data_dir: Path, models_root: Path) -> list[str]:
    """旧版本把 OCR 引擎的运行环境放在 <数据目录>/engines，显卡运行库放在 <数据目录>/runtimes。
    启动时把它们改名到模型目录下；新位置已有内容时不动。返回迁移了的子目录名。改名失败只记警告。
    """
    moved: list[str] = []
    for name in ("engines", "runtimes"):
        old, new = data_dir / name, models_root / name
        if not old.is_dir() or same_path(old, new) or new.exists():
            continue
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old, new)
        except OSError as exc:
            logger.warning("没能把 %s 移到模型目录 %s：%s", old, models_root, describe_os_error(exc))
            continue
        moved.append(name)
        logger.info("已把 %s 移到模型目录 %s", old, new)
    return moved


# ---------------------------------------------------------------- 卸载时的清理范围


def data_entries(data_dir: Path) -> list[Path]:
    """数据目录里由程序创建的条目（默认模型目录单独处理，见 model_entries）。"""
    found: list[Path] = [data_dir / name for name in DATA_DIRS if (data_dir / name).exists()]
    for pattern in DATA_FILE_PATTERNS:
        found.extend(sorted(data_dir.glob(pattern)))
    return found


def model_entries(models_root: Path) -> list[Path]:
    """模型目录里由程序创建的子目录。"""
    return [models_root / name for name in MODEL_SUBDIRS if (models_root / name).exists()]


def default_models_root(data_dir: Path) -> Path:
    return data_dir / MODELS_DIR_NAME


__all__ = [
    "COPY_SKIPPED",
    "CopyPlan",
    "ModelsMove",
    "Probe",
    "StorageError",
    "copy_data",
    "data_entries",
    "default_models_root",
    "describe_os_error",
    "free_bytes",
    "is_inside",
    "migrate_legacy_layout",
    "model_entries",
    "plan_data_copy",
    "plan_models_move",
    "probe_writable",
    "remove_created",
    "remove_paths",
    "same_path",
    "tree_size",
]
