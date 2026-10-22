"""真实 PaddleOCR 截屏回归测试：截屏测试页上 OCR 认出的文字都要成为段。

测试图是 fixtures/screenshot_test_page.jpg（800×811，data/tmp/testpage/index.html 在浏览器里的截图），
页面上依次是标题、两段英文、一排带边框的短词按钮（Public、Star、Code、Actions、Wiki、Sign in、
• Python ☆4）和一个三行两列的表格（Region/Sales、North/120、South/95）。

每个用例走一遍真实的识别链路：OCR 子进程（.venv-ocr 里的 PaddleBackend，版面模式）→ decode_blocks →
默认配置的结构归一化 → blocks_to_segments，与 OcrModule._complete 相同。两种版面各测一次：

- 原图：PP-DocLayout 把每个按钮判成一个 text 块，表格是 table 块。按钮的文本行框连边框一起包进去，
  只有约 45% 落在版面块内，转换时要把行归给这个块，不然同一个词会出两个段。
- 放大 1.3 倍再裁成 950×758（真机选区的物理像素尺寸）：PP-DocLayout 把按钮连同表格判成一个 image 块，
  块内的文本行要逐行出段。修复前这些行在转换时被丢掉，覆盖窗口只显示这片区域的原图。

表格识别开与关各测一次：开时表格按单元格出段，关时按文本行出段，两种情况 6 个格子的文字都在段里。
OCR 进程的配置由 worker_settings 与 request_options 从默认的 [ocr]、[ocr_paddle] 生成，
与组合根（lst/app/bootstrap.py 的 ocr_process_settings）给出的相同，设备固定为 CPU。

前提与 test_paddle_process.py 相同：.venv-ocr 与本机模型缓存都在，缺一项整个模块跳过。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from lst.contracts.ocr import BlockCategory, LayoutBlock, OcrSegment
from lst.contracts.ocr_engines import OcrEngineKind
from lst.contracts.worker_protocol import JsonValue, OcrRequest, OcrResult
from lst.services.ocr import OcrConfig, OcrMode, PaddleOcrConfig, request_options, worker_settings
from lst.services.ocr.blocks import decode_blocks
from lst.services.ocr.normalize import NormalizeConfig, NormalizeContext, build_pipeline
from lst.services.ocr.paragraphs import LineMerger
from lst.services.ocr.segments import blocks_to_segments
from tests.workers.ocr.harness import (
    MODEL_CACHE,
    OCR_PYTHON,
    OFFICIAL_MODELS,
    PADDLE_BACKEND,
    OcrWorkerProcess,
    has_ocr_env,
    leftover_workers,
)

pytestmark = [
    pytest.mark.skipif(not has_ocr_env(), reason="本机没有 OCR 环境 .venv-ocr"),
    pytest.mark.skipif(not OFFICIAL_MODELS.is_dir(), reason="本机没有 PaddleX 模型缓存"),
    pytest.mark.timeout(60, func_only=True),
]

PAGE = Path(__file__).with_name("fixtures") / "screenshot_test_page.jpg"
LOAD_TIMEOUT = 300.0

PARAGRAPHS = (
    "The file could not be found. Please check the name and try again.",
    "Your changes have been saved. Restart the application to apply the new settings.",
)
SHORT_WORDS = ("Public", "Star", "Code", "Actions", "Wiki", "Sign in", "Python☆4")
TABLE = ("Region", "Sales", "North", "120", "South", "95")


def squeeze(text: str) -> str:
    """去掉空白和项目符号再比较：识别结果里词间空格时有时无（实测 “Yourchanges”），
    “• Python ☆4” 的圆点有时识别成 “·”，有时丢掉。"""
    return "".join(char for char in text if not char.isspace() and char not in BULLETS)


BULLETS = frozenset("•·")


CONFIG = OcrConfig(engine=OcrEngineKind.PADDLE, mode=OcrMode.STRUCTURE)
PIPELINE = PaddleOcrConfig()


@pytest.fixture(scope="module")
def worker() -> Iterator[OcrWorkerProcess]:
    settings: dict[str, JsonValue] = {
        **worker_settings(CONFIG, PIPELINE),
        "model_cache_dir": str(MODEL_CACHE),
        "device": "cpu",
        # 两种表格设置共用一个进程：加载时带上表格模型，请求里的 table 决定这一次开不开。
        "table": True,
    }
    process = OcrWorkerProcess(PADDLE_BACKEND, python=OCR_PYTHON)
    process.start(connect_timeout=120)
    try:
        process.handshake(settings, timeout=LOAD_TIMEOUT)
        yield process
    finally:
        process.close()
        assert leftover_workers(process.address) == []


def page_image(scale: float, crop: tuple[int, int, int, int] | None) -> QImage:
    image = QImage(str(PAGE)).convertToFormat(QImage.Format.Format_ARGB32)
    assert not image.isNull(), PAGE
    if scale != 1.0:
        image = image.scaled(
            round(image.width() * scale),
            round(image.height() * scale),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    if crop is not None:
        image = image.copy(QRect(*crop))
    return image


def recognize(
    worker: OcrWorkerProcess, rid: str, image: QImage, table: bool
) -> tuple[tuple[LayoutBlock, ...], tuple[OcrSegment, ...]]:
    """走一遍 OcrModule._complete 的版面模式链路，返回（归一化后的块，段）。"""
    width, height = image.width(), image.height()
    stride = image.bytesPerLine()
    pixels = bytes(image.constBits())[: stride * height]  # pyright: ignore[reportArgumentType]
    options: dict[str, JsonValue] = {**request_options(CONFIG, PIPELINE), "table": table}
    reply = worker.ocr(OcrRequest(rid, "s1", width, height, stride, options), pixels, 60.0)
    assert isinstance(reply, OcrResult), f"{reply}\n{worker.stderr[-3000:]}"
    decoded = decode_blocks(reply.blocks, width=width, height=height)
    blocks = build_pipeline(NormalizeConfig()).apply(decoded, NormalizeContext(width, height, False))
    return blocks, blocks_to_segments(blocks, OcrMode.STRUCTURE, LineMerger())


LAYOUTS = {
    # 原图：按钮是 text 块，表格是 table 块。
    "original": (1.0, None, BlockCategory.TABLE),
    # 真机选区的尺寸：按钮连同表格是一个 image 块。
    "capture": (1.3, (40, 60, 990, 818), BlockCategory.IMAGE),
}


@pytest.mark.parametrize("table", [False, True], ids=["table-off", "table-on"])
@pytest.mark.parametrize("layout", list(LAYOUTS))
def test_all_recognized_text_becomes_segments(
    qapp: QApplication, worker: OcrWorkerProcess, layout: str, table: bool
) -> None:
    del qapp
    scale, crop, lower_category = LAYOUTS[layout]
    blocks, segments = recognize(worker, f"{layout}-{table}", page_image(scale, crop), table)
    texts = [squeeze(segment.segment.text) for segment in segments]

    # 按钮与表格所在的那个块的类别证明这次走的是预期的版面。
    def holds_region(block: LayoutBlock) -> bool:
        return any(squeeze(item.text) == "Region" for item in (*block.lines, *block.cells))

    table_block = next(block for block in blocks if holds_region(block))
    assert table_block.category is lower_category
    if lower_category is BlockCategory.TABLE and table:
        assert {(cell.row, cell.col) for cell in table_block.cells} == {
            (r, c) for r in range(3) for c in range(2)
        }

    assert texts[0] == squeeze("Screenshot translation test")
    for paragraph in PARAGRAPHS:
        assert texts.count(squeeze(paragraph)) == 1, (paragraph, texts)
    for word in (*SHORT_WORDS, *TABLE):
        assert texts.count(squeeze(word)) == 1, (word, texts)
    assert len(segments) == 1 + len(PARAGRAPHS) + len(SHORT_WORDS) + len(TABLE), texts
