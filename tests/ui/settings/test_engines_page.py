"""翻译引擎页的测试：列表、拖拽排序后的优先级重新编号、增删实例、参数编辑、API key、测试连接。"""

import logging

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QLabel, QLineEdit

from lst.contracts.config import ConfigRejected, UpdateConfig
from lst.contracts.errors import ErrorCode
from lst.contracts.events import (
    CheckEngineConnection,
    EngineConnectionChecked,
    EngineHealth,
    EngineStatus,
    EngineStatuses,
)
from lst.contracts.ids import EngineId
from lst.contracts.worker_protocol import JsonValue
from lst.ui.settings.controls import IntEditor, SwitchEditor, TextEditor
from lst.ui.settings.page_engines import AddEngineDialog, EnginesPage
from tests.ui.settings.conftest import Harness, StartSettings, wait_until
from tests.ui.settings.support import default_config, engine

KEY = "sk-live-SECRET-0123456789"
IDS = ["argos", "deepseek", "deepl", "ollama"]


def statuses_for(**items: EngineHealth | EngineStatus) -> EngineStatuses:
    mapping = {EngineId(k): v if isinstance(v, EngineStatus) else EngineStatus(v) for k, v in items.items()}
    names = {
        EngineId("argos"): "Argos",
        EngineId("deepseek"): "DeepSeek",
        EngineId("deepl"): "DeepL",
        EngineId("ollama"): "Ollama",
    }
    return EngineStatuses(mapping, names, {})


async def open_engines(harness: Harness, select: str | None = "deepseek") -> EnginesPage:
    window = await harness.open()
    window.show_page("engines")
    page = window.page("engines")
    assert isinstance(page, EnginesPage)
    if select is not None:
        page.select(select)
    return page


def rows(page: EnginesPage) -> list[str]:
    return [page.list.item(row).text() for row in range(page.list.count())]


def sent_engines(harness: Harness, index: int = -1) -> list[dict[str, JsonValue]]:
    value = harness.recorded.updates[index].patch["engines"]
    assert isinstance(value, list)
    return [item for item in value if isinstance(item, dict)]


def last_update(harness: Harness) -> UpdateConfig:
    return harness.recorded.updates[-1]


# ---------------------------------------------------------------- 列表


async def test_list_shows_instances_by_priority_with_status(harness: Harness) -> None:
    await harness.publish(
        statuses_for(
            argos=EngineHealth.READY,
            deepseek=EngineStatus(EngineHealth.ERROR, "", ErrorCode.AUTH),
            deepl=EngineHealth.LOADING,
            ollama=EngineHealth.BUSY,
        )
    )
    page = await open_engines(harness, None)
    assert page.ids() == IDS
    assert rows(page) == [
        "1. Argos（就绪）",
        "2. DeepSeek（出错）",
        "3. DeepL（加载中）",
        "4. Ollama（忙碌）",
    ]
    # 出错的实例悬停时给出原因和处理建议，不显示技术细节。
    tip = page.list.item(1).toolTip()
    assert "API key" in tip and "设置" in tip
    assert page.list.item(0).toolTip() == ""
    assert not page.list.item(0).icon().isNull()  # 标识颜色圆点


async def test_list_follows_priority_not_file_order(start_settings: StartSettings) -> None:
    config = default_config(engines=[engine("deepl", "b", 2), engine("local_nmt", "a", 1)])
    harness = await start_settings(config=config)
    assert harness.window.page("engines").ids() == ["a", "b"]  # type: ignore[attr-defined]


async def test_disabled_instances_are_unchecked_and_say_so(start_settings: StartSettings) -> None:
    config = default_config(engines=[engine("local_nmt", "a", 1), engine("deepl", "b", 2, enabled=False)])
    harness = await start_settings(config=config)
    page = await open_engines(harness, None)
    assert page.list.item(0).checkState() == Qt.CheckState.Checked
    assert page.list.item(1).checkState() == Qt.CheckState.Unchecked
    assert rows(page)[1].endswith("（未启用）")


async def test_unchecking_disables_the_instance(harness: Harness) -> None:
    page = await open_engines(harness)
    page.list.item(2).setCheckState(Qt.CheckState.Unchecked)
    await harness.flush()
    entries = sent_engines(harness)
    assert entries[2]["id"] == "deepl" and entries[2]["enabled"] is False
    assert all(e["enabled"] is True for i, e in enumerate(entries) if i != 2)


async def test_list_rows_are_draggable_and_checkable(harness: Harness) -> None:
    page = await open_engines(harness)
    flags = page.list.item(0).flags()
    assert flags & Qt.ItemFlag.ItemIsDragEnabled and flags & Qt.ItemFlag.ItemIsUserCheckable
    assert page.list.dragDropMode().name == "InternalMove"


async def test_refreshes_do_not_rebuild_the_list_or_lose_the_selection(harness: Harness) -> None:
    page = await open_engines(harness)
    item = page.list.item(1)
    await harness.publish(statuses_for(argos=EngineHealth.READY, deepseek=EngineHealth.READY))
    await harness.publish(default_config(revision=2))
    assert page.list.item(1) is item  # 顺序不变时只更新文字，不重建
    assert page.selected_id == "deepseek"


# ---------------------------------------------------------------- 排序


async def test_drag_and_drop_renumbers_priorities_from_one(harness: Harness) -> None:
    page = await open_engines(harness)
    # 拖拽放下时 Qt 对模型做的就是这个操作（QListWidget.dropEvent 调用 model().moveRow）。
    page.list.model().moveRow(QModelIndex(), 3, QModelIndex(), 0)  # Ollama 拖到最前
    await harness.flush()
    entries = sent_engines(harness)
    assert [(e["id"], e["priority"]) for e in entries] == [
        ("ollama", 1),
        ("argos", 2),
        ("deepseek", 3),
        ("deepl", 4),
    ]
    assert rows(page)[0].startswith("1. Ollama")  # 序号立即按新顺序重写


async def test_drag_down_to_the_end(harness: Harness) -> None:
    page = await open_engines(harness)
    page.list.model().moveRow(QModelIndex(), 0, QModelIndex(), 4)
    await harness.flush()
    assert [e["id"] for e in sent_engines(harness)] == ["deepseek", "deepl", "ollama", "argos"]
    assert [e["priority"] for e in sent_engines(harness)] == [1, 2, 3, 4]


async def test_reorder_renumbers_even_when_priorities_had_gaps(start_settings: StartSettings) -> None:
    config = default_config(
        engines=[engine("local_nmt", "a", 1), engine("deepl", "b", 5), engine("google", "c", 9)]
    )
    harness = await start_settings(config=config)
    page = await open_engines(harness, "c")
    page.list.model().moveRow(QModelIndex(), 2, QModelIndex(), 0)
    await harness.flush()
    assert [(e["id"], e["priority"]) for e in sent_engines(harness)] == [("c", 1), ("a", 2), ("b", 3)]


async def test_move_buttons_use_the_same_path(harness: Harness) -> None:
    page = await open_engines(harness, "deepl")
    assert page.up_button.isEnabled() and page.down_button.isEnabled()
    page.up_button.click()
    await harness.flush()
    assert [e["id"] for e in sent_engines(harness)] == ["argos", "deepl", "deepseek", "ollama"]
    assert page.selected_id == "deepl"  # 移动后仍选中它
    page.down_button.click()
    page.down_button.click()
    await harness.flush()
    assert [e["id"] for e in sent_engines(harness)] == ["argos", "deepseek", "ollama", "deepl"]
    assert [e["priority"] for e in sent_engines(harness)] == [1, 2, 3, 4]


async def test_move_buttons_are_disabled_at_the_edges(harness: Harness) -> None:
    page = await open_engines(harness, "argos")
    assert not page.up_button.isEnabled() and page.down_button.isEnabled()
    page.select("ollama")
    assert page.up_button.isEnabled() and not page.down_button.isEnabled()


async def test_a_second_edit_right_after_a_reorder_keeps_the_reorder(harness: Harness) -> None:
    page = await open_engines(harness, "deepl")
    page.up_button.click()
    editor = page.editor_editor("name")
    assert isinstance(editor, TextEditor)
    editor.edit.setText("DeepL 新")
    editor.edit.textEdited.emit("DeepL 新")
    editor.edit.editingFinished.emit()
    await harness.flush()
    entries = sent_engines(harness)
    assert [e["id"] for e in entries] == ["argos", "deepl", "deepseek", "ollama"]
    assert entries[1]["name"] == "DeepL 新"


# ---------------------------------------------------------------- 参数表单


async def test_editor_shows_the_parameters_of_the_selected_kind(harness: Harness) -> None:
    page = await open_engines(harness, "deepseek")
    names = [b.location[-1] for b in page.all_bindings() if b.location[:2] == ("engines", "deepseek")]
    assert names[:2] == ["name", "enabled"]
    assert {"provider", "base_url", "model", "temperature", "system_prompt", "max_batch_chars"} <= set(names)
    assert names[-4:] == ["color", "timeout_s", "max_concurrency", "max_retries"]
    assert not {"id", "priority", "secret"} & set(names)  # 技术字段不显示
    assert page.secret_field is not None
    assert page.editor_title.text() == "DeepSeek 的参数"

    page.select("argos")  # 本地 NMT：没有 API key 卡片，也没有网络参数
    names = [b.location[-1] for b in page.all_bindings() if b.location[:2] == ("engines", "argos")]
    assert page.secret_field is None
    assert "base_url" not in names and "processing_timeout_s" in names


async def test_every_kind_has_labelled_cards_for_all_its_fields(start_settings: StartSettings) -> None:
    from tests.ui.settings.support import engine_kinds

    config = default_config(
        engines=[
            engine("local_nmt", "n", 1),
            engine("openai_compat", "o", 2),
            engine("deepl", "d", 3),
            engine("google", "g", 4),
            engine("baidu", "b", 5),
        ]
    )
    harness = await start_settings(config=config)
    page = await open_engines(harness, "n")
    models = engine_kinds()
    for engine_id, kind in (
        ("n", "local_nmt"),
        ("o", "openai_compat"),
        ("d", "deepl"),
        ("g", "google"),
        ("b", "baidu"),
    ):
        page.select(engine_id)
        fields = set(models[kind].model_fields) - {"id", "priority", "secret"}
        cards = {
            b.location[-1]: b.card for b in page.all_bindings() if b.location[:2] == ("engines", engine_id)
        }
        assert set(cards) == fields, kind
        for name, card in cards.items():
            assert card.title_label.text() and card.title_label.text() != name, (kind, name)  # 已登记中文标题


async def test_editor_controls_match_field_types(harness: Harness) -> None:
    page = await open_engines(harness, "deepseek")
    enabled = page.editor_editor("enabled")
    assert isinstance(enabled, SwitchEditor) and enabled.json_value() is True
    provider = page.editor_editor("provider")
    assert provider is not None and provider.json_value() == "deepseek"
    temperature = page.editor_editor("temperature")
    assert temperature is not None and temperature.json_value() == 0.2
    assert page.editor_editor("max_concurrency").json_value() == 2  # type: ignore[union-attr]
    assert page.editor_editor("system_prompt").json_value() is None  # type: ignore[union-attr]
    assert page.editor_editor("languages").json_value() is None  # type: ignore[union-attr]
    assert page.editor_editor("first_token_timeout_s").json_value() is None  # type: ignore[union-attr]


async def test_editing_a_parameter_sends_the_whole_validated_entry(harness: Harness) -> None:
    page = await open_engines(harness)
    editor = page.editor_editor("model")
    assert isinstance(editor, TextEditor)
    editor.edit.setText("deepseek-v4-pro")
    editor.edit.textEdited.emit("deepseek-v4-pro")
    await wait_until(lambda: bool(harness.recorded.updates))
    await harness.flush()
    entries = sent_engines(harness)
    assert [e["id"] for e in entries] == IDS
    assert entries[1]["model"] == "deepseek-v4-pro" and entries[1]["priority"] == 2


async def test_numeric_parameter_uses_the_model_range(harness: Harness) -> None:
    page = await open_engines(harness)
    editor = page.editor_editor("max_concurrency")
    assert isinstance(editor, IntEditor)
    spin = editor.spin
    assert (spin.minimum(), spin.maximum()) == (1, 32)  # 来自 Field(ge=1, le=32)
    spin.setValue(4)
    await wait_until(lambda: bool(harness.recorded.updates))
    await harness.flush()
    assert sent_engines(harness)[1]["max_concurrency"] == 4


async def test_invalid_parameter_is_shown_beside_the_field_and_the_instance_survives(
    harness: Harness,
) -> None:
    page = await open_engines(harness)
    editor = page.editor_editor("base_url")
    card = page.editor_card("base_url")
    assert isinstance(editor, TextEditor) and card is not None
    editor.edit.setText("ftp://nope")
    editor.edit.textEdited.emit("ftp://nope")
    editor.edit.editingFinished.emit()
    await harness.flush()
    assert harness.recorded.updates == []  # 不发送：否则配置服务会把整个实例丢弃
    assert "http" in card.error_text
    assert page.ids() == IDS

    editor.edit.setText("https://api.deepseek.com/v1")  # 换一个合法值：提示消失，修改发出
    editor.edit.textEdited.emit("https://api.deepseek.com/v1")
    assert card.error_text == ""
    editor.edit.editingFinished.emit()
    await harness.flush()
    assert sent_engines(harness)[1]["base_url"] == "https://api.deepseek.com/v1"


async def test_empty_required_text_is_reported(harness: Harness) -> None:
    page = await open_engines(harness)
    editor = page.editor_editor("name")
    card = page.editor_card("name")
    assert isinstance(editor, TextEditor) and card is not None
    editor.edit.setText("")
    editor.edit.textEdited.emit("")
    editor.edit.editingFinished.emit()
    assert card.error_text == "未保存：不能为空"
    assert harness.recorded.updates == []


async def test_service_rejection_is_mapped_to_the_instance_field(harness: Harness) -> None:
    page = await open_engines(harness)
    editor = page.editor_editor("model")
    assert isinstance(editor, TextEditor)
    editor.edit.setText("m2")
    editor.edit.textEdited.emit("m2")
    editor.edit.editingFinished.emit()
    await harness.flush()
    await harness.publish(ConfigRejected(("engines", 1, "model"), "服务端拒绝"))
    card = page.editor_card("model")
    assert card is not None and card.error_text == "未保存：服务端拒绝"
    assert page.editor_card("base_url").error_text == ""  # type: ignore[union-attr]


async def test_switching_instance_commits_the_pending_edit_of_the_previous_one(
    start_settings: StartSettings,
) -> None:
    harness = await start_settings(debounce_ms=5000)
    page = await open_engines(harness, "deepseek")
    editor = page.editor_editor("name")
    assert isinstance(editor, TextEditor)
    editor.edit.setText("改名")
    editor.edit.textEdited.emit("改名")
    page.select("deepl")  # 还在防抖等待中就切换了实例
    await harness.flush()
    assert sent_engines(harness)[1]["name"] == "改名"
    assert page.editor_title.text() == "DeepL 的参数"


async def test_config_changes_refresh_the_open_editor(harness: Harness) -> None:
    page = await open_engines(harness)
    changed = default_config(
        engines=[
            engine("local_nmt", "argos", 1),
            engine("openai_compat", "deepseek", 2, model="外部修改"),
            engine("deepl", "deepl", 3),
            engine(
                "openai_compat",
                "ollama",
                4,
                provider="ollama",
                base_url="http://x/v1",
                secret=None,
                model="m",
            ),
        ],
        revision=2,
    )
    await harness.publish(changed)
    assert page.editor_editor("model").json_value() == "外部修改"  # type: ignore[union-attr]


# ---------------------------------------------------------------- 添加与删除


async def add_dialog(page: EnginesPage) -> AddEngineDialog:
    page.add_button.click()
    assert page.add_dialog is not None
    return page.add_dialog


def pick(dialog: AddEngineDialog, kind: str) -> None:
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData(kind))


async def test_add_dialog_requires_the_models_required_fields(harness: Harness) -> None:
    page = await open_engines(harness)
    dialog = await add_dialog(page)
    pick(dialog, "baidu")
    assert not dialog.add_button.isEnabled()  # 应用 ID 是模型里没有默认值的字段
    assert "应用 ID" in dialog.problem_label.text()
    dialog.field_edit("appid").setText("2015063000000001")
    assert dialog.add_button.isEnabled() and dialog.problem_label.text() == ""
    dialog.key_edit.setText(KEY)
    dialog.add_button.click()
    await harness.flush()
    assert page.add_dialog is None  # 对话框关闭
    secret, _update = harness.recorded.commands
    assert (secret.name, secret.value) == ("engine", KEY)  # type: ignore[attr-defined]
    entry = sent_engines(harness)[-1]
    assert entry["kind"] == "baidu" and entry["priority"] == 5 and entry["secret"] == "engine"


async def test_add_dialog_prefills_from_the_provider_preset(harness: Harness) -> None:
    page = await open_engines(harness)
    dialog = await add_dialog(page)
    pick(dialog, "openai_compat")
    assert dialog.provider_combo.isVisibleTo(dialog)
    dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("deepseek"))
    assert dialog.name_edit.text() == "DeepSeek"
    assert dialog.field_edit("base_url").text() == "https://api.deepseek.com"
    assert dialog.field_edit("model").text() == "deepseek-flash"
    assert dialog.add_button.isEnabled()
    dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("ollama"))
    assert dialog.field_edit("model").text() == ""  # Ollama 的模型名由用户决定
    assert not dialog.add_button.isEnabled()
    dialog.field_edit("model").setText("hy-mt2")
    assert dialog.add_button.isEnabled()
    values = dialog.values()
    assert values["provider"] == "ollama" and values["timeout_s"] == 60.0


async def test_add_dialog_hides_key_for_local_nmt_and_cancel_sends_nothing(harness: Harness) -> None:
    page = await open_engines(harness)
    dialog = await add_dialog(page)
    pick(dialog, "local_nmt")
    assert not dialog.key_edit.isVisibleTo(dialog)
    assert not dialog.provider_combo.isVisibleTo(dialog)
    dialog.cancel_button.click()
    await harness.flush()
    assert page.add_dialog is None and harness.recorded.commands == []


async def test_add_dialog_lists_every_registered_kind_with_names(harness: Harness) -> None:
    page = await open_engines(harness)
    dialog = await add_dialog(page)
    assert [dialog.kind_combo.itemData(i) for i in range(dialog.kind_combo.count())] == [
        "openai_compat",
        "deepl",
        "google",
        "baidu",
        "local_nmt",
    ]
    assert dialog.kind_combo.itemText(0) == "OpenAI 兼容的大语言模型"


async def test_new_instance_is_selected_once_it_appears(harness: Harness) -> None:
    page = await open_engines(harness, "argos")
    dialog = await add_dialog(page)
    pick(dialog, "deepl")
    dialog.add_button.click()
    await harness.flush()
    added = sent_engines(harness)
    assert added[-1]["id"] == "deepl-2"
    await harness.publish(default_config(engines=added, revision=2))
    assert page.ids()[-1] == "deepl-2" and page.selected_id == "deepl-2"
    assert page.editor_title.text() == "DeepL 的参数"


async def test_remove_asks_for_confirmation(harness: Harness) -> None:
    page = await open_engines(harness, "deepseek")
    harness.dialogs.confirm_answer = False
    page.remove_button.click()
    await harness.flush()
    assert harness.recorded.commands == []
    assert "DeepSeek" in harness.dialogs.confirms[-1][1]

    harness.dialogs.confirm_answer = True
    page.remove_button.click()
    await harness.flush()
    update, secret = harness.recorded.commands
    assert [(e["id"], e["priority"]) for e in sent_engines(harness)] == [
        ("argos", 1),
        ("deepl", 2),
        ("ollama", 3),
    ]
    assert (secret.name, secret.value) == ("deepseek", None)  # type: ignore[attr-defined]
    assert isinstance(update, UpdateConfig)


async def test_removing_every_instance_shows_the_empty_hint(start_settings: StartSettings) -> None:
    harness = await start_settings(config=default_config(engines=[engine("local_nmt", "argos", 1)]))
    page = await open_engines(harness, "argos")
    page.remove_button.click()
    await harness.flush()
    await harness.publish(default_config(engines=[], revision=2))
    assert page.list.count() == 0 and not page.empty_label.isHidden()
    assert page.selected_id is None and not page.remove_button.isEnabled()
    assert page.editor_title.text() == "" and not page.select_hint.isHidden()


# ---------------------------------------------------------------- API key


def all_visible_text(page: EnginesPage) -> str:
    texts = [label.text() for label in page.findChildren(QLabel)]
    texts += [edit.text() for edit in page.findChildren(QLineEdit)]
    texts += [edit.placeholderText() for edit in page.findChildren(QLineEdit)]
    return "\n".join(texts)


async def test_api_key_is_submitted_by_update_secret_and_never_shown(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    page = await open_engines(harness)
    field = page.secret_field
    assert field is not None
    with caplog.at_level(logging.DEBUG):
        field.editor.edit.setText(KEY)
        field.editor.edit.textEdited.emit(KEY)
        field.editor.edit.editingFinished.emit()
        await harness.flush()
    (secret,) = harness.recorded.secrets
    assert (secret.name, secret.value) == ("deepseek", KEY)
    assert harness.recorded.updates == []  # 不经过配置
    assert field.editor.edit.text() == ""  # 提交后清空
    assert KEY not in all_visible_text(page)
    assert KEY not in caplog.text
    assert KEY not in repr(harness.recorded.commands)
    assert field.editor.edit.echoMode() == QLineEdit.EchoMode.Password


async def test_api_key_debounce_commit_keeps_typing_intact(start_settings: StartSettings) -> None:
    harness = await start_settings(debounce_ms=30)
    page = await open_engines(harness)
    field = page.secret_field
    assert field is not None
    field.editor.edit.setText("sk-part")
    field.editor.edit.textEdited.emit("sk-part")
    await wait_until(lambda: bool(harness.recorded.commands) or bool(field.editor.pending) is False)
    await harness.flush()
    assert [c.value for c in harness.recorded.secrets] == ["sk-part"]
    assert field.editor.edit.text() == "sk-part"  # 还在输入时不清空
    field.editor.edit.editingFinished.emit()
    assert field.editor.edit.text() == ""


async def test_key_status_label_follows_the_engine_status(harness: Harness) -> None:
    await harness.publish(statuses_for(deepseek=EngineStatus(EngineHealth.ERROR, "", ErrorCode.AUTH)))
    page = await open_engines(harness)
    field = page.secret_field
    assert field is not None and field.status_label.text() == "未设置"
    assert not field.clear_button.isEnabled()
    await harness.publish(statuses_for(deepseek=EngineHealth.READY))
    assert field.status_label.text() == "已设置" and field.clear_button.isEnabled()
    page.select("deepl")
    await harness.publish(statuses_for(deepl=EngineHealth.DISABLED))
    assert page.secret_field is not None and page.secret_field.status_label.text() == "启用后显示状态"


async def test_optional_key_for_local_llm_service(harness: Harness) -> None:
    page = await open_engines(harness, "ollama")
    assert page.secret_field is not None
    assert page.secret_field.status_label.text() == "未设置（可选）"


async def test_clear_key_button_deletes_the_stored_key(harness: Harness) -> None:
    await harness.publish(statuses_for(deepseek=EngineHealth.READY))
    page = await open_engines(harness)
    assert page.secret_field is not None
    page.secret_field.clear_button.click()
    await harness.flush()
    (secret,) = harness.recorded.secrets
    assert (secret.name, secret.value) == ("deepseek", None)
    assert page.secret_field.status_label.text() == "未设置"


async def test_secret_rejection_is_shown_at_the_key_card(harness: Harness) -> None:
    page = await open_engines(harness)
    await harness.publish(ConfigRejected(("secrets", "deepseek"), "写入密钥失败：凭据管理器不可用"))
    assert page.secret_field is not None
    card = page._key_card
    assert card is not None and "凭据管理器不可用" in card.error_text
    assert page.claims(("engines", "deepseek", "api_key"))
    assert harness.window.unclaimed_rejections() == []


# ---------------------------------------------------------------- 测试连接


async def test_connection_test_button_round_trip(harness: Harness) -> None:
    page = await open_engines(harness)
    page.test_button.click()
    await harness.flush()
    assert harness.recorded.of(CheckEngineConnection) == [CheckEngineConnection(EngineId("deepseek"))]
    assert page.test_label.text() == "正在测试…" and not page.test_button.isEnabled()

    await harness.publish(EngineConnectionChecked(EngineId("deepseek"), ok=True, latency_ms=321.0))
    assert page.test_label.text() == "连接正常（321 毫秒）" and page.test_button.isEnabled()

    page.test_button.click()
    await harness.publish(EngineConnectionChecked(EngineId("deepseek"), ok=False, error_code=ErrorCode.AUTH))
    text = page.test_label.text()
    assert text.startswith("连接失败：API key 无效或没有权限。") and "检查 API key" in text
    assert "engine.auth" not in text  # 不显示错误码这类技术细节


async def test_connection_result_belongs_to_the_selected_instance(harness: Harness) -> None:
    page = await open_engines(harness, "deepseek")
    await harness.publish(EngineConnectionChecked(EngineId("deepseek"), ok=True))
    assert page.test_label.text() == "连接正常"
    page.select("deepl")
    assert page.test_label.text() == ""  # deepl 还没测过
    page.select("deepseek")
    assert page.test_label.text() == "连接正常"


async def test_language_switch_relabels_the_list_and_the_form(harness: Harness) -> None:
    await harness.publish(statuses_for(argos=EngineHealth.READY))
    page = await open_engines(harness)
    await harness.publish(default_config({"ui": {"language": "en"}}, revision=3))
    assert rows(page)[0] == "1. Argos (Ready)"
    assert page.test_button.text() == "&Test connection"
    assert page.editor_title.text() == "Settings of DeepSeek"
    assert page.editor_card("base_url").title_label.text() == "Endpoint"  # type: ignore[union-attr]
