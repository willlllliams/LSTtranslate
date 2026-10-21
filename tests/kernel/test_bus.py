"""事件总线测试。

用例测试按架构文档 5.2 节的约定逐条验证：排队投递、顺序、跨线程、异常处理、命令处理者数量、
State 回放、订阅取消、drain 与 aclose。最后一组性质测试用 hypothesis 随机交错发布（含从其他线程发布）、
订阅、取消和让出循环，验证每个 State 订阅者收到的序列恰好是“订阅时刻的最新值 + 之后发布的每一个值”，
取消后不再收到任何值。
"""

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lst.contracts.messages import Command, Event, Message, State
from lst.kernel import EventBus, Subscription

LOGGER_NAME = "tests.kernel.bus"


@dataclass(frozen=True, slots=True)
class Ping(Event):
    n: int


@dataclass(frozen=True, slots=True)
class LoudPing(Ping):
    """Ping 的子类，用来验证不做父类分发。"""


@dataclass(frozen=True, slots=True)
class Pong(Event):
    n: int


@dataclass(frozen=True, slots=True)
class Tagged(Event):
    source: int
    n: int


@dataclass(frozen=True, slots=True)
class DoWork(Command):
    n: int


@dataclass(frozen=True, slots=True)
class Volume(State):
    level: int


@dataclass(frozen=True, slots=True)
class Theme(State):
    name: str


class Recorder[M: Message]:
    """把收到的消息按顺序记下来的同步处理函数。"""

    def __init__(self) -> None:
        self.items: list[M] = []

    def __call__(self, message: M) -> None:
        self.items.append(message)


def bus_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """总线日志器记录的 ERROR 级别日志。"""
    return [r for r in caplog.records if r.name == LOGGER_NAME and r.levelno >= logging.ERROR]


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    instance = EventBus(asyncio.get_running_loop(), logger=logging.getLogger(LOGGER_NAME))
    yield instance
    await instance.aclose()


@pytest.fixture
async def strict_bus() -> AsyncIterator[EventBus]:
    instance = EventBus(asyncio.get_running_loop(), logger=logging.getLogger(LOGGER_NAME), raise_errors=True)
    yield instance
    await instance.aclose()


# ---------------------------------------------------------------- 排队投递与顺序


async def test_handlers_never_run_inside_caller_stack(bus: EventBus) -> None:
    publishing = False
    seen: list[tuple[str, bool]] = []
    bus.subscribe(Ping, lambda m: seen.append(("ping", publishing)))
    bus.subscribe(DoWork, lambda m: seen.append(("work", publishing)))
    bus.publish(Volume(1))
    await bus.drain()

    publishing = True
    bus.publish(Ping(1))
    bus.send(DoWork(1))
    # State 回放同样排队，不在 subscribe 里同步执行。
    bus.subscribe(Volume, lambda m: seen.append(("volume", publishing)))
    assert seen == []
    publishing = False

    await bus.drain()
    assert seen == [("ping", False), ("work", False), ("volume", False)]


async def test_queued_delivery_runs_on_next_loop_iteration(bus: EventBus) -> None:
    rec = Recorder[Ping]()
    bus.subscribe(Ping, rec)
    bus.publish(Ping(1))
    assert rec.items == []
    await asyncio.sleep(0)
    assert rec.items == [Ping(1)]


async def test_messages_delivered_in_publish_order(bus: EventBus) -> None:
    log: list[str] = []
    bus.subscribe(Ping, lambda m: log.append(f"ping{m.n}"))
    bus.subscribe(Pong, lambda m: log.append(f"pong{m.n}"))
    bus.subscribe(DoWork, lambda m: log.append(f"work{m.n}"))
    bus.subscribe(Volume, lambda m: log.append(f"volume{m.level}"))

    bus.publish(Ping(1))
    bus.publish(Pong(2))
    bus.send(DoWork(3))
    bus.publish(Volume(4))
    bus.publish(Ping(5))
    bus.publish(Pong(6))
    await bus.drain()

    assert log == ["ping1", "pong2", "work3", "volume4", "ping5", "pong6"]


async def test_message_published_by_handler_waits_for_current_message(bus: EventBus) -> None:
    log: list[str] = []

    def first(message: Ping) -> None:
        log.append(f"first{message.n}")
        if message.n == 1:
            bus.publish(Ping(2))

    bus.subscribe(Ping, first)
    bus.subscribe(Ping, lambda m: log.append(f"second{m.n}"))
    bus.publish(Ping(1))
    await bus.drain()

    # 处理函数里发布的 Ping(2) 排在 Ping(1) 交给全部处理函数之后。
    assert log == ["first1", "second1", "first2", "second2"]


async def test_same_message_delivered_in_subscription_order(bus: EventBus) -> None:
    order: list[int] = []
    subs = [bus.subscribe(Ping, lambda m, i=i: order.append(i)) for i in range(5)]
    subs[1].cancel()
    bus.subscribe(Ping, lambda m: order.append(1))  # 重新订阅排到最后

    bus.publish(Ping(0))
    await bus.drain()
    assert order == [0, 2, 3, 4, 1]


async def test_exact_type_match_without_parent_dispatch(bus: EventBus) -> None:
    pings = Recorder[Ping]()
    loud = Recorder[LoudPing]()
    events = Recorder[Event]()
    bus.subscribe(Ping, pings)
    bus.subscribe(LoudPing, loud)
    bus.subscribe(Event, events)

    bus.publish(LoudPing(1))
    bus.publish(Ping(2))
    await bus.drain()

    assert pings.items == [Ping(2)]
    assert loud.items == [LoudPing(1)]
    assert events.items == []


# ---------------------------------------------------------------- 跨线程


async def test_publish_from_other_thread_runs_handlers_on_loop_thread(bus: EventBus) -> None:
    loop_thread = threading.get_ident()
    publisher_thread: list[int] = []
    handler_threads: list[tuple[str, int]] = []

    async def on_pong(message: Pong) -> None:
        handler_threads.append(("pong", threading.get_ident()))

    bus.subscribe(Ping, lambda m: handler_threads.append(("ping", threading.get_ident())))
    bus.subscribe(Pong, on_pong)
    bus.subscribe(DoWork, lambda m: handler_threads.append(("work", threading.get_ident())))

    def worker() -> None:
        publisher_thread.append(threading.get_ident())
        bus.publish(Ping(1))
        bus.publish(Pong(2))
        bus.send(DoWork(3))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    await bus.drain()

    assert publisher_thread[0] != loop_thread
    # 协程处理函数包装成任务，晚一轮才开始执行，所以这里只比较线程，不比较先后。
    assert dict(handler_threads) == {"ping": loop_thread, "pong": loop_thread, "work": loop_thread}


async def test_publish_order_preserved_per_thread(bus: EventBus) -> None:
    rec = Recorder[Tagged]()
    bus.subscribe(Tagged, rec)
    count = 300

    def worker(source: int) -> None:
        for n in range(count):
            bus.publish(Tagged(source, n))

    threads = [threading.Thread(target=worker, args=(source,)) for source in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    await bus.drain()

    assert len(rec.items) == 3 * count
    for source in range(3):
        assert [m.n for m in rec.items if m.source == source] == list(range(count))


async def test_subscribe_from_other_thread_raises(bus: EventBus) -> None:
    with pytest.raises(RuntimeError, match="事件循环线程"):
        await asyncio.to_thread(bus.subscribe, Ping, Recorder[Ping]())


def test_subscribe_without_running_loop_raises() -> None:
    loop = asyncio.new_event_loop()
    try:
        bus = EventBus(loop)
        with pytest.raises(RuntimeError, match="事件循环线程"):
            bus.subscribe(Ping, Recorder[Ping]())
    finally:
        loop.close()


# ---------------------------------------------------------------- 异常处理


def broken(message: Ping) -> None:
    raise ValueError(f"broken {message.n}")


async def test_sync_handler_error_logged_and_others_still_receive(
    bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    rec = Recorder[Ping]()
    bus.subscribe(Ping, broken)
    bus.subscribe(Ping, rec)

    bus.publish(Ping(1))
    bus.publish(Ping(2))
    await bus.drain()

    assert rec.items == [Ping(1), Ping(2)]
    errors = bus_errors(caplog)
    assert len(errors) == 2
    for record in errors:
        assert "Ping" in record.getMessage()
        assert "broken" in record.getMessage()
        assert record.exc_info is not None
        assert isinstance(record.exc_info[1], ValueError)


async def test_async_handler_error_logged_and_others_still_receive(
    bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    received: list[int] = []

    async def broken_async(message: Ping) -> None:
        await asyncio.sleep(0)
        raise ValueError("async boom")

    async def healthy(message: Ping) -> None:
        await asyncio.sleep(0)
        received.append(message.n)

    bus.subscribe(Ping, broken_async)
    bus.subscribe(Ping, healthy)
    bus.publish(Ping(1))
    await bus.drain()  # 默认模式下 drain 不抛出处理函数的异常

    assert received == [1]
    errors = bus_errors(caplog)
    assert len(errors) == 1
    assert "Ping" in errors[0].getMessage()
    assert "broken_async" in errors[0].getMessage()
    assert errors[0].exc_info is not None
    assert isinstance(errors[0].exc_info[1], ValueError)


async def test_raise_errors_reraises_sync_error_into_event_loop(
    strict_bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    loop = asyncio.get_running_loop()
    contexts: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))

    def also_broken(message: Ping) -> None:
        raise KeyError("second")

    rec = Recorder[Ping]()
    strict_bus.subscribe(Ping, broken)
    strict_bus.subscribe(Ping, rec)
    strict_bus.subscribe(Ping, also_broken)
    try:
        strict_bus.publish(Ping(1))
        await strict_bus.drain()
    finally:
        loop.set_exception_handler(None)

    # 先交给全部处理函数，再把第一个异常抛给事件循环；两个异常都有日志。
    assert rec.items == [Ping(1)]
    assert len(contexts) == 1
    assert isinstance(contexts[0]["exception"], ValueError)
    assert len(bus_errors(caplog)) == 2


async def test_raise_errors_collects_async_errors_for_drain(strict_bus: EventBus) -> None:
    received: list[int] = []

    async def broken_async(message: Ping) -> None:
        await asyncio.sleep(0)
        raise ValueError(f"async boom {message.n}")

    strict_bus.subscribe(Ping, broken_async)
    strict_bus.subscribe(Ping, lambda m: received.append(m.n))
    strict_bus.publish(Ping(1))
    strict_bus.publish(Ping(2))

    with pytest.raises(ExceptionGroup) as excinfo:
        await strict_bus.drain()

    assert received == [1, 2]
    assert sorted(str(e) for e in excinfo.value.exceptions) == ["async boom 1", "async boom 2"]
    # 抛出过的异常被清空，下一次 drain 正常返回。
    await strict_bus.drain()


# ---------------------------------------------------------------- 命令


async def test_send_with_one_handler(bus: EventBus, caplog: pytest.LogCaptureFixture) -> None:
    rec = Recorder[DoWork]()
    bus.subscribe(DoWork, rec)
    bus.send(DoWork(1))
    await bus.drain()

    assert rec.items == [DoWork(1)]
    assert bus_errors(caplog) == []


async def test_send_without_handler_logs_error_and_delivers_nothing(
    bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    bus.send(DoWork(1))
    # 发送时没有处理者，命令不入队；之后才订阅的处理者也收不到它。
    late = Recorder[DoWork]()
    bus.subscribe(DoWork, late)
    await bus.drain()

    assert late.items == []
    errors = bus_errors(caplog)
    assert len(errors) == 1
    assert "DoWork" in errors[0].getMessage()
    assert "0" in errors[0].getMessage()


async def test_send_with_two_handlers_logs_error_and_delivers_to_both(
    bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    first = Recorder[DoWork]()
    second = Recorder[DoWork]()
    bus.subscribe(DoWork, first)
    bus.subscribe(DoWork, second)
    bus.send(DoWork(1))
    await bus.drain()

    assert first.items == [DoWork(1)]
    assert second.items == [DoWork(1)]
    errors = bus_errors(caplog)
    assert len(errors) == 1
    assert "DoWork" in errors[0].getMessage()
    assert "2" in errors[0].getMessage()


@pytest.mark.parametrize("handler_count", [0, 2])
async def test_send_raise_errors_rejects_wrong_handler_count(
    strict_bus: EventBus, caplog: pytest.LogCaptureFixture, handler_count: int
) -> None:
    recorders = [Recorder[DoWork]() for _ in range(handler_count)]
    for rec in recorders:
        strict_bus.subscribe(DoWork, rec)

    with pytest.raises(RuntimeError, match=rf"DoWork.*{handler_count}"):
        strict_bus.send(DoWork(1))
    await strict_bus.drain()

    assert all(rec.items == [] for rec in recorders)
    assert bus_errors(caplog) == []


async def test_send_raise_errors_accepts_single_handler(strict_bus: EventBus) -> None:
    rec = Recorder[DoWork]()
    strict_bus.subscribe(DoWork, rec)
    strict_bus.send(DoWork(1))
    await strict_bus.drain()
    assert rec.items == [DoWork(1)]


async def test_publish_rejects_command(bus: EventBus) -> None:
    rec = Recorder[DoWork]()
    bus.subscribe(DoWork, rec)
    with pytest.raises(TypeError, match="send"):
        bus.publish(DoWork(1))  # pyright: ignore[reportArgumentType]
    await bus.drain()
    assert rec.items == []


@pytest.mark.parametrize("message", [Ping(1), Volume(1)])
async def test_send_rejects_non_command(bus: EventBus, message: Message) -> None:
    with pytest.raises(TypeError, match="Command"):
        bus.send(message)  # pyright: ignore[reportArgumentType]


async def test_publish_rejects_bare_message(bus: EventBus) -> None:
    with pytest.raises(TypeError, match="Event 或 State"):
        bus.publish(Message())  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------- State


async def test_state_replayed_to_late_subscriber(bus: EventBus) -> None:
    bus.publish(Volume(1))
    await bus.drain()

    rec = Recorder[Volume]()
    bus.subscribe(Volume, rec)
    assert rec.items == []  # 回放排队执行
    await bus.drain()
    assert rec.items == [Volume(1)]


async def test_state_replay_sends_only_latest_value(bus: EventBus) -> None:
    for level in (1, 2, 3):
        bus.publish(Volume(level))
    await bus.drain()

    rec = Recorder[Volume]()
    bus.subscribe(Volume, rec)
    await bus.drain()
    assert rec.items == [Volume(3)]


async def test_state_replay_only_goes_to_new_subscriber(bus: EventBus) -> None:
    early = Recorder[Volume]()
    bus.subscribe(Volume, early)
    bus.publish(Volume(1))
    await bus.drain()

    late = Recorder[Volume]()
    bus.subscribe(Volume, late)
    await bus.drain()
    assert early.items == [Volume(1)]
    assert late.items == [Volume(1)]

    bus.publish(Volume(2))
    await bus.drain()
    assert early.items == [Volume(1), Volume(2)]
    assert late.items == [Volume(1), Volume(2)]


async def test_state_subscribed_before_dispatch_received_once(bus: EventBus) -> None:
    # 发布后立即订阅：分发和回放都会排队，订阅者只收到一次。
    bus.publish(Volume(1))
    rec = Recorder[Volume]()
    bus.subscribe(Volume, rec)
    await bus.drain()
    assert rec.items == [Volume(1)]


async def test_state_replay_not_duplicated_by_queued_dispatch(bus: EventBus) -> None:
    bus.publish(Volume(1))
    await bus.drain()

    # Volume(2) 的分发排在回放之前，回放的值在订阅时取定为 Volume(2)；订阅者只收到一次。
    bus.publish(Volume(2))
    rec = Recorder[Volume]()
    bus.subscribe(Volume, rec)
    await bus.drain()
    assert rec.items == [Volume(2)]


async def test_state_new_subscriber_skips_values_older_than_latest(bus: EventBus) -> None:
    # 三个值都还在排队时订阅：新订阅者从订阅时刻的最新值开始收，不会先收到旧值。
    for level in (1, 2, 3):
        bus.publish(Volume(level))
    rec = Recorder[Volume]()
    bus.subscribe(Volume, rec)
    bus.publish(Volume(4))
    await bus.drain()
    assert rec.items == [Volume(3), Volume(4)]


async def test_state_replay_then_newer_value(bus: EventBus) -> None:
    bus.publish(Volume(1))
    await bus.drain()

    rec = Recorder[Volume]()
    bus.subscribe(Volume, rec)
    bus.publish(Volume(2))
    await bus.drain()
    assert rec.items == [Volume(1), Volume(2)]


async def test_latest_returns_most_recent_value(bus: EventBus) -> None:
    assert bus.latest(Volume) is None

    bus.publish(Volume(1))
    bus.publish(Volume(2))
    # 最新值在 publish 时更新，不必等分发。
    assert bus.latest(Volume) == Volume(2)
    assert bus.latest(Theme) is None

    bus.publish(Theme("dark"))
    await bus.drain()
    assert bus.latest(Volume) == Volume(2)
    assert bus.latest(Theme) == Theme("dark")


async def test_latest_updated_by_other_thread(bus: EventBus) -> None:
    await asyncio.to_thread(bus.publish, Volume(7))
    assert bus.latest(Volume) == Volume(7)

    rec = Recorder[Volume]()
    bus.subscribe(Volume, rec)
    await bus.drain()
    assert rec.items == [Volume(7)]


async def test_events_are_not_replayed(bus: EventBus) -> None:
    bus.publish(Ping(1))
    await bus.drain()

    rec = Recorder[Ping]()
    bus.subscribe(Ping, rec)
    await bus.drain()
    assert rec.items == []


# ---------------------------------------------------------------- 订阅取消


async def test_cancel_drops_queued_messages(bus: EventBus) -> None:
    rec = Recorder[Ping]()
    sub = bus.subscribe(Ping, rec)
    bus.publish(Ping(1))
    sub.cancel()
    await bus.drain()

    assert not sub.active
    assert rec.items == []


async def test_cancel_is_idempotent(bus: EventBus) -> None:
    other = Recorder[Ping]()
    sub = bus.subscribe(Ping, Recorder[Ping]())
    bus.subscribe(Ping, other)

    sub.cancel()
    sub.cancel()
    bus.publish(Ping(1))
    await bus.drain()

    assert not sub.active
    assert other.items == [Ping(1)]


async def test_cancel_during_delivery(bus: EventBus) -> None:
    later = Recorder[Ping]()
    self_cancelling: list[int] = []
    subs: dict[str, Subscription] = {}

    def canceller(message: Ping) -> None:
        subs["later"].cancel()

    def once(message: Ping) -> None:
        self_cancelling.append(message.n)
        subs["once"].cancel()

    bus.subscribe(Ping, canceller)
    subs["once"] = bus.subscribe(Ping, once)
    subs["later"] = bus.subscribe(Ping, later)

    bus.publish(Ping(1))
    bus.publish(Ping(2))
    await bus.drain()

    # later 排在 canceller 之后，同一条消息还没轮到它就被取消了。
    assert later.items == []
    # once 在处理 Ping(1) 时取消自己，已经排队的 Ping(2) 不再交给它。
    assert self_cancelling == [1]


async def test_cancel_drops_pending_state_replay(bus: EventBus) -> None:
    bus.publish(Volume(1))
    await bus.drain()

    rec = Recorder[Volume]()
    sub = bus.subscribe(Volume, rec)
    sub.cancel()
    await bus.drain()
    assert rec.items == []


async def test_cancel_from_other_thread(bus: EventBus) -> None:
    rec = Recorder[Ping]()
    sub = bus.subscribe(Ping, rec)
    await asyncio.to_thread(sub.cancel)

    bus.publish(Ping(1))
    await bus.drain()
    assert not sub.active
    assert rec.items == []


# ---------------------------------------------------------------- drain 与 aclose


async def test_drain_waits_for_coroutine_handlers(bus: EventBus) -> None:
    loop = asyncio.get_running_loop()
    gate = asyncio.Event()
    finished: list[int] = []

    async def slow(message: Ping) -> None:
        await gate.wait()
        finished.append(message.n)

    bus.subscribe(Ping, slow)
    bus.publish(Ping(1))
    loop.call_later(0.02, gate.set)
    await asyncio.wait_for(bus.drain(), timeout=5)

    assert gate.is_set()
    assert finished == [1]


async def test_drain_waits_for_messages_published_by_handlers(bus: EventBus) -> None:
    pongs: list[int] = []

    async def relay(message: Ping) -> None:
        await asyncio.sleep(0.01)
        bus.publish(Pong(message.n))

    async def on_pong(message: Pong) -> None:
        await asyncio.sleep(0.01)
        pongs.append(message.n)

    bus.subscribe(Ping, relay)
    bus.subscribe(Pong, on_pong)
    bus.publish(Ping(1))
    bus.publish(Ping(2))
    await asyncio.wait_for(bus.drain(), timeout=5)

    assert sorted(pongs) == [1, 2]


async def test_drain_returns_when_idle(bus: EventBus) -> None:
    await asyncio.wait_for(bus.drain(), timeout=1)


async def test_drain_inside_coroutine_handler_raises(bus: EventBus) -> None:
    outcome: list[str] = []

    async def handler(message: Ping) -> None:
        try:
            await bus.drain()
        except RuntimeError:
            outcome.append("rejected")

    bus.subscribe(Ping, handler)
    bus.publish(Ping(1))
    await asyncio.wait_for(bus.drain(), timeout=5)
    assert outcome == ["rejected"]


async def test_aclose_cancels_running_handler_tasks(bus: EventBus) -> None:
    started = asyncio.Event()
    cancelled: list[int] = []

    async def forever(message: Ping) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(message.n)
            raise

    bus.subscribe(Ping, forever)
    bus.publish(Ping(1))
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.wait_for(bus.aclose(), timeout=5)

    assert cancelled == [1]
    await asyncio.wait_for(bus.drain(), timeout=1)


async def test_aclose_drops_queued_and_new_messages(bus: EventBus, caplog: pytest.LogCaptureFixture) -> None:
    rec = Recorder[Ping]()
    bus.subscribe(Ping, rec)
    bus.publish(Ping(1))
    await bus.aclose()
    await bus.aclose()  # 幂等

    bus.publish(Ping(2))
    bus.send(DoWork(3))  # 关闭后直接丢弃，不检查处理者数量
    await bus.drain()

    assert rec.items == []
    assert bus_errors(caplog) == []
    with pytest.raises(RuntimeError, match="关闭"):
        bus.subscribe(Ping, rec)


# ---------------------------------------------------------------- 性质测试

_operations = st.lists(
    st.one_of(
        st.just(("publish", 0)),
        st.just(("publish_in_thread", 0)),
        st.just(("subscribe", 0)),
        st.tuples(st.just("cancel"), st.integers(min_value=0, max_value=20)),
        st.just(("yield", 0)),
    ),
    max_size=40,
)


class _StateProbe:
    """一个 Volume 订阅者，记录收到的值以及订阅、取消时的进度。"""

    def __init__(self, bus: EventBus, published_before: int) -> None:
        self.received: list[int] = []
        self.published_before = published_before
        self.received_at_cancel: int | None = None
        self.subscription = bus.subscribe(Volume, self._on_volume)

    def _on_volume(self, message: Volume) -> None:
        self.received.append(message.level)


async def _run_state_scenario(operations: list[tuple[str, int]]) -> None:
    bus = EventBus(asyncio.get_running_loop())
    published: list[int] = []
    probes: list[_StateProbe] = []
    for name, arg in operations:
        if name == "publish":
            published.append(len(published) + 1)
            bus.publish(Volume(published[-1]))
        elif name == "publish_in_thread":
            published.append(len(published) + 1)
            await asyncio.to_thread(bus.publish, Volume(published[-1]))
        elif name == "subscribe":
            probes.append(_StateProbe(bus, len(published)))
        elif name == "cancel":
            if probes:
                probe = probes[arg % len(probes)]
                probe.subscription.cancel()
                if probe.received_at_cancel is None:
                    probe.received_at_cancel = len(probe.received)
        else:
            await asyncio.sleep(0)
    await bus.drain()

    for probe in probes:
        # 应收序列：订阅时刻的最新值（若有），加上订阅之后发布的每一个值。
        expected = published[max(probe.published_before - 1, 0) :]
        assert all(a < b for a, b in pairwise(probe.received))
        if probe.received_at_cancel is None:
            assert probe.received == expected
        else:
            # 取消前收到的是应收序列的前缀，取消后一个也不再收到。
            assert len(probe.received) == probe.received_at_cancel
            assert probe.received == expected[: len(probe.received)]
    await bus.aclose()


@settings(max_examples=300, deadline=None)
@given(_operations)
def test_state_delivery_properties(operations: list[tuple[str, int]]) -> None:
    asyncio.run(_run_state_scenario(operations))
