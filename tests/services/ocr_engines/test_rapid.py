"""RapidOCR 引擎的目录、安装、取消、失败清理、卸载与源码模式。

uv 由假运行器扮演，往 --target 写入假的 rapidocr 包与两个模型文件；RapidOCR 没有单独的模型下载，
假服务器只用来确认安装过程中没有任何 HTTP 请求。
"""

import asyncio
import json
import logging
from pathlib import Path

import pytest

from lst.contracts.config import AppConfig
from lst.contracts.ocr_engines import (
    CancelOcrEngineInstall,
    InstallOcrEngine,
    OcrEngineErrorCode,
    OcrEngineKind,
    OcrEnginesChanged,
    OcrEngineState,
    UninstallOcrEngine,
)
from lst.kernel import EventBus
from lst.services.ocr_engines import DevRapidEnvironment, PaddleInstallPaths, RapidInstallPaths
from lst.services.ocr_engines.catalog import RAPID_BUNDLED_FILES, V6_LANGUAGES
from lst.services.ocr_engines.layout import RUNTIME_STAMP
from lst.workers.ocr_worker import rapid_backend
from tests.services.ocr_engines.conftest import MODULE_LOGGER, Published, StartModule, wait_until
from tests.services.ocr_engines.helpers import (
    CPU,
    MODELS,
    RAPID_BUNDLED_REC,
    RAPID_MODEL_FILES,
    RAPID_TEST_MODELS,
    RAPID_UV_FILES,
    FakeServer,
    FakeUv,
    install_layout,
    install_rapid_layout,
    make_rapid_paths,
    tree,
    write_files,
)

K = OcrEngineKind
S = OcrEngineState


@pytest.fixture
def rapid_paths(tmp_path: Path) -> RapidInstallPaths:
    return make_rapid_paths(tmp_path)


@pytest.fixture
def rapid_uv(uv: FakeUv) -> FakeUv:
    uv.files = dict(RAPID_UV_FILES)
    return uv


def no_leftovers(paths: RapidInstallPaths) -> None:
    assert not paths.site_part.exists()
    assert not paths.uv_cache.exists()
    assert not paths.marker_part.exists()


# ---------------------------------------------------------------- 目录


async def test_catalog_lists_rapid_between_windows_and_paddle(
    start_module: StartModule, published: Published, rapid_paths: RapidInstallPaths
) -> None:
    await start_module(rapid=rapid_paths)
    assert [info.kind for info in published.catalog.engines] == [K.WINDOWS, K.RAPID, K.PADDLE]
    rapid = published.engine(K.RAPID)
    assert rapid.state is S.NOT_INSTALLED
    assert rapid.needs_download
    assert rapid.download_bytes == 10_000  # 默认组合都随运行环境自带，只计运行环境
    assert rapid.features == ("text",)
    assert rapid.languages == RAPID_BUNDLED_REC.languages
    assert rapid.progress is None and rapid.error_code is None


async def test_without_rapid_paths_the_engine_is_absent_and_commands_are_ignored(
    bus: EventBus, start_module: StartModule, published: Published, uv: FakeUv
) -> None:
    module = await start_module()
    assert published.catalog.engine(K.RAPID) is None
    assert not module.can_run(K.RAPID)
    bus.send(InstallOcrEngine(K.RAPID))
    bus.send(CancelOcrEngineInstall(K.RAPID))
    bus.send(UninstallOcrEngine(K.RAPID))
    await bus.drain()
    assert uv.calls == []
    assert published.changed == []


async def test_installed_layout_is_reported_installed(
    start_module: StartModule, published: Published, rapid_paths: RapidInstallPaths
) -> None:
    install_rapid_layout(rapid_paths)
    module = await start_module(rapid=rapid_paths)
    assert published.engine(K.RAPID).state is S.INSTALLED
    assert module.can_run(K.RAPID)
    assert published.engine(K.PADDLE).state is S.NOT_INSTALLED


async def test_missing_model_in_site_means_not_installed(
    start_module: StartModule, published: Published, rapid_paths: RapidInstallPaths
) -> None:
    install_rapid_layout(rapid_paths)
    (rapid_paths.site_dir / RAPID_MODEL_FILES[1]).unlink()
    await start_module(rapid=rapid_paths)
    assert published.engine(K.RAPID).state is S.NOT_INSTALLED


async def test_changed_requirements_need_runtime_update(
    start_module: StartModule, published: Published, rapid_paths: RapidInstallPaths
) -> None:
    install_rapid_layout(rapid_paths)
    rapid_paths.requirements(CPU).write_text("rapidocr==3.11.0\n", encoding="utf-8")
    await start_module(rapid=rapid_paths)
    rapid = published.engine(K.RAPID)
    assert rapid.state is S.NOT_INSTALLED
    assert "RapidOCR" in rapid.detail and "版本清单" in rapid.detail


async def test_start_removes_leftovers_of_interrupted_install(
    start_module: StartModule, published: Published, rapid_paths: RapidInstallPaths
) -> None:
    (rapid_paths.site_part / "rapidocr").mkdir(parents=True)
    rapid_paths.uv_cache.mkdir(parents=True)
    await start_module(rapid=rapid_paths)
    assert not rapid_paths.site_part.exists()
    assert not rapid_paths.uv_cache.exists()
    assert published.engine(K.RAPID).state is S.NOT_INSTALLED


async def test_process_state_is_merged_into_active_rapid(
    bus: EventBus, start_module: StartModule, published: Published, rapid_paths: RapidInstallPaths
) -> None:
    install_rapid_layout(rapid_paths)
    module = await start_module(rapid=rapid_paths)
    bus.publish(AppConfig({"ocr": {"engine": "rapid"}}, revision=1))
    await bus.drain()
    assert published.catalog.active is K.RAPID
    module.set_process_state(K.RAPID, S.READY)
    await bus.drain()
    assert published.engine(K.RAPID).state is S.READY
    assert module.can_run(K.RAPID)
    module.set_process_state(K.RAPID, None)
    await bus.drain()
    assert published.engine(K.RAPID).state is S.INSTALLED


# ---------------------------------------------------------------- 安装


async def test_install_success(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    rapid_uv: FakeUv,
    rapid_paths: RapidInstallPaths,
) -> None:
    await start_module(rapid=rapid_paths)
    bus.send(InstallOcrEngine(K.RAPID))
    await bus.drain()

    assert published.states(K.RAPID) == [S.NOT_INSTALLED, S.QUEUED, S.DOWNLOADING, S.INSTALLING, S.INSTALLED]
    assert published.changed == [OcrEnginesChanged(K.RAPID, True)]
    assert published.states(K.PADDLE) == [S.NOT_INSTALLED]
    assert server.requests == [], "RapidOCR 的模型在 wheel 里，不应有别的下载"
    for name in RAPID_MODEL_FILES:
        assert (rapid_paths.site_dir / name).is_file()
    stamp = json.loads((rapid_paths.site_dir / RUNTIME_STAMP).read_text(encoding="utf-8"))
    marker = json.loads(rapid_paths.marker.read_text(encoding="utf-8"))
    assert marker["runtime_requirements_sha256"] == stamp["requirements_sha256"]
    assert marker["models"] == [spec.id for spec in RAPID_TEST_MODELS if spec.default]
    assert marker["variant"] == "cpu"
    no_leftovers(rapid_paths)
    [args] = rapid_uv.calls
    assert args[args.index("--target") + 1] == str(rapid_paths.site_part)
    assert args[-2:] == ["-r", str(rapid_paths.requirements(CPU))]
    assert "--require-hashes" in args and "--no-build" in args
    [env] = rapid_uv.envs
    assert env["UV_CACHE_DIR"] == str(rapid_paths.uv_cache)


async def test_unhashed_requirements_install_without_require_hashes(
    bus: EventBus, start_module: StartModule, rapid_uv: FakeUv, rapid_paths: RapidInstallPaths
) -> None:
    rapid_paths.requirements(CPU).write_text(
        "# 不带哈希\nrapidocr==3.10.0\nonnxruntime==1.31.0\n", encoding="utf-8"
    )
    await start_module(rapid=rapid_paths)
    bus.send(InstallOcrEngine(K.RAPID))
    await bus.drain()
    [args] = rapid_uv.calls
    assert "--require-hashes" not in args


async def test_runtime_progress_from_uv_cache(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    rapid_uv: FakeUv,
    rapid_paths: RapidInstallPaths,
) -> None:
    rapid_uv.gate = asyncio.Event()
    rapid_uv.cache_bytes = 5_000
    await start_module(rapid=rapid_paths, rapid_runtime_bytes=10_000)
    bus.send(InstallOcrEngine(K.RAPID))
    await asyncio.wait_for(rapid_uv.reached.wait(), 5)
    await wait_until(lambda: any(value >= 0.45 for value in published.progress(K.RAPID)))
    rapid_uv.gate.set()
    await bus.drain()
    values = published.progress(K.RAPID)
    assert values == sorted(values)
    assert max(values) == 1.0
    assert published.engine(K.RAPID).state is S.INSTALLED


async def test_package_without_models_is_invalid_and_retry_reinstalls_runtime(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    rapid_uv: FakeUv,
    rapid_paths: RapidInstallPaths,
) -> None:
    rapid_uv.files = {"rapidocr/__init__.py": b"# fake\n"}
    await start_module(rapid=rapid_paths)
    bus.send(InstallOcrEngine(K.RAPID))
    await bus.drain()
    rapid = published.engine(K.RAPID)
    assert rapid.state is S.ERROR
    assert rapid.error_code is OcrEngineErrorCode.INVALID_PACKAGE
    assert Path(RAPID_MODEL_FILES[0]).name in rapid.detail
    assert not rapid_paths.marker.exists()
    no_leftovers(rapid_paths)
    assert published.changed == []
    # 版本清单没变，但运行环境里缺模型：重试时重装运行环境。
    rapid_uv.files = dict(RAPID_UV_FILES)
    bus.send(InstallOcrEngine(K.RAPID))
    await bus.drain()
    assert len(rapid_uv.calls) == 2
    assert published.engine(K.RAPID).state is S.INSTALLED


async def test_uv_failure_is_reported_and_cleaned_up(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    rapid_uv: FakeUv,
    rapid_paths: RapidInstallPaths,
) -> None:
    rapid_uv.returncode = 2
    rapid_uv.output = "error: Failed to fetch: `https://pypi.org/simple/rapidocr/`\n  dns error"
    await start_module(rapid=rapid_paths)
    bus.send(InstallOcrEngine(K.RAPID))
    await bus.drain()
    rapid = published.engine(K.RAPID)
    assert rapid.state is S.ERROR
    assert rapid.error_code is OcrEngineErrorCode.NETWORK
    assert "uv 退出码 2" in rapid.detail
    no_leftovers(rapid_paths)
    assert not rapid_paths.site_dir.exists()


async def test_cancel_during_runtime_install_cleans_up(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    rapid_uv: FakeUv,
    rapid_paths: RapidInstallPaths,
) -> None:
    rapid_uv.gate = asyncio.Event()
    await start_module(rapid=rapid_paths)
    bus.send(InstallOcrEngine(K.RAPID))
    await asyncio.wait_for(rapid_uv.reached.wait(), 5)
    assert published.engine(K.RAPID).state is S.DOWNLOADING
    bus.send(CancelOcrEngineInstall(K.RAPID))
    await bus.drain()
    assert rapid_uv.cancelled == 1
    assert published.engine(K.RAPID).state is S.NOT_INSTALLED
    no_leftovers(rapid_paths)
    assert not rapid_paths.site_dir.exists()
    assert published.changed == []


async def test_cancel_of_paddle_does_not_touch_rapid_install(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    rapid_uv: FakeUv,
    rapid_paths: RapidInstallPaths,
) -> None:
    rapid_uv.gate = asyncio.Event()
    await start_module(rapid=rapid_paths)
    bus.send(InstallOcrEngine(K.RAPID))
    await asyncio.wait_for(rapid_uv.reached.wait(), 5)
    bus.send(CancelOcrEngineInstall(K.PADDLE))
    await asyncio.sleep(0.05)  # 取消命令已经处理（安装还卡在 uv 上，不能 drain）
    assert rapid_uv.cancelled == 0
    rapid_uv.gate.set()
    await bus.drain()
    assert published.engine(K.RAPID).state is S.INSTALLED


async def test_runtime_update_stops_worker_first_and_reinstalls(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    rapid_uv: FakeUv,
    rapid_paths: RapidInstallPaths,
) -> None:
    install_rapid_layout(rapid_paths)
    rapid_paths.requirements(CPU).write_text("rapidocr==3.11.0\n", encoding="utf-8")
    calls: list[tuple[OcrEngineKind, bool]] = []

    async def before_update(kind: OcrEngineKind) -> None:
        calls.append((kind, rapid_paths.site_dir.is_dir()))

    await start_module(rapid=rapid_paths, before_runtime_update=before_update)
    bus.send(InstallOcrEngine(K.RAPID))
    await bus.drain()
    assert calls == [(K.RAPID, True)], "替换旧 site 之前应先请组合根停掉 OCR 进程"
    assert len(rapid_uv.calls) == 1
    assert published.engine(K.RAPID).state is S.INSTALLED
    assert published.changed == [OcrEnginesChanged(K.RAPID, True)]


# ---------------------------------------------------------------- 卸载


async def test_uninstall_removes_runtime_only_of_rapid(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    rapid_paths: RapidInstallPaths,
    paths: PaddleInstallPaths,
) -> None:
    install_rapid_layout(rapid_paths)
    install_layout(paths, MODELS)
    stopped: list[OcrEngineKind] = []

    async def before_uninstall(kind: OcrEngineKind) -> None:
        stopped.append(kind)

    await start_module(rapid=rapid_paths, before_uninstall=before_uninstall)
    assert published.engine(K.RAPID).state is S.INSTALLED
    paddle_files = tree(paths.engine_dir) + tree(paths.models_dir)
    bus.send(UninstallOcrEngine(K.RAPID))
    await bus.drain()
    assert stopped == [K.RAPID]
    assert published.engine(K.RAPID).state is S.NOT_INSTALLED
    assert published.changed == [OcrEnginesChanged(K.RAPID, False)]
    assert tree(rapid_paths.engine_dir) == []
    assert tree(paths.engine_dir) + tree(paths.models_dir) == paddle_files
    assert published.engine(K.PADDLE).state is S.INSTALLED


async def test_uninstall_is_ignored_while_installing(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    rapid_uv: FakeUv,
    rapid_paths: RapidInstallPaths,
) -> None:
    rapid_uv.gate = asyncio.Event()
    await start_module(rapid=rapid_paths)
    bus.send(InstallOcrEngine(K.RAPID))
    await asyncio.wait_for(rapid_uv.reached.wait(), 5)
    bus.send(UninstallOcrEngine(K.RAPID))
    rapid_uv.gate.set()
    await bus.drain()
    assert published.engine(K.RAPID).state is S.INSTALLED
    assert published.changed == [OcrEnginesChanged(K.RAPID, True)]


# ---------------------------------------------------------------- 源码模式


def dev_rapid(root: Path, *, onnxruntime: bool = True, models: bool = True) -> DevRapidEnvironment:
    env = DevRapidEnvironment(root / ".venv-ocr")
    env.python.parent.mkdir(parents=True)
    env.python.write_bytes(b"MZ")
    site = env.site_packages
    (site / "rapidocr-3.10.0.dist-info").mkdir(parents=True)
    if onnxruntime:
        (site / "onnxruntime-1.31.0.dist-info").mkdir(parents=True)
    if models:
        write_files(site, dict.fromkeys(RAPID_MODEL_FILES, b"onnx"))
    return env


async def test_source_mode_reports_installed_and_ignores_commands(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    tmp_path: Path,
    uv: FakeUv,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = tmp_path / "repo"
    dev = dev_rapid(repo)
    before = tree(repo)
    module = await start_module(rapid=dev)
    rapid = published.engine(K.RAPID)
    assert rapid.state is S.INSTALLED
    assert not rapid.needs_download and rapid.download_bytes is None
    assert module.can_run(K.RAPID)
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        bus.send(UninstallOcrEngine(K.RAPID))
        bus.send(InstallOcrEngine(K.RAPID))
        bus.send(CancelOcrEngineInstall(K.RAPID))
        await bus.drain()
    assert tree(repo) == before
    assert uv.calls == [] and published.changed == []
    messages = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert any("源码运行，不卸载 RapidOCR" in message for message in messages)
    assert any("源码运行，不安装 RapidOCR" in message for message in messages)


@pytest.mark.parametrize(
    ("onnxruntime", "models", "hint"), [(False, True, "onnxruntime"), (True, False, ".onnx")]
)
async def test_source_mode_requires_packages_and_models(
    start_module: StartModule,
    published: Published,
    tmp_path: Path,
    onnxruntime: bool,
    models: bool,
    hint: str,
) -> None:
    await start_module(rapid=dev_rapid(tmp_path / "repo", onnxruntime=onnxruntime, models=models))
    rapid = published.engine(K.RAPID)
    assert rapid.state is S.NOT_INSTALLED
    assert hint in rapid.detail


async def test_source_mode_without_venv_is_not_installed(
    start_module: StartModule, published: Published, tmp_path: Path
) -> None:
    await start_module(rapid=DevRapidEnvironment(tmp_path / "missing" / ".venv-ocr"))
    rapid = published.engine(K.RAPID)
    assert rapid.state is S.NOT_INSTALLED
    assert ".venv-ocr" in rapid.detail


# ---------------------------------------------------------------- 与 OCR 进程一致


def test_languages_and_model_files_match_the_worker() -> None:
    assert rapid_backend.LANGUAGES == V6_LANGUAGES
    assert set(rapid_backend.MODEL_FILES) <= set(RAPID_BUNDLED_FILES)
    assert rapid_backend.CLS_MODEL in RAPID_BUNDLED_FILES
