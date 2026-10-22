"""窗口内的按键与鼠标操作键位（需求 UI-09，架构文档 11.8 节）。

截屏蒙版、覆盖窗口与双语窗口里的操作都可以在设置里改键位。每个操作（``ActionSpec``）有一组键位，
写在配置的 ``[bindings]`` 节，字段名是操作 id 把点换成下划线（例如 ``overlay_toggle_source``），
取值是键位文字的列表，空列表表示不绑定。

键位文字有三类：

- 键盘：一个组合键，按 ``QKeySequence`` 的可移植写法，例如 ``Ctrl+C``、``Esc``、``Ctrl+=``、``Ctrl+Return``；
- 鼠标按键：可选的修饰键加按键与动作，按键是 Left、Right、Middle，动作是 Click、DoubleClick、Drag，
  例如 ``RightClick``、``Ctrl+LeftDrag``、``MiddleDoubleClick``；
- 滚轮：可选的修饰键加 ``Wheel``，例如 ``Ctrl+Wheel``。

鼠标与滚轮的修饰键只认 Ctrl、Alt、Shift，按这个顺序写。操作分三种（``ActionKind``），各自只接受对应的键位：

- 触发（TRIGGER）：键盘、单击、双击，例如关闭、固定；
- 拖动（DRAG）：按住某个键拖动，例如移动窗口、选择文字；
- 滚轮（WHEEL）：例如缩放。

同一个窗口里的两个操作不能用同一个键位，配置校验时报错。同一个按键的单击与拖动可以分给两个操作：
按下后移动超过 4 个逻辑像素算拖动，否则松开时算单击；这个按键加修饰键没有绑定拖动时，单击在按下时就生效
（``MouseGestures``）。
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from PySide6.QtCore import QKeyCombination, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence

from lst.ui.i18n import register_catalog, tr


class ActionKind(StrEnum):
    TRIGGER = "trigger"
    DRAG = "drag"
    WHEEL = "wheel"


class Scope(StrEnum):
    """键位生效的窗口。同一个窗口里的键位不能重复。"""

    CAPTURE = "capture"
    OVERLAY = "overlay"
    BILINGUAL = "bilingual"


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """一个可改键位的操作。id 形如 ``overlay.toggle_source``，defaults 是默认键位。"""

    id: str
    scope: Scope
    kind: ActionKind
    defaults: tuple[str, ...]

    @property
    def field(self) -> str:
        """配置 [bindings] 节里的字段名。"""
        return self.id.replace(".", "_")


ACTIONS: Final[tuple[ActionSpec, ...]] = (
    ActionSpec("capture.select", Scope.CAPTURE, ActionKind.DRAG, ("LeftDrag",)),
    ActionSpec("capture.cancel", Scope.CAPTURE, ActionKind.TRIGGER, ("RightClick", "Esc")),
    ActionSpec("overlay.toggle_source", Scope.OVERLAY, ActionKind.TRIGGER, ("RightClick",)),
    ActionSpec("overlay.move", Scope.OVERLAY, ActionKind.DRAG, ("RightDrag",)),
    ActionSpec("overlay.zoom", Scope.OVERLAY, ActionKind.WHEEL, ("Wheel",)),
    ActionSpec("overlay.zoom_in", Scope.OVERLAY, ActionKind.TRIGGER, ("Ctrl+=",)),
    ActionSpec("overlay.zoom_out", Scope.OVERLAY, ActionKind.TRIGGER, ("Ctrl+-",)),
    ActionSpec("overlay.zoom_reset", Scope.OVERLAY, ActionKind.TRIGGER, ("Ctrl+0",)),
    ActionSpec("overlay.pin", Scope.OVERLAY, ActionKind.TRIGGER, ("MiddleClick",)),
    ActionSpec("overlay.select", Scope.OVERLAY, ActionKind.DRAG, ("LeftDrag",)),
    ActionSpec("overlay.close", Scope.OVERLAY, ActionKind.TRIGGER, ("Esc",)),
    ActionSpec("bilingual.translate", Scope.BILINGUAL, ActionKind.TRIGGER, ("Ctrl+Return",)),
    ActionSpec("bilingual.swap", Scope.BILINGUAL, ActionKind.TRIGGER, ("Ctrl+Shift+S",)),
    ActionSpec("bilingual.pin", Scope.BILINGUAL, ActionKind.TRIGGER, ("Ctrl+Shift+T",)),
    ActionSpec("bilingual.clear", Scope.BILINGUAL, ActionKind.TRIGGER, ("Ctrl+Shift+L",)),
    ActionSpec("bilingual.close", Scope.BILINGUAL, ActionKind.TRIGGER, ("Esc",)),
)
"""全部可改键位的操作，也是设置页列出的顺序。默认键位与 0.2 版写死的操作相同；放大、缩小、还原是新增的。
复制选中的文字用系统标准的复制键，不在这张表里（见 REMOVED_FIELDS）。"""

ACTIONS_BY_ID: Final[Mapping[str, ActionSpec]] = {spec.id: spec for spec in ACTIONS}

MAX_BINDINGS: Final = 3
"""每个操作最多绑定几个键位。"""

DRAG_SLOP: Final = 4.0
"""按下后移动超过这么多逻辑像素算拖动，否则算单击。"""

# ---------------------------------------------------------------- 键位文字

type Button = Literal["Left", "Right", "Middle"]
type Gesture = Literal["Click", "DoubleClick", "Drag"]

_BUTTONS: Final[Mapping[str, Qt.MouseButton]] = {
    "Left": Qt.MouseButton.LeftButton,
    "Right": Qt.MouseButton.RightButton,
    "Middle": Qt.MouseButton.MiddleButton,
}
_BUTTON_NAMES: Final[tuple[Button, ...]] = ("Left", "Right", "Middle")
_GESTURES: Final[tuple[Gesture, ...]] = ("DoubleClick", "Click", "Drag")  # DoubleClick 在 Click 之前匹配
_MOUSE_MODIFIERS: Final[tuple[tuple[str, Qt.KeyboardModifier], ...]] = (
    ("Ctrl", Qt.KeyboardModifier.ControlModifier),
    ("Alt", Qt.KeyboardModifier.AltModifier),
    ("Shift", Qt.KeyboardModifier.ShiftModifier),
)
_MODIFIER_MASK: Final = (
    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ShiftModifier
)
_MODIFIER_KEYS: Final = frozenset(
    {Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Shift, Qt.Key.Key_Meta, Qt.Key.Key_AltGr}
)


@dataclass(frozen=True, slots=True)
class Binding:
    """解析后的键位。text 是规范写法；键盘键位的 key 是 QKeySequence 的组合值，
    鼠标键位的 button 不为 None。"""

    text: str
    kind: Literal["key", "mouse", "wheel"]
    modifiers: int = 0
    key: int = 0
    button: Button | None = None
    gesture: Gesture | None = None

    def accepts(self, kind: ActionKind) -> bool:
        """这个键位能不能给 kind 类的操作用。"""
        match kind:
            case ActionKind.TRIGGER:
                return self.kind == "key" or self.gesture in ("Click", "DoubleClick")
            case ActionKind.DRAG:
                return self.gesture == "Drag"
            case ActionKind.WHEEL:
                return self.kind == "wheel"


def _modifier_text(modifiers: int) -> str:
    return "".join(f"{name}+" for name, flag in _MOUSE_MODIFIERS if modifiers & flag.value)


def _split_mouse_modifiers(text: str) -> tuple[int, str]:
    parts = [part.strip() for part in text.split("+")]
    names = {name.lower(): flag for name, flag in _MOUSE_MODIFIERS}
    modifiers = 0
    for part in parts[:-1]:
        flag = names.get(part.lower())
        if flag is None:
            raise ValueError(tr("bindings.invalid.modifier", part=part))
        modifiers |= flag.value
    return modifiers, parts[-1]


def _mouse_like(last: str) -> bool:
    lowered = last.lower()
    return lowered == "wheel" or any(
        lowered == f"{button}{gesture}".lower() for button in _BUTTONS for gesture in _GESTURES
    )


def _parse_mouse(text: str) -> Binding | None:
    """鼠标或滚轮键位；最后一段不是鼠标写法时返回 None，修饰键无效时抛 ValueError。"""
    if not _mouse_like(text.rsplit("+", 1)[-1].strip()):
        return None
    modifiers, last = _split_mouse_modifiers(text)
    if last.lower() == "wheel":
        return Binding(f"{_modifier_text(modifiers)}Wheel", "wheel", modifiers)
    for button in _BUTTON_NAMES:
        if not last.lower().startswith(button.lower()):
            continue
        rest = last[len(button) :].lower()
        gesture = next((g for g in _GESTURES if g.lower() == rest), None)
        if gesture is None:
            return None
        text = f"{_modifier_text(modifiers)}{button}{gesture}"
        return Binding(text, "mouse", modifiers, 0, button, cast(Gesture, gesture))
    return None


def parse_binding(text: str) -> Binding:
    """解析一个键位，返回规范写法；写法无效时抛 ValueError（信息已本地化）。"""
    stripped = text.strip()
    if not stripped:
        raise ValueError(tr("bindings.invalid.empty"))
    mouse = _parse_mouse(stripped)
    if mouse is not None:
        return mouse
    sequence = QKeySequence.fromString(stripped, QKeySequence.SequenceFormat.PortableText)
    if sequence.count() != 1:
        raise ValueError(tr("bindings.invalid.format", text=stripped))
    combination = cast(QKeyCombination, sequence[0])  # pyright: ignore[reportIndexIssue]
    key = combination.key()
    if key == Qt.Key.Key_unknown or key in _MODIFIER_KEYS:
        raise ValueError(tr("bindings.invalid.format", text=stripped))
    canonical = QKeySequence(combination).toString(QKeySequence.SequenceFormat.PortableText)
    return Binding(canonical, "key", combination.keyboardModifiers().value, combination.toCombined())


def key_binding_text(event: QKeyEvent) -> str | None:
    """按键事件对应的键盘键位写法；只按了修饰键时返回 None。设置页录制键位时用。"""
    key = event.key()
    if key in _MODIFIER_KEYS or key == Qt.Key.Key_unknown:
        return None
    modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
    return QKeySequence(modifiers.value | key).toString(QKeySequence.SequenceFormat.PortableText)


def mouse_binding_text(
    button: Qt.MouseButton, modifiers: Qt.KeyboardModifier, gesture: Gesture
) -> str | None:
    """鼠标手势对应的键位写法；不支持的按键返回 None。设置页录制键位时用。"""
    name = next((n for n, b in _BUTTONS.items() if b == button), None)
    if name is None:
        return None
    return f"{_modifier_text((modifiers & _MODIFIER_MASK).value)}{name}{gesture}"


def wheel_binding_text(modifiers: Qt.KeyboardModifier) -> str:
    return f"{_modifier_text((modifiers & _MODIFIER_MASK).value)}Wheel"


# ---------------------------------------------------------------- 配置节


def _validate_action(spec: ActionSpec, values: Sequence[str]) -> list[str]:
    if len(values) > MAX_BINDINGS:
        raise ValueError(tr("bindings.invalid.too_many", count=MAX_BINDINGS))
    result: list[str] = []
    for value in values:
        binding = parse_binding(value)
        if not binding.accepts(spec.kind):
            raise ValueError(tr(f"bindings.invalid.kind.{spec.kind.value}", text=binding.text))
        if binding.text not in result:
            result.append(binding.text)
    return result


REMOVED_FIELDS: Final = frozenset({"overlay_copy", "bilingual_copy"})
"""[bindings] 节里已经去掉的字段。覆盖窗口与双语窗口原来各有一个可改键位的“复制”操作；现在覆盖窗口
与双语窗口里显示的文字都可以选中，复制选中的文字用系统标准的复制键（Ctrl+C、Ctrl+Insert），双语窗口
复制全部译文用“复制译文”按钮，两个操作都不再进键位表。旧版本写下的配置文件里可能还有这两个字段，
读取时丢掉它们；否则 extra="forbid" 会让整个 [bindings] 节校验失败，用户改过的其他键位全部退回默认值。"""


def _defaults(action: str) -> list[str]:
    return list(ACTIONS_BY_ID[action].defaults)


class BindingsConfig(BaseModel):
    """配置文件的 [bindings] 节：每个操作一个字段，取值是规范写法的键位列表，空列表表示不绑定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_select: list[str] = Field(default_factory=lambda: _defaults("capture.select"))
    capture_cancel: list[str] = Field(default_factory=lambda: _defaults("capture.cancel"))
    overlay_toggle_source: list[str] = Field(default_factory=lambda: _defaults("overlay.toggle_source"))
    overlay_move: list[str] = Field(default_factory=lambda: _defaults("overlay.move"))
    overlay_zoom: list[str] = Field(default_factory=lambda: _defaults("overlay.zoom"))
    overlay_zoom_in: list[str] = Field(default_factory=lambda: _defaults("overlay.zoom_in"))
    overlay_zoom_out: list[str] = Field(default_factory=lambda: _defaults("overlay.zoom_out"))
    overlay_zoom_reset: list[str] = Field(default_factory=lambda: _defaults("overlay.zoom_reset"))
    overlay_pin: list[str] = Field(default_factory=lambda: _defaults("overlay.pin"))
    overlay_select: list[str] = Field(default_factory=lambda: _defaults("overlay.select"))
    overlay_close: list[str] = Field(default_factory=lambda: _defaults("overlay.close"))
    bilingual_translate: list[str] = Field(default_factory=lambda: _defaults("bilingual.translate"))
    bilingual_swap: list[str] = Field(default_factory=lambda: _defaults("bilingual.swap"))
    bilingual_pin: list[str] = Field(default_factory=lambda: _defaults("bilingual.pin"))
    bilingual_clear: list[str] = Field(default_factory=lambda: _defaults("bilingual.clear"))
    bilingual_close: list[str] = Field(default_factory=lambda: _defaults("bilingual.close"))

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        raw = cast(dict[str, object], data).copy()
        for field in REMOVED_FIELDS:
            raw.pop(field, None)
        for spec in ACTIONS:
            value = raw.get(spec.field)
            if isinstance(value, str):
                value = [value]
            if isinstance(value, list):
                raw[spec.field] = _validate_action(spec, [str(v) for v in cast(list[object], value)])
        return raw

    @model_validator(mode="after")
    def _distinct(self) -> "BindingsConfig":
        seen: dict[tuple[Scope, str], str] = {}
        for spec in ACTIONS:
            for text in self.bindings_of(spec):
                other = seen.setdefault((spec.scope, text), spec.id)
                if other != spec.id:
                    raise ValueError(
                        tr(
                            "bindings.invalid.duplicate",
                            binding=text,
                            first=tr(f"bindings.action.{other}"),
                            second=tr(f"bindings.action.{spec.id}"),
                        )
                    )
        return self

    def bindings_of(self, spec: ActionSpec) -> tuple[str, ...]:
        value = cast(list[str], getattr(self, spec.field))
        return tuple(value)


CONFIG_SECTIONS: dict[str, type[BaseModel]] = {"bindings": BindingsConfig}


def bindings_from(section: object) -> BindingsConfig:
    """从 AppConfig 的 [bindings] 节取键位；节缺失或无法解析时用默认值。"""
    try:
        return BindingsConfig.model_validate(section if isinstance(section, dict) else {})
    except ValueError:
        return BindingsConfig()


# ---------------------------------------------------------------- 匹配


class Keymap:
    """一个窗口的键位表：按事件查操作 id。"""

    def __init__(self, bindings: Iterable[tuple[str, Binding]]) -> None:
        self._keys: dict[int, str] = {}
        self._clicks: dict[tuple[Button, Gesture, int], str] = {}
        self._wheels: dict[int, str] = {}
        for action, binding in bindings:
            if binding.kind == "key":
                self._keys.setdefault(binding.key, action)
            elif binding.kind == "wheel":
                self._wheels.setdefault(binding.modifiers, action)
            elif binding.button is not None and binding.gesture is not None:
                self._clicks.setdefault((binding.button, binding.gesture, binding.modifiers), action)

    @classmethod
    def for_scope(cls, config: BindingsConfig, scope: Scope) -> "Keymap":
        pairs: list[tuple[str, Binding]] = []
        for spec in ACTIONS:
            if spec.scope is scope:
                pairs.extend((spec.id, parse_binding(text)) for text in config.bindings_of(spec))
        return cls(pairs)

    @classmethod
    def defaults(cls, scope: Scope) -> "Keymap":
        return cls.for_scope(BindingsConfig(), scope)

    def key_action(self, event: QKeyEvent) -> str | None:
        key = event.key()
        if key in _MODIFIER_KEYS:
            return None
        modifiers = (event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier).value
        action = self._keys.get(modifiers | key)
        if action is None and key == Qt.Key.Key_Enter:  # 小键盘回车与主键盘回车等同
            action = self._keys.get(modifiers | Qt.Key.Key_Return)
        return action

    def mouse_action(
        self, button: Qt.MouseButton, modifiers: Qt.KeyboardModifier, gesture: Gesture
    ) -> str | None:
        name = next((n for n, b in _BUTTONS.items() if b == button), None)
        if name is None:
            return None
        return self._clicks.get((name, gesture, (modifiers & _MODIFIER_MASK).value))  # pyright: ignore[reportArgumentType]

    def wheel_action(self, modifiers: Qt.KeyboardModifier) -> str | None:
        return self._wheels.get((modifiers & _MODIFIER_MASK).value)


@dataclass(slots=True)
class _Press:
    button: Qt.MouseButton
    modifiers: Qt.KeyboardModifier
    origin: QPointF
    drag: str | None
    fired: bool = False
    dragging: bool = False


class MouseGestures:
    """把按下、移动、松开归成单击或拖动，按键位表给出操作 id。

    - press：记下按键、修饰键与位置。这个按键加修饰键没有绑定拖动、绑定了单击时，单击在按下时就生效，
      返回 ("click", 操作)；绑定了拖动时要等松开才知道是单击还是拖动，返回 None；
    - move：移动超过 DRAG_SLOP 且绑定了拖动时，返回 ("start", 操作)，之后每次移动返回 ("move", 操作)；
    - release：拖动中松开返回 ("end", 操作)；没有拖动时按单击查操作，返回 ("click", 操作)；
      没有单击操作、但按键绑定了拖动时返回 ("tap", 拖动操作)，窗口据此执行拖动操作自带的单击行为
      （例如覆盖窗口的选择文字在单击时清除选择、点开图表）；
    - 双击由窗口的 mouseDoubleClickEvent 直接查 ``keymap.mouse_action(..., "DoubleClick")``。

    没有匹配的操作时返回 None。同一时间只跟踪一个按键：另一个按键在跟踪期间按下时，
    只有按下就生效的单击会执行，不改变正在跟踪的按键，例如框选途中按右键取消截屏。
    """

    def __init__(self, keymap: Keymap) -> None:
        self.keymap = keymap
        self._press: _Press | None = None

    @property
    def dragging(self) -> bool:
        return self._press is not None and self._press.dragging

    @property
    def origin(self) -> QPointF | None:
        return None if self._press is None else self._press.origin

    @property
    def drag_action(self) -> str | None:
        """正在跟踪的按键绑定的拖动操作。"""
        return None if self._press is None else self._press.drag

    def press(
        self, button: Qt.MouseButton, modifiers: Qt.KeyboardModifier, position: QPointF
    ) -> tuple[Literal["click"], str] | None:
        drag = self.keymap.mouse_action(button, modifiers, "Drag")
        click = self.keymap.mouse_action(button, modifiers, "Click") if drag is None else None
        if self._press is None:
            self._press = _Press(button, modifiers, QPointF(position), drag, fired=click is not None)
        return None if click is None else ("click", click)

    def move(self, position: QPointF) -> tuple[Literal["start", "move"], str] | None:
        press = self._press
        if press is None or press.drag is None:
            return None
        if press.dragging:
            return ("move", press.drag)
        if math.hypot(position.x() - press.origin.x(), position.y() - press.origin.y()) > DRAG_SLOP:
            press.dragging = True
            return ("start", press.drag)
        return None

    def release(self, button: Qt.MouseButton) -> tuple[Literal["end", "click", "tap"], str] | None:
        press = self._press
        if press is None or press.button != button:
            return None
        self._press = None
        if press.fired:
            return None
        if press.dragging and press.drag is not None:
            return ("end", press.drag)
        action = self.keymap.mouse_action(press.button, press.modifiers, "Click")
        if action is not None:
            return ("click", action)
        return None if press.drag is None else ("tap", press.drag)

    def reset(self) -> None:
        self._press = None


def binding_label(text: str) -> str:
    """键位在界面上的显示文字：键盘键位按系统的写法（Windows 上与规范写法相同），
    鼠标与滚轮键位按界面语言写，例如“Ctrl+右键单击”“滚轮”。"""
    binding = parse_binding(text)
    if binding.kind == "key":
        return QKeySequence(binding.key).toString(QKeySequence.SequenceFormat.NativeText)
    name = "Wheel" if binding.kind == "wheel" else f"{binding.button}{binding.gesture}"
    return _modifier_text(binding.modifiers) + tr(f"bindings.mouse.{name}")


def action_label(config: BindingsConfig, action: str) -> str:
    """操作当前键位的显示文字，多个键位用“ / ”隔开；没有绑定时是空字符串。"""
    return " / ".join(binding_label(text) for text in config.bindings_of(ACTIONS_BY_ID[action]))


register_catalog(
    {
        "bindings.invalid.empty": {"zh-Hans": "键位不能为空", "en": "A binding cannot be empty"},
        "bindings.invalid.format": {"zh-Hans": "键位写法无效：{text}", "en": "Invalid binding: {text}"},
        "bindings.invalid.modifier": {
            "zh-Hans": "鼠标键位只能用 Ctrl、Alt、Shift 作修饰键，不认识：{part}",
            "en": "Mouse bindings only take Ctrl, Alt and Shift as modifiers, not: {part}",
        },
        "bindings.invalid.too_many": {
            "zh-Hans": "每个操作最多绑定 {count} 个键位",
            "en": "Each action takes at most {count} bindings",
        },
        "bindings.invalid.kind.trigger": {
            "zh-Hans": "这个操作只能用按键、单击或双击，不能用 {text}",
            "en": "This action takes a key, click or double-click, not {text}",
        },
        "bindings.invalid.kind.drag": {
            "zh-Hans": "这个操作只能用鼠标拖动，不能用 {text}",
            "en": "This action takes a mouse drag, not {text}",
        },
        "bindings.invalid.kind.wheel": {
            "zh-Hans": "这个操作只能用滚轮，不能用 {text}",
            "en": "This action takes the mouse wheel, not {text}",
        },
        "bindings.invalid.duplicate": {
            "zh-Hans": "“{first}”和“{second}”都用了 {binding}",
            "en": "“{first}” and “{second}” both use {binding}",
        },
        "bindings.mouse.LeftClick": {"zh-Hans": "左键单击", "en": "Left-click"},
        "bindings.mouse.LeftDoubleClick": {"zh-Hans": "左键双击", "en": "Left double-click"},
        "bindings.mouse.LeftDrag": {"zh-Hans": "左键拖动", "en": "Left-drag"},
        "bindings.mouse.RightClick": {"zh-Hans": "右键单击", "en": "Right-click"},
        "bindings.mouse.RightDoubleClick": {"zh-Hans": "右键双击", "en": "Right double-click"},
        "bindings.mouse.RightDrag": {"zh-Hans": "右键拖动", "en": "Right-drag"},
        "bindings.mouse.MiddleClick": {"zh-Hans": "中键单击", "en": "Middle-click"},
        "bindings.mouse.MiddleDoubleClick": {"zh-Hans": "中键双击", "en": "Middle double-click"},
        "bindings.mouse.MiddleDrag": {"zh-Hans": "中键拖动", "en": "Middle-drag"},
        "bindings.mouse.Wheel": {"zh-Hans": "滚轮", "en": "Wheel"},
        "bindings.action.capture.select": {"zh-Hans": "框选区域", "en": "Select an area"},
        "bindings.action.capture.cancel": {"zh-Hans": "取消截屏", "en": "Cancel capture"},
        "bindings.action.overlay.toggle_source": {
            "zh-Hans": "切换原文与译文",
            "en": "Switch between source and translation",
        },
        "bindings.action.overlay.move": {"zh-Hans": "拖动窗口", "en": "Move the window"},
        "bindings.action.overlay.zoom": {"zh-Hans": "缩放整张图", "en": "Zoom the whole image"},
        "bindings.action.overlay.zoom_in": {"zh-Hans": "放大", "en": "Zoom in"},
        "bindings.action.overlay.zoom_out": {"zh-Hans": "缩小", "en": "Zoom out"},
        "bindings.action.overlay.zoom_reset": {"zh-Hans": "还原大小", "en": "Reset zoom"},
        "bindings.action.overlay.pin": {
            "zh-Hans": "固定在最上层（失去焦点也不关闭）",
            "en": "Pin on top (stay open when focus is lost)",
        },
        "bindings.action.overlay.select": {"zh-Hans": "选择文字", "en": "Select text"},
        "bindings.action.overlay.close": {"zh-Hans": "关闭", "en": "Close"},
        "bindings.action.bilingual.translate": {"zh-Hans": "立即翻译", "en": "Translate now"},
        "bindings.action.bilingual.swap": {"zh-Hans": "交换语言", "en": "Swap languages"},
        "bindings.action.bilingual.pin": {"zh-Hans": "置顶", "en": "Keep on top"},
        "bindings.action.bilingual.clear": {"zh-Hans": "清空", "en": "Clear"},
        "bindings.action.bilingual.close": {"zh-Hans": "关闭窗口", "en": "Close the window"},
    }
)
