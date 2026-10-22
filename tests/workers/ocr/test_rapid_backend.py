"""RapidOCR 后端的单元测试：假的 RapidOCR 识别器，不导入 rapidocr、onnxruntime、cv2。

检查设置与选项的校验、识别语言的映射、模型文件的检查、坐标换算回原图、行的排列与过滤、空结果，
以及输出能被主进程的解码器接受。
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest

from lst.contracts.ocr import BlockCategory
from lst.contracts.worker_protocol import JsonValue
from lst.services.ocr.blocks import decode_blocks
from lst.workers._runtime import ConfigError
from lst.workers.ocr_worker import rapid_backend
from lst.workers.ocr_worker.backend import ModelMissing
from lst.workers.ocr_worker.image import Pixels
from lst.workers.ocr_worker.rapid_backend import (
    CPU_PROVIDER,
    CUDA_PROVIDER,
    DET_MODEL,
    DML_PROVIDER,
    LANGUAGES,
    MODEL_FILES,
    REC_MODEL,
    OrtRuntime,
    RapidBackend,
    RapidOptions,
    RapidSettings,
    UnsupportedLanguage,
    build_blocks_json,
    choose_provider,
    engine_params,
    output_lines,
    rapid_language,
)
from lst.workers.ocr_worker.settings import BadOptions
from tests.workers.ocr.validation import validate_blocks

type Quad = Sequence[Sequence[float]]


def no_progress(kind: str, detail: str = "", done: int | None = None, total: int | None = None) -> None:
    del kind, detail, done, total


def quad(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    """轴对齐矩形的四个角点，顺序与 RapidOCR 相同：左上、右上、右下、左下。"""
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


@dataclass
class FakeOutput:
    """RapidOCROutput 用到的三个字段。没有识别到文字时 RapidOCR 返回三者都为 None 的输出。"""

    boxes: object = None
    txts: tuple[str, ...] | None = None
    scores: tuple[float, ...] | None = None


def output(*lines: tuple[Quad, str, float]) -> FakeOutput:
    return FakeOutput(
        boxes=np.array([line[0] for line in lines], dtype=np.float32),
        txts=tuple(line[1] for line in lines),
        scores=tuple(line[2] for line in lines),
    )


@dataclass
class FakeEngine:
    """假的 RapidOCR 识别器：记录收到的图像，返回预设的输出。"""

    result: FakeOutput = field(default_factory=FakeOutput)
    images: list[Pixels] = field(default_factory=lambda: list[Pixels]())
    calls: list[dict[str, object]] = field(default_factory=lambda: list[dict[str, object]]())

    def __call__(self, img_content: Pixels, **kwargs: object) -> object:
        self.images.append(img_content)
        self.calls.append(kwargs)
        return self.result


@dataclass
class FakeFactory:
    engine: FakeEngine = field(default_factory=FakeEngine)
    params: list[dict[str, object]] = field(default_factory=lambda: list[dict[str, object]]())

    def __call__(self, params: dict[str, object]) -> FakeEngine:
        self.params.append(params)
        return self.engine


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "rapidocr" / "models"
    directory.mkdir(parents=True)
    for name in MODEL_FILES:
        (directory / name).write_bytes(b"onnx")
    return directory


def warm_image() -> Pixels:
    return np.zeros((8, 8, 3), np.uint8)


def loaded(
    model_dir: Path, settings: dict[str, JsonValue] | None = None, result: FakeOutput | None = None
) -> tuple[RapidBackend, FakeFactory]:
    factory = FakeFactory(FakeEngine(result or FakeOutput()))
    backend = RapidBackend(factory, model_dir, warmup_source=warm_image)
    backend.load(settings or {"mode": "text", "engine": "rapid"}, no_progress)
    return backend, factory


# ---------------------------------------------------------------- 设置与选项


def test_settings_defaults_and_ignored_paddle_keys() -> None:
    settings = RapidSettings.parse(
        {"mode": "text", "engine": "rapid", "device": "gpu", "table": True, "model_cache_dir": "x"}
    )
    assert settings == RapidSettings(
        language="auto",
        rapid_language="ch",
        grayscale=False,
        resize_max_side=0,
        cpu_threads=0,
        warmup=True,
        device="gpu",
    )
    assert settings.construct["det_thresh"] == 0.3 and settings.call["text_score"] == 0.5


@pytest.mark.parametrize(
    "raw",
    [
        {"mode": "structure"},
        {"engine": "windows"},
        {"engine": "paddle"},
        {"language": ""},
        {"language": 3},
        {"language": "ko"},
        {"grayscale": "yes"},
        {"resize_max_side": -1},
        {"cpu_threads": 1.5},
        {"warmup": 1},
        {"device": "tpu"},
        {"det_thresh": 1.5},
        {"det_limit_side_len": 8},
        {"det_limit_type": "both"},
        {"text_score": "high"},
        {"det_model_path": ""},
        {"recognition_languages": "en"},
        {"language": "de", "recognition_languages": ["zh-Hans", "en"]},
    ],
)
def test_invalid_settings_are_config_errors(raw: dict[str, JsonValue]) -> None:
    with pytest.raises(ConfigError):
        RapidSettings.parse(raw)


def test_request_options_override_defaults() -> None:
    settings = RapidSettings("auto", "ch", grayscale=True, resize_max_side=2000, cpu_threads=0, warmup=False)
    assert RapidOptions.parse({}, settings) == RapidOptions(True, 2000)
    assert RapidOptions.parse({"grayscale": False, "resize_max_side": 0, "formula": True}, settings) == (
        RapidOptions(False, 0)
    )
    options = RapidOptions.parse({"det_box_thresh": 0.7, "textline_orientation": True}, settings)
    assert options.call_kwargs() == {
        "box_thresh": 0.7,
        "unclip_ratio": 1.6,
        "text_score": 0.5,
        "use_cls": True,
    }
    with pytest.raises(BadOptions):
        RapidOptions.parse({"text_score": 2.0}, settings)
    with pytest.raises(BadOptions):
        RapidOptions.parse({"grayscale": 1}, settings)
    with pytest.raises(BadOptions):
        RapidOptions.parse({"resize_max_side": -5}, settings)


# ---------------------------------------------------------------- 识别语言


@pytest.mark.parametrize(
    ("language", "name"),
    [
        ("auto", "ch"),
        ("AUTO", "ch"),
        ("zh-Hans", "ch"),
        ("zh-CN", "ch"),
        ("zh-Hant", "chinese_cht"),
        ("zh-TW", "chinese_cht"),
        ("en", "en"),
        ("en-US", "en"),
        ("ja", "japan"),
        ("de", "de"),
        ("fr-CA", "fr"),
        ("no", "no"),
        ("nb", "no"),
        ("vi", "vi"),
    ],
)
def test_language_mapping(language: str, name: str) -> None:
    assert rapid_language(language) == name


@pytest.mark.parametrize("language", ["ko", "ru", "ar", "th", "hi", "sr", "x-bogus!"])
def test_languages_outside_the_model_are_config_errors(language: str) -> None:
    with pytest.raises(UnsupportedLanguage) as info:
        rapid_language(language)
    assert isinstance(info.value, ConfigError)


def test_reported_languages_start_with_general_ones_and_are_canonical() -> None:
    assert LANGUAGES[:4] == ("zh-Hans", "zh-Hant", "en", "ja")
    assert len(set(LANGUAGES)) == len(LANGUAGES)
    for code in LANGUAGES:
        rapid_language(code)


# ---------------------------------------------------------------- 加载


def test_load_uses_local_models_and_reports_capabilities(model_dir: Path) -> None:
    factory = FakeFactory()
    backend = RapidBackend(factory, model_dir, warmup_source=warm_image)
    steps: list[str] = []

    def progress(kind: str, detail: str = "", done: int | None = None, total: int | None = None) -> None:
        del done, total
        steps.append(f"{kind}:{detail}")

    result = backend.load({"mode": "text", "engine": "rapid", "language": "en", "cpu_threads": 3}, progress)
    [params] = factory.params
    assert params["Det.model_path"] == str(model_dir / DET_MODEL)
    assert params["Rec.model_path"] == str(model_dir / REC_MODEL)
    assert params["Global.use_cls"] is False
    assert params["EngineConfig.onnxruntime.intra_op_num_threads"] == 3
    assert steps == ["model_load:rapid", "warmup:"]
    assert len(factory.engine.images) == 1, "预热应推理一次"
    capabilities = result.capabilities
    assert capabilities["engine"] == "rapid"
    assert capabilities["mode"] == "text"
    assert capabilities["features"] == ["text"]
    assert capabilities["language"] == "en"
    assert capabilities["languages"] == list(LANGUAGES)
    assert result.versions["engine"] == "rapid"


def test_engine_params_never_leave_model_path_empty(tmp_path: Path) -> None:
    params = engine_params(tmp_path, 8)
    # model_path 为空时 RapidOCR 会按 default_models.yaml 去下载模型。
    assert params["Det.model_path"] and params["Rec.model_path"]
    assert params["Global.model_root_dir"] == str(tmp_path)


def test_warmup_can_be_disabled_and_failure_is_tolerated(model_dir: Path) -> None:
    _, factory = loaded(model_dir, {"warmup": False})
    assert factory.engine.images == []

    def broken() -> Pixels:
        raise RuntimeError("no cv2")

    backend = RapidBackend(FakeFactory(), model_dir, warmup_source=broken)
    backend.load({}, no_progress)  # 预热失败只记警告


def test_missing_model_files_are_model_missing(model_dir: Path) -> None:
    (model_dir / REC_MODEL).unlink()
    backend = RapidBackend(FakeFactory(), model_dir, warmup_source=warm_image)
    with pytest.raises(ModelMissing) as info:
        backend.load({}, no_progress)
    assert REC_MODEL in str(info.value)


def test_unsupported_language_fails_before_creating_engine(model_dir: Path) -> None:
    factory = FakeFactory()
    backend = RapidBackend(factory, model_dir, warmup_source=warm_image)
    with pytest.raises(ConfigError):
        backend.load({"language": "ko"}, no_progress)
    assert factory.params == []


def test_recognize_before_load_fails() -> None:
    with pytest.raises(RuntimeError):
        RapidBackend(FakeFactory(), Path("."), warmup_source=warm_image).recognize(
            np.zeros((4, 4, 3), np.uint8), {}
        )


def test_module_import_does_not_touch_inference_libraries() -> None:
    # 本模块只在 load 时导入 rapidocr；模块本身只依赖标准库、numpy 与本包。
    source = Path(rapid_backend.__file__).read_text(encoding="utf-8")
    for name in ("import rapidocr", "import onnxruntime", "import cv2", "paddle"):
        assert f"\n{name}" not in source and f"from {name}" not in source


# ---------------------------------------------------------------- 结果转换


def test_output_lines_reads_quads_and_drops_blank_lines() -> None:
    result = output(
        (quad(10, 50, 200, 80), "  second  ", 0.9),
        (quad(10, 10, 200, 40), "first", 0.8),
        (quad(10, 90, 200, 120), "   ", 0.99),
    )
    lines = output_lines(result)
    assert [line.text for line in lines] == ["first", "second"]
    assert lines[0].box == (10.0, 10.0, 200.0, 40.0)
    assert lines[1].score == pytest.approx(0.9)


def test_output_lines_reading_order_within_a_row() -> None:
    # 同一行的两段：右边那段先给出，输出按从左到右排列。
    result = output(
        (quad(300, 12, 400, 38), "right", 0.9),
        (quad(10, 10, 200, 40), "left", 0.9),
        (quad(10, 60, 200, 90), "below", 0.9),
    )
    assert [line.text for line in output_lines(result)] == ["left", "right", "below"]


def test_rotated_quad_uses_axis_aligned_bounds() -> None:
    result = output(([[20, 30], [120, 10], [130, 40], [30, 60]], "tilted", 0.7))
    [line] = output_lines(result)
    assert line.box == (20.0, 10.0, 130.0, 60.0)


def test_empty_output_gives_no_blocks() -> None:
    assert output_lines(FakeOutput()) == []
    assert build_blocks_json(FakeOutput(), (100, 100), (100, 100)) == []
    assert build_blocks_json(output((quad(1, 1, 5, 5), " ", 0.9)), (100, 100), (100, 100)) == []


def test_mismatched_fields_are_an_error() -> None:
    broken = FakeOutput(boxes=np.zeros((2, 4, 2)), txts=("a",), scores=(0.5,))
    with pytest.raises(ValueError):
        output_lines(broken)


def test_blocks_cover_whole_screenshot_and_map_coordinates_back() -> None:
    # 原图 4000×2000 缩小到 2000×1000 后识别；坐标应放大两倍。
    result = output((quad(100.2, 50.6, 300.4, 80.1), "Hello", 0.8), (quad(10, 200, 50, 230), "世界", 0.6))
    raw = build_blocks_json(result, (4000, 2000), (2000, 1000))
    blocks = validate_blocks(raw, 4000, 2000)
    [block] = blocks
    assert block["category"] == "text" and block["order"] == 0 and block["text"] == ""
    assert block["bbox"] == [0, 0, 4000, 2000]
    assert block["score"] == pytest.approx(0.7)
    lines = block["lines"]
    assert isinstance(lines, list)
    assert [line["text"] for line in lines] == ["Hello", "世界"]  # pyright: ignore[reportUnknownVariableType]
    assert lines[0]["bbox"] == [200, 101, 601, 161]  # 左上向下取整、右下向上取整
    assert lines[1]["bbox"] == [20, 400, 100, 460]
    decoded = decode_blocks(raw, width=4000, height=2000)
    assert len(decoded) == 1 and decoded[0].category is BlockCategory.TEXT
    assert [line.text for line in decoded[0].lines] == ["Hello", "世界"]


def test_lines_outside_the_image_are_dropped() -> None:
    result = output((quad(-50, -50, -10, -10), "gone", 0.9), (quad(5, 5, 50, 20), "kept", 0.9))
    [block] = validate_blocks(build_blocks_json(result, (100, 100), (100, 100)), 100, 100)
    lines = block["lines"]
    assert isinstance(lines, list) and len(lines) == 1  # pyright: ignore[reportUnknownArgumentType]


# ---------------------------------------------------------------- 识别


def test_recognize_resizes_then_grayscales_and_maps_back(model_dir: Path) -> None:
    result = output((quad(100, 100, 300, 150), "text", 0.95))
    backend, factory = loaded(model_dir, {"warmup": False, "grayscale": True}, result)
    image = np.zeros((1000, 2000, 3), np.uint8)
    image[:, :, 2] = 200  # 纯红
    blocks = backend.recognize(image, {"resize_max_side": 1000})
    [sent] = factory.engine.images
    assert sent.shape == (500, 1000, 3)
    assert sent.flags["C_CONTIGUOUS"]
    assert np.all(sent[:, :, 0] == sent[:, :, 1]) and np.all(sent[:, :, 1] == sent[:, :, 2])
    assert int(sent[0, 0, 0]) == round(0.299 * 200)
    [block] = validate_blocks(blocks, 2000, 1000)
    lines = block["lines"]
    assert isinstance(lines, list)
    assert lines[0]["bbox"] == [200, 200, 600, 300]  # pyright: ignore[reportUnknownMemberType]


def test_recognize_without_resizing_passes_original_pixels(model_dir: Path) -> None:
    backend, factory = loaded(model_dir, {"warmup": False})
    image = np.arange(4 * 6 * 3, dtype=np.uint8).reshape(4, 6, 3)
    assert backend.recognize(image, {}) == []
    [sent] = factory.engine.images
    assert np.array_equal(sent, image)


def test_bad_request_options(model_dir: Path) -> None:
    backend, _ = loaded(model_dir, {"warmup": False})
    with pytest.raises(BadOptions):
        backend.recognize(np.zeros((4, 4, 3), np.uint8), {"resize_max_side": "big"})


# ---------------------------------------------------------------- 推理设备、模型路径与参数


@dataclass
class FakeSession:
    providers: list[str]

    def get_providers(self) -> list[str]:
        return self.providers


@dataclass
class AcceleratedEngine(FakeEngine):
    """预热后检测会话使用 used 指定的执行器，结构与 rapidocr 的 text_det.session.session 相同。"""

    used: str = CPU_PROVIDER

    def __call__(self, img_content: Pixels, **kwargs: object) -> object:
        self.text_det = type(
            "Det", (), {"session": type("Ort", (), {"session": FakeSession([self.used])})()}
        )()
        return super().__call__(img_content, **kwargs)


def runtime(*providers: str, preload_error: Exception | None = None) -> tuple[OrtRuntime, list[str]]:
    calls: list[str] = []

    def preload() -> None:
        calls.append("preload")
        if preload_error is not None:
            raise preload_error

    return OrtRuntime((*providers, CPU_PROVIDER), preload, "onnxruntime-gpu", "1.31.0"), calls


@pytest.mark.parametrize(
    ("device", "providers", "expected"),
    [
        ("cpu", (CUDA_PROVIDER, CPU_PROVIDER), None),
        ("auto", (CUDA_PROVIDER, CPU_PROVIDER), CUDA_PROVIDER),
        ("auto", (DML_PROVIDER, CPU_PROVIDER), DML_PROVIDER),
        ("auto", (CPU_PROVIDER,), None),
        ("gpu", (DML_PROVIDER, CPU_PROVIDER), DML_PROVIDER),
    ],
)
def test_choose_provider(
    device: rapid_backend.Device, providers: tuple[str, ...], expected: str | None
) -> None:
    assert choose_provider(device, providers) == expected


def test_gpu_on_cpu_runtime_is_a_dependency_error(model_dir: Path) -> None:
    ort, _ = runtime()
    backend = RapidBackend(FakeFactory(), model_dir, warmup_source=warm_image, runtime=lambda: ort)
    with pytest.raises(ImportError) as info:
        backend.load({"device": "gpu"}, no_progress)
    assert "CPU 版" in str(info.value)


def test_cuda_preloads_dlls_and_enables_the_provider(model_dir: Path) -> None:
    ort, calls = runtime(CUDA_PROVIDER)
    factory = FakeFactory(AcceleratedEngine(used=CUDA_PROVIDER))
    backend = RapidBackend(factory, model_dir, warmup_source=warm_image, runtime=lambda: ort)
    result = backend.load({"device": "auto", "warmup": False}, no_progress)
    assert calls == ["preload"]
    [params] = factory.params
    assert params["EngineConfig.onnxruntime.use_cuda"] is True
    assert params["EngineConfig.onnxruntime.use_dml"] is False
    assert len(factory.engine.images) == 1, "用加速时总要预热一次，核对实际的执行器"
    assert result.capabilities["device"] == "cuda"
    assert result.versions["onnxruntime_package"] == "onnxruntime-gpu"


def test_accelerator_not_working_fails_for_gpu_and_falls_back_for_auto(model_dir: Path) -> None:
    ort, _ = runtime(DML_PROVIDER)
    engine = AcceleratedEngine(used=CPU_PROVIDER)
    backend = RapidBackend(FakeFactory(engine), model_dir, warmup_source=warm_image, runtime=lambda: ort)
    with pytest.raises(ImportError) as info:
        backend.load({"device": "gpu"}, no_progress)
    assert DML_PROVIDER in str(info.value)
    backend = RapidBackend(FakeFactory(engine), model_dir, warmup_source=warm_image, runtime=lambda: ort)
    assert backend.load({"device": "auto"}, no_progress).capabilities["device"] == "cpu"


def test_cuda_preload_failure(model_dir: Path) -> None:
    ort, _ = runtime(CUDA_PROVIDER, preload_error=OSError("cudnn64_9.dll"))
    backend = RapidBackend(FakeFactory(), model_dir, warmup_source=warm_image, runtime=lambda: ort)
    with pytest.raises(ImportError):
        backend.load({"device": "gpu"}, no_progress)
    backend = RapidBackend(FakeFactory(), model_dir, warmup_source=warm_image, runtime=lambda: ort)
    assert backend.load({"device": "auto", "warmup": False}, no_progress).capabilities["device"] == "cpu"


def test_selected_model_paths_and_construct_params(model_dir: Path, tmp_path: Path) -> None:
    rec = tmp_path / "PP-OCRv6_rec_medium.onnx"
    rec.write_bytes(b"onnx")
    backend, factory = loaded(
        model_dir,
        {
            "warmup": False,
            "rec_model_path": str(rec),
            "recognition_languages": ["zh-Hans", "en", "de"],
            "language": "de",
            "det_thresh": 0.2,
            "det_limit_side_len": 960,
            "det_limit_type": "max",
            "max_side_len": 4000,
            "text_score": 0.6,
        },
    )
    [params] = factory.params
    assert params["Rec.model_path"] == str(rec)
    assert params["Det.thresh"] == 0.2 and params["Det.limit_side_len"] == 960
    assert params["Det.limit_type"] == "max" and params["Global.max_side_len"] == 4000
    assert params["Global.text_score"] == 0.6
    del backend


def test_missing_selected_model_file_is_model_missing(model_dir: Path, tmp_path: Path) -> None:
    backend = RapidBackend(FakeFactory(), model_dir, warmup_source=warm_image)
    with pytest.raises(ModelMissing) as info:
        backend.load({"rec_model_path": str(tmp_path / "absent.onnx")}, no_progress)
    assert "absent.onnx" in str(info.value)


def test_per_call_params_and_textline_orientation(model_dir: Path) -> None:
    (model_dir / rapid_backend.CLS_MODEL).write_bytes(b"onnx")
    backend, factory = loaded(
        model_dir, {"warmup": False, "det_box_thresh": 0.4, "textline_orientation": True}
    )
    backend.recognize(np.zeros((8, 8, 3), np.uint8), {"text_score": 0.7})
    [call] = factory.engine.calls
    assert call == {"box_thresh": 0.4, "unclip_ratio": 1.6, "text_score": 0.7, "use_cls": True}
    backend.recognize(np.zeros((8, 8, 3), np.uint8), {"textline_orientation": False})
    assert factory.engine.calls[-1]["use_cls"] is False
