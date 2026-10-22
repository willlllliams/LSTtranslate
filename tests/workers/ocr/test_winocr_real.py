"""Windows 自带 OCR 的真实测试：调用系统的 Windows.Media.Ocr。

- 在内存里用 PySide6 画几行中文和英文（tests/workers/ocr/screens.py），转成 BGRA，直接调用后端识别，
  检查关键字与行的包围盒落在画字的位置附近；
- 起一个真实的 OCR 子进程（主环境解释器，--backend 指向 Windows OCR 后端），做一次握手与识别；
- 检查选 Windows OCR 后端时子进程的导入链不含 paddle、paddlex、cv2（这一项不需要 OCR 语言）。

系统没有任何 OCR 语言（或不是 Windows、缺 pywinrt）时，调用系统 OCR 的测试跳过。
测试不读写用户目录，也不访问网络。
"""

import logging
import os
import subprocess
import sys
import time

import pytest
from PySide6.QtWidgets import QApplication

from lst.contracts.worker_protocol import OcrRequest, OcrResult, Shutdown
from lst.services.ocr.blocks import decode_blocks
from lst.workers.ocr_worker.image import bgra_to_bgr
from lst.workers.ocr_worker.winocr_backend import WinOcrBackend, WinRtOcrApi, match_language
from tests.workers.ocr.harness import ROOT, OcrWorkerProcess, leftover_workers
from tests.workers.ocr.screens import FONT_FILE, assert_near, ensure_font, render
from tests.workers.ocr.validation import validate_blocks

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

WINOCR_BACKEND = "lst.workers.ocr_worker.winocr_backend:WinOcrBackend"


def _available_languages() -> list[str]:
    try:
        return WinRtOcrApi().available_languages()
    except (ImportError, OSError):
        return []


AVAILABLE = _available_languages()
needs_ocr = pytest.mark.skipif(not AVAILABLE, reason="系统没有安装 Windows OCR 语言")


def no_progress(kind: str, detail: str = "", done: int | None = None, total: int | None = None) -> None:
    del kind, detail, done, total


def load(language: str) -> WinOcrBackend:
    backend = WinOcrBackend()
    backend.load({"mode": "text", "engine": "windows", "language": language}, no_progress)
    return backend


@pytest.fixture
def app(qapp: QApplication) -> QApplication:
    if not ensure_font():
        pytest.skip(f"没有字体文件 {FONT_FILE}")
    return qapp


# ---------------------------------------------------------------- 直接调用后端


@needs_ocr
@pytest.mark.skipif(match_language("zh-Hans", AVAILABLE) is None, reason="系统没有简体中文 OCR")
def test_chinese_lines_are_recognised_in_place(app: QApplication) -> None:
    del app
    shot = render([("屏幕翻译测试文字", 60, 80), ("今天天气很好", 60, 180)], 640, 240)
    backend = load("zh-Hans")
    blocks = backend.recognize(bgra_to_bgr(shot.data, shot.width, shot.height, shot.stride), {})
    (block,) = validate_blocks(blocks, shot.width, shot.height)
    assert block["bbox"] == [0, 0, shot.width, shot.height]
    (decoded,) = decode_blocks(blocks, width=shot.width, height=shot.height)
    assert [line.text for line in decoded.lines] == ["屏幕翻译测试文字", "今天天气很好"]
    for line, drawn in zip(decoded.lines, shot.lines, strict=True):
        assert_near((line.bbox.x0, line.bbox.y0, line.bbox.x1, line.bbox.y1), drawn.box, 8)
        assert line.score == 1.0


@needs_ocr
@pytest.mark.skipif(match_language("en", AVAILABLE) is None, reason="系统没有英文 OCR")
def test_english_lines_with_padding_and_downscaling(app: QApplication) -> None:
    del app
    shot = render([("Hello Windows OCR", 40, 70), ("Screen translation 2026", 40, 160)], 900, 220, 36)
    backend = load("en")
    image = bgra_to_bgr(shot.data, shot.width, shot.height, shot.stride)
    blocks = backend.recognize(image, {"resize_max_side": 600})  # 缩小到 600×147 识别，坐标换算回原图
    (decoded,) = decode_blocks(blocks, width=shot.width, height=shot.height)
    assert [line.text for line in decoded.lines] == ["Hello Windows OCR", "Screen translation 2026"]
    for line, drawn in zip(decoded.lines, shot.lines, strict=True):
        assert_near((line.bbox.x0, line.bbox.y0, line.bbox.x1, line.bbox.y1), drawn.box, 10)


@needs_ocr
def test_blank_screenshot_gives_no_blocks(app: QApplication) -> None:
    del app
    shot = render([], 320, 200)
    backend = load("auto")
    assert backend.recognize(bgra_to_bgr(shot.data, shot.width, shot.height, shot.stride), {}) == []


@needs_ocr
def test_full_hd_screenshot_timing(app: QApplication) -> None:
    """1920×1080、30 行文字的截图，识别耗时写 INFO 日志供参考，只要求在文本模式的处理时限之内。"""
    del app
    rows = [(f"Line {i:02d} the quick brown fox jumps over the lazy dog 屏幕翻译", 40, 40 + i * 34)
            for i in range(30)]  # fmt: skip
    shot = render(rows, 1920, 1080, 24)
    backend = load("auto")
    image = bgra_to_bgr(shot.data, shot.width, shot.height, shot.stride)
    backend.recognize(image, {})
    started = time.perf_counter()
    blocks = backend.recognize(image, {})
    elapsed = time.perf_counter() - started
    validate_blocks(blocks, shot.width, shot.height)
    (decoded,) = decode_blocks(blocks, width=shot.width, height=shot.height)
    assert len(decoded.lines) >= 25
    logging.getLogger(__name__).info(
        "1920×1080、30 行：识别 %.0f 毫秒，%d 行", elapsed * 1000, len(decoded.lines)
    )
    assert elapsed < 30


# ---------------------------------------------------------------- 子进程


@needs_ocr
def test_worker_process_end_to_end(app: QApplication) -> None:
    del app
    shot = render([("Windows OCR engine", 30, 60)], 500, 100, 32)
    with OcrWorkerProcess(WINOCR_BACKEND) as worker:
        ready = worker.handshake({"mode": "text", "engine": "windows", "language": "auto", "device": "auto"})
        assert ready.capabilities["engine"] == "windows"
        assert ready.capabilities["mode"] == "text"
        assert ready.capabilities["features"] == ["text"]
        assert ready.capabilities["languages"]
        assert ready.versions["engine"] == "windows"
        request = OcrRequest("r1", "s1", shot.width, shot.height, shot.stride, {"mode": "text"})
        reply = worker.ocr(request, shot.data)
        assert isinstance(reply, OcrResult), worker.stderr
        validate_blocks(reply.blocks, shot.width, shot.height)
        (decoded,) = decode_blocks(reply.blocks, width=shot.width, height=shot.height)
        assert "OCR" in " ".join(line.text for line in decoded.lines)
        worker.send(Shutdown())
        assert worker.wait_exit() == 0
        assert "paddle" not in worker.stderr.lower()
    assert leftover_workers(worker.address) == []


def test_winocr_import_chain_has_no_paddle() -> None:
    """模拟进程入口加载 Windows OCR 后端，检查 paddle、paddlex、cv2 都没有被导入。

    load 时才导入 winrt；没有 OCR 语言时 load 抛 ConfigError，导入链照样完整走过一遍。
    """
    code = (
        "import sys\n"
        "import lst.workers.ocr_worker.__main__\n"
        "from lst.workers._runtime import load_backend\n"
        f"backend = load_backend({WINOCR_BACKEND!r})\n"
        "try:\n"
        "    backend.load({}, lambda *args, **kwargs: None)\n"
        "except Exception:\n"
        "    pass\n"
        "banned = ('paddle', 'paddlex', 'paddleocr', 'cv2')\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in banned)\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
