"""数据目录、日志初始化、单实例的测试。"""

import ctypes
import logging
import sys
import threading
import uuid
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from lst.app.logging_setup import setup_logging
from lst.app.paths import DATA_DIR_ENV, PROGRAM_DIR, AppPaths
from lst.app.single_instance import RunningMutex, SingleInstance


def test_paths_default_to_program_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    paths = AppPaths.resolve()
    assert paths.data_dir == PROGRAM_DIR / "data"
    assert paths.config_file == PROGRAM_DIR / "data" / "config" / "settings.toml"
    assert paths.nmt_models_dir.parent.name == "models"


def test_paths_env_and_argument_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "from-env"))
    assert AppPaths.resolve().data_dir == (tmp_path / "from-env").resolve()
    assert AppPaths.resolve(tmp_path / "arg").data_dir == (tmp_path / "arg").resolve()


def test_paths_ensure_creates_directories(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path)
    paths.ensure()
    for directory in (paths.config_file.parent, paths.logs_dir, paths.nmt_models_dir, paths.tmp_dir):
        assert directory.is_dir()


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
    assert not paths.is_installed()
    assert AppPaths.resolve().data_dir == PROGRAM_DIR / "data"
    marker.write_text("{}", encoding="utf-8")
    assert paths.is_installed()
    assert AppPaths.resolve().data_dir == (tmp_path / "local" / "LSTtranslate").resolve()
