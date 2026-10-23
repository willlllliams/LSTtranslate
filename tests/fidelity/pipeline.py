"""保真度测试的识别链路：英文截图 → 真实 OCR 子进程 → 主进程解码、归一化与分段。

主进程一侧与 OcrModule._complete（lst/services/ocr/module.py）完全相同：默认 [ocr_normalize] 配置的管线，
文本模式以 premerged 方式一块一段，版面模式按 LineMerger 处理。OCR 进程的 Configure 由 worker_settings 从
各引擎的默认 [ocr] 与流水线配置生成，请求 options 由 request_options 生成，与组合根相同；设备固定为 CPU。

四种引擎：

| 名称 | 后端 | 解释器 | 模式 |
|---|---|---|---|
| paddle-structure | PaddleBackend | .venv-ocr | 版面模式（PP-StructureV3） |
| paddle-text | PaddleBackend | .venv-ocr | 文本模式 |
| rapid | RapidBackend | .venv-ocr | 文本模式 |
| windows | WinOcrBackend | 主环境 | 文本模式，识别语言 en |
| windows-auto | WinOcrBackend | 主环境 | 文本模式，自动选择互补识别器 |

Windows OCR 的 en 与 auto 分别保留基线。auto 在英文与已安装的中日文之间互补，避免系统首选中文时
英文页面被中文识别器误读。两种配置使用同一套人工标注，分别约束指定语言和默认配置。

``python -m tests.fidelity.pipeline [引擎 ...]`` 把各引擎对各测试图的原始识别结果写到 data/tmp/fidelity/raw，
调参时可以只重跑主进程一侧（``load_raw``）。
"""

import hashlib
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from PySide6.QtGui import QImage

from lst.contracts.ocr import LayoutBlock, OcrSegment, RawImage
from lst.contracts.ocr_engines import OcrEngineKind
from lst.contracts.worker_protocol import JsonValue, OcrRequest, OcrResult
from lst.services.ocr import (
    OcrConfig,
    OcrMode,
    PaddleOcrConfig,
    PipelineConfig,
    RapidOcrConfig,
    WindowsOcrConfig,
    effective_mode,
    request_options,
    worker_settings,
)
from lst.services.ocr.blocks import decode_blocks
from lst.services.ocr.normalize import NormalizeConfig, NormalizeContext, build_pipeline
from lst.services.ocr.paragraphs import LineMerger
from lst.services.ocr.segments import blocks_to_segments
from tests.workers.ocr.harness import MODEL_CACHE, OCR_PYTHON, OFFICIAL_MODELS, PADDLE_BACKEND, ROOT

FIXTURES = Path(__file__).with_name("fixtures")
OUTPUT = ROOT / "data" / "tmp" / "fidelity"
RAW = OUTPUT / "raw"
IMAGES = ("addons", "blog")
"""测试图：fixtures/<名>-en.png 是英文原图，<名>-zh-hant.png 是参考译图，<名>.expected.json 是期望单元。"""

RAPID_BACKEND = "lst.workers.ocr_worker.rapid_backend:RapidBackend"
WINOCR_BACKEND = "lst.workers.ocr_worker.winocr_backend:WinOcrBackend"
OCR_SITE = ROOT / ".venv-ocr" / "Lib" / "site-packages"
LOAD_TIMEOUT = 300.0
RECOGNIZE_TIMEOUT = 120.0


def _no_ocr_env() -> str | None:
    return None if OCR_PYTHON.is_file() else "本机没有 OCR 环境 .venv-ocr"


def _paddle_missing() -> str | None:
    if (reason := _no_ocr_env()) is not None:
        return reason
    return None if OFFICIAL_MODELS.is_dir() else "本机没有 PaddleX 模型缓存"


def _rapid_missing() -> str | None:
    if (reason := _no_ocr_env()) is not None:
        return reason
    has = any(OCR_SITE.glob("rapidocr-*.dist-info")) and any(OCR_SITE.glob("onnxruntime-*.dist-info"))
    return None if has else ".venv-ocr 里没有 rapidocr 或 onnxruntime"


def _windows_missing() -> str | None:
    try:
        from lst.workers.ocr_worker.winocr_backend import WinRtOcrApi, match_language

        languages = WinRtOcrApi().available_languages()
    except (ImportError, OSError):
        return "本机不能调用 Windows OCR"
    return None if match_language("en", languages) is not None else "系统没有英文 OCR"


@dataclass(frozen=True, slots=True)
class Engine:
    """一种引擎配置。missing() 返回跳过的原因，可用时返回 None。"""

    name: str
    backend: str
    python: Path
    config: OcrConfig
    pipeline: PipelineConfig
    missing: Callable[[], str | None]
    extra: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])

    @property
    def mode(self) -> OcrMode:
        return effective_mode(self.config)

    def settings(self) -> dict[str, JsonValue]:
        return {**worker_settings(self.config, self.pipeline), "device": "cpu", **self.extra}

    def options(self) -> dict[str, JsonValue]:
        return request_options(self.config, self.pipeline)


ENGINES: dict[str, Engine] = {
    "paddle-structure": Engine(
        "paddle-structure",
        PADDLE_BACKEND,
        OCR_PYTHON,
        OcrConfig(engine=OcrEngineKind.PADDLE, mode=OcrMode.STRUCTURE),
        PaddleOcrConfig(),
        _paddle_missing,
        {"model_cache_dir": str(MODEL_CACHE)},
    ),
    "paddle-text": Engine(
        "paddle-text",
        PADDLE_BACKEND,
        OCR_PYTHON,
        OcrConfig(engine=OcrEngineKind.PADDLE, mode=OcrMode.TEXT),
        PaddleOcrConfig(),
        _paddle_missing,
        {"model_cache_dir": str(MODEL_CACHE)},
    ),
    "rapid": Engine(
        "rapid",
        RAPID_BACKEND,
        OCR_PYTHON,
        OcrConfig(engine=OcrEngineKind.RAPID),
        RapidOcrConfig(),
        _rapid_missing,
    ),
    "windows": Engine(
        "windows",
        WINOCR_BACKEND,
        Path(sys.executable),
        OcrConfig(engine=OcrEngineKind.WINDOWS, language="en"),
        WindowsOcrConfig(),
        _windows_missing,
    ),
    "windows-auto": Engine(
        "windows-auto",
        WINOCR_BACKEND,
        Path(sys.executable),
        OcrConfig(engine=OcrEngineKind.WINDOWS),
        WindowsOcrConfig(),
        _windows_missing,
    ),
}


# ---------------------------------------------------------------- 图像


def english_path(image: str) -> Path:
    for suffix in ("png", "webp"):
        path = FIXTURES / f"{image}-en.{suffix}"
        if path.is_file():
            return path
    raise FileNotFoundError(f"没有测试原图 {image}")


def reference_path(image: str) -> Path:
    original = FIXTURES / f"{image}-zh-hant.png"
    if original.is_file():
        return original
    return FIXTURES / f"{image}-zh-hant.webp"


def expected_path(image: str) -> Path:
    return FIXTURES / f"{image}.expected.json"


def load_image(path: Path) -> QImage:
    """读入图像，转成 ARGB32（内存里按 BGRA 排列，与截屏的像素帧相同）。"""
    image = QImage(str(path))
    assert not image.isNull(), path
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def bgra(image: QImage) -> tuple[bytes, int]:
    """（像素帧，stride）。"""
    stride = image.bytesPerLine()
    return bytes(cast(bytes, image.constBits()))[: stride * image.height()], stride


# ---------------------------------------------------------------- 识别


@dataclass(frozen=True, slots=True)
class Recognized:
    """一次识别：OCR 进程的原始 blocks 与主进程转换的结果。"""

    engine: str
    image: str
    width: int
    height: int
    raw: list[JsonValue]
    blocks: tuple[LayoutBlock, ...]
    segments: tuple[OcrSegment, ...]
    seconds: float


def convert(
    raw: list[JsonValue], width: int, height: int, mode: OcrMode, image: RawImage | None = None
) -> tuple[tuple[LayoutBlock, ...], tuple[OcrSegment, ...]]:
    """主进程一侧的转换，与 OcrModule._complete 相同。"""
    decoded = decode_blocks(raw, width=width, height=height)
    normalizer = build_pipeline(NormalizeConfig())
    text_mode = mode is OcrMode.TEXT
    blocks = normalizer.apply(decoded, NormalizeContext(width, height, text_mode, image))
    if text_mode and normalizer.merges_paragraphs:
        return blocks, blocks_to_segments(blocks, mode, premerged=True)
    return blocks, blocks_to_segments(blocks, mode, LineMerger())


def recognize(worker: "OcrWorker", engine: Engine, image: str) -> Recognized:
    picture = load_image(english_path(image))
    pixels, stride = bgra(picture)
    width, height = picture.width(), picture.height()
    request = OcrRequest(f"{engine.name}-{image}", "s1", width, height, stride, engine.options())
    started = time.perf_counter()
    reply = worker.ocr(request, pixels, RECOGNIZE_TIMEOUT)
    seconds = time.perf_counter() - started
    assert isinstance(reply, OcrResult), f"{reply}\n{worker.stderr[-3000:]}"
    blocks, segments = convert(
        reply.blocks, width, height, engine.mode, RawImage(width, height, stride, pixels)
    )
    return Recognized(engine.name, image, width, height, reply.blocks, blocks, segments, seconds)


def load_raw(engine: Engine, image: str, directory: Path = RAW) -> Recognized:
    """从 data/tmp/fidelity/raw 读回原始识别结果，重新做主进程一侧的转换。"""
    data = json.loads((directory / f"{engine.name}-{image}.json").read_text("utf-8"))
    raw = cast(list[JsonValue], data["blocks"])
    width, height = cast(int, data["width"]), cast(int, data["height"])
    picture = load_image(english_path(image))
    pixels, stride = bgra(picture)
    source_hash = hashlib.sha256(english_path(image).read_bytes()).hexdigest()
    if data.get("source_sha256") != source_hash:
        raise ValueError(f"{engine.name}-{image} 的识别缓存与当前原图不一致，请重新推理")
    if (width, height) != (picture.width(), picture.height()):
        raise ValueError(f"{engine.name}-{image} 的缓存尺寸与当前原图不一致")
    blocks, segments = convert(raw, width, height, engine.mode, RawImage(width, height, stride, pixels))
    return Recognized(engine.name, image, width, height, raw, blocks, segments, float(data["seconds"]))


def save_raw(result: Recognized) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_sha256": hashlib.sha256(english_path(result.image).read_bytes()).hexdigest(),
        "width": result.width,
        "height": result.height,
        "seconds": result.seconds,
        "blocks": result.raw,
    }
    (RAW / f"{result.engine}-{result.image}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), "utf-8"
    )


# OcrWorkerProcess 的导入放在这里，类型注解用字符串，避免模块文档之前出现大段导入。
from tests.workers.ocr.harness import OcrWorkerProcess as OcrWorker  # noqa: E402


def start_worker(engine: Engine) -> OcrWorker:
    worker = OcrWorker(engine.backend, python=engine.python)
    worker.start(connect_timeout=120)
    try:
        worker.handshake(engine.settings(), timeout=LOAD_TIMEOUT)
    except BaseException:
        worker.close()
        raise
    return worker


def main(names: Sequence[str]) -> None:
    for name in names or list(ENGINES):
        engine = ENGINES[name]
        reason = engine.missing()
        if reason is not None:
            print(f"{name}: 跳过（{reason}）")
            continue
        started = time.perf_counter()
        worker = start_worker(engine)
        try:
            print(f"{name}: 加载 {time.perf_counter() - started:.1f} 秒")
            for image in IMAGES:
                result = recognize(worker, engine, image)
                save_raw(result)
                print(f"{name} {image}: {len(result.segments)} 段，识别 {result.seconds:.1f} 秒")
        finally:
            worker.close()


if __name__ == "__main__":
    from PySide6.QtGui import QGuiApplication

    _app = QGuiApplication(["fidelity"])
    main(sys.argv[1:])
