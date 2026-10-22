"""各环节的可选模型（需求 OCRE-08）：目录、选用、单独下载、取消、删除，以及模型目录与配置、
OCR 进程的一致性。"""

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path

import pytest

from lst.contracts.config import AppConfig
from lst.contracts.ocr_engines import (
    CancelOcrModelDownload,
    DeleteOcrModel,
    DownloadOcrModel,
    OcrEngineErrorCode,
    OcrEngineKind,
    OcrEngineState,
    OcrModelInfo,
    OcrModelState,
    OcrStage,
)
from lst.kernel import EventBus
from lst.services.ocr import PADDLE_DEFAULT_MODELS, PaddleOcrConfig, RapidOcrConfig
from lst.services.ocr_engines import (
    PADDLE_AUXILIARY,
    PADDLE_MODELS,
    PADDLE_STAGE_DEFAULTS,
    RAPID_MODELS,
    RAPID_STAGE_DEFAULTS,
    DevRapidEnvironment,
    PaddleInstallPaths,
    RapidInstallPaths,
)
from lst.services.ocr_engines.catalog import RAPID_BUNDLED_FILES, RAPID_SOURCE
from lst.workers.ocr_worker import languages as worker_languages
from lst.workers.ocr_worker import settings as worker_settings
from tests.services.ocr_engines.conftest import MODULE_LOGGER, Published, StartModule, wait_until
from tests.services.ocr_engines.helpers import (
    AUX_MODEL,
    EXTRA_MODEL,
    MODELS,
    RAPID_EXTRA,
    RAPID_EXTRA_BODY,
    RAPID_UV_FILES,
    FakeServer,
    Route,
    install_layout,
    install_rapid_layout,
    make_rapid_paths,
    model_tar,
    tree,
    write_files,
)

K = OcrEngineKind
S = OcrEngineState
M = OcrModelState
ROOT = Path(__file__).resolve().parents[3]
RAPID_YAML = ROOT / ".venv-ocr" / "Lib" / "site-packages" / "rapidocr" / "default_models.yaml"


def model(published: Published, kind: OcrEngineKind, model_id: str) -> OcrModelInfo:
    found = next((info for info in published.engine(kind).models if info.id == model_id), None)
    assert found is not None, f"目录里没有 {model_id}"
    return found


@pytest.fixture
def rapid_paths(tmp_path: Path) -> RapidInstallPaths:
    return make_rapid_paths(tmp_path)


# ---------------------------------------------------------------- 目录与选用


async def test_models_are_listed_with_state_and_default_selection(
    start_module: StartModule, published: Published, paths: PaddleInstallPaths
) -> None:
    install_layout(paths, MODELS)
    await start_module(paddle_models=(*MODELS, EXTRA_MODEL, AUX_MODEL))
    infos = published.engine(K.PADDLE).models
    assert [info.id for info in infos] == [m.id for m in MODELS] + [EXTRA_MODEL.id]  # 附属模型不列出
    det = model(published, K.PADDLE, "PP-OCRv5_mobile_det")
    assert det.state is M.DOWNLOADED and det.default and det.selected and det.size_bytes == 3000
    assert det.stage is OcrStage.DETECTION
    extra = model(published, K.PADDLE, EXTRA_MODEL.id)
    assert extra.state is M.NOT_DOWNLOADED and not extra.default and not extra.selected
    assert model(published, K.PADDLE, "PP-OCRv5_mobile_rec").languages == ("zh-Hans", "en")


async def test_selection_follows_the_pipeline_section(
    bus: EventBus, start_module: StartModule, published: Published, paths: PaddleInstallPaths
) -> None:
    install_layout(paths, MODELS)
    await start_module()
    bus.publish(AppConfig({"ocr_paddle": {"detection_model": EXTRA_MODEL.id}}, revision=1))
    await bus.drain()
    assert model(published, K.PADDLE, EXTRA_MODEL.id).selected
    assert not model(published, K.PADDLE, "PP-OCRv5_mobile_det").selected
    bus.publish(AppConfig({}, revision=2))
    await bus.drain()
    assert model(published, K.PADDLE, "PP-OCRv5_mobile_det").selected


async def test_install_downloads_only_the_default_combination(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    paths: PaddleInstallPaths,
) -> None:
    from lst.contracts.ocr_engines import InstallOcrEngine

    server.add_models((*MODELS, EXTRA_MODEL, AUX_MODEL))
    await start_module(paddle_models=(*MODELS, EXTRA_MODEL, AUX_MODEL))
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    assert published.engine(K.PADDLE).state is S.INSTALLED
    downloaded = {url.rsplit("/", 1)[1] for url in server.requests}
    assert downloaded == {f"{m.id}_infer.tar" for m in (*MODELS, AUX_MODEL)}
    assert not paths.model_dir(EXTRA_MODEL.id).exists()


# ---------------------------------------------------------------- PaddleOCR 模型的下载与删除


async def test_download_extra_model(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    paths: PaddleInstallPaths,
) -> None:
    install_layout(paths, MODELS)
    server.add_model(
        EXTRA_MODEL.id, Route(body=model_tar(EXTRA_MODEL.id, payload=b"s" * 4000), chunk_size=512)
    )
    await start_module()
    bus.send(DownloadOcrModel(K.PADDLE, EXTRA_MODEL.id))
    await bus.drain()
    info = model(published, K.PADDLE, EXTRA_MODEL.id)
    assert info.state is M.DOWNLOADED and info.progress is None
    assert (paths.model_dir(EXTRA_MODEL.id) / "inference.pdiparams").is_file()
    states = [
        next(m.state for m in catalog.engine(K.PADDLE).models if m.id == EXTRA_MODEL.id)  # type: ignore[union-attr]
        for catalog in published.catalogs
    ]
    assert M.DOWNLOADING in states
    progress = [
        m.progress
        for catalog in published.catalogs
        for m in catalog.engine(K.PADDLE).models  # type: ignore[union-attr]
        if m.id == EXTRA_MODEL.id and m.progress is not None
    ]
    assert progress == sorted(progress) and progress
    assert published.engine(K.PADDLE).state is S.INSTALLED
    assert not any(name.startswith(".") for name in tree(paths.models_dir) if "/" not in name)


async def test_download_requires_an_installed_engine(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await start_module()
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        bus.send(DownloadOcrModel(K.PADDLE, EXTRA_MODEL.id))
        bus.send(DownloadOcrModel(K.PADDLE, "no-such-model"))
        bus.send(DownloadOcrModel(K.WINDOWS, "x"))
        await bus.drain()
    assert server.requests == []
    assert model(published, K.PADDLE, EXTRA_MODEL.id).state is M.NOT_DOWNLOADED


async def test_cancel_model_download_cleans_up(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    paths: PaddleInstallPaths,
) -> None:
    install_layout(paths, MODELS)
    gate = asyncio.Event()
    route = server.add_model(
        EXTRA_MODEL.id, Route(body=model_tar(EXTRA_MODEL.id, payload=b"q" * 5000), gate=gate)
    )
    await start_module()
    bus.send(DownloadOcrModel(K.PADDLE, EXTRA_MODEL.id))
    await asyncio.wait_for(route.reached.wait(), 5)
    assert model(published, K.PADDLE, EXTRA_MODEL.id).state is M.DOWNLOADING
    bus.send(CancelOcrModelDownload(K.PADDLE, EXTRA_MODEL.id))
    await bus.drain()
    assert model(published, K.PADDLE, EXTRA_MODEL.id).state is M.NOT_DOWNLOADED
    assert not paths.download_part(EXTRA_MODEL.id).exists()
    assert not paths.model_dir(EXTRA_MODEL.id).exists()
    gate.set()


async def test_failed_model_download_is_reported(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    paths: PaddleInstallPaths,
) -> None:
    install_layout(paths, MODELS)
    server.add_model(EXTRA_MODEL.id, Route(status=503))
    await start_module()
    bus.send(DownloadOcrModel(K.PADDLE, EXTRA_MODEL.id))
    await bus.drain()
    info = model(published, K.PADDLE, EXTRA_MODEL.id)
    assert info.state is M.ERROR and info.error_code is OcrEngineErrorCode.HTTP
    assert published.engine(K.PADDLE).state is S.INSTALLED, "模型下载失败不影响引擎"
    # 重试成功后错误清除。
    server.add_model(EXTRA_MODEL.id)
    bus.send(DownloadOcrModel(K.PADDLE, EXTRA_MODEL.id))
    await bus.drain()
    assert model(published, K.PADDLE, EXTRA_MODEL.id).state is M.DOWNLOADED


async def test_delete_refuses_the_selected_model_and_deletes_others(
    bus: EventBus, start_module: StartModule, published: Published, paths: PaddleInstallPaths
) -> None:
    install_layout(paths, (*MODELS, EXTRA_MODEL))
    await start_module()
    bus.send(DeleteOcrModel(K.PADDLE, "PP-OCRv5_mobile_det"))  # 选用中
    bus.send(DeleteOcrModel(K.PADDLE, EXTRA_MODEL.id))
    await bus.drain()
    assert paths.model_dir("PP-OCRv5_mobile_det").is_dir()
    assert model(published, K.PADDLE, "PP-OCRv5_mobile_det").state is M.DOWNLOADED
    assert not paths.model_dir(EXTRA_MODEL.id).exists()
    assert model(published, K.PADDLE, EXTRA_MODEL.id).state is M.NOT_DOWNLOADED
    # 先把检测环节改为 server，再删 mobile：允许；引擎仍是已安装。
    bus.publish(AppConfig({"ocr_paddle": {"detection_model": EXTRA_MODEL.id}}, revision=1))
    bus.send(DeleteOcrModel(K.PADDLE, "PP-OCRv5_mobile_det"))
    await bus.drain()
    assert not paths.model_dir("PP-OCRv5_mobile_det").exists()
    assert published.engine(K.PADDLE).state is S.INSTALLED


async def test_selected_models_ready_reflects_downloads(
    bus: EventBus, start_module: StartModule, server: FakeServer, paths: PaddleInstallPaths
) -> None:
    install_layout(paths, MODELS)
    server.add_model(EXTRA_MODEL.id)
    module = await start_module()
    bus.publish(AppConfig({"ocr_paddle": {"detection_model": EXTRA_MODEL.id}}, revision=1))
    await bus.drain()
    assert (EXTRA_MODEL.id, False) in module.selected_models_ready(K.PADDLE)
    bus.send(DownloadOcrModel(K.PADDLE, EXTRA_MODEL.id))
    await bus.drain()
    assert (EXTRA_MODEL.id, True) in module.selected_models_ready(K.PADDLE)


# ---------------------------------------------------------------- RapidOCR 模型


async def test_rapid_bundled_models_are_downloaded_and_cannot_be_deleted(
    bus: EventBus, start_module: StartModule, published: Published, rapid_paths: RapidInstallPaths
) -> None:
    install_rapid_layout(rapid_paths)
    await start_module(rapid=rapid_paths)
    det = model(published, K.RAPID, "PP-OCRv6/small/multi/det")
    assert det.state is M.DOWNLOADED and det.bundled and det.selected
    assert model(published, K.RAPID, RAPID_EXTRA.id).state is M.NOT_DOWNLOADED
    bus.send(DeleteOcrModel(K.RAPID, "PP-OCRv6/small/multi/det"))
    bus.send(DownloadOcrModel(K.RAPID, "PP-OCRv6/small/multi/det"))
    await bus.drain()
    assert (rapid_paths.bundled_dir / "PP-OCRv6_det_small.onnx").is_file()


async def test_rapid_download_checks_sha256_and_lands_in_model_dir(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    rapid_paths: RapidInstallPaths,
) -> None:
    install_rapid_layout(rapid_paths)
    server.add_url(RAPID_EXTRA.url, Route(body=b"tampered" * 10))
    await start_module(rapid=rapid_paths)
    bus.send(DownloadOcrModel(K.RAPID, RAPID_EXTRA.id))
    await bus.drain()
    info = model(published, K.RAPID, RAPID_EXTRA.id)
    assert info.state is M.ERROR and info.error_code is OcrEngineErrorCode.INVALID_PACKAGE
    assert tree(rapid_paths.model_dir) == []

    server.add_url(RAPID_EXTRA.url, Route(body=RAPID_EXTRA_BODY, chunk_size=200))
    bus.send(DownloadOcrModel(K.RAPID, RAPID_EXTRA.id))
    await bus.drain()
    assert model(published, K.RAPID, RAPID_EXTRA.id).state is M.DOWNLOADED
    assert (rapid_paths.model_dir / RAPID_EXTRA.filename).read_bytes() == RAPID_EXTRA_BODY
    assert tree(rapid_paths.model_dir) == [RAPID_EXTRA.filename]


async def test_rapid_worker_settings_point_at_selected_files(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    rapid_paths: RapidInstallPaths,
) -> None:
    install_rapid_layout(rapid_paths)
    server.add_url(RAPID_EXTRA.url, Route(body=RAPID_EXTRA_BODY))
    module = await start_module(rapid=rapid_paths)
    settings = module.worker_model_settings(K.RAPID)
    assert settings["det_model_path"] == str(rapid_paths.bundled_dir / "PP-OCRv6_det_small.onnx")
    assert settings["rec_model_path"] == str(rapid_paths.bundled_dir / "PP-OCRv6_rec_small.onnx")
    assert settings["recognition_languages"] == ["zh-Hans", "en", "ja"]
    bus.send(DownloadOcrModel(K.RAPID, RAPID_EXTRA.id))
    bus.publish(
        AppConfig({"ocr": {"engine": "rapid"}, "ocr_rapid": {"recognition_model": RAPID_EXTRA.id}}, 1)
    )
    await bus.drain()
    settings = module.worker_model_settings(K.RAPID)
    assert settings["rec_model_path"] == str(rapid_paths.model_dir / RAPID_EXTRA.filename)
    assert settings["recognition_languages"] == list(RAPID_EXTRA.languages)
    assert published.engine(K.RAPID).languages == RAPID_EXTRA.languages
    assert module.worker_model_settings(K.PADDLE) == {}


async def test_uninstall_removes_downloaded_rapid_models(
    bus: EventBus, start_module: StartModule, server: FakeServer, rapid_paths: RapidInstallPaths
) -> None:
    from lst.contracts.ocr_engines import UninstallOcrEngine

    install_rapid_layout(rapid_paths)
    server.add_url(RAPID_EXTRA.url, Route(body=RAPID_EXTRA_BODY))
    await start_module(rapid=rapid_paths)
    bus.send(DownloadOcrModel(K.RAPID, RAPID_EXTRA.id))
    await bus.drain()
    assert rapid_paths.model_dir.is_dir()
    bus.send(UninstallOcrEngine(K.RAPID))
    await bus.drain()
    assert not rapid_paths.model_dir.exists()


async def test_rapid_source_mode_downloads_into_the_data_dir(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, tmp_path: Path
) -> None:
    venv = tmp_path / "repo" / ".venv-ocr"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"MZ")
    site = venv / "Lib" / "site-packages"
    for name in ("rapidocr-3.10.0.dist-info", "onnxruntime-1.31.0.dist-info"):
        (site / name).mkdir(parents=True)
    write_files(site, {k: v for k, v in RAPID_UV_FILES.items() if k.endswith(".onnx")})
    before = sorted(str(p) for p in venv.rglob("*"))
    model_dir = tmp_path / "data" / "models" / "ocr" / "rapid"
    server.add_url(RAPID_EXTRA.url, Route(body=RAPID_EXTRA_BODY))
    await start_module(rapid=DevRapidEnvironment(venv, model_dir))
    bus.send(DownloadOcrModel(K.RAPID, RAPID_EXTRA.id))
    await wait_until(lambda: model(published, K.RAPID, RAPID_EXTRA.id).state is M.DOWNLOADED)
    assert (model_dir / RAPID_EXTRA.filename).is_file()
    assert sorted(str(p) for p in venv.rglob("*")) == before, "源码运行时不改 .venv-ocr"


# ---------------------------------------------------------------- 一致性


def test_catalog_defaults_match_the_config_and_the_worker() -> None:
    assert dict(PADDLE_STAGE_DEFAULTS) == PADDLE_DEFAULT_MODELS
    for stage, field in PaddleOcrConfig.stage_fields().items():
        assert PaddleOcrConfig.model_fields[field].default == PADDLE_STAGE_DEFAULTS[stage]
    for stage, field in RapidOcrConfig.stage_fields().items():
        assert RapidOcrConfig.model_fields[field].default == RAPID_STAGE_DEFAULTS[stage]
    for key, default in worker_settings.DEFAULT_MODELS.items():
        assert PaddleOcrConfig.model_fields[key].default == default
    assert PADDLE_STAGE_DEFAULTS[OcrStage.DETECTION] == worker_settings.DEFAULT_DET_MODEL
    assert PADDLE_STAGE_DEFAULTS[OcrStage.RECOGNITION] == worker_settings.DEFAULT_REC_MODEL


def test_catalog_entries_are_consistent() -> None:
    for specs in (PADDLE_MODELS + PADDLE_AUXILIARY, RAPID_MODELS):
        ids = [spec.id for spec in specs]
        assert len(ids) == len(set(ids))
    for spec in PADDLE_MODELS:
        if spec.stage is OcrStage.RECOGNITION and spec.id.endswith("_PP-OCRv5_mobile_rec"):
            script = spec.id.split("_", 1)[0]
            for code in spec.languages:
                if code not in worker_languages.GENERAL_LANGUAGES:
                    assert worker_languages.recognition_model(code, "x").startswith(f"{script}_")
    assert {spec.filename for spec in RAPID_MODELS if spec.bundled} == set(RAPID_BUNDLED_FILES)
    assert all(spec.default for spec in RAPID_MODELS if spec.bundled)
    assert all(spec.url.startswith(RAPID_SOURCE) and len(spec.sha256) == 64 for spec in RAPID_MODELS)
    assert all(spec.stage is None for spec in PADDLE_AUXILIARY)


@pytest.mark.skipif(not RAPID_YAML.is_file(), reason=".venv-ocr 里没有 rapidocr")
def test_rapid_urls_and_hashes_come_from_the_installed_package() -> None:
    """地址与 SHA-256 与 rapidocr 3.10.0 的 default_models.yaml 逐项一致（只读文件，不联网）。"""
    text = RAPID_YAML.read_text(encoding="utf-8")
    pairs = dict(re.findall(r"model_dir:\s*(https://\S+/onnx/\S+\.onnx)\s+SHA256:\s*([0-9a-f]{64})", text))
    for spec in RAPID_MODELS:
        assert pairs.get(spec.url) == spec.sha256, spec.id
    bundled = RAPID_YAML.parent / "models"
    for spec in RAPID_MODELS:
        if spec.bundled:
            data = (bundled / spec.filename).read_bytes()
            assert len(data) == spec.size_bytes
            assert hashlib.sha256(data).hexdigest() == spec.sha256


def test_marker_records_defaults(paths: PaddleInstallPaths) -> None:
    install_layout(paths, MODELS)
    assert json.loads(paths.marker.read_text(encoding="utf-8"))["models"] == [m.id for m in MODELS]
