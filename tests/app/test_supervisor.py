"""推理进程监管器测试（架构文档 8.2 节）。

每个测试用 tests/app/fake_worker.py 启动真实的子进程，经命名管道按协议通信。
退避等待和崩溃窗口的时钟通过注入的 sleep、clock 跳过；其余时限都设得很短。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import sys
import time
import zlib
from collections.abc import Callable
from dataclasses import replace

import pytest

from lst.app import jobobject
from lst.app.supervisor import SupervisedRequest, WorkerSupervisor
from lst.contracts.errors import ErrorCode
from lst.contracts.ports import WorkerChannel, WorkerState
from lst.contracts.worker_protocol import (
    CANCELLED,
    PROTOCOL_VERSION,
    ExitCode,
    JsonValue,
    OcrRequest,
    OcrResult,
    Progress,
    RequestDone,
    RequestFailed,
    SegmentResult,
    Started,
    TranslateRequest,
    TranslateSegment,
    WorkerReply,
)
from tests.app.conftest import FAST, ROOT, FakeTime, GatedSleep, SupervisorFactory
from tests.app.fake_worker import SEEN_PROBE

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="推理进程通过 Windows 命名管道通信")

S = WorkerState
LOGGER = "lst.app.supervisor"
_probe_ids = itertools.count()


def translate(request_id: str, *texts: str) -> TranslateRequest:
    segments = tuple(TranslateSegment(f"g{i}", text) for i, text in enumerate(texts, 1))
    return TranslateRequest(request_id, "s1", "en", "zh-Hans", segments)


async def collect(handle: SupervisedRequest, timeout: float = 10.0) -> list[WorkerReply]:
    async def run() -> list[WorkerReply]:
        return [reply async for reply in handle.replies()]

    return await asyncio.wait_for(run(), timeout)


async def wait_state(
    supervisor: WorkerSupervisor, *states: WorkerState, timeout: float = 20.0
) -> WorkerState:
    return await asyncio.wait_for(supervisor.wait_for_state(*states), timeout)


async def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def failure(replies: list[WorkerReply]) -> RequestFailed:
    last = replies[-1]
    assert isinstance(last, RequestFailed), replies
    return last


async def seen_by_worker(supervisor: WorkerSupervisor) -> list[str]:
    """子进程到目前为止收到的请求号与取消记录，不含这次探测本身。"""
    probe = f"probe{next(_probe_ids)}"
    replies = await collect(supervisor.submit(translate(probe, SEEN_PROBE), timeout_s=5))
    segment = replies[1]
    assert isinstance(segment, SegmentResult) and segment.text is not None
    return [item for item in segment.text.split(",") if item and item != probe]


async def started(supervisor: WorkerSupervisor) -> None:
    await supervisor.start()
    await wait_state(supervisor, S.READY)


# ---------------------------------------------------------------- 启动与请求


async def test_start_reaches_ready_and_reports_capabilities(make_supervisor: SupervisorFactory) -> None:
    capabilities: dict[str, JsonValue] = {"pairs": [["en", "zh-Hans"]], "device": "cpu"}
    supervisor, log = make_supervisor({"capabilities": capabilities})
    channel: WorkerChannel = supervisor  # 监管器满足端口协议
    assert channel.capabilities is None

    await supervisor.start()
    assert supervisor.state is S.STARTING
    await wait_state(supervisor, S.READY)

    assert channel.capabilities == capabilities
    assert log.states == [S.STARTING, S.LOADING, S.READY]
    assert supervisor.pid is not None


async def test_reply_stream_in_order_and_busy_ready_toggle(make_supervisor: SupervisorFactory) -> None:
    supervisor, log = make_supervisor({"segment_delay": 0.05})
    await started(supervisor)

    handle = supervisor.submit(translate("r1", "a", "b"), timeout_s=5)
    assert supervisor.state is S.BUSY
    replies = await collect(handle)

    assert replies == [
        Started("r1"),
        SegmentResult("r1", "g1", text="<a>"),
        SegmentResult("r1", "g2", text="<b>"),
        RequestDone("r1"),
    ]
    assert handle.done
    assert supervisor.state is S.READY
    assert log.states == [S.STARTING, S.LOADING, S.READY, S.BUSY, S.READY]


async def test_requests_before_ready_are_queued_and_sent_in_order(make_supervisor: SupervisorFactory) -> None:
    supervisor, log = make_supervisor({"ready_delay": 0.5})
    await supervisor.start()
    handles = [supervisor.submit(translate(f"r{i}", "x"), timeout_s=5) for i in (1, 2, 3)]
    assert supervisor.state in (S.STARTING, S.LOADING)

    for handle in handles:
        assert (await collect(handle))[-1] == RequestDone(handle.request_id)
    assert await seen_by_worker(supervisor) == ["r1", "r2", "r3"]
    # 三个请求在 READY 之后一次发出：READY → BUSY，全部结束后回到 READY。
    assert log.states[:5] == [S.STARTING, S.LOADING, S.READY, S.BUSY, S.READY]


async def test_worker_output_is_drained_logged_and_progress_forwarded(
    make_supervisor: SupervisorFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    progress: list[Progress] = []
    # 60 万字节的标准输出远超管道缓冲区；监管器不读走的话，子进程会阻塞在写入上，永远到不了 Ready。
    supervisor, _ = make_supervisor(
        {"stdout_lines": 5000, "stderr_text": "加载警告：示例", "progress_steps": 3},
        on_progress=progress.append,
    )
    await started(supervisor)

    assert [(p.done, p.total) for p in progress] == [(1, 3), (2, 3), (3, 3)]

    def logged(level: int, text: str) -> bool:
        return any(r.levelno == level and text in r.getMessage() for r in caplog.records)

    assert await wait_until(lambda: logged(logging.WARNING, "加载警告：示例"))
    assert await wait_until(lambda: logged(logging.DEBUG, "stdout line 4999"))


async def test_large_frame_is_sent_off_the_event_loop(make_supervisor: SupervisorFactory) -> None:
    # 子进程收到 OCR 请求的 JSON 帧后推迟 1 秒才读像素帧，写线程的发送在这段时间里一直阻塞。
    supervisor, _ = make_supervisor({"payload_read_delay": 1.0})
    await started(supervisor)
    width, height = 1920, 1080
    stride = width * 4
    payload = bytes(range(256)) * (stride * height // 256)

    gaps: list[float] = []
    done = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticking = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    begin = time.perf_counter()
    handle = supervisor.submit(OcrRequest("ocr1", "s1", width, height, stride), payload=payload, timeout_s=10)
    submit_cost = time.perf_counter() - begin
    replies = await collect(handle)
    elapsed = time.perf_counter() - begin
    done.set()
    await ticking

    result = replies[-1]
    assert isinstance(result, OcrResult)
    assert result.blocks == [{"bytes": len(payload), "crc32": zlib.crc32(payload)}]
    assert elapsed >= 1.0
    assert submit_cost < 0.05
    # 事件循环在发送阻塞的 1 秒里照常运行：相邻两次计时之间没有出现接近 1 秒的空档。
    assert max(gaps) < 0.3, max(gaps)


async def test_submit_validates_arguments(make_supervisor: SupervisorFactory) -> None:
    supervisor, _ = make_supervisor({"ready_delay": 5.0}, timings=replace(FAST, shutdown_grace_s=0.3))
    await supervisor.start()
    with pytest.raises(ValueError):
        supervisor.submit(OcrRequest("o", "s", 1, 1, 4), timeout_s=1)
    with pytest.raises(ValueError):
        supervisor.submit(translate("t", "a"), payload=b"x", timeout_s=1)
    with pytest.raises(ValueError):
        supervisor.submit(translate("t", "a"), timeout_s=0)
    supervisor.submit(translate("dup", "a"), timeout_s=1)
    with pytest.raises(ValueError):
        supervisor.submit(translate("dup", "b"), timeout_s=1)


async def test_submit_before_start_fails_immediately(make_supervisor: SupervisorFactory) -> None:
    supervisor, _ = make_supervisor()
    handle = supervisor.submit(translate("r1", "a"), timeout_s=1)
    assert handle.done
    result = failure(await collect(handle, 0.5))
    assert result.code == ErrorCode.WORKER_CRASHED
    assert "未运行" in result.error


async def test_replies_for_finished_requests_are_dropped(
    make_supervisor: SupervisorFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    # 子进程在 r1 结束后再发一遍 SegmentResult 和 RequestDone。
    supervisor, _ = make_supervisor({"late_duplicates": ["r1"]})
    await started(supervisor)

    replies = await collect(supervisor.submit(translate("r1", "secret text"), timeout_s=5))
    assert replies == [Started("r1"), SegmentResult("r1", "g1", text="<secret text>"), RequestDone("r1")]

    def dropped() -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.DEBUG and "丢弃不属于在途请求的回复" in r.getMessage()
        ]

    assert await wait_until(lambda: len(dropped()) == 2)
    assert all("request_id=r1" in message and "late" not in message for message in dropped())
    assert (await collect(supervisor.submit(translate("r2", "b"), timeout_s=5)))[-1] == RequestDone("r2")


# ---------------------------------------------------------------- 取消


async def test_cancel_request_queued_in_supervisor(make_supervisor: SupervisorFactory) -> None:
    supervisor, _ = make_supervisor({"ready_delay": 0.5})
    await supervisor.start()
    handle = supervisor.submit(translate("r1", "a"), timeout_s=5)

    handle.cancel()
    handle.cancel()
    replies = await collect(handle, 0.2)  # 不等子进程，立即结束

    assert len(replies) == 1
    assert failure(replies).code == CANCELLED
    await wait_state(supervisor, S.READY)
    assert await seen_by_worker(supervisor) == []  # 从未发给子进程


async def test_cancel_request_already_sent_to_worker(make_supervisor: SupervisorFactory) -> None:
    supervisor, _ = make_supervisor({"segment_delay": 0.3})
    await started(supervisor)
    running = supervisor.submit(translate("running", "a"), timeout_s=5)
    waiting = supervisor.submit(translate("waiting", "b"), timeout_s=5)  # 在子进程的队列里排在 running 后面
    running_replies = running.replies()
    assert await asyncio.wait_for(anext(running_replies), 5) == Started("running")

    waiting.cancel()  # 子进程还没开始处理：它丢弃请求并回复 cancelled
    running.cancel()  # 子进程已经开始：照常跑完，结果交给消费者
    running.cancel()

    assert await collect(waiting) == [RequestFailed("waiting", CANCELLED, "取消")]

    async def rest() -> list[WorkerReply]:
        return [reply async for reply in running_replies]

    assert await asyncio.wait_for(rest(), 5) == [
        SegmentResult("running", "g1", text="<a>"),
        RequestDone("running"),
    ]
    # 重复取消只发出一条 Cancel。
    assert await seen_by_worker(supervisor) == ["running", "waiting", "cancel:waiting", "cancel:running"]


async def test_cancel_after_finish_has_no_effect(make_supervisor: SupervisorFactory) -> None:
    supervisor, _ = make_supervisor()
    await started(supervisor)
    handle = supervisor.submit(translate("r1", "a"), timeout_s=5)
    assert (await collect(handle))[-1] == RequestDone("r1")

    handle.cancel()
    handle.cancel()

    assert await collect(handle, 0.5) == []
    assert await seen_by_worker(supervisor) == ["r1"]  # 没有发出 Cancel


# ---------------------------------------------------------------- 处理时限


async def test_timeout_counts_from_started_not_from_queueing(make_supervisor: SupervisorFactory) -> None:
    # 每个请求处理 0.4 秒，时限 0.7 秒。
    supervisor, log = make_supervisor({"ready_delay": 1.0, "segment_delay": 0.4})
    await supervisor.start()
    # 在监管器里排队约 1 秒，超过时限，但排队时间不计入。
    queued = supervisor.submit(translate("queued", "a"), timeout_s=0.7)
    # 在子进程的队列里等 queued 处理完，再处理 0.4 秒：从发出算起约 0.8 秒，从 Started 算起约 0.4 秒。
    behind = supervisor.submit(translate("behind", "b"), timeout_s=0.7)

    assert (await collect(queued))[-1] == RequestDone("queued")
    assert (await collect(behind))[-1] == RequestDone("behind")
    assert S.CRASHED not in log.states


async def test_processing_timeout_fails_request_and_restarts_worker(
    make_supervisor: SupervisorFactory,
) -> None:
    supervisor, log = make_supervisor({"hang_on": ["stuck"]})
    await started(supervisor)
    first_pid = supervisor.pid
    stuck = supervisor.submit(translate("stuck", "a"), timeout_s=0.3)
    other = supervisor.submit(translate("other", "b"), timeout_s=30)  # 同一进程上的另一个在途请求

    stuck_replies = await collect(stuck)
    assert stuck_replies[0] == Started("stuck")
    assert failure(stuck_replies).code == ErrorCode.TIMEOUT
    assert failure(await collect(other)).code == ErrorCode.WORKER_CRASHED

    assert S.CRASHED in log.states
    assert "超时" in log.details[log.states.index(S.CRASHED)]
    await wait_state(supervisor, S.READY)
    assert supervisor.pid != first_pid
    assert supervisor.recent_crashes == 1
    assert (await collect(supervisor.submit(translate("after", "c"), timeout_s=5)))[-1] == RequestDone(
        "after"
    )


# ---------------------------------------------------------------- 崩溃与重启


async def test_crash_fails_inflight_and_queued_requests_complete_after_restart(
    make_supervisor: SupervisorFactory,
) -> None:
    gate = GatedSleep()
    supervisor, log = make_supervisor({"crash_on": {"boom": "exit"}}, sleep=gate)
    await started(supervisor)
    boom = supervisor.submit(translate("boom", "a"), timeout_s=5)
    sibling = supervisor.submit(translate("sibling", "b"), timeout_s=5)

    boom_replies = await collect(boom)
    assert boom_replies[0] == Started("boom")
    assert failure(boom_replies).code == ErrorCode.WORKER_CRASHED
    assert failure(await collect(sibling)).code == ErrorCode.WORKER_CRASHED
    await wait_state(supervisor, S.CRASHED)
    assert "退出码 3" in supervisor.state_detail

    later = supervisor.submit(translate("later", "c"), timeout_s=5)  # CRASHED 期间在监管器里排队
    await asyncio.sleep(0.1)
    assert not later.done
    gate.gate.set()

    assert (await collect(later))[-1] == RequestDone("later")
    assert gate.calls == [FAST.backoff_s[0]]
    assert log.states == [
        S.STARTING,
        S.LOADING,
        S.READY,
        S.BUSY,
        S.CRASHED,
        S.STARTING,
        S.LOADING,
        S.READY,
        S.BUSY,
        S.READY,
    ]


async def test_native_crash_is_detected_and_faulthandler_output_logged(
    make_supervisor: SupervisorFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    gate = GatedSleep()
    supervisor, _ = make_supervisor({"crash_on": {"segv": "segfault"}}, sleep=gate)
    await started(supervisor)

    replies = await collect(supervisor.submit(translate("segv", "a"), timeout_s=5))

    assert failure(replies).code == ErrorCode.WORKER_CRASHED
    await wait_state(supervisor, S.CRASHED)
    assert "0xC0000005" in supervisor.state_detail
    stderr = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and " stderr]" in r.getMessage()
    ]
    assert any("Windows fatal exception: access violation" in line for line in stderr), stderr
    assert any("fake_worker.py" in line for line in stderr), stderr  # 崩溃时的 Python 堆栈


@pytest.mark.parametrize(
    "code", [ExitCode.PROTOCOL_MISMATCH, ExitCode.DEPENDENCY_ERROR, ExitCode.CONFIG_ERROR]
)
async def test_deterministic_exit_code_fails_without_restart(
    make_supervisor: SupervisorFactory, code: ExitCode
) -> None:
    supervisor, log = make_supervisor({"exit_on_configure": int(code)})
    await supervisor.start()
    queued = supervisor.submit(translate("r1", "a"), timeout_s=5)

    await wait_state(supervisor, S.FAILED)
    assert code.name in supervisor.state_detail
    result = failure(await collect(queued, 1))
    assert result.code == ErrorCode.WORKER_CRASHED
    assert "已停用" in result.error

    await asyncio.sleep(0.3)
    assert log.states == [S.STARTING, S.LOADING, S.FAILED]


async def test_deterministic_exit_before_connecting(make_supervisor: SupervisorFactory) -> None:
    supervisor, log = make_supervisor(args=["--exit-code", str(int(ExitCode.DEPENDENCY_ERROR))])
    await supervisor.start()
    await wait_state(supervisor, S.FAILED)
    assert "DEPENDENCY_ERROR" in supervisor.state_detail
    assert log.states == [S.STARTING, S.FAILED]


async def test_protocol_version_mismatch_in_ready_fails(make_supervisor: SupervisorFactory) -> None:
    supervisor, log = make_supervisor({"protocol_version": PROTOCOL_VERSION + 1})
    await supervisor.start()
    await wait_state(supervisor, S.FAILED)
    assert "协议版本不符" in supervisor.state_detail
    assert log.states == [S.STARTING, S.LOADING, S.FAILED]
    assert supervisor.capabilities is None


async def test_missing_interpreter_fails(make_supervisor: SupervisorFactory) -> None:
    supervisor, log = make_supervisor(command=[str(ROOT / "no-such-python.exe")])
    await supervisor.start()
    await wait_state(supervisor, S.FAILED)
    assert "无法启动" in supervisor.state_detail
    assert log.states == [S.STARTING, S.FAILED]


async def test_worker_that_never_connects_is_treated_as_crash(make_supervisor: SupervisorFactory) -> None:
    gate = GatedSleep()
    supervisor, log = make_supervisor(
        args=["--no-connect"], sleep=gate, timings=replace(FAST, accept_timeout_s=0.5)
    )
    await supervisor.start()
    await wait_state(supervisor, S.CRASHED)
    assert "没有连接管道" in supervisor.state_detail
    assert log.states == [S.STARTING, S.CRASHED]
    # accept 线程已被唤醒并退出，夹具在测试结束时检查线程没有残留。


async def test_worker_that_never_becomes_ready_is_treated_as_crash(
    make_supervisor: SupervisorFactory,
) -> None:
    gate = GatedSleep()
    supervisor, log = make_supervisor(
        {"ready_delay": 30.0}, sleep=gate, timings=replace(FAST, ready_timeout_s=0.5)
    )
    await supervisor.start()
    await wait_state(supervisor, S.CRASHED)
    assert "没有就绪" in supervisor.state_detail
    assert log.states == [S.STARTING, S.LOADING, S.CRASHED]


async def test_fifth_crash_within_window_enters_failed(make_supervisor: SupervisorFactory) -> None:
    fake = FakeTime()
    supervisor, log = make_supervisor(
        args=["--exit-code", "3"],
        clock=fake.clock,
        sleep=fake.sleep,
        timings=replace(FAST, backoff_s=(1.0, 2.0, 4.0, 8.0)),
    )
    await supervisor.start()
    await wait_state(supervisor, S.FAILED, timeout=40)

    assert fake.sleeps == [1.0, 2.0, 4.0, 8.0]
    assert log.states.count(S.STARTING) == 5
    assert log.states.count(S.CRASHED) == 4
    assert log.states[-1] is S.FAILED
    assert "10 分钟内崩溃 5 次" in supervisor.state_detail


async def test_backoff_holds_at_last_step_when_crashes_are_spread_out(
    make_supervisor: SupervisorFactory,
) -> None:
    # 每次退避之外再过 150 秒：10 分钟窗口里最多同时有 4 次崩溃，永远到不了第 5 次。
    fake = FakeTime(extra_per_sleep=150.0)
    supervisor, log = make_supervisor(
        args=["--exit-code", "3"],
        clock=fake.clock,
        sleep=fake.sleep,
        timings=replace(FAST, backoff_s=(1.0, 2.0, 4.0, 8.0)),
    )
    await supervisor.start()
    assert await wait_until(lambda: len(fake.sleeps) >= 7, timeout=40)

    assert fake.sleeps[:7] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0]
    assert S.FAILED not in log.states


async def test_failed_rejects_submit_and_retry_recovers(make_supervisor: SupervisorFactory) -> None:
    fake = FakeTime()
    booms = [f"boom{i}" for i in range(5)]
    supervisor, _ = make_supervisor(
        {"crash_on": dict.fromkeys(booms, "exit")}, clock=fake.clock, sleep=fake.sleep
    )
    await supervisor.start()
    for boom in booms:
        await wait_state(supervisor, S.READY)
        assert failure(await collect(supervisor.submit(translate(boom, "x"), timeout_s=5))).code == (
            ErrorCode.WORKER_CRASHED
        )
    await wait_state(supervisor, S.FAILED)

    rejected = supervisor.submit(translate("after", "x"), timeout_s=5)
    assert rejected.done
    result = failure(await collect(rejected, 0.5))
    assert result.code == ErrorCode.WORKER_CRASHED
    assert "已停用" in result.error

    supervisor.retry()
    assert supervisor.state is S.STARTING
    await wait_state(supervisor, S.READY)
    assert supervisor.recent_crashes == 0
    assert (await collect(supervisor.submit(translate("after", "x"), timeout_s=5)))[-1] == RequestDone(
        "after"
    )


# ---------------------------------------------------------------- 停止与重载


async def test_stop_shuts_worker_down_and_cancels_unfinished_requests(
    make_supervisor: SupervisorFactory,
) -> None:
    supervisor, log = make_supervisor({"hang_on": ["stuck"]})
    await started(supervisor)
    stuck = supervisor.submit(translate("stuck", "a"), timeout_s=30)
    stuck_replies = stuck.replies()
    assert await asyncio.wait_for(anext(stuck_replies), 5) == Started("stuck")

    begin = time.monotonic()
    await supervisor.stop()
    elapsed = time.monotonic() - begin

    # 子进程收到 Shutdown 后自行以 0 退出，不需要等到强制结束。
    assert elapsed < FAST.shutdown_grace_s
    assert supervisor.last_exit_code == 0
    rest = [reply async for reply in stuck_replies]
    assert len(rest) == 1 and failure(rest).code == CANCELLED
    assert log.states[-1] is S.STOPPED
    assert supervisor.pid is None
    assert failure(await collect(supervisor.submit(translate("late", "a"), timeout_s=1), 0.5)).code == (
        ErrorCode.WORKER_CRASHED
    )


async def test_stop_kills_worker_that_ignores_shutdown(make_supervisor: SupervisorFactory) -> None:
    supervisor, _ = make_supervisor({"ignore_shutdown": True}, timings=replace(FAST, shutdown_grace_s=0.5))
    await started(supervisor)

    begin = time.monotonic()
    await supervisor.stop()
    elapsed = time.monotonic() - begin

    assert 0.5 <= elapsed < 3.0
    assert supervisor.last_exit_code == 1  # TerminateProcess 的退出码


async def test_stop_while_loading_cancels_queued_requests(make_supervisor: SupervisorFactory) -> None:
    supervisor, log = make_supervisor({"ready_delay": 30.0}, timings=replace(FAST, shutdown_grace_s=0.3))
    await supervisor.start()
    await wait_state(supervisor, S.LOADING)
    queued = supervisor.submit(translate("r1", "a"), timeout_s=5)

    await supervisor.stop()

    assert failure(await collect(queued, 0.5)).code == CANCELLED
    assert log.states == [S.STARTING, S.LOADING, S.STOPPED]


async def test_stop_during_backoff(make_supervisor: SupervisorFactory) -> None:
    gate = GatedSleep()
    supervisor, log = make_supervisor(args=["--exit-code", "3"], sleep=gate)
    await supervisor.start()
    await wait_state(supervisor, S.CRASHED)
    queued = supervisor.submit(translate("r1", "a"), timeout_s=5)

    await asyncio.wait_for(supervisor.stop(), 1.0)

    assert failure(await collect(queued, 0.5)).code == CANCELLED
    assert log.states == [S.STARTING, S.CRASHED, S.STOPPED]


async def test_reload_restarts_with_new_settings(make_supervisor: SupervisorFactory) -> None:
    supervisor, log = make_supervisor({"capabilities": {"v": 1}, "hang_on": ["stuck"]})
    await started(supervisor)
    old_pid = supervisor.pid
    stuck = supervisor.submit(translate("stuck", "a"), timeout_s=30)
    stuck_replies = stuck.replies()
    assert await asyncio.wait_for(anext(stuck_replies), 5) == Started("stuck")

    await supervisor.reload({"capabilities": {"v": 2}})

    # 已发给旧进程的请求以 cancelled 结束。
    rest = [reply async for reply in stuck_replies]
    assert len(rest) == 1 and failure(rest).code == CANCELLED
    await wait_state(supervisor, S.READY)
    assert supervisor.capabilities == {"v": 2}
    assert supervisor.pid != old_pid
    # 重载直接回到 STARTING，中间没有 STOPPED 或 CRASHED。
    assert log.states == [S.STARTING, S.LOADING, S.READY, S.BUSY, S.STARTING, S.LOADING, S.READY]


async def test_reload_from_failed_starts_with_new_settings(make_supervisor: SupervisorFactory) -> None:
    supervisor, log = make_supervisor({"exit_on_configure": int(ExitCode.CONFIG_ERROR)})
    await supervisor.start()
    await wait_state(supervisor, S.FAILED)

    await supervisor.reload({"capabilities": {"fixed": True}})

    await wait_state(supervisor, S.READY)
    assert supervisor.capabilities == {"fixed": True}
    assert log.states == [S.STARTING, S.LOADING, S.FAILED, S.STARTING, S.LOADING, S.READY]


# ---------------------------------------------------------------- 作业对象


async def test_worker_process_is_assigned_to_kill_on_close_job(make_supervisor: SupervisorFactory) -> None:
    supervisor, _ = make_supervisor()
    await started(supervisor)
    pid = supervisor.pid
    assert pid is not None
    assert jobobject.contains_process(pid)
    assert pid in jobobject.process_ids()
