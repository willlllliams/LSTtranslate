"""PaddleX 产线配置的构造。只做字典操作，不导入 paddlex，主环境可以测试。

输入是 PaddleX 自带的默认配置（paddlex.inference.load_pipeline_config("PP-StructureV3") 或 "OCR"），
在它的副本上做三件事：

1. 子能力：按要开启的子能力设置 use_* 开关，并删掉关闭的子能力对应的子产线和子模块。PaddleX 只在
   开关为真时才创建子模块，删掉配置是为了让第 3 步只检查真正要加载的模型；印章识别总是关闭
   （默认的印章检测模型不在模型目录里，需求也不要求印章）。
2. 模型：版面检测、文本检测、文本识别换成设置里的模型。表格子产线里还有一份 OCR 配置，PaddleX 只在
   检测到表格被旋转时才临时创建它，这里也换成同样的模型，避免它去加载另一套。
3. 模型目录：给配置里出现的每个 model_name 写上 model_dir = <模型缓存>/official_models/<模型名>，
   目录不存在就抛 ConfigError（列出全部缺失的模型）。PaddleX 拿到 model_dir 就直接加载，不经过下载逻辑。

oneDNN：PaddlePaddle 3.3.1 的 CPU 版在 oneDNN（MKLDNN）下运行含插值算子的模型会报
“ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<pir::DoubleAttribute>]”，
本机实测 PP-OCRv5/PP-OCRv3 的文本检测、PP-DocLayout_plus-L、PP-DocBlockLayout、SLANet_plus、
RT-DETR 单元格检测、UVDoc 都会失败；PP-OCRv5 mobile/server 与 PP-OCRv3 mobile 的文本识别、三个 PP-LCNet
分类模型正常。全部关闭 oneDNN 时 1920×1080 的文本识别要 9 秒左右，只给能用的模型开 oneDNN 后约 2.5 秒。
所以设备是 CPU 时，每个模型单独配置运行方式：ONEDNN_MODELS 里的用 mkldnn，其余用 paddle。
PaddleX 的模型级 engine_config 会整体替换产线级的 paddle_static 一节，所以每个模型都写全这一节。
"""

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Final, cast

from lst.workers._runtime import ConfigError
from lst.workers.ocr_worker.backend import ModelMissing
from lst.workers.ocr_worker.settings import OcrSettings

type Config = dict[str, object]

ONEDNN_MODELS: Final = frozenset(
    {
        "PP-OCRv5_mobile_rec",
        "PP-OCRv5_server_rec",
        "PP-OCRv3_mobile_rec",
        "PP-LCNet_x1_0_textline_ori",
        "PP-LCNet_x1_0_doc_ori",
        "PP-LCNet_x1_0_table_cls",
    }
)
"""在 PaddlePaddle 3.3.1 CPU 版的 oneDNN 下实测能运行的模型，见模块说明。"""

MKLDNN_CACHE_CAPACITY: Final = 10


def _as_config(value: object) -> Config | None:
    return cast(Config, value) if isinstance(value, dict) else None


def _child(node: Config, *path: str) -> Config:
    """沿路径取嵌套的配置字典；不存在时抛 ConfigError（说明 PaddleX 的默认配置结构变了）。"""
    current = node
    for key in path:
        child = _as_config(current.get(key))
        if child is None:
            raise ConfigError(f"PaddleX 默认配置中缺少 {'.'.join(path)}，或它不是字典")
        current = child
    return current


def _configure_ocr(ocr: Config, settings: OcrSettings, textline: bool) -> None:
    """一份 OCR 子产线配置：换检测、识别模型，文本行方向分类按开关保留或删除。"""
    ocr["use_textline_orientation"] = textline
    modules = _child(ocr, "SubModules")
    _child(modules, "TextDetection")["model_name"] = settings.det_model
    _child(modules, "TextRecognition")["model_name"] = settings.rec_model
    if not textline:
        modules.pop("TextLineOrientation", None)


def _configure_doc_preprocessor(owner: Config, features: frozenset[str]) -> None:
    """文档方向校正与去畸变：都关时删掉整个子产线，否则只保留开着的子模块。"""
    orientation = "doc_orientation" in features
    unwarping = "doc_unwarping" in features
    owner["use_doc_preprocessor"] = orientation or unwarping
    owner["use_doc_orientation_classify"] = orientation
    owner["use_doc_unwarping"] = unwarping
    sub_pipelines = _child(owner, "SubPipelines")
    if not (orientation or unwarping):
        sub_pipelines.pop("DocPreprocessor", None)
        return
    preprocessor = _child(sub_pipelines, "DocPreprocessor")
    preprocessor["use_doc_orientation_classify"] = orientation
    preprocessor["use_doc_unwarping"] = unwarping
    modules = _child(preprocessor, "SubModules")
    if not orientation:
        modules.pop("DocOrientationClassify", None)
    if not unwarping:
        modules.pop("DocUnwarping", None)


def structure_config(base: Config, settings: OcrSettings, features: frozenset[str]) -> Config:
    """PP-StructureV3 的配置，开启 features 中的子能力。"""
    config = copy.deepcopy(base)
    table, formula, chart = "table" in features, "formula" in features, "chart" in features
    textline = "textline_orientation" in features
    config.update(
        use_seal_recognition=False,
        use_table_recognition=table,
        use_formula_recognition=formula,
        use_chart_recognition=chart,
        use_region_detection=True,
    )
    _configure_doc_preprocessor(config, features)
    sub_pipelines = _child(config, "SubPipelines")
    sub_modules = _child(config, "SubModules")
    _child(sub_modules, "LayoutDetection")["model_name"] = settings.layout_model
    _configure_ocr(_child(sub_pipelines, "GeneralOCR"), settings, textline)
    sub_pipelines.pop("SealRecognition", None)
    if table:
        _configure_ocr(
            _child(sub_pipelines, "TableRecognition", "SubPipelines", "GeneralOCR"), settings, textline
        )
    else:
        sub_pipelines.pop("TableRecognition", None)
    if not formula:
        sub_pipelines.pop("FormulaRecognition", None)
    if not chart:
        sub_modules.pop("ChartRecognition", None)
    return config


def text_config(base: Config, settings: OcrSettings, features: frozenset[str]) -> Config:
    """OCR 产线（文本模式）的配置。"""
    config = copy.deepcopy(base)
    _configure_doc_preprocessor(config, features)
    _configure_ocr(config, settings, "textline_orientation" in features)
    return config


def _model_nodes(node: object) -> Iterator[Config]:
    """配置中所有带 model_name 的字典（子模块）。"""
    if isinstance(node, dict):
        mapping = cast(Config, node)
        if isinstance(mapping.get("model_name"), str):
            yield mapping
        for value in mapping.values():
            yield from _model_nodes(value)
    elif isinstance(node, list):
        for item in cast(list[object], node):
            yield from _model_nodes(item)


def model_names(config: Config) -> list[str]:
    """配置中用到的模型名，去重并保持出现顺序。"""
    names: list[str] = []
    for node in _model_nodes(config):
        name = cast(str, node["model_name"])
        if name not in names:
            names.append(name)
    return names


def cpu_engine_config(model_name: str, cpu_threads: int) -> Config:
    """CPU 上一个模型的运行方式，见模块说明的 oneDNN 一段。"""
    run_mode = "mkldnn" if model_name in ONEDNN_MODELS else "paddle"
    return {
        "paddle_static": {
            "run_mode": run_mode,
            "cpu_threads": cpu_threads,
            "mkldnn_cache_capacity": MKLDNN_CACHE_CAPACITY,
            "enable_cinn": False,
        }
    }


def attach_models(config: Config, models_dir: Path, cpu_threads: int | None) -> list[str]:
    """给每个模型写上本地目录；cpu_threads 不为 None（设备是 CPU）时同时写上运行方式。

    返回用到的模型名。任何一个模型目录不存在时抛 ModelMissing（ConfigError 的子类），不会让 PaddleX 去下载。
    """
    missing: list[str] = []
    for node in _model_nodes(config):
        name = cast(str, node["model_name"])
        directory = models_dir / name
        if not directory.is_dir():
            if name not in missing:
                missing.append(name)
            continue
        node["model_dir"] = str(directory)
        if cpu_threads is not None:
            node["engine_config"] = cpu_engine_config(name, cpu_threads)
    if missing:
        raise ModelMissing(f"模型目录 {models_dir} 中缺少：{', '.join(missing)}")
    return model_names(config)
