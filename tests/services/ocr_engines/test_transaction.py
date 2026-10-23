"""运行环境切换的发布、安装标记提交、回滚与启动恢复。"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from lst import _fileops
from lst._fileops import publish_directory
from lst.contracts.ocr_engines import InstallOcrEngine, OcrEngineErrorCode, OcrEngineKind, OcrEngineState
from lst.kernel import EventBus
from lst.services.ocr_engines import installer
from lst.services.ocr_engines.layout import RUNTIME_STAMP, PaddleInstallPaths
from tests.services.ocr_engines.conftest import Published, StartModule
from tests.services.ocr_engines.helpers import CPU, CUDA, MODELS, FakeServer, FakeUv, install_layout
from tests.test_fileops import directory_lock

K = OcrEngineKind.PADDLE


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows 文件与目录句柄")
@pytest.mark.parametrize("failure", ["runtime", "marker"])
async def test_native_windows_locks_restore_the_previous_runtime(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    paths: PaddleInstallPaths,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    install_layout(paths, MODELS, CPU)
    (paths.site_dir / "old.txt").write_bytes(b"old runtime")
    marker = paths.marker.read_bytes()
    module = await start_module()
    delays: list[float] = []

    def locked_publication(source: Path, target: Path) -> None:
        if source == paths.site_part:
            with directory_lock(source):
                publish_directory(source, target)
        else:
            publish_directory(source, target)

    monkeypatch.setattr(_fileops.time, "sleep", delays.append)
    if failure == "runtime":
        monkeypatch.setattr(installer, "publish_directory", locked_publication)
        bus.send(InstallOcrEngine(K, CUDA))
        await bus.drain()
        assert len(delays) == 6
    else:
        with directory_lock(paths.marker):
            bus.send(InstallOcrEngine(K, CUDA))
            await bus.drain()
    assert published.engine(K).state is OcrEngineState.ERROR
    assert published.engine(K).installed_variant is CPU and module.can_run(K)
    assert paths.marker.read_bytes() == marker
    assert (paths.site_dir / "old.txt").read_bytes() == b"old runtime"
    assert not paths.site_backup.exists() and not paths.site_part.exists()


@pytest.mark.parametrize("failure", ["runtime", "marker"])
async def test_failed_publication_restores_the_old_runtime_and_allows_retry(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    paths: PaddleInstallPaths,
    server: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    install_layout(paths, MODELS, CPU)
    (paths.site_dir / "old.txt").write_bytes(b"old runtime")
    original_marker = paths.marker.read_bytes()
    module = await start_module()
    replace = _fileops.os.replace
    delays: list[float] = []

    def denied(source: Path, target: Path) -> None:
        blocked = paths.site_part if failure == "runtime" else paths.marker_part
        if source == blocked:
            raise PermissionError(13, "publication locked", str(source), 5)
        replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(_fileops.os, "replace", denied)
        patch.setattr(_fileops.time, "sleep", delays.append)
        bus.send(InstallOcrEngine(K, CUDA))
        await bus.drain()
        info = published.engine(K)
        assert info.state is OcrEngineState.ERROR and info.error_code is OcrEngineErrorCode.DISK
        assert info.installed_variant is CPU and module.can_run(K)
        assert (paths.site_dir / "old.txt").read_bytes() == b"old runtime"
        assert paths.marker.read_bytes() == original_marker
        assert not paths.site_backup.exists() and not paths.site_part.exists()
        assert not paths.uv_cache.exists() and not paths.marker_part.exists()
        assert len(delays) == (6 if failure == "runtime" else 0)
    bus.send(InstallOcrEngine(K, CUDA))
    await bus.drain()
    assert published.engine(K).state is OcrEngineState.INSTALLED
    assert published.engine(K).installed_variant is CUDA and module.can_run(K)
    assert not (paths.site_dir / "old.txt").exists() and not paths.site_backup.exists()
    assert server.requests == []


@pytest.mark.parametrize("stage", ["old_moved", "new_published", "marker_committed"])
@pytest.mark.parametrize("same_requirements", [False, True])
async def test_startup_recovers_each_interruption_point(
    start_module: StartModule,
    published: Published,
    paths: PaddleInstallPaths,
    uv: FakeUv,
    server: FakeServer,
    stage: str,
    same_requirements: bool,
) -> None:
    install_layout(paths, MODELS, CPU)
    (paths.site_dir / "old.txt").write_bytes(b"old runtime")
    publish_directory(paths.site_dir, paths.site_backup)
    directory = paths.site_part if stage == "old_moved" else paths.site_dir
    (directory / "paddle").mkdir(parents=True)
    (directory / "paddle" / "__init__.py").write_bytes(b"# new runtime")
    target = CPU if same_requirements else CUDA
    digest = hashlib.sha256(paths.requirements(target).read_bytes()).hexdigest()
    generation = "f" * 32
    (directory / RUNTIME_STAMP).write_text(
        json.dumps({"requirements_sha256": digest, "generation": generation}), encoding="utf-8"
    )
    if stage == "marker_committed":
        marker = json.loads(paths.marker.read_text("utf-8"))
        marker.update(variant=target.value, runtime_requirements_sha256=digest, runtime_generation=generation)
        paths.marker.write_text(json.dumps(marker), encoding="utf-8")
    module = await start_module()
    expected = target if stage == "marker_committed" else CPU
    assert published.engine(K).state is OcrEngineState.INSTALLED
    assert published.engine(K).installed_variant is expected and module.can_run(K)
    assert (paths.site_dir / "old.txt").exists() is (stage != "marker_committed")
    assert not paths.site_backup.exists() and not paths.site_part.exists()
    assert uv.calls == server.requests == []


async def test_backup_cleanup_failure_keeps_the_committed_new_runtime(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    paths: PaddleInstallPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_layout(paths, MODELS, CPU)
    (paths.site_dir / "old.txt").write_bytes(b"old runtime")
    module = await start_module()
    remove = installer.remove_tree

    def busy_backup(path: Path) -> None:
        if path == paths.site_backup:
            raise PermissionError("backup busy")
        remove(path)

    with monkeypatch.context() as patch:
        patch.setattr(installer, "remove_tree", busy_backup)
        bus.send(InstallOcrEngine(K, CUDA))
        await bus.drain()
        assert published.engine(K).state is OcrEngineState.INSTALLED
        assert published.engine(K).installed_variant is CUDA and module.can_run(K)
        assert paths.site_backup.is_dir() and not (paths.site_dir / "old.txt").exists()
    installer.recover_runtime(paths)
    assert not paths.site_backup.exists()
    assert json.loads(paths.marker.read_text("utf-8"))["variant"] == "cuda"
