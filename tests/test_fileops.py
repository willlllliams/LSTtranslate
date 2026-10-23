"""目录发布的原子性、错误边界及 Windows 真实目录句柄占用。"""

import ctypes
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import cast

import pytest

from lst import _fileops
from lst._fileops import publish_directory


def prepared(root: Path) -> tuple[Path, Path]:
    source, target = root / "prepared", root / "installed"
    source.mkdir()
    (source / "payload").write_bytes(b"verified contents")
    return source, target


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_transient_windows_error_retries_the_same_atomic_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    source, target = prepared(tmp_path)
    replace = _fileops.os.replace
    calls: list[tuple[Path, Path]] = []
    delays: list[float] = []

    def locked(first: Path, second: Path) -> None:
        calls.append((first, second))
        if len(calls) < 3:
            raise PermissionError(13, "locked", str(first), winerror)
        replace(first, second)

    monkeypatch.setattr(_fileops.os, "replace", locked)
    monkeypatch.setattr(_fileops.time, "sleep", delays.append)
    publish_directory(source, target)
    assert calls == [(source, target)] * 3
    assert delays == [0.025, 0.05]
    assert not source.exists()
    assert (target / "payload").read_bytes() == b"verified contents"


def test_persistent_error_preserves_the_source_and_original_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = prepared(tmp_path)
    error = PermissionError(13, "locked", str(source), 5)
    delays: list[float] = []

    def denied(_first: Path, _second: Path) -> None:
        raise error

    monkeypatch.setattr(_fileops.os, "replace", denied)
    monkeypatch.setattr(_fileops.time, "sleep", delays.append)
    with pytest.raises(PermissionError) as raised:
        publish_directory(source, target)
    assert raised.value is error
    assert len(delays) == 6 and sum(delays) == pytest.approx(1.575)
    assert source.is_dir() and not target.exists()
    assert (source / "payload").read_bytes() == b"verified contents"


@pytest.mark.parametrize("error", [PermissionError(13, "permissions"), OSError(112, "disk full"),
                                 FileNotFoundError(2, "gone")])  # fmt: skip
def test_other_errors_do_not_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    source, target = prepared(tmp_path)
    delays: list[float] = []

    def denied(_first: Path, _second: Path) -> None:
        raise error

    monkeypatch.setattr(_fileops.os, "replace", denied)
    monkeypatch.setattr(_fileops.time, "sleep", delays.append)
    with pytest.raises(OSError) as raised:
        publish_directory(source, target)
    assert raised.value is error and not delays
    assert source.is_dir() and not target.exists()


def test_existing_nonempty_destination_is_preserved(tmp_path: Path) -> None:
    source, target = prepared(tmp_path)
    target.mkdir()
    (target / "existing").write_bytes(b"existing installation")
    with pytest.raises(OSError):
        publish_directory(source, target)
    assert (target / "existing").read_bytes() == b"existing installation"
    assert (source / "payload").read_bytes() == b"verified contents"


@contextmanager
def directory_lock(path: Path) -> Generator[Callable[[], None]]:
    """打开目录且不允许 FILE_SHARE_DELETE，使真实内核拒绝改名；返回幂等释放函数。"""
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    dll.CreateFileW.restype = wintypes.HANDLE
    dll.CloseHandle.argtypes = (wintypes.HANDLE,)
    dll.CloseHandle.restype = wintypes.BOOL
    # FILE_LIST_DIRECTORY 读取权限，READ|WRITE 共享，OPEN_EXISTING，BACKUP_SEMANTICS。
    handle = cast(int, dll.CreateFileW(str(path), 1, 0x3, None, 3, 0x02000000, None))
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    closed = False

    def close() -> None:
        nonlocal closed
        if not closed:
            if not dll.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            closed = True

    try:
        yield close
    finally:
        close()


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows 目录句柄")
def test_real_windows_lock_can_be_released_during_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = prepared(tmp_path)
    delays: list[float] = []
    with directory_lock(source) as release:

        def wait(delay: float) -> None:
            delays.append(delay)
            release()

        monkeypatch.setattr(_fileops.time, "sleep", wait)
        publish_directory(source, target)
    assert delays == [0.025]  # 首次改名由真实内核拒绝，释放句柄后的下一次改名成功。
    assert not source.exists() and (target / "payload").read_bytes() == b"verified contents"


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows 目录句柄")
def test_real_persistent_windows_lock_preserves_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = prepared(tmp_path)
    delays: list[float] = []
    monkeypatch.setattr(_fileops.time, "sleep", delays.append)
    with directory_lock(source), pytest.raises(PermissionError) as raised:
        publish_directory(source, target)
    assert raised.value.winerror in {5, 32, 33}
    assert len(delays) == 6 and sum(delays) == pytest.approx(1.575)
    assert not target.exists() and (source / "payload").read_bytes() == b"verified contents"
    publish_directory(source, target)
    assert (target / "payload").read_bytes() == b"verified contents"
