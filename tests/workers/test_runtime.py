"""推理进程运行时中不需要子进程的部分：任务队列。进程级行为见 test_nmt_process.py。"""

import threading

from lst.contracts.worker_protocol import TranslateRequest
from lst.workers._runtime import WorkQueue, _ConfigureJob, _RequestJob, _StopJob


def job(request_id: str) -> _RequestJob[TranslateRequest]:
    return _RequestJob(TranslateRequest(request_id, "s", "en", "zh-Hans", ()), None)


def test_queue_is_fifo_across_requests_and_configure() -> None:
    queue: WorkQueue[TranslateRequest] = WorkQueue()
    configure = _ConfigureJob({"a": 1})
    for item in (job("r1"), configure, job("r2")):
        queue.put(item)
    assert [queue.get(), queue.get(), queue.get()] == [job("r1"), configure, job("r2")]


def test_remove_takes_out_only_the_named_request() -> None:
    queue: WorkQueue[TranslateRequest] = WorkQueue()
    for rid in ("r1", "r2", "r3"):
        queue.put(job(rid))
    assert queue.remove("r2") == job("r2")
    assert queue.remove("r2") is None
    assert queue.remove("unknown") is None
    assert [queue.get(), queue.get()] == [job("r1"), job("r3")]


def test_close_returns_pending_requests_and_stop_ends_the_queue() -> None:
    queue: WorkQueue[TranslateRequest] = WorkQueue()
    queue.put(job("r1"))
    queue.put(_ConfigureJob({}))
    queue.put(job("r2"))
    assert queue.close() == [job("r1"), job("r2")]
    assert not queue.put(job("late")), "Shutdown 之后不再接收请求"
    queue.stop()
    assert isinstance(queue.get(), _StopJob)


def test_get_blocks_until_put() -> None:
    queue: WorkQueue[TranslateRequest] = WorkQueue()
    got: list[object] = []
    consumer = threading.Thread(target=lambda: got.append(queue.get()))
    consumer.start()
    consumer.join(0.1)
    assert consumer.is_alive()
    queue.put(job("r1"))
    consumer.join(5)
    assert got == [job("r1")]
