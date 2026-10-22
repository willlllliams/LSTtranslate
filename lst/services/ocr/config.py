"""OCR 模块拥有的配置节（需求 OCR-03、OCR-04、OCR-09、OCR-11、OCRE-08、OCRE-09、CAP-05，架构文档 8.2、
8.4 节与第 9 章）。

组合根把 CONFIG_SECTIONS 交给配置服务与设置窗口，配置服务用这些模型校验配置文件并发布 AppConfig；
OCR 模块再用同一批模型解析各节（lst/contracts/config.py 的“配置如何传递”）。共有五节：

- ``[ocr]``（OcrConfig）：OCR 总开关、当前引擎、识别模式、推理设备、识别语言、子能力开关、截图预处理与
  处理时限，各引擎共用；
- ``[ocr_windows]``、``[ocr_rapid]``、``[ocr_paddle]``（WindowsOcrConfig、RapidOcrConfig、PaddleOcrConfig）：
  各引擎的识别流水线配置，即每个环节选用的模型与流水线参数（OCRE-08、OCRE-09）；
- ``[ocr_normalize]``（NormalizeConfig，见 normalize.py）：结构归一化中间层各步骤的开关与参数（OCR-11）。

配置分两类，传给 OCR 进程的途径不同：

- 进程级：OCR 引擎、识别模式、推理设备、识别语言，以及流水线配置里标了 ``reload`` 的字段（模型选择、
  线程数、只能在创建识别器时给出的参数）。它们决定 OCR 进程加载什么，变化后要由监管器重载 OCR 进程。
  ``worker_settings`` 给出重载（以及首次启动）时 Configure 消息的内容，``process_key`` 给出决定进程加载内容
  的字段；组合根比较前后两次的 process_key 决定是否重载。
- 请求级：子能力开关（OCR-03）、截图预处理（CAP-05）与流水线配置里没有标 ``reload`` 的参数。
  ``request_options`` 把它们连同模式、语言一起放进每个 OcrRequest 的 options，OCR 进程以请求里的值为准，
  修改后从下一次截屏起生效，不需要重载。

流水线配置的字段约定
====================

设置窗口按字段的元数据自动生成界面，各引擎的流水线配置模型都遵守下面的约定：

1. 选择环节模型的字段名是 ``<环节>_model``（环节取 lst.contracts.ocr_engines.OcrStage 的值，例如
   ``detection_model``、``layout_model``），类型是 str，默认值是该环节默认模型的 id（与
   OcrEngineInfo.models 里 default 为真、stage 相同的那一项一致），``json_schema_extra`` 里有
   ``{"ocr_stage": "<环节>", "reload": True}``。取值不做枚举校验：模型目录随引擎版本变化，界面从
   OcrEngineInfo.models 里取可选项；OCR 进程加载时发现模型不存在会以配置错误退出。
2. 参数字段都有 ``title`` 与 ``description``（中文），数值的取值范围用 ``ge``、``le`` 写出，取值集合用
   Literal 写出；界面据此标明范围，“恢复默认”取字段的默认值。
3. 不常用的参数在 ``json_schema_extra`` 里带 ``{"advanced": True}``，界面把它们放进“高级”；
   需要重载 OCR 进程的字段带 ``{"reload": True}``，其余随每次请求发送。
4. 数值参数里约定 0 表示“使用引擎的默认值”的字段，在 description 里写明。

字段名同时是发给 OCR 进程的键名（worker_settings 与 request_options 原样使用），后端按同样的名字读取。

旧 [ocr] 节的字段保持不变，迁移关系是：

- ``mode``：只对 PaddleOCR 生效，Windows OCR 与 RapidOCR 总是文本模式（TEXT_ONLY_ENGINES）；
- ``device``：PaddleOCR 与 RapidOCR 共用。auto 用已装运行环境版本所带的加速（PaddleOCR 的 CUDA 版、RapidOCR
  的 DirectML 版或 CUDA 版），cpu 强制用 CPU，gpu 要求加速、运行环境是 CPU 版时 OCR 进程以依赖错误退出；
- ``table``、``formula``、``chart``、``region``、``doc_orientation``、``doc_unwarping``、
  ``textline_orientation``：子能力开关仍在 [ocr]，选用哪个模型在各引擎的节里
  （例如 ``[ocr_paddle].table_model``）。RapidOCR 只认
  ``textline_orientation``（文本行方向分类）；
- 原来写死在 OCR 进程里的模型名（PP-DocLayout_plus-L、PP-OCRv5_mobile_det 等）改为各引擎节的
  ``<环节>_model`` 字段，默认值与原来相同，没有这些节的旧配置文件行为不变。

OCR 引擎（lst/contracts/ocr_engines.py）：默认是 Windows 自带的 OCR，安装版装好即可截屏识别。
Windows OCR 与 RapidOCR 只有文本模式（TEXT_ONLY_ENGINES），engine 是它们时 mode 字段保留用户的取值，
但不生效：发给 OCR 进程的模式、处理时限和结果转换都按 ``effective_mode`` 取 text。这样配置文件里写着
``mode = "structure"`` 也照常通过校验，切回 PaddleOCR 时恢复版面模式。
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.fields import FieldInfo

from lst.contracts.langs import normalize_lang
from lst.contracts.ocr_engines import OcrEngineKind, OcrStage
from lst.contracts.worker_protocol import JsonValue
from lst.services.ocr.normalize import NormalizeConfig

AUTO_LANGUAGE: Final = "auto"
"""识别语言为“自动”：使用同时覆盖中文、英文、日文的通用识别模型（需求 OCR-04）。"""

MIN_RESIZE_SIDE: Final = 256
"""resize_max_side 非零时的下限。再小的图像里文字只剩几个像素，识别没有意义。"""

MAX_DET_BOX_THRESH: Final = 0.7
"""PaddleOCR 检测框阈值（[ocr_paddle].text_det_box_thresh）的上限。

DB 检测的框得分是框内各像素文字概率的平均值，PaddleX 丢弃得分低于阈值的框（paddlex 3.7
text_detection/processors.py 的 ``if box_thresh > score: continue``）。平均概率达不到 1，阈值取 1 时
一个框也留不下。截屏测试页（tests/workers/ocr/fixtures/screenshot_test_page.jpg）上 18 个文字行的
得分如下，原图与放大 1.5 倍各测一次：

- PP-OCRv5_server_det：0.84 至 0.98；
- PP-OCRv5_mobile_det：0.75 至 0.95；
- PP-OCRv3_mobile_det：0.70 至 0.93。

检测一行也没有时，PP-StructureV3 把每个没有文字的版面块整块裁下来当作一行识别（paddlex 3.7
layout_parsing/pipeline_v2.py 的 standardized_data），两行以上的段落识别成空串或一两个字母，正文就丢了。
0.7 是 PaddleX 文本检测模块自己的默认值，也是上面三个模型里清晰文字行得分的下沿。
"""

STAGE_KEY: Final = "ocr_stage"
ADVANCED_KEY: Final = "advanced"
RELOAD_KEY: Final = "reload"


class OcrMode(StrEnum):
    """识别模式（需求 OCR-09）。"""

    STRUCTURE = "structure"
    """版面模式：PP-StructureV3，输出完整版面块。"""
    TEXT = "text"
    """文本模式：PaddleOCR 文本检测加识别，或 Windows OCR、RapidOCR；只有文本行，全部归为正文块，
    由本模块合并成段落。"""


class OcrDevice(StrEnum):
    """推理设备。auto 表示运行环境带加速（CUDA、DirectML）并且本机可用时用加速，否则用 CPU。"""

    AUTO = "auto"
    CPU = "cpu"
    GPU = "gpu"


class OcrConfig(BaseModel):
    """[ocr] 节。

    三项处理时限对应架构文档 8.2 节第 3 条：文本模式（含 Windows OCR）text_timeout_s；
    版面模式 structure_timeout_s，表格、公式、图表识别每开一项再加 feature_timeout_s。
    时限只用来判断 OCR 进程是否卡死，与需求 6.1 节的性能目标无关。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    """是否启动 OCR 进程。关闭后截屏翻译不可用，省下 OCR 模型约 1 GB 的内存；由组合根处理。"""
    engine: OcrEngineKind = OcrEngineKind.WINDOWS
    """OCR 引擎。windows 与 rapid 只有文本模式，mode 不生效（见模块说明与 effective_mode）。"""
    mode: OcrMode = OcrMode.STRUCTURE
    """识别模式，只对 PaddleOCR 生效。"""
    device: OcrDevice = OcrDevice.AUTO
    """推理设备，对 PaddleOCR 与 RapidOCR 生效，见模块说明的迁移关系。"""
    language: str = AUTO_LANGUAGE
    """"auto" 或语言代码；语言代码在校验时规范化，例如 zh-CN → zh-Hans、EN → en。"""

    # 子能力开关（需求 OCR-03）。只对版面模式有意义的开关在文本模式下由 OCR 进程忽略。
    table: bool = False
    """表格识别。默认关闭，沿用重构前 PP-StructureV3 的取值（旧版 config.ini 与默认配置都是 false），
    表格较多时在设置里打开。"""
    formula: bool = False
    chart: bool = False
    region: bool = True
    """版面区域检测（PaddleOCR 版面模式）：把版面块按区域分组，多栏与图文混排时阅读顺序更准。"""
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


# ---------------------------------------------------------------- 流水线配置的字段工具


def stage_model(stage: OcrStage, default: str, title: str, description: str) -> Any:
    """选择一个环节模型的字段，见模块说明的约定第 1 条。"""
    return Field(
        default=default,
        min_length=1,
        title=title,
        description=description,
        json_schema_extra={STAGE_KEY: stage.value, RELOAD_KEY: True},
    )


def param(
    default: Any,
    title: str,
    description: str,
    *,
    advanced: bool = False,
    reload: bool = False,
    ge: float | None = None,
    le: float | None = None,
) -> Any:
    """一个流水线参数字段，见模块说明的约定第 2 至 4 条。"""
    extra: dict[str, JsonValue] = {}
    if advanced:
        extra[ADVANCED_KEY] = True
    if reload:
        extra[RELOAD_KEY] = True
    return Field(
        default=default,
        title=title,
        description=description,
        ge=ge,
        le=le,
        json_schema_extra=extra or None,
    )


class PipelineConfig(BaseModel):
    """各引擎流水线配置的基类：字段约定见模块说明。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @classmethod
    def reload_fields(cls) -> tuple[str, ...]:
        """带 reload 标记的字段名，按定义顺序。"""
        return tuple(name for name, info in cls.model_fields.items() if _extra(info).get(RELOAD_KEY) is True)

    @classmethod
    def request_fields(cls) -> tuple[str, ...]:
        """随每次请求发送的字段名（没有 reload 标记），按定义顺序。"""
        reload = set(cls.reload_fields())
        return tuple(name for name in cls.model_fields if name not in reload)

    @classmethod
    def stage_fields(cls) -> dict[OcrStage, str]:
        """环节 → 选择该环节模型的字段名。"""
        result: dict[OcrStage, str] = {}
        for name, info in cls.model_fields.items():
            stage = _extra(info).get(STAGE_KEY)
            if isinstance(stage, str):
                result[OcrStage(stage)] = name
        return result

    def selected_models(self) -> dict[OcrStage, str]:
        """环节 → 当前选用的模型 id。"""
        return {stage: str(getattr(self, name)) for stage, name in self.stage_fields().items()}

    def values(self, names: tuple[str, ...]) -> dict[str, JsonValue]:
        dumped = self.model_dump(mode="json")
        return {name: dumped[name] for name in names}


def _extra(info: FieldInfo) -> Mapping[str, object]:
    extra = info.json_schema_extra
    return cast(dict[str, object], extra) if isinstance(extra, dict) else {}


# ---------------------------------------------------------------- Windows OCR


class WindowsOcrConfig(PipelineConfig):
    """[ocr_windows] 节。

    Windows.Media.Ocr 只有一个按语言创建的识别器，系统接口不提供检测阈值、模型选择等可调参数，所以这一节
    没有字段；保留这一节是为了让三个引擎的配置结构一致，系统接口以后开放参数时加在这里。
    """


# ---------------------------------------------------------------- RapidOCR

RAPID_DEFAULT_DET: Final = "PP-OCRv6/small/multi/det"
RAPID_DEFAULT_REC: Final = "PP-OCRv6/small/multi/rec"
RAPID_DEFAULT_CLS: Final = "PP-OCRv4/mobile/ch/cls"
"""RapidOCR 各环节的默认模型 id，与 services.ocr_engines 的 RapidOCR 模型目录（rapid_models.py）一致。"""


class RapidOcrConfig(PipelineConfig):
    """[ocr_rapid] 节：RapidOCR 的模型与参数。

    参数名与取值范围对应 rapidocr 3.10.0 的 config.yaml（Det、Cls、Rec、Global、
    EngineConfig.onnxruntime 几节）。
    RapidOCR 的 ``__call__`` 只接受 text_score、box_thresh、unclip_ratio 与 use_cls 几个参数，其余参数只能在
    创建识别器时给出，所以标为 reload。
    """

    detection_model: str = stage_model(
        OcrStage.DETECTION, RAPID_DEFAULT_DET, "文字检测模型", "检测文本行位置的模型。"
    )
    recognition_model: str = stage_model(
        OcrStage.RECOGNITION,
        RAPID_DEFAULT_REC,
        "文字识别模型",
        "识别文本行内容的模型，决定能识别的语言；识别语言为“自动”时用它，选其他语言时它要覆盖该语言。",
    )
    textline_orientation_model: str = stage_model(
        OcrStage.TEXTLINE_ORIENTATION,
        RAPID_DEFAULT_CLS,
        "文本行方向模型",
        "判断文本行是否倒置的分类模型，只在 [ocr] 开启“文本行方向判断”时使用。",
    )

    det_thresh: float = param(
        0.3, "检测像素阈值", "文字概率图二值化的阈值，越低越容易检出浅色、细小的文字。", ge=0.0, le=1.0,
        reload=True,
    )  # fmt: skip
    det_box_thresh: float = param(
        0.5, "检测框阈值", "文本框平均得分低于该值时丢弃，越高误检越少、漏检越多。", ge=0.0, le=1.0
    )
    det_unclip_ratio: float = param(
        1.6, "文本框外扩系数", "检测框向外扩张的比例，越大框越宽松，文字边缘越不容易被裁掉。", ge=1.0, le=4.0
    )
    det_limit_side_len: int = param(
        736, "检测输入尺寸", "检测前按“限制方式”把图像的边长缩放到该值（像素）。", ge=32, le=4096, reload=True
    )
    det_limit_type: Literal["min", "max"] = param(
        "min",
        "检测尺寸限制方式",
        "min：短边不小于检测输入尺寸（放大小图）；max：长边不大于检测输入尺寸（缩小大图）。",
        advanced=True,
        reload=True,
    )
    det_max_candidates: int = param(
        1000, "最多文本框数", "一张图最多保留的候选文本框数。", ge=1, le=10000, advanced=True, reload=True
    )
    det_use_dilation: bool = param(
        True, "膨胀概率图", "二值化后先做一次膨胀，把断开的笔画连起来。", advanced=True, reload=True
    )
    det_score_mode: Literal["fast", "slow"] = param(
        "fast",
        "文本框打分方式",
        "fast 按外接矩形打分；slow 按多边形打分，弯曲文字更准但更慢。",
        advanced=True,
        reload=True,
    )
    text_score: float = param(0.5, "识别分数阈值", "识别置信度低于该值的文本行丢弃。", ge=0.0, le=1.0)
    cls_thresh: float = param(
        0.9, "方向判断阈值", "文本行方向分类的置信度高于该值才旋转该行。", ge=0.0, le=1.0, advanced=True,
        reload=True,
    )  # fmt: skip
    rec_batch_num: int = param(
        6, "识别批大小", "一次送入识别模型的文本行数。", ge=1, le=64, advanced=True, reload=True
    )
    min_side_len: int = param(
        30, "最小边长", "短边小于该值（像素）的图像先放大再识别。", ge=1, le=1024, advanced=True, reload=True
    )
    max_side_len: int = param(
        2000,
        "最大边长",
        "长边大于该值（像素）的图像先缩小再识别。",
        ge=256,
        le=16384,
        advanced=True,
        reload=True,
    )
    cpu_threads: int = param(
        0,
        "CPU 线程数",
        "onnxruntime 的算子内线程数；0 表示自动（不超过 8 个）。用显卡推理时只影响前后处理。",
        ge=0,
        le=256,
        reload=True,
    )


# ---------------------------------------------------------------- PaddleOCR

PADDLE_DEFAULT_MODELS: Final[dict[OcrStage, str]] = {
    OcrStage.DETECTION: "PP-OCRv5_mobile_det",
    OcrStage.RECOGNITION: "PP-OCRv5_mobile_rec",
    OcrStage.TEXTLINE_ORIENTATION: "PP-LCNet_x1_0_textline_ori",
    OcrStage.DOC_ORIENTATION: "PP-LCNet_x1_0_doc_ori",
    OcrStage.DOC_UNWARPING: "UVDoc",
    OcrStage.LAYOUT: "PP-DocLayout_plus-L",
    OcrStage.REGION: "PP-DocBlockLayout",
    OcrStage.TABLE: "SLANet_plus",
    OcrStage.FORMULA: "PP-FormulaNet_plus-L",
    OcrStage.CHART: "PP-Chart2Table",
}
"""PaddleOCR 各环节的默认模型（PaddleX 模型名），与 services.ocr_engines 的 PaddleOCR 模型目录一致。"""


class PaddleOcrConfig(PipelineConfig):
    """[ocr_paddle] 节：PaddleOCR 的模型与参数。

    参数对应 PP-StructureV3 与 OCR 产线 ``predict`` 的同名参数（paddlex 3.7 的
    inference/pipelines/layout_parsing/pipeline_v2.py 与 ocr/pipeline.py），都能逐次请求给出，所以不需要重载；
    模型选择与线程数要重新创建产线，标为 reload。表格环节选的是无线表格的结构识别模型，有线表格的结构模型、
    表格分类与单元格检测模型固定随引擎安装。
    """

    detection_model: str = stage_model(
        OcrStage.DETECTION, PADDLE_DEFAULT_MODELS[OcrStage.DETECTION], "文字检测模型",
        "检测文本行位置的模型：mobile 快，server 准。",
    )  # fmt: skip
    recognition_model: str = stage_model(
        OcrStage.RECOGNITION,
        PADDLE_DEFAULT_MODELS[OcrStage.RECOGNITION],
        "文字识别模型",
        "识别语言为“自动”或中英日时使用；选了其他语言时，模型覆盖该语言才使用它，否则改用该语言的专用模型。",
    )
    textline_orientation_model: str = stage_model(
        OcrStage.TEXTLINE_ORIENTATION, PADDLE_DEFAULT_MODELS[OcrStage.TEXTLINE_ORIENTATION], "文本行方向模型",
        "判断文本行是否倒置，只在开启“文本行方向判断”时使用。",
    )  # fmt: skip
    doc_orientation_model: str = stage_model(
        OcrStage.DOC_ORIENTATION, PADDLE_DEFAULT_MODELS[OcrStage.DOC_ORIENTATION], "文档方向模型",
        "判断整张图是否旋转，只在开启“文档方向校正”时使用。",
    )  # fmt: skip
    doc_unwarping_model: str = stage_model(
        OcrStage.DOC_UNWARPING, PADDLE_DEFAULT_MODELS[OcrStage.DOC_UNWARPING], "去畸变模型",
        "把拍照造成的弯曲页面拉平，只在开启“文档去畸变”时使用。",
    )  # fmt: skip
    layout_model: str = stage_model(
        OcrStage.LAYOUT, PADDLE_DEFAULT_MODELS[OcrStage.LAYOUT], "版面检测模型",
        "版面模式下划分正文、标题、表格、公式等区域：L 准，S 快。",
    )  # fmt: skip
    region_model: str = stage_model(
        OcrStage.REGION, PADDLE_DEFAULT_MODELS[OcrStage.REGION], "区域检测模型",
        "版面模式下把版面块分组成栏与区域，用于排阅读顺序。",
    )  # fmt: skip
    table_model: str = stage_model(
        OcrStage.TABLE, PADDLE_DEFAULT_MODELS[OcrStage.TABLE], "表格结构模型",
        "识别无线表格（没有框线的表格）的行列结构，只在开启“表格识别”时使用。",
    )  # fmt: skip
    formula_model: str = stage_model(
        OcrStage.FORMULA, PADDLE_DEFAULT_MODELS[OcrStage.FORMULA], "公式识别模型",
        "把公式转换为 LaTeX，只在开启“公式识别”时使用。",
    )  # fmt: skip
    chart_model: str = stage_model(
        OcrStage.CHART, PADDLE_DEFAULT_MODELS[OcrStage.CHART], "图表识别模型",
        "把图表转换为数据表，只在开启“图表识别”并且用显卡推理时使用。",
    )  # fmt: skip

    text_det_limit_side_len: int = param(
        0,
        "检测输入尺寸",
        "检测前把图像边长缩放到该值（像素）；0 表示用产线的默认值（版面模式 736，文本模式 64）。",
        ge=0,
        le=4096,
    )
    text_det_limit_type: Literal["default", "min", "max"] = param(
        "default",
        "检测尺寸限制方式",
        "min：短边不小于检测输入尺寸；max：长边不大于检测输入尺寸；default：用产线的默认值（min）。",
        advanced=True,
    )
    text_det_thresh: float = param(
        0.3, "检测像素阈值", "文字概率图二值化的阈值，越低越容易检出浅色、细小的文字。", ge=0.0, le=1.0
    )
    text_det_box_thresh: float = param(
        0.6,
        "检测框阈值",
        "文本框平均得分低于该值时丢弃。上限 0.7：截图里清晰的文字行得分在 0.7 至 0.98 之间，"
        "阈值再高会丢掉正常的文字行。",
        ge=0.0,
        le=MAX_DET_BOX_THRESH,
    )
    text_det_unclip_ratio: float = param(
        1.5, "文本框外扩系数", "检测框向外扩张的比例。", ge=1.0, le=4.0, advanced=True
    )
    text_rec_score_thresh: float = param(
        0.0, "识别分数阈值", "识别置信度低于该值的文本行丢弃；0 表示全部保留。", ge=0.0, le=1.0
    )
    layout_threshold: float = param(
        0.0,
        "版面检测阈值",
        "版面块置信度低于该值时丢弃；0 表示用各类别的默认阈值（0.3 至 0.5）。",
        ge=0.0,
        le=1.0,
    )
    layout_nms: bool = param(True, "版面框去重", "对重叠的版面框做非极大值抑制。", advanced=True)
    layout_unclip_ratio: float = param(
        1.0, "版面框外扩系数", "版面框向外扩张的比例。", ge=0.5, le=3.0, advanced=True
    )
    layout_merge_bboxes_mode: Literal["default", "large", "small", "union"] = param(
        "default",
        "重叠版面框的处理",
        "large 保留大框，small 保留小框，union 两者都保留；default 用各类别的默认方式。",
        advanced=True,
    )
    cpu_threads: int = param(
        0, "CPU 线程数", "CPU 推理线程数；0 表示自动（不超过 8 个）。", ge=0, le=256, reload=True
    )
    enable_mkldnn: bool = param(
        True,
        "CPU 加速（oneDNN）",
        "用 CPU 推理时，对能在 oneDNN 下运行的模型启用 oneDNN 加速；"
        "关闭后全部模型用普通方式运行，更慢但最稳。"
        "用显卡推理时不起作用。",
        advanced=True,
        reload=True,
    )


# ---------------------------------------------------------------- 注册与组合

PIPELINE_SECTIONS: Final[dict[OcrEngineKind, tuple[str, type[PipelineConfig]]]] = {
    OcrEngineKind.WINDOWS: ("ocr_windows", WindowsOcrConfig),
    OcrEngineKind.RAPID: ("ocr_rapid", RapidOcrConfig),
    OcrEngineKind.PADDLE: ("ocr_paddle", PaddleOcrConfig),
}
"""引擎 → （配置节名，流水线配置模型）。"""

NORMALIZE_SECTION: Final = "ocr_normalize"

CONFIG_SECTIONS: Final[dict[str, type[BaseModel]]] = {
    "ocr": OcrConfig,
    **{name: model for name, model in PIPELINE_SECTIONS.values()},
    NORMALIZE_SECTION: NormalizeConfig,
}
"""节名 → 该节的配置模型，由组合根注册给配置服务与设置窗口。"""

TEXT_ONLY_ENGINES: Final = frozenset({OcrEngineKind.WINDOWS, OcrEngineKind.RAPID})
"""只有文本模式的引擎：识别模式对它们不生效。"""

DEVICE_ENGINES: Final = frozenset({OcrEngineKind.RAPID, OcrEngineKind.PADDLE})
"""推理设备对之生效的引擎。"""


def default_pipeline(engine: OcrEngineKind) -> PipelineConfig:
    """引擎的默认流水线配置。"""
    return PIPELINE_SECTIONS[engine][1]()


def parse_pipeline(engine: OcrEngineKind, raw: object) -> PipelineConfig:
    """解析引擎的流水线配置节；节缺失或不是字典时用默认值，校验失败抛 ValidationError。"""
    model = PIPELINE_SECTIONS[engine][1]
    return model.model_validate(raw if isinstance(raw, dict) else {})


def pipeline_or_default(engine: OcrEngineKind, raw: object) -> PipelineConfig:
    """同 parse_pipeline，校验失败时用默认值（配置服务已经校验过，这只发生在节没有注册时）。"""
    try:
        return parse_pipeline(engine, raw)
    except ValidationError:
        return default_pipeline(engine)


def _pipeline(config: OcrConfig, pipeline: PipelineConfig | None) -> PipelineConfig:
    expected = PIPELINE_SECTIONS[config.engine][1]
    if pipeline is None or not isinstance(pipeline, expected):
        return expected()
    return pipeline


def effective_mode(config: OcrConfig) -> OcrMode:
    """实际生效的识别模式：Windows OCR 与 RapidOCR 总是文本模式，PaddleOCR 取 mode 字段。"""
    return OcrMode.TEXT if config.engine in TEXT_ONLY_ENGINES else config.mode


def processing_timeout(config: OcrConfig) -> float:
    """一次识别请求的处理时限（秒），交给 WorkerChannel.submit（架构文档 8.2 节第 3 条）。

    文本模式（含 Windows OCR 与 RapidOCR）用 text_timeout_s；版面模式按开启的子能力加时。
    """
    if effective_mode(config) is OcrMode.TEXT:
        return config.text_timeout_s
    features = sum((config.table, config.formula, config.chart))
    return config.structure_timeout_s + features * config.feature_timeout_s


def request_options(config: OcrConfig, pipeline: PipelineConfig | None = None) -> dict[str, JsonValue]:
    """OcrRequest.options 的内容：识别模式与语言、子能力开关、预处理参数，以及当前引擎流水线配置里的
    请求级参数。键名与配置模型的字段名相同。

    mode 是 effective_mode 的值；Windows OCR 与 RapidOCR 忽略版面专用的子能力开关。pipeline 为 None 或不是
    当前引擎的配置时用当前引擎的默认流水线配置。
    """
    chosen = _pipeline(config, pipeline)
    return {
        "mode": effective_mode(config).value,
        "language": config.language,
        "table": config.table,
        "formula": config.formula,
        "chart": config.chart,
        "region": config.region,
        "doc_orientation": config.doc_orientation,
        "doc_unwarping": config.doc_unwarping,
        "textline_orientation": config.textline_orientation,
        "grayscale": config.grayscale,
        "resize_max_side": config.resize_max_side,
        **chosen.values(chosen.request_fields()),
    }


def worker_settings(config: OcrConfig, pipeline: PipelineConfig | None = None) -> dict[str, JsonValue]:
    """OCR 进程的 Configure 内容：request_options 的全部字段，加上推理设备、OCR 引擎与流水线配置的进程级字段。

    子能力开关也放进来，供 OCR 进程启动时决定预加载哪些模型；之后开关变化不重载进程，
    OCR 进程按请求里的 options 执行，用到没有预加载的模型时再加载。
    engine 供 OCR 进程核对自己的后端；用哪个后端由组合根启动进程时的 --backend 决定。
    """
    chosen = _pipeline(config, pipeline)
    return {
        **request_options(config, chosen),
        **chosen.values(chosen.reload_fields()),
        "device": config.device.value,
        "engine": config.engine.value,
    }


def process_key(config: OcrConfig, pipeline: PipelineConfig | None = None) -> tuple[object, ...]:
    """决定 OCR 进程加载内容的字段，组合根比较前后两次的值决定是否重载 OCR 进程。

    - 引擎与识别语言总在其中；
    - PaddleOCR 还看识别模式，PaddleOCR 与 RapidOCR 还看推理设备；Windows OCR 下改 mode、device 不重载，
      它们在切回对应引擎时才生效；
    - 当前引擎流水线配置里带 reload 标记的字段（模型选择、线程数等）。
    子能力开关、预处理参数与请求级参数随每个请求发送，不进入这里。
    """
    chosen = _pipeline(config, pipeline)
    key: list[object] = [config.engine.value]
    if config.engine not in TEXT_ONLY_ENGINES:
        key.append(config.mode.value)
    if config.engine in DEVICE_ENGINES:
        key.append(config.device.value)
    key.append(config.language)
    key.extend(sorted(chosen.values(chosen.reload_fields()).items()))
    return tuple(key)
