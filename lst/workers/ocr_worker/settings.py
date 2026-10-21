"""OCR 进程的设置（Configure）与单次请求的选项（OcrRequest.options）。

Configure 的设置项（主进程的 lst/services/ocr/config.py 的 worker_settings 给出前 11 项）：

| 键 | 默认值 | 含义 |
|---|---|---|
| mode | "structure" | 识别模式：structure（PP-StructureV3）或 text（只做文本检测与识别） |
| device | "auto" | auto、cpu 或 gpu；auto 在 PaddlePaddle 带 CUDA 且有 GPU 时用 GPU |
| language | "auto" | 识别语言，决定识别模型（languages.py） |
| table、formula、chart | true、false、false | 预加载的子能力；只对版面模式有意义 |
| doc_orientation、doc_unwarping、textline_orientation | false | 预加载的子能力 |
| grayscale | false | 请求没有给出时的默认预处理：灰度化 |
| resize_max_side | 0 | 请求没有给出时的默认预处理：长边上限，0 表示不缩放 |
| model_cache_dir | 见下 | PaddleX 模型缓存目录（PADDLE_PDX_CACHE_HOME），模型在其中的 official_models 下 |
| layout_model | "PP-DocLayout_plus-L" | 版面检测模型 |
| det_model | "PP-OCRv5_mobile_det" | 文本检测模型 |
| rec_model | 按 language 选择 | 识别模型；显式给出时不再按语言选择 |
| cpu_threads | 0 | CPU 推理线程数，0 表示自动 |
| warmup | true | 加载阶段用一张合成小图推理一次，把首次推理的开销放在 Ready 之前 |

model_cache_dir 不设时取环境变量 PADDLE_PDX_CACHE_HOME（监管器启动 OCR 进程时设置，架构文档 8.5 节），
环境变量也没有时为工作目录下的 data/models/ocr。

子能力开关与预处理参数以每个请求的 options 为准（键名同上），Configure 里的同名项只决定预加载哪些模型、
以及请求缺少该项时的默认值。请求用到没有预加载的子能力时，后端在这次请求里补加载。
mode、language 在请求里也会出现，但切换它们要重启进程（主进程负责重载），请求里的值与当前设置不同时
只记 WARNING。
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from lst.contracts.worker_protocol import JsonValue
from lst.workers._runtime import ConfigError
from lst.workers.ocr_worker.languages import UnsupportedLanguage, recognition_model

log = logging.getLogger(__name__)

type Mode = Literal["structure", "text"]
type Device = Literal["auto", "cpu", "gpu"]

FEATURES: Final = ("table", "formula", "chart", "doc_orientation", "doc_unwarping", "textline_orientation")
"""子能力（需求 OCR-03）。"""

STRUCTURE_ONLY_FEATURES: Final = frozenset({"table", "formula", "chart"})
"""只在版面模式下有意义的子能力；文本模式忽略它们。"""

DEFAULT_FEATURES: Final = frozenset({"table"})
DEFAULT_LAYOUT_MODEL: Final = "PP-DocLayout_plus-L"
DEFAULT_DET_MODEL: Final = "PP-OCRv5_mobile_det"
DEFAULT_REC_MODEL: Final = "PP-OCRv5_mobile_rec"
DEFAULT_MODEL_CACHE_DIR: Final = Path("data") / "models" / "ocr"

_KNOWN_KEYS: Final = frozenset(
    {"mode", "device", "language", *FEATURES, "grayscale", "resize_max_side", "model_cache_dir",
     "layout_model", "det_model", "rec_model", "cpu_threads", "warmup"}
)  # fmt: skip


class BadOptions(ValueError):
    """请求的 options 无效。处理器回复 RequestFailed(engine.bad_request)。"""


# ---------------------------------------------------------------- 取值校验


def _value_error(error: type[Exception], key: str, expected: str, value: JsonValue) -> Exception:
    return error(f"{key} 应为{expected}，实际为 {type(value).__name__}")


def _get_bool(raw: Mapping[str, JsonValue], key: str, default: bool, error: type[Exception]) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise _value_error(error, key, "布尔值", value)
    return value


def _get_int(raw: Mapping[str, JsonValue], key: str, default: int, error: type[Exception]) -> int:
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


@dataclass(frozen=True, slots=True)
class OcrSettings:
    """Configure 的设置，见模块说明的表格。features 是预加载的子能力（文本模式已去掉版面专用的几项）。"""

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

    @classmethod
    def parse(cls, raw: Mapping[str, JsonValue]) -> "OcrSettings":
        """校验 Configure 的设置；无效时抛 ConfigError。未知的键记警告后忽略。"""
        unknown = sorted(set(raw) - _KNOWN_KEYS)
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
        language = _get_str(raw, "language", "auto")
        features = frozenset(
            name for name in FEATURES if _get_bool(raw, name, name in DEFAULT_FEATURES, ConfigError)
        )
        if mode == "text":
            features -= STRUCTURE_ONLY_FEATURES
        rec_model = raw.get("rec_model")
        if rec_model is None:
            try:
                rec_model = recognition_model(language, DEFAULT_REC_MODEL)
            except UnsupportedLanguage as exc:
                raise ConfigError(str(exc)) from exc
        elif not isinstance(rec_model, str) or not rec_model:
            raise ConfigError("rec_model 应为非空字符串")
        return cls(
            mode=mode,
            device=device,
            language=language,
            features=features,
            grayscale=_get_bool(raw, "grayscale", False, ConfigError),
            resize_max_side=_get_int(raw, "resize_max_side", 0, ConfigError),
            model_cache_dir=_model_cache_dir(raw),
            layout_model=_get_str(raw, "layout_model", DEFAULT_LAYOUT_MODEL),
            det_model=_get_str(raw, "det_model", DEFAULT_DET_MODEL),
            rec_model=rec_model,
            cpu_threads=_get_int(raw, "cpu_threads", 0, ConfigError),
            warmup=_get_bool(raw, "warmup", True, ConfigError),
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
            if _get_bool(raw, name, name in settings.features, BadOptions)
            and not (settings.mode == "text" and name in STRUCTURE_ONLY_FEATURES)
        )
        return cls(
            features=features,
            grayscale=_get_bool(raw, "grayscale", settings.grayscale, BadOptions),
            resize_max_side=_get_int(raw, "resize_max_side", settings.resize_max_side, BadOptions),
        )
