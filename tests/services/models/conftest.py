"""模型管理测试的公共夹具：真实事件总线、假服务器、模块启动器与消息记录。"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from lst.contracts.models import DownloadProgress, DownloadState, ModelCatalog, ModelsInstalled
from lst.kernel import EventBus, ModuleContext
from lst.services.models import ModelsModule
from tests.services.models.helpers import DICT_INDEX_URL, INDEX_URL, FakeServer

MODULE_LOGGER = "lst.models"


@dataclass
class Published:
    """模块发布的全部 ModelCatalog 与 ModelsInstalled。"""

    catalogs: list[ModelCatalog] = field(default_factory=list[ModelCatalog])
    installed: list[ModelsInstalled] = field(default_factory=list[ModelsInstalled])

    @property
    def catalog(self) -> ModelCatalog:
        assert self.catalogs, "模块还没有发布 ModelCatalog"
        return self.catalogs[-1]

    def progress(self, model_id: str) -> DownloadProgress | None:
        return self.catalog.downloads.get(model_id)

    def states(self, model_id: str) -> list[DownloadState]:
        """该模型在各次发布中的下载状态，连续相同的只记一次。"""
        states: list[DownloadState] = []
        for catalog in self.catalogs:
            progress = catalog.downloads.get(model_id)
            if progress is not None and (not states or states[-1] is not progress.state):
                states.append(progress.state)
        return states


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    """真实事件总线（raise_errors=True）。处理函数的异常在 drain 时抛出，或由这里收集后在结束时判失败。"""
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    instance = EventBus(loop, logger=logging.getLogger("tests.models.bus"), raise_errors=True)
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
    bus.subscribe(ModelCatalog, record.catalogs.append)
    bus.subscribe(ModelsInstalled, record.installed.append)
    return record


@pytest.fixture
def models_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "models" / "nmt"


@pytest.fixture
def dict_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "models" / "dict"


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
async def client(server: FakeServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=server.transport()) as instance:
        yield instance


type StartModule = Callable[..., Awaitable[ModelsModule]]


@pytest.fixture
async def start_module(
    bus: EventBus, client: httpx.AsyncClient, models_dir: Path, published: Published
) -> AsyncIterator[StartModule]:
    """返回启动函数：创建并启动 ModelsModule，等它发布第一份 ModelCatalog。测试结束时停止。

    依赖 published 是为了让记录在模块启动之前订阅，模块发布的每一份目录都被记下。
    dict_dir 缺省为 None：模块不管理词典包，与只有 NMT 模型时的行为相同。
    """
    started: list[tuple[ModelsModule, ModuleContext]] = []

    async def start(
        *,
        index_url: str = INDEX_URL,
        progress_interval: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
        write_chunk_bytes: int = 1 << 20,
        dict_dir: Path | None = None,
        dict_index_url: str = DICT_INDEX_URL,
    ) -> ModelsModule:
        module = ModelsModule(
            models_dir,
            client,
            index_url,
            dict_dir=dict_dir,
            dict_index_url=dict_index_url,
            progress_interval=progress_interval,
            clock=clock,
            write_chunk_bytes=write_chunk_bytes,
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
    """让事件循环运转，直到条件成立。"""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待的条件在时限内没有成立")
        await asyncio.sleep(0.005)
