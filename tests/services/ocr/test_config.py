"""[ocr] 配置节的测试：默认值、校验、OCR 引擎与实际生效的模式、处理时限分档、请求 options
与决定重载的 process_key。"""

import json
from typing import cast

import pytest
from pydantic import ValidationError

from lst.contracts.ocr_engines import OcrEngineKind, OcrStage
from lst.services.ocr import (
    CONFIG_SECTIONS,
    PIPELINE_SECTIONS,
    NormalizeConfig,
    OcrConfig,
    OcrDevice,
    OcrMode,
    PaddleOcrConfig,
    PipelineConfig,
    RapidOcrConfig,
    WindowsOcrConfig,
    effective_mode,
    process_key,
    processing_timeout,
    request_options,
    worker_settings,
)


def test_paddle_defaults_match_the_pre_rewrite_tuning() -> None:
    """PaddleOCR 的默认参数与重构前（LingZero，私有仓库 myocr）的 PP-StructureV3 调参一致。

    旧版 src/ocr/pp_structure_ocr.py:89-139 创建 PPStructureV3，用 src/config/config_manager.py:28-60
    与已部署的 config/config.ini:19-52 的取值：
    - 模型：版面 PP-DocLayout_plus-L、检测与识别 PP-OCRv5_mobile、区域 PP-DocBlockLayout、
      无线表格 SLANet_plus、公式 PP-FormulaNet_plus-L、图表 PP-Chart2Table（config.ini:20-33）；
    - 子能力：表格、公式、图表关，区域检测开，文档方向、去畸变、文本行方向关（config.ini:46-52）；
    - 检测与识别的阈值、版面阈值（config.ini:34-43）全部留空，即传 None 走产线默认，也就是 paddlex 3.4.1
      自带的 PP-StructureV3.yaml 的取值：thresh 0.3、box_thresh 0.6、unclip_ratio 1.5、limit_side_len 736、
      limit_type min、score_thresh 0.0、layout_nms 真、layout_unclip_ratio 1.0、各类别自带的版面阈值与合并方式
      （用 0 与 "default" 表示）；
    - CPU 线程 8、启用 MKLDNN（config.ini:45、19）；印章识别没有传，yaml 里是关闭。
    """
    ocr, paddle = OcrConfig(), PaddleOcrConfig()
    assert (ocr.table, ocr.formula, ocr.chart, ocr.region) == (False, False, False, True)
    assert (ocr.doc_orientation, ocr.doc_unwarping, ocr.textline_orientation) == (False, False, False)
    assert paddle.selected_models() == {
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
    assert (paddle.text_det_limit_side_len, paddle.text_det_limit_type) == (0, "default")
    det = (paddle.text_det_thresh, paddle.text_det_box_thresh, paddle.text_det_unclip_ratio)
    assert det == (0.3, 0.6, 1.5)
    assert paddle.text_rec_score_thresh == 0.0
    assert (paddle.layout_threshold, paddle.layout_nms, paddle.layout_unclip_ratio) == (0.0, True, 1.0)
    assert paddle.layout_merge_bboxes_mode == "default"
    assert (paddle.cpu_threads, paddle.enable_mkldnn) == (0, True)
    assert "enable_mkldnn" in paddle.reload_fields()


def test_defaults() -> None:
    config = OcrConfig()
    assert config.engine is OcrEngineKind.WINDOWS
    assert config.mode is OcrMode.STRUCTURE
    assert effective_mode(config) is OcrMode.TEXT
    assert config.device is OcrDevice.AUTO
    assert config.language == "auto"
    assert (config.table, config.formula, config.chart) == (False, False, False)
    assert (config.doc_orientation, config.doc_unwarping, config.textline_orientation) == (
        False,
        False,
        False,
    )
    assert (config.grayscale, config.resize_max_side) == (False, 4000)


def test_config_sections_register_all_ocr_sections() -> None:
    assert set(CONFIG_SECTIONS) == {"ocr", "ocr_windows", "ocr_rapid", "ocr_paddle", "ocr_normalize"}
    assert CONFIG_SECTIONS["ocr"] is OcrConfig


def test_parses_a_toml_like_section() -> None:
    config = OcrConfig.model_validate(
        {"engine": "paddle", "mode": "text", "device": "gpu", "table": False, "grayscale": True}
    )
    assert (config.mode, config.device, config.table, config.grayscale) == (
        OcrMode.TEXT,
        OcrDevice.GPU,
        False,
        True,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("auto", "auto"), ("AUTO", "auto"), ("zh-CN", "zh-Hans"), ("EN", "en"), ("ko", "ko")],
)
def test_language_is_normalised(raw: str, expected: str) -> None:
    assert OcrConfig(language=raw).language == expected


@pytest.mark.parametrize(
    "raw",
    [
        {"language": "not a language"},
        {"language": ""},
        {"mode": "layout"},
        {"engine": "tesseract"},
        {"device": "tpu"},
        {"resize_max_side": -1},
        {"resize_max_side": 100},
        {"resize_max_side": 20000},
        {"text_timeout_s": 0},
        {"unknown": True},
    ],
)
def test_invalid_values_are_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OcrConfig.model_validate(raw)


@pytest.mark.parametrize("value", [0, 256, 4000, 16384])
def test_resize_limits(value: int) -> None:
    assert OcrConfig(resize_max_side=value).resize_max_side == value


@pytest.mark.parametrize(
    ("raw", "mode"),
    [
        ({}, OcrMode.TEXT),
        ({"mode": "structure"}, OcrMode.TEXT),
        ({"engine": "windows", "mode": "text"}, OcrMode.TEXT),
        ({"engine": "paddle"}, OcrMode.STRUCTURE),
        ({"engine": "paddle", "mode": "text"}, OcrMode.TEXT),
    ],
)
def test_windows_engine_always_runs_in_text_mode(raw: dict[str, object], mode: OcrMode) -> None:
    config = OcrConfig.model_validate(raw)
    assert effective_mode(config) is mode
    assert request_options(config)["mode"] == mode.value


@pytest.mark.parametrize(
    ("fields", "seconds"),
    [
        ({"mode": "text"}, 30),
        ({"mode": "text", "table": True, "formula": True, "chart": True}, 30),
        ({}, 60),  # 版面模式，默认不开任何耗时的子能力
        ({"table": False}, 60),
        ({"formula": True}, 120),
        ({"table": True, "formula": True, "chart": True}, 240),
        ({"doc_orientation": True, "doc_unwarping": True, "textline_orientation": True, "table": False}, 60),
        ({"structure_timeout_s": 90, "feature_timeout_s": 30, "chart": True}, 120),
        ({"mode": "text", "text_timeout_s": 12.5}, 12.5),
    ],
)
def test_processing_timeout_by_mode_and_features(fields: dict[str, object], seconds: float) -> None:
    """PaddleOCR：时限按模式和子能力分档。"""
    assert processing_timeout(OcrConfig.model_validate({"engine": "paddle", **fields})) == seconds


@pytest.mark.parametrize(
    ("fields", "seconds"),
    [
        ({}, 30),
        ({"mode": "structure", "table": True, "formula": True, "chart": True}, 30),
        ({"text_timeout_s": 12.5, "structure_timeout_s": 90}, 12.5),
    ],
)
def test_windows_engine_uses_the_text_timeout(fields: dict[str, object], seconds: float) -> None:
    assert processing_timeout(OcrConfig.model_validate({"engine": "windows", **fields})) == seconds


def test_request_options_carry_recognition_and_preprocessing_fields() -> None:
    config = OcrConfig.model_validate(
        {
            "mode": "text",
            "language": "ja",
            "formula": True,
            "grayscale": True,
            "resize_max_side": 2000,
            "device": "cpu",
        }
    )
    options = request_options(config)
    assert options == {
        "mode": "text",
        "language": "ja",
        "table": False,
        "formula": True,
        "chart": False,
        "region": True,
        "doc_orientation": False,
        "doc_unwarping": False,
        "textline_orientation": False,
        "grayscale": True,
        "resize_max_side": 2000,
    }
    json.dumps(options)  # 必须能原样放进 JSON 消息


def test_worker_settings_add_the_device_engine_and_reload_fields() -> None:
    config = OcrConfig(engine=OcrEngineKind.PADDLE, device=OcrDevice.GPU)
    pipeline = PaddleOcrConfig()
    reload = {name: getattr(pipeline, name) for name in PaddleOcrConfig.reload_fields()}
    assert worker_settings(config) == {
        **request_options(config),
        **reload,
        "device": "gpu",
        "engine": "paddle",
    }
    assert worker_settings(config)["mode"] == "structure"
    assert worker_settings(config)["layout_model"] == "PP-DocLayout_plus-L"


def test_worker_settings_for_windows_force_text_mode() -> None:
    settings = worker_settings(OcrConfig(mode=OcrMode.STRUCTURE))
    assert (settings["engine"], settings["mode"]) == ("windows", "text")
    json.dumps(settings)


@pytest.mark.parametrize(
    ("change", "reload"),
    [
        ({"engine": "windows"}, True),
        ({"mode": "text"}, True),
        ({"device": "cpu"}, True),
        ({"language": "ko"}, True),
        ({"table": False}, False),
        ({"chart": True}, False),
        ({"grayscale": True}, False),
        ({"resize_max_side": 0}, False),
        ({"text_timeout_s": 5}, False),
    ],
)
def test_reload_only_for_process_level_fields(change: dict[str, object], reload: bool) -> None:
    """PaddleOCR：引擎、识别模式、推理设备、识别语言变化时重载。"""
    base = OcrConfig(engine=OcrEngineKind.PADDLE)
    changed = OcrConfig.model_validate({"engine": "paddle", **change})
    assert (process_key(base) != process_key(changed)) is reload


@pytest.mark.parametrize(
    ("change", "reload"),
    [
        ({"engine": "paddle"}, True),
        ({"language": "en"}, True),
        ({"mode": "text"}, False),
        ({"device": "gpu"}, False),
        ({"table": False}, False),
        ({"grayscale": True}, False),
    ],
)
def test_windows_engine_reloads_only_for_engine_and_language(change: dict[str, object], reload: bool) -> None:
    """Windows OCR 不看识别模式和推理设备，改它们不重载。"""
    assert (process_key(OcrConfig()) != process_key(OcrConfig.model_validate(change))) is reload


@pytest.mark.parametrize("fields", [{}, {"mode": "structure"}, {"mode": "text"}])
def test_rapid_engine_always_runs_in_text_mode(fields: dict[str, object]) -> None:
    """RapidOCR 只有文本模式：mode 保留用户的取值，发给 OCR 进程的模式与处理时限都按文本模式。"""
    config = OcrConfig.model_validate({"engine": "rapid", "table": True, "formula": True, **fields})
    assert effective_mode(config) is OcrMode.TEXT
    assert request_options(config)["mode"] == "text"
    assert processing_timeout(config) == config.text_timeout_s
    settings = worker_settings(config)
    assert (settings["engine"], settings["mode"]) == ("rapid", "text")
    json.dumps(settings)


@pytest.mark.parametrize(
    ("change", "reload"),
    [
        ({"engine": "windows"}, True),
        ({"engine": "paddle"}, True),
        ({"language": "en"}, True),
        ({"mode": "text"}, False),
        ({"device": "gpu"}, True),
        ({"formula": True}, False),
        ({"resize_max_side": 0}, False),
    ],
)
def test_rapid_engine_reloads_for_engine_language_and_device(change: dict[str, object], reload: bool) -> None:
    """RapidOCR 只有文本模式，看引擎、识别语言与推理设备（运行环境可能带 DirectML 或 CUDA 加速）。"""
    base = OcrConfig(engine=OcrEngineKind.RAPID)
    changed = OcrConfig.model_validate({"engine": "rapid", **change})
    assert (process_key(base) != process_key(changed)) is reload


# ---------------------------------------------------------------- 流水线配置节与字段约定（OCRE-08、OCRE-09）

PIPELINE_MODELS: list[type[PipelineConfig]] = [model for _name, model in PIPELINE_SECTIONS.values()]


type BaseModelLike = PipelineConfig | NormalizeConfig


def _extra(model: type[BaseModelLike], name: str) -> dict[str, object]:
    extra = model.model_fields[name].json_schema_extra
    return dict(cast(dict[str, object], extra)) if isinstance(extra, dict) else {}


@pytest.mark.parametrize("model", PIPELINE_MODELS, ids=lambda m: m.__name__)
def test_stage_model_fields_follow_the_convention(model: type[PipelineConfig]) -> None:
    """每个 <环节>_model 字段：环节是 OcrStage 的值，带 ocr_stage 与 reload 标记，类型是 str，默认值非空。"""
    for name, info in model.model_fields.items():
        if not name.endswith("_model"):
            continue
        stage = name.removesuffix("_model")
        assert stage in {s.value for s in OcrStage}, name
        extra = _extra(model, name)
        assert extra.get("ocr_stage") == stage, name
        assert extra.get("reload") is True, name
        assert info.annotation is str and isinstance(info.default, str) and info.default, name
        assert info.title and info.description, name
    assert set(model.stage_fields().values()) == {n for n in model.model_fields if n.endswith("_model")}


@pytest.mark.parametrize("model", [*PIPELINE_MODELS, NormalizeConfig], ids=lambda m: m.__name__)
def test_parameter_fields_have_titles_ranges_and_markers(model: type[BaseModelLike]) -> None:
    for name, info in model.model_fields.items():
        assert info.title and info.description, f"{model.__name__}.{name} 缺少 title 或 description"
        if info.annotation is int or info.annotation is float:
            bounds = [type(item).__name__ for item in info.metadata]
            assert "Ge" in bounds and "Le" in bounds, f"{model.__name__}.{name} 缺少取值范围"
        extra = _extra(model, name)
        assert set(extra) <= {"ocr_stage", "reload", "advanced"}, name


def test_advanced_marker_exists_and_windows_section_is_empty() -> None:
    for model in (RapidOcrConfig, PaddleOcrConfig, NormalizeConfig):
        advanced = [name for name in model.model_fields if _extra(model, name).get("advanced") is True]
        common = [name for name in model.model_fields if not _extra(model, name).get("advanced")]
        assert advanced and common, model.__name__
    assert WindowsOcrConfig.model_fields == {}
    assert WindowsOcrConfig.reload_fields() == () and WindowsOcrConfig.request_fields() == ()
    assert not any(_extra(NormalizeConfig, name).get("reload") for name in NormalizeConfig.model_fields)


def test_defaults_validate_and_round_trip() -> None:
    for model in (*PIPELINE_MODELS, NormalizeConfig):
        default = model()
        assert model.model_validate(default.model_dump(mode="json")) == default
    with pytest.raises(ValidationError):
        RapidOcrConfig(det_thresh=1.5)
    with pytest.raises(ValidationError):
        PaddleOcrConfig(text_det_limit_type="both")  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationError):
        RapidOcrConfig.model_validate({"unknown": 1})


def test_request_options_carry_only_request_level_pipeline_fields() -> None:
    config = OcrConfig(engine=OcrEngineKind.RAPID)
    pipeline = RapidOcrConfig(det_box_thresh=0.4, det_thresh=0.2)
    options = request_options(config, pipeline)
    assert options["det_box_thresh"] == 0.4 and options["text_score"] == 0.5
    assert "det_thresh" not in options and "detection_model" not in options
    settings = worker_settings(config, pipeline)
    assert settings["det_thresh"] == 0.2 and settings["detection_model"] == "PP-OCRv6/small/multi/det"
    # 不是当前引擎的流水线配置被忽略，改用当前引擎的默认值。
    assert "text_det_thresh" not in request_options(config, PaddleOcrConfig())
    json.dumps(settings)


@pytest.mark.parametrize(
    ("change", "reload"),
    [
        ({"detection_model": "PP-OCRv5_server_det"}, True),
        ({"cpu_threads": 4}, True),
        ({"text_det_thresh": 0.2}, False),
        ({"layout_threshold": 0.5}, False),
    ],
)
def test_paddle_reload_follows_the_reload_marker(change: dict[str, object], reload: bool) -> None:
    config = OcrConfig(engine=OcrEngineKind.PADDLE)
    base = process_key(config, PaddleOcrConfig())
    changed = process_key(config, PaddleOcrConfig.model_validate(change))
    assert (base != changed) is reload
