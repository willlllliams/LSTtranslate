"""用后端真实配置模型检查 OCR 引擎管理界面（需求 OCRE-08、OCRE-09、OCR-11、UI-01、UI-07）。

各节模型取自组合根的 ``all_config_sections()``（ocr_windows、ocr_rapid、ocr_paddle、ocr_normalize），
检查界面按字段约定生成的环节、参数分组、步骤开关与英文文案都与后端一致。引擎目录里的模型 id 用配置里的
默认模型名，另加几个可选模型。

设置环境变量 ``LST_SETTINGS_SHOTS=1`` 时，``test_capture_screenshots`` 把展开流水线、打开高级折叠区、
选过运行环境版本的页面截成浅色、深色两张图，存到 data/tmp/settings-shots/ocr-mgmt-*.png（RapidOCR 子页）。
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel, create_model
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from lst.contracts.config import ConfigRejected
from lst.contracts.ocr_engines import (
    OcrEngineCatalog,
    OcrEngineInfo,
    OcrEngineKind,
    OcrEngineState,
    OcrModelInfo,
    OcrModelState,
    OcrRuntimeVariant,
    OcrStage,
)
from lst.services.ocr.config import (
    PADDLE_DEFAULT_MODELS,
    RAPID_DEFAULT_CLS,
    RAPID_DEFAULT_DET,
    RAPID_DEFAULT_REC,
    NormalizeConfig,
    OcrConfig,
    PaddleOcrConfig,
    RapidOcrConfig,
)
from lst.ui.settings.page_ocr_engine import NormalizePage, OcrEnginePage
from lst.ui.settings.page_ocr_pipeline import ParamsGroup, collect_params, stage_fields
from lst.ui.settings.strings_ocr import PARAM_TEXTS
from lst.ui.theme import apply_app_style
from tests.ui.conftest import on_native_platform
from tests.ui.settings import conftest as settings_conftest
from tests.ui.settings import support
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import default_config
from tests.ui.settings.test_keyboard import assert_visual_order, tab_stops

MB = 1024 * 1024
DOWNLOADED, MISSING = OcrModelState.DOWNLOADED, OcrModelState.NOT_DOWNLOADED


def rapid_info() -> OcrEngineInfo:
    det, rec, cls = OcrStage.DETECTION, OcrStage.RECOGNITION, OcrStage.TEXTLINE_ORIENTATION
    models = (
        OcrModelInfo(
            RAPID_DEFAULT_DET, det, "PP-OCRv6 检测（small）", DOWNLOADED, 4 * MB, default=True, bundled=True
        ),
        OcrModelInfo("PP-OCRv6/server/multi/det", det, "PP-OCRv6 检测（server）", MISSING, 88 * MB),
        OcrModelInfo(
            RAPID_DEFAULT_REC,
            rec,
            "PP-OCRv6 识别（多语言）",
            DOWNLOADED,
            16 * MB,
            ("zh-Hans", "en", "ja"),
            default=True,
            bundled=True,
        ),
        OcrModelInfo(
            "PP-OCRv5/mobile/korean/rec",
            rec,
            "PP-OCRv5 识别（韩文）",
            OcrModelState.DOWNLOADING,
            13 * MB,
            ("ko", "en"),
            progress=0.42,
        ),
        OcrModelInfo(
            "PP-OCRv5/mobile/latin/rec",
            rec,
            "PP-OCRv5 识别（拉丁字母）",
            DOWNLOADED,
            8 * MB,
            ("fr", "de", "es"),
        ),
        OcrModelInfo(
            RAPID_DEFAULT_CLS, cls, "文本行方向分类", DOWNLOADED, 1 * MB, default=True, bundled=True
        ),
    )
    return OcrEngineInfo(
        OcrEngineKind.RAPID,
        OcrEngineState.READY,
        needs_download=True,
        download_bytes=110 * MB,
        languages=("zh-Hans", "en", "ja"),
        features=("text",),
        variants=(OcrRuntimeVariant.CPU, OcrRuntimeVariant.DIRECTML, OcrRuntimeVariant.CUDA),
        installed_variant=OcrRuntimeVariant.DIRECTML,
        recommended_variant=OcrRuntimeVariant.CUDA,
        models=models,
    )


def paddle_info() -> OcrEngineInfo:
    models = [
        OcrModelInfo(model_id, stage, model_id, DOWNLOADED, 20 * MB, default=True)
        for stage, model_id in PADDLE_DEFAULT_MODELS.items()
    ]
    models.append(OcrModelInfo("PP-DocLayout-S", OcrStage.LAYOUT, "PP-DocLayout-S", MISSING, 5 * MB))
    return OcrEngineInfo(
        OcrEngineKind.PADDLE,
        OcrEngineState.NOT_INSTALLED,
        needs_download=True,
        download_bytes=1200 * MB,
        features=("text", "layout", "table", "formula"),
        variants=(OcrRuntimeVariant.CPU, OcrRuntimeVariant.CUDA),
        recommended_variant=OcrRuntimeVariant.CUDA,
        models=tuple(models),
    )


def catalog() -> OcrEngineCatalog:
    windows = OcrEngineInfo(
        OcrEngineKind.WINDOWS, OcrEngineState.INSTALLED, needs_download=False, features=("text",)
    )
    return OcrEngineCatalog((windows, rapid_info(), paddle_info()), OcrEngineKind.RAPID)


def engine_page(harness: Harness, kind: OcrEngineKind) -> OcrEnginePage:
    page = harness.window.page(f"capture.{kind.value}")
    assert isinstance(page, OcrEnginePage)
    return page


def normalize_page(harness: Harness) -> NormalizePage:
    page = harness.window.page("capture.normalize")
    assert isinstance(page, NormalizePage)
    return page


def rapid_params(harness: Harness) -> ParamsGroup:
    group = engine_page(harness, OcrEngineKind.RAPID).pipeline.params
    assert group is not None
    return group


@pytest.fixture
async def shown(start_settings: StartSettings) -> Harness:
    harness = await start_settings(config=default_config({"ocr": {"engine": "rapid"}}))
    await harness.publish(catalog())
    window = await harness.open()
    window.resize(1100, 860)
    window.show_page("capture.rapid")
    QApplication.processEvents()
    return harness


def test_backend_models_follow_the_field_conventions() -> None:
    assert set(stage_fields(PaddleOcrConfig)) == set(OcrStage)
    assert stage_fields(RapidOcrConfig) == {
        OcrStage.DETECTION: "detection_model",
        OcrStage.RECOGNITION: "recognition_model",
        OcrStage.TEXTLINE_ORIENTATION: "textline_orientation_model",
    }
    normalize = {param.path[0]: param.switch for param in collect_params(NormalizeConfig, steps=True)}
    assert normalize["clean_min_score"] == ("clean",)
    assert normalize["split_window"] == ("split",)
    assert normalize["same_line_tolerance"] == normalize["min_column_gap"] == ("aggregate",)
    assert normalize["merge_block_gap"] == ("merge_blocks",)
    assert normalize["merge_blocks"] is None


def test_every_backend_param_has_an_english_text() -> None:
    names = {
        param.path[-1]
        for model in (RapidOcrConfig, PaddleOcrConfig, NormalizeConfig)
        for param in collect_params(model)
    }
    assert names <= set(PARAM_TEXTS), sorted(names - set(PARAM_TEXTS))


async def test_rapid_pipeline_and_params_from_the_backend_models(shown: Harness) -> None:
    rapid = engine_page(shown, OcrEngineKind.RAPID).pipeline
    assert rapid.visible_stages() == [OcrStage.DETECTION, OcrStage.RECOGNITION, OcrStage.TEXTLINE_ORIENTATION]
    group = rapid_params(shown)

    def common(params: ParamsGroup) -> list[str | int]:
        return [b.location[-1] for b in params.bindings if not params.advanced_box.isAncestorOf(b.card)]

    # 参数按字段名前缀归到环节，挂在对应节点的面板里；不属于任何环节的留在流程图下方。
    detection = rapid.stage_params[OcrStage.DETECTION]
    assert common(detection) == ["det_thresh", "det_box_thresh", "det_unclip_ratio", "det_limit_side_len"]
    assert rapid.flow.panels[OcrStage.DETECTION.value].isAncestorOf(detection)
    assert common(rapid.stage_params[OcrStage.RECOGNITION]) == ["text_score"]
    assert common(group) == ["cpu_threads"]
    threads = group.binding(("cpu_threads",))
    assert threads is not None
    assert threads.card.title_label.text() == "CPU 线程数"  # 中文界面用后端写的标题与说明
    assert threads.card.desc_label.text().startswith("onnxruntime 的算子内线程数；0 表示自动")
    assert threads.card.desc_label.text().endswith("取值范围 0 至 256，默认 0。修改后自动重新加载识别组件。")
    recognition = rapid.stage_cards[OcrStage.RECOGNITION]
    latin = recognition.rows[2]
    latin.radio.click()
    await shown.flush()
    assert shown.recorded.updates[-1].patch == {
        "ocr_rapid": {"recognition_model": "PP-OCRv5/mobile/latin/rec"}
    }


async def test_normalize_steps_from_the_backend_model(shown: Harness) -> None:
    normalize = normalize_page(shown).normalize
    assert normalize is not None
    aggregate, gap = normalize.binding(("aggregate",)), normalize.binding(("same_line_tolerance",))
    assert aggregate is not None and gap is not None and gap.card.isEnabled()
    await shown.publish(
        default_config({"ocr": {"engine": "rapid"}, "ocr_normalize": {"aggregate": False}}, revision=2)
    )
    assert not gap.card.isEnabled()


async def test_backend_params_in_english(shown: Harness) -> None:
    await shown.publish(default_config({"ui": {"language": "en"}, "ocr": {"engine": "rapid"}}, revision=2))
    threads = rapid_params(shown).binding(("cpu_threads",))
    assert threads is not None
    assert threads.card.title_label.text() == "CPU threads"
    assert threads.card.desc_label.text().startswith("Number of CPU inference threads; 0 means automatic")
    normalize = normalize_page(shown).normalize
    assert normalize is not None
    clean = normalize.binding(("clean",))
    assert clean is not None and clean.card.title_label.text() == "Clean"


async def test_tab_order_with_the_backend_models(shown: Harness) -> None:
    rapid_params(shown).set_advanced_open(True)
    QApplication.processEvents()
    stops = tab_stops(shown.window, engine_page(shown, OcrEngineKind.RAPID))
    assert_visual_order(shown.window, stops, "capture.rapid")
    shown.window.show_page("capture.normalize")
    normalize = normalize_page(shown).normalize
    assert normalize is not None
    normalize.set_advanced_open(True)
    QApplication.processEvents()
    assert_visual_order(shown.window, tab_stops(shown.window, normalize_page(shown)), "capture.normalize")


# ---------------------------------------------------------------- 环节开关

PADDLE_TOGGLES = (
    "doc_orientation",
    "doc_unwarping",
    "textline_orientation",
    "region",
    "table",
    "formula",
    "chart",
)


async def test_paddle_stages_have_switches_bound_to_the_ocr_section(shown: Harness) -> None:
    shown.window.show_page("capture.paddle")
    page = engine_page(shown, OcrEngineKind.PADDLE)
    assert {stage.value for stage in page.pipeline.toggles} == set(PADDLE_TOGGLES)
    for stage, toggle in page.pipeline.toggles.items():
        assert toggle.location == ("ocr", stage.value)
        section = page.pipeline.stage_cards[stage]
        # 开关在环节卡片的标题行里，不是单独的卡片。
        assert section.isAncestorOf(toggle.editor)
        assert not section.isHidden()
    # 版面、检测、识别这些没有对应开关的环节总是启用。
    assert OcrStage.LAYOUT not in page.pipeline.toggles and OcrStage.DETECTION not in page.pipeline.toggles
    assert page.pipeline.stage_cards[OcrStage.LAYOUT].toggle is None


async def test_rapid_has_only_the_textline_orientation_switch_and_windows_none(shown: Harness) -> None:
    rapid = engine_page(shown, OcrEngineKind.RAPID).pipeline
    assert list(rapid.toggles) == [OcrStage.TEXTLINE_ORIENTATION]
    assert engine_page(shown, OcrEngineKind.WINDOWS).pipeline.toggles == {}


async def test_switch_commits_only_the_ocr_field(shown: Harness) -> None:
    page = engine_page(shown, OcrEngineKind.PADDLE)
    shown.recorded.clear()
    page.pipeline.toggles[OcrStage.DOC_ORIENTATION].editor.focus_widget().click()  # type: ignore[attr-defined]
    page.pipeline.toggles[OcrStage.REGION].editor.focus_widget().click()  # type: ignore[attr-defined]
    await shown.flush()
    assert [c.patch for c in shown.recorded.updates] == [
        {"ocr": {"doc_orientation": True}},
        {"ocr": {"region": False}},
    ]


async def test_turning_a_stage_off_greys_out_its_models_and_says_so(shown: Harness) -> None:
    page = engine_page(shown, OcrEngineKind.PADDLE)
    region = page.pipeline.stage_cards[OcrStage.REGION]  # 版面区域检测默认打开
    assert region.content.isEnabled() and "已关闭" not in region.desc_label.text()
    await shown.publish(default_config({"ocr": {"engine": "rapid", "region": False}}, revision=2))
    assert region.toggle is not None and region.toggle.editor.json_value() is False
    assert not region.content.isEnabled()
    assert region.desc_label.text().endswith("已关闭，识别时跳过这一步。")
    assert region.toggle.editor.isEnabled()  # 开关本身仍可操作，能再打开
    # 没有开关的环节不受影响。
    assert page.pipeline.stage_cards[OcrStage.LAYOUT].content.isEnabled()
    await shown.publish(default_config({"ocr": {"engine": "rapid", "region": True}}, revision=3))
    assert region.content.isEnabled() and "已关闭" not in region.desc_label.text()


async def test_switch_follows_the_language(shown: Harness) -> None:
    await shown.publish(
        default_config({"ui": {"language": "en"}, "ocr": {"engine": "rapid", "formula": False}}, revision=2)
    )
    page = engine_page(shown, OcrEngineKind.PADDLE)
    formula = page.pipeline.stage_cards[OcrStage.FORMULA]
    assert not formula.content.isEnabled()
    assert formula.desc_label.text().endswith("Turned off; this step is skipped during recognition.")
    assert formula.toggle is not None
    assert formula.toggle.editor.focus_widget().accessibleName() == f"Enable {formula.title_label.text()}"


async def test_rejected_switch_is_shown_on_its_stage(shown: Harness) -> None:
    await shown.publish(ConfigRejected(("ocr", "chart"), "图表识别需要显卡"))
    page = engine_page(shown, OcrEngineKind.PADDLE)
    assert page.pipeline.stage_cards[OcrStage.CHART].error_text == "未保存：图表识别需要显卡"
    assert shown.window.unclaimed_rejections() == []


async def test_a_switch_is_hidden_when_the_section_has_no_such_field(
    start_settings: StartSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fields = {
        name: (info.annotation, info) for name, info in OcrConfig.model_fields.items() if name != "region"
    }
    without_region = create_model("OcrWithoutRegion", **fields)  # type: ignore[call-overload]
    models: dict[str, type[BaseModel]] = {**support.all_models(), "ocr": without_region}
    monkeypatch.setattr(settings_conftest, "all_models", lambda: models)
    monkeypatch.setattr(support, "all_models", lambda: models)
    harness = await start_settings()
    page = engine_page(harness, OcrEngineKind.PADDLE)
    assert OcrStage.REGION not in page.pipeline.toggles
    assert page.pipeline.stage_cards[OcrStage.REGION].toggle is None
    assert OcrStage.TABLE in page.pipeline.toggles


async def test_options_page_no_longer_repeats_the_stage_switches(shown: Harness) -> None:
    options = shown.window.page("capture.options")
    fields = {str(b.location[-1]) for b in options.all_bindings() if b.location[0] == "ocr"}
    assert fields == {"enabled", "mode", "device", "language", "grayscale", "resize_max_side"}


# ---------------------------------------------------------------- 截图（手动开启）


@pytest.fixture
def restore_scheme(qapp: QApplication) -> Iterator[None]:
    yield
    apply_app_style(qapp, "system")


@pytest.mark.skipif(not os.environ.get("LST_SETTINGS_SHOTS"), reason="只在需要截图时运行")
@pytest.mark.usefixtures("restore_scheme")
async def test_capture_screenshots(start_settings: StartSettings, qapp: QApplication) -> None:
    target = Path(__file__).resolve().parents[3] / "data" / "tmp" / "settings-shots"
    target.mkdir(parents=True, exist_ok=True)
    for scheme in ("light", "dark"):
        apply_app_style(qapp, scheme)
        config = default_config({"ocr": {"engine": "rapid"}, "ocr_rapid": {"det_box_thresh": 0.6}})
        harness = await start_settings(config=config)
        await harness.publish(catalog())
        window = await harness.open()
        if on_native_platform():
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.resize(1100, 900)
        window.show_page("capture.rapid")
        page = engine_page(harness, OcrEngineKind.RAPID)
        rapid_params(harness).set_advanced_open(True)
        QApplication.processEvents()
        # 每个设置项显示的值都与配置一致（截图里看到的就是配置里的值）。
        mismatched = [
            b.location
            for b in [*page.all_bindings(), *page.pipeline.toggles.values()]
            if b.editor.json_value()
            != harness.presenter.value(str(b.location[0]), [str(k) for k in b.location[1:]])
        ]
        assert mismatched == [], scheme
        content = page.widget()
        assert content is not None
        content.adjustSize()
        content.grab().save(str(target / f"ocr-mgmt-{scheme}.png"))
        await harness.stop()
