"""数据目录、日志初始化、单实例的测试。"""

import ctypes
import json
import logging
import sys
import threading
import uuid
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from lst.app.logging_setup import setup_logging
from lst.app.paths import (
    DATA_DIR_ENV,
    PROGRAM_DIR,
    AppPaths,
    DataDirChoice,
    choose_data_dir,
    location_file,
    write_location,
)
from lst.app.single_instance import RunningMutex, SingleInstance
from lst.contracts.storage import DataDirSource


def test_paths_default_to_program_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    paths = AppPaths.resolve(location=tmp_path / "location.json")
    assert paths.data_dir == PROGRAM_DIR / "data"
    assert paths.config_file == PROGRAM_DIR / "data" / "config" / "settings.toml"
    assert paths.nmt_models_dir.parent.name == "models"
    assert location_file() == PROGRAM_DIR / "data" / "location.json"  # 源码版的位置文件


def test_paths_env_and_argument_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "from-env"))
    location = tmp_path / "location.json"
    assert AppPaths.resolve(location=location).data_dir == (tmp_path / "from-env").resolve()
    assert AppPaths.resolve(tmp_path / "arg", location=location).data_dir == (tmp_path / "arg").resolve()


def test_data_dir_priority_argument_env_location_default(tmp_path: Path) -> None:
    """数据目录的优先级：--data-dir → LST_DATA_DIR → 位置文件 → 默认位置（需求 CFG-06）。"""
    location = tmp_path / "pointer" / "location.json"
    chosen = tmp_path / "chosen"
    write_location(location, chosen)
    assert json.loads(location.read_text(encoding="utf-8")) == {"data_dir": str(chosen)}
    assert not location.with_name("location.json.part").exists()
    env = {DATA_DIR_ENV: str(tmp_path / "env")}

    by_arg = choose_data_dir(tmp_path / "arg", environ=env, location=location)
    assert (by_arg.path, by_arg.source) == ((tmp_path / "arg").resolve(), DataDirSource.ARGUMENT)
    by_env = choose_data_dir(None, environ=env, location=location)
    assert (by_env.path, by_env.source) == ((tmp_path / "env").resolve(), DataDirSource.ENVIRONMENT)
    by_file = choose_data_dir(None, environ={}, location=location)
    assert (by_file.path, by_file.source, by_file.location) == (chosen, DataDirSource.CHOSEN, location)
    assert chosen.is_dir()  # 位置文件指向的目录在选择时创建

    write_location(location, None)  # 回到默认位置：删除位置文件
    assert not location.exists()
    default = choose_data_dir(None, environ={}, location=location)
    assert (default.path, default.source) == (PROGRAM_DIR / "data", DataDirSource.DEFAULT)
    assert DataDirSource.ARGUMENT.fixed and DataDirSource.ENVIRONMENT.fixed
    assert not DataDirSource.CHOSEN.fixed and not DataDirSource.DEFAULT.fixed


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"data_dir": 3}', '{"data_dir": "relative/dir"}', '{"data_dir": "  "}', "{}"],
)
def test_broken_location_file_falls_back_to_default(tmp_path: Path, content: str) -> None:
    location = tmp_path / "location.json"
    location.write_text(content, encoding="utf-8")
    choice = choose_data_dir(None, environ={}, location=location)
    assert (choice.path, choice.source, choice.fallback) == (
        PROGRAM_DIR / "data",
        DataDirSource.DEFAULT,
        None,
    )


def test_unusable_chosen_dir_falls_back_and_remembers_it(tmp_path: Path) -> None:
    """位置文件指向的目录无法创建（这里有一个同名文件挡在路上）时，本次用默认位置，fallback 记下原目录。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    location = tmp_path / "location.json"
    write_location(location, blocker / "data")
    choice = choose_data_dir(None, environ={}, location=location)
    assert (choice.path, choice.source, choice.fallback) == (
        PROGRAM_DIR / "data",
        DataDirSource.DEFAULT,
        blocker / "data",
    )


def test_main_falls_back_when_the_chosen_dir_cannot_hold_data(tmp_path: Path) -> None:
    """位置文件指向的目录存在、但建不了子目录时，入口同样退回默认位置；命令行指定的目录不退回。"""
    from lst.__main__ import prepare_data_dir

    chosen = tmp_path / "chosen"
    chosen.mkdir()
    (chosen / "config").write_text("挡住 config 目录", encoding="utf-8")
    choice = DataDirChoice(chosen, DataDirSource.CHOSEN, tmp_path / "location.json")
    paths, used = prepare_data_dir(choice)
    assert paths.data_dir == PROGRAM_DIR / "data" and used.fallback == chosen
    with pytest.raises(OSError):
        prepare_data_dir(DataDirChoice(chosen, DataDirSource.ARGUMENT, tmp_path / "location.json"))


def test_installed_location_file_is_outside_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lst.app import paths

    monkeypatch.setattr(paths, "INSTALL_MARKER", tmp_path / "installed.json")
    (tmp_path / "installed.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert paths.location_file() == tmp_path / "roaming" / "LSTtranslate" / "location.json"
    assert not paths.location_file().is_relative_to(paths.default_data_dir())


def test_paths_ensure_creates_directories(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path)
    paths.ensure()
    for directory in (paths.config_file.parent, paths.logs_dir, paths.tmp_dir):
        assert directory.is_dir()
    assert not paths.models_root.exists()  # 模型目录在读到配置之后才创建
    paths.ensure_models()
    for directory in (paths.nmt_models_dir, paths.ocr_models_dir, paths.dict_models_dir):
        assert directory.is_dir()


def test_models_root_holds_every_model_kind(tmp_path: Path) -> None:
    """模型目录默认是数据目录下的 models；换到别处后五类内容都跟着走，子目录名不变（需求 MOD-04）。"""
    paths = AppPaths(tmp_path / "data")
    assert paths.models_root == tmp_path / "data" / "models" == paths.default_models_dir
    moved = paths.with_models_dir(tmp_path / "big")
    assert moved.models_root == tmp_path / "big"
    assert (moved.nmt_models_dir, moved.ocr_models_dir, moved.dict_models_dir) == (
        tmp_path / "big" / "nmt",
        tmp_path / "big" / "ocr",
        tmp_path / "big" / "dict",
    )
    assert moved.engines_dir == tmp_path / "big" / "engines"
    assert moved.nmt_runtime_dir == tmp_path / "big" / "runtimes" / "cublas12"
    assert moved.config_file == paths.config_file  # 数据目录不变
    assert moved.with_models_dir("").models_dir is None


class _Redact(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = str(record.msg).replace("sk-SECRET", "***")
        return True


def test_logging_writes_file_in_background_and_applies_filter(tmp_path: Path) -> None:
    httpx_logger = logging.getLogger("httpx")
    levels_before = (logging.getLogger().level, httpx_logger.level)
    handle = setup_logging(tmp_path / "logs", process_name="unit", level=logging.DEBUG)
    try:
        handle.add_filter(_Redact())
        logger = logging.getLogger("test.app.logging")
        logger.info("key is sk-SECRET")
        httpx_logger.info("GET https://example.test/?key=abc")
        assert httpx_logger.level == logging.WARNING
    finally:
        handle.stop()
    text = (tmp_path / "logs" / "unit.log").read_text(encoding="utf-8")
    assert "key is ***" in text
    assert "sk-SECRET" not in text
    assert "example.test" not in text  # httpx 的 INFO 日志被压到 WARNING 以上
    # stop() 还原级别，同一进程里后续的测试仍能收到 httpx 的日志
    assert (logging.getLogger().level, httpx_logger.level) == levels_before


def test_single_instance_second_acquire_notifies_first(qtbot: QtBot, tmp_path: Path) -> None:
    """第二个实例在后台线程里启动：真实场景是两个进程，第一个实例的事件循环一直在运行。

    Windows 上 QLocalSocket 的写入要等对端读走才算完成，两端在同一个线程里时会互相等待，所以不能同线程测试。
    """
    name = f"lst-test-{uuid.uuid4().hex}"
    notified: list[bool] = []
    first = SingleInstance(name, tmp_path / "a.lock", lambda: notified.append(True))
    second_result: list[bool] = []
    try:
        assert first.acquire()
        sender = threading.Thread(
            target=lambda: second_result.append(
                SingleInstance(name, tmp_path / "b.lock", lambda: None).acquire()
            )
        )
        sender.start()
        qtbot.waitUntil(lambda: notified == [True] and second_result == [False], timeout=5000)
        sender.join(timeout=5)
    finally:
        first.release()


def test_single_instance_lock_blocks_concurrent_start(qtbot: QtBot, tmp_path: Path) -> None:
    lock = tmp_path / "same.lock"
    first = SingleInstance(f"lst-test-{uuid.uuid4().hex}", lock, lambda: None)
    other = SingleInstance(f"lst-test-{uuid.uuid4().hex}", lock, lambda: None)
    try:
        assert first.acquire()
        assert not other.acquire()  # 服务名不同连不上，但锁文件被占用
    finally:
        other.release()
        first.release()
    again = SingleInstance(f"lst-test-{uuid.uuid4().hex}", lock, lambda: None)
    try:
        assert again.acquire()  # 释放后可以重新获取
    finally:
        again.release()


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上有命名互斥体")
def test_running_mutex_is_visible_while_held() -> None:
    name = f"LSTtranslate.Test.{uuid.uuid4().hex}"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    synchronize = 0x00100000

    def exists() -> bool:
        handle = kernel32.OpenMutexW(synchronize, False, name)
        if handle:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return True
        return False

    assert not exists()
    mutex = RunningMutex(name)
    try:
        assert exists()
    finally:
        mutex.release()
    assert not exists()
    mutex.release()  # 重复释放没有效果


def test_installed_mode_uses_localappdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lst.app import paths

    marker = tmp_path / "installed.json"
    monkeypatch.setattr(paths, "INSTALL_MARKER", marker)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    location = tmp_path / "location.json"
    assert not paths.is_installed()
    assert AppPaths.resolve(location=location).data_dir == PROGRAM_DIR / "data"
    marker.write_text("{}", encoding="utf-8")
    assert paths.is_installed()
    assert AppPaths.resolve(location=location).data_dir == (tmp_path / "local" / "LSTtranslate").resolve()
