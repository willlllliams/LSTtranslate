"""监管器测试的公共夹具：用假推理进程创建监管器，测试结束后停止它们并检查没有残留的进程和线程。"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import pytest

from lst.app import jobobject
from lst.app.supervisor import SupervisorTimings, WorkerSupervisor
from lst.contracts.ports import WorkerState
from lst.contracts.worker_protocol import JsonValue, Progress

ROOT = Path(__file__).resolve().parents[2]
WORKER_COMMAND = (sys.executable, "-m", "tests.app.fake_worker")

# 测试用的时限：连接与就绪给足余量，退避和关停等待都很短。
FAST = SupervisorTimings(
    accept_timeout_s=15.0,
    ready_timeout_s=15.0,
    shutdown_grace_s=2.0,
    reload_grace_s=2.0,
    backoff_s=(0.01, 0.02, 0.04, 0.08),
    io_drain_s=2.0,
    pipe_closed_grace_s=1.0,
)


@dataclass
class StateLog:
    """记录 on_state 回调收到的状态序列。"""

    states: list[WorkerState] = field(default_factory=list[WorkerState])
    details: list[str] = field(default_factory=list[str])

    def __call__(self, state: WorkerState, detail: str) -> None:
        self.states.append(state)
        self.details.append(detail)


class FakeTime:
    """注入监管器的时钟与退避等待：等待不花真实时间，只把时钟往前拨。

    extra_per_sleep 是每次等待额外拨过的秒数，用来模拟崩溃之间相隔很久。
    """

    def __init__(self, extra_per_sleep: float = 0.0) -> None:
        self.now = 0.0
        self.extra_per_sleep = extra_per_sleep
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay + self.extra_per_sleep
        await asyncio.sleep(0)


class GatedSleep:
    """退避等待一直挂起，直到测试打开闸门。用来在 CRASHED 期间做事情。"""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        await self.gate.wait()


class SupervisorFactory(Protocol):
    def __call__(
        self,
        settings: Mapping[str, JsonValue] | None = None,
        *,
        args: Sequence[str] = (),
        command: Sequence[str] | None = None,
        timings: SupervisorTimings = FAST,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_progress: Callable[[Progress], None] | None = None,
    ) -> tuple[WorkerSupervisor, StateLog]: ...


SUPERVISOR_NAME = "fake"


def _supervisor_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(f"lst-{SUPERVISOR_NAME}#") and t.is_alive()]


async def _wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


@pytest.fixture
async def make_supervisor() -> AsyncIterator[SupervisorFactory]:
    created: list[WorkerSupervisor] = []

    def factory(
        settings: Mapping[str, JsonValue] | None = None,
        *,
        args: Sequence[str] = (),
        command: Sequence[str] | None = None,
        timings: SupervisorTimings = FAST,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_progress: Callable[[Progress], None] | None = None,
    ) -> tuple[WorkerSupervisor, StateLog]:
        log = StateLog()
        supervisor = WorkerSupervisor(
            SUPERVISOR_NAME,
            [*(command if command is not None else WORKER_COMMAND), *args],
            settings=settings or {},
            cwd=ROOT,
            on_state=log,
            on_progress=on_progress,
            clock=clock,
            sleep=sleep,
            timings=timings,
        )
        created.append(supervisor)
        return supervisor, log

    yield factory

    for supervisor in created:
        await supervisor.stop()
        assert supervisor.state is WorkerState.STOPPED
        assert supervisor.pid is None
    # 所有子进程都在作业对象里：作业里没有仍在运行的进程，就说明没有残留。
    assert await _wait_until(lambda: not jobobject.process_ids(), 5.0), jobobject.process_ids()
    # 每个子进程配的线程（accept、读写、输出、等待退出）都应随进程结束。
    assert await _wait_until(lambda: not _supervisor_threads(), 5.0), _supervisor_threads()
