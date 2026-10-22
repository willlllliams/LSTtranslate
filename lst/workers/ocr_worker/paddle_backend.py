"""PaddleOCR 后端（架构文档 2.4、8.4 节）。

版面模式用 PP-StructureV3，文本模式用 PaddleOCR 的文本检测 + 识别产线（OCR 产线）。

推理库（paddle、paddleocr、paddlex、cv2）只在 load 时导入，本模块在没有 Paddle 的主环境里也能导入。

加载流程（load）：

1. 校验设置（settings.OcrSettings）。
2. 设置环境变量，必须在导入 paddlex 之前：PADDLE_PDX_CACHE_HOME 指向模型缓存目录；
   PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True 关闭 PaddleX 启动时对模型站点的连通性检查（架构文档第 10 章）；
   GLOG_minloglevel=2 让 Paddle 的 C++ 日志只输出错误（主进程把标准错误的每一行记为 WARNING）。
3. 导入推理库。失败时 ImportError、OSError 原样抛出，进程以 DEPENDENCY_ERROR 退出。
4. 选择设备，见 _resolve_device。
5. 按 paddle_config 构造配置并创建产线。每个模型都显式给出本地目录，缺模型时报 ConfigError，不会下载。
6. 预热：用一张合成小图推理一次。CPU 上第一次推理要初始化 oneDNN 的算子和内存，比后续慢得多，
   放在 Ready 之前完成，第一个真实请求就不会承担这部分开销（监管器等待 Ready 不计处理时限）。

子能力：Configure 里开着的子能力在加载时创建；请求的 options 以请求为准，用到还没创建的子能力时，
在这次请求里带上它重新创建产线（之前创建过的子能力一并保留），之后的请求直接使用。

图表识别（PP-Chart2Table）只在 GPU 上提供。本机 CPU（Intel Core Ultra 7 270K Plus）实测，含一个柱状图的
1280×1000 页面单次推理 560–600 秒，输出是乱码，远超监管器的处理时限，开着只会让进程反复被判定卡死。
所以设备是 CPU 时这项子能力不可用：Ready 的能力清单里标为不可用，请求里开着也忽略（记一次 WARNING）。

表格方向分类：PaddleX 默认对每个表格做方向分类（PP-LCNet_x1_0_doc_ori），实测会把正放的两行三列表格判成
旋转 180 度，pred_html 的行列整个倒过来。截图里的表格几乎总是正放的，所以只有开了文档方向校正时才做表格
方向分类。
"""

import importlib
import logging
import os
import time
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast

import numpy as np

from lst.contracts.worker_protocol import JsonValue
from lst.workers._runtime import LoadResult, ProgressReporter
from lst.workers.ocr_worker.blocks import blocks_to_json
from lst.workers.ocr_worker.convert import convert_structure, convert_text
from lst.workers.ocr_worker.extract import extract_structure, extract_text
from lst.workers.ocr_worker.geometry import CoordinateMap, plan_resize
from lst.workers.ocr_worker.image import Pixels, to_grayscale
from lst.workers.ocr_worker.paddle_config import Config, attach_models, structure_config, text_config
from lst.workers.ocr_worker.settings import (
    FEATURES,
    OcrSettings,
    PredictParams,
    RequestOptions,
    predict_kwargs,
    resolve_cpu_threads,
)

log = logging.getLogger(__name__)

GPU_ONLY_FEATURES: Final = frozenset({"chart"})
"""只在 GPU 上提供的子能力，见模块说明。"""

# ---------------------------------------------------------------- 推理库的类型（只列出用到的部分）


class _Pipeline(Protocol):
    def predict(self, input: Pixels, **kwargs: object) -> list[object]: ...

    def close(self) -> None: ...


class _PaddleOcrModule(Protocol):
    __version__: str

    def PPStructureV3(self, **kwargs: object) -> _Pipeline: ...

    def PaddleOCR(self, **kwargs: object) -> _Pipeline: ...


class _CudaModule(Protocol):
    def device_count(self) -> int: ...


class _DeviceModule(Protocol):
    @property
    def cuda(self) -> _CudaModule: ...


class _PaddleModule(Protocol):
    __version__: str

    def is_compiled_with_cuda(self) -> bool: ...

    @property
    def device(self) -> _DeviceModule: ...


class _PaddlexModule(Protocol):
    __version__: str


class _PaddlexInference(Protocol):
    def load_pipeline_config(self, pipeline: str) -> Config: ...


class _Cv2Module(Protocol):
    INTER_AREA: int
    FONT_HERSHEY_SIMPLEX: int
    LINE_AA: int

    def resize(self, src: Pixels, dsize: tuple[int, int], *, interpolation: int) -> Pixels: ...

    def putText(
        self,
        img: Pixels,
        text: str,
        org: tuple[int, int],
        fontFace: int,
        fontScale: float,
        color: tuple[int, int, int],
        thickness: int,
        lineType: int,
    ) -> Pixels: ...

    def rectangle(
        self,
        img: Pixels,
        pt1: tuple[int, int],
        pt2: tuple[int, int],
        color: tuple[int, int, int],
        thickness: int,
    ) -> Pixels: ...


@dataclass(frozen=True, slots=True)
class _Modules:
    paddle: _PaddleModule
    paddleocr: _PaddleOcrModule
    paddlex: _PaddlexModule
    inference: _PaddlexInference
    cv2: _Cv2Module


# ---------------------------------------------------------------- 环境与设备


def prepare_environment(model_cache_dir: Path) -> None:
    """导入 paddlex 之前要设置的环境变量，见模块说明第 2 步。"""
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(model_cache_dir)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ.setdefault("GLOG_minloglevel", "2")


def _import_modules() -> _Modules:
    """导入推理库。paddle 导入时检查 ccache 的提示与本程序无关，屏蔽掉。"""
    warnings.filterwarnings("ignore", message="No ccache found")
    paddle = cast(_PaddleModule, importlib.import_module("paddle"))
    paddleocr = cast(_PaddleOcrModule, importlib.import_module("paddleocr"))
    paddlex = cast(_PaddlexModule, importlib.import_module("paddlex"))
    inference = cast(_PaddlexInference, importlib.import_module("paddlex.inference"))
    cv2 = cast(_Cv2Module, importlib.import_module("cv2"))
    # PaddleX 关掉了自己 logger 的向上传递；改回来，它的警告按本进程的日志格式输出，INFO 不输出。
    paddlex_logger = logging.getLogger("paddlex")
    paddlex_logger.propagate = True
    paddlex_logger.setLevel(logging.WARNING)
    return _Modules(paddle, paddleocr, paddlex, inference, cv2)


def resolve_device(compiled_with_cuda: bool, gpu_count: int, requested: str) -> str:
    """选择推理设备，返回 "cpu" 或 "gpu"。

    - cpu：总是 CPU。
    - gpu：安装的 PaddlePaddle 没有编译 CUDA（CPU 版）时抛 ImportError，进程以 DEPENDENCY_ERROR 退出：
      缺的是推理依赖的能力，要换装 CUDA 版运行环境（或把 device 改成 auto、cpu）；
      编译了 CUDA 但 CUDA 不可用（没有 GPU、驱动太旧）时同样抛 ImportError：运行时缺的是推理依赖的能力，
      设置窗口显示依赖错误与处理建议（需求 OCRE-07）。
    - auto：有 CUDA 且有 GPU 时用 GPU，否则 CPU。
    """
    if requested == "cpu":
        return "cpu"
    if requested == "gpu":
        if not compiled_with_cuda:
            raise ImportError(
                "推理设备设为显卡，但 PaddleOCR 运行环境是 CPU 版（PaddlePaddle 未编译 CUDA）；"
                "请在设置里安装 PaddleOCR 的 CUDA 版，或把推理设备改为自动或 CPU"
            )
        if gpu_count <= 0:
            raise ImportError(
                "推理设备设为显卡，PaddleOCR 是 CUDA 版，但 CUDA 不可用"
                "（没有 NVIDIA 显卡，或驱动不支持 CUDA 12.9）；请更新显卡驱动，或把推理设备改为自动或 CPU"
            )
        return "gpu"
    return "gpu" if compiled_with_cuda and gpu_count > 0 else "cpu"


def warmup_image(cv2: _Cv2Module) -> Pixels:
    """预热用的合成图：几行英文和一个两行三列的表格，覆盖检测、识别、版面和表格模型。"""
    image = np.full((360, 640, 3), 255, dtype=np.uint8)
    for index, line in enumerate(("LSTtranslate warm up", "Screen translation 2026", "Hello OCR 12345")):
        cv2.putText(
            image, line, (24, 48 + index * 44), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2, cv2.LINE_AA
        )
    for row in range(2):
        for col in range(3):
            top_left = (24 + col * 190, 200 + row * 64)
            cv2.rectangle(image, top_left, (top_left[0] + 190, top_left[1] + 64), (0, 0, 0), 2)
            text = ("Name", "Count", "Price")[col] if row == 0 else ("Apple", "12", "3.50")[col]
            org = (top_left[0] + 16, top_left[1] + 42)
            cv2.putText(image, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2, cv2.LINE_AA)
    return image


# ---------------------------------------------------------------- 后端


class PaddleBackend:
    """PaddleOCR 后端，实现 backend.OcrBackend。"""

    def __init__(self) -> None:
        self._modules: _Modules | None = None
        self._imported_cache_dir: Path | None = None
        self._settings: OcrSettings | None = None
        self._device = "cpu"
        self._pipeline: _Pipeline | None = None
        self._loaded: frozenset[str] = frozenset()
        self._models: list[str] = []
        self._warned_unavailable: set[str] = set()

    # ---- 加载

    def load(self, settings: Mapping[str, JsonValue], progress: ProgressReporter) -> LoadResult:
        parsed = OcrSettings.parse(settings)
        modules = self._import(parsed.model_cache_dir, progress)
        device = resolve_device(
            modules.paddle.is_compiled_with_cuda(),
            modules.paddle.device.cuda.device_count() if modules.paddle.is_compiled_with_cuda() else 0,
            parsed.device,
        )
        self._close_pipeline()
        self._settings, self._device = parsed, device
        self._warned_unavailable.clear()
        progress("model_load", parsed.mode)
        started = time.perf_counter()
        self._build(self._usable(parsed.features))
        log.info(
            "%s 产线已创建：设备 %s，子能力 %s，模型 %s，耗时 %.1f 秒",
            "PP-StructureV3" if parsed.mode == "structure" else "OCR",
            device,
            ", ".join(sorted(self._loaded)) or "无",
            ", ".join(self._models),
            time.perf_counter() - started,
        )
        if parsed.warmup:
            progress("warmup")
            self._warm_up(modules)
        return LoadResult(self._capabilities(), self._versions(modules))

    def _import(self, cache_dir: Path, progress: ProgressReporter) -> _Modules:
        if self._modules is None:
            prepare_environment(cache_dir)
            progress("import")
            started = time.perf_counter()
            self._modules = _import_modules()
            self._imported_cache_dir = cache_dir
            log.info("推理库导入完成，耗时 %.1f 秒", time.perf_counter() - started)
        elif cache_dir != self._imported_cache_dir:
            # PaddleX 在导入时读取 PADDLE_PDX_CACHE_HOME；模型目录是显式给出的，不受影响，
            # 只有 PaddleX 自己的锁文件等仍写在旧目录。
            log.warning("模型缓存目录在进程运行中改变，PaddleX 的运行文件仍写在 %s", self._imported_cache_dir)
        return self._modules

    def _usable(self, features: frozenset[str]) -> frozenset[str]:
        """去掉当前设备上不可用的子能力，每项只警告一次。"""
        if self._device != "cpu":
            return features
        dropped = features & GPU_ONLY_FEATURES
        for name in dropped - self._warned_unavailable:
            self._warned_unavailable.add(name)
            log.warning("子能力 %s 只在 GPU 上提供，CPU 上已忽略", name)
        return features - dropped

    def _build(self, features: frozenset[str]) -> None:
        """按 features 创建产线，替换当前产线。"""
        settings, modules = self._settings, self._modules
        assert settings is not None and modules is not None
        structure = settings.mode == "structure"
        base = modules.inference.load_pipeline_config("PP-StructureV3" if structure else "OCR")
        config = (structure_config if structure else text_config)(base, settings, features)
        threads = resolve_cpu_threads(settings.cpu_threads) if self._device == "cpu" else None
        models_dir = settings.model_cache_dir / "official_models"
        models = attach_models(config, models_dir, threads, settings.enable_mkldnn)
        kwargs: dict[str, object] = {
            "paddlex_config": config,
            "device": "gpu:0" if self._device == "gpu" else "cpu",
            # PaddleOCR 的包装类在检测、识别模型名都为空时会换成它自己的默认模型，这里显式给出。
            "text_detection_model_name": settings.det_model,
            "text_detection_model_dir": str(models_dir / settings.det_model),
            "text_recognition_model_name": settings.rec_model,
            "text_recognition_model_dir": str(models_dir / settings.rec_model),
        }
        if threads is not None:
            kwargs.update(enable_mkldnn=False, cpu_threads=threads)
        self._close_pipeline()
        factory = modules.paddleocr.PPStructureV3 if structure else modules.paddleocr.PaddleOCR
        self._pipeline = factory(**kwargs)
        self._loaded = features
        self._models = models

    def _close_pipeline(self) -> None:
        if self._pipeline is not None:
            try:
                self._pipeline.close()
            except Exception as exc:
                log.warning("关闭旧产线时出错：%s: %s", type(exc).__name__, exc)
            self._pipeline = None
            self._loaded = frozenset()

    def _warm_up(self, modules: _Modules) -> None:
        started = time.perf_counter()
        try:
            image = warmup_image(modules.cv2)
            self._predict(image, self._loaded)
        except Exception as exc:
            log.warning("预热失败，照常就绪：%s: %s", type(exc).__name__, exc)
            return
        log.info("预热完成，耗时 %.1f 秒", time.perf_counter() - started)

    def _capabilities(self) -> dict[str, JsonValue]:
        settings = self._settings
        assert settings is not None
        features: dict[str, JsonValue] = {name: name in self._loaded for name in FEATURES}
        unavailable: dict[str, JsonValue] = {}
        if settings.mode == "structure" and self._device == "cpu":
            unavailable = {name: "cpu" for name in sorted(GPU_ONLY_FEATURES)}
        models: dict[str, JsonValue] = {"det": settings.det_model, "rec": settings.rec_model}
        if settings.mode == "structure":
            models["layout"] = settings.layout_model
            models["region"] = settings.model("region_model")
        models["all"] = list[JsonValue](self._models)
        return {
            "mode": settings.mode,
            "language": settings.language,
            "device": self._device,
            "features": features,
            "unavailable": unavailable,
            "models": models,
            "preprocess": ["grayscale", "resize_max_side"],
        }

    def _versions(self, modules: _Modules) -> dict[str, str]:
        return {
            "paddle": modules.paddle.__version__,
            "paddleocr": modules.paddleocr.__version__,
            "paddlex": modules.paddlex.__version__,
            "device": self._device,
        }

    # ---- 识别

    def recognize(self, image: Pixels, options: Mapping[str, JsonValue]) -> list[JsonValue]:
        settings, modules = self._settings, self._modules
        if settings is None or modules is None:
            raise RuntimeError("后端尚未加载")
        parsed = RequestOptions.parse(options, settings)
        features = self._usable(parsed.features)
        if self._pipeline is None or not features <= self._loaded:
            # 上次补加载失败时产线已经关闭，这里连同之前的子能力一起重建。
            added = sorted(features - self._loaded)
            log.info("请求用到未加载的子能力 %s，重新创建产线", ", ".join(added) or "（重建）")
            self._build(self._loaded | features)
        height, width = image.shape[:2]
        target = plan_resize(width, height, parsed.resize_max_side)
        prepared = image
        if target != (width, height):
            prepared = modules.cv2.resize(prepared, target, interpolation=modules.cv2.INTER_AREA)
        # 先缩放再灰度化：灰度化只处理缩小后的像素，与 Windows OCR 后端的顺序相同。
        if parsed.grayscale:
            prepared = to_grayscale(prepared)
        result = self._predict(prepared, features, parsed.predict)
        if settings.mode == "structure":
            page = extract_structure(result)
            blocks = convert_structure(
                page, CoordinateMap((width, height), target, page.angle, page.output_size)
            )
        else:
            page = extract_text(result, target)
            blocks = convert_text(page, CoordinateMap((width, height), target, page.angle, page.output_size))
        return blocks_to_json(blocks)

    def _predict(
        self, image: Pixels, features: frozenset[str], params: PredictParams | None = None
    ) -> object:
        """推理一张图，返回 PaddleX 的结果对象。params 是请求的产线参数，只传与默认值不同的项。"""
        settings, pipeline = self._settings, self._pipeline
        assert settings is not None and pipeline is not None
        orientation = "doc_orientation" in features
        flags: dict[str, object] = {
            "use_doc_orientation_classify": orientation,
            "use_doc_unwarping": "doc_unwarping" in features,
            "use_textline_orientation": "textline_orientation" in features,
        }
        if settings.mode == "structure":
            flags.update(
                use_table_recognition="table" in features,
                use_formula_recognition="formula" in features,
                use_chart_recognition="chart" in features,
                use_seal_recognition=False,
                # 与建产线时一致：关掉区域检测时产线里没有这个模型，这里再传 True 会在预测时出错。
                use_region_detection="region" in features,
                use_table_orientation_classify=orientation,
            )
        flags.update(predict_kwargs(params or settings.predict, settings.mode == "structure"))
        results = pipeline.predict(image, **flags)
        if len(results) != 1:
            raise RuntimeError(f"产线返回了 {len(results)} 个结果，应为 1 个")
        result = results[0]
        error = _error_of(result)
        if error is not None:
            raise RuntimeError(f"产线拒绝了本次参数：{error}")
        return result


def _error_of(result: object) -> object | None:
    """PaddleX 的参数校验不通过时，产线产出 {"error": ...} 而不是结果对象。"""
    if isinstance(result, dict):
        return cast(dict[str, object], result).get("error")
    return None
