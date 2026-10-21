"""NMT 推理进程的进程级测试：启动真实子进程，后端换成假实现（tests/workers/fake_backend.py）。

覆盖握手与确定性故障、FIFO 顺序、取消、运行中重新配置、Shutdown、连接断开、崩溃与段错误、段级错误。
"""

import time
from collections.abc import Callable
from typing import Any

import pytest

from lst.contracts.errors import ErrorCode
from lst.contracts.worker_protocol import (
    CANCELLED,
    PROTOCOL_VERSION,
    Cancel,
    Configure,
    ExitCode,
    Hello,
    JsonValue,
    OcrRequest,
    Progress,
    Ready,
    RequestDone,
    RequestFailed,
    SegmentResult,
    Shutdown,
    Started,
    TranslateRequest,
    TranslateSegment,
    WorkerMessage,
)
from tests.workers.harness import WorkerExited, WorkerProcess

ACCESS_VIOLATION = 0xC0000005


def translate(request_id: str, *texts: str, source: str = "en", target: str = "zh-Hans") -> TranslateRequest:
    segments = tuple(TranslateSegment(f"{request_id}-{i}", text) for i, text in enumerate(texts))
    return TranslateRequest(request_id, "s1", source, target, segments)


def done_or_failed(request_id: str) -> Callable[[WorkerMessage], bool]:
    """谓词：消息是 request_id 的结束消息（RequestDone 或 RequestFailed）。"""

    def predicate(message: WorkerMessage) -> bool:
        return isinstance(message, RequestDone | RequestFailed) and message.request_id == request_id

    return predicate


def for_request(messages: list[WorkerMessage], request_id: str) -> list[WorkerMessage]:
    return [m for m in messages if getattr(m, "request_id", None) == request_id]


# ---------------------------------------------------------------- 握手与确定性故障


def test_ready_carries_capabilities_and_versions() -> None:
    with WorkerProcess() as worker:
        ready = worker.handshake({"pairs": [["en", "zh-Hans"], ["ja", "en"]], "pivot": "en"})
    assert ready.protocol_version == PROTOCOL_VERSION
    assert ready.capabilities["pairs"] == [["en", "zh-Hans"], ["ja", "en"]]
    assert ready.capabilities["pivot"] == "en"
    assert isinstance(ready.capabilities["fingerprint"], str)
    assert ready.versions == {"fake": "1.0"}


def test_progress_is_reported_while_loading() -> None:
    with WorkerProcess() as worker:
        worker.handshake({"load_delay": 0.2})
        progress = worker.progress
    assert progress, "加载期间应发送 Progress"
    assert all(isinstance(p, Progress) and p.kind == "model_load" for p in progress)
    assert progress[-1].done == progress[-1].total


def test_protocol_version_mismatch_exits_64() -> None:
    with WorkerProcess() as worker:
        worker.send(Hello(PROTOCOL_VERSION + 1))
        assert worker.wait_exit() == ExitCode.PROTOCOL_MISMATCH
    assert "协议版本不符" in worker.stderr


def test_first_message_must_be_hello() -> None:
    with WorkerProcess() as worker:
        worker.send(Configure({}))
        assert worker.wait_exit() == ExitCode.PROTOCOL_MISMATCH


@pytest.mark.parametrize(
    ("load_error", "code"),
    [
        ("import", ExitCode.DEPENDENCY_ERROR),
        ("oserror", ExitCode.DEPENDENCY_ERROR),
        ("config", ExitCode.CONFIG_ERROR),
        ("crash", 1),
    ],
)
def test_load_failure_exit_codes(load_error: str, code: int) -> None:
    with WorkerProcess() as worker:
        worker.send(Hello(PROTOCOL_VERSION))
        worker.send(Configure({"load_error": load_error}))
        assert worker.wait_exit() == code


# 子进程在连接之前就应退出的场景不建监听端，给一个不存在的管道地址。
UNUSED_PIPE = r"\\.\pipe\lst-test-unused"


def test_unimportable_backend_exits_with_dependency_error() -> None:
    with WorkerProcess("tests.workers.no_such_module:Backend", address=UNUSED_PIPE) as worker:
        assert worker.wait_exit() == ExitCode.DEPENDENCY_ERROR


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({"backend": "not-a-spec", "address": UNUSED_PIPE}, id="bad-backend-spec"),
        pytest.param({"stdin_line": b"not hex\n", "address": UNUSED_PIPE}, id="bad-authkey"),
        pytest.param({"stdin_line": b"", "address": UNUSED_PIPE}, id="empty-stdin"),
        pytest.param({"extra_args": ["--unknown"], "address": UNUSED_PIPE}, id="bad-arg"),
        pytest.param({"child_authkey": b"wrong key"}, id="wrong-authkey"),
    ],
)
def test_invalid_startup_exits_with_config_error(options: dict[str, Any]) -> None:
    with WorkerProcess(**options) as worker:
        assert worker.wait_exit() == ExitCode.CONFIG_ERROR


# ---------------------------------------------------------------- 顺序


def test_requests_are_processed_in_fifo_order() -> None:
    with WorkerProcess() as worker:
        worker.handshake({"segment_delay": 0.02})
        ids = [f"r{i}" for i in range(5)]
        for rid in ids:
            worker.send(translate(rid, "Hello.", "World."))
        messages = worker.recv_until(done_or_failed(ids[-1]))

    expected: list[WorkerMessage] = []
    for rid in ids:
        expected += [
            Started(rid),
            SegmentResult(rid, f"{rid}-0", text="[en>zh-Hans]Hello."),
            SegmentResult(rid, f"{rid}-1", text="[en>zh-Hans]World."),
            RequestDone(rid),
        ]
    assert messages == expected


def test_segment_sentences_are_split_and_joined() -> None:
    with WorkerProcess() as worker:
        worker.handshake()
        worker.send(translate("r", "Dr. Smith left. He was tired!\nNew line.", "你好。世界！", source="en"))
        messages = worker.recv_until(done_or_failed("r"))
    results = [m for m in messages if isinstance(m, SegmentResult)]
    assert results[0].text == "[en>zh-Hans]Dr. Smith left.[en>zh-Hans]He was tired!\n[en>zh-Hans]New line."
    assert results[1].text == "[en>zh-Hans]你好。[en>zh-Hans]世界！"


# ---------------------------------------------------------------- 取消


def test_cancel_queued_request_replies_cancelled_and_never_starts() -> None:
    with WorkerProcess() as worker:
        worker.handshake({"segment_delay": 0.4})
        worker.send(translate("slow", "a"))
        worker.send(translate("queued", "b"))
        worker.send(translate("after", "c"))
        assert worker.recv() == Started("slow")
        worker.send(Cancel("queued"))
        messages = worker.recv_until(done_or_failed("after"))

    queued = for_request(messages, "queued")
    assert len(queued) == 1
    assert isinstance(queued[0], RequestFailed) and queued[0].code == CANCELLED
    assert Started("queued") not in messages
    assert RequestDone("slow") in messages and RequestDone("after") in messages


def test_cancel_running_request_still_completes() -> None:
    with WorkerProcess() as worker:
        worker.handshake({"segment_delay": 0.2})
        worker.send(translate("r", "a", "b"))
        assert worker.recv() == Started("r")
        worker.send(Cancel("r"))
        messages = worker.recv_until(done_or_failed("r"))
    assert [type(m) for m in messages] == [SegmentResult, SegmentResult, RequestDone]


def test_cancel_unknown_request_is_ignored() -> None:
    with WorkerProcess() as worker:
        worker.handshake()
        worker.send(Cancel("no-such-request"))
        worker.send(translate("r", "a"))
        messages = worker.recv_until(done_or_failed("r"))
    assert messages == [Started("r"), SegmentResult("r", "r-0", text="[en>zh-Hans]a"), RequestDone("r")]


# ---------------------------------------------------------------- 重新配置


def test_configure_while_running_reloads_in_queue_order() -> None:
    with WorkerProcess() as worker:
        first = worker.handshake({"tag": "one", "segment_delay": 0.2})
        worker.send(translate("before", "a"))
        worker.send(Configure({"tag": "two", "pairs": [["ja", "en"]]}))
        worker.send(translate("after", "b", source="ja", target="en"))
        messages = worker.recv_until(done_or_failed("after"))

    kinds = [type(m) for m in messages]
    assert kinds == [Started, SegmentResult, RequestDone, Ready, Started, SegmentResult, RequestDone]
    second = messages[3]
    assert isinstance(second, Ready)
    assert second.capabilities["pairs"] == [["ja", "en"]]
    assert second.capabilities["loads"] == 2
    assert second.capabilities["fingerprint"] != first.capabilities["fingerprint"]
    assert messages[5] == SegmentResult("after", "after-0", text="[ja>en]b")


def test_failed_reload_exits_with_config_error() -> None:
    with WorkerProcess() as worker:
        worker.handshake()
        worker.send(Configure({"load_error": "config"}))
        assert worker.wait_exit() == ExitCode.CONFIG_ERROR


# ---------------------------------------------------------------- 退出


def test_shutdown_cancels_queue_finishes_current_and_exits_zero() -> None:
    with WorkerProcess() as worker:
        worker.handshake({"segment_delay": 0.3})
        for rid in ("current", "q1", "q2"):
            worker.send(translate(rid, "a"))
        assert worker.recv() == Started("current")
        worker.send(Shutdown())
        seen: list[WorkerMessage] = []

        def complete(message: WorkerMessage) -> bool:
            seen.append(message)
            return RequestDone("current") in seen and sum(isinstance(m, RequestFailed) for m in seen) == 2

        messages = worker.recv_until(complete)
        assert worker.wait_exit() == 0

    failed = {m.request_id: m.code for m in messages if isinstance(m, RequestFailed)}
    assert failed == {"q1": CANCELLED, "q2": CANCELLED}
    assert RequestDone("current") in messages
    assert Started("q1") not in messages and Started("q2") not in messages


def test_shutdown_when_idle_exits_zero() -> None:
    with WorkerProcess() as worker:
        worker.handshake()
        worker.send(Shutdown())
        assert worker.wait_exit() == 0


def test_closing_connection_makes_worker_exit() -> None:
    with WorkerProcess() as worker:
        worker.handshake()
        assert worker.conn is not None
        started = time.monotonic()
        worker.close_connection()
        worker.wait_exit(timeout=5)
        assert time.monotonic() - started < 5


def test_closing_connection_exits_even_while_inference_hangs() -> None:
    with WorkerProcess() as worker:
        worker.handshake({"hang_on": "HANG"})
        worker.send(translate("r", "HANG"))
        assert worker.recv() == Started("r")
        worker.close_connection()
        worker.wait_exit(timeout=5)


def test_hanging_request_sends_started_then_nothing() -> None:
    """卡死时 Started 已经发出（监管器从这里开始计时），之后没有任何消息，只能由主进程结束它。"""
    with WorkerProcess() as worker:
        worker.handshake({"hang_on": "HANG"})
        worker.send(translate("r", "HANG"))
        assert worker.recv() == Started("r")
        worker.send(Cancel("r"))
        with pytest.raises(TimeoutError):
            worker.recv(timeout=0.5)
        assert worker.proc is not None and worker.proc.poll() is None
        worker.close()
        assert worker.proc.poll() is not None


# ---------------------------------------------------------------- 崩溃


def test_os_exit_during_request_is_a_nonzero_exit() -> None:
    with WorkerProcess() as worker:
        worker.handshake({"exit_on": "BOOM"})
        worker.send(translate("r", "BOOM"))
        assert worker.recv() == Started("r")
        with pytest.raises(WorkerExited):
            worker.recv_until(lambda m: False)
        assert worker.wait_exit() == 3


def test_segfault_exits_nonzero_with_faulthandler_traceback() -> None:
    with WorkerProcess() as worker:
        worker.handshake({"segfault_on": "SEGV"})
        worker.send(translate("r", "fine", "SEGV"))
        with pytest.raises(WorkerExited):
            worker.recv_until(lambda m: False)
        code = worker.wait_exit()
    assert code == ACCESS_VIOLATION  # STATUS_ACCESS_VIOLATION，原生崩溃的退出码
    assert "Windows fatal exception: access violation" in worker.stderr
    assert "fake_backend.py" in worker.stderr  # faulthandler 打印了推理线程的 Python 堆栈


# ---------------------------------------------------------------- 段级错误与协议不变式


def test_segment_error_does_not_affect_other_segments() -> None:
    with WorkerProcess() as worker:
        worker.handshake({"fail_on": "FAIL"})
        worker.send(translate("r", "ok one", "FAIL here", "ok two"))
        messages = worker.recv_until(done_or_failed("r"))
    assert messages[0] == Started("r")
    assert messages[1] == SegmentResult("r", "r-0", text="[en>zh-Hans]ok one")
    failed = messages[2]
    assert isinstance(failed, SegmentResult) and failed.code == ErrorCode.INTERNAL and failed.text is None
    assert failed.error is not None and "RuntimeError" in failed.error
    assert messages[3] == SegmentResult("r", "r-2", text="[en>zh-Hans]ok two")
    assert messages[4] == RequestDone("r")


def test_missing_model_fails_every_segment_with_model_missing() -> None:
    with WorkerProcess() as worker:
        worker.handshake()
        worker.send(translate("r", "a", "b", source="ja", target="de"))
        messages = worker.recv_until(done_or_failed("r"))
    results = [m for m in messages if isinstance(m, SegmentResult)]
    assert [r.code for r in results] == [ErrorCode.MODEL_MISSING, ErrorCode.MODEL_MISSING]
    assert messages[-1] == RequestDone("r")


def test_pivot_translates_in_two_hops_and_sends_only_final_text() -> None:
    settings: dict[str, JsonValue] = {"pairs": [["ja", "en"], ["en", "de"]], "pivot": "en"}
    with WorkerProcess() as worker:
        worker.handshake(settings)
        worker.send(translate("r", "こんにちは。元気？", source="ja", target="de"))
        messages = worker.recv_until(done_or_failed("r"))
    assert messages == [
        Started("r"),
        SegmentResult("r", "r-0", text="[en>de][ja>en]こんにちは。 [en>de][ja>en]元気？"),
        RequestDone("r"),
    ]


def test_handler_fills_in_segments_the_backend_skipped() -> None:
    with WorkerProcess("tests.workers.fake_backend:SkippingBackend") as worker:
        worker.handshake()
        worker.send(translate("r", "a", "b"))
        messages = worker.recv_until(done_or_failed("r"))
    assert messages[1] == SegmentResult("r", "r-0", text="[en>zh-Hans]a")
    missing = messages[2]
    assert isinstance(missing, SegmentResult) and missing.segment_id == "r-1"
    assert missing.code == ErrorCode.INTERNAL
    assert messages[3] == RequestDone("r")


def test_handler_fails_remaining_segments_when_backend_raises() -> None:
    with WorkerProcess("tests.workers.fake_backend:RaisingBackend") as worker:
        worker.handshake()
        worker.send(translate("r", "a", "b", "c"))
        messages = worker.recv_until(done_or_failed("r"))
    results = [m for m in messages if isinstance(m, SegmentResult)]
    assert [r.segment_id for r in results] == ["r-0", "r-1", "r-2"]
    assert results[0].text == "[en>zh-Hans]a"
    assert [r.code for r in results[1:]] == [ErrorCode.INTERNAL, ErrorCode.INTERNAL]
    assert messages[-1] == RequestDone("r")


def test_ocr_request_is_rejected_by_nmt_worker() -> None:
    with WorkerProcess() as worker:
        worker.handshake()
        worker.send(OcrRequest("o", "s", 1, 1, 4), b"\x00" * 4)
        reply = worker.recv()
        worker.send(translate("r", "a"))
        after = worker.recv_until(done_or_failed("r"))
    assert (
        isinstance(reply, RequestFailed) and reply.request_id == "o" and reply.code == ErrorCode.BAD_REQUEST
    )
    assert after[-1] == RequestDone("r")


def test_harness_close_kills_running_child() -> None:
    worker = WorkerProcess().start()
    try:
        worker.handshake()
    finally:
        worker.close()
    assert worker.proc is not None and worker.proc.poll() is not None
