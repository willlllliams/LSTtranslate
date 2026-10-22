"""OCR 引擎页与引擎子页的管理测试（需求 OCRE-07 至 OCRE-09、OCR-11、UI-01、UI-05 至 UI-07）。

检查运行环境版本的选择与更换、引擎子页上流水线各环节的模型行（选用、下载、取消、删除）、
引擎配置节的常用参数与“高级参数”折叠区、恢复默认、结构归一化页的步骤开关，以及键盘顺序与中英文切换。

后端的配置模型（ocr_rapid、ocr_paddle、ocr_normalize）用符合字段约定的假模型代替：选模型字段名为
``<环节>_model`` 并带 ``{"ocr_stage": ...}``，高级参数带 ``{"advanced": True}``，需要重新加载的带
``{"reload": True}``。

用后端真实配置模型的检查与截图在 test_ocr_pipeline_real.py。
"""

import pytest
from pydantic import BaseModel, Field
from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox

from lst.contracts.config import ConfigRejected
from lst.contracts.ocr_engines import (
    CancelOcrModelDownload,
    DeleteOcrModel,
    DownloadOcrModel,
    InstallOcrEngine,
    OcrEngineCatalog,
    OcrEngineErrorCode,
    OcrEngineInfo,
    OcrEngineKind,
    OcrEngineState,
    OcrModelInfo,
    OcrModelState,
    OcrRuntimeVariant,
    OcrStage,
)
from lst.ui import i18n
from lst.ui.settings.cards import field_details
from lst.ui.settings.ocr_engine_rows import build_variant_view
from lst.ui.settings.ocr_pipeline_rows import build_stage_views
from lst.ui.settings.page_capture import CaptureEnginesPage, OcrEngineCard
from lst.ui.settings.page_ocr_engine import NormalizePage, OcrEnginePage
from lst.ui.settings.page_ocr_pipeline import ParamsGroup, StageSection, collect_params, stage_fields
from lst.ui.settings.schema import field_spec
from tests.ui.settings import conftest as settings_conftest
from tests.ui.settings import support
from tests.ui.settings.conftest import Harness, StartSettings
from tests.ui.settings.support import FakeDialogs, all_models, default_config
from tests.ui.settings.test_keyboard import assert_visual_order, tab_stops

MB = 1024 * 1024
CPU, DML, CUDA = OcrRuntimeVariant.CPU, OcrRuntimeVariant.DIRECTML, OcrRuntimeVariant.CUDA


# ---------------------------------------------------------------- 符合字段约定的假配置模型


class FakeRapid(BaseModel):
    detection_model: str | None = Field(None, title="检测模型", json_schema_extra={"ocr_stage": "detection"})
    recognition_model: str | None = Field(
        None, title="识别模型", json_schema_extra={"ocr_stage": "recognition"}
    )
    box_thresh: float = Field(0.5, ge=0, le=1, title="检测阈值", description="低于阈值的文字框会被丢弃。")
    use_angle_cls: bool = Field(False, title="文字方向分类", description="判断文字行是否倒置。")
    max_side: int = Field(
        2000,
        ge=320,
        le=8192,
        title="最长边",
        description="检测前把截图缩到这个尺寸以内。",
        json_schema_extra={"advanced": True, "reload": True},
    )
    unclip_ratio: float = Field(1.6, ge=1, le=3, title="文字框外扩比例", json_schema_extra={"advanced": True})


class FakePaddle(BaseModel):
    layout_model: str | None = Field(None, title="版面模型", json_schema_extra={"ocr_stage": "layout"})
    recognition_model: str | None = Field(
        None, title="识别模型", json_schema_extra={"ocr_stage": "recognition"}
    )
    layout_threshold: float = Field(0.5, ge=0, le=1, title="版面阈值", description="版面块的置信度下限。")
    cpu_threads: int = Field(
        4, ge=1, le=64, title="CPU 线程数", json_schema_extra={"advanced": True, "reload": True}
    )


class FakeNormalize(BaseModel):
    clean: bool = Field(True, title="清洗", description="去掉面积过小或没有文字的版面块。")
    clean_min_area: int = Field(64, ge=0, le=100000, title="最小面积", description="单位为平方像素。")
    split: bool = Field(False, title="拆分", description="把跨越多栏的大块拆开。")
    split_max_lines: int = Field(40, ge=2, le=500, title="单块最多行数")
    merge: bool = Field(False, title="聚合", description="把相邻的小块合并成段落。")
    merge_gap: float = Field(
        1.2, ge=0, le=10, title="合并间距", description="以行高为单位。", json_schema_extra={"advanced": True}
    )


FAKE_SECTIONS: dict[str, type[BaseModel]] = {
    "ocr_rapid": FakeRapid,
    "ocr_paddle": FakePaddle,
    "ocr_normalize": FakeNormalize,
}


@pytest.fixture(autouse=True)
def fake_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    """组合根还没有这几节时，用假模型代替；已经有了也换成假模型，测试不受后端字段变化影响。"""
    models = {**all_models(), **FAKE_SECTIONS}
    monkeypatch.setattr(settings_conftest, "all_models", lambda: models)
    # default_config 按 support.all_models 生成各节的默认值，也换成假模型，配置里的值与模型的默认值一致。
    monkeypatch.setattr(support, "all_models", lambda: models)


# ---------------------------------------------------------------- 引擎目录


def model(
    model_id: str,
    stage: OcrStage,
    state: OcrModelState = OcrModelState.DOWNLOADED,
    *,
    name: str | None = None,
    size: int | None = 5 * MB,
    languages: tuple[str, ...] = (),
    default: bool = False,
    bundled: bool = False,
    selected: bool = False,
    progress: float | None = None,
    error_code: OcrEngineErrorCode | None = None,
) -> OcrModelInfo:
    return OcrModelInfo(
        model_id,
        stage,
        name or model_id,
        state,
        size_bytes=size,
        languages=languages,
        default=default,
        bundled=bundled,
        selected=selected,
        progress=progress,
        error_code=error_code,
    )


def rapid_models(
    server: OcrModelState = OcrModelState.NOT_DOWNLOADED, server_error: OcrEngineErrorCode | None = None
) -> tuple[OcrModelInfo, ...]:
    """检测两个模型（自带的默认模型、未下载的服务器版），识别三个（默认、已下载、下载中）。"""
    detection, recognition = OcrStage.DETECTION, OcrStage.RECOGNITION
    return (
        model("det-small", detection, name="PP-OCRv5 检测（小）", default=True, bundled=True),
        model(
            "det-server",
            detection,
            server,
            name="PP-OCRv5 检测（服务器版）",
            size=84 * MB,
            error_code=server_error,
        ),
        model(
            "rec-ch", recognition, name="中文识别", languages=("zh-Hans", "en"), default=True, size=10 * MB
        ),
        model("rec-ja", recognition, name="日文识别", languages=("ja",), size=9 * MB),
        model(
            "rec-ko",
            recognition,
            OcrModelState.DOWNLOADING,
            name="韩文识别",
            languages=("ko",),
            progress=0.25,
        ),
    )


def rapid_info(
    state: OcrEngineState = OcrEngineState.READY,
    *,
    installed: OcrRuntimeVariant | None = DML,
    models: tuple[OcrModelInfo, ...] | None = None,
) -> OcrEngineInfo:
    return OcrEngineInfo(
        OcrEngineKind.RAPID,
        state,
        needs_download=True,
        download_bytes=110 * MB,
        languages=("zh-Hans", "en", "ja"),
        features=("text",),
        variants=(CPU, DML, CUDA),
        installed_variant=installed if state not in (OcrEngineState.NOT_INSTALLED,) else None,
        recommended_variant=CUDA,
        models=rapid_models() if models is None else models,
    )


def paddle_info(
    state: OcrEngineState = OcrEngineState.NOT_INSTALLED,
    *,
    installed: OcrRuntimeVariant | None = None,
    recommended: OcrRuntimeVariant = CUDA,
) -> OcrEngineInfo:
    return OcrEngineInfo(
        OcrEngineKind.PADDLE,
        state,
        needs_download=True,
        download_bytes=1200 * MB,
        features=("text", "layout", "table", "formula"),
        variants=(CPU, CUDA),
        installed_variant=installed,
        recommended_variant=recommended,
        models=(
            model("PP-DocLayout-L", OcrStage.LAYOUT, default=True, size=120 * MB),
            model("PP-DocLayout-S", OcrStage.LAYOUT, OcrModelState.NOT_DOWNLOADED, size=8 * MB),
            model(
                "PP-OCRv5_server_rec", OcrStage.RECOGNITION, default=True, languages=("zh-Hans", "en", "ja")
            ),
        ),
    )


def windows_info() -> OcrEngineInfo:
    return OcrEngineInfo(
        OcrEngineKind.WINDOWS,
        OcrEngineState.INSTALLED,
        needs_download=False,
        languages=("zh-Hans", "en"),
        features=("text",),
    )


def catalog(
    rapid: OcrEngineInfo | None = None,
    paddle: OcrEngineInfo | None = None,
    active: OcrEngineKind = OcrEngineKind.RAPID,
) -> OcrEngineCatalog:
    return OcrEngineCatalog((windows_info(), rapid or rapid_info(), paddle or paddle_info()), active)


def capture_page(harness: Harness) -> CaptureEnginesPage:
    page = harness.window.page("capture.engines")
    assert isinstance(page, CaptureEnginesPage)
    return page


def engine_page(harness: Harness, kind: OcrEngineKind) -> OcrEnginePage:
    page = harness.window.page(f"capture.{kind.value}")
    assert isinstance(page, OcrEnginePage) and page.kind is kind
    return page


def normalize_page(harness: Harness) -> NormalizePage:
    page = harness.window.page("capture.normalize")
    assert isinstance(page, NormalizePage)
    return page


def engine_card(harness: Harness, kind: OcrEngineKind) -> OcrEngineCard:
    return capture_page(harness).engine_cards[kind]


def stage_card(harness: Harness, kind: OcrEngineKind, stage: OcrStage) -> StageSection:
    return engine_page(harness, kind).pipeline.stage_cards[stage]


def params(harness: Harness, kind: OcrEngineKind) -> ParamsGroup:
    group = engine_page(harness, kind).pipeline.params
    assert group is not None
    return group


def combo_texts(combo: QComboBox) -> list[str]:
    return [combo.itemText(index) for index in range(combo.count())]


def choose(combo: QComboBox, index: int) -> None:
    """像用户一样选中下拉框的一项（发出 activated）。"""
    combo.setCurrentIndex(index)
    combo.activated.emit(index)


@pytest.fixture
async def shown(start_settings: StartSettings) -> Harness:
    harness = await start_settings(config=default_config({"ocr": {"engine": "rapid"}}))
    await harness.publish(catalog())
    window = await harness.open()
    window.resize(1100, 860)
    window.show_page("capture.rapid")
    QApplication.processEvents()
    return harness


# ---------------------------------------------------------------- 纯逻辑


def test_variant_view_defaults_to_recommended_then_installed() -> None:
    i18n.set_language("zh-Hans")
    fresh = build_variant_view(paddle_info(), None)
    assert fresh.visible and fresh.enabled and fresh.chosen is CUDA
    assert [text for _, text in fresh.options] == ["CPU · 不需要显卡", "CUDA · NVIDIA 显卡 · 推荐"]
    assert fresh.recommendation == "推荐：CUDA（检测到 NVIDIA 显卡）"
    assert not fresh.show_change

    installed = paddle_info(OcrEngineState.INSTALLED, installed=CPU)
    view = build_variant_view(installed, None)
    assert view.chosen is CPU and view.show_change and not view.can_change
    assert build_variant_view(installed, CUDA).can_change
    busy = build_variant_view(paddle_info(OcrEngineState.DOWNLOADING), None)
    assert not busy.enabled
    reasons = {
        variant: build_variant_view(paddle_info(recommended=variant), None).recommendation
        for variant in (CPU, DML)
    }
    assert reasons[CPU] == "推荐：CPU（没有检测到这个引擎能用的显卡）"
    assert reasons[DML] == "推荐：DirectML（检测到支持 DirectX 12 的显卡）"


def test_stage_views_follow_the_configured_model_and_block_unsafe_deletes() -> None:
    i18n.set_language("zh-Hans")
    info = rapid_info()
    fields = stage_fields(FakeRapid)
    assert fields == {OcrStage.DETECTION: "detection_model", OcrStage.RECOGNITION: "recognition_model"}
    views = build_stage_views(
        info, fields=fields, configured={OcrStage.RECOGNITION: "rec-ja"}, installed=True
    )
    assert [view.stage for view in views] == [OcrStage.DETECTION, OcrStage.RECOGNITION]
    detection, recognition = views
    assert detection.selected is not None and detection.selected.id == "det-small"  # 没有配置：默认模型
    small = detection.model("det-small")
    assert small is not None and not small.show_delete  # 自带的模型不能删
    assert small.meta == "5.0 MB · 默认" and small.status == "随引擎自带"
    server = detection.model("det-server")
    assert server is not None and not server.selectable and server.can_download
    assert recognition.selected is not None and recognition.selected.id == "rec-ja"
    japanese = recognition.model("rec-ja")
    assert japanese is not None and japanese.show_delete and not japanese.delete_blocked
    chinese = recognition.model("rec-ch")
    assert chinese is not None and chinese.meta == "10.0 MB · 语言：简体中文、英语 · 默认"
    korean = recognition.model("rec-ko")
    assert (
        korean is not None
        and korean.status == "正在下载 25%"
        and korean.can_cancel
        and korean.progress == 0.25
    )

    # 默认模型正在选用时不能删除；引擎未安装时不能下载。
    views = build_stage_views(info, fields=fields, configured={}, installed=False)
    chinese = views[1].model("rec-ch")
    assert chinese is not None and chinese.selected and chinese.delete_blocked
    server = views[0].model("det-server")
    assert server is not None and not server.can_download


def test_params_split_common_advanced_and_step_switches() -> None:
    rapid = {param.path: param for param in collect_params(FakeRapid)}
    assert list(rapid) == [("box_thresh",), ("use_angle_cls",), ("max_side",), ("unclip_ratio",)]
    assert [path for path, param in rapid.items() if param.advanced] == [("max_side",), ("unclip_ratio",)]
    normalize = {param.path: param.switch for param in collect_params(FakeNormalize)}
    assert normalize[("clean_min_area",)] == ("clean",)
    assert normalize[("split_max_lines",)] == ("split",)
    assert normalize[("merge_gap",)] == ("merge",)
    assert normalize[("clean",)] is None


def test_field_details_show_range_default_and_reload() -> None:
    i18n.set_language("zh-Hans")
    spec = field_spec(FakeRapid, ("max_side",))
    assert spec.advanced and spec.reload and spec.title == "最长边"
    assert field_details(spec, "") == "取值范围 320 至 8192，默认 2000。修改后自动重新加载识别组件。"
    assert field_details(field_spec(FakeRapid, ("use_angle_cls",)), "") == "默认：关。"
    assert field_details(field_spec(FakeRapid, ("detection_model",)), "") == "默认不设置。"
    i18n.set_language("en")
    assert field_details(spec, "") == (
        "Range 320 to 8192, default 2000. The recognition component reloads automatically after a change."
    )
    i18n.set_language("zh-Hans")


# ---------------------------------------------------------------- 运行环境版本


async def test_install_uses_the_recommended_or_chosen_variant(shown: Harness) -> None:
    paddle = engine_card(shown, OcrEngineKind.PADDLE)
    assert not paddle.variant_row.isHidden()
    assert combo_texts(paddle.variant_combo) == ["CPU · 不需要显卡", "CUDA · NVIDIA 显卡 · 推荐"]
    assert paddle.variant_combo.currentIndex() == 1
    assert paddle.recommend_label.text().startswith(
        "推荐：CUDA（检测到 NVIDIA 显卡）。CPU 版在任何电脑上都能用"
    )
    assert paddle.change_button.isHidden()
    paddle.download_button.click()
    choose(paddle.variant_combo, 0)
    paddle.download_button.click()
    await shown.flush()
    assert shown.recorded.of(InstallOcrEngine) == [
        InstallOcrEngine(OcrEngineKind.PADDLE, CUDA),
        InstallOcrEngine(OcrEngineKind.PADDLE, CPU),
    ]
    # 用户的选择在状态刷新后保留；安装进行中下拉框不可用。
    await shown.publish(catalog(paddle=paddle_info(OcrEngineState.DOWNLOADING)))
    assert paddle.variant_combo.currentIndex() == 0 and not paddle.variant_combo.isEnabled()


async def test_change_variant_confirms_and_reinstalls(start_settings: StartSettings) -> None:
    dialogs = FakeDialogs(confirm=False)
    harness = await start_settings(config=default_config({"ocr": {"engine": "rapid"}}), dialogs=dialogs)
    await harness.publish(catalog())
    rapid = engine_card(harness, OcrEngineKind.RAPID)
    assert rapid.status_label.text() == "就绪（DirectML）"
    assert not rapid.change_button.isHidden() and not rapid.change_button.isEnabled()
    assert rapid.variant_combo.currentText() == "DirectML · NVIDIA、AMD、Intel 显卡"
    choose(rapid.variant_combo, 2)
    assert rapid.change_button.isEnabled()
    rapid.change_button.click()
    await harness.flush()
    assert harness.recorded.of(InstallOcrEngine) == []
    text = dialogs.confirms[0][1]
    assert "从 DirectML 换成 CUDA" in text and "已下载的模型保留" in text and "当前引擎" in text

    dialogs.confirm_answer = True
    rapid.change_button.click()
    await harness.flush()
    assert harness.recorded.of(InstallOcrEngine) == [InstallOcrEngine(OcrEngineKind.RAPID, CUDA)]
    # 换好以后：下拉框回到已装版本，“更换版本”不可用。
    await harness.publish(catalog(rapid=rapid_info(installed=CUDA)))
    assert rapid.variant_combo.currentIndex() == 2 and not rapid.change_button.isEnabled()
    assert rapid.status_label.text() == "就绪（CUDA）"


async def test_engines_without_variants_keep_the_old_card(shown: Harness) -> None:
    windows = engine_card(shown, OcrEngineKind.WINDOWS)
    assert windows.variant_row.isHidden() and windows.recommend_label.isHidden()


# ---------------------------------------------------------------- 流水线：模型行


async def test_each_engine_page_shows_its_own_pipeline(shown: Harness) -> None:
    rapid = engine_page(shown, OcrEngineKind.RAPID)
    assert not rapid.pipeline.isHidden()
    assert rapid.heading.text() == "截屏与识别 › RapidOCR"
    assert rapid.note_label.isHidden()  # RapidOCR 是当前引擎
    assert rapid.pipeline.visible_stages() == [OcrStage.DETECTION, OcrStage.RECOGNITION]
    paddle = engine_page(shown, OcrEngineKind.PADDLE)
    assert paddle.pipeline.visible_stages() == [
        OcrStage.RECOGNITION,
        OcrStage.LAYOUT,
    ]  # 不管当前引擎是谁，各页固定
    detection = stage_card(shown, OcrEngineKind.RAPID, OcrStage.DETECTION)
    assert detection.title_label.text() == "文字检测"
    assert [row.radio.text() for row in detection.rows] == [
        "PP-OCRv5 检测（小）",
        "PP-OCRv5 检测（服务器版）",
    ]
    small, server = detection.rows
    assert small.radio.isChecked() and small.radio.isEnabled()
    assert not server.radio.isEnabled() and server.status_label.text() == "未下载 · 84.0 MB"
    assert "下载完成后才能选用" in server.radio.accessibleDescription()
    assert not server.download_button.isHidden() and small.delete_button.isHidden()
    recognition = stage_card(shown, OcrEngineKind.RAPID, OcrStage.RECOGNITION)
    korean = recognition.rows[2]
    assert (
        korean.status_label.text() == "正在下载 25% · 5.0 MB · 语言：韩语"
        and not korean.progress_bar.isHidden()
    )
    assert korean.progress_bar.value() == 250 and not korean.cancel_button.isHidden()


async def test_choosing_a_downloaded_model_writes_the_stage_field(shown: Harness) -> None:
    recognition = stage_card(shown, OcrEngineKind.RAPID, OcrStage.RECOGNITION)
    japanese = recognition.rows[1]
    japanese.radio.click()
    await shown.flush()
    assert shown.recorded.updates[-1].patch == {"ocr_rapid": {"recognition_model": "rec-ja"}}
    await shown.publish(
        default_config({"ocr": {"engine": "rapid"}, "ocr_rapid": {"recognition_model": "rec-ja"}}, revision=2)
    )
    assert japanese.radio.isChecked() and not recognition.rows[0].radio.isChecked()
    # 未下载的模型不能选用：按钮不可用，直接点也不发送。
    shown.recorded.clear()
    server = stage_card(shown, OcrEngineKind.RAPID, OcrStage.DETECTION).rows[1]
    server.radio.click()
    await shown.flush()
    assert shown.recorded.updates == []


async def test_download_cancel_and_retry_send_model_commands(shown: Harness) -> None:
    detection = stage_card(shown, OcrEngineKind.RAPID, OcrStage.DETECTION)
    detection.rows[1].download_button.click()
    stage_card(shown, OcrEngineKind.RAPID, OcrStage.RECOGNITION).rows[2].cancel_button.click()
    failed = rapid_models(OcrModelState.ERROR, OcrEngineErrorCode.NETWORK)
    await shown.publish(catalog(rapid=rapid_info(models=failed)))
    server = detection.rows[1]
    assert server.status_label.text() == "下载失败：网络连接失败 · 84.0 MB"
    assert server.download_button.text() == "重试"
    server.download_button.click()
    await shown.flush()
    assert [c for c in shown.recorded.commands if not isinstance(c, InstallOcrEngine)] == [
        DownloadOcrModel(OcrEngineKind.RAPID, "det-server"),
        CancelOcrModelDownload(OcrEngineKind.RAPID, "rec-ko"),
        DownloadOcrModel(OcrEngineKind.RAPID, "det-server"),
    ]


async def test_rows_update_in_place_and_rebuild_when_models_change(shown: Harness) -> None:
    detection = stage_card(shown, OcrEngineKind.RAPID, OcrStage.DETECTION)
    first = detection.rows[0]
    await shown.publish(catalog(rapid=rapid_info(models=rapid_models(OcrModelState.DOWNLOADED))))
    assert detection.rows[0] is first  # 模型列表不变：只更新文字
    assert detection.rows[1].radio.isEnabled() and detection.rows[1].status_label.text() == "已下载 · 84.0 MB"
    fewer = tuple(m for m in rapid_models() if m.id != "det-server")
    await shown.publish(catalog(rapid=rapid_info(models=fewer)))
    assert [row.model_id for row in detection.rows] == ["det-small"]


async def test_delete_asks_and_resets_a_selected_model_to_the_default(start_settings: StartSettings) -> None:
    dialogs = FakeDialogs()
    config = default_config({"ocr": {"engine": "rapid"}, "ocr_rapid": {"recognition_model": "rec-ja"}})
    harness = await start_settings(config=config, dialogs=dialogs)
    await harness.publish(catalog())
    recognition = stage_card(harness, OcrEngineKind.RAPID, OcrStage.RECOGNITION)
    chinese, japanese, _ = recognition.rows
    assert not chinese.delete_button.isHidden() and chinese.delete_button.isEnabled()
    japanese.delete_button.click()
    await harness.flush()
    assert "「日文识别」是文字识别正在选用的模型" in dialogs.confirms[0][1]
    assert "「中文识别」" in dialogs.confirms[0][1]
    commands = [c for c in harness.recorded.commands if not isinstance(c, InstallOcrEngine)]
    assert [getattr(c, "patch", c) for c in commands] == [
        {"ocr_rapid": {"recognition_model": "rec-ch"}},
        DeleteOcrModel(OcrEngineKind.RAPID, "rec-ja"),
    ]

    # 改回默认之后，默认模型正在选用：它的“删除”不可用。
    harness.recorded.clear()
    await harness.publish(default_config({"ocr": {"engine": "rapid"}}, revision=2))
    assert chinese.delete_button.isHidden() is False and not chinese.delete_button.isEnabled()
    assert "不能删除" in chinese.delete_button.toolTip()
    japanese.delete_button.click()
    await harness.flush()
    assert "删除模型「日文识别」" in dialogs.confirms[1][1]
    assert harness.recorded.commands == [DeleteOcrModel(OcrEngineKind.RAPID, "rec-ja")]


async def test_declining_the_delete_sends_nothing(start_settings: StartSettings) -> None:
    dialogs = FakeDialogs(confirm=False)
    harness = await start_settings(config=default_config({"ocr": {"engine": "rapid"}}), dialogs=dialogs)
    await harness.publish(catalog())
    stage_card(harness, OcrEngineKind.RAPID, OcrStage.RECOGNITION).rows[1].delete_button.click()
    await harness.flush()
    assert harness.recorded.commands == []


async def test_rejected_model_choice_is_shown_on_the_stage_card(shown: Harness) -> None:
    await shown.publish(ConfigRejected(("ocr_rapid", "recognition_model"), "模型未下载"))
    recognition = stage_card(shown, OcrEngineKind.RAPID, OcrStage.RECOGNITION)
    assert recognition.error_text == "未保存：模型未下载"
    assert shown.window.unclaimed_rejections() == []


# ---------------------------------------------------------------- 引擎子页与引擎卡片


async def test_manage_opens_the_engine_page_and_other_engines_are_noted(shown: Harness) -> None:
    await shown.publish(catalog(paddle=paddle_info(OcrEngineState.INSTALLED, installed=CPU)))
    window = shown.window
    window.show_page("capture.engines")
    rapid, paddle = engine_card(shown, OcrEngineKind.RAPID), engine_card(shown, OcrEngineKind.PADDLE)
    assert not rapid.manage_button.isHidden() and not paddle.manage_button.isHidden()
    paddle.manage_button.click()
    QApplication.processEvents()
    assert window.current_page.page_id == "capture.paddle"
    page = engine_page(shown, OcrEngineKind.PADDLE)
    assert "PaddleOCR 不是当前引擎" in page.note_label.text()
    assert page.pipeline.visible_stages() == [OcrStage.RECOGNITION, OcrStage.LAYOUT]
    assert not page.pipeline.isHidden() and page.empty_label.isHidden()

    await shown.publish(default_config({"ocr": {"engine": "paddle"}}, revision=2))
    assert page.note_label.isHidden()
    # 当前引擎是 Windows 自带 OCR：它没有环节和参数，子页只有流程图和一句说明。
    await shown.publish(default_config({"ocr": {"engine": "windows"}}, revision=3))
    windows = engine_page(shown, OcrEngineKind.WINDOWS)
    assert not windows.pipeline.isHidden() and not windows.empty_label.isHidden()
    assert windows.pipeline.visible_stages() == []
    assert not page.pipeline.isHidden()  # PaddleOCR 页的内容不随当前引擎变化


async def test_a_not_installed_engine_page_explains_models_need_the_engine(
    start_settings: StartSettings,
) -> None:
    harness = await start_settings(config=default_config({"ocr": {"engine": "paddle"}}))
    await harness.publish(catalog())
    page = engine_page(harness, OcrEngineKind.PADDLE)
    assert not page.install_card.isHidden()
    assert "PaddleOCR 尚未安装" in page.install_card.title_label.text()
    layout = stage_card(harness, OcrEngineKind.PADDLE, OcrStage.LAYOUT)
    assert all(row.download_button.isHidden() for row in layout.rows)


# ---------------------------------------------------------------- 参数与高级折叠区


async def test_common_params_are_shown_and_advanced_ones_fold(shown: Harness) -> None:
    group = params(shown, OcrEngineKind.RAPID)
    assert group.title_label.text() == "识别参数"
    box = group.binding(("box_thresh",))
    assert box is not None and not box.card.isHidden()
    assert box.card.title_label.text() == "检测阈值"
    assert box.card.desc_label.text() == "低于阈值的文字框会被丢弃。取值范围 0 至 1，默认 0.5。"
    assert group.binding(("detection_model",)) is None  # 选模型的字段在环节卡片里
    assert group.advanced_card.title_label.text() == "高级参数"
    assert group.advanced_card.desc_label.text() == "2 项，一般不需要修改。"
    assert group.advanced_box.isHidden() and group.advanced_button.text() == "展开(&A)"
    group.advanced_button.click()
    assert not group.advanced_box.isHidden() and group.advanced_button.text() == "收起(&A)"
    max_side = group.binding(("max_side",))
    assert max_side is not None and max_side.card.isVisibleTo(group)
    assert max_side.card.desc_label.text().endswith("修改后自动重新加载识别组件。")


async def test_reset_one_and_reset_all(shown: Harness) -> None:
    group = params(shown, OcrEngineKind.RAPID)
    box = group.binding(("box_thresh",))
    assert box is not None and box.reset_button is not None
    assert not box.reset_button.isEnabled()
    assert box.reset_button.accessibleName() == "恢复默认值：检测阈值"
    config = default_config(
        {"ocr": {"engine": "rapid"}, "ocr_rapid": {"box_thresh": 0.7, "max_side": 4000}}, revision=2
    )
    await shown.publish(config)
    assert box.reset_button.isEnabled() and box.editor.json_value() == 0.7
    box.reset_button.click()
    await shown.flush()
    assert shown.recorded.updates[-1].patch == {"ocr_rapid": {"box_thresh": 0.5}}
    assert box.editor.json_value() == 0.5

    group.reset_button.click()
    await shown.flush()
    assert "选用的模型不变" in shown.dialogs.confirms[-1][1]
    assert shown.recorded.updates[-1].patch == {
        "ocr_rapid": {"box_thresh": 0.5, "use_angle_cls": False, "max_side": 2000, "unclip_ratio": 1.6}
    }


async def test_editing_a_param_commits_to_the_engine_section(shown: Harness) -> None:
    group = params(shown, OcrEngineKind.RAPID)
    angle = group.binding(("use_angle_cls",))
    assert angle is not None
    switch = angle.editor.focus_widget()
    assert isinstance(switch, QAbstractButton)
    switch.click()
    await shown.flush()
    assert shown.recorded.updates[-1].patch == {"ocr_rapid": {"use_angle_cls": True}}


# ---------------------------------------------------------------- 结构归一化


async def test_normalize_group_shows_steps_and_disables_params_of_off_steps(shown: Harness) -> None:
    page = normalize_page(shown)
    normalize = page.normalize
    assert normalize is not None and not normalize.isHidden()
    assert normalize.title_label.text() == "结构归一化"
    assert "全部关闭时版面块原样通过" in normalize.intro_label.text()
    clean, area = normalize.binding(("clean",)), normalize.binding(("clean_min_area",))
    lines = normalize.binding(("split_max_lines",))
    assert clean is not None and area is not None and lines is not None
    assert area.card.isEnabled() and not lines.card.isEnabled()
    assert lines.card.desc_label.text().endswith("先打开上面的开关。")
    await shown.publish(
        default_config(
            {"ocr": {"engine": "rapid"}, "ocr_normalize": {"split": True, "clean": False}}, revision=2
        )
    )
    assert lines.card.isEnabled() and not area.card.isEnabled()
    assert "先打开" not in lines.card.desc_label.text()


# ---------------------------------------------------------------- 中英文与键盘


async def test_english_texts(shown: Harness) -> None:
    await shown.publish(default_config({"ui": {"language": "en"}, "ocr": {"engine": "rapid"}}, revision=3))
    paddle = engine_card(shown, OcrEngineKind.PADDLE)
    assert combo_texts(paddle.variant_combo) == [
        "CPU · no graphics card needed",
        "CUDA · NVIDIA graphics · recommended",
    ]
    assert paddle.recommend_label.text().startswith("Recommended: CUDA (an NVIDIA graphics card was found). ")
    assert engine_card(shown, OcrEngineKind.RAPID).status_label.text() == "Ready (DirectML)"
    assert engine_page(shown, OcrEngineKind.RAPID).heading.text() == "Capture & recognition › RapidOCR"
    detection = stage_card(shown, OcrEngineKind.RAPID, OcrStage.DETECTION)
    assert detection.title_label.text() == "Text detection"
    assert detection.rows[1].status_label.text() == "Not downloaded · 84.0 MB"
    assert detection.rows[1].download_button.text() == "Download"
    group = params(shown, OcrEngineKind.RAPID)
    assert group.title_label.text() == "Recognition parameters"
    box = group.binding(("box_thresh",))
    assert box is not None
    # 没有登记英文文案的字段用模型里的中文标题，说明后面的范围照常是英文。
    assert box.card.title_label.text() == "检测阈值"
    assert box.card.desc_label.text().endswith("Range 0 to 1, default 0.5.")
    normalize = normalize_page(shown).normalize
    assert normalize is not None and normalize.title_label.text() == "Layout normalization"


async def test_tab_order_follows_the_screen_with_pipeline_and_advanced_open(shown: Harness) -> None:
    group = params(shown, OcrEngineKind.RAPID)
    group.set_advanced_open(True)
    # 重建一次模型行：重建的行要接回原来的位置。
    await shown.publish(catalog(rapid=rapid_info(models=rapid_models()[:4])))
    await shown.publish(catalog())
    QApplication.processEvents()
    window, page = shown.window, engine_page(shown, OcrEngineKind.RAPID)
    detection = stage_card(shown, OcrEngineKind.RAPID, OcrStage.DETECTION)
    recognition = stage_card(shown, OcrEngineKind.RAPID, OcrStage.RECOGNITION)
    box = group.binding(("box_thresh",))
    assert box is not None
    flow = page.pipeline.flow
    # 流程图上的当前节点在前，然后是被点开的节点的面板，最后是不属于任何环节的参数。
    for stage, rows in (
        (OcrStage.DETECTION, detection.rows[:2]),
        (OcrStage.RECOGNITION, recognition.rows[1:3]),
    ):
        flow.open(stage.value)
        QApplication.processEvents()
        stops = tab_stops(window, page)
        node = flow.chart.nodes[stage.value]
        widgets = [
            rows[0].radio,
            rows[1].download_button if stage is OcrStage.DETECTION else rows[1].cancel_button,
        ]
        order = [node, *widgets, group.reset_button, box.editor.focus_widget(), group.advanced_button]
        indexes = [stops.index(widget) for widget in order]
        assert indexes == sorted(indexes)
        assert_visual_order(window, stops, "capture.rapid")
    flow.open(OcrStage.DETECTION.value)
    QApplication.processEvents()
    assert detection.rows[1].radio not in tab_stops(window, page)  # 未下载的模型不能选用，也不在 Tab 顺序里


async def test_tab_order_on_the_normalize_page(shown: Harness) -> None:
    shown.window.show_page("capture.normalize")
    page = normalize_page(shown)
    normalize = page.normalize
    assert normalize is not None
    normalize.set_advanced_open(True)
    QApplication.processEvents()
    stops = tab_stops(shown.window, page)
    assert stops.index(normalize.reset_button) < stops.index(normalize.advanced_button)
    assert_visual_order(shown.window, stops, "capture.normalize")


async def test_missing_normalize_section_leaves_a_note(start_settings: StartSettings) -> None:
    harness = await start_settings(sections=["ui", "ocr"])
    page = normalize_page(harness)
    assert page.normalize is None
    assert not page.missing_label.isHidden() and "结构归一化" in page.missing_label.text()
