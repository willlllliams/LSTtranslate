"""PaddleOCR 的安装、取消、失败清理与卸载。

uv 由假运行器扮演，模型包是内存里生成的小 tar，由 MockTransport 扮演的假服务器分块返回。
"""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx

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
from lst.services.ocr_engines import PaddleInstallPaths
from lst.services.ocr_engines.layout import RUNTIME_STAMP
from tests.services.ocr_engines.conftest import Published, StartModule, wait_until
from tests.services.ocr_engines.helpers import (
    CPU,
    MODELS,
    FakeServer,
    FakeUv,
    Route,
    install_layout,
    model_tar,
    symlink_tar,
    tree,
    write_model,
)

K = OcrEngineKind
S = OcrEngineState


def no_leftovers(paths: PaddleInstallPaths) -> None:
    assert not paths.site_part.exists()
    assert not paths.uv_cache.exists()
    assert not any(name.startswith(".") for name in tree(paths.models_dir) if "/" not in name)


async def test_install_success(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    uv: FakeUv,
    paths: PaddleInstallPaths,
) -> None:
    server.add_models()
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()

    assert published.states(K.PADDLE) == [S.NOT_INSTALLED, S.QUEUED, S.DOWNLOADING, S.INSTALLING, S.INSTALLED]
    assert published.changed == [OcrEnginesChanged(K.PADDLE, True)]
    paddle = published.engine(K.PADDLE)
    assert paddle.progress is None and paddle.error_code is None
    # 运行环境：原子改名后的 site 带运行环境标记，临时目录与 uv 缓存都已删除。
    assert (paths.site_dir / "paddle" / "__init__.py").is_file()
    stamp = json.loads((paths.site_dir / RUNTIME_STAMP).read_text(encoding="utf-8"))
    assert len(stamp["requirements_sha256"]) == 64
    no_leftovers(paths)
    # 模型：顶层的 <模型名>_infer/ 改名为 official_models/<模型名>/。
    for model in MODELS:
        assert sorted(p.name for p in paths.model_dir(model.id).iterdir()) == [
            "inference.json",
            "inference.pdiparams",
            "inference.yml",
        ]
    assert sorted(p.name for p in paths.models_dir.iterdir()) == sorted(m.id for m in MODELS)
    marker = json.loads(paths.marker.read_text(encoding="utf-8"))
    assert marker["models"] == [m.id for m in MODELS]
    # uv 的命令行与环境变量。
    [args] = uv.calls
    assert args[:3] == [str(paths.uv), "pip", "install"]
    assert args[args.index("--python") + 1] == str(paths.python)
    assert args[args.index("--target") + 1] == str(paths.site_part)
    assert "--no-deps" in args and "--require-hashes" in args and "--no-config" in args
    assert args[-2:] == ["-r", str(paths.requirements(CPU))]
    [env] = uv.envs
    assert env["UV_CACHE_DIR"] == str(paths.uv_cache)
    assert "UV_INDEX_URL" not in env and "PYTHONPATH" not in env
    assert env["PATH"] == "C:\\Windows"


async def test_install_ignored_when_installed_or_busy(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, uv: FakeUv
) -> None:
    server.add_models()
    uv.gate = asyncio.Event()
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await asyncio.wait_for(uv.reached.wait(), 5)
    bus.send(InstallOcrEngine(K.PADDLE))
    uv.gate.set()
    await bus.drain()
    assert len(uv.calls) == 1
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    assert len(uv.calls) == 1
    assert len(published.changed) == 1


async def test_progress_is_monotonic_and_throttled(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, uv: FakeUv
) -> None:
    now = [0.0]

    def clock() -> float:
        return now[0]

    def tick() -> None:
        now[0] += 0.05

    for model in MODELS:
        body = model_tar(model.id, payload=b"p" * 4000)
        server.add_model(model.id, Route(body=body, chunk_size=256, on_chunk=tick))
    uv.cache_bytes = 4000
    await start_module(progress_interval=0.2, clock=clock)
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()

    values = published.progress(K.PADDLE)
    assert values == sorted(values), values
    assert values[0] == 0.0 and values[-1] == 1.0
    chunks = sum(len(route.body) // 256 + 1 for route in server.routes.values())
    # 每块数据推进 0.05 秒，节流间隔 0.2 秒：发布次数约为块数的四分之一，加上每段结束时的强制发布。
    assert len(values) < chunks / 2
    assert len(values) > 4


async def test_runtime_progress_from_uv_cache(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, uv: FakeUv
) -> None:
    server.add_models()
    uv.gate = asyncio.Event()
    uv.cache_bytes = 5000
    await start_module(runtime_bytes=10_000)
    bus.send(InstallOcrEngine(K.PADDLE))
    await asyncio.wait_for(uv.reached.wait(), 5)
    total = 10_000 + sum(model.size_bytes or 0 for model in MODELS)
    # 缓存里有 5000 字节，运行环境阶段完成一半：整体进度约为 5000 / total。
    await wait_until(lambda: (published.engine(K.PADDLE).progress or 0) > 0)
    progress = published.engine(K.PADDLE).progress
    assert progress is not None and abs(progress - 5000 / total) < 1e-6
    uv.gate.set()
    await bus.drain()
    assert published.engine(K.PADDLE).state is S.INSTALLED


async def test_cancel_during_runtime_install_cleans_up(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    uv: FakeUv,
    paths: PaddleInstallPaths,
) -> None:
    server.add_models()
    uv.gate = asyncio.Event()
    uv.cache_bytes = 100
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await asyncio.wait_for(uv.reached.wait(), 5)
    assert paths.site_part.is_dir()
    bus.send(CancelOcrEngineInstall(K.PADDLE))
    await bus.drain()

    assert uv.cancelled == 1
    assert published.engine(K.PADDLE).state is S.NOT_INSTALLED
    assert not paths.site_dir.exists()
    no_leftovers(paths)
    assert server.requests == []
    assert published.changed == []


async def test_cancel_during_model_download_keeps_runtime(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    uv: FakeUv,
    paths: PaddleInstallPaths,
) -> None:
    first, second, third = MODELS
    server.add_model(first.id)
    gate = asyncio.Event()
    route = server.add_model(second.id, Route(body=model_tar(second.id, payload=b"q" * 5000), gate=gate))
    server.add_model(third.id)
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await asyncio.wait_for(route.reached.wait(), 5)
    assert paths.download_part(second.id).is_file()
    bus.send(CancelOcrEngineInstall(K.PADDLE))
    await bus.drain()

    assert published.engine(K.PADDLE).state is S.NOT_INSTALLED
    assert published.changed == []
    no_leftovers(paths)
    assert not paths.download_part(second.id).exists()
    assert not paths.model_dir(second.id).exists()
    # 已经完整的运行环境与模型保留，重试时复用。
    assert paths.site_dir.is_dir()
    assert paths.model_dir(first.id).is_dir()
    assert not paths.marker.exists()

    gate.set()
    requests_before = len(server.requests)
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    assert published.engine(K.PADDLE).state is S.INSTALLED
    assert len(uv.calls) == 1, "运行环境的版本清单没变，重试不应再运行 uv"
    retried = server.requests[requests_before:]
    assert [url.rsplit("/", 1)[1] for url in retried] == [
        f"{second.id}_infer.tar",
        f"{third.id}_infer.tar",
    ]


async def test_cancel_before_task_runs(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, uv: FakeUv
) -> None:
    server.add_models()
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    bus.send(CancelOcrEngineInstall(K.PADDLE))
    await bus.drain()
    assert published.engine(K.PADDLE).state in {S.NOT_INSTALLED, S.INSTALLED}


async def test_uv_failure_cleans_up(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    uv: FakeUv,
    paths: PaddleInstallPaths,
) -> None:
    server.add_models()
    uv.returncode = 2
    uv.output = "error: Failed to fetch: `https://pypi.invalid/simple/paddle?token=secret`\n  dns error"
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()

    paddle = published.engine(K.PADDLE)
    assert paddle.state is S.ERROR
    assert paddle.error_code is OcrEngineErrorCode.NETWORK
    assert "secret" not in paddle.detail and "uv 退出码 2" in paddle.detail
    assert not paths.site_dir.exists()
    no_leftovers(paths)
    assert server.requests == []


async def test_uv_generic_failure_is_install_failed(
    bus: EventBus, start_module: StartModule, published: Published, uv: FakeUv
) -> None:
    uv.returncode = 1
    uv.output = "error: Hash mismatch for `paddlepaddle==3.3.1`"
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    assert published.engine(K.PADDLE).error_code is OcrEngineErrorCode.INSTALL_FAILED


async def test_uv_missing_is_reported(
    bus: EventBus, start_module: StartModule, published: Published, uv: FakeUv
) -> None:
    uv.error = FileNotFoundError(2, "系统找不到指定的文件")
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    assert published.engine(K.PADDLE).error_code is OcrEngineErrorCode.INSTALL_FAILED


async def test_incomplete_layout_is_internal_error(
    bus: EventBus, start_module: StartModule, published: Published, uv: FakeUv, paths: PaddleInstallPaths
) -> None:
    paths.uv.unlink()
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    paddle = published.engine(K.PADDLE)
    assert paddle.state is S.ERROR and paddle.error_code is OcrEngineErrorCode.INTERNAL
    assert uv.calls == []


async def test_http_error_cleans_up(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    paths: PaddleInstallPaths,
) -> None:
    server.add_model(MODELS[0].id)
    server.add_model(MODELS[1].id, Route(status=503))
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    paddle = published.engine(K.PADDLE)
    assert paddle.state is S.ERROR and paddle.error_code is OcrEngineErrorCode.HTTP
    no_leftovers(paths)
    assert not paths.model_dir(MODELS[1].id).exists()
    assert not paths.marker.exists()
    assert published.changed == []


async def test_network_error_is_classified(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer
) -> None:
    server.add_model(MODELS[0].id, Route(error=httpx.ReadTimeout("timed out")))
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    assert published.engine(K.PADDLE).error_code is OcrEngineErrorCode.TIMEOUT


async def test_truncated_download_is_invalid(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    paths: PaddleInstallPaths,
) -> None:
    body = model_tar(MODELS[0].id)
    server.add_model(MODELS[0].id, Route(body=body, content_length=len(body) + 100))
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    paddle = published.engine(K.PADDLE)
    assert paddle.error_code in {OcrEngineErrorCode.INVALID_PACKAGE, OcrEngineErrorCode.NETWORK}
    no_leftovers(paths)


async def test_path_traversal_tar_is_rejected(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    paths: PaddleInstallPaths,
    tmp_path: Path,
) -> None:
    name = MODELS[0].id
    evil = model_tar(name, extra={f"{name}_infer/../../../../evil.txt": b"pwned"})
    server.add_model(name, Route(body=evil))
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()

    paddle = published.engine(K.PADDLE)
    assert paddle.state is S.ERROR and paddle.error_code is OcrEngineErrorCode.INVALID_PACKAGE
    assert not list(tmp_path.rglob("evil.txt"))
    assert not paths.model_dir(name).exists()
    no_leftovers(paths)


async def test_symlink_tar_is_rejected(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    paths: PaddleInstallPaths,
) -> None:
    name = MODELS[0].id
    server.add_model(name, Route(body=symlink_tar(name)))
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    assert published.engine(K.PADDLE).error_code is OcrEngineErrorCode.INVALID_PACKAGE
    no_leftovers(paths)


async def test_tar_without_model_files_is_rejected(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer
) -> None:
    name = MODELS[0].id
    server.add_model(name, Route(body=model_tar(name, files=False)))
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    assert published.engine(K.PADDLE).error_code is OcrEngineErrorCode.INVALID_PACKAGE


async def test_existing_models_are_not_downloaded_again(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    paths: PaddleInstallPaths,
) -> None:
    server.add_models()
    write_model(paths.model_dir(MODELS[0].id))
    await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    assert published.engine(K.PADDLE).state is S.INSTALLED
    assert not any(MODELS[0].id in url for url in server.requests)


async def test_uninstall_removes_runtime_and_models(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    paths: PaddleInstallPaths,
) -> None:
    server.add_models()
    calls: list[OcrEngineKind] = []

    async def before_uninstall(kind: OcrEngineKind) -> None:
        calls.append(kind)

    await start_module(before_uninstall=before_uninstall)
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()
    assert published.engine(K.PADDLE).state is S.INSTALLED

    bus.send(UninstallOcrEngine(K.PADDLE))
    await bus.drain()
    assert calls == [K.PADDLE]
    assert published.engine(K.PADDLE).state is S.NOT_INSTALLED
    assert published.changed == [OcrEnginesChanged(K.PADDLE, True), OcrEnginesChanged(K.PADDLE, False)]
    assert not paths.site_dir.exists()
    assert not paths.marker.exists()
    assert not paths.models_dir.exists()


async def test_runtime_update_reinstalls_runtime_and_keeps_models(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    uv: FakeUv,
    paths: PaddleInstallPaths,
) -> None:
    install_layout(paths, MODELS)
    paths.requirements(CPU).write_text("paddlepaddle==3.4.0\n", encoding="utf-8")
    (paths.site_dir / "old.txt").write_text("old runtime", encoding="utf-8")
    seen: list[tuple[OcrEngineKind, bool]] = []

    async def before_runtime_update(kind: OcrEngineKind) -> None:
        # 调用时旧的运行环境还在：组合根在这里停 OCR 进程，之后才替换 site 目录。
        seen.append((kind, (paths.site_dir / "old.txt").is_file()))

    await start_module(before_runtime_update=before_runtime_update)
    assert published.engine(K.PADDLE).state is S.NOT_INSTALLED
    bus.send(InstallOcrEngine(K.PADDLE))
    await bus.drain()

    assert published.engine(K.PADDLE).state is S.INSTALLED
    assert len(uv.calls) == 1
    assert server.requests == []  # 模型全部复用
    assert seen == [(K.PADDLE, True)]
    assert not (paths.site_dir / "old.txt").exists()
    digest = hashlib.sha256(paths.requirements(CPU).read_bytes()).hexdigest()
    assert (
        json.loads((paths.site_dir / RUNTIME_STAMP).read_text(encoding="utf-8"))["requirements_sha256"]
        == digest
    )
    assert json.loads(paths.marker.read_text(encoding="utf-8"))["runtime_requirements_sha256"] == digest
    assert published.changed == [OcrEnginesChanged(K.PADDLE, True)]


async def test_install_is_ignored_while_uninstalling(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    uv: FakeUv,
    paths: PaddleInstallPaths,
) -> None:
    server.add_models()
    install_layout(paths, MODELS)
    gate = asyncio.Event()
    reached = asyncio.Event()
    calls: list[OcrEngineKind] = []

    async def before_uninstall(kind: OcrEngineKind) -> None:
        calls.append(kind)
        reached.set()
        await gate.wait()

    await start_module(before_uninstall=before_uninstall)
    bus.send(UninstallOcrEngine(K.PADDLE))
    await asyncio.wait_for(reached.wait(), 5)
    # 卸载进行中：安装命令与重复的卸载命令都被忽略，不会一边删文件一边装。
    bus.send(InstallOcrEngine(K.PADDLE))
    bus.send(UninstallOcrEngine(K.PADDLE))
    await asyncio.sleep(0.05)
    gate.set()
    await bus.drain()

    assert calls == [K.PADDLE]
    assert uv.calls == []
    assert published.engine(K.PADDLE).state is S.NOT_INSTALLED
    assert not paths.site_dir.exists()


async def test_uninstall_windows_is_ignored(
    bus: EventBus, start_module: StartModule, published: Published
) -> None:
    await start_module()
    bus.send(UninstallOcrEngine(K.WINDOWS))
    bus.send(CancelOcrEngineInstall(K.WINDOWS))
    await bus.drain()
    assert published.engine(K.WINDOWS).state is S.INSTALLED
    assert published.changed == []


async def test_stop_during_install_cleans_up(
    bus: EventBus, start_module: StartModule, server: FakeServer, uv: FakeUv, paths: PaddleInstallPaths
) -> None:
    server.add_models()
    uv.gate = asyncio.Event()
    module = await start_module()
    bus.send(InstallOcrEngine(K.PADDLE))
    await asyncio.wait_for(uv.reached.wait(), 5)
    await module.stop()
    await bus.drain()
    no_leftovers(paths)
