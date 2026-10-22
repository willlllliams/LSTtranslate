"""更新服务测试的公共夹具：真实事件总线、假 GitHub、模块启动器与消息记录。"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from lst.contracts.events import NotificationRequested, QuitRequested
from lst.contracts.updates import UpdateState, UpdateStatus
from lst.kernel import EventBus, ModuleContext
from lst.services.updates import UpdatesModule
from tests.services.updates.helpers import RELEASES_URL, FakeClock, FakeGitHub, FakeLauncher, FakeSleep

MODULE_LOGGER = "lst.updates"


@dataclass
class Published:
    """模块发布的全部 UpdateStatus、NotificationRequested 与 QuitRequested。"""

    statuses: list[UpdateStatus] = field(default_factory=list[UpdateStatus])
    notices: list[NotificationRequested] = field(default_factory=list[NotificationRequested])
    quits: list[QuitRequested] = field(default_factory=list[QuitRequested])

    @property
    def status(self) -> UpdateStatus:
        assert self.statuses, "模块还没有发布 UpdateStatus"
        return self.statuses[-1]

    def states(self) -> list[UpdateState]:
        """各次发布的状态，连续相同的只记一次。"""
        states: list[UpdateState] = []
        for status in self.statuses:
            if not states or states[-1] is not status.state:
                states.append(status.state)
        return states


@dataclass
class Env:
    data_dir: Path
    sleep: FakeSleep
    clock: FakeClock
    launcher: FakeLauncher

    @property
    def record_file(self) -> Path:
        return self.data_dir / "updates.json"

    @property
    def download_dir(self) -> Path:
        return self.data_dir / "tmp" / "updates"


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    """真实事件总线（raise_errors=True）。处理函数的异常在 drain 时抛出，或由这里收集后在结束时判失败。"""
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    instance = EventBus(loop, logger=logging.getLogger("tests.updates.bus"), raise_errors=True)
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
    bus.subscribe(UpdateStatus, record.statuses.append)
    bus.subscribe(NotificationRequested, record.notices.append)
    bus.subscribe(QuitRequested, record.quits.append)
    return record


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
async def client(github: FakeGitHub) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=github.transport()) as instance:
        yield instance


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path / "data", FakeSleep(), FakeClock(), FakeLauncher())


type StartModule = Callable[..., Awaitable[UpdatesModule]]


@pytest.fixture
async def start_module(
    bus: EventBus, client: httpx.AsyncClient, env: Env, published: Published
) -> AsyncIterator[StartModule]:
    """返回启动函数：创建并启动 UpdatesModule，等它发布初始状态。测试结束时停止。

    默认用假 sleep：自动检查要等测试调用 env.sleep.release() 才开始。
    """
    started: list[tuple[UpdatesModule, ModuleContext]] = []

    async def start(
        *,
        current_version: str = "0.1.0",
        can_install: bool = True,
        startup_delay: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        progress_interval: float = 0.2,
        write_chunk_bytes: int = 1 << 20,
    ) -> UpdatesModule:
        module = UpdatesModule(
            client,
            current_version=current_version,
            can_install=can_install,
            record_file=env.record_file,
            download_dir=env.download_dir,
            releases_url=RELEASES_URL,
            launcher=env.launcher,
            startup_delay=startup_delay,
            sleep=env.sleep,
            now=env.clock,
            clock=clock,
            progress_interval=progress_interval,
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
