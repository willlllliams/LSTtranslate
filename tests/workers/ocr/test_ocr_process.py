"""OCR 进程的进程级测试（主环境，假后端 tests/workers/ocr/fake_backend.py，不需要 Paddle）。

覆盖：握手与能力清单、加载失败的退出码、像素帧解包（含行尾填充）、
坏的像素帧与 options → engine.bad_request、缺模型 → engine.model_missing、推理异常 → engine.internal、
网络拦截、本进程不处理翻译请求、Shutdown 后正常退出。
"""

import pytest

from lst.contracts.errors import ErrorCode
from lst.contracts.worker_protocol import (
    PROTOCOL_VERSION,
    Configure,
    ExitCode,
    Hello,
    JsonValue,
    OcrRequest,
    OcrResult,
    RequestFailed,
    Shutdown,
    TranslateRequest,
    TranslateSegment,
)
from tests.workers.ocr.harness import OcrWorkerProcess, leftover_workers, pad_rows
from tests.workers.ocr.validation import validate_blocks


def solid_frame(width: int, height: int, padding: int = 0) -> tuple[bytes, int]:
    """左上像素 BGRA=(1,2,3,255)，右下像素 (7,8,9,255)，其余 (100,100,100,255)；行尾补 padding 字节 0xEE。"""
    pixels = bytearray(b"\x64\x64\x64\xff" * (width * height))
    pixels[0:4] = b"\x01\x02\x03\xff"
    pixels[-4:] = b"\x07\x08\x09\xff"
    frame, stride = pad_rows(bytes(pixels), width, height, padding)
    padded = bytearray(frame)
    for y in range(height):
        padded[y * stride + width * 4 : (y + 1) * stride] = b"\xee" * padding
    return bytes(padded), stride


def request(rid: str, width: int, height: int, stride: int, **options: JsonValue) -> OcrRequest:
    return OcrRequest(rid, "s1", width, height, stride, dict(options))


def test_ready_and_pixel_unpacking_with_padding() -> None:
    with OcrWorkerProcess() as worker:
        ready = worker.handshake({"mode": "structure"})
        assert ready.protocol_version == PROTOCOL_VERSION
        assert ready.capabilities == {"mode": "structure", "fake": True}
        assert [p.kind for p in worker.progress] == ["model_load"]
        frame, stride = solid_frame(33, 17, padding=12)
        reply = worker.ocr(request("r1", 33, 17, stride), frame)
    assert isinstance(reply, OcrResult)
    blocks = validate_blocks(reply.blocks, 33, 17)
    assert blocks[0]["text"] == "33x17 1/2/3 7/8/9 contiguous=True"


@pytest.mark.parametrize(
    ("width", "height", "stride", "length", "message"),
    [
        (10, 10, 40, 399, "帧长"),
        (10, 10, 40, 4000, "帧长"),
        (10, 10, 36, 360, "stride"),
        (0, 10, 40, 400, "宽高"),
    ],
)
def test_bad_pixel_frame_is_bad_request(
    width: int, height: int, stride: int, length: int, message: str
) -> None:
    with OcrWorkerProcess() as worker:
        worker.handshake()
        reply = worker.ocr(request("bad", width, height, stride), b"\0" * length)
        assert isinstance(reply, RequestFailed)
        assert reply.code == ErrorCode.BAD_REQUEST
        assert message in reply.error
        # 进程照常工作：下一个请求正常完成。
        frame, stride = solid_frame(4, 4)
        assert isinstance(worker.ocr(request("ok", 4, 4, stride), frame), OcrResult)


@pytest.mark.parametrize("options", [{"table": "yes"}, {"resize_max_side": -1}, {"grayscale": 1}])
def test_bad_options_are_bad_request(options: dict[str, JsonValue]) -> None:
    with OcrWorkerProcess() as worker:
        worker.handshake()
        frame, stride = solid_frame(4, 4)
        reply = worker.ocr(request("bad", 4, 4, stride, **options), frame)
    assert isinstance(reply, RequestFailed)
    assert reply.code == ErrorCode.BAD_REQUEST


def test_inference_exception_is_internal_and_process_survives() -> None:
    with OcrWorkerProcess() as worker:
        worker.handshake()
        frame, stride = solid_frame(4, 4)
        reply = worker.ocr(request("boom", 4, 4, stride, **{"raise": True}), frame)
        assert isinstance(reply, RequestFailed)
        assert reply.code == ErrorCode.INTERNAL
        assert "RuntimeError" in reply.error
        assert isinstance(worker.ocr(request("after", 4, 4, stride), frame), OcrResult)
    assert "Traceback" in worker.stderr


def test_missing_model_is_model_missing() -> None:
    with OcrWorkerProcess() as worker:
        worker.handshake()
        frame, stride = solid_frame(4, 4)
        reply = worker.ocr(request("missing", 4, 4, stride, missing=True), frame)
        assert isinstance(reply, RequestFailed)
        assert reply.code == ErrorCode.MODEL_MISSING
        assert "PP-FormulaNet_plus-L" in reply.error
        assert isinstance(worker.ocr(request("after", 4, 4, stride), frame), OcrResult)


def test_network_access_is_blocked() -> None:
    with OcrWorkerProcess() as worker:
        worker.handshake()
        frame, stride = solid_frame(4, 4)
        reply = worker.ocr(request("net", 4, 4, stride, network=True), frame)
    assert isinstance(reply, RequestFailed)
    assert reply.code == ErrorCode.INTERNAL
    assert "PermissionError" in reply.error
    assert "已拦截网络访问" in worker.stderr and "example.com" in worker.stderr


def test_translate_request_is_rejected() -> None:
    with OcrWorkerProcess() as worker:
        worker.handshake()
        worker.send(TranslateRequest("t1", "s1", "en", "zh-Hans", (TranslateSegment("0", "hi"),)))
        reply = worker.recv(10)
    assert isinstance(reply, RequestFailed)
    assert reply.code == ErrorCode.BAD_REQUEST


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
    with OcrWorkerProcess() as worker:
        worker.send(Hello(PROTOCOL_VERSION))
        worker.send(Configure({"load_error": load_error}))
        assert worker.wait_exit() == code


def test_invalid_settings_exit_with_config_error() -> None:
    with OcrWorkerProcess() as worker:
        worker.send(Hello(PROTOCOL_VERSION))
        worker.send(Configure({"mode": "layout"}))
        assert worker.wait_exit() == ExitCode.CONFIG_ERROR
    assert "mode" in worker.stderr


def test_unimportable_backend_exits_with_dependency_error() -> None:
    with OcrWorkerProcess("tests.workers.ocr.no_such_module:Backend") as worker:
        assert worker.wait_exit() == ExitCode.DEPENDENCY_ERROR


def test_shutdown_exits_cleanly_and_leaves_no_process() -> None:
    with OcrWorkerProcess() as worker:
        worker.handshake()
        worker.send(Shutdown())
        assert worker.wait_exit() == ExitCode.OK
    assert leftover_workers(worker.address) == []
