"""用 OCR 环境（.venv-ocr）的真实 PaddleOCR 后端运行 OCR 子进程。

前提：.venv-ocr、本机 PaddleX 模型缓存（assets/PaddleOCR_and_PP-Structure）与
C:\\Windows\\Fonts\\msyh.ttc 都在，缺任何一项整个模块跳过。
模型缓存只读使用，PaddleX 只在其中写锁文件等运行文件。

测试图片由 OCR 环境里的 Pillow 渲染（render_document.py），内容见 documents.py：
- 小文档（1280×900）：标题、中文段落、英文段落、两行三列带边框的表格；
- 1920×1080 文档：需求 6.1 节的测量条件，用于性能测试。

版面模式与文本模式各用一个 module 级的子进程，多个测试共用，加载只做一次。加载阶段（导入 Paddle、创建产线、
预热）由 handshake 的超时约束，不计入测试函数的 60 秒上限（timeout 的 func_only）；性能测试和需要补加载模型的
测试放宽到 300 秒。

是否发生下载用三种方法确认（test_no_model_download）：
1. 子进程装了网络拦截（network_guard），任何解析域名、建立连接的尝试都会记“已拦截网络访问”；
2. PaddleX 只有在需要下载时才会检查模型站点的连通性并记 “Connectivity check ...” 警告；
3. 模型缓存的 official_models 目录在测试前后的子目录与文件清单不变。
"""

import statistics
import time
from collections.abc import Iterator
from typing import cast

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
    Ready,
    RequestFailed,
)
from tests.workers.ocr.documents import (
    CHINESE_LINES,
    ENGLISH_LINES,
    FONT,
    FULL_HD_TABLE,
    SMALL_TABLE,
    TITLE,
    full_hd_document,
    small_document,
)
from tests.workers.ocr.harness import (
    MODEL_CACHE,
    OCR_PYTHON,
    OFFICIAL_MODELS,
    PADDLE_BACKEND,
    OcrWorkerProcess,
    has_ocr_env,
    leftover_workers,
    pad_rows,
    render_document,
)
from tests.workers.ocr.validation import validate_blocks

pytestmark = [
    pytest.mark.skipif(not has_ocr_env(), reason="本机没有 OCR 环境 .venv-ocr"),
    pytest.mark.skipif(not OFFICIAL_MODELS.is_dir(), reason="本机没有 PaddleX 模型缓存"),
    pytest.mark.skipif(not FONT.is_file(), reason="本机没有 msyh.ttc，无法渲染中文测试图"),
    pytest.mark.timeout(60, func_only=True),
]

SETTINGS: dict[str, JsonValue] = {"model_cache_dir": str(MODEL_CACHE)}
SMALL_W, SMALL_H = 1280, 900
HD_W, HD_H = 1920, 1080
LOAD_TIMEOUT = 300.0
# 识别允许少量误差：表格单元格的包围盒与画出的格子相差不超过这么多像素。
CELL_TOLERANCE = 10

Blocks = list[dict[str, object]]


def model_listing() -> list[str]:
    return sorted(str(path.relative_to(OFFICIAL_MODELS)) for path in OFFICIAL_MODELS.rglob("*"))


@pytest.fixture(scope="module")
def models_before() -> list[str]:
    return model_listing()


def start_worker(mode: str) -> tuple[OcrWorkerProcess, Ready]:
    worker = OcrWorkerProcess(PADDLE_BACKEND, python=OCR_PYTHON)
    worker.start(connect_timeout=120)
    try:
        ready = worker.handshake({**SETTINGS, "mode": mode}, timeout=LOAD_TIMEOUT)
    except BaseException:
        worker.close()
        raise
    return worker, ready


@pytest.fixture(scope="module")
def structure_worker(models_before: list[str]) -> Iterator[tuple[OcrWorkerProcess, Ready]]:
    worker, ready = start_worker("structure")
    try:
        yield worker, ready
    finally:
        worker.close()
        assert leftover_workers(worker.address) == []


@pytest.fixture(scope="module")
def text_worker(models_before: list[str]) -> Iterator[tuple[OcrWorkerProcess, Ready]]:
    worker, ready = start_worker("text")
    try:
        yield worker, ready
    finally:
        worker.close()
        assert leftover_workers(worker.address) == []


@pytest.fixture(scope="module")
def small_pixels() -> bytes:
    return render_document(small_document())


@pytest.fixture(scope="module")
def hd_pixels() -> bytes:
    return render_document(full_hd_document())


def ocr(
    worker: OcrWorkerProcess,
    rid: str,
    pixels: bytes,
    size: tuple[int, int],
    timeout: float = 60.0,
    stride: int | None = None,
    **options: JsonValue,
) -> Blocks:
    width, height = size
    reply = worker.ocr(
        OcrRequest(rid, "s1", width, height, stride or width * 4, dict(options)), pixels, timeout
    )
    assert isinstance(reply, OcrResult), f"{reply}\n{worker.stderr[-3000:]}"
    return validate_blocks(reply.blocks, width, height)


def of(blocks: Blocks, category: str) -> Blocks:
    return [block for block in blocks if block["category"] == category]


def table_grid(block: dict[str, object]) -> dict[tuple[int, int], dict[str, object]]:
    cells = cast(list[dict[str, object]], block["cells"])
    return {(cast(int, cell["row"]), cast(int, cell["col"])): cell for cell in cells}


def assert_near(actual: object, expected: tuple[int, int, int, int], tolerance: int) -> None:
    box = cast(list[int], actual)
    assert all(abs(a - e) <= tolerance for a, e in zip(box, expected, strict=True)), (
        f"{box} 与 {expected} 相差过大"
    )


# ---------------------------------------------------------------- 版面模式


def test_structure_ready(structure_worker: tuple[OcrWorkerProcess, Ready]) -> None:
    worker, ready = structure_worker
    assert ready.protocol_version == PROTOCOL_VERSION
    caps = ready.capabilities
    assert caps["mode"] == "structure"
    assert caps["device"] == "cpu"
    assert caps["features"] == {
        "table": True,
        "formula": False,
        "chart": False,
        "doc_orientation": False,
        "doc_unwarping": False,
        "textline_orientation": False,
    }
    assert caps["unavailable"] == {"chart": "cpu"}
    models = cast(dict[str, JsonValue], caps["models"])
    assert (models["layout"], models["det"], models["rec"]) == (
        "PP-DocLayout_plus-L",
        "PP-OCRv5_mobile_det",
        "PP-OCRv5_mobile_rec",
    )
    assert "SLANet_plus" in cast(list[str], models["all"])
    assert set(ready.versions) == {"paddle", "paddleocr", "paddlex", "device"}
    assert ready.versions["paddleocr"].startswith("3.7")
    assert ready.versions["device"] == "cpu"
    assert [p.kind for p in worker.progress] == ["import", "model_load", "warmup"]


def test_structure_recognizes_document(
    structure_worker: tuple[OcrWorkerProcess, Ready], small_pixels: bytes
) -> None:
    worker, _ = structure_worker
    blocks = ocr(worker, "doc", small_pixels, (SMALL_W, SMALL_H))
    texts = [(block["category"], cast(str, block["text"])) for block in blocks]
    titles = [text for category, text in texts if category == "title"]
    assert any(TITLE[:4] in text for text in titles), texts
    body = [text for category, text in texts if category == "text"]
    assert any("机器翻译" in text and "汉字" in text for text in body), texts
    assert any("Machine translation" in text and "English" in text for text in body), texts
    # 正文块按行给出文本行，行数与画出的行数相同。
    chinese = next(block for block in blocks if "机器翻译" in cast(str, block["text"]))
    assert len(cast(list[object], chinese["lines"])) == len(CHINESE_LINES)
    english = next(block for block in blocks if "Machine translation" in cast(str, block["text"]))
    assert len(cast(list[object], english["lines"])) == len(ENGLISH_LINES)
    # 阅读顺序：标题、中文、英文、表格。
    tables = of(blocks, "table")
    assert len(tables) == 1
    orders = [cast(int, block["order"]) for block in (blocks[0], chinese, english, tables[0])]
    assert orders == sorted(orders)

    table = tables[0]
    assert_near(table["bbox"], SMALL_TABLE.bbox, CELL_TOLERANCE)
    grid = table_grid(table)
    assert set(grid) == {(r, c) for r in range(2) for c in range(3)}, "表格应为两行三列"
    for (row, col), cell in grid.items():
        assert (cell["row_span"], cell["col_span"]) == (1, 1)
        assert cell["text"] == SMALL_TABLE.rows[row][col]
        assert_near(cell["bbox"], SMALL_TABLE.cell_bbox(row, col), CELL_TOLERANCE)
    assert table["lines"] == []


def test_resize_option_maps_coordinates_back(
    structure_worker: tuple[OcrWorkerProcess, Ready], small_pixels: bytes
) -> None:
    worker, _ = structure_worker
    blocks = ocr(worker, "resized", small_pixels, (SMALL_W, SMALL_H), resize_max_side=800)
    tables = of(blocks, "table")
    assert len(tables) == 1
    # 送进产线的图只有 800×563，坐标换算回 1280×900 的原图后仍与画出的表格重合。
    assert_near(tables[0]["bbox"], SMALL_TABLE.bbox, 2 * CELL_TOLERANCE)
    grid = table_grid(tables[0])
    assert_near(grid[(1, 2)]["bbox"], SMALL_TABLE.cell_bbox(1, 2), 2 * CELL_TOLERANCE)
    assert grid[(0, 0)]["text"] == "名称"


def test_grayscale_with_padded_stride(
    structure_worker: tuple[OcrWorkerProcess, Ready], small_pixels: bytes
) -> None:
    worker, _ = structure_worker
    padded, stride = pad_rows(small_pixels, SMALL_W, SMALL_H, 64)
    blocks = ocr(worker, "gray", padded, (SMALL_W, SMALL_H), stride=stride, grayscale=True)
    tables = of(blocks, "table")
    assert len(tables) == 1
    assert {cast(str, cell["text"]) for cell in table_grid(tables[0]).values()} == {
        text for row in SMALL_TABLE.rows for text in row
    }
    assert any("Machine translation" in cast(str, block["text"]) for block in blocks)


def test_chart_is_ignored_on_cpu(
    structure_worker: tuple[OcrWorkerProcess, Ready], small_pixels: bytes
) -> None:
    worker, _ = structure_worker
    blocks = ocr(worker, "chart", small_pixels, (SMALL_W, SMALL_H), chart=True)
    assert of(blocks, "table")
    assert "只在 GPU 上提供" in worker.stderr


def test_bad_payload_is_bad_request(structure_worker: tuple[OcrWorkerProcess, Ready]) -> None:
    worker, _ = structure_worker
    reply = worker.ocr(OcrRequest("short", "s1", 64, 64, 256, {}), b"\xff" * (256 * 64 - 1))
    assert isinstance(reply, RequestFailed)
    assert reply.code == ErrorCode.BAD_REQUEST


# ---------------------------------------------------------------- 文本模式


def test_text_ready(text_worker: tuple[OcrWorkerProcess, Ready]) -> None:
    _, ready = text_worker
    caps = ready.capabilities
    assert caps["mode"] == "text"
    assert not any(cast(dict[str, bool], caps["features"]).values())
    assert caps["unavailable"] == {}
    assert cast(dict[str, JsonValue], caps["models"])["all"] == ["PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec"]


def test_text_mode_recognizes_lines(text_worker: tuple[OcrWorkerProcess, Ready], small_pixels: bytes) -> None:
    worker, _ = text_worker
    blocks = ocr(worker, "text", small_pixels, (SMALL_W, SMALL_H))
    assert len(blocks) == 1
    block = blocks[0]
    assert (block["category"], block["bbox"], block["order"], block["text"]) == (
        "text",
        [0, 0, SMALL_W, SMALL_H],
        0,
        "",
    )
    lines = [cast(str, line["text"]) for line in cast(list[dict[str, object]], block["lines"])]
    expected = [TITLE, *CHINESE_LINES, *ENGLISH_LINES, *(text for row in SMALL_TABLE.rows for text in row)]
    assert len(lines) == len(expected), lines
    exact = sum(1 for text in expected if text in lines)
    assert exact >= len(expected) - 2, f"完全正确的行太少：{lines}"
    joined = "\n".join(lines)
    for keyword in ("屏幕翻译", "机器翻译", "Machine translation", "English", "苹果", "3.50"):
        assert keyword in joined


# ---------------------------------------------------------------- 性能（需求 6.1 节：文本模式 CPU ≤ 4 秒）


def measure(worker: OcrWorkerProcess, pixels: bytes, label: str, runs: int = 3) -> float:
    ocr(worker, f"{label}-warm", pixels, (HD_W, HD_H), timeout=120)  # 第一次遇到这个尺寸，不计时
    durations: list[float] = []
    for index in range(runs):
        started = time.perf_counter()
        ocr(worker, f"{label}-{index}", pixels, (HD_W, HD_H), timeout=120)
        durations.append(time.perf_counter() - started)
    median = statistics.median(durations)
    # 控制台可能是 cp1252 编码，这一行只用 ASCII。
    print(f"\n[perf] {label} 1920x1080: {', '.join(f'{d:.2f}' for d in durations)} s, median {median:.2f} s")
    return median


@pytest.mark.timeout(300, func_only=True)
def test_text_mode_performance(text_worker: tuple[OcrWorkerProcess, Ready], hd_pixels: bytes) -> None:
    worker, _ = text_worker
    blocks = ocr(worker, "hd-check", hd_pixels, (HD_W, HD_H), timeout=120)
    assert len(cast(list[object], blocks[0]["lines"])) >= 20
    assert measure(worker, hd_pixels, "text-mode") < 15.0


@pytest.mark.timeout(300, func_only=True)
def test_structure_mode_performance(
    structure_worker: tuple[OcrWorkerProcess, Ready], hd_pixels: bytes
) -> None:
    worker, _ = structure_worker
    blocks = ocr(worker, "hd-check", hd_pixels, (HD_W, HD_H), timeout=120)
    tables = of(blocks, "table")
    assert len(tables) == 1
    assert set(table_grid(tables[0])) == {(r, c) for r in range(3) for c in range(3)}
    assert_near(tables[0]["bbox"], FULL_HD_TABLE.bbox, CELL_TOLERANCE)
    assert measure(worker, hd_pixels, "structure-mode-table-only") < 60.0


# ---------------------------------------------------------------- 按请求补加载子能力（放在性能测试之后）


@pytest.mark.timeout(300, func_only=True)
def test_request_loads_missing_feature(
    structure_worker: tuple[OcrWorkerProcess, Ready], small_pixels: bytes
) -> None:
    worker, _ = structure_worker
    blocks = ocr(worker, "formula", small_pixels, (SMALL_W, SMALL_H), timeout=240, formula=True)
    assert of(blocks, "table")
    assert "请求用到未加载的子能力 formula" in worker.stderr
    # 之后不带 formula 的请求照常使用同一条产线，不再重建。
    ocr(worker, "after-formula", small_pixels, (SMALL_W, SMALL_H))
    assert worker.stderr.count("请求用到未加载的子能力") == 1


# ---------------------------------------------------------------- 确定性故障


def configure_and_wait_exit(settings: dict[str, JsonValue]) -> tuple[int, str]:
    with OcrWorkerProcess(PADDLE_BACKEND, python=OCR_PYTHON) as worker:
        worker.send(Hello(PROTOCOL_VERSION))
        worker.send(Configure({**SETTINGS, **settings}))
        code = worker.wait_exit(timeout=120)
    assert leftover_workers(worker.address) == []
    return code, worker.stderr


def test_gpu_on_cpu_paddle_is_dependency_error() -> None:
    code, stderr = configure_and_wait_exit({"device": "gpu"})
    assert code == ExitCode.DEPENDENCY_ERROR
    assert "CPU 版" in stderr


def test_missing_model_is_config_error_without_download() -> None:
    code, stderr = configure_and_wait_exit({"layout_model": "PP-DocLayout-NoSuchModel"})
    assert code == ExitCode.CONFIG_ERROR
    assert "PP-DocLayout-NoSuchModel" in stderr
    assert "已拦截网络访问" not in stderr


# ---------------------------------------------------------------- 没有下载（放在最后）


def test_no_model_download(
    structure_worker: tuple[OcrWorkerProcess, Ready],
    text_worker: tuple[OcrWorkerProcess, Ready],
    models_before: list[str],
) -> None:
    for worker, _ in (structure_worker, text_worker):
        stderr = worker.stderr
        assert "已拦截网络访问" not in stderr
        assert "Connectivity check" not in stderr and "Checking connectivity" not in stderr
        assert "download" not in stderr.lower()
    assert model_listing() == models_before
