"""PaddleX 产线配置的构造：子能力开关与裁剪、模型替换、模型目录检查、CPU 上按模型选择 oneDNN。

BASE_STRUCTURE、BASE_OCR 按 PaddleX 3.7 的 PP-StructureV3.yaml、OCR.yaml 的结构节选，
只保留与本模块有关的键。
真实默认配置由 test_paddle_process.py 在 OCR 环境中走一遍。
"""

import copy
from pathlib import Path
from typing import cast

import pytest

from lst.workers._runtime import ConfigError
from lst.workers.ocr_worker.backend import ModelMissing
from lst.workers.ocr_worker.paddle_config import (
    ONEDNN_MODELS,
    Config,
    attach_models,
    cpu_engine_config,
    model_names,
    structure_config,
    text_config,
)
from lst.workers.ocr_worker.settings import OcrSettings
from tests.workers.ocr.harness import OFFICIAL_MODELS


def _ocr(det: str, rec: str) -> Config:
    return {
        "pipeline_name": "OCR",
        "use_textline_orientation": True,
        "SubModules": {
            "TextDetection": {"model_name": det, "model_dir": None},
            "TextLineOrientation": {"model_name": "PP-LCNet_x1_0_textline_ori", "model_dir": None},
            "TextRecognition": {"model_name": rec, "model_dir": None},
        },
    }


def _preprocessor() -> Config:
    return {
        "use_doc_orientation_classify": True,
        "use_doc_unwarping": True,
        "SubModules": {
            "DocOrientationClassify": {"model_name": "PP-LCNet_x1_0_doc_ori", "model_dir": None},
            "DocUnwarping": {"model_name": "UVDoc", "model_dir": None},
        },
    }


BASE_STRUCTURE: Config = {
    "pipeline_name": "PP-StructureV3",
    "use_doc_preprocessor": False,
    "use_seal_recognition": False,
    "use_table_recognition": True,
    "use_formula_recognition": True,
    "use_chart_recognition": False,
    "use_region_detection": True,
    "SubModules": {
        "LayoutDetection": {"model_name": "PP-DocLayout_plus-L", "model_dir": None},
        "ChartRecognition": {"model_name": "PP-Chart2Table", "model_dir": None},
        "RegionDetection": {"model_name": "PP-DocBlockLayout", "model_dir": None},
    },
    "SubPipelines": {
        "DocPreprocessor": _preprocessor(),
        "GeneralOCR": _ocr("PP-OCRv5_server_det", "PP-OCRv5_server_rec"),
        "TableRecognition": {
            "use_ocr_model": False,
            "SubModules": {
                "TableClassification": {"model_name": "PP-LCNet_x1_0_table_cls"},
                "WiredTableStructureRecognition": {"model_name": "SLANeXt_wired"},
                "WirelessTableStructureRecognition": {"model_name": "SLANet_plus"},
                "WiredTableCellsDetection": {"model_name": "RT-DETR-L_wired_table_cell_det"},
                "WirelessTableCellsDetection": {"model_name": "RT-DETR-L_wireless_table_cell_det"},
                "TableOrientationClassify": {"model_name": "PP-LCNet_x1_0_doc_ori"},
            },
            "SubPipelines": {"GeneralOCR": _ocr("PP-OCRv5_server_det", "PP-OCRv5_server_rec")},
        },
        "SealRecognition": {
            "SubPipelines": {"SealOCR": _ocr("PP-OCRv4_server_seal_det", "PP-OCRv5_server_rec")}
        },
        "FormulaRecognition": {"SubModules": {"FormulaRecognition": {"model_name": "PP-FormulaNet_plus-L"}}},
    },
}

BASE_OCR: Config = {
    **_ocr("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"),
    "use_doc_preprocessor": True,
    "SubPipelines": {"DocPreprocessor": _preprocessor()},
}

SETTINGS = OcrSettings.parse({"model_cache_dir": "unused"})


def sub(config: Config, *path: str) -> Config:
    node = config
    for key in path:
        child = node[key]
        assert isinstance(child, dict)
        node = cast(Config, child)
    return node


def test_default_structure_features() -> None:
    config = structure_config(BASE_STRUCTURE, SETTINGS, frozenset({"table"}))
    assert BASE_STRUCTURE["use_formula_recognition"] is True, "不能改动传入的默认配置"
    assert (config["use_table_recognition"], config["use_formula_recognition"]) == (True, False)
    assert (config["use_chart_recognition"], config["use_seal_recognition"]) == (False, False)
    assert config["use_doc_preprocessor"] is False
    assert config["use_doc_orientation_classify"] is False and config["use_doc_unwarping"] is False
    pipelines = sub(config, "SubPipelines")
    assert set(pipelines) == {"GeneralOCR", "TableRecognition"}
    assert "ChartRecognition" not in sub(config, "SubModules")
    assert model_names(config) == [
        "PP-DocLayout_plus-L",
        "PP-DocBlockLayout",
        "PP-OCRv5_mobile_det",
        "PP-OCRv5_mobile_rec",
        "PP-LCNet_x1_0_table_cls",
        "SLANeXt_wired",
        "SLANet_plus",
        "RT-DETR-L_wired_table_cell_det",
        "RT-DETR-L_wireless_table_cell_det",
        "PP-LCNet_x1_0_doc_ori",
    ]


def test_ocr_models_replaced_in_both_ocr_configs() -> None:
    config = structure_config(BASE_STRUCTURE, SETTINGS, frozenset({"table"}))
    for path in (
        ("SubPipelines", "GeneralOCR"),
        ("SubPipelines", "TableRecognition", "SubPipelines", "GeneralOCR"),
    ):
        ocr = sub(config, *path)
        assert ocr["use_textline_orientation"] is False
        modules = sub(ocr, "SubModules")
        assert set(modules) == {"TextDetection", "TextRecognition"}
        assert sub(modules, "TextDetection")["model_name"] == "PP-OCRv5_mobile_det"
        assert sub(modules, "TextRecognition")["model_name"] == "PP-OCRv5_mobile_rec"


def test_all_structure_features() -> None:
    features = frozenset(
        {"table", "formula", "chart", "doc_orientation", "doc_unwarping", "textline_orientation"}
    )
    config = structure_config(BASE_STRUCTURE, SETTINGS, features)
    assert config["use_doc_preprocessor"] is True
    preprocessor = sub(config, "SubPipelines", "DocPreprocessor")
    assert set(sub(preprocessor, "SubModules")) == {"DocOrientationClassify", "DocUnwarping"}
    assert "FormulaRecognition" in sub(config, "SubPipelines")
    assert "ChartRecognition" in sub(config, "SubModules")
    assert "SealRecognition" not in sub(config, "SubPipelines")
    assert sub(config, "SubPipelines", "GeneralOCR")["use_textline_orientation"] is True
    assert "PP-LCNet_x1_0_textline_ori" in model_names(config)


def test_only_orientation_keeps_only_its_module() -> None:
    config = structure_config(BASE_STRUCTURE, SETTINGS, frozenset({"doc_orientation"}))
    preprocessor = sub(config, "SubPipelines", "DocPreprocessor")
    assert (preprocessor["use_doc_orientation_classify"], preprocessor["use_doc_unwarping"]) == (True, False)
    assert set(sub(preprocessor, "SubModules")) == {"DocOrientationClassify"}
    assert "TableRecognition" not in sub(config, "SubPipelines")
    assert "UVDoc" not in model_names(config)


def test_layout_model_from_settings() -> None:
    settings = OcrSettings.parse({"layout_model": "PP-DocLayout-S", "det_model": "PP-OCRv3_mobile_det"})
    config = structure_config(BASE_STRUCTURE, settings, frozenset())
    assert sub(config, "SubModules", "LayoutDetection")["model_name"] == "PP-DocLayout-S"
    assert model_names(config) == [
        "PP-DocLayout-S",
        "PP-DocBlockLayout",
        "PP-OCRv3_mobile_det",
        "PP-OCRv5_mobile_rec",
    ]


def test_text_config() -> None:
    config = text_config(BASE_OCR, SETTINGS, frozenset())
    assert config["use_doc_preprocessor"] is False and config["use_textline_orientation"] is False
    assert "DocPreprocessor" not in sub(config, "SubPipelines")
    assert model_names(config) == ["PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec"]
    rotated = text_config(BASE_OCR, SETTINGS, frozenset({"doc_unwarping", "textline_orientation"}))
    assert set(model_names(rotated)) == {
        "UVDoc",
        "PP-OCRv5_mobile_det",
        "PP-LCNet_x1_0_textline_ori",
        "PP-OCRv5_mobile_rec",
    }


def test_missing_paddlex_structure_is_a_config_error() -> None:
    broken = copy.deepcopy(BASE_STRUCTURE)
    del sub(broken, "SubPipelines")["GeneralOCR"]
    with pytest.raises(ConfigError, match="GeneralOCR"):
        structure_config(broken, SETTINGS, frozenset())


# ---------------------------------------------------------------- 模型目录与运行方式


def test_missing_models_are_listed_without_download() -> None:
    config = structure_config(BASE_STRUCTURE, SETTINGS, frozenset({"table"}))
    with pytest.raises(ConfigError) as caught:
        attach_models(config, Path(__file__).parent / "no-such-models", 8)
    assert isinstance(caught.value, ModelMissing)
    message = str(caught.value)
    assert "PP-DocLayout_plus-L" in message and "RT-DETR-L_wireless_table_cell_det" in message


@pytest.mark.skipif(not OFFICIAL_MODELS.is_dir(), reason="本机没有 PaddleX 模型缓存")
def test_attach_local_models_and_cpu_engine() -> None:
    config = structure_config(BASE_STRUCTURE, SETTINGS, frozenset({"table", "formula"}))
    names = attach_models(config, OFFICIAL_MODELS, 6)
    assert "PP-FormulaNet_plus-L" in names
    layout = sub(config, "SubModules", "LayoutDetection")
    assert layout["model_dir"] == str(OFFICIAL_MODELS / "PP-DocLayout_plus-L")
    assert layout["engine_config"] == cpu_engine_config("PP-DocLayout_plus-L", 6)
    rec = sub(config, "SubPipelines", "GeneralOCR", "SubModules", "TextRecognition")
    assert sub(rec, "engine_config", "paddle_static")["run_mode"] == "mkldnn"
    assert sub(layout, "engine_config", "paddle_static")["run_mode"] == "paddle"


@pytest.mark.skipif(not OFFICIAL_MODELS.is_dir(), reason="本机没有 PaddleX 模型缓存")
def test_gpu_leaves_engine_config_to_paddlex() -> None:
    config = text_config(BASE_OCR, SETTINGS, frozenset())
    attach_models(config, OFFICIAL_MODELS, None)
    assert "engine_config" not in sub(config, "SubModules", "TextDetection")


def test_cpu_engine_config_is_complete() -> None:
    assert cpu_engine_config("PP-OCRv5_mobile_rec", 8) == {
        "paddle_static": {
            "run_mode": "mkldnn",
            "cpu_threads": 8,
            "mkldnn_cache_capacity": 10,
            "enable_cinn": False,
        }
    }
    assert sub(cpu_engine_config("PP-OCRv5_mobile_det", 8), "paddle_static")["run_mode"] == "paddle"
    assert "PP-OCRv5_mobile_det" not in ONEDNN_MODELS


def test_onednn_off_runs_every_model_the_plain_way() -> None:
    """enable_mkldnn 为 false 时（旧版 pp_structure_ocr_enable_mkldnn 的对应项）全部模型用 paddle。"""
    for name in ("PP-OCRv5_mobile_rec", "PP-LCNet_x1_0_doc_ori", "PP-OCRv5_mobile_det"):
        assert sub(cpu_engine_config(name, 8, onednn=False), "paddle_static")["run_mode"] == "paddle"
    enabled = cpu_engine_config("PP-OCRv5_mobile_rec", 8, onednn=True)
    assert sub(enabled, "paddle_static")["run_mode"] == "mkldnn"


def test_every_stage_model_comes_from_settings() -> None:
    """[ocr_paddle] 的 <环节>_model 字段经 Configure 进入产线配置（OCRE-08）。"""
    settings = OcrSettings.parse(
        {
            "model_cache_dir": "unused",
            "detection_model": "PP-OCRv5_server_det",
            "recognition_model": "PP-OCRv5_server_rec",
            "layout_model": "PP-DocLayout-S",
            "region_model": "PP-DocBlockLayout",
            "table_model": "SLANeXt_wireless",
            "formula_model": "PP-FormulaNet_plus-S",
            "chart_model": "PP-Chart2Table",
            "textline_orientation_model": "PP-LCNet_x0_25_textline_ori",
            "doc_orientation_model": "PP-LCNet_x1_0_doc_ori",
            "doc_unwarping_model": "UVDoc",
        }
    )
    features = frozenset(
        {"table", "formula", "chart", "doc_orientation", "doc_unwarping", "textline_orientation"}
    )
    config = structure_config(BASE_STRUCTURE, settings, features)
    ocr = sub(config, "SubPipelines", "GeneralOCR", "SubModules")
    assert sub(ocr, "TextDetection")["model_name"] == "PP-OCRv5_server_det"
    assert sub(ocr, "TextRecognition")["model_name"] == "PP-OCRv5_server_rec"
    assert sub(ocr, "TextLineOrientation")["model_name"] == "PP-LCNet_x0_25_textline_ori"
    assert sub(config, "SubModules", "LayoutDetection")["model_name"] == "PP-DocLayout-S"
    assert sub(config, "SubModules", "RegionDetection")["model_name"] == "PP-DocBlockLayout"
    table = sub(config, "SubPipelines", "TableRecognition", "SubModules")
    assert sub(table, "WirelessTableStructureRecognition")["model_name"] == "SLANeXt_wireless"
    assert sub(table, "WiredTableStructureRecognition")["model_name"] == "SLANeXt_wired"
    formula = sub(config, "SubPipelines", "FormulaRecognition", "SubModules", "FormulaRecognition")
    assert formula["model_name"] == "PP-FormulaNet_plus-S"
    names = model_names(config)
    assert "PP-OCRv5_server_det" in names and "SLANet_plus" not in names


def test_legacy_det_model_key_still_works() -> None:
    settings = OcrSettings.parse({"model_cache_dir": "unused", "det_model": "PP-OCRv3_mobile_det"})
    assert settings.det_model == "PP-OCRv3_mobile_det"
    settings = OcrSettings.parse(
        {
            "model_cache_dir": "unused",
            "det_model": "PP-OCRv3_mobile_det",
            "detection_model": "PP-OCRv5_server_det",
        }
    )
    assert settings.det_model == "PP-OCRv5_server_det"
