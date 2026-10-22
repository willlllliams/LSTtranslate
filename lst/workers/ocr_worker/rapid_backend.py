r"""RapidOCR 后端（需求 OCRE-01、OCRE-06、OCRE-07、OCRE-08、OCRE-09、OCR-10，lst/contracts/ocr_engines.py）。

RapidOCR 用 onnxruntime 运行 PP-OCR 的检测与识别模型，只有文本模式。启动命令是
``python -m lst.workers.ocr_worker --backend lst.workers.ocr_worker.rapid_backend:RapidBackend``，
解释器是装有 rapidocr 的环境：安装版经 launcher\worker.py 的 ``--ocr-site <数据目录>\engines\rapid\site``，
源码版是 .venv-ocr。本模块只在 load 时导入 rapidocr（它再导入 onnxruntime、cv2、numpy），
导入链不含 paddle、paddlex；导入失败时进程以 DEPENDENCY_ERROR 退出。

模型
----

rapidocr 3.10 的 wheel 自带默认模型，放在包目录的 models 下（核实于 rapidocr 3.10.0 的 RECORD）：

| 文件 | 用途 | 大小 |
|---|---|---|
| PP-OCRv6_det_small.onnx | 文本检测（多语言） | 9.9 MB |
| PP-OCRv6_rec_small.onnx | 文本识别（多语言，字表在模型的元数据里） | 21.2 MB |
| ch_ppocr_mobile_v2.0_cls_mobile.onnx | 文本行方向分类 | 0.6 MB |

主进程在 Configure 里给出各环节选用的模型文件的完整路径（det_model_path、rec_model_path、cls_model_path，
由 services.ocr_engines 按配置 ``[ocr_rapid].<环节>_model`` 算出）；没有给出时用包目录里的默认模型。
RapidOCR 在 model_path 为空时会按 default_models.yaml 的地址下载模型，这里总是显式给出 model_path，
onnxruntime 直接加载本地文件，不经过下载逻辑；文件缺失时以 ModelMissing（配置错误）退出，列出缺少的文件。
识别模型的字表来自 onnx 元数据（``character``），不读字典文件。
进程入口的网络拦截（network_guard）一直开着，作为兜底。

Configure 的设置
----------------

由 lst/services/ocr/config.py 的 worker_settings 给出（[ocr] 节的通用字段与 [ocr_rapid] 节的全部字段），
组合根再加上模型路径与 recognition_languages。这里用到的键：

| 键 | 默认值 | 含义 |
|---|---|---|
| mode | "text" | 只能是 text；RapidOCR 没有版面分析，给出 structure 按配置错误处理 |
| engine | "rapid" | 给出时只能是 rapid |
| language | "auto" | auto 或规范语言码，见下文“识别语言” |
| device | "auto" | auto、cpu 或 gpu，见下文“推理设备” |
| grayscale、resize_max_side | false、0 | 请求没有给出时的默认预处理 |
| textline_orientation | false | 请求没有给出时是否做文本行方向分类（RapidOCR 的 use_cls） |
| det_model_path、rec_model_path、cls_model_path | 包目录里的默认模型 | 模型文件 |
| recognition_languages | LANGUAGES | 识别模型覆盖的语言 |
| det_thresh 等 | 见 CONSTRUCT_PARAMS | 只能在创建识别器时给出的参数（RapidOCR 的 Det、Cls、Rec、Global） |
| det_box_thresh、det_unclip_ratio、text_score | 0.5、1.6、0.5 | 每次识别给出的参数，见 CALL_PARAMS |
| cpu_threads | 0 | onnxruntime 的算子内线程数，0 表示自动（settings.resolve_cpu_threads） |
| warmup | true | 加载阶段推理一张合成小图，把建立推理会话的开销放在 Ready 之前 |

PaddleOCR 专用的键直接忽略；其余未知的键记 WARNING 后忽略。请求 options 里的 grayscale、resize_max_side、
textline_orientation 与每次识别的参数覆盖上表的默认值，取值无效时回复 engine.bad_request；
mode、language 与当前设置不同时只记 WARNING（切换由主进程重载进程）。

推理设备
--------

运行环境的版本决定 onnxruntime 带哪个加速（OCRE-07）：onnxruntime-gpu 提供 CUDAExecutionProvider，
onnxruntime-directml 提供 DmlExecutionProvider，onnxruntime 只有 CPU。device 的含义：

- cpu：总是用 CPU；
- auto：运行环境带加速就用（CUDA 优先于 DirectML），加速在本机不可用时退回 CPU 并记 WARNING；
- gpu：要求加速。运行环境是 CPU 版，或者加速在本机不可用（驱动太旧、CUDA 运行库加载失败、显卡不支持），
  抛 ImportError，进程以 DEPENDENCY_ERROR 退出，设置窗口显示出错。

用 CUDA 时先调用 ``onnxruntime.preload_dlls()``：onnxruntime-gpu 的 CUDA 13 与 cuDNN 9 运行库装在 site 的
nvidia 目录下，不在 DLL 搜索路径上，需要它按包的位置加载。RapidOCR 在加速不可用时自己退回 CPU，只记一条
警告，所以这里在预热之后读取检测会话实际使用的执行器核对；用了加速时预热总会执行。

识别语言
--------

识别语言不改变加载的模型，只用来核对选用的识别模型是否覆盖它：auto 总是可以；其他语言规范化后要在
recognition_languages（默认 LANGUAGES，即 PP-OCRv6 多语言模型覆盖的语言）里，否则按配置错误处理，
消息列出支持的语言。``rapid_language`` 给出规范语言码对应的 RapidOCR 语言名，用于日志。

识别与坐标
----------

1. 处理器给出 BGR 图像；先按 resize_max_side 等比缩小（image.resize_area，面积平均），再按 grayscale
   灰度化，交给 RapidOCR。RapidOCR 内部还会把长边限制在 max_side_len 以内，它返回的坐标已经换算回它的输入。
2. RapidOCR 的每个结果是一行：四个角点的多边形、文字、置信度。多边形取轴对齐包围盒，经
   geometry.CoordinateMap 换算回原始截图，左上角向下取整、右下角向上取整并裁剪到截图内。
3. 输出遵守文本模式的约定（convert.convert_text）：一个覆盖整张截图的 text 块，order 为 0，text 为空串，
   lines 为全部非空行（按 convert.reading_order 从上到下、从左到右排列），块的 score 为各行置信度的平均值；
   没有识别到文字时返回空数组。

RapidOCR 还能给出词级与字级的框（return_word_box、return_single_char_box）。版面块格式里的文本行对象只有
bbox、text、score 三个字段（lst/services/ocr/blocks.py 的解码器拒绝多余字段），所以这里只输出行级位置，
不请求词级框。
"""

import importlib
import importlib.metadata
import importlib.util
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol, cast

import numpy as np

from lst.contracts.langs import normalize_lang
from lst.contracts.worker_protocol import JsonValue
from lst.workers._runtime import ConfigError, LoadResult, ProgressReporter
from lst.workers.ocr_worker.backend import ModelMissing
from lst.workers.ocr_worker.blocks import blocks_to_json
from lst.workers.ocr_worker.convert import convert_text, reading_order
from lst.workers.ocr_worker.extract import RawLine, RawPage
from lst.workers.ocr_worker.geometry import CoordinateMap, box_from, plan_resize
from lst.workers.ocr_worker.image import Pixels, resize_area, to_grayscale
from lst.workers.ocr_worker.settings import (
    BASE_KEYS,
    BadOptions,
    get_bool,
    get_float,
    get_int,
    resolve_cpu_threads,
)

log = logging.getLogger(__name__)

ENGINE: Final = "rapid"
AUTO: Final = "auto"

DET_MODEL: Final = "PP-OCRv6_det_small.onnx"
REC_MODEL: Final = "PP-OCRv6_rec_small.onnx"
CLS_MODEL: Final = "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
MODEL_FILES: Final = (DET_MODEL, REC_MODEL)
"""没有给出模型路径时加载的检测与识别模型，都在 rapidocr 包目录的 models 下。"""

CUDA_PROVIDER: Final = "CUDAExecutionProvider"
DML_PROVIDER: Final = "DmlExecutionProvider"
CPU_PROVIDER: Final = "CPUExecutionProvider"
ORT_DISTRIBUTIONS: Final = ("onnxruntime-gpu", "onnxruntime-directml", "onnxruntime")

_LATIN_LANGUAGES: Final = (
    "af", "az", "bs", "ca", "cs", "cy", "da", "de", "es", "et", "eu", "fi", "fr", "ga", "gl", "hr", "hu",
    "id", "is", "it", "ku", "la", "lb", "lt", "lv", "mi", "ms", "mt", "nl", "oc", "pl", "pt", "qu", "rm",
    "ro", "sk", "sl", "sq", "sv", "sw", "tl", "tr", "uz", "vi",
)  # fmt: skip
"""PP-OCRv6 多语言模型覆盖、本程序的规范写法与 RapidOCR 语言名相同的拉丁字母语言。"""

LANGUAGE_CODES: Final[dict[str, str]] = {
    "zh-Hans": "ch",
    "zh-Hant": "chinese_cht",
    "en": "en",
    "ja": "japan",
    **{code: code for code in _LATIN_LANGUAGES},
    # 挪威语：本程序把 no 规范成 nb（lst.contracts.langs），RapidOCR 写作 no；新挪威语同样用拉丁字母。
    "nb": "no",
    "nn": "no",
}
"""规范语言码 → RapidOCR 的语言名（default_models.yaml 的 ppocrv6_langs）。

塞尔维亚语（RapidOCR 的 rs_latin）没有收录：规范写法 sr 不区分拉丁字母与西里尔字母，屏幕上常见的是
西里尔字母，PP-OCRv6 不覆盖。
"""

LANGUAGES: Final = tuple(LANGUAGE_CODES)
"""默认识别模型（PP-OCRv6 多语言）可识别的规范语言码，中英日在前。"""

RAPID_LOGGER: Final = "RapidOCR"
"""rapidocr 自己的 logger 名。它默认带一个写标准错误的彩色处理器，并且不向上传递。"""

_WARMUP_LINES: Final = ("LSTtranslate warm up", "Screen translation 2026")

type Scalar = bool | int | float | str

CONSTRUCT_PARAMS: Final[dict[str, tuple[str, Scalar]]] = {
    "det_thresh": ("Det.thresh", 0.3),
    "det_limit_side_len": ("Det.limit_side_len", 736),
    "det_limit_type": ("Det.limit_type", "min"),
    "det_max_candidates": ("Det.max_candidates", 1000),
    "det_use_dilation": ("Det.use_dilation", True),
    "det_score_mode": ("Det.score_mode", "fast"),
    "cls_thresh": ("Cls.cls_thresh", 0.9),
    "rec_batch_num": ("Rec.rec_batch_num", 6),
    "min_side_len": ("Global.min_side_len", 30),
    "max_side_len": ("Global.max_side_len", 2000),
}
"""创建识别器时给出的参数：设置键 → （RapidOCR 的参数名，默认值）。
默认值与 rapidocr 3.10.0 的 config.yaml 相同，
也与 [ocr_rapid] 节的默认值相同。"""

_CONSTRUCT_LIMITS: Final[dict[str, tuple[float, float]]] = {
    "det_thresh": (0.0, 1.0),
    "det_limit_side_len": (32, 4096),
    "det_max_candidates": (1, 10000),
    "cls_thresh": (0.0, 1.0),
    "rec_batch_num": (1, 64),
    "min_side_len": (1, 1024),
    "max_side_len": (256, 16384),
}
_CONSTRUCT_CHOICES: Final[dict[str, frozenset[str]]] = {
    "det_limit_type": frozenset({"min", "max"}),
    "det_score_mode": frozenset({"fast", "slow"}),
}

CALL_PARAMS: Final[dict[str, tuple[str, float, float, float]]] = {
    "det_box_thresh": ("box_thresh", 0.5, 0.0, 1.0),
    "det_unclip_ratio": ("unclip_ratio", 1.6, 1.0, 4.0),
    "text_score": ("text_score", 0.5, 0.0, 1.0),
}
"""每次识别给出的参数：设置键 → （RapidOCR ``__call__`` 的参数名，默认值，下限，上限）。"""

MODEL_PATH_KEYS: Final = ("det_model_path", "rec_model_path", "cls_model_path")
MODEL_ID_KEYS: Final = ("detection_model", "recognition_model", "textline_orientation_model")

RAPID_KEYS: Final = frozenset(
    {*BASE_KEYS, *CONSTRUCT_PARAMS, *CALL_PARAMS, *MODEL_PATH_KEYS, *MODEL_ID_KEYS, "recognition_languages",
     "cpu_threads"}
)  # fmt: skip
"""本后端认识的设置键。"""

IGNORED_KEYS: Final = frozenset(
    {"model_cache_dir", "layout_model", "det_model", "rec_model", "region_model", "table_model",
     "formula_model", "chart_model", "doc_orientation_model", "doc_unwarping_model"}
)  # fmt: skip
"""PaddleOCR 专用、本后端直接忽略的键。"""

type Device = Literal["auto", "cpu", "gpu"]


class UnsupportedLanguage(ConfigError):
    """选用的识别模型不覆盖这种语言。"""


def rapid_language(language: str) -> str:
    """识别语言对应的 RapidOCR 语言名（PP-OCRv6 的写法）；auto 对应 ch。不支持时抛 UnsupportedLanguage。"""
    if language.strip().casefold() == AUTO:
        return LANGUAGE_CODES["zh-Hans"]
    try:
        code = normalize_lang(language)
    except ValueError as exc:
        raise UnsupportedLanguage(f"识别语言 {language!r} 不是有效的语言码") from exc
    name = LANGUAGE_CODES.get(code)
    if name is None:
        raise UnsupportedLanguage(
            f"RapidOCR 的 PP-OCRv6 识别模型不覆盖语言 {code}；支持的语言：{', '.join(LANGUAGES)}。"
            "可以把识别语言改为 auto，或改用 PaddleOCR"
        )
    return name


def check_language(language: str, languages: Sequence[str]) -> None:
    """核对识别语言是否被识别模型覆盖，见模块说明“识别语言”。"""
    if language.strip().casefold() == AUTO:
        return
    try:
        code = normalize_lang(language)
    except ValueError as exc:
        raise UnsupportedLanguage(f"识别语言 {language!r} 不是有效的语言码") from exc
    if code not in languages:
        raise UnsupportedLanguage(
            f"RapidOCR 选用的识别模型不覆盖语言 {code}；支持的语言：{', '.join(languages)}。"
            "可以把识别语言改为 auto、在设置里换一个覆盖该语言的识别模型，或改用 PaddleOCR"
        )


# ---------------------------------------------------------------- 设置与选项


def _construct_value(raw: Mapping[str, JsonValue], key: str, default: Scalar) -> Scalar:
    if key in _CONSTRUCT_CHOICES:
        value = raw.get(key, default)
        if not isinstance(value, str) or value not in _CONSTRUCT_CHOICES[key]:
            raise ConfigError(
                f"{key} 应为 {'、'.join(sorted(_CONSTRUCT_CHOICES[key]))} 之一，实际为 {value!r}"
            )
        return value
    if isinstance(default, bool):
        return get_bool(raw, key, default, ConfigError)
    low, high = _CONSTRUCT_LIMITS[key]
    if isinstance(default, int):
        value = get_int(raw, key, default, ConfigError)
        if not low <= value <= high:
            raise ConfigError(f"{key} 应在 {low:g} 至 {high:g} 之间，实际为 {value}")
        return value
    return get_float(raw, key, float(default), low, high, ConfigError)


def _path(raw: Mapping[str, JsonValue], key: str) -> Path | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} 应为非空字符串")
    return Path(value)


def _languages(raw: Mapping[str, JsonValue]) -> tuple[str, ...]:
    value = raw.get("recognition_languages")
    if value is None:
        return LANGUAGES
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError("recognition_languages 应为非空字符串的数组")
    return tuple(cast(list[str], value)) or LANGUAGES


@dataclass(frozen=True, slots=True)
class RapidSettings:
    """Configure 的设置，见模块说明的表格。rapid_language 是 language 对应的 RapidOCR 语言名（日志用）。"""

    language: str
    rapid_language: str
    grayscale: bool
    resize_max_side: int
    cpu_threads: int
    warmup: bool
    device: Device = "auto"
    textline_orientation: bool = False
    languages: tuple[str, ...] = LANGUAGES
    det_model_path: Path | None = None
    rec_model_path: Path | None = None
    cls_model_path: Path | None = None
    construct: dict[str, Scalar] = field(
        default_factory=lambda: {k: v for k, (_n, v) in CONSTRUCT_PARAMS.items()}
    )
    call: dict[str, float] = field(default_factory=lambda: {k: v[1] for k, v in CALL_PARAMS.items()})

    @classmethod
    def parse(cls, raw: Mapping[str, JsonValue]) -> "RapidSettings":
        """校验设置；无效时抛 ConfigError。"""
        unknown = sorted(set(raw) - RAPID_KEYS - IGNORED_KEYS)
        if unknown:
            log.warning("忽略未知的设置项：%s", ", ".join(unknown))
        mode = raw.get("mode", "text")
        if mode != "text":
            raise ConfigError(f"RapidOCR 只有文本模式，mode 应为 text，实际为 {mode!r}")
        engine = raw.get("engine", ENGINE)
        if engine != ENGINE:
            raise ConfigError(f"本后端是 RapidOCR，engine 应为 {ENGINE}，实际为 {engine!r}")
        language = raw.get("language", AUTO)
        if not isinstance(language, str) or not language.strip():
            raise ConfigError("language 应为非空字符串")
        language = language.strip()
        languages = _languages(raw)
        check_language(language, languages)
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
        try:
            name = rapid_language(language)
        except UnsupportedLanguage:
            name = language
        return cls(
            language=language,
            rapid_language=name,
            grayscale=get_bool(raw, "grayscale", False, ConfigError),
            resize_max_side=get_int(raw, "resize_max_side", 0, ConfigError),
            cpu_threads=get_int(raw, "cpu_threads", 0, ConfigError),
            warmup=get_bool(raw, "warmup", True, ConfigError),
            device=device,
            textline_orientation=get_bool(raw, "textline_orientation", False, ConfigError),
            languages=languages,
            det_model_path=_path(raw, "det_model_path"),
            rec_model_path=_path(raw, "rec_model_path"),
            cls_model_path=_path(raw, "cls_model_path"),
            construct={
                key: _construct_value(raw, key, default) for key, (_name, default) in CONSTRUCT_PARAMS.items()
            },
            call={
                key: get_float(raw, key, default, low, high, ConfigError)
                for key, (_name, default, low, high) in CALL_PARAMS.items()
            },
        )


@dataclass(frozen=True, slots=True)
class RapidOptions:
    """一次请求实际生效的预处理参数、是否做方向分类与每次识别的参数。"""

    grayscale: bool
    resize_max_side: int
    textline_orientation: bool = False
    call: dict[str, float] = field(default_factory=lambda: {k: v[1] for k, v in CALL_PARAMS.items()})

    @classmethod
    def parse(cls, raw: Mapping[str, JsonValue], settings: RapidSettings) -> "RapidOptions":
        """按请求的 options 覆盖设置里的默认值；取值无效时抛 BadOptions。版面子能力的开关全部忽略。"""
        for key, current in (("mode", "text"), ("language", settings.language)):
            requested = raw.get(key)
            if requested is not None and requested != current:
                log.warning("请求的 %s 为 %r，与进程当前的 %r 不同；切换需要重载进程，本次按 %r 处理",
                            key, requested, current, current)  # fmt: skip
        return cls(
            grayscale=get_bool(raw, "grayscale", settings.grayscale, BadOptions),
            resize_max_side=get_int(raw, "resize_max_side", settings.resize_max_side, BadOptions),
            textline_orientation=get_bool(
                raw, "textline_orientation", settings.textline_orientation, BadOptions
            ),
            call={
                key: get_float(raw, key, settings.call[key], low, high, BadOptions)
                for key, (_name, _default, low, high) in CALL_PARAMS.items()
            },
        )

    def call_kwargs(self) -> dict[str, object]:
        """RapidOCR ``__call__`` 的关键字参数。"""
        kwargs: dict[str, object] = {CALL_PARAMS[key][0]: value for key, value in self.call.items()}
        kwargs["use_cls"] = self.textline_orientation
        return kwargs


# ---------------------------------------------------------------- 结果转换


def _number_list(value: object) -> list[float]:
    """多边形（numpy 数组或嵌套列表）展平成浮点数列表。"""
    return [float(v) for v in np.asarray(value, dtype=np.float64).reshape(-1)]


def output_lines(result: object) -> list[RawLine]:
    """从 RapidOCR 的输出（RapidOCROutput）取出文本行，按阅读顺序排列。

    用到 boxes（N × 4 × 2 的角点）、txts、scores 三个字段；没有识别到文字时 RapidOCR 返回各字段为 None 的
    空输出，这里返回空列表。文字为空白的行丢弃。
    三个字段长度不一致时抛 ValueError（处理器记 engine.internal）。
    """
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if boxes is None or texts is None:
        return []
    text_list = [str(text) for text in cast(Sequence[object], texts)]
    box_list = list(cast(Sequence[object], boxes))
    score_list = (
        [1.0] * len(text_list)
        if scores is None
        else [float(cast(float, score)) for score in cast(Sequence[object], scores)]
    )
    lines: list[RawLine] = []
    for box, text, score in zip(box_list, text_list, score_list, strict=True):
        stripped = text.strip()
        if stripped:
            lines.append(RawLine(box_from(_number_list(box)), stripped, score))
    return reading_order(lines)


def build_blocks_json(result: object, original: tuple[int, int], target: tuple[int, int]) -> list[JsonValue]:
    """RapidOCR 的输出转成 OcrResult.blocks，见模块说明“识别与坐标”。

    original 是原始截图的宽高，target 是缩放后送入 RapidOCR 的图像宽高。
    """
    page = RawPage(output_size=target, angle=None, lines=tuple(output_lines(result)))
    return blocks_to_json(convert_text(page, CoordinateMap(original, target)))


# ---------------------------------------------------------------- 推理库接口


class RapidEngine(Protocol):
    """rapidocr.RapidOCR 的实例：调用它识别一张 BGR 图像，返回 RapidOCROutput。"""

    def __call__(self, img_content: Pixels, **kwargs: object) -> object: ...


type RapidFactory = Callable[[dict[str, object]], RapidEngine]
"""按参数（RapidOCR 的 params，键形如 "Det.model_path"）创建识别器。默认实现导入 rapidocr，测试换成假的。"""


@dataclass(frozen=True, slots=True)
class OrtRuntime:
    """onnxruntime 的情况：可用的执行器、加载 CUDA 运行库的函数、发行包名与版本。"""

    providers: tuple[str, ...]
    preload: Callable[[], None]
    distribution: str = "onnxruntime"
    version: str = "unknown"


type RuntimeProbe = Callable[[], OrtRuntime]


class _OrtModule(Protocol):
    def get_available_providers(self) -> list[str]: ...

    def preload_dlls(self) -> None: ...


class _RapidModule(Protocol):
    def RapidOCR(
        self, config_path: str | None = None, params: dict[str, object] | None = None
    ) -> RapidEngine: ...


class _Cv2Module(Protocol):
    FONT_HERSHEY_SIMPLEX: int
    LINE_AA: int

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


def _quiet_rapid_logger() -> None:
    """rapidocr 的 logger 改为向上传递、去掉它自己的处理器：日志按本进程的格式输出，不重复。"""
    logger = logging.getLogger(RAPID_LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.propagate = True


def import_rapidocr() -> RapidFactory:
    """导入 rapidocr，返回创建识别器的函数。导入失败时 ImportError 原样抛出。"""
    module = cast(_RapidModule, importlib.import_module("rapidocr.main"))
    _quiet_rapid_logger()

    def create(params: dict[str, object]) -> RapidEngine:
        return module.RapidOCR(params=params)

    return create


def _distribution() -> tuple[str, str]:
    for name in ORT_DISTRIBUTIONS:
        try:
            return name, importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return "onnxruntime", "unknown"


def probe_onnxruntime() -> OrtRuntime:
    """导入 onnxruntime，读出可用的执行器。导入失败时 ImportError、OSError 原样抛出。"""
    module = cast(_OrtModule, importlib.import_module("onnxruntime"))
    name, version = _distribution()
    preload = getattr(module, "preload_dlls", None)
    return OrtRuntime(
        providers=tuple(module.get_available_providers()),
        preload=cast(Callable[[], None], preload) if callable(preload) else (lambda: None),
        distribution=name,
        version=version,
    )


def package_model_dir() -> Path:
    """rapidocr 包目录下的 models。只查找包的位置，不导入它；找不到包时抛 ImportError。"""
    spec = importlib.util.find_spec("rapidocr")
    if spec is None or spec.origin is None:
        raise ImportError("没有安装 rapidocr")
    return Path(spec.origin).parent / "models"


def engine_params(
    model_dir: Path,
    threads: int,
    *,
    settings: RapidSettings | None = None,
    provider: str | None = None,
) -> dict[str, object]:
    """创建 RapidOCR 识别器的参数：模型显式给出本地路径，限定线程数，按 provider 打开 CUDA 或 DirectML。

    方向分类按请求决定（use_cls 在每次识别时给出），这里默认关闭；不请求词级框。
    """
    chosen = settings or RapidSettings.parse({})
    params: dict[str, object] = {
        "Global.log_level": "error",
        "Global.use_cls": False,
        "Global.return_word_box": False,
        "Global.model_root_dir": str(model_dir),
        "Det.model_path": str(chosen.det_model_path or model_dir / DET_MODEL),
        "Rec.model_path": str(chosen.rec_model_path or model_dir / REC_MODEL),
        "Cls.model_path": str(chosen.cls_model_path or model_dir / CLS_MODEL),
        "EngineConfig.onnxruntime.intra_op_num_threads": threads,
        "EngineConfig.onnxruntime.use_cuda": provider == CUDA_PROVIDER,
        "EngineConfig.onnxruntime.use_dml": provider == DML_PROVIDER,
    }
    for key, (name, _default) in CONSTRUCT_PARAMS.items():
        params[name] = chosen.construct[key]
    for key, (name, _default, _low, _high) in CALL_PARAMS.items():
        if name == "text_score":
            params["Global.text_score"] = chosen.call[key]
        else:
            params[f"Det.{name}"] = chosen.call[key]
    return params


def choose_provider(device: Device, providers: Sequence[str]) -> str | None:
    """按推理设备与可用执行器选择加速执行器，None 表示用 CPU，见模块说明“推理设备”。"""
    if device == "cpu":
        return None
    accelerated = next((name for name in (CUDA_PROVIDER, DML_PROVIDER) if name in providers), None)
    if accelerated is None and device == "gpu":
        raise ImportError(
            "推理设备设为显卡，但 RapidOCR 运行环境是 CPU 版（onnxruntime 没有 CUDA 或 DirectML 执行器）；"
            "请在设置里安装 RapidOCR 的 DirectML 版或 CUDA 版，或把推理设备改为自动或 CPU"
        )
    return accelerated


def active_provider(engine: object) -> str | None:
    """识别器的检测会话实际使用的第一个执行器；读不到（会话尚未创建或结构变了）时返回 None。"""
    try:
        session = getattr(getattr(getattr(engine, "text_det"), "session"), "session")  # noqa: B009
        providers = cast(Callable[[], list[str]], session.get_providers)()
    except (AttributeError, TypeError):
        return None
    return providers[0] if providers else None


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def warmup_image() -> Pixels:
    """预热用的合成图：白底上两行英文。用 OpenCV 画字；rapidocr 依赖 OpenCV，这时它已经导入。"""
    cv2 = cast(_Cv2Module, importlib.import_module("cv2"))
    image = np.full((160, 640, 3), 255, dtype=np.uint8)
    for index, line in enumerate(_WARMUP_LINES):
        cv2.putText(
            image, line, (24, 56 + index * 56), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 2, cv2.LINE_AA
        )
    return image


# ---------------------------------------------------------------- 后端


class RapidBackend:
    """RapidOCR 后端，实现 backend.OcrBackend。

    factory、model_dir、warmup_source、runtime 缺省时分别用 import_rapidocr()、package_model_dir()、
    warmup_image 与 probe_onnxruntime，测试注入假的识别器、模型目录与 onnxruntime 情况。
    """

    def __init__(
        self,
        factory: RapidFactory | None = None,
        model_dir: Path | None = None,
        warmup_source: Callable[[], Pixels] = warmup_image,
        runtime: RuntimeProbe | None = None,
    ) -> None:
        self._factory = factory
        self._model_dir = model_dir
        self._warmup_source = warmup_source
        self._runtime_probe = runtime
        self._runtime: OrtRuntime | None = None
        self._settings: RapidSettings | None = None
        self._engine: RapidEngine | None = None
        self._threads = 1
        self._provider: str | None = None

    def _ort(self) -> OrtRuntime:
        if self._runtime is None:
            if self._runtime_probe is not None:
                self._runtime = self._runtime_probe()
            elif self._factory is not None:
                # 注入了假识别器（测试）且没有给出 onnxruntime 情况时，按 CPU 版处理。
                self._runtime = OrtRuntime((CPU_PROVIDER,), lambda: None)
            else:
                self._runtime = probe_onnxruntime()
        return self._runtime

    def load(self, settings: Mapping[str, JsonValue], progress: ProgressReporter) -> LoadResult:
        parsed = RapidSettings.parse(settings)
        model_dir = self._model_dir if self._model_dir is not None else package_model_dir()
        files = {
            "det": parsed.det_model_path or model_dir / DET_MODEL,
            "rec": parsed.rec_model_path or model_dir / REC_MODEL,
        }
        if parsed.textline_orientation or parsed.cls_model_path is not None:
            files["cls"] = parsed.cls_model_path or model_dir / CLS_MODEL
        missing = [str(path) for path in files.values() if not path.is_file()]
        if missing:
            raise ModelMissing(f"RapidOCR 的模型文件不存在：{', '.join(missing)}")
        factory = self._factory
        if factory is None:
            progress("import")
            started = time.perf_counter()
            factory = self._factory = import_rapidocr()
            log.info("rapidocr 导入完成，耗时 %.1f 秒", time.perf_counter() - started)
        runtime = self._ort()
        provider = choose_provider(parsed.device, runtime.providers)
        if provider == CUDA_PROVIDER:
            try:
                runtime.preload()
            except (OSError, RuntimeError) as exc:
                if parsed.device == "gpu":
                    raise ImportError(f"CUDA 运行库加载失败：{type(exc).__name__}: {exc}") from exc
                log.warning("CUDA 运行库加载失败，改用 CPU：%s: %s", type(exc).__name__, exc)
                provider = None
        progress("model_load", ENGINE)
        threads = resolve_cpu_threads(parsed.cpu_threads)
        engine = factory(engine_params(model_dir, threads, settings=parsed, provider=provider))
        self._settings, self._engine, self._threads = parsed, engine, threads
        if parsed.warmup or provider is not None:
            progress("warmup")
            self._warm_up(engine)
        self._provider = self._verify_provider(engine, provider, parsed.device)
        log.info(
            "RapidOCR 已就绪：识别语言 %s（%s），执行器 %s，线程 %d，检测模型 %s，识别模型 %s",
            parsed.language,
            parsed.rapid_language,
            self._provider or CPU_PROVIDER,
            threads,
            files["det"],
            files["rec"],
        )
        return LoadResult(self._capabilities(parsed, threads, files), self._versions(runtime))

    @staticmethod
    def _verify_provider(engine: object, wanted: str | None, device: Device) -> str | None:
        """核对检测会话实际使用的执行器，见模块说明“推理设备”。"""
        if wanted is None:
            return None
        actual = active_provider(engine)
        if actual is None or actual == wanted:
            return wanted
        message = f"{wanted} 在本机不可用（检测会话实际使用 {actual}），通常是驱动版本太旧或显卡不支持"
        if device == "gpu":
            raise ImportError(message)
        log.warning("%s；改用 CPU", message)
        return None

    def _warm_up(self, engine: RapidEngine) -> None:
        """推理一次合成图：onnxruntime 在第一次推理时才建立检测与识别会话。失败只记警告，照常就绪。"""
        started = time.perf_counter()
        try:
            engine(self._warmup_source())
        except Exception as exc:
            log.warning("预热失败，照常就绪：%s: %s", type(exc).__name__, exc)
            return
        log.info("预热完成，耗时 %.2f 秒", time.perf_counter() - started)

    def _capabilities(
        self, settings: RapidSettings, threads: int, files: Mapping[str, Path]
    ) -> dict[str, JsonValue]:
        languages: list[JsonValue] = list(settings.languages)
        features: list[JsonValue] = ["text"]
        models: dict[str, JsonValue] = {key: path.name for key, path in files.items()}
        device = {CUDA_PROVIDER: "cuda", DML_PROVIDER: "directml"}.get(self._provider or "", "cpu")
        return {
            "mode": "text",
            "engine": ENGINE,
            "language": settings.language,
            "languages": languages,
            "features": features,
            "models": models,
            "device": device,
            "cpu_threads": threads,
            "preprocess": ["grayscale", "resize_max_side"],
        }

    @staticmethod
    def _versions(runtime: OrtRuntime) -> dict[str, str]:
        return {
            "engine": ENGINE,
            "rapidocr": _version("rapidocr"),
            "onnxruntime": runtime.version,
            "onnxruntime_package": runtime.distribution,
        }

    def recognize(self, image: Pixels, options: Mapping[str, JsonValue]) -> list[JsonValue]:
        settings, engine = self._settings, self._engine
        if settings is None or engine is None:
            raise RuntimeError("后端尚未加载")
        parsed = RapidOptions.parse(options, settings)
        height, width = image.shape[:2]
        target = plan_resize(width, height, parsed.resize_max_side)
        # 先缩放再灰度化：灰度化只处理缩小后的像素，与其他后端的顺序相同。
        prepared = resize_area(image, target)
        if parsed.grayscale:
            prepared = to_grayscale(prepared)
        result = engine(np.ascontiguousarray(prepared), **parsed.call_kwargs())
        return build_blocks_json(result, (width, height), target)
