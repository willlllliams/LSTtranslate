"""由 pydantic 字段生成设置卡片，并把卡片接到展示器上（需求 UI-06，架构文档 11.7 节）。

页面只声明“哪一节的哪个字段，用什么控件”（``FieldDef``），控件的种类、数值范围、可选值和默认值
都从模型读出（``schema.field_spec``）。生成的 ``Binding`` 把卡片与展示器连起来：

- 用户修改：``edited`` 让该设置项的错误提示消失，``committed`` 提交修改；
- 配置变化：``refresh`` 把配置里的值显示到控件上；
- ConfigRejected：``refresh_error`` 把展示器记下的原因显示在卡片里。

OCR 引擎配置节的字段按字段约定生成卡片（需求 OCRE-09）：``FieldDef.schema_texts`` 让没有登记文案的字段
用模型里的 title、description；``details`` 在说明后面写出取值范围、默认值，以及“修改后自动重新加载识别组件”；
``resettable`` 在控件右边加一个“恢复默认值”按钮，取值已经是默认值时按钮不可用。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, cast

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QBoxLayout, QPushButton, QWidget

from lst.contracts.worker_protocol import JsonValue
from lst.ui.common.text import LANGUAGES, language_name
from lst.ui.i18n import current_language, tr
from lst.ui.settings.controls import (
    DEFAULT_DEBOUNCE_MS,
    ChoiceEditor,
    DirectoryEditor,
    FloatEditor,
    HotkeyEditor,
    IntEditor,
    PairEditor,
    SettingCard,
    SwitchEditor,
    TextAreaEditor,
    TextEditor,
    ValueEditor,
)
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.presenter import Location, SettingsPresenter
from lst.ui.settings.schema import FieldSpec, Kind
from lst.ui.settings.strings_ocr import text_or
from lst.ui.theme import Glyph, fluent_icon

FIELD_GLYPHS: Final[dict[str, str]] = {
    "ui.language": Glyph.GLOBE,
    "ui.theme": Glyph.PALETTE,
    "hotkeys.capture": Glyph.CAMERA,
    "hotkeys.bilingual": Glyph.CHAT,
    "ocr.enabled": Glyph.FONT,
    "ocr.mode": Glyph.LAYOUT,
    "ocr.device": Glyph.CHIP,
    "ocr.language": Glyph.TRANSLATE_AA,
    "ocr.table": Glyph.TABLE,
    "ocr.formula": Glyph.CALCULATOR,
    "ocr.chart": Glyph.CHART,
    "ocr.doc_orientation": Glyph.REFRESH,
    "ocr.doc_unwarping": Glyph.UNWARP,
    "ocr.textline_orientation": Glyph.ITALIC,
    "ocr.grayscale": Glyph.PICTURE,
    "ocr.resize_max_side": Glyph.SLIDERS,
    "screenshot.target_lang": Glyph.TRANSLATE_AA,
    "bilingual.pair": Glyph.TRANSLATE_AA,
    "bilingual.debounce_ms": Glyph.STOPWATCH,
    "bilingual.layout": Glyph.LAYOUT,
    "nmt.model_dir": Glyph.FOLDER,
    "updates.auto_check": Glyph.SYNC,
    "updates.check_interval_hours": Glyph.STOPWATCH,
    "updates.include_prerelease": Glyph.TAG,
    # 引擎实例的参数，键为 engine.<字段名>
    "engine.name": Glyph.PENCIL,
    "engine.enabled": Glyph.POWER,
    "engine.api_key": Glyph.KEY,
    "engine.color": Glyph.PALETTE,
    "engine.timeout_s": Glyph.STOPWATCH,
    "engine.max_concurrency": Glyph.LIST,
    "engine.max_retries": Glyph.REFRESH,
    "engine.provider": Glyph.GLOBE,
    "engine.base_url": Glyph.LINK,
    "engine.model": Glyph.PUZZLE,
    "engine.temperature": Glyph.SLIDERS,
    "engine.system_prompt": Glyph.CHAT,
    "engine.max_batch_segments": Glyph.LIST,
    "engine.max_batch_chars": Glyph.LINES,
    "engine.first_token_timeout_s": Glyph.STOPWATCH,
    "engine.structured_output": Glyph.LAYOUT,
    "engine.languages": Glyph.TRANSLATE_AA,
    "engine.formality": Glyph.TAG,
    "engine.appid": Glyph.TAG,
    "engine.tier": Glyph.LIBRARY,
    "engine.processing_timeout_s": Glyph.STOPWATCH,
    "engine.send_image": Glyph.PICTURE,
    "engine.image_max_side": Glyph.SLIDERS,
    "engine.pivot_via_english": Glyph.TRANSLATE_AA,
}
"""设置卡片左侧的图标。键是设置项的位置（“节.字段”）或 engine.<引擎参数名>；没有登记的设置项不显示图标。"""

INT_MIN: Final = -(2**31)
INT_MAX: Final = 2**31 - 1
MULTILINE_FIELDS: Final = frozenset({"system_prompt"})
"""用多行文本框编辑的字段。"""
INT_SUFFIXES: Final = {"image_max_side": "settings.suffix.pixels"}
"""页面没有声明单位后缀时，这些整数字段按字段名取单位后缀（引擎参数表单没有逐项声明）。"""


@dataclass(frozen=True, slots=True)
class FieldDef:
    """页面对一个设置项的声明。

    widget：``auto`` 按字段类型选控件；``language`` 语言下拉框；``language_auto`` 在语言之前多一个“自动”；
    ``pair`` 一对语言；``hotkey`` 热键录入；``dir`` 目录选择。
    suffix、zero 是文案键：数值框的单位后缀，以及 0 显示成什么文字。

    以下几项供按字段约定生成的卡片使用（OCR 引擎配置节）：

    - text_key：文案键的前缀，空串时用 settings.f.<节>.<字段路径>；
    - schema_texts：没有登记文案时，标题与说明用模型里的 title、description；
    - details：说明后面写出取值范围与默认值，需要重新加载的字段注明会自动重新加载；
    - resettable：控件右边加“恢复默认值”按钮；
    - icon：卡片左侧的图标，None 时按 FIELD_GLYPHS 取。
    """

    section: str
    path: tuple[str, ...]
    widget: str = "auto"
    suffix: str = ""
    zero: str = ""
    text_key: str = ""
    schema_texts: bool = False
    details: bool = False
    resettable: bool = False
    icon: str | None = None

    @property
    def key(self) -> str:
        """文案键的前缀：text_key，或 settings.f.<节>.<字段路径>。"""
        return self.text_key or ".".join(("settings", "f", self.section, *self.path))


class Binding:
    """一张卡片与它背后的设置项。"""

    def __init__(
        self,
        location: Location,
        card: SettingCard,
        editor: ValueEditor,
        *,
        title_key: str,
        relabel: Callable[[], None],
        load: Callable[[SettingsPresenter], JsonValue],
        fallback: tuple[str, str] | None = None,
        details: Callable[[], str] | None = None,
        default: JsonValue = None,
        reset_button: QPushButton | None = None,
    ) -> None:
        self.location = location
        self.card = card
        self.editor = editor
        self._title_key = title_key
        self._relabel = relabel
        self._load = load
        self._fallback = fallback
        self._details = details
        self.default = default
        """设置项的默认值（规范 JSON），“恢复默认值”写回的就是它。"""
        self.reset_button = reset_button
        """“恢复默认值”按钮；页面没有要求时为 None。"""
        self._note_key = ""
        self._note_replaces = False

    def retranslate(self) -> None:
        self._relabel()
        self._apply_texts()

    def _apply_texts(self) -> None:
        fallback_title = self.location[-1] if isinstance(self.location[-1], str) else ""
        fallback_desc = ""
        if self._fallback is not None:
            fallback_title = self._fallback[0] or fallback_title
            fallback_desc = self._fallback[1]
        chinese = current_language() == "zh-Hans"
        # 按字段约定生成的卡片：中文界面直接用模型里的中文标题与说明，文案表只补英文。
        schema_first = self._fallback is not None and chinese
        description = fallback_desc if schema_first else text_or(f"{self._title_key}.desc", fallback_desc)
        # 中文句子之间不加空格，英文加一个空格。
        separator = "" if chinese else " "
        details = self._details() if self._details is not None else ""
        if details:
            description = f"{description}{separator}{details}" if description else details
        if self._note_key:
            note = tr(self._note_key)
            replace = self._note_replaces or not description
            description = note if replace else f"{description}{separator}{note}"
        title = (
            fallback_title
            if schema_first and self._fallback is not None and self._fallback[0]
            else text_or(f"{self._title_key}.title", fallback_title)
        )
        self.card.set_texts(title, description)
        if self.reset_button is not None:
            self.reset_button.setToolTip(tr("settings.ocr_params.reset_one"))
            self.reset_button.setAccessibleName(tr("settings.ocr_params.reset_one.name", name=title))

    @property
    def note_key(self) -> str:
        return self._note_key

    def set_note(self, key: str, *, replace: bool = False) -> None:
        """在说明后面追加一句（文案键），例如“仅 PaddleOCR 可用”；replace 为真时用它代替原来的说明。

        key 为空串表示去掉，恢复原来的说明。不重建控件的可选项。
        """
        if (key, replace) != (self._note_key, self._note_replaces):
            self._note_key, self._note_replaces = key, replace
            self._apply_texts()

    def refresh(self, presenter: SettingsPresenter) -> None:
        value = self._load(presenter)
        self.editor.set_json(value)
        if self.reset_button is not None:
            self.reset_button.setEnabled(value != self.default)

    def refresh_error(self, presenter: SettingsPresenter) -> None:
        self.card.set_error(presenter.rejection_for(self.location))

    def matches(self, location: Location) -> bool:
        """一条 ConfigRejected 的位置是否落在这张卡片之内。"""
        return location[: len(self.location)] == self.location


def language_options(*, with_auto: bool) -> list[tuple[str, str]]:
    options: list[tuple[str, str]] = [(str(code), language_name(code)) for code in LANGUAGES]
    if with_auto:
        options.insert(0, ("auto", tr("settings.choice.ocr.language.auto")))
    return options


def choice_options(prefix: str, choices: tuple[str, ...]) -> list[tuple[str, str]]:
    """可选值与显示名；没有登记显示名的取值直接显示取值本身。"""
    return [(value, text_or(f"{prefix}.{value}", value)) for value in choices]


def _number_text(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def value_text(spec: FieldSpec, value: JsonValue, choice_prefix: str) -> str:
    """一个取值的显示文字：开关为“开”“关”，可选值用它的显示名，数字去掉多余的小数位。"""
    if isinstance(value, bool):
        return tr("settings.switch.on" if value else "settings.switch.off")
    if isinstance(value, int | float):
        return _number_text(float(value))
    if spec.kind is Kind.CHOICE and isinstance(value, str):
        return text_or(f"{choice_prefix}.{value}", value)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def field_details(spec: FieldSpec, choice_prefix: str) -> str:
    """说明后面的补充：取值范围与默认值；需要重新加载的字段再注明“修改后自动重新加载识别组件”。"""
    parts: list[str] = []
    default = spec.default
    if default is None or default == "" or default == []:
        if not spec.required:
            parts.append(tr("settings.ocr_params.default_none"))
    else:
        shown = value_text(spec, default, choice_prefix)
        numeric = spec.kind in (Kind.INT, Kind.FLOAT, Kind.OPTIONAL_FLOAT)
        low = _number_text(spec.minimum) if numeric and spec.minimum is not None else None
        high = _number_text(spec.maximum) if numeric and spec.maximum is not None else None
        if low is not None and high is not None:
            parts.append(tr("settings.ocr_params.range", low=low, high=high, default=shown))
        elif low is not None:
            parts.append(tr("settings.ocr_params.min", low=low, default=shown))
        elif high is not None:
            parts.append(tr("settings.ocr_params.max", high=high, default=shown))
        else:
            parts.append(tr("settings.ocr_params.default", default=shown))
    if spec.reload:
        parts.append(tr("settings.ocr_params.reload"))
    separator = "" if current_language() == "zh-Hans" else " "
    return separator.join(parts)


def make_editor(
    spec: FieldSpec,
    *,
    widget: str = "auto",
    choice_prefix: str = "",
    suffix: str = "",
    zero: str = "",
    dialogs: Dialogs,
    debounce_ms: int = DEFAULT_DEBOUNCE_MS,
) -> tuple[ValueEditor, Callable[[], None]]:
    """按字段描述创建编辑器，返回 (编辑器, 界面语言切换时重设文案的函数)。"""

    def nothing() -> None:
        return None

    if widget == "hotkey":
        hotkey = HotkeyEditor()
        return hotkey, hotkey.retranslate
    if widget == "pair":
        pair = PairEditor()
        return pair, pair.retranslate
    if widget == "dir":
        directory = DirectoryEditor(dialogs, debounce_ms=debounce_ms)
        return directory, directory.retranslate
    if widget in ("language", "language_auto"):
        with_auto = widget == "language_auto"
        language = ChoiceEditor(language_options(with_auto=with_auto))
        return language, lambda: language.set_options(language_options(with_auto=with_auto))
    match spec.kind:
        case Kind.BOOL:
            switch = SwitchEditor()
            return switch, switch.retranslate
        case Kind.CHOICE:
            choice = ChoiceEditor(choice_options(choice_prefix, spec.choices))
            return choice, lambda: choice.set_options(choice_options(choice_prefix, spec.choices))
        case Kind.INT:
            minimum = int(spec.minimum) if spec.minimum is not None else INT_MIN
            maximum = int(spec.maximum) if spec.maximum is not None else INT_MAX
            integer = IntEditor(minimum, maximum, debounce_ms=debounce_ms)
            if suffix == "" and spec.name in INT_SUFFIXES:
                suffix = INT_SUFFIXES[spec.name]

            def retext() -> None:
                integer.set_texts(tr(suffix) if suffix else "", tr(zero) if zero else "")

            retext()
            return integer, retext
        case Kind.FLOAT:
            low = spec.minimum if spec.minimum is not None else 0.0
            if spec.exclusive_minimum:
                low += 0.01
            high = spec.maximum if spec.maximum is not None else 1_000_000.0
            step = 0.5 if high > 10 else 0.05
            return FloatEditor(low, high, step=step, debounce_ms=debounce_ms), nothing
        case Kind.OPTIONAL_FLOAT:
            return TextEditor(
                number=True, placeholder=tr("settings.default_value"), debounce_ms=debounce_ms
            ), nothing
        case Kind.OPTIONAL_TEXT if spec.name in MULTILINE_FIELDS:
            return TextAreaEditor(debounce_ms=debounce_ms), nothing
        case Kind.OPTIONAL_TEXT:
            return TextEditor(optional=True, debounce_ms=debounce_ms), nothing
        case Kind.TEXT_LIST:
            return TextEditor(as_list=True, debounce_ms=debounce_ms), nothing
        case _:
            return TextEditor(debounce_ms=debounce_ms), nothing


class CardBuilder:
    """为展示器生成接好线的设置卡片。debounce_ms 是文本框停止输入后提交的等待时间。"""

    def __init__(
        self, presenter: SettingsPresenter, dialogs: Dialogs, debounce_ms: int = DEFAULT_DEBOUNCE_MS
    ) -> None:
        self._presenter = presenter
        self._dialogs = dialogs
        self._debounce_ms = debounce_ms

    @property
    def debounce_ms(self) -> int:
        return self._debounce_ms

    def build(self, definition: FieldDef, parent: QWidget | None = None) -> Binding | None:
        """生成一个配置节里的设置项的卡片。组合根没有注入这一节或模型里没有这个字段时返回 None。"""
        presenter = self._presenter
        spec = presenter.spec(definition.section, definition.path)
        if spec is None:
            return None
        editor, relabel = make_editor(
            spec,
            widget=definition.widget,
            choice_prefix=f"settings.choice.{definition.section}.{'.'.join(definition.path)}",
            suffix=definition.suffix,
            zero=definition.zero,
            dialogs=self._dialogs,
            debounce_ms=self._debounce_ms,
        )
        location: Location = (definition.section, *definition.path)
        icon = definition.icon if definition.icon is not None else FIELD_GLYPHS.get(".".join(location), "")
        card = SettingCard(editor, location=location, icon=icon, parent=parent)
        section, path = definition.section, definition.path
        choice_prefix = f"settings.choice.{definition.section}.{'.'.join(definition.path)}"

        def commit(value: object) -> None:
            presenter.set_value(section, path, cast(JsonValue, value))

        editor.committed.connect(commit)
        editor.edited.connect(lambda: presenter.clear_rejection(location))
        if isinstance(editor, HotkeyEditor):
            editor.button.recording_changed.connect(presenter.set_hotkey_recording)
        reset_button = self._reset_button(card) if definition.resettable else None
        default = spec.default
        binding = Binding(
            location,
            card,
            editor,
            title_key=definition.key,
            relabel=relabel,
            load=lambda p: p.value(section, path),
            fallback=(spec.title, spec.description) if definition.schema_texts else None,
            details=(lambda: field_details(spec, choice_prefix)) if definition.details else None,
            default=default,
            reset_button=reset_button,
        )
        if reset_button is not None:

            def reset() -> None:
                editor.reload(default)
                presenter.set_value(section, path, default)

            reset_button.clicked.connect(reset)
        binding.retranslate()
        binding.refresh(presenter)
        return binding

    @staticmethod
    def _reset_button(card: SettingCard) -> QPushButton:
        """卡片第一行最右边、控件之后的“恢复默认值”按钮。

        按钮一直占位，取值已是默认值时只是不可用，控件不会因为按钮出现或消失而左右移动。
        """
        button = QPushButton()
        button.setIcon(fluent_icon(Glyph.REFRESH))
        button.setFlat(True)
        top = card._top_row.layout()
        if isinstance(top, QBoxLayout):
            top.addWidget(button, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return button
