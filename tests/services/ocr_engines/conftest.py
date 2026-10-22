"""OCR 引擎测试的公共夹具：真实事件总线、假服务器、假 uv、模块启动器与消息记录。"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from lst.contracts.ocr_engines import (
    OcrEngineCatalog,
    OcrEngineInfo,
    OcrEngineKind,
    OcrEnginesChanged,
    OcrEngineState,
    OcrRuntimeVariant,
)
from lst.kernel import EventBus, ModuleContext
from lst.services.ocr_engines import DevPaddleEnvironment, GpuAdapter, OcrEnginesModule, PaddleInstallPaths
from tests.services.ocr_engines.helpers import (
    BASE_URL,
    PADDLE_TEST_MODELS,
    RAPID_TEST_MODELS,
    FakeServer,
    FakeUv,
    make_paths,
)

MODULE_LOGGER = "lst.ocr_engines"


@dataclass
class Published:
    """模块发布的全部 OcrEngineCatalog 与 OcrEnginesChanged。"""

    catalogs: list[OcrEngineCatalog] = field(default_factory=list[OcrEngineCatalog])
    changed: list[OcrEnginesChanged] = field(default_factory=list[OcrEnginesChanged])

    @property
    def catalog(self) -> OcrEngineCatalog:
        assert self.catalogs, "模块还没有发布 OcrEngineCatalog"
        return self.catalogs[-1]

    def engine(self, kind: OcrEngineKind) -> OcrEngineInfo:
        info = self.catalog.engine(kind)
        assert info is not None
        return info

    def states(self, kind: OcrEngineKind) -> list[OcrEngineState]:
        """该引擎在各次发布中的状态，连续相同的只记一次。"""
        states: list[OcrEngineState] = []
        for catalog in self.catalogs:
            info = catalog.engine(kind)
            if info is not None and (not states or states[-1] is not info.state):
                states.append(info.state)
        return states

    def progress(self, kind: OcrEngineKind) -> list[float]:
        values: list[float] = []
        for catalog in self.catalogs:
            info = catalog.engine(kind)
            if info is not None and info.progress is not None:
                values.append(info.progress)
        return values


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    instance = EventBus(loop, logger=logging.getLogger("tests.ocr_engines.bus"), raise_errors=True)
    try:
        yield instance
        await instance.drain()
    finally:
        await instance.aclose()
        loop.set_exception_handler(None)
    assert not errors, [context.get("exception") or context.get("message") for context in errors]


@pytest.fixture
async def published(bus: EventBus) -> Published:
    record = Published()
    bus.subscribe(OcrEngineCatalog, record.catalogs.append)
    bus.subscribe(OcrEnginesChanged, record.changed.append)
    return record


@pytest.fixture
def paths(tmp_path: Path) -> PaddleInstallPaths:
    return make_paths(tmp_path)


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def uv() -> FakeUv:
    return FakeUv()


@pytest.fixture
async def client(server: FakeServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=server.transport()) as instance:
        yield instance


def windows_ok() -> list[str]:
    return ["en-US", "zh-Hans-CN"]


type StartModule = Callable[..., Awaitable[OcrEnginesModule]]


@pytest.fixture
async def start_module(
    bus: EventBus,
    client: httpx.AsyncClient,
    paths: PaddleInstallPaths,
    uv: FakeUv,
    published: Published,
) -> AsyncIterator[StartModule]:
    """返回启动函数：创建并启动 OcrEnginesModule，等它发布第一份目录。测试结束时停止。"""
    started: list[tuple[OcrEnginesModule, ModuleContext]] = []

    async def start(
        *,
        paddle: PaddleInstallPaths | DevPaddleEnvironment | None = None,
        windows_probe: Callable[[], list[str]] = windows_ok,
        progress_interval: float = 0.0,
        poll_interval: float = 0.001,
        clock: Callable[[], float] = time.monotonic,
        runtime_bytes: int = 10_000,
        rapid_runtime_bytes: int = 10_000,
        gpu_probe: Callable[[], list[GpuAdapter]] = list[GpuAdapter],
        **extra: Any,
    ) -> OcrEnginesModule:
        sizes = {
            **{(OcrEngineKind.PADDLE, variant): runtime_bytes for variant in OcrRuntimeVariant},
            **{(OcrEngineKind.RAPID, variant): rapid_runtime_bytes for variant in OcrRuntimeVariant},
        }
        extra.setdefault("paddle_models", PADDLE_TEST_MODELS)
        extra.setdefault("rapid_models", RAPID_TEST_MODELS)
        module = OcrEnginesModule(
            paddle if paddle is not None else paths,
            client,
            model_base_url=BASE_URL,
            runner=uv,
            windows_probe=windows_probe,
            gpu_probe=gpu_probe,
            environ={"PATH": "C:\\Windows", "UV_INDEX_URL": "https://evil.invalid", "PYTHONPATH": "x"},
            runtime_bytes=sizes,
            download_bytes=sizes,
            progress_interval=progress_interval,
            poll_interval=poll_interval,
            clock=clock,
            write_chunk_bytes=512,
            **extra,
        )
        ctx = ModuleContext(bus, logging.getLogger(MODULE_LOGGER))
        await module.start(ctx)
        started.append((module, ctx))
        await bus.drain()
        return module

    yield start
    for module, ctx in reversed(started):
        await module.stop()
        ctx.close()


async def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待的条件在时限内没有成立")
        await asyncio.sleep(0.005)
