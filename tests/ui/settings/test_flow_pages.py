"""OCR 引擎子页与本地翻译模型页的流程图（需求 UI-05、OCRE-08、OCRE-09、OCR-11）。

检查三件事：

1. 节点顺序与代码一致。flow_specs 里的顺序常量逐项对照代码位置（见各测试的说明），能从源代码读出的顺序
   直接读源代码核对：函数里各个调用出现的先后就是执行的先后。
2. 节点上的开关发出正确的 UpdateConfig：环节开关写 [ocr] 节，结构归一化开关写 [ocr_normalize] 节。
3. 点开节点，流程图下方只显示这个节点的面板，面板里是已有的设置组件。

设置环境变量 ``LST_SETTINGS_SHOTS=1`` 时，``test_capture_screenshots`` 把 PaddleOCR（版面模式、文本模式）、
RapidOCR 子页与本地翻译模型页截成浅色、深色两张图，存到 data/tmp/settings-shots/flow-*.png。
"""

import dataclasses
import importlib
import inspect
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from lst.contracts.events import WorkerStatus, WorkerStatuses
from lst.contracts.ocr_engines import (
    OcrEngineCatalog,
    OcrEngineKind,
    OcrEngineState,
    OcrRuntimeVariant,
    OcrStage,
)
from lst.contracts.ports import WorkerState
from lst.ui.settings import flow_specs
from lst.ui.settings.flow import FlowBranch, flow_keys
from lst.ui.settings.page_models import NmtModelsPage
from lst.ui.settings.page_ocr_engine import OcrEnginePage
from lst.ui.theme import apply_app_style
from tests.ui.conftest import on_native_platform
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import default_config
from tests.ui.settings.test_keyboard import assert_visual_order, tab_stops
from tests.ui.settings.test_ocr_pipeline_real import paddle_info, rapid_info

# ---------------------------------------------------------------- 夹具


def catalog(active: OcrEngineKind = OcrEngineKind.PADDLE) -> OcrEngineCatalog:
    from lst.contracts.ocr_engines import OcrEngineInfo

    windows = OcrEngineInfo(
        OcrEngineKind.WINDOWS, OcrEngineState.INSTALLED, needs_download=False, features=("text",)
    )
    paddle = dataclasses.replace(
        paddle_info(), state=OcrEngineState.READY, installed_variant=OcrRuntimeVariant.CPU
    )
    return OcrEngineCatalog((windows, rapid_info(), paddle), active)


def workers(ocr_device: str = "gpu", nmt_device: str = "cuda") -> WorkerStatuses:
    return WorkerStatuses(
        {
            "ocr": WorkerStatus(WorkerState.READY, versions={"device": ocr_device}),
            "nmt": WorkerStatus(
                WorkerState.READY,
                versions={
                    "device": nmt_device,
                    "compute_type": "float16" if nmt_device == "cuda" else "int8",
                },
            ),
        }
    )


def engine_page(harness: Harness, kind: OcrEngineKind) -> OcrEnginePage:
    page = harness.window.page(f"capture.{kind.value}")
    assert isinstance(page, OcrEnginePage)
    return page


def nmt_page(harness: Harness) -> NmtModelsPage:
    page = harness.window.page("models.nmt")
    assert isinstance(page, NmtModelsPage)
    return page


@pytest.fixture
async def shown(start_settings: StartSettings) -> Harness:
    harness = await start_settings(config=default_config({"ocr": {"engine": "paddle"}}))
    await harness.publish(catalog(), workers())
    window = await harness.open()
    window.resize(1100, 860)
    window.show_page("capture.paddle")
    QApplication.processEvents()
    return harness


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
    cases = (
        ("paddle-layout", "capture.paddle", {"ocr": {"engine": "paddle", "formula": True}}, "layout"),
        ("paddle-text", "capture.paddle", {"ocr": {"engine": "paddle", "mode": "text"}}, "detection"),
        ("rapid", "capture.rapid", {"ocr": {"engine": "rapid"}}, "recognition"),
        ("nmt", "models.nmt", {"ocr": {"engine": "paddle"}}, "infer"),
    )
    for scheme in ("light", "dark"):
        apply_app_style(qapp, scheme)
        for name, page_id, sections, node in cases:
            harness = await start_settings(config=default_config(sections))
            await harness.publish(catalog(), workers(ocr_device="cpu"))
            window = await harness.open()
            if on_native_platform():
                window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            window.resize(1100, 900)
            window.show_page(page_id)
            page = window.page(page_id)
            flow = (
                page.flow
                if isinstance(page, NmtModelsPage)
                else engine_page(harness, OcrEngineKind(page_id.split(".")[1])).pipeline.flow
            )
            flow.open(node)
            QApplication.processEvents()
            content = page.widget()
            assert content is not None
            content.adjustSize()
            content.grab().save(str(target / f"flow-{name}-{scheme}.png"))
            await harness.stop()


# ---------------------------------------------------------------- 节点顺序与代码一致


def assert_in_order(source: str, *calls: str) -> None:
    """calls 在 source 里依次出现（每一项从上一项之后开始找）。"""
    position = -1
    for index, call in enumerate(calls):
        found = source.find(call, position + 1)
        assert found > position, f"{call} 没有出现在 {calls[:index]} 之后"
        position = found


MAIN_TEXT = ["decode", "clean", "split", "aggregate", "segments"]
MAIN_STRUCTURE = ["decode", "clean", "merge_blocks", "segments"]


def test_paddle_structure_order() -> None:
    # PaddleOCR 进程：PaddleBackend.recognize 先缩放、灰度，再 _predict，
    # 最后 extract_structure、convert_structure。PP-StructureV3 内部的顺序见 flow_specs.PADDLE_STRUCTURE_FLOW
    # 的说明（PaddleX 3.7 pipeline_v2.py 的 predict：
    # doc_preprocessor → layout_det_model → region_detection_model → formula_recognition_pipeline →
    # general_ocr_pipeline → table_recognition_pipeline → chart_recognition_model）。
    from lst.workers.ocr_worker.paddle_backend import PaddleBackend

    source = inspect.getsource(PaddleBackend.recognize)
    assert_in_order(
        source, "plan_resize", "to_grayscale", "self._predict", "extract_structure", "convert_structure"
    )
    assert flow_keys(flow_specs.PADDLE_STRUCTURE_FLOW) == [
        "preprocess",
        "doc_orientation",
        "doc_unwarping",
        "layout",
        "region",
        "formula",
        "detection",
        "textline_orientation",
        "recognition",
        "table",
        "chart",
        "convert",
        *MAIN_STRUCTURE,
    ]


def test_paddle_text_order() -> None:
    # 文本模式：paddle_config.text_config 只配置文档预处理与 OCR 子产线（检测、文本行方向、识别）。
    from lst.workers.ocr_worker import paddle_config

    source = inspect.getsource(paddle_config.text_config)
    assert_in_order(source, "_configure_doc_preprocessor", "_configure_ocr")
    assert "LayoutDetection" not in source and "Table" not in source
    assert_in_order(inspect.getsource(paddle_config._configure_ocr), "TextDetection", "TextRecognition")
    assert flow_keys(flow_specs.PADDLE_TEXT_FLOW) == [
        "preprocess",
        "doc_orientation",
        "doc_unwarping",
        "detection",
        "textline_orientation",
        "recognition",
        "convert",
        *MAIN_TEXT,
    ]


def test_rapid_and_windows_order() -> None:
    # RapidBackend.recognize：resize_area → to_grayscale → RapidOCR（检测、方向分类 use_cls、识别）
    # → build_blocks_json。
    from lst.workers.ocr_worker.rapid_backend import RapidBackend, RapidOptions
    from lst.workers.ocr_worker.winocr_backend import WinOcrBackend

    assert_in_order(
        inspect.getsource(RapidBackend.recognize),
        "resize_area",
        "to_grayscale",
        "engine(",
        "build_blocks_json",
    )
    assert "use_cls" in inspect.getsource(RapidOptions.call_kwargs)
    assert flow_keys(flow_specs.RAPID_FLOW) == [
        "preprocess",
        "detection",
        "textline_orientation",
        "recognition",
        "convert",
        *MAIN_TEXT,
    ]
    # WinOcrBackend.recognize：resize_area → to_grayscale → recognizer.recognize（系统 OCR）
    # → build_blocks（拼行）。
    assert_in_order(
        inspect.getsource(WinOcrBackend.recognize),
        "resize_area",
        "to_grayscale",
        "recognizer.recognize",
        "build_blocks",
    )
    assert flow_keys(flow_specs.WINDOWS_FLOW) == ["preprocess", "system_ocr", "assemble", *MAIN_TEXT]


def test_main_process_order_and_normalize_switches() -> None:
    # OcrModule._complete：decode_blocks → 结构归一化 → blocks_to_segments；
    # build_pipeline 依次加入清洗、拆分、聚合。
    from lst.services.ocr import normalize
    from lst.services.ocr.module import OcrModule

    assert_in_order(
        inspect.getsource(OcrModule._complete), "decode_blocks", "normalizer.apply", "blocks_to_segments"
    )
    assert_in_order(inspect.getsource(normalize.build_pipeline), "CleanStep", "SplitStep", "AggregateStep")
    # 拆分在版面模式下原样返回，所以版面模式的流程里没有拆分节点。
    assert "if not context.text_mode" in inspect.getsource(normalize.SplitStep.apply)
    fields = normalize.NormalizeConfig.model_fields
    assert all(field in fields for field in flow_specs.NORMALIZE_SWITCHES.values())


def test_nmt_order() -> None:
    # 主程序：race/module.py 先 _detect 再 start_session；
    # scheduler 的 _dispatch 先 _apply_cache 再 _enqueue_pairs，
    # 后者用 pack_segments 分批；local_nmt 引擎把一批转成 TranslateRequest 交给 NMT 进程。
    from lst.services.engines.local_nmt.engine import LocalNmtEngine
    from lst.workers.nmt_worker import routing
    from lst.workers.nmt_worker.ct2_backend import Ct2Backend

    module = importlib.import_module("lst.services.race.module")
    scheduler = importlib.import_module("lst.services.race.scheduler")
    assert_in_order(inspect.getsource(module), "self._detect(", "start_session(")
    scheduler_source = inspect.getsource(scheduler)
    dispatch = scheduler_source[scheduler_source.index("async def _dispatch(") :]
    assert_in_order(dispatch, "_apply_cache", "_enqueue_pairs")
    enqueue = scheduler_source[scheduler_source.index("def _enqueue_pairs(") :]
    assert "pack_segments" in enqueue[: enqueue.index("\n    def ", 10)]
    assert_in_order(inspect.getsource(LocalNmtEngine.translate), "TranslateRequest(", "self._submit(")
    # NMT 进程：translate_segments 先 plan_route，再逐段 split_sentences、逐跳翻译、join_sentences；
    # Ct2Backend.translate_sentences 依次是取模型、分词、translate_batch、detokenize。
    assert_in_order(
        inspect.getsource(routing.translate_segments),
        "plan_route(",
        "split_sentences(",
        "for hop in route",
        "join_sentences(",
    )
    assert_in_order(
        inspect.getsource(Ct2Backend.translate_sentences),
        "self._model(",
        "tokenizer.encode(",
        "translate_batch(",
        "detokenize(",
    )
    assert flow_keys(flow_specs.NMT_FLOW) == [
        "langid",
        "cache",
        "batch",
        "submit",
        "route",
        "sentences",
        "direct",
        "pivot",
        "model",
        "tokenize",
        "infer",
        "detokenize",
        "join",
    ]
    assert flow_specs.NMT_FLOW[6] == FlowBranch((("direct",), ("pivot",)))
    # 中转语言是英语：没有直连模型时，路线经英语走两跳。
    assert routing.plan_route("ja", "zh-Hans", {("ja", "en"), ("en", "zh-Hans")}, "en") == (
        ("ja", "en"),
        ("en", "zh-Hans"),
    )


def test_every_engine_param_lands_on_a_stage_the_engine_has() -> None:
    from lst.services.ocr.config import PaddleOcrConfig, RapidOcrConfig
    from lst.ui.settings.page_ocr_pipeline import collect_params, param_stage, stage_fields

    for model in (RapidOcrConfig, PaddleOcrConfig):
        stages = set(stage_fields(model))
        for param in collect_params(model):
            stage = param_stage(param)
            assert stage is None or stage in stages, (model.__name__, param.path)
    rapid = {param.path[0]: param_stage(param) for param in collect_params(RapidOcrConfig)}
    assert rapid["det_thresh"] is OcrStage.DETECTION and rapid["cls_thresh"] is OcrStage.TEXTLINE_ORIENTATION
    assert rapid["text_score"] is OcrStage.RECOGNITION and rapid["cpu_threads"] is None


def test_every_flow_text_has_both_languages() -> None:
    from lst.ui.i18n import SUPPORTED
    from lst.ui.settings.strings_flow import CATALOG

    for key, texts in CATALOG.items():
        assert set(texts) == set(SUPPORTED) and all(texts.values()), key
    stages = {stage.value for stage in OcrStage}
    for key in (*flow_specs.OCR_FLOW_KEYS, *flow_specs.NMT_FLOW_KEYS):
        if key not in stages:
            assert (
                f"settings.flow.node.{key}.title" in CATALOG and f"settings.flow.node.{key}.desc" in CATALOG
            )


# ---------------------------------------------------------------- OCR 引擎子页


async def test_engine_pages_show_their_own_flow(shown: Harness) -> None:
    paddle = engine_page(shown, OcrEngineKind.PADDLE).pipeline
    assert paddle.flow.chart.items == flow_specs.PADDLE_STRUCTURE_FLOW
    assert engine_page(shown, OcrEngineKind.RAPID).pipeline.flow.chart.items == flow_specs.RAPID_FLOW
    assert engine_page(shown, OcrEngineKind.WINDOWS).pipeline.flow.chart.items == flow_specs.WINDOWS_FLOW
    # 识别模式改成文本模式：PaddleOCR 换成文本模式的流程，打开的节点仍在流程里就保持打开。
    paddle.flow.open("detection")
    await shown.publish(default_config({"ocr": {"engine": "paddle", "mode": "text"}}, revision=2))
    assert paddle.flow.chart.items == flow_specs.PADDLE_TEXT_FLOW
    assert paddle.flow.selected == "detection"
    paddle.flow.open("layout")  # 文本模式没有版面检测，打不开
    assert paddle.flow.selected == "detection"


async def test_stage_nodes_show_the_chosen_model_and_switch_state(shown: Harness) -> None:
    chart = engine_page(shown, OcrEngineKind.PADDLE).pipeline.flow.chart
    layout = chart.state("layout")
    assert (layout.title, layout.status, layout.switch, layout.bypassed) == (
        "版面检测",
        "PP-DocLayout_plus-L",
        None,
        False,
    )
    formula = chart.state("formula")
    assert formula.switch is False and formula.bypassed and formula.status == "已关闭"
    assert chart.state("region").switch is True
    assert chart.state("preprocess").status == "长边 ≤ 4000 · 彩色"
    assert chart.state("clean").switch is True and chart.state("merge_blocks").switch is False


async def test_chart_is_skipped_on_cpu(shown: Harness) -> None:
    chart = engine_page(shown, OcrEngineKind.PADDLE).pipeline.flow.chart
    await shown.publish(default_config({"ocr": {"engine": "paddle", "chart": True}}, revision=2))
    assert chart.state("chart").switch is True and not chart.state("chart").bypassed
    await shown.publish(workers(ocr_device="cpu"))
    state = chart.state("chart")
    assert state.switch is True and state.bypassed and state.status == "CPU 上跳过"


async def test_node_switches_send_the_right_updates(shown: Harness) -> None:
    pipeline = engine_page(shown, OcrEngineKind.PADDLE).pipeline
    chart = pipeline.flow.chart
    shown.recorded.clear()
    chart.toggle("formula")
    chart.toggle("region")
    chart.toggle("merge_blocks")
    QTest.keyClick(chart.nodes["clean"], Qt.Key.Key_Space)
    await shown.flush()
    assert [c.patch for c in shown.recorded.updates] == [
        {"ocr": {"formula": True}},
        {"ocr": {"region": False}},
        {"ocr_normalize": {"merge_blocks": True}},
        {"ocr_normalize": {"clean": False}},
    ]
    # 环节开关与环节卡片标题行里的开关是同一个设置项，节点上切换后两边一致。
    assert pipeline.toggles[OcrStage.FORMULA].editor.json_value() is True
    assert chart.state("formula").switch is True and not chart.state("formula").bypassed
    assert chart.state("merge_blocks").switch is True


async def test_opening_a_node_shows_its_existing_settings(shown: Harness) -> None:
    page = engine_page(shown, OcrEngineKind.PADDLE)
    pipeline = page.pipeline
    flow = pipeline.flow
    assert flow.selected == "recognition"  # 默认打开文字识别
    flow.chart.activate("layout")
    QApplication.processEvents()
    assert [key for key, panel in flow.panels.items() if not panel.isHidden()] == ["layout"]
    card = pipeline.stage_cards[OcrStage.LAYOUT]
    assert flow.panels["layout"].isAncestorOf(card) and card.isVisibleTo(page)
    params = pipeline.stage_params[OcrStage.LAYOUT]
    assert params.binding(("layout_threshold",)) is not None and params.isVisibleTo(page)
    assert not pipeline.stage_cards[OcrStage.DETECTION].isVisibleTo(page)
    # 结构归一化与截图预处理的面板有跳转按钮，参数在那两页。
    flow.open("clean")
    note = pipeline.notes["clean"]
    assert note.isVisibleTo(page) and note.title_label.text() == "清洗"
    assert note.button is not None
    note.button.click()
    assert shown.window.current_page.page_id == "capture.normalize"
    preprocess = pipeline.notes["preprocess"].button
    assert preprocess is not None
    preprocess.click()
    assert shown.window.current_page.page_id == "capture.options"


async def test_tab_enters_the_flow_on_the_opened_node(shown: Harness) -> None:
    page = engine_page(shown, OcrEngineKind.PADDLE)
    flow = page.pipeline.flow
    flow.open("formula")
    QApplication.processEvents()
    stops = tab_stops(shown.window, page)
    nodes = [stop for stop in stops if stop in flow.chart.nodes.values()]
    assert nodes == [flow.chart.nodes["formula"]]  # 流程图在 Tab 顺序里只占一个位置
    assert_visual_order(shown.window, stops, "capture.paddle")


# ---------------------------------------------------------------- 本地翻译模型页


async def test_nmt_flow_nodes_and_panels(shown: Harness) -> None:
    page = nmt_page(shown)
    chart = page.flow.chart
    assert chart.items == flow_specs.NMT_FLOW
    assert page.flow.selected == "infer" and not page.flow.panels["infer"].isHidden()
    assert chart.state("infer").status == "显卡 · float16"
    assert chart.state("batch").status == "每批 ≤ 16 段、800 字"
    assert chart.state("submit").status == "进程：就绪"
    assert "float16" in page.device_card.desc_label.text()
    await shown.publish(WorkerStatuses({}))
    assert chart.state("infer").status == "设备：自动"
    page.gpu_button.click()
    assert shown.window.current_page.page_id == "models.gpu"
    page.flow.open("batch")
    assert page.engines_button.isVisibleTo(page)
    page.engines_button.click()
    assert shown.window.current_page.page_id == "engines"


async def test_nmt_beam_size_commits(shown: Harness) -> None:
    page = nmt_page(shown)
    beam = next(b for b in page.bindings if b.location == ("nmt", "beam_size"))
    assert page.flow.panels["infer"].isAncestorOf(beam.card)
    shown.recorded.clear()
    beam.editor.committed.emit(3)
    await shown.flush()
    assert [c.patch for c in shown.recorded.updates] == [{"nmt": {"beam_size": 3}}]


async def test_flow_texts_in_english(shown: Harness) -> None:
    await shown.publish(default_config({"ui": {"language": "en"}, "ocr": {"engine": "paddle"}}, revision=2))
    chart = engine_page(shown, OcrEngineKind.PADDLE).pipeline.flow.chart
    assert chart.state("preprocess").title == "Image preprocessing"
    assert chart.state("formula").status == "Off"
    assert chart.accessibleName() == "PaddleOCR recognition flow chart"
    assert nmt_page(shown).flow.chart.state("pivot").title == "Via English"
