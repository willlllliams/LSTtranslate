"""uv 命令行、环境变量、失败分类、Windows 语言整理与模型包解压的单元测试。"""

import asyncio
import io
import logging
import os
import sys
import tarfile
import time
from pathlib import Path

import pytest

from lst import _fileops
from lst.contracts.ocr_engines import OcrEngineErrorCode
from lst.services.ocr_engines import archive as archive_module
from lst.services.ocr_engines.archive import InvalidArchive, dir_size, extract_model
from lst.services.ocr_engines.layout import model_url
from lst.services.ocr_engines.net import safe_url, scrub
from lst.services.ocr_engines.runtime import (
    CommandResult,
    classify_uv_failure,
    failure_detail,
    requires_hashes,
    run_command,
    uv_environment,
    uv_install_args,
)
from lst.services.ocr_engines.windows import normalize_languages
from tests.services.ocr_engines.helpers import model_tar

log = logging.getLogger("tests.ocr_engines")


def test_uv_install_args_with_hashes_and_index() -> None:
    args = uv_install_args(
        Path("C:/app/tools/uv.exe"),
        Path("C:/app/runtime/python.exe"),
        Path("D:/data/site.part"),
        Path("C:/app/app/req.txt"),
        hashed=True,
        index_url="https://mirror.invalid/simple",
    )
    assert args[1:3] == ["pip", "install"]
    assert "--require-hashes" in args
    assert args[args.index("--index-url") + 1] == "https://mirror.invalid/simple"
    assert args[-2:] == ["-r", str(Path("C:/app/app/req.txt"))]


def test_uv_install_args_without_hashes() -> None:
    args = uv_install_args(Path("uv"), Path("py"), Path("t"), Path("r"), hashed=False)
    assert "--require-hashes" not in args and "--index-url" not in args


def test_uv_install_args_never_build_from_source() -> None:
    # 只装预编译的 wheel：从源码包构建要运行包里的 setup 脚本，用户机器上也没有编译器。
    args = uv_install_args(Path("uv"), Path("py"), Path("t"), Path("r"), hashed=True)
    assert "--no-build" in args
    assert "--no-config" in args


def test_requires_hashes() -> None:
    assert requires_hashes("numpy==2.3.0 \\\n    --hash=sha256:abc\n")
    assert not requires_hashes("numpy==2.3.0\n")


def test_uv_environment_drops_interfering_variables() -> None:
    env = uv_environment(
        {"PATH": "p", "HTTPS_PROXY": "http://proxy.invalid:8080", "UV_INDEX_URL": "x", "PYTHONHOME": "y",
         "VIRTUAL_ENV": "z", "PIP_INDEX_URL": "w", "uv_python": "v"},
        Path("D:/cache"),
    )  # fmt: skip
    assert env == {
        "PATH": "p",
        "HTTPS_PROXY": "http://proxy.invalid:8080",
        "UV_CACHE_DIR": str(Path("D:/cache")),
        "UV_PYTHON_DOWNLOADS": "never",
    }


@pytest.mark.parametrize(
    ("output", "code"),
    [
        (
            "error: failed to write: There is not enough space on the disk. (os error 112)",
            OcrEngineErrorCode.DISK,
        ),
        ("error: Request failed after 3 retries: operation timed out", OcrEngineErrorCode.TIMEOUT),
        (
            "error: Failed to fetch: `https://pypi.org/simple/numpy/`\n  Caused by: dns error",
            OcrEngineErrorCode.NETWORK,
        ),
        ("error: Hash mismatch for `numpy==2.3.0`", OcrEngineErrorCode.INSTALL_FAILED),
    ],
)
def test_classify_uv_failure(output: str, code: OcrEngineErrorCode) -> None:
    assert classify_uv_failure(output) is code


def test_failure_detail_scrubs_query_strings() -> None:
    detail = failure_detail(CommandResult(2, "line\nerror: https://host.invalid/a.whl?sig=secret failed\n"))
    assert detail.startswith("uv 退出码 2")
    assert "secret" not in detail and "https://host.invalid/a.whl" in detail


def test_safe_url_and_model_url() -> None:
    assert model_url("https://m.invalid/p3/", "UVDoc") == "https://m.invalid/p3/UVDoc_infer.tar"
    assert safe_url("https://user:pw@m.invalid/a.tar?x=1#f") == "https://m.invalid/a.tar"


def test_normalize_windows_languages() -> None:
    assert normalize_languages(["en-US", "zh-Hans-CN", "en-GB", "zh-TW", "!!"], log) == (
        "en",
        "zh-Hans",
        "zh-Hant",
    )


def write_tar(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_extract_model_single_root(tmp_path: Path) -> None:
    archive = write_tar(tmp_path / "a.tar.part", model_tar("M"))
    final = tmp_path / "official_models" / "M"
    extract_model(archive, tmp_path / ".M.extract", final, "M")
    assert sorted(p.name for p in final.iterdir()) == [
        "inference.json",
        "inference.pdiparams",
        "inference.yml",
    ]
    assert not archive.exists() and not (tmp_path / ".M.extract").exists()


def test_extract_model_multiple_roots_uses_model_name(tmp_path: Path) -> None:
    data = model_tar("M", root="M", extra={"README.txt": b"hi"})
    final = tmp_path / "out" / "M"
    extract_model(write_tar(tmp_path / "a.tar", data), tmp_path / "x", final, "M")
    assert (final / "inference.yml").is_file()


def test_extract_model_replaces_stale_directory(tmp_path: Path) -> None:
    final = tmp_path / "out" / "M"
    final.mkdir(parents=True)
    (final / "stale").write_bytes(b"old")
    extract_model(write_tar(tmp_path / "a.tar", model_tar("M")), tmp_path / "x", final, "M")
    assert not (final / "stale").exists()


@pytest.mark.parametrize("persistent", [False, True])
def test_model_publication_handles_temporary_and_persistent_rename_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistent: bool
) -> None:
    archive = write_tar(tmp_path / "a.tar.part", model_tar("M"))
    final, staging = tmp_path / "models" / "M", tmp_path / ".M.extract"
    replace = _fileops.os.replace
    calls: list[Path] = []
    delays: list[float] = []

    def locked(source: Path, target: Path) -> None:
        calls.append(source)
        if persistent or len(calls) < 3:
            raise PermissionError(13, "busy", str(source), 32)
        replace(source, target)

    monkeypatch.setattr(_fileops.os, "replace", locked)
    monkeypatch.setattr(_fileops.time, "sleep", delays.append)
    if persistent:
        with pytest.raises(PermissionError):
            extract_model(archive, staging, final, "M")
        assert not final.exists() and archive.exists() and len(delays) == 6
    else:
        extract_model(archive, staging, final, "M")
        assert (final / "inference.yml").is_file() and not archive.exists()
        assert len(calls) == 3 and delays == [0.025, 0.05]
    assert not staging.exists()


@pytest.mark.parametrize(
    "member",
    ["/abs/evil.txt", "M_infer/../../evil.txt", "C:/evil.txt", "..\\evil.txt"],
)
def test_extract_model_rejects_unsafe_paths(tmp_path: Path, member: str) -> None:
    archive = write_tar(tmp_path / "a.tar", model_tar("M", extra={member: b"pwned"}))
    final = tmp_path / "out" / "M"
    with pytest.raises(InvalidArchive):
        extract_model(archive, tmp_path / "x", final, "M")
    assert not final.exists() and not (tmp_path / "x").exists()
    assert not list(tmp_path.rglob("evil.txt"))


def test_extract_model_rejects_hardlink(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        link = tarfile.TarInfo("M_infer/inference.pdiparams")
        link.type = tarfile.LNKTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    with pytest.raises(InvalidArchive):
        extract_model(write_tar(tmp_path / "a.tar", buffer.getvalue()), tmp_path / "x", tmp_path / "M", "M")


def test_extract_model_rejects_garbage(tmp_path: Path) -> None:
    with pytest.raises(InvalidArchive):
        extract_model(
            write_tar(tmp_path / "a.tar", b"not a tar at all" * 100), tmp_path / "x", tmp_path / "M", "M"
        )
    assert not (tmp_path / "x").exists()


def test_dir_size(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "1.bin").write_bytes(b"x" * 10)
    (tmp_path / "a" / "b" / "2.bin").write_bytes(b"x" * 5)
    assert dir_size(tmp_path) == 15
    assert dir_size(tmp_path / "missing") == 0


async def test_run_command_collects_output_and_exit_code() -> None:
    env = dict(os.environ)
    result = await run_command([sys.executable, "-c", "import sys; print('hello'); sys.exit(3)"], env)
    assert result.returncode == 3
    assert result.output.strip() == "hello"


async def test_run_command_kills_process_on_cancel() -> None:
    env = dict(os.environ)
    task = asyncio.create_task(run_command([sys.executable, "-c", "import time; time.sleep(60)"], env))
    await asyncio.sleep(0.5)
    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 10


async def test_run_command_cancel_kills_whole_process_tree() -> None:
    # 子进程再起一个继承了标准输出的孙进程。只结束子进程时管道仍被孙进程占着，收集输出的线程要等 60 秒。
    grandchild = "import time; time.sleep(60)"
    code = (
        "import subprocess, sys, time;"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]);"
        "print('started', flush=True); time.sleep(60)"
    )
    env = dict(os.environ)
    task = asyncio.create_task(run_command([sys.executable, "-c", code], env))
    await asyncio.sleep(1.5)
    task.cancel()
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 10


def test_scrub_removes_userinfo() -> None:
    url = "https://alice:s3cr3t-token@mirror.example/simple/paddleocr/"
    assert safe_url(url) == "https://mirror.example/simple/paddleocr/"
    assert "s3cr3t" not in scrub(f"error: Failed to fetch: `{url}`")
    detail = failure_detail(CommandResult(2, f"error: Failed to fetch: {url}?x=1"))
    assert "s3cr3t" not in detail and "alice" not in detail and "x=1" not in detail
    assert "https://mirror.example/simple/paddleocr/" in detail


def test_extract_model_counts_members_while_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = {f"M_infer/extra{index}.txt": b"x" for index in range(5)}
    archive = write_tar(tmp_path / "a.tar", model_tar("M", extra=extra))
    monkeypatch.setattr(archive_module, "MAX_MEMBERS", 4)

    def forbidden(self: tarfile.TarFile) -> list[tarfile.TarInfo]:
        raise AssertionError("getmembers 会先把整个包的成员表读进内存")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", forbidden)
    final = tmp_path / "out" / "M"
    with pytest.raises(InvalidArchive, match="成员数量"):
        extract_model(archive, tmp_path / "x", final, "M")
    assert not final.exists() and not (tmp_path / "x").exists()


def test_extract_model_limits_total_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = write_tar(tmp_path / "a.tar", model_tar("M", payload=b"p" * 4096))
    monkeypatch.setattr(archive_module, "MAX_EXTRACT_BYTES", 1024)
    final = tmp_path / "out" / "M"
    with pytest.raises(InvalidArchive, match="解压后的大小"):
        extract_model(archive, tmp_path / "x", final, "M")
    assert not final.exists() and not (tmp_path / "x").exists()


def test_resolve_marker_drops_no_deps() -> None:
    from lst.services.ocr_engines.runtime import RESOLVE_MARKER, requires_resolution

    assert requires_resolution(f"# header\n{RESOLVE_MARKER}\nnumpy==2.3.0\n")
    assert not requires_resolution("numpy==2.3.0\n# lst: resolve-dependencies is mentioned inline\n")
    args = uv_install_args(
        Path("uv.exe"), Path("python.exe"), Path("t"), Path("r.txt"), hashed=False, resolve=True
    )
    assert "--no-deps" not in args and "--no-build" in args
