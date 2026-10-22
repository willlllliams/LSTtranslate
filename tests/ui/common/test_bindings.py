"""窗口内键位（需求 UI-09，架构文档 11.8 节）：键位文字的解析与规范化、配置节的校验、键位表的查找，
以及 MouseGestures 对单击与拖动的区分。"""

import pytest
from pydantic import ValidationError
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent

from lst.ui import i18n
from lst.ui.common.bindings import (
    ACTIONS,
    DRAG_SLOP,
    MAX_BINDINGS,
    REMOVED_FIELDS,
    ActionKind,
    BindingsConfig,
    Keymap,
    MouseGestures,
    Scope,
    action_label,
    binding_label,
    bindings_from,
    parse_binding,
)

CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
ALT = Qt.KeyboardModifier.AltModifier
NONE = Qt.KeyboardModifier.NoModifier
LEFT, RIGHT, MIDDLE = Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton


def key(code: Qt.Key, modifiers: Qt.KeyboardModifier = NONE, text: str = "") -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, code, modifiers, text)


# ---------------------------------------------------------------- 解析与规范化


@pytest.mark.parametrize(
    ("text", "canonical", "kind"),
    [
        ("Ctrl+C", "Ctrl+C", "key"),
        ("ctrl+c", "Ctrl+C", "key"),
        (" Esc ", "Esc", "key"),
        ("Escape", "Esc", "key"),
        ("Ctrl+=", "Ctrl+=", "key"),
        ("Ctrl+-", "Ctrl+-", "key"),
        ("Ctrl+Return", "Ctrl+Return", "key"),
        ("shift+ctrl+s", "Ctrl+Shift+S", "key"),
        ("F5", "F5", "key"),
        ("RightClick", "RightClick", "mouse"),
        ("rightclick", "RightClick", "mouse"),
        ("Shift+Ctrl+LeftDrag", "Ctrl+Shift+LeftDrag", "mouse"),
        ("alt + middledoubleclick", "Alt+MiddleDoubleClick", "mouse"),
        ("Wheel", "Wheel", "wheel"),
        ("shift+ctrl+wheel", "Ctrl+Shift+Wheel", "wheel"),
    ],
)
def test_parse_normalizes(text: str, canonical: str, kind: str) -> None:
    binding = parse_binding(text)
    assert binding.text == canonical
    assert binding.kind == kind
    assert parse_binding(binding.text) == binding, "规范写法再解析一次结果不变"


def test_mouse_binding_fields() -> None:
    binding = parse_binding("Ctrl+Alt+RightDrag")
    assert (binding.button, binding.gesture) == ("Right", "Drag")
    assert binding.modifiers == (CTRL | ALT).value


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "Ctrl",
        "Ctrl+Shift",
        "Meta+LeftClick",
        "Win+Wheel",
        "Foo+RightClick",
        "Ctrl+C, Ctrl+V",
        "LeftHold",
    ],
)
def test_invalid_bindings_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match=r"\S"):
        parse_binding(text)


def test_error_messages_are_localized() -> None:
    i18n.set_language("zh-Hans")
    with pytest.raises(ValueError, match="不认识：Meta"):
        parse_binding("Meta+LeftClick")
    i18n.set_language("en")
    with pytest.raises(ValueError, match="not: Meta"):
        parse_binding("Meta+LeftClick")


@pytest.mark.parametrize(
    ("text", "kind", "accepted"),
    [
        ("Ctrl+C", ActionKind.TRIGGER, True),
        ("RightClick", ActionKind.TRIGGER, True),
        ("MiddleDoubleClick", ActionKind.TRIGGER, True),
        ("LeftDrag", ActionKind.TRIGGER, False),
        ("Wheel", ActionKind.TRIGGER, False),
        ("LeftDrag", ActionKind.DRAG, True),
        ("LeftClick", ActionKind.DRAG, False),
        ("Space", ActionKind.DRAG, False),
        ("Ctrl+Wheel", ActionKind.WHEEL, True),
        ("Ctrl+Z", ActionKind.WHEEL, False),
    ],
)
def test_binding_kind_must_match_action(text: str, kind: ActionKind, accepted: bool) -> None:
    assert parse_binding(text).accepts(kind) is accepted


def test_labels() -> None:
    i18n.set_language("zh-Hans")
    assert binding_label("ctrl+shift+s") == "Ctrl+Shift+S"
    assert binding_label("Ctrl+RightDrag") == "Ctrl+右键拖动"
    assert binding_label("Wheel") == "滚轮"
    assert action_label(BindingsConfig(), "capture.cancel") == "右键单击 / Esc"
    assert action_label(BindingsConfig(bilingual_close=[]), "bilingual.close") == ""
    i18n.set_language("en")
    assert binding_label("MiddleClick") == "Middle-click"


# ---------------------------------------------------------------- 配置节


def test_defaults_cover_every_action_and_are_valid() -> None:
    config = BindingsConfig()
    assert set(BindingsConfig.model_fields) == {spec.field for spec in ACTIONS}
    for spec in ACTIONS:
        assert config.bindings_of(spec) == spec.defaults
        assert all(parse_binding(text).accepts(spec.kind) for text in spec.defaults)
    # 默认值本身通过冲突校验。
    assert BindingsConfig.model_validate(config.model_dump()) == config


def test_config_normalizes_and_deduplicates() -> None:
    config = BindingsConfig.model_validate(
        {"overlay_toggle_source": "space", "overlay_move": ["ctrl+leftdrag", "Ctrl+LeftDrag"]}
    )
    assert config.overlay_toggle_source == ["Space"]
    assert config.overlay_move == ["Ctrl+LeftDrag"]
    assert config.overlay_select == ["LeftDrag"], "没写的字段取默认值"


def test_empty_list_unbinds() -> None:
    assert BindingsConfig.model_validate({"overlay_pin": []}).overlay_pin == []


@pytest.mark.parametrize(
    "section",
    [
        {"overlay_close": ["Ctrl+"]},
        {"overlay_zoom": ["RightClick"]},
        {"overlay_move": ["Ctrl+C"]},
        {"capture_cancel": ["Wheel"]},
        {"overlay_close": ["Ctrl+C", "Ctrl+Insert", "F2", "F3"][: MAX_BINDINGS + 1]},
        {"unknown_action": ["Esc"]},
    ],
)
def test_invalid_sections_are_rejected(section: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BindingsConfig.model_validate(section)


def test_conflict_within_a_window_is_rejected() -> None:
    i18n.set_language("zh-Hans")
    with pytest.raises(ValidationError, match=r"“固定在最上层（失去焦点也不关闭）”和“关闭”都用了 Esc"):
        BindingsConfig.model_validate({"overlay_pin": ["Esc"]})
    # 拖动与单击是两个键位，可以分给两个操作（默认就是右键单击切换、右键拖动移动）。
    BindingsConfig.model_validate({"overlay_pin": ["LeftClick"]})


def test_same_binding_in_different_windows_is_allowed() -> None:
    config = BindingsConfig.model_validate(
        {"capture_cancel": ["Esc"], "overlay_close": ["Esc"], "bilingual_close": ["Esc"]}
    )
    assert config.capture_cancel == config.overlay_close == config.bilingual_close == ["Esc"]


def test_removed_copy_fields_in_old_config_are_ignored() -> None:
    """旧版本的配置里有 overlay_copy、bilingual_copy 两个已经去掉的字段：读取时丢掉，
    同一节里的其他键位照常生效，哪怕旧字段的取值本身已经无效或与别的操作冲突。"""
    assert {"overlay_copy", "bilingual_copy"} == REMOVED_FIELDS
    section = {
        "overlay_copy": ["Ctrl+C", "Ctrl+Ins"],
        "bilingual_copy": ["Esc", "not a key"],
        "overlay_toggle_source": ["Space"],
    }
    config = BindingsConfig.model_validate(section)
    assert config.overlay_toggle_source == ["Space"]
    assert "overlay_copy" not in config.model_dump()
    assert "bilingual_copy" not in config.model_dump()
    assert bindings_from(section).overlay_toggle_source == ["Space"], "整节没有退回默认值"
    assert all(spec.id not in {"overlay.copy", "bilingual.copy"} for spec in ACTIONS)


def test_bindings_from_falls_back_to_defaults() -> None:
    assert bindings_from(None) == BindingsConfig()
    assert bindings_from(["not", "a", "table"]) == BindingsConfig()
    assert bindings_from({"overlay_pin": ["Esc"]}) == BindingsConfig(), "冲突的节整体退回默认值"
    assert bindings_from({"overlay_close": ["Q"]}).overlay_close == ["Q"]


# ---------------------------------------------------------------- 键位表


def test_keymap_looks_up_keys_mouse_and_wheel() -> None:
    keymap = Keymap.defaults(Scope.OVERLAY)
    assert keymap.key_action(key(Qt.Key.Key_C, CTRL)) is None, "复制用标准复制键，不在键位表里"
    assert keymap.key_action(key(Qt.Key.Key_0, CTRL)) == "overlay.zoom_reset"
    assert keymap.key_action(key(Qt.Key.Key_Escape)) == "overlay.close"
    assert keymap.key_action(key(Qt.Key.Key_Equal, CTRL)) == "overlay.zoom_in"
    assert keymap.key_action(key(Qt.Key.Key_Control, CTRL)) is None, "只按修饰键不算"
    assert keymap.mouse_action(RIGHT, NONE, "Click") == "overlay.toggle_source"
    assert keymap.mouse_action(RIGHT, NONE, "Drag") == "overlay.move"
    assert keymap.mouse_action(RIGHT, CTRL, "Click") is None, "修饰键要完全一致"
    assert keymap.mouse_action(Qt.MouseButton.BackButton, NONE, "Click") is None
    assert keymap.wheel_action(NONE) == "overlay.zoom"
    assert keymap.wheel_action(CTRL) is None
    # 键位表只含本窗口的操作。
    assert Keymap.defaults(Scope.CAPTURE).key_action(key(Qt.Key.Key_0, CTRL)) is None


def test_keymap_keypad_enter_matches_return() -> None:
    keymap = Keymap.defaults(Scope.BILINGUAL)
    keypad = CTRL | Qt.KeyboardModifier.KeypadModifier
    assert keymap.key_action(key(Qt.Key.Key_Enter, keypad)) == "bilingual.translate"
    assert keymap.key_action(key(Qt.Key.Key_Return, CTRL)) == "bilingual.translate"


def test_keymap_follows_config() -> None:
    config = BindingsConfig.model_validate(
        {"overlay_zoom": ["Ctrl+Wheel"], "overlay_toggle_source": ["Space"]}
    )
    keymap = Keymap.for_scope(config, Scope.OVERLAY)
    assert keymap.wheel_action(NONE) is None
    assert keymap.wheel_action(CTRL) == "overlay.zoom"
    assert keymap.key_action(key(Qt.Key.Key_Space, NONE, " ")) == "overlay.toggle_source"
    assert keymap.mouse_action(RIGHT, NONE, "Click") is None


# ---------------------------------------------------------------- 单击与拖动


def test_click_without_drag_binding_fires_on_press() -> None:
    gestures = MouseGestures(Keymap.defaults(Scope.OVERLAY))
    assert gestures.press(MIDDLE, NONE, QPointF(0, 0)) == ("click", "overlay.pin")
    assert gestures.move(QPointF(50, 50)) is None
    assert gestures.release(MIDDLE) is None, "按下时已经执行过，松开不再执行"


def test_small_movement_is_a_click_and_large_movement_a_drag() -> None:
    gestures = MouseGestures(Keymap.defaults(Scope.OVERLAY))
    assert gestures.press(RIGHT, NONE, QPointF(10, 10)) is None
    assert gestures.move(QPointF(10 + DRAG_SLOP, 10)) is None
    assert not gestures.dragging
    assert gestures.release(RIGHT) == ("click", "overlay.toggle_source")

    gestures.press(RIGHT, NONE, QPointF(10, 10))
    assert gestures.move(QPointF(10 + DRAG_SLOP + 0.5, 10)) == ("start", "overlay.move")
    assert gestures.dragging
    assert gestures.move(QPointF(12, 10)) == ("move", "overlay.move"), "开始拖动后不再看距离"
    assert gestures.release(RIGHT) == ("end", "overlay.move")
    assert gestures.origin is None


def test_tap_on_a_drag_button_without_click_action() -> None:
    gestures = MouseGestures(Keymap.defaults(Scope.OVERLAY))
    assert gestures.press(LEFT, NONE, QPointF(0, 0)) is None
    assert gestures.drag_action == "overlay.select"
    assert gestures.release(LEFT) == ("tap", "overlay.select")


def test_unbound_button_does_nothing() -> None:
    gestures = MouseGestures(Keymap.defaults(Scope.CAPTURE))
    assert gestures.press(MIDDLE, NONE, QPointF(0, 0)) is None
    assert gestures.move(QPointF(100, 100)) is None
    assert gestures.release(MIDDLE) is None


def test_modifiers_select_the_binding() -> None:
    config = BindingsConfig.model_validate({"overlay_move": ["Ctrl+LeftDrag"]})
    gestures = MouseGestures(Keymap.for_scope(config, Scope.OVERLAY))
    gestures.press(LEFT, CTRL, QPointF(0, 0))
    assert gestures.move(QPointF(30, 0)) == ("start", "overlay.move")
    assert gestures.release(LEFT) == ("end", "overlay.move")
    gestures.press(LEFT, NONE, QPointF(0, 0))
    assert gestures.move(QPointF(30, 0)) == ("start", "overlay.select")


def test_second_button_during_drag() -> None:
    """跟踪一个按键期间按下另一个键：按下就生效的单击照常执行，正在进行的拖动不受影响。"""
    gestures = MouseGestures(Keymap.defaults(Scope.CAPTURE))
    gestures.press(LEFT, NONE, QPointF(0, 0))
    assert gestures.move(QPointF(100, 0)) == ("start", "capture.select")
    assert gestures.press(RIGHT, NONE, QPointF(100, 0)) == ("click", "capture.cancel")
    assert gestures.release(RIGHT) is None
    assert gestures.move(QPointF(120, 0)) == ("move", "capture.select")
    assert gestures.release(LEFT) == ("end", "capture.select")

    overlay = MouseGestures(Keymap.defaults(Scope.OVERLAY))
    overlay.press(LEFT, NONE, QPointF(0, 0))
    assert overlay.press(RIGHT, NONE, QPointF(0, 0)) is None, "右键绑定了拖动，要等松开，这里忽略"
    assert overlay.release(RIGHT) is None
    assert overlay.release(LEFT) == ("tap", "overlay.select")


def test_keymap_swap_keeps_the_ongoing_press() -> None:
    gestures = MouseGestures(Keymap.defaults(Scope.OVERLAY))
    gestures.press(RIGHT, NONE, QPointF(0, 0))
    gestures.keymap = Keymap.for_scope(BindingsConfig(overlay_move=[]), Scope.OVERLAY)
    assert gestures.move(QPointF(30, 0)) == ("start", "overlay.move"), "按下时的拖动操作做完"
    gestures.release(RIGHT)
    # 新键位里右键没有拖动操作，单击在按下时就生效。
    assert gestures.press(RIGHT, NONE, QPointF(0, 0)) == ("click", "overlay.toggle_source")
    assert gestures.move(QPointF(30, 0)) is None
    assert gestures.release(RIGHT) is None
