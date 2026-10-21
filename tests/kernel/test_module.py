"""模块上下文与模块生命周期测试（架构文档 4.3 节）。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from lst.contracts.messages import Command, Event, State
from lst.kernel import EventBus, Module, ModuleContext


@dataclass(frozen=True, slots=True)
class Ping(Event):
    n: int


@dataclass(frozen=True, slots=True)
class Pong(Event):
    n: int


@dataclass(frozen=True, slots=True)
class DoWork(Command):
    n: int


@dataclass(frozen=True, slots=True)
class Volume(State):
    level: int


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    instance = EventBus(asyncio.get_running_loop(), logger=logging.getLogger("tests.kernel.module"))
    yield instance
    await instance.aclose()


def make_context(bus: EventBus, name: str = "test") -> ModuleContext:
    return ModuleContext(bus, logging.getLogger(f"tests.kernel.module.{name}"))


async def test_context_exposes_bus_and_logger(bus: EventBus) -> None:
    logger = logging.getLogger("tests.kernel.module.exposed")
    ctx = ModuleContext(bus, logger)
    assert ctx.bus is bus
    assert ctx.logger is logger
    assert not ctx.closed


async def test_context_subscribe_delivers_through_bus(bus: EventBus) -> None:
    ctx = make_context(bus)
    received: list[int] = []
    sub = ctx.subscribe(Ping, lambda m: received.append(m.n))

    bus.publish(Ping(1))
    await bus.drain()
    assert sub.active
    assert received == [1]


async def test_close_cancels_every_subscription(bus: EventBus) -> None:
    ctx = make_context(bus)
    received: list[object] = []
    subs = [
        ctx.subscribe(Ping, received.append),
        ctx.subscribe(DoWork, received.append),
        ctx.subscribe(Volume, received.append),
    ]
    # 消息已经排队，close 之后仍然不再投递。
    bus.publish(Ping(1))
    bus.send(DoWork(2))
    bus.publish(Volume(3))
    ctx.close()
    await bus.drain()

    assert ctx.closed
    assert all(not sub.active for sub in subs)
    assert received == []


async def test_close_is_idempotent(bus: EventBus) -> None:
    ctx = make_context(bus)
    first = ctx.subscribe(Ping, lambda m: None)
    second = ctx.subscribe(Pong, lambda m: None)
    first.cancel()  # 模块自己提前取消的订阅，close 时再取消一次没有影响

    ctx.close()
    ctx.close()
    assert not first.active
    assert not second.active


async def test_close_leaves_other_contexts_untouched(bus: EventBus) -> None:
    closing = make_context(bus, "closing")
    staying = make_context(bus, "staying")
    closed_received: list[int] = []
    staying_received: list[int] = []
    closing.subscribe(Ping, lambda m: closed_received.append(m.n))
    staying.subscribe(Ping, lambda m: staying_received.append(m.n))

    closing.close()
    bus.publish(Ping(1))
    await bus.drain()

    assert closed_received == []
    assert staying_received == [1]


async def test_subscribe_after_close_raises(bus: EventBus) -> None:
    ctx = make_context(bus)
    ctx.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        ctx.subscribe(Ping, lambda m: None)


class EchoModule:
    """示例模块：收到 Ping 就发布同编号的 Pong，并记录看到的 Volume。"""

    name = "echo"

    def __init__(self) -> None:
        self.volumes: list[int] = []
        self.stopped = False
        self._ctx: ModuleContext | None = None

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        ctx.subscribe(Ping, self._on_ping)
        ctx.subscribe(Volume, self._on_volume)

    async def stop(self) -> None:
        self.stopped = True

    async def _on_ping(self, message: Ping) -> None:
        assert self._ctx is not None
        self._ctx.bus.publish(Pong(message.n))

    def _on_volume(self, message: Volume) -> None:
        self.volumes.append(message.level)


async def test_module_lifecycle(bus: EventBus) -> None:
    # 静态检查：EchoModule 满足 Module 协议。
    module: Module = EchoModule()
    pongs: list[int] = []
    bus.subscribe(Pong, lambda m: pongs.append(m.n))
    bus.publish(Volume(5))

    # 组合根的启动顺序：创建上下文、启动模块。
    ctx = ModuleContext(bus, logging.getLogger(f"lst.{module.name}"))
    await module.start(ctx)
    bus.publish(Ping(1))
    await bus.drain()
    assert pongs == [1]

    # 退出顺序：先 stop，再由上下文统一取消订阅。
    await module.stop()
    ctx.close()
    bus.publish(Ping(2))
    bus.publish(Volume(6))
    await bus.drain()

    assert isinstance(module, EchoModule)
    assert module.stopped
    assert module.volumes == [5]
    assert pongs == [1]
