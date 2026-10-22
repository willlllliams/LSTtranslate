"""存储位置的磁盘操作与存储位置模块（需求 CFG-06、CFG-07、MOD-04）。

被测的是 lst/app/storage.py 与 lst/app/storage_module.py。

磁盘操作都在临时目录里真实执行：写入检查实际创建文件，SQLite 库在另一个连接打开、WAL 里还有未合并的数据时
复制，模型移动覆盖同卷改名、跨卷复制、失败回滚与提交。存储位置模块用记录调用的组合根替身，检查停下、移动、
写配置、恢复的顺序，以及各种失败时数据目录与模型目录保持原样。
"""

import asyncio
import json
import logging
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

import lst.app.storage as storage
from lst.app.paths import MODEL_SUBDIRS, AppPaths, DataDirChoice, read_location, write_location
from lst.app.storage import (
    StorageError,
    copy_data,
    data_entries,
    migrate_legacy_layout,
    model_entries,
    plan_data_copy,
    plan_models_move,
    probe_writable,
)
from lst.app.storage_module import StorageModule
from lst.contracts.storage import (
    ChangeDataDir,
    ChangeModelsDir,
    DataDirSource,
    RestartApp,
    StorageErrorCode,
    StorageKind,
    StorageLocations,
)
from lst.kernel import EventBus, ModuleContext

# ---------------------------------------------------------------- 写入检查


def test_probe_creates_the_directory_and_leaves_no_files(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b"
    probe = probe_writable(target)
    assert probe.error is None
    assert probe.created == (target, tmp_path / "a")  # 从深到浅
    assert target.is_dir() and list(target.iterdir()) == []


def test_probe_rejects_a_file_and_a_path_below_a_file(tmp_path: Path) -> None:
    blocker = tmp_path / "file.txt"
    blocker.write_text("x", encoding="utf-8")
    assert probe_writable(blocker).error is not None
    below = probe_writable(blocker / "sub")
    assert below.error is not None and below.created == ()
    assert blocker.read_text(encoding="utf-8") == "x"


@contextmanager
def deny_writes(directory: Path) -> Generator[None]:
    """用 ACL 拒绝所有人在 directory 里创建文件（只改测试自己建的临时目录），结束后撤销。"""
    everyone = "*S-1-1-0"
    subprocess.run(
        ["icacls", str(directory), "/deny", f"{everyone}:(OI)(CI)(WD,AD)"], check=True, capture_output=True
    )
    try:
        yield
    finally:
        subprocess.run(["icacls", str(directory), "/remove:d", everyone], check=True, capture_output=True)


@pytest.mark.skipif(sys.platform != "win32", reason="ACL 只在 Windows 上测试")
def test_probe_reports_access_denied_from_the_real_acl(tmp_path: Path) -> None:
    """ACL 拒绝写入时实际写一次才知道：目录存在、可以列出，写入检查报“拒绝访问”（errno 13 或 WinError 5）。

    源码版以 Low 完整性运行时写用户目录也是这个错误（架构文档 2.9.5 节），检查方式相同。
    """
    target = tmp_path / "locked"
    target.mkdir()
    with deny_writes(target):
        assert target.is_dir() and list(target.iterdir()) == []
        probe = probe_writable(target)
        assert probe.error is not None and ("13" in probe.error or "5" in probe.error)
        deep = probe_writable(target / "sub")
        assert deep.error is not None
    assert probe_writable(target).error is None


# ---------------------------------------------------------------- 复制数据目录


def make_data_dir(root: Path) -> sqlite3.Connection:
    """一个正在使用的数据目录：配置、日志、缓存库（另一个连接开着，WAL 里有未合并的行）、临时文件与锁文件。"""
    (root / "config").mkdir(parents=True)
    (root / "config" / "settings.toml").write_text('[ui]\nlanguage = "en"\n', encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "main.log").write_text("line\n" * 1000, encoding="utf-8")
    (root / "tmp").mkdir()
    (root / "tmp" / "junk.bin").write_bytes(b"0" * 100)
    (root / "lsttranslate.lock").write_text("123", encoding="utf-8")
    (root / "location.json").write_text("{}", encoding="utf-8")
    (root / "ui_state.ini").write_text("[a]\nb=1\n", encoding="utf-8")
    (root / "models" / "nmt").mkdir(parents=True)
    (root / "models" / "nmt" / "m.argosmodel").write_bytes(b"model" * 100)
    connection = sqlite3.connect(root / "cache.sqlite3")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE t (v TEXT)")
    connection.executemany("INSERT INTO t VALUES (?)", [(f"row{i}",) for i in range(50)])
    connection.commit()
    assert (root / "cache.sqlite3-wal").stat().st_size > 0
    return connection


def test_copy_data_takes_a_consistent_snapshot_of_open_databases(tmp_path: Path) -> None:
    source, target = tmp_path / "old", tmp_path / "new"
    connection = make_data_dir(source)
    try:
        target.mkdir()
        plan = plan_data_copy(source, target)
        names = [entry.name for entry in plan.entries]
        assert names == ["cache.sqlite3", "config", "logs", "models", "ui_state.ini"]
        reported: list[int] = []
        copy_data(plan, reported.append)
    finally:
        connection.close()
    assert sum(reported) >= plan.total_bytes > 0
    copied = sqlite3.connect(target / "cache.sqlite3")
    try:
        assert copied.execute("SELECT count(*) FROM t").fetchone() == (50,)  # 只在 WAL 里的行也在
    finally:
        copied.close()
    assert (target / "config" / "settings.toml").read_text(encoding="utf-8").startswith("[ui]")
    assert (target / "models" / "nmt" / "m.argosmodel").stat().st_size == 500
    for skipped in ("tmp", "lsttranslate.lock", "location.json", "cache.sqlite3-wal", "cache.sqlite3-shm"):
        assert not (target / skipped).exists(), skipped
    assert (source / "cache.sqlite3").is_file()  # 复制不动原目录


def test_copy_excludes_a_custom_model_dir_inside_the_data_dir(tmp_path: Path) -> None:
    source = tmp_path / "old"
    (source / "config").mkdir(parents=True)
    (source / "my-models" / "nmt").mkdir(parents=True)
    plan = plan_data_copy(source, tmp_path, exclude=[source / "my-models"])
    assert [entry.name for entry in plan.entries] == ["config"]


def test_copy_refuses_a_target_that_already_has_data(tmp_path: Path) -> None:
    source, target = tmp_path / "old", tmp_path / "new"
    make_data_dir(source).close()
    (target / "config").mkdir(parents=True)
    with pytest.raises(StorageError) as caught:
        plan_data_copy(source, target)
    assert caught.value.code is StorageErrorCode.OCCUPIED and "config" in caught.value.detail


def test_copy_refuses_when_the_drive_is_too_small(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = tmp_path / "old", tmp_path / "new"
    make_data_dir(source).close()
    target.mkdir()

    def tiny_drive(_path: Path) -> int:
        return 10

    monkeypatch.setattr(storage, "free_bytes", tiny_drive)
    with pytest.raises(StorageError) as caught:
        plan_data_copy(source, target)
    assert caught.value.code is StorageErrorCode.NO_SPACE


def failing_after(count: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """第 count 个文件之后的复制抛 OSError（模拟磁盘拔出、写满）。"""
    original = storage._copy_file  # pyright: ignore[reportPrivateUsage]
    calls = {"n": 0}

    def copy(source: Path, target: Path, progress: Callable[[int], None]) -> None:
        calls["n"] += 1
        if calls["n"] > count:
            raise OSError(28, "磁盘空间不足")
        original(source, target, progress)

    monkeypatch.setattr(storage, "_copy_file", copy)


def test_failed_copy_removes_what_it_copied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = tmp_path / "old", tmp_path / "new"
    make_data_dir(source).close()
    target.mkdir()
    plan = plan_data_copy(source, target)
    failing_after(1, monkeypatch)
    with pytest.raises(OSError):
        copy_data(plan, lambda _n: None)
    assert list(target.iterdir()) == []


# ---------------------------------------------------------------- 移动模型目录


def make_models(root: Path) -> None:
    for name in MODEL_SUBDIRS:
        (root / name / "inner").mkdir(parents=True)
        (root / name / "inner" / f"{name}.bin").write_bytes(name.encode() * 200)


def assert_models(root: Path) -> None:
    for name in MODEL_SUBDIRS:
        assert (root / name / "inner" / f"{name}.bin").read_bytes() == name.encode() * 200, name


def test_move_on_the_same_volume_renames_and_rolls_back(tmp_path: Path) -> None:
    source, target = tmp_path / "models", tmp_path / "elsewhere"
    make_models(source)
    target.mkdir()
    (target / "nmt").mkdir()  # 空的同名子目录不算冲突
    move = plan_models_move(source, target)
    assert move.same_volume and move.total_bytes == 0 and move.items == MODEL_SUBDIRS
    move.run(lambda _n: None)
    assert_models(target)
    assert not any((source / name).exists() for name in MODEL_SUBDIRS)
    move.rollback()  # 例如写配置失败
    assert_models(source)
    assert list(target.iterdir()) == []


def test_move_across_volumes_copies_then_deletes_on_commit(tmp_path: Path) -> None:
    source, target = tmp_path / "models", tmp_path / "other-drive"
    make_models(source)
    target.mkdir()
    move = plan_models_move(source, target)
    move.same_volume = False  # 模拟目标在另一个卷上：只能复制
    reported: list[int] = []
    move.run(reported.append)
    assert move.copied == list(MODEL_SUBDIRS)
    assert sum(reported) == sum(len(name) * 200 for name in MODEL_SUBDIRS)
    assert_models(source)  # 配置写好之前旧目录保持完整
    assert_models(target)
    assert move.commit() == []
    assert not any((source / name).exists() for name in MODEL_SUBDIRS)


def test_failed_copy_across_volumes_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = tmp_path / "models", tmp_path / "other-drive"
    make_models(source)
    target.mkdir()
    move = plan_models_move(source, target)
    move.same_volume = False
    failing_after(3, monkeypatch)
    with pytest.raises(OSError):
        move.run(lambda _n: None)
    assert_models(source)
    assert list(target.iterdir()) == []


def test_failed_rename_rolls_back_the_ones_already_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第三个子目录改名失败（例如文件被占用）时，前两个改回原处。"""
    source, target = tmp_path / "models", tmp_path / "elsewhere"
    make_models(source)
    target.mkdir()
    move = plan_models_move(source, target)
    real_replace = storage.os.replace
    moved: list[str] = []

    def replace(src: object, dst: object) -> None:
        if Path(str(src)).parent == source and len(moved) == 2:
            raise PermissionError(13, "另一个程序正在使用此文件")
        if Path(str(src)).parent == source:
            moved.append(Path(str(src)).name)
        real_replace(src, dst)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(storage.os, "replace", replace)
    with pytest.raises(PermissionError):
        move.run(lambda _n: None)
    monkeypatch.setattr(storage.os, "replace", real_replace)
    assert_models(source)
    assert list(target.iterdir()) == []


def test_move_refuses_non_empty_subdirectories_in_the_target(tmp_path: Path) -> None:
    source, target = tmp_path / "models", tmp_path / "elsewhere"
    make_models(source)
    (target / "dict").mkdir(parents=True)
    (target / "dict" / "x.tsv.gz").write_bytes(b"x")
    with pytest.raises(StorageError) as caught:
        plan_models_move(source, target)
    assert caught.value.code is StorageErrorCode.OCCUPIED and caught.value.detail == "dict"


def test_legacy_engines_and_runtimes_move_into_the_models_dir(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "engines" / "paddle" / "site").mkdir(parents=True)
    (data / "runtimes" / "cublas12").mkdir(parents=True)
    (tmp_path / "custom" / "runtimes").mkdir(parents=True)  # 新位置已有内容：不动
    assert migrate_legacy_layout(data, data / "models") == ["engines", "runtimes"]
    assert (data / "models" / "engines" / "paddle" / "site").is_dir()
    assert (data / "models" / "runtimes" / "cublas12").is_dir()
    assert migrate_legacy_layout(data, data / "models") == []  # 再次启动：没有要迁移的
    (data / "runtimes").mkdir()
    assert migrate_legacy_layout(data, tmp_path / "custom") == []
    assert (data / "runtimes").is_dir()


def test_cleanup_scope_lists_only_what_the_program_created(tmp_path: Path) -> None:
    data = tmp_path / "data"
    make_data_dir(data).close()
    (data / "my-own-notes.txt").write_text("别删", encoding="utf-8")
    (data / "stats.sqlite3.corrupt-20261022").write_bytes(b"x")  # 损坏后改名保留的库
    names = sorted(path.name for path in data_entries(data))
    expected = ["cache.sqlite3", "config", "logs", "lsttranslate.lock", "stats.sqlite3.corrupt-20261022"]
    assert names == sorted([*expected, "tmp", "ui_state.ini"])  # 最后一个连接关闭时 -wal、-shm 已删除
    assert "my-own-notes.txt" not in names and "location.json" not in names
    make_models(tmp_path / "models")
    (tmp_path / "models" / "photos").mkdir()
    assert sorted(p.name for p in model_entries(tmp_path / "models")) == sorted(MODEL_SUBDIRS)


# ---------------------------------------------------------------- 存储位置模块


class FakeHost:
    """记录调用的组合根替身。save_error 不为空时写配置失败。"""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.calls: list[str] = []
        self.save_error: str | None = None
        self.saved: list[str] = []
        self.restarted = False

    async def suspend_models(self) -> None:
        self.calls.append("suspend")

    async def resume_models(self, models_dir: Path | None) -> None:
        self.calls.append(f"resume:{models_dir}")
        self.paths = self.paths.with_models_dir(models_dir)

    async def save_models_dir(self, value: str) -> str | None:
        self.calls.append(f"save:{value}")
        if self.save_error is None:
            self.saved.append(value)
        return self.save_error

    def request_restart(self) -> None:
        self.restarted = True


class Rig:
    def __init__(self, bus: EventBus, module: StorageModule, host: FakeHost, location: Path) -> None:
        self.bus = bus
        self.module = module
        self.host = host
        self.location = location
        self.states: list[StorageLocations] = []
        bus.subscribe(StorageLocations, self.states.append)

    @property
    def latest(self) -> StorageLocations:
        state = self.bus.latest(StorageLocations)
        assert state is not None
        return state

    async def send(self, command: object) -> StorageLocations:
        self.bus.send(command)  # pyright: ignore[reportArgumentType]
        await self.bus.drain()
        return self.latest


async def make_rig(tmp_path: Path, source: DataDirSource = DataDirSource.CHOSEN) -> Rig:
    data = tmp_path / "data"
    connection = make_data_dir(data)
    connection.close()
    make_models(data / "models")
    host = FakeHost(AppPaths(data))
    location = tmp_path / "roaming" / "location.json"
    module = StorageModule(lambda: host.paths, DataDirChoice(data, source, location), host)
    bus = EventBus(asyncio.get_running_loop())
    await module.start(ModuleContext(bus, logging.getLogger("test.storage")))
    return Rig(bus, module, host, location)


@pytest.fixture
async def rig(tmp_path: Path) -> Rig:
    return await make_rig(tmp_path)


async def test_publishes_the_current_locations(rig: Rig, tmp_path: Path) -> None:
    state = rig.latest
    assert state.data_dir == str(tmp_path / "data")
    assert state.models_dir == str(tmp_path / "data" / "models") and state.models_default
    assert (state.data_source, state.pending_data_dir, state.task, state.failure) == (
        DataDirSource.CHOSEN,
        None,
        None,
        None,
    )


async def test_change_data_dir_copies_then_writes_the_location_file(rig: Rig, tmp_path: Path) -> None:
    target = tmp_path / "new-data"
    state = await rig.send(ChangeDataDir(str(target), copy=True))
    assert state.failure is None and state.task is None
    assert state.pending_data_dir == str(target)
    assert state.data_dir == str(tmp_path / "data")  # 重启前不变
    assert read_location(rig.location) == target
    assert (target / "config" / "settings.toml").is_file()
    assert_models(target / "models")  # 默认位置的模型随数据一起复制
    assert any(s.task is not None and s.task.kind is StorageKind.DATA for s in rig.states)  # 报告过进度
    assert rig.host.calls == []  # 数据目录的更改不停任何进程

    # 选回当前目录：撤销还没生效的更改，位置文件改回当前目录。
    state = await rig.send(ChangeDataDir(str(tmp_path / "data"), copy=False))
    assert state.pending_data_dir is None
    assert read_location(rig.location) == tmp_path / "data"


async def test_change_data_dir_without_copy_only_writes_the_location(rig: Rig, tmp_path: Path) -> None:
    target = tmp_path / "empty"
    state = await rig.send(ChangeDataDir(str(target), copy=False))
    assert state.pending_data_dir == str(target)
    assert list(target.iterdir()) == []
    assert json.loads(rig.location.read_text(encoding="utf-8")) == {"data_dir": str(target)}


async def test_failed_copy_does_not_switch(rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "new-data"
    failing_after(2, monkeypatch)
    state = await rig.send(ChangeDataDir(str(target), copy=True))
    assert state.failure is not None and state.failure.code is StorageErrorCode.COPY_FAILED
    assert state.failure.kind is StorageKind.DATA and state.task is None
    assert state.pending_data_dir is None and not rig.location.exists()
    assert not target.exists()  # 复制了一半的内容与新建的目录都删掉了


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("relative/dir", StorageErrorCode.RELATIVE),
        ("{data}/sub", StorageErrorCode.NESTED),
        ("{file}", StorageErrorCode.NOT_WRITABLE),
        ("{occupied}", StorageErrorCode.OCCUPIED),
    ],
)
async def test_invalid_data_dirs_are_refused(
    rig: Rig, tmp_path: Path, target: str, code: StorageErrorCode
) -> None:
    blocker = tmp_path / "file.txt"
    blocker.write_text("x", encoding="utf-8")
    (tmp_path / "occupied" / "config").mkdir(parents=True)
    path = target.format(data=tmp_path / "data", file=blocker, occupied=tmp_path / "occupied")
    state = await rig.send(ChangeDataDir(path, copy=True))
    assert state.failure is not None and state.failure.code is code
    assert state.pending_data_dir is None and not rig.location.exists()


async def test_data_dir_fixed_by_argument_cannot_change(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, DataDirSource.ARGUMENT)
    state = await rig.send(ChangeDataDir(str(tmp_path / "x"), copy=False))
    assert state.failure is not None and state.failure.code is StorageErrorCode.FIXED
    assert not (tmp_path / "x").exists() and not rig.location.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="ACL 只在 Windows 上测试")
async def test_unwritable_data_dir_is_refused_with_the_system_reason(rig: Rig, tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    with deny_writes(locked):
        state = await rig.send(ChangeDataDir(str(locked), copy=True))
    assert state.failure is not None and state.failure.code is StorageErrorCode.NOT_WRITABLE
    assert state.failure.detail  # 系统给出的原因，例如“拒绝访问。（5）”
    assert not rig.location.exists() and list(locked.iterdir()) == []


async def test_change_models_dir_stops_moves_saves_and_resumes(rig: Rig, tmp_path: Path) -> None:
    target = tmp_path / "big-drive" / "models"
    state = await rig.send(ChangeModelsDir(str(target), move=True))
    assert state.failure is None and state.task is None
    assert rig.host.calls == ["suspend", f"save:{target}", f"resume:{target}"]
    assert_models(target)
    assert not any((tmp_path / "data" / "models" / name).exists() for name in MODEL_SUBDIRS)
    assert state.models_dir == str(target) and not state.models_default

    # 恢复默认位置：配置里写空串。
    state = await rig.send(ChangeModelsDir("", move=True))
    assert rig.host.calls[-2:] == ["save:", "resume:None"]
    assert_models(tmp_path / "data" / "models")
    assert state.models_default


async def test_change_models_dir_without_move_keeps_the_files(rig: Rig, tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    state = await rig.send(ChangeModelsDir(str(target), move=False))
    assert state.failure is None and rig.host.saved == [str(target)]
    assert_models(tmp_path / "data" / "models")
    assert list(target.iterdir()) == []


async def test_config_write_failure_moves_the_models_back(rig: Rig, tmp_path: Path) -> None:
    rig.host.save_error = "写入配置文件失败，修改未生效（PermissionError）"
    target = tmp_path / "big-drive"
    state = await rig.send(ChangeModelsDir(str(target), move=True))
    assert state.failure is not None and state.failure.code is StorageErrorCode.SAVE_FAILED
    assert "PermissionError" in state.failure.detail
    assert rig.host.calls == ["suspend", f"save:{target}", "resume:None"]  # 按原目录恢复
    assert_models(tmp_path / "data" / "models")
    assert not target.exists()


async def test_move_failure_resumes_with_the_old_dir(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "other"

    def other_volume(_a: Path, _b: Path) -> bool:
        return False  # 目标在另一个卷上：只能复制

    monkeypatch.setattr(storage, "_same_volume", other_volume)
    failing_after(2, monkeypatch)
    state = await rig.send(ChangeModelsDir(str(target), move=True))
    assert state.failure is not None and state.failure.code is StorageErrorCode.MOVE_FAILED
    assert rig.host.calls == ["suspend", "resume:None"]  # 没有写配置
    assert_models(tmp_path / "data" / "models")
    assert not target.exists()
    assert state.models_default


async def test_nested_models_dir_is_refused_before_stopping_anything(rig: Rig, tmp_path: Path) -> None:
    state = await rig.send(ChangeModelsDir(str(tmp_path / "data" / "models" / "nmt" / "x"), move=True))
    assert state.failure is not None and state.failure.code is StorageErrorCode.NESTED
    assert rig.host.calls == []


async def test_a_second_change_while_busy_is_refused(rig: Rig, tmp_path: Path) -> None:
    gate = asyncio.Event()
    original = rig.host.suspend_models

    async def slow_suspend() -> None:
        await original()
        await gate.wait()

    rig.host.suspend_models = slow_suspend  # type: ignore[method-assign]
    rig.bus.send(ChangeModelsDir(str(tmp_path / "a"), move=True))
    await asyncio.sleep(0.05)
    rig.bus.send(ChangeDataDir(str(tmp_path / "b"), copy=False))
    await asyncio.sleep(0.05)
    busy = rig.latest
    assert busy.failure is not None and busy.failure.code is StorageErrorCode.BUSY
    assert busy.task is not None and busy.task.kind is StorageKind.MODELS
    gate.set()
    await rig.bus.drain()
    assert rig.latest.models_dir == str(tmp_path / "a")


async def test_restart_command_asks_the_host(rig: Rig) -> None:
    await rig.send(RestartApp())
    assert rig.host.restarted


def test_location_file_round_trip(tmp_path: Path) -> None:
    location = tmp_path / "x" / "location.json"
    write_location(location, tmp_path / "d")
    assert read_location(location) == tmp_path / "d"
