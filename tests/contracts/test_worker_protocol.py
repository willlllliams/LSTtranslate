"""主进程与推理进程通信协议的编解码测试。"""

import threading
from multiprocessing import Pipe

import pytest

from lst.contracts.worker_protocol import (
    PROTOCOL_VERSION,
    Cancel,
    Configure,
    ExitCode,
    Hello,
    OcrRequest,
    OcrResult,
    Progress,
    ProtocolError,
    Ready,
    RequestDone,
    RequestFailed,
    SegmentResult,
    Shutdown,
    Started,
    TranslateRequest,
    TranslateSegment,
    WorkerMessage,
    decode,
    encode,
    recv_message,
    send_message,
)

MESSAGES: list[WorkerMessage] = [
    Hello(PROTOCOL_VERSION),
    Configure({"device": "cpu", "models": ["a", "b"], "threads": 4, "nested": {"x": None}}),
    TranslateRequest(
        "r1", "s1", "en", "zh-Hans", (TranslateSegment("g1", "Hello"), TranslateSegment("g2", "中文“引号”"))
    ),
    OcrRequest("r2", "s1", 640, 480, 2560, {"table": True}),
    Cancel("r1"),
    Shutdown(),
    Ready(PROTOCOL_VERSION, {"pairs": [["en", "zh-Hans"]]}, {"ctranslate2": "4.8.3"}),
    Progress("model_load", "en→zh", 1, 2),
    Progress("download"),
    Started("r1"),
    SegmentResult("r1", "g1", text="你好"),
    SegmentResult("r1", "g2", error="模型缺失", code="engine.model_missing"),
    OcrResult("r2", [{"label": "text", "bbox": [0, 0, 10, 10]}]),
    RequestDone("r1"),
    RequestFailed("r2", "engine.timeout", "超时"),
]


@pytest.mark.parametrize("message", MESSAGES, ids=lambda m: type(m).__name__)
def test_roundtrip(message: WorkerMessage) -> None:
    assert decode(encode(message)) == message


def test_encoding_is_utf8_json_with_type_field() -> None:
    frame = encode(SegmentResult("r", "g", text="你好"))
    assert frame.startswith(b'{"type":"segment_result"')
    assert "你好".encode() in frame  # ensure_ascii=False，中文不转义


@pytest.mark.parametrize(
    "frame",
    [
        b"\xff\xfe",
        b"not json",
        b"[1, 2]",
        b'{"no_type": 1}',
        b'{"type": "unknown"}',
        b'{"type": "hello"}',
        b'{"type": "hello", "protocol_version": 1, "extra": 2}',
        b'{"type": "translate", "request_id": "r", "session_id": "s", "source": "en", "target": "zh",'
        b' "segments": "oops"}',
        b'{"type": "translate", "request_id": "r", "session_id": "s", "source": "en", "target": "zh",'
        b' "segments": [{"id": "g"}]}',
    ],
)
def test_malformed_frames_raise_protocol_error(frame: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode(frame)


def test_payload_frame_follows_ocr_request_over_real_pipe() -> None:
    left, right = Pipe()
    # 像素帧远大于 Windows 管道缓冲区，写入会阻塞到对端读走数据，所以在后台线程里接收。
    pixels = bytes(range(256)) * 4096
    request = OcrRequest("r", "s", 1024, 1024, 1024, {})
    received: list[tuple[WorkerMessage, bytes | None]] = []
    reader = threading.Thread(target=lambda: received.extend(recv_message(right) for _ in range(2)))
    reader.start()
    send_message(left, request, pixels)
    send_message(left, Cancel("r"))
    reader.join(timeout=10)
    assert received == [(request, pixels), (Cancel("r"), None)]


def test_payload_presence_is_enforced() -> None:
    left, _ = Pipe()
    with pytest.raises(ValueError):
        send_message(left, OcrRequest("r", "s", 1, 1, 4, {}))
    with pytest.raises(ValueError):
        send_message(left, Cancel("r"), b"x")


def test_recv_raises_eof_when_peer_closes() -> None:
    left, right = Pipe()
    left.close()
    with pytest.raises(EOFError):
        recv_message(right)


def test_deterministic_exit_codes() -> None:
    assert ExitCode.PROTOCOL_MISMATCH.deterministic
    assert ExitCode.DEPENDENCY_ERROR.deterministic
    assert ExitCode.CONFIG_ERROR.deterministic
    assert not ExitCode.OK.deterministic
