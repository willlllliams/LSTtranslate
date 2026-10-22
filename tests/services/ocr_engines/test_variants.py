"""运行环境的版本（需求 OCRE-07）：可装版本、显卡检测与推荐、按版本安装、换版本重装运行环境并保留模型。

显卡由注入的假适配器列表扮演，不调用 DXGI；uv 由假运行器扮演。
"""

import asyncio
import json
import logging
from pathlib import Path

import pytest

from lst.contracts.ocr_engines import (
    CancelOcrEngineInstall,
    InstallOcrEngine,
    OcrEngineErrorCode,
    OcrEngineKind,
    OcrEnginesChanged,
    OcrEngineState,
    OcrRuntimeVariant,
)
from lst.kernel import EventBus
from lst.services._shared.gpu import NVIDIA_VENDOR_ID, probe_adapters, safe_probe
from lst.services.ocr_engines import (
    DevPaddleEnvironment,
    DevRapidEnvironment,
    GpuAdapter,
    PaddleInstallPaths,
    recommend_variant,
)
from tests.services.ocr_engines.conftest import MODULE_LOGGER, Published, StartModule, wait_until
from tests.services.ocr_engines.helpers import (
    CPU,
    CUDA,
    DML,
    MODELS,
    RAPID_UV_FILES,
    FakeServer,
    FakeUv,
    install_layout,
    install_rapid_layout,
    make_paths,
    make_rapid_paths,
    write_files,
    write_model,
)

K = OcrEngineKind
S = OcrEngineState
V = OcrRuntimeVariant

NVIDIA = GpuAdapter("NVIDIA GeForce RTX 5080", NVIDIA_VENDOR_ID, 0x2C02, 16 << 30, d3d12=True)
AMD = GpuAdapter("AMD Radeon RX 7800 XT", 0x1002, 0x747E, 16 << 30, d3d12=True)
OLD_INTEL = GpuAdapter("Intel HD Graphics 3000", 0x8086, 0x0116, 0, d3d12=False)


# ---------------------------------------------------------------- 推荐规则与检测


@pytest.mark.parametrize(
    ("adapters", "variants", "expected"),
    [
        ([NVIDIA], (CPU, DML, CUDA), CUDA),
        ([NVIDIA], (CPU, CUDA), CUDA),
        ([AMD], (CPU, DML, CUDA), DML),
        ([AMD], (CPU, CUDA), CPU),  # PaddleOCR 没有 DirectML 版
        ([OLD_INTEL], (CPU, DML, CUDA), CPU),
        ([], (CPU, DML, CUDA), CPU),
        ([AMD, NVIDIA], (CPU, DML, CUDA), CUDA),
        ([NVIDIA], (CPU,), CPU),  # 构建时没有生成 GPU 版清单
        ([NVIDIA], (), None),
    ],
)
def test_recommend_variant(adapters: list[GpuAdapter], variants: tuple[V, ...], expected: V | None) -> None:
    assert recommend_variant(adapters, variants) is expected


def test_probe_failure_counts_as_no_gpu(caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> list[GpuAdapter]:
        raise OSError("dxgi.dll 缺失")

    with caplog.at_level(logging.WARNING):
        assert safe_probe(broken, logging.getLogger("test")) == ()
    assert "无法检测显卡" in caplog.text


def test_same_gpu_listed_several_times_is_reported_once() -> None:
    """本机实测：DXGI 把同一块 RTX 5080 以 LUID 不同的三个适配器列出。"""
    intel = GpuAdapter("Intel(R) Graphics", 0x8086, 0x7D67, 128 << 20, d3d12=True)
    adapters = safe_probe(lambda: [NVIDIA, intel, NVIDIA, NVIDIA], logging.getLogger("test"))
    assert adapters == (NVIDIA, intel)


def test_real_probe_returns_hardware_adapters_only() -> None:
    """真实的 DXGI 枚举（只读本机信息，不联网）：结果里没有软件渲染适配器。"""
    adapters = probe_adapters()
    assert all(adapter.vendor_id != 0x1414 for adapter in adapters)
    assert all(adapter.name for adapter in adapters)


# ---------------------------------------------------------------- 目录里的版本


async def test_catalog_lists_variants_and_recommendation(
    start_module: StartModule, published: Published, tmp_path: Path
) -> None:
    await start_module(rapid=make_rapid_paths(tmp_path), gpu_probe=lambda: [AMD])
    rapid = published.engine(K.RAPID)
    paddle = published.engine(K.PADDLE)
    assert rapid.variants == (CPU, DML, CUDA)
    assert paddle.variants == (CPU, CUDA)
    assert rapid.recommended_variant is DML
    assert paddle.recommended_variant is CPU
    assert rapid.installed_variant is None and paddle.installed_variant is None
    assert published.engine(K.WINDOWS).variants == ()


async def test_nvidia_recommends_cuda_for_both(
    start_module: StartModule, published: Published, tmp_path: Path
) -> None:
    await start_module(rapid=make_rapid_paths(tmp_path), gpu_probe=lambda: [OLD_INTEL, NVIDIA])
    assert published.engine(K.RAPID).recommended_variant is CUDA
    assert published.engine(K.PADDLE).recommended_variant is CUDA


async def test_variant_without_requirements_is_not_offered(
    start_module: StartModule, published: Published, tmp_path: Path
) -> None:
    paths = make_paths(tmp_path / "p", variants=(CPU,))
    await start_module(paddle=paths, gpu_probe=lambda: [NVIDIA])
    paddle = published.engine(K.PADDLE)
    assert paddle.variants == (CPU,)
    assert paddle.recommended_variant is CPU


async def test_installed_variant_is_read_from_the_marker(
    start_module: StartModule, published: Published, paths: PaddleInstallPaths
) -> None:
    install_layout(paths, MODELS, CUDA)
    await start_module()
    paddle = published.engine(K.PADDLE)
    assert paddle.state is S.INSTALLED
    assert paddle.installed_variant is CUDA
    assert "CUDA" in paddle.detail


async def test_old_marker_without_variant_counts_as_cpu(
    start_module: StartModule, published: Published, paths: PaddleInstallPaths
) -> None:
    install_layout(paths, MODELS)
    marker = json.loads(paths.marker.read_text(encoding="utf-8"))
    del marker["variant"]
    paths.marker.write_text(json.dumps(marker), encoding="utf-8")
    await start_module()
    assert published.engine(K.PADDLE).installed_variant is CPU


async def test_source_mode_detects_the_installed_variant(
    start_module: StartModule, published: Published, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    venv = repo / ".venv-ocr"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"MZ")
    site = venv / "Lib" / "site-packages"
    for name in (
        "paddlepaddle_gpu-3.3.1.dist-info",
        "rapidocr-3.10.0.dist-info",
        "onnxruntime_directml-1.24.4.dist-info",
    ):
        (site / name).mkdir(parents=True)
    write_files(site, {name: data for name, data in RAPID_UV_FILES.items() if name.endswith(".onnx")})
    cache = repo / "assets"
    write_model(cache / "official_models" / "PP-OCRv5_mobile_det")
    await start_module(paddle=DevPaddleEnvironment(venv, (cache,)), rapid=DevRapidEnvironment(venv))
    assert published.engine(K.PADDLE).installed_variant is CUDA
    assert published.engine(K.RAPID).installed_variant is DML


# ---------------------------------------------------------------- 按版本安装


async def test_install_uses_the_requested_variant_requirements(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    uv: FakeUv,
    paths: PaddleInstallPaths,
) -> None:
    server.add_models()
    await start_module(gpu_probe=lambda: [NVIDIA])
    bus.send(InstallOcrEngine(K.PADDLE, CPU))
    await bus.drain()
    [args] = uv.calls
    assert args[-1] == str(paths.requirements(CPU))
    paddle = published.engine(K.PADDLE)
    assert paddle.state is S.INSTALLED and paddle.installed_variant is CPU
    assert json.loads(paths.marker.read_text(encoding="utf-8"))["variant"] == "cpu"


async def test_install_without_variant_uses_the_recommendation(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    uv: FakeUv,
    paths: PaddleInstallPaths,
) -> None:
    server.add_models()
    await start_module(gpu_probe=lambda: [NVIDIA])
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    assert uv.calls[0][-1] == str(paths.requirements(CUDA))
    assert published.engine(K.PADDLE).installed_variant is CUDA


async def test_unknown_variant_is_ignored(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    uv: FakeUv,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await start_module()
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        bus.send(InstallOcrEngine(K.PADDLE, DML))  # PaddleOCR 没有 DirectML 版
        await bus.drain()
    assert uv.calls == []
    assert published.engine(K.PADDLE).state is S.NOT_INSTALLED
    assert "directml" in caplog.text


async def test_same_variant_is_not_reinstalled(
    bus: EventBus, start_module: StartModule, uv: FakeUv, paths: PaddleInstallPaths
) -> None:
    install_layout(paths, MODELS, CUDA)
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE, CUDA))
    await bus.drain()
    assert uv.calls == []


async def test_switching_variant_reinstalls_runtime_and_keeps_models(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    uv: FakeUv,
    paths: PaddleInstallPaths,
) -> None:
    install_layout(paths, MODELS, CPU)
    (paths.site_dir / "old.txt").write_text("cpu runtime", encoding="utf-8")
    uv.gate = asyncio.Event()
    stopped: list[tuple[OcrEngineKind, bool]] = []

    async def before_runtime_update(kind: OcrEngineKind) -> None:
        stopped.append((kind, (paths.site_dir / "old.txt").is_file()))

    module = await start_module(before_runtime_update=before_runtime_update)
    bus.send(InstallOcrEngine(K.PADDLE, CUDA))
    await asyncio.wait_for(uv.reached.wait(), 5)
    # 下载新运行环境期间 OCR 照常使用旧运行环境。
    assert published.engine(K.PADDLE).state is S.DOWNLOADING
    assert published.engine(K.PADDLE).installed_variant is CPU
    assert module.can_run(K.PADDLE)
    uv.gate.set()
    await bus.drain()

    paddle = published.engine(K.PADDLE)
    assert paddle.state is S.INSTALLED and paddle.installed_variant is CUDA
    assert uv.calls[0][-1] == str(paths.requirements(CUDA))
    assert stopped == [(K.PADDLE, True)]
    assert not (paths.site_dir / "old.txt").exists()
    assert server.requests == [], "模型在 site 之外，换版本不应重新下载"
    for model in MODELS:
        assert paths.model_dir(model.id).is_dir()
    assert json.loads(paths.marker.read_text(encoding="utf-8"))["variant"] == "cuda"
    assert published.changed == [OcrEnginesChanged(K.PADDLE, True)]
    assert module.installed_variant(K.PADDLE) == "cuda"


async def test_cancelled_switch_keeps_the_old_variant(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    uv: FakeUv,
    paths: PaddleInstallPaths,
) -> None:
    install_layout(paths, MODELS, CPU)
    uv.gate = asyncio.Event()
    module = await start_module()
    bus.send(InstallOcrEngine(K.PADDLE, CUDA))
    await asyncio.wait_for(uv.reached.wait(), 5)
    bus.send(CancelOcrEngineInstall(K.PADDLE))
    await bus.drain()
    paddle = published.engine(K.PADDLE)
    assert paddle.state is S.INSTALLED and paddle.installed_variant is CPU
    assert module.can_run(K.PADDLE)
    assert not paths.site_part.exists()


async def test_failed_switch_reports_error_but_old_runtime_still_runs(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    uv: FakeUv,
    paths: PaddleInstallPaths,
) -> None:
    install_layout(paths, MODELS, CPU)
    uv.returncode = 1
    uv.output = "error: no matching distribution for paddlepaddle-gpu"
    module = await start_module()
    bus.send(InstallOcrEngine(K.PADDLE, CUDA))
    await bus.drain()
    paddle = published.engine(K.PADDLE)
    assert paddle.state is S.ERROR
    assert paddle.error_code is OcrEngineErrorCode.INSTALL_FAILED
    assert paddle.installed_variant is CPU
    assert module.can_run(K.PADDLE), "旧运行环境完好，OCR 照常可用"


async def test_rapid_switch_to_directml(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    uv: FakeUv,
    tmp_path: Path,
) -> None:
    rapid = make_rapid_paths(tmp_path)
    install_rapid_layout(rapid, CPU)
    uv.files = dict(RAPID_UV_FILES)
    await start_module(rapid=rapid, gpu_probe=lambda: [AMD])
    assert published.engine(K.RAPID).recommended_variant is DML
    bus.send(InstallOcrEngine(K.RAPID))
    await wait_until(lambda: published.engine(K.RAPID).installed_variant is DML)
    await bus.drain()
    assert uv.calls[0][-1] == str(rapid.requirements(DML))
    assert published.engine(K.RAPID).state is S.INSTALLED
