r"""RapidOCR 的真实测试：OCR 子进程用 .venv-ocr 的解释器运行 RapidOCR 后端，
加载 rapidocr 自带的 PP-OCRv6 模型。

- 在内存里用 PySide6 画中文、英文和中英混排的行（screens.py），经子进程识别，检查关键字与行的包围盒
  落在画字的位置附近，缩小后识别的坐标换算回原图；
- 1920×1080 的文档截图（render_document.py 用 .venv-ocr 的 Pillow 渲染）测识别耗时，写 INFO 日志；
- 不联网：子进程装着网络拦截，stderr 里不应出现“已拦截网络访问”；rapidocr 的模型目录在测试前后不变；
- 导入链不含 paddle、paddlex；
- 经安装版的启动器（launcher\worker.py --ocr-site <运行环境>）以隔离模式启动，与安装版的 RapidOCR 相同。

前提：.venv-ocr 里装了 rapidocr 与 onnxruntime，系统有 msyh.ttc；缺任何一项整个模块跳过。
模型目录只读使用。测试不读写用户目录。
"""

import logging
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from lst.contracts.worker_protocol import (
    PROTOCOL_VERSION,
    Configure,
    ExitCode,
    Hello,
    JsonValue,
    OcrRequest,
    OcrResult,
    Shutdown,
)
from lst.services.ocr.blocks import decode_blocks
from tests.workers.ocr.documents import FONT, FULL_HD_ENGLISH, full_hd_document
from tests.workers.ocr.harness import (
    OCR_PYTHON,
    ROOT,
    OcrWorkerProcess,
    has_ocr_env,
    leftover_workers,
    render_document,
)
from tests.workers.ocr.screens import FONT_FILE, Screenshot, assert_near, ensure_font, render
from tests.workers.ocr.validation import validate_blocks

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

RAPID_BACKEND = "lst.workers.ocr_worker.rapid_backend:RapidBackend"
OCR_SITE = ROOT / ".venv-ocr" / "Lib" / "site-packages"
RAPID_MODELS = OCR_SITE / "rapidocr" / "models"
SETTINGS: dict[str, JsonValue] = {"mode": "text", "engine": "rapid", "language": "auto", "device": "auto"}
LOAD_TIMEOUT = 120.0
TOLERANCE = 14
"""行包围盒与画字范围的允许误差（像素）。检测框按 unclip_ratio 向外扩了一圈，比字形的紧包围盒略大。"""


def _has_rapidocr() -> bool:
    return any(OCR_SITE.glob("rapidocr-*.dist-info")) and any(OCR_SITE.glob("onnxruntime-*.dist-info"))


pytestmark = [
    pytest.mark.skipif(not has_ocr_env(), reason="本机没有 OCR 环境 .venv-ocr"),
    pytest.mark.skipif(
        not has_ocr_env() or not _has_rapidocr(), reason=".venv-ocr 里没有 rapidocr 或 onnxruntime"
    ),
    pytest.mark.skipif(not FONT.is_file(), reason="本机没有 msyh.ttc，无法渲染中文测试图"),
    pytest.mark.timeout(120, func_only=True),
]

log = logging.getLogger(__name__)


def model_listing() -> list[tuple[str, int]]:
    return sorted((path.name, path.stat().st_size) for path in RAPID_MODELS.iterdir())


@pytest.fixture(scope="module")
def worker() -> Iterator[OcrWorkerProcess]:
    """模块内共用一个 RapidOCR 子进程，加载只做一次。"""
    before = model_listing()
    with OcrWorkerProcess(RAPID_BACKEND, python=OCR_PYTHON) as process:
        started = time.perf_counter()
        ready = process.handshake(SETTINGS, timeout=LOAD_TIMEOUT)
        log.info("RapidOCR 子进程启动到就绪 %.1f 秒", time.perf_counter() - started)
        assert ready.capabilities["engine"] == "rapid"
        assert ready.capabilities["mode"] == "text"
        assert ready.capabilities["features"] == ["text"]
        yield process
        process.send(Shutdown())
        assert process.wait_exit() == 0
        assert "已拦截网络访问" not in process.stderr, process.stderr
    assert leftover_workers(process.address) == []
    assert model_listing() == before, "rapidocr 的模型目录不应有任何变化"


@pytest.fixture
def app(qapp: QApplication) -> QApplication:
    if not ensure_font():
        pytest.skip(f"没有字体文件 {FONT_FILE}")
    return qapp


def recognize(
    worker: OcrWorkerProcess, shot: Screenshot, options: dict[str, JsonValue] | None = None
) -> list[JsonValue]:
    request = OcrRequest("r", "s", shot.width, shot.height, shot.stride, {"mode": "text", **(options or {})})
    reply = worker.ocr(request, shot.data, timeout=60)
    assert isinstance(reply, OcrResult), worker.stderr
    return reply.blocks


def line_boxes(blocks: list[JsonValue], shot: Screenshot) -> list[tuple[str, tuple[int, int, int, int]]]:
    validate_blocks(blocks, shot.width, shot.height)
    (decoded,) = decode_blocks(blocks, width=shot.width, height=shot.height)
    return [(line.text, (line.bbox.x0, line.bbox.y0, line.bbox.x1, line.bbox.y1)) for line in decoded.lines]


# ---------------------------------------------------------------- 识别内容与位置


def test_ready_reports_languages(worker: OcrWorkerProcess) -> None:
    del worker  # 握手的检查在夹具里；这里确认进程没有在识别前退出
    from lst.services.ocr_engines.catalog import V6_LANGUAGES as RAPID_LANGUAGES

    with OcrWorkerProcess(RAPID_BACKEND, python=OCR_PYTHON) as process:
        ready = process.handshake({**SETTINGS, "language": "de", "warmup": False}, timeout=LOAD_TIMEOUT)
        assert ready.capabilities["language"] == "de"
        assert ready.capabilities["languages"] == list(RAPID_LANGUAGES)
        assert ready.versions["rapidocr"] == "3.10.0"
        process.send(Shutdown())
        assert process.wait_exit() == 0


def test_chinese_english_and_mixed_lines_in_place(worker: OcrWorkerProcess, app: QApplication) -> None:
    del app
    shot = render(
        [("屏幕翻译测试文字", 60, 80), ("Hello RapidOCR 2026", 60, 180), ("中英混排 mixed text", 60, 280)],
        800,
        340,
    )
    blocks = recognize(worker, shot)
    (block,) = validate_blocks(blocks, shot.width, shot.height)
    assert block["bbox"] == [0, 0, shot.width, shot.height]
    lines = line_boxes(blocks, shot)
    assert [text for text, _ in lines] == ["屏幕翻译测试文字", "Hello RapidOCR 2026", "中英混排 mixed text"]
    for (_, box), drawn in zip(lines, shot.lines, strict=True):
        assert_near(box, drawn.box, TOLERANCE)


def test_downscaled_recognition_maps_back_to_original(worker: OcrWorkerProcess, app: QApplication) -> None:
    del app
    shot = render([("Screen translation 2026", 80, 120), ("截屏翻译", 80, 260)], 1600, 320, 48)
    lines = line_boxes(recognize(worker, shot, {"resize_max_side": 800, "grayscale": True}), shot)
    assert [text for text, _ in lines] == ["Screen translation 2026", "截屏翻译"]
    for (_, box), drawn in zip(lines, shot.lines, strict=True):
        assert_near(box, drawn.box, TOLERANCE * 2)  # 缩小一半识别，误差随之放大


def test_blank_screenshot_gives_no_blocks(worker: OcrWorkerProcess, app: QApplication) -> None:
    del app
    assert recognize(worker, render([], 320, 200)) == []


def test_full_hd_document_timing(worker: OcrWorkerProcess) -> None:
    """需求 6.1 节的测量条件：1920×1080 的文档截图。耗时写 INFO 日志，只要求在文本模式的处理时限之内。"""
    width, height = 1920, 1080
    pixels = render_document(full_hd_document())
    request = OcrRequest("hd", "s", width, height, width * 4, {"mode": "text"})
    durations: list[float] = []
    blocks: list[JsonValue] = []
    for _ in range(3):
        started = time.perf_counter()
        reply = worker.ocr(request, pixels, timeout=60)
        durations.append(time.perf_counter() - started)
        assert isinstance(reply, OcrResult), worker.stderr
        blocks = reply.blocks
    validate_blocks(blocks, width, height)
    (decoded,) = decode_blocks(blocks, width=width, height=height)
    text = "\n".join(line.text for line in decoded.lines)
    assert "Screen translation recognizes" in text
    assert "截屏翻译" in text
    english = [line for line in FULL_HD_ENGLISH if line in text]
    message = (
        f"RapidOCR 1920×1080 文档：{len(decoded.lines)} 行，"
        f"英文 {len(english)}/{len(FULL_HD_ENGLISH)} 行完全一致，"
        f"耗时 {', '.join(f'{d:.2f}' for d in durations)} 秒"
        f"（中位数 {statistics.median(durations):.2f} 秒）"
    )
    log.info(message)
    assert statistics.median(durations) < 30


# ---------------------------------------------------------------- 配置错误、导入链、启动器


def test_unsupported_language_is_config_error() -> None:
    with OcrWorkerProcess(RAPID_BACKEND, python=OCR_PYTHON) as process:
        process.send(Hello(PROTOCOL_VERSION))
        process.send(Configure({**SETTINGS, "language": "ko"}))
        code = process.wait_exit(timeout=LOAD_TIMEOUT)
    assert code == ExitCode.CONFIG_ERROR
    assert "ko" in process.stderr


def test_import_chain_has_no_paddle() -> None:
    """模拟进程入口加载 RapidOCR 后端并预热一次，检查 paddle、paddlex、paddleocr 都没有被导入。"""
    code = (
        "import sys\n"
        "import lst.workers.ocr_worker.__main__\n"
        "from lst.workers.ocr_worker import network_guard\n"
        "network_guard.install()\n"
        "from lst.workers._runtime import load_backend\n"
        f"backend = load_backend({RAPID_BACKEND!r})\n"
        "backend.load({'mode': 'text', 'engine': 'rapid'}, lambda *args, **kwargs: None)\n"
        "banned = ('paddle', 'paddlex', 'paddleocr', 'torch')\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in banned)\n"
        "print('banned:' + ','.join(bad))\n"
        "names = ('rapidocr', 'onnxruntime', 'cv2')\n"
        "print('loaded:' + ','.join(sorted(m for m in names if m in sys.modules)))\n"
    )
    result = subprocess.run(
        [str(OCR_PYTHON), "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "banned:\n" in result.stdout
    assert "loaded:cv2,onnxruntime,rapidocr" in result.stdout
    assert "已拦截网络访问" not in result.stderr


def test_through_installed_launcher(tmp_path: Path, app: QApplication) -> None:
    r"""按安装布局经 launcher\worker.py 启动：app\lst 是仓库 lst 的副本，--ocr-site 指向装有 rapidocr 的目录。

    解释器用主环境的基础解释器（uv 管理的 CPython 3.13，site-packages 里只有 pip），与安装版的
    runtime\python.exe 相同；-I 隔离模式，模块只从 app\ 与 --ocr-site 目录导入。
    """
    del app
    install = tmp_path / "LSTtranslate"
    shutil.copytree(ROOT / "packaging" / "launcher", install / "launcher")
    shutil.copytree(ROOT / "lst", install / "app" / "lst", ignore=shutil.ignore_patterns("__pycache__"))
    python = Path(getattr(sys, "_base_executable", sys.executable))
    command = [str(python), "-I", "-X", "utf8", str(install / "launcher" / "worker.py"), "ocr",
               "--ocr-site", str(OCR_SITE)]  # fmt: skip
    shot = render([("Launcher RapidOCR", 40, 70)], 600, 110)
    with OcrWorkerProcess(RAPID_BACKEND, command=command) as process:
        ready = process.handshake({**SETTINGS, "warmup": False}, timeout=LOAD_TIMEOUT)
        assert ready.capabilities["engine"] == "rapid"
        lines = line_boxes(recognize(process, shot), shot)
        assert [text for text, _ in lines] == ["Launcher RapidOCR"]
        process.send(Shutdown())
        assert process.wait_exit() == 0
        assert "已拦截网络访问" not in process.stderr
