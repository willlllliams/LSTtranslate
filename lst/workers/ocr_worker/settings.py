"""OCR 进程的设置（Configure）与单次请求的选项（OcrRequest.options）。

Configure 的设置项由主进程的 lst/services/ocr/config.py 的 worker_settings 给出：[ocr] 节的通用字段，
加上当前引擎
流水线配置节（[ocr_paddle]）的全部字段，键名就是配置模型的字段名。PaddleOCR 后端用到的键：

| 键 | 默认值 | 含义 |
|---|---|---|
| mode | "structure" | 识别模式：structure（PP-StructureV3）或 text（只做文本检测与识别） |
| device | "auto" | auto、cpu 或 gpu；auto 在 PaddlePaddle 带 CUDA 且有 GPU 时用 GPU |
| language | "auto" | 识别语言，决定识别模型（languages.py） |
| table、formula、chart、region | false、false、false、true | 预加载的子能力；只对版面模式有意义 |
| doc_orientation、doc_unwarping、textline_orientation | false | 预加载的子能力 |
| grayscale | false | 请求没有给出时的默认预处理：灰度化 |
| resize_max_side | 0 | 请求没有给出时的默认预处理：长边上限，0 表示不缩放 |
| model_cache_dir | 见下 | PaddleX 模型缓存目录（PADDLE_PDX_CACHE_HOME），模型在其中的 official_models 下 |
| detection_model（旧名 det_model） | "PP-OCRv5_mobile_det" | 文本检测模型 |
| recognition_model | "PP-OCRv5_mobile_rec" | 选用的识别模型；语言不是中英日时见 languages.py |
| rec_model | 无 | 显式给出识别模型时不再按语言选择（调试用） |
| layout_model、region_model | "PP-DocLayout_plus-L"、"PP-DocBlockLayout" | 版面检测与区域检测模型 |
| table_model | "SLANet_plus" | 无线表格的结构识别模型 |
| formula_model、chart_model | "PP-FormulaNet_plus-L"、"PP-Chart2Table" | 公式与图表识别模型 |
| *_orientation_model、doc_unwarping_model | 见 DEFAULT_MODELS | 方向分类与去畸变模型 |
| cpu_threads | 0 | CPU 推理线程数，0 表示自动 |
| enable_mkldnn | true | CPU 上对能运行的模型启用 oneDNN（ONEDNN_MODELS），false 时全部用普通方式 |
| warmup | true | 加载阶段用一张合成小图推理一次，把首次推理的开销放在 Ready 之前 |
| engine | 无 | OCR 引擎（worker_settings 给出）；本后端是 paddle，取其他值时记 WARNING 后照常加载 |
| text_det_*、text_rec_score_thresh、layout_* | 见 PREDICT_DEFAULTS | 产线 predict 的参数，见下 |

model_cache_dir 由组合根经 Configure 给出（lst/app/bootstrap.py 的 ocr_process_settings，架构文档 8.5 节）。
没有给出时取环境变量 PADDLE_PDX_CACHE_HOME（单独运行 OCR 进程调试时用），环境变量也没有时为工作目录下的
data/models/ocr。

子能力开关、预处理参数与产线参数以每个请求的 options 为准（键名同上），Configure 里的同名项只决定预加载
哪些模型、以及请求缺少该项时的默认值。请求用到没有预加载的子能力时，后端在这次请求里补加载。
mode、language 在请求里也会出现，但切换它们要重启进程（主进程负责重载），请求里的值与当前设置不同时
只记 WARNING。

产线参数对应 PP-StructureV3 与 OCR 产线 predict 的同名参数（PREDICT_DEFAULTS），只有与默认值不同的才传给
predict，等于默认值时产线按它自己的配置运行（例如文本检测的输入尺寸，版面模式是 736，文本模式是 64）。
text_det_limit_side_len、layout_threshold 为 0，text_det_limit_type、layout_merge_bboxes_mode 为 "default"
表示用产线的默认值。layout_* 只在版面模式下传。
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from lst.contracts.worker_protocol import JsonValue
from lst.workers._runtime import ConfigError
from lst.workers.ocr_worker.languages import UnsupportedLanguage, recognition_model

log = logging.getLogger(__name__)

type Mode = Literal["structure", "text"]
type Device = Literal["auto", "cpu", "gpu"]

FEATURES: Final = (
    "table",
    "formula",
    "chart",
    "region",
    "doc_orientation",
    "doc_unwarping",
    "textline_orientation",
)
"""子能力（需求 OCR-03）。"""

STRUCTURE_ONLY_FEATURES: Final = frozenset({"table", "formula", "chart", "region"})
"""只在版面模式下有意义的子能力；文本模式忽略它们。"""

DEFAULT_FEATURES: Final = frozenset({"region"})
"""预加载的子能力默认值：版面区域检测开，表格识别关（重构前 PP-StructureV3 的取值，见架构文档 8.4 节）。"""
DEFAULT_LAYOUT_MODEL: Final = "PP-DocLayout_plus-L"
DEFAULT_DET_MODEL: Final = "PP-OCRv5_mobile_det"
DEFAULT_REC_MODEL: Final = "PP-OCRv5_mobile_rec"
DEFAULT_MODEL_CACHE_DIR: Final = Path("data") / "models" / "ocr"

DEFAULT_MODELS: Final[dict[str, str]] = {
    "layout_model": DEFAULT_LAYOUT_MODEL,
    "region_model": "PP-DocBlockLayout",
    "table_model": "SLANet_plus",
    "formula_model": "PP-FormulaNet_plus-L",
    "chart_model": "PP-Chart2Table",
    "textline_orientation_model": "PP-LCNet_x1_0_textline_ori",
    "doc_orientation_model": "PP-LCNet_x1_0_doc_ori",
    "doc_unwarping_model": "UVDoc",
}
"""检测与识别之外各环节的默认模型，与 lst/services/ocr/config.py 的 PADDLE_DEFAULT_MODELS 相同。"""

PREDICT_DEFAULTS: Final[dict[str, bool | int | float | str]] = {
    "text_det_limit_side_len": 0,
    "text_det_limit_type": "default",
    "text_det_thresh": 0.3,
    "text_det_box_thresh": 0.6,
    "text_det_unclip_ratio": 1.5,
    "text_rec_score_thresh": 0.0,
    "layout_threshold": 0.0,
    "layout_nms": True,
    "layout_unclip_ratio": 1.0,
    "layout_merge_bboxes_mode": "default",
}
"""产线参数的默认值（与 [ocr_paddle] 节的默认值相同，等于 PaddleX 默认配置里的取值或“用产线默认值”）。"""

STRUCTURE_ONLY_PARAMS: Final = frozenset(name for name in PREDICT_DEFAULTS if name.startswith("layout_"))

_CHOICES: Final[dict[str, frozenset[str]]] = {
    "text_det_limit_type": frozenset({"default", "min", "max"}),
    "layout_merge_bboxes_mode": frozenset({"default", "large", "small", "union"}),
}
_RANGES: Final[dict[str, tuple[float, float]]] = {
    "text_det_limit_side_len": (0, 4096),
    "text_det_thresh": (0.0, 1.0),
    "text_det_box_thresh": (0.0, 1.0),
    "text_det_unclip_ratio": (1.0, 4.0),
    "text_rec_score_thresh": (0.0, 1.0),
    "layout_threshold": (0.0, 1.0),
    "layout_unclip_ratio": (0.5, 3.0),
}

BASE_KEYS: Final = frozenset(
    {"mode", "device", "language", *FEATURES, "grayscale", "resize_max_side", "warmup", "engine"}
)
"""各后端共有的键（[ocr] 节给出的部分）。"""

KNOWN_KEYS: Final = frozenset(
    {*BASE_KEYS, "model_cache_dir", "det_model", "rec_model", "detection_model", "recognition_model",
     *DEFAULT_MODELS, "cpu_threads", "enable_mkldnn", *PREDICT_DEFAULTS}
)  # fmt: skip
"""PaddleOCR 后端 Configure 设置的全部键（上表）。Windows OCR 后端用同一份清单判断未知的键。"""


DEFAULT_CPU_THREADS: Final = 8
"""cpu_threads 为 0（自动）时的线程数，取值依据见 resolve_cpu_threads。"""


def resolve_cpu_threads(requested: int) -> int:
    """CPU 推理线程数。0 表示自动，取 DEFAULT_CPU_THREADS 与逻辑处理器数中较小的一个。

    本机（Intel Core Ultra 7 270K Plus，8 个性能核 + 16 个能效核）实测：
    - PaddleOCR 1920×1080 文本模式：4、8、12、16 线程的耗时差别在测量波动之内，8 线程与 PaddleOCR 的默认值 10
      相当，并给其他进程留出处理器；
    - RapidOCR（onnxruntime）1920×1080、20 行：onnxruntime 默认的线程数（-1，按物理核数）每次约 1.7 秒，
      4 线程约 0.9 秒，8 线程约 0.6 秒，12 线程约 0.6 秒。默认线程数把能效核也算进去，反而最慢。
    """
    if requested > 0:
        return requested
    return max(1, min(DEFAULT_CPU_THREADS, os.cpu_count() or 1))


class BadOptions(ValueError):
    """请求的 options 无效。处理器回复 RequestFailed(engine.bad_request)。"""


# ---------------------------------------------------------------- 取值校验


def _value_error(error: type[Exception], key: str, expected: str, value: JsonValue) -> Exception:
    return error(f"{key} 应为{expected}，实际为 {type(value).__name__}")


def get_bool(raw: Mapping[str, JsonValue], key: str, default: bool, error: type[Exception]) -> bool:
    """取布尔值；键缺失时取 default，类型不对时抛 error（设置用 ConfigError，请求选项用 BadOptions）。"""
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise _value_error(error, key, "布尔值", value)
    return value


def get_int(raw: Mapping[str, JsonValue], key: str, default: int, error: type[Exception]) -> int:
    """取非负整数；键缺失时取 default，类型不对或为负时抛 error。"""
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _value_error(error, key, "整数", value)
    if value < 0:
        raise error(f"{key} 不能为负，实际为 {value}")
    return value


def _get_str(raw: Mapping[str, JsonValue], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} 应为非空字符串")
    return value


def get_float(
    raw: Mapping[str, JsonValue], key: str, default: float, low: float, high: float, error: type[Exception]
) -> float:
    """取 [low, high] 内的数；键缺失时取 default，类型不对或越界时抛 error。"""
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _value_error(error, key, "数", value)
    if not low <= float(value) <= high:
        raise error(f"{key} 应在 {low:g} 至 {high:g} 之间，实际为 {value}")
    return float(value)


type PredictParams = dict[str, bool | int | float | str]


def predict_params(
    raw: Mapping[str, JsonValue], defaults: Mapping[str, bool | int | float | str], error: type[Exception]
) -> PredictParams:
    """按 raw 覆盖 defaults，得到全部产线参数（PREDICT_DEFAULTS 的键）；取值无效时抛 error。"""
    result: PredictParams = {}
    for name, fallback in PREDICT_DEFAULTS.items():
        default = defaults.get(name, fallback)
        if name in _CHOICES:
            value = raw.get(name, default)
            if not isinstance(value, str) or value not in _CHOICES[name]:
                raise error(f"{name} 应为 {'、'.join(sorted(_CHOICES[name]))} 之一，实际为 {value!r}")
            result[name] = value
        elif isinstance(fallback, bool):
            result[name] = get_bool(raw, name, bool(default), error)
        elif name == "text_det_limit_side_len":
            value = get_int(raw, name, int(default), error)
            if value > _RANGES[name][1]:
                raise error(f"{name} 不能大于 {_RANGES[name][1]:g}，实际为 {value}")
            result[name] = value
        else:
            low, high = _RANGES[name]
            result[name] = get_float(raw, name, float(default), low, high, error)
    return result


def predict_kwargs(params: Mapping[str, bool | int | float | str], structure: bool) -> PredictParams:
    """传给产线 predict 的参数：只含与默认值不同的项，见模块说明。"""
    return {
        name: value
        for name, value in params.items()
        if value != PREDICT_DEFAULTS[name] and (structure or name not in STRUCTURE_ONLY_PARAMS)
    }


@dataclass(frozen=True, slots=True)
class OcrSettings:
    """Configure 的设置，见模块说明的表格。features 是预加载的子能力（文本模式已去掉版面专用的几项）。

    models 是检测、识别之外各环节的模型（DEFAULT_MODELS 的键）；predict 是产线参数的默认值。
    """

    mode: Mode
    device: Device
    language: str
    features: frozenset[str]
    grayscale: bool
    resize_max_side: int
    model_cache_dir: Path
    layout_model: str
    det_model: str
    rec_model: str
    cpu_threads: int
    warmup: bool
    enable_mkldnn: bool = True
    models: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    predict: PredictParams = field(default_factory=lambda: dict(PREDICT_DEFAULTS))

    def model(self, key: str) -> str:
        """检测、识别之外某个环节的模型名（DEFAULT_MODELS 的键）。"""
        return self.models.get(key, DEFAULT_MODELS[key])

    @classmethod
    def parse(cls, raw: Mapping[str, JsonValue]) -> "OcrSettings":
        """校验 Configure 的设置；无效时抛 ConfigError。未知的键记警告后忽略。"""
        unknown = sorted(set(raw) - KNOWN_KEYS)
        if unknown:
            log.warning("忽略未知的设置项：%s", ", ".join(unknown))
        mode: Mode
        match raw.get("mode", "structure"):
            case "structure":
                mode = "structure"
            case "text":
                mode = "text"
            case other:
                raise ConfigError(f"mode 应为 structure 或 text，实际为 {other!r}")
        device: Device
        match raw.get("device", "auto"):
            case "auto":
                device = "auto"
            case "cpu":
                device = "cpu"
            case "gpu":
                device = "gpu"
            case other:
                raise ConfigError(f"device 应为 auto、cpu 或 gpu，实际为 {other!r}")
        engine = raw.get("engine")
        if engine is not None and engine != "paddle":
            log.warning("engine 为 %r，但本进程使用 PaddleOCR 后端；按 PaddleOCR 的设置加载", engine)
        language = _get_str(raw, "language", "auto")
        features = frozenset(
            name for name in FEATURES if get_bool(raw, name, name in DEFAULT_FEATURES, ConfigError)
        )
        if mode == "text":
            features -= STRUCTURE_ONLY_FEATURES
        general = _get_str(raw, "recognition_model", DEFAULT_REC_MODEL)
        rec_model = raw.get("rec_model")
        if rec_model is None:
            try:
                rec_model = recognition_model(language, general)
            except UnsupportedLanguage as exc:
                raise ConfigError(str(exc)) from exc
        elif not isinstance(rec_model, str) or not rec_model:
            raise ConfigError("rec_model 应为非空字符串")
        det_model = _get_str(raw, "detection_model", _get_str(raw, "det_model", DEFAULT_DET_MODEL))
        return cls(
            mode=mode,
            device=device,
            language=language,
            features=features,
            grayscale=get_bool(raw, "grayscale", False, ConfigError),
            resize_max_side=get_int(raw, "resize_max_side", 0, ConfigError),
            model_cache_dir=_model_cache_dir(raw),
            layout_model=_get_str(raw, "layout_model", DEFAULT_LAYOUT_MODEL),
            det_model=det_model,
            rec_model=rec_model,
            cpu_threads=get_int(raw, "cpu_threads", 0, ConfigError),
            warmup=get_bool(raw, "warmup", True, ConfigError),
            enable_mkldnn=get_bool(raw, "enable_mkldnn", True, ConfigError),
            models={key: _get_str(raw, key, default) for key, default in DEFAULT_MODELS.items()},
            predict=predict_params(raw, PREDICT_DEFAULTS, ConfigError),
        )


def _model_cache_dir(raw: Mapping[str, JsonValue]) -> Path:
    value = raw.get("model_cache_dir")
    if value is not None and (not isinstance(value, str) or not value):
        raise ConfigError("model_cache_dir 应为非空字符串")
    chosen = value or os.environ.get("PADDLE_PDX_CACHE_HOME") or str(DEFAULT_MODEL_CACHE_DIR)
    return Path(chosen).absolute()


@dataclass(frozen=True, slots=True)
class RequestOptions:
    """一次请求实际生效的选项：子能力集合与预处理参数。"""

    features: frozenset[str]
    grayscale: bool
    resize_max_side: int
    predict: PredictParams = field(default_factory=lambda: dict(PREDICT_DEFAULTS))

    @classmethod
    def parse(cls, raw: Mapping[str, JsonValue], settings: OcrSettings) -> "RequestOptions":
        """按请求的 options 覆盖设置里的默认值；取值类型不对时抛 BadOptions。

        文本模式忽略版面专用的子能力；mode、language 与当前设置不同时只记 WARNING（见模块说明）。
        """
        for key, current in (("mode", settings.mode), ("language", settings.language)):
            requested = raw.get(key)
            if requested is not None and requested != current:
                log.warning("请求的 %s 为 %r，与进程当前的 %r 不同；切换需要重载进程，本次按 %r 处理",
                            key, requested, current, current)  # fmt: skip
        features = frozenset(
            name
            for name in FEATURES
            if get_bool(raw, name, name in settings.features, BadOptions)
            and not (settings.mode == "text" and name in STRUCTURE_ONLY_FEATURES)
        )
        return cls(
            features=features,
            grayscale=get_bool(raw, "grayscale", settings.grayscale, BadOptions),
            resize_max_side=get_int(raw, "resize_max_side", settings.resize_max_side, BadOptions),
            predict=predict_params(raw, settings.predict, BadOptions),
        )
