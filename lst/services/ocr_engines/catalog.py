r"""各引擎识别流水线每个环节的可选模型（需求 OCRE-08，OcrModelInfo）。

一个模型是一个 ``ModelSpec``：id 在引擎内唯一，也是配置 ``[ocr_<引擎>].<环节>_model`` 的取值；各环节的默认
模型 id 与 lst/services/ocr/config.py 里的字段默认值一致（两个模块互不导入，tests/services/ocr_engines 里有
一条测试核对两边相同）。

PaddleOCR
=========

id 是 PaddleX 的模型名，下载地址是 ``<DEFAULT_MODEL_BASE_URL>/<模型名>_infer.tar``（layout.model_url），解压到
``<模型缓存>\official_models\<模型名>``。大小（size_bytes）取自开发机 assets/PaddleOCR_and_PP-Structure/
official_models 下同名模型目录里文件的合计（PP-OCRv5_mobile_det、PP-OCRv5_mobile_rec、PP-DocLayout_plus-L 三个
与官方下载地址的 Content-Length 一致）；开发机上没有的模型写 None，界面显示“大小未知”。

- 默认组合（default=True）与原来安装时下载的清单相同：版面模式与表格识别所需的模型，另加体积小的文档方向
  分类、去畸变、文本行方向分类模型，让这些开关不需要再次下载就能打开。公式（PP-FormulaNet_plus-L，约
  730 MB）与图表（PP-Chart2Table，约 1.4 GB）默认不下载；
- 表格识别（PP-StructureV3 的 table_recognition_v2 子产线）同时用到 5 个模型。可选的只有无线表格的结构识别
  模型（TABLE 环节，默认 SLANet_plus）；有线表格结构 SLANeXt_wired、表格分类 PP-LCNet_x1_0_table_cls、
  两个单元格检测 RT-DETR 模型是附属模型（PADDLE_AUXILIARY，stage 为 None）：随引擎安装，不列在
  OcrEngineInfo.models 里，也不能单独删除；
- 识别模型的语言：PP-OCRv5 通用识别模型覆盖简体中文、繁体中文、英文与日文；其余按文字系统划分的专用模型
  覆盖的语言与 OCR 进程的 lst/workers/ocr_worker/languages.py 相同。

RapidOCR
========

id 写作 ``<OCR 版本>/<模型规格>/<语言>/<任务>``，取值组合来自 rapidocr 3.10.0 的 default_models.yaml
（onnxruntime 一节与 model_routes），下载地址与 SHA-256 也从这份文件抄录（RAPID_SOURCE），没有联网核对。

- PP-OCRv6 的检测与识别各有 tiny、small、medium 三档，是一个多语言模型（语言写作 multi）。tiny 档的识别模型
  不覆盖日文（model_routes 的 ppocrv6_tiny_langs）；
- PP-OCRv5 的检测有 mobile、server，识别有通用（ch）的 mobile、server 与按文字系统划分的 mobile 专用模型；
- rapidocr 的 wheel 自带三个模型（rapidocr/models）：PP-OCRv6_det_small.onnx、PP-OCRv6_rec_small.onnx、
  ch_ppocr_mobile_v2.0_cls_mobile.onnx，是默认组合，bundled=True，大小是 wheel 里文件的实际大小；
  其余模型大小未知（default_models.yaml 只记录地址与哈希），下载到 ``<模型目录>\ocr\rapid``；
- PP-OCRv5 的文本行方向模型（PP-LCNet textline_ori）输入尺寸是 80×160，rapidocr 3.10 的 Cls 按 48×192
  预处理（config.yaml 的 cls_image_shape），换上它会得到错误的结果，所以文本行方向环节只提供自带的模型。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from lst.contracts.ocr_engines import OcrStage

S = OcrStage


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """一个可下载（或随运行环境自带）的模型。

    - id：引擎内唯一；stage：所属环节，None 表示附属模型（不单独列出、随引擎安装、不能删除）；
    - size_bytes：下载大小，未知为 None；languages：识别模型覆盖的语言；
    - default：属于默认组合（安装引擎时一并下载）；bundled：随运行环境自带；
    - url、sha256、filename：单文件模型（RapidOCR）的下载地址、SHA-256 与保存的文件名；
      PaddleOCR 的模型包地址由 id 拼出，这三项为空。
    """

    id: str
    stage: OcrStage | None
    size_bytes: int | None = None
    name: str = ""
    languages: tuple[str, ...] = ()
    default: bool = False
    bundled: bool = False
    url: str = ""
    sha256: str = ""
    filename: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.id


def by_id(specs: Iterable[ModelSpec]) -> dict[str, ModelSpec]:
    return {spec.id: spec for spec in specs}


def stage_defaults(specs: Sequence[ModelSpec], defaults: dict[OcrStage, str]) -> dict[OcrStage, str]:
    """核对默认模型都在目录里，返回 defaults 本身。"""
    known = by_id(specs)
    for stage, model_id in defaults.items():
        spec = known.get(model_id)
        if spec is None or spec.stage is not stage:
            raise ValueError(f"环节 {stage.value} 的默认模型 {model_id} 不在目录里")
    return defaults


# ---------------------------------------------------------------- 语言

GENERAL_LANGUAGES: Final = ("zh-Hans", "zh-Hant", "en", "ja")
"""PP-OCRv5 通用识别模型（与 PP-OCRv6 多语言模型）覆盖的中英日。"""

LATIN_LANGUAGES: Final = (
    "af", "az", "bs", "cs", "cy", "da", "de", "es", "et", "fr", "ga", "hr", "hu", "id", "is", "it",
    "la", "lt", "lv", "mi", "ms", "mt", "nl", "nb", "nn", "oc", "pl", "pt", "ro", "sk", "sl",
    "sq", "sv", "sw", "tl", "tr", "uz", "vi", "fi", "eu", "gl", "lb", "rm", "ca", "qu", "sr",
)  # fmt: skip
"""PP-OCRv5 latin 识别模型覆盖的语言（lst/workers/ocr_worker/languages.py，no 规范写作 nb、nn）。"""

SCRIPT_LANGUAGES: Final[dict[str, tuple[str, ...]]] = {
    "korean": ("ko",),
    "th": ("th",),
    "el": ("el",),
    "te": ("te",),
    "ta": ("ta",),
    "eslav": ("ru", "be", "uk"),
    "cyrillic": ("bg", "mn", "kk", "ky", "tg", "mk", "tt", "cv", "ba", "kv", "os"),
    "arabic": ("ar", "fa", "ug", "ur", "ps", "sd", "ku"),
    "devanagari": ("hi", "mr", "ne", "bh", "mai", "sa", "bho", "gom"),
    "latin": LATIN_LANGUAGES,
    "en": ("en",),
}
"""PP-OCRv5 专用识别模型的文字系统 → 覆盖的语言。"""

V6_LATIN: Final = (
    "af", "az", "bs", "ca", "cs", "cy", "da", "de", "es", "et", "eu", "fi", "fr", "ga", "gl", "hr", "hu",
    "id", "is", "it", "ku", "la", "lb", "lt", "lv", "mi", "ms", "mt", "nl", "oc", "pl", "pt", "qu", "rm",
    "ro", "sk", "sl", "sq", "sv", "sw", "tl", "tr", "uz", "vi", "nb", "nn",
)  # fmt: skip
V6_LANGUAGES: Final = (*GENERAL_LANGUAGES, *V6_LATIN)
"""PP-OCRv6 small、medium 多语言识别模型覆盖的语言（model_routes 的 ppocrv6_langs）。"""
V6_TINY_LANGUAGES: Final = tuple(code for code in V6_LANGUAGES if code != "ja")
"""PP-OCRv6 tiny 识别模型覆盖的语言（ppocrv6_tiny_langs，没有 japan）。"""


# ---------------------------------------------------------------- PaddleOCR


def _paddle_rec(script: str) -> ModelSpec:
    return ModelSpec(
        f"{script}_PP-OCRv5_mobile_rec",
        S.RECOGNITION,
        None,
        f"PP-OCRv5 mobile（{script}）",
        SCRIPT_LANGUAGES[script],
    )


PADDLE_MODELS: Final[tuple[ModelSpec, ...]] = (
    ModelSpec("PP-OCRv5_mobile_det", S.DETECTION, 4_923_617, "PP-OCRv5 mobile", default=True),
    ModelSpec("PP-OCRv5_server_det", S.DETECTION, 88_336_270, "PP-OCRv5 server"),
    ModelSpec("PP-OCRv5_mobile_rec", S.RECOGNITION, 16_824_734, "PP-OCRv5 mobile", GENERAL_LANGUAGES,
              default=True),
    ModelSpec("PP-OCRv5_server_rec", S.RECOGNITION, 84_863_666, "PP-OCRv5 server", GENERAL_LANGUAGES),
    *(_paddle_rec(script) for script in SCRIPT_LANGUAGES),
    ModelSpec("PP-LCNet_x1_0_textline_ori", S.TEXTLINE_ORIENTATION, 6_849_052, "PP-LCNet x1.0", default=True),
    ModelSpec("PP-LCNet_x0_25_textline_ori", S.TEXTLINE_ORIENTATION, None, "PP-LCNet x0.25"),
    ModelSpec("PP-LCNet_x1_0_doc_ori", S.DOC_ORIENTATION, 6_859_373, "PP-LCNet x1.0", default=True),
    ModelSpec("UVDoc", S.DOC_UNWARPING, 32_245_627, "UVDoc", default=True),
    ModelSpec("PP-DocLayout_plus-L", S.LAYOUT, 130_391_437, "PP-DocLayout plus L", default=True),
    ModelSpec("PP-DocLayout-L", S.LAYOUT, 130_105_173, "PP-DocLayout L"),
    ModelSpec("PP-DocLayout-M", S.LAYOUT, None, "PP-DocLayout M"),
    ModelSpec("PP-DocLayout-S", S.LAYOUT, 5_146_359, "PP-DocLayout S"),
    ModelSpec("PP-DocBlockLayout", S.REGION, 129_924_055, "PP-DocBlockLayout", default=True),
    ModelSpec("SLANet_plus", S.TABLE, 7_984_210, "SLANet plus", default=True),
    ModelSpec("SLANeXt_wireless", S.TABLE, None, "SLANeXt wireless"),
    ModelSpec("PP-FormulaNet_plus-L", S.FORMULA, 731_493_173, "PP-FormulaNet plus L"),
    ModelSpec("PP-FormulaNet_plus-M", S.FORMULA, None, "PP-FormulaNet plus M"),
    ModelSpec("PP-FormulaNet_plus-S", S.FORMULA, None, "PP-FormulaNet plus S"),
    ModelSpec("PP-Chart2Table", S.CHART, 1_434_692_976, "PP-Chart2Table"),
)  # fmt: skip
"""PaddleOCR 的可选模型，按环节、再按推荐程度排列。"""

PADDLE_AUXILIARY: Final[tuple[ModelSpec, ...]] = (
    ModelSpec("PP-LCNet_x1_0_table_cls", None, 6_849_121, default=True),
    ModelSpec("SLANeXt_wired", None, 365_732_881, default=True),
    ModelSpec("RT-DETR-L_wired_table_cell_det", None, 129_924_066, default=True),
    ModelSpec("RT-DETR-L_wireless_table_cell_det", None, 129_924_069, default=True),
)
"""表格识别的附属模型：随引擎安装，不单独列出，不能删除（见模块说明）。"""

PADDLE_STAGE_DEFAULTS: Final = stage_defaults(
    PADDLE_MODELS,
    {
        S.DETECTION: "PP-OCRv5_mobile_det",
        S.RECOGNITION: "PP-OCRv5_mobile_rec",
        S.TEXTLINE_ORIENTATION: "PP-LCNet_x1_0_textline_ori",
        S.DOC_ORIENTATION: "PP-LCNet_x1_0_doc_ori",
        S.DOC_UNWARPING: "UVDoc",
        S.LAYOUT: "PP-DocLayout_plus-L",
        S.REGION: "PP-DocBlockLayout",
        S.TABLE: "SLANet_plus",
        S.FORMULA: "PP-FormulaNet_plus-L",
        S.CHART: "PP-Chart2Table",
    },
)
"""各环节的默认模型，与 lst/services/ocr/config.py 的 PADDLE_DEFAULT_MODELS 相同。"""

PADDLE_LANGUAGES: Final = tuple(dict.fromkeys(code for spec in PADDLE_MODELS for code in spec.languages))
"""PaddleOCR 能识别的语言：全部识别模型覆盖的语言之和（OCR 进程按识别语言自动换专用模型）。"""


# ---------------------------------------------------------------- RapidOCR

RAPID_SOURCE: Final = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx"
"""rapidocr 3.10.0 的 default_models.yaml 里 onnxruntime 模型的地址前缀。"""


def _rapid(
    model_id: str,
    stage: OcrStage,
    path: str,
    sha256: str,
    *,
    size: int | None = None,
    name: str = "",
    languages: tuple[str, ...] = (),
    bundled: bool = False,
) -> ModelSpec:
    return ModelSpec(
        model_id,
        stage,
        size,
        name,
        languages,
        default=bundled,
        bundled=bundled,
        url=f"{RAPID_SOURCE}/{path}",
        sha256=sha256,
        filename=path.rsplit("/", 1)[-1],
    )


def _rapid_v5_rec(lang: str, sha256: str) -> ModelSpec:
    languages = GENERAL_LANGUAGES if lang == "ch" else SCRIPT_LANGUAGES[lang]
    return _rapid(
        f"PP-OCRv5/mobile/{lang}/rec",
        S.RECOGNITION,
        f"PP-OCRv5/rec/{lang}_PP-OCRv5_rec_mobile.onnx",
        sha256,
        name=f"PP-OCRv5 mobile（{lang}）",
        languages=languages,
    )


RAPID_MODELS: Final[tuple[ModelSpec, ...]] = (
    _rapid("PP-OCRv6/small/multi/det", S.DETECTION, "PP-OCRv6/det/PP-OCRv6_det_small.onnx",
           "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
           size=9_929_594, name="PP-OCRv6 small", bundled=True),
    _rapid("PP-OCRv6/tiny/multi/det", S.DETECTION, "PP-OCRv6/det/PP-OCRv6_det_tiny.onnx",
           "f42c0fbd294d95eac1a550e131b277dac97462c8025fa4b6c3cec1b7894bd3d5", name="PP-OCRv6 tiny"),
    _rapid("PP-OCRv6/medium/multi/det", S.DETECTION, "PP-OCRv6/det/PP-OCRv6_det_medium.onnx",
           "92078b7355007ccfffcd4c8cd441a3afd4538904d06881b29a155e1e679907c2", name="PP-OCRv6 medium"),
    _rapid("PP-OCRv5/mobile/ch/det", S.DETECTION, "PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx",
           "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae", name="PP-OCRv5 mobile"),
    _rapid("PP-OCRv5/server/ch/det", S.DETECTION, "PP-OCRv5/det/ch_PP-OCRv5_det_server.onnx",
           "0f8846b1d4bba223a2a2f9d9b44022fbc22cc019051a602b41a7fda9667e4cad", name="PP-OCRv5 server"),
    _rapid("PP-OCRv6/small/multi/rec", S.RECOGNITION, "PP-OCRv6/rec/PP-OCRv6_rec_small.onnx",
           "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
           size=21_234_383, name="PP-OCRv6 small", languages=V6_LANGUAGES, bundled=True),
    _rapid("PP-OCRv6/tiny/multi/rec", S.RECOGNITION, "PP-OCRv6/rec/PP-OCRv6_rec_tiny.onnx",
           "e16e242de5937ad92609223f19bc2aff3727ee40b095f996907c24749bad251b",
           name="PP-OCRv6 tiny", languages=V6_TINY_LANGUAGES),
    _rapid("PP-OCRv6/medium/multi/rec", S.RECOGNITION, "PP-OCRv6/rec/PP-OCRv6_rec_medium.onnx",
           "eef444829dbbe18d7fea59a3f6eb75647518d2b3a9568d27c92e42940204894b",
           name="PP-OCRv6 medium", languages=V6_LANGUAGES),
    _rapid_v5_rec("ch", "5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5"),
    _rapid("PP-OCRv5/server/ch/rec", S.RECOGNITION, "PP-OCRv5/rec/ch_PP-OCRv5_rec_server.onnx",
           "e09385400eaaaef34ceff54aeb7c4f0f1fe014c27fa8b9905d4709b65746562a",
           name="PP-OCRv5 server", languages=GENERAL_LANGUAGES),
    _rapid_v5_rec("en", "c3461add59bb4323ecba96a492ab75e06dda42467c9e3d0c18db5d1d21924be8"),
    _rapid_v5_rec("korean", "cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b"),
    _rapid_v5_rec("latin", "b20bd37c168a570f583afbc8cd7925603890efbcdc000a59e22c269d160b5f5a"),
    _rapid_v5_rec("eslav", "08705d6721849b1347d26187f15a5e362c431963a2a62bfff4feac578c489aab"),
    _rapid_v5_rec("cyrillic", "90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711"),
    _rapid_v5_rec("th", "de541dd83161c241ff426f7ecfd602a0ba77d686cf3ab9a6c255ea82fd08006e"),
    _rapid_v5_rec("el", "b4368bccd557123c702b7549fee6cd1e94b581337d1c9b65310f109131542b7f"),
    _rapid_v5_rec("arabic", "c1192e632d0baa9146ae5b756a0e635e3dc63c1733737ebfd1629e87144e9295"),
    _rapid_v5_rec("devanagari", "d6f0a906580e3fa6b324a318718f1f31f268b6ea8ef985f91c2012a37f52c91e"),
    _rapid_v5_rec("ta", "a42448808b7dea87597336f12438935f40353f1949e8360acd9e06b4da21bfe1"),
    _rapid_v5_rec("te", "a3690451b50028a09a3316a1274f7c05728151ea3f8fd392696397a7fefcbd92"),
    _rapid("PP-OCRv4/mobile/ch/cls", S.TEXTLINE_ORIENTATION,
           "PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
           "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
           size=585_532, name="PP-OCR mobile v2.0", bundled=True),
)  # fmt: skip
"""RapidOCR 的可选模型，按环节、再按推荐程度排列。地址与哈希来自 rapidocr 3.10.0 的 default_models.yaml。"""

RAPID_STAGE_DEFAULTS: Final = stage_defaults(
    RAPID_MODELS,
    {
        S.DETECTION: "PP-OCRv6/small/multi/det",
        S.RECOGNITION: "PP-OCRv6/small/multi/rec",
        S.TEXTLINE_ORIENTATION: "PP-OCRv4/mobile/ch/cls",
    },
)
"""各环节的默认模型，与 lst/services/ocr/config.py 的 RAPID_DEFAULT_* 相同。"""

RAPID_BUNDLED_FILES: Final = tuple(spec.filename for spec in RAPID_MODELS if spec.bundled)
"""wheel 自带的模型文件名（rapidocr/models 下）。"""
