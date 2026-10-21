"""OCR 模块拥有的配置节 [ocr]（需求 OCR-03、OCR-04、OCR-09、CAP-05，架构文档 8.2、8.4 节与第 9 章）。

组合根把 CONFIG_SECTIONS 交给配置服务，配置服务用 OcrConfig 校验配置文件并发布 AppConfig；
OCR 模块再用同一个模型解析 [ocr] 节（lst/contracts/config.py 的“配置如何传递”）。

配置分两类，传给 OCR 进程的途径不同：

- 进程级：识别模式、推理设备、识别语言。它们决定 OCR 进程加载哪条产线和哪个识别模型，变化后要由监管器
  重载 OCR 进程。``worker_settings`` 给出重载（以及首次启动）时 Configure 消息的内容，
  ``needs_reload`` 判断两份配置之间是否需要重载。
- 请求级：子能力开关（OCR-03）和截图预处理（CAP-05）。``request_options`` 把它们连同模式、语言一起放进
  每个 OcrRequest 的 options，OCR 进程以请求里的值为准，所以修改后从下一次截屏起生效，不需要重载。
"""

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lst.contracts.langs import normalize_lang
from lst.contracts.worker_protocol import JsonValue

AUTO_LANGUAGE: Final = "auto"
"""识别语言为“自动”：使用同时覆盖中文、英文、日文的通用识别模型（需求 OCR-04）。"""

MIN_RESIZE_SIDE: Final = 256
"""resize_max_side 非零时的下限。再小的图像里文字只剩几个像素，识别没有意义。"""


class OcrMode(StrEnum):
    """识别模式（需求 OCR-09）。"""

    STRUCTURE = "structure"
    """版面模式：PP-StructureV3，输出完整版面块。"""
    TEXT = "text"
    """文本模式：RapidOCR，只有文本行，全部归为正文块，由本模块合并成段落。"""


class OcrDevice(StrEnum):
    """推理设备。auto 表示有可用的 NVIDIA GPU 就用 GPU，否则用 CPU。"""

    AUTO = "auto"
    CPU = "cpu"
    GPU = "gpu"


class OcrConfig(BaseModel):
    """[ocr] 节。

    三项处理时限对应架构文档 8.2 节第 3 条：文本模式 text_timeout_s；版面模式 structure_timeout_s，
    表格、公式、图表识别每开一项再加 feature_timeout_s。时限只用来判断 OCR 进程是否卡死，
    与需求 6.1 节的性能目标无关。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    """是否启动 OCR 进程。关闭后截屏翻译不可用，省下 OCR 模型约 1 GB 的内存；由组合根处理。"""
    mode: OcrMode = OcrMode.STRUCTURE
    device: OcrDevice = OcrDevice.AUTO
    language: str = AUTO_LANGUAGE
    """"auto" 或语言代码；语言代码在校验时规范化，例如 zh-CN → zh-Hans、EN → en。"""

    # 子能力开关（需求 OCR-03）。只对版面模式有意义的开关在文本模式下由 OCR 进程忽略。
    table: bool = True
    formula: bool = False
    chart: bool = False
    doc_orientation: bool = False
    doc_unwarping: bool = False
    textline_orientation: bool = False

    # 截图预处理（需求 CAP-05），只作用于送入 OCR 的图像。
    grayscale: bool = False
    resize_max_side: int = Field(default=4000, ge=0, le=16384)
    """宽或高超过该值时等比缩小到该值；0 表示不缩放。"""

    text_timeout_s: float = Field(default=30.0, gt=0, le=3600)
    structure_timeout_s: float = Field(default=60.0, gt=0, le=3600)
    feature_timeout_s: float = Field(default=60.0, ge=0, le=3600)

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: str) -> str:
        if value.strip().lower() == AUTO_LANGUAGE:
            return AUTO_LANGUAGE
        return normalize_lang(value)

    @field_validator("resize_max_side")
    @classmethod
    def _check_resize(cls, value: int) -> int:
        if 0 < value < MIN_RESIZE_SIDE:
            raise ValueError(f"缩放上限为 0（不缩放）或不小于 {MIN_RESIZE_SIDE} 像素")
        return value


CONFIG_SECTIONS: Final[dict[str, type[BaseModel]]] = {"ocr": OcrConfig}
"""节名 → 该节的配置模型，由组合根注册给配置服务。"""


def processing_timeout(config: OcrConfig) -> float:
    """一次识别请求的处理时限（秒），交给 WorkerChannel.submit（架构文档 8.2 节第 3 条）。"""
    if config.mode is OcrMode.TEXT:
        return config.text_timeout_s
    features = sum((config.table, config.formula, config.chart))
    return config.structure_timeout_s + features * config.feature_timeout_s


def request_options(config: OcrConfig) -> dict[str, JsonValue]:
    """OcrRequest.options 的内容：识别模式与语言、子能力开关、预处理参数。键名与 OcrConfig 的字段名相同。"""
    return {
        "mode": config.mode.value,
        "language": config.language,
        "table": config.table,
        "formula": config.formula,
        "chart": config.chart,
        "doc_orientation": config.doc_orientation,
        "doc_unwarping": config.doc_unwarping,
        "textline_orientation": config.textline_orientation,
        "grayscale": config.grayscale,
        "resize_max_side": config.resize_max_side,
    }


def worker_settings(config: OcrConfig) -> dict[str, JsonValue]:
    """OCR 进程的 Configure 内容：request_options 的全部字段加上推理设备。

    子能力开关也放进来，供 OCR 进程启动时决定预加载哪些模型；之后开关变化不重载进程，
    OCR 进程按请求里的 options 执行，用到没有预加载的模型时再加载。
    """
    return {**request_options(config), "device": config.device.value}


def needs_reload(old: OcrConfig, new: OcrConfig) -> bool:
    """两份配置之间是否需要重载 OCR 进程：识别模式、推理设备或识别语言（决定识别模型）变化时需要。"""
    return (old.mode, old.device, old.language) != (new.mode, new.device, new.language)
