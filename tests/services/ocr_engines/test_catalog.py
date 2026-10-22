"""OcrEnginesModule 的目录：启动时的状态、Windows OCR 的语言、源码模式、当前引擎与进程状态的合并。"""

import json
import logging
from pathlib import Path

import pytest

from lst.contracts.config import AppConfig
from lst.contracts.ocr_engines import (
    InstallOcrEngine,
    OcrEngineErrorCode,
    OcrEngineKind,
    OcrEngineState,
    UninstallOcrEngine,
)
from lst.kernel import EventBus
from lst.services.ocr_engines import DevPaddleEnvironment, PaddleInstallPaths
from lst.services.ocr_engines.layout import DEFAULT_PADDLE_MODELS, features_of
from tests.services.ocr_engines.conftest import MODULE_LOGGER, Published, StartModule
from tests.services.ocr_engines.helpers import MODELS, FakeServer, FakeUv, install_layout, tree, write_model

K = OcrEngineKind
S = OcrEngineState


def install_everything(paths: PaddleInstallPaths) -> None:
    """按已安装的样子摆好文件：带运行环境标记的运行环境、全部模型、安装标记。"""
    install_layout(paths, MODELS)


async def test_initial_catalog_lists_both_engines(start_module: StartModule, published: Published) -> None:
    await start_module()
    catalog = published.catalog
    assert [info.kind for info in catalog.engines] == [K.WINDOWS, K.PADDLE]
    windows = published.engine(K.WINDOWS)
    assert windows.state is S.INSTALLED
    assert not windows.needs_download
    assert windows.languages == ("en", "zh-Hans")
    assert windows.features == ("text",)
    paddle = published.engine(K.PADDLE)
    assert paddle.state is S.NOT_INSTALLED
    assert paddle.needs_download
    assert paddle.download_bytes == 10_000 + sum(model.size_bytes for model in MODELS)
    assert paddle.progress is None
    assert paddle.features == ("text", "layout", "table")


async def test_windows_without_language_is_unavailable(
    start_module: StartModule, published: Published
) -> None:
    await start_module(windows_probe=list[str])
    windows = published.engine(K.WINDOWS)
    assert windows.state is S.UNAVAILABLE
    assert windows.error_code is OcrEngineErrorCode.NO_LANGUAGE
    assert windows.languages == ()


async def test_windows_probe_failure_is_unavailable(start_module: StartModule, published: Published) -> None:
    def broken() -> list[str]:
        raise ImportError("No module named 'winrt'")

    await start_module(windows_probe=broken)
    windows = published.engine(K.WINDOWS)
    assert windows.state is S.UNAVAILABLE
    assert windows.error_code is OcrEngineErrorCode.INTERNAL


async def test_install_windows_reprobes_languages(
    bus: EventBus, start_module: StartModule, published: Published
) -> None:
    languages: list[str] = []
    await start_module(windows_probe=lambda: list(languages))
    assert published.engine(K.WINDOWS).state is S.UNAVAILABLE
    languages.append("ja-JP")
    bus.send(InstallOcrEngine(K.WINDOWS))
    await bus.drain()
    windows = published.engine(K.WINDOWS)
    assert windows.state is S.INSTALLED
    assert windows.languages == ("ja",)


async def test_installed_layout_is_reported_installed(
    start_module: StartModule, published: Published, paths: PaddleInstallPaths
) -> None:
    install_everything(paths)
    await start_module()
    assert published.engine(K.PADDLE).state is S.INSTALLED


async def test_changed_requirements_need_runtime_update(
    start_module: StartModule, published: Published, paths: PaddleInstallPaths
) -> None:
    install_everything(paths)
    # 程序升级后安装目录里的版本清单变了，数据目录里的运行环境还是按旧清单装的。
    paths.requirements.write_text("paddlepaddle==3.4.0\n", encoding="utf-8")
    await start_module()
    paddle = published.engine(K.PADDLE)
    assert paddle.state is S.NOT_INSTALLED
    assert "版本清单" in paddle.detail


async def test_outdated_marker_needs_update(
    start_module: StartModule, published: Published, paths: PaddleInstallPaths
) -> None:
    install_everything(paths)
    paths.marker.write_text(
        json.dumps({"runtime_requirements_sha256": "0" * 64, "models": []}), encoding="utf-8"
    )
    await start_module()
    assert published.engine(K.PADDLE).state is S.NOT_INSTALLED


async def test_missing_model_or_marker_means_not_installed(
    start_module: StartModule, published: Published, paths: PaddleInstallPaths
) -> None:
    install_everything(paths)
    paths.marker.unlink()
    await start_module()
    assert published.engine(K.PADDLE).state is S.NOT_INSTALLED


async def test_start_removes_leftovers_of_interrupted_install(
    start_module: StartModule, published: Published, paths: PaddleInstallPaths
) -> None:
    (paths.site_part / "paddle").mkdir(parents=True)
    paths.uv_cache.mkdir(parents=True)
    paths.models_dir.mkdir(parents=True)
    paths.download_part("SLANet_plus").write_bytes(b"half")
    (paths.extract_dir("SLANet_plus") / "x").mkdir(parents=True)
    write_model(paths.model_dir("PP-OCRv5_mobile_det"))
    await start_module()
    assert not paths.site_part.exists()
    assert not paths.uv_cache.exists()
    assert tree(paths.models_dir)[0].startswith("PP-OCRv5_mobile_det")
    assert not any(name.startswith(".") for name in tree(paths.models_dir))
    assert published.engine(K.PADDLE).state is S.NOT_INSTALLED


def dev_environment(root: Path, *, venv: bool = True, models: bool = True) -> DevPaddleEnvironment:
    env = DevPaddleEnvironment(
        venv=root / ".venv-ocr",
        model_caches=(root / "data" / "models" / "ocr", root / "assets" / "PaddleOCR_and_PP-Structure"),
    )
    if venv:
        env.python.parent.mkdir(parents=True)
        env.python.write_bytes(b"MZ")
    if models:
        write_model(
            root / "assets" / "PaddleOCR_and_PP-Structure" / "official_models" / "PP-OCRv5_mobile_det"
        )
    return env


async def test_source_mode_reports_installed_and_ignores_commands(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    tmp_path: Path,
    uv: FakeUv,
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = tmp_path / "repo"
    dev = dev_environment(repo)
    before = tree(repo)
    await start_module(paddle=dev)
    paddle = published.engine(K.PADDLE)
    assert paddle.state is S.INSTALLED
    assert not paddle.needs_download
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        bus.send(UninstallOcrEngine(K.PADDLE))
        bus.send(InstallOcrEngine(K.PADDLE))
        await bus.drain()
    assert tree(repo) == before
    assert uv.calls == [] and server.requests == []
    assert published.changed == []
    assert published.engine(K.PADDLE).state is S.INSTALLED
    messages = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert any("源码运行，不卸载" in message for message in messages)
    assert any("源码运行，不安装" in message for message in messages)


async def test_source_mode_without_venv_is_not_installed(
    start_module: StartModule, published: Published, tmp_path: Path
) -> None:
    await start_module(paddle=dev_environment(tmp_path / "repo", venv=False))
    paddle = published.engine(K.PADDLE)
    assert paddle.state is S.NOT_INSTALLED
    assert ".venv-ocr" in paddle.detail


async def test_source_mode_without_models_is_not_installed(
    start_module: StartModule, published: Published, tmp_path: Path
) -> None:
    await start_module(paddle=dev_environment(tmp_path / "repo", models=False))
    assert published.engine(K.PADDLE).state is S.NOT_INSTALLED


async def test_active_engine_follows_config(
    bus: EventBus, start_module: StartModule, published: Published
) -> None:
    await start_module()
    assert published.catalog.active is None
    bus.publish(AppConfig({"ocr": {"enabled": True, "engine": "paddle"}}, revision=1))
    await bus.drain()
    assert published.catalog.active is K.PADDLE
    bus.publish(AppConfig({"ocr": {"enabled": False, "engine": "paddle"}}, revision=2))
    await bus.drain()
    assert published.catalog.active is None
    bus.publish(AppConfig({}, revision=3))
    await bus.drain()
    assert published.catalog.active is K.WINDOWS


async def test_process_state_is_merged_only_into_active_installed_engine(
    bus: EventBus, start_module: StartModule, published: Published
) -> None:
    module = await start_module()
    bus.publish(AppConfig({"ocr": {"engine": "windows"}}, revision=1))
    await bus.drain()
    module.set_process_state(K.WINDOWS, S.READY)
    await bus.drain()
    assert published.engine(K.WINDOWS).state is S.READY
    module.set_process_state(K.WINDOWS, S.ERROR, OcrEngineErrorCode.WORKER_FAILED, "exit 65")
    await bus.drain()
    windows = published.engine(K.WINDOWS)
    assert windows.state is S.ERROR and windows.error_code is OcrEngineErrorCode.WORKER_FAILED
    # paddle 没有安装：进程状态不覆盖安装状态。
    module.set_process_state(K.PADDLE, S.LOADING)
    bus.publish(AppConfig({"ocr": {"engine": "paddle"}}, revision=2))
    await bus.drain()
    assert published.engine(K.PADDLE).state is S.NOT_INSTALLED
    assert published.engine(K.WINDOWS).state is S.INSTALLED
    module.set_process_state(K.PADDLE, None)
    await bus.drain()
    assert published.engine(K.PADDLE).state is S.NOT_INSTALLED


def test_default_model_list_matches_worker_defaults() -> None:
    names = {model.name for model in DEFAULT_PADDLE_MODELS}
    # lst/workers/ocr_worker/settings.py 的默认模型。
    assert {"PP-DocLayout_plus-L", "PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec"} <= names
    assert features_of(DEFAULT_PADDLE_MODELS) == ("text", "layout", "table")
