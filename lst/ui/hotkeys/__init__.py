"""全局热键（需求 TR-04、TR-05，架构文档 11.3 节）。

用 RegisterHotKey 注册热键，hWnd 为 NULL 时 WM_HOTKEY 投递到注册线程（Qt 主线程）的消息队列，
由 QAbstractNativeEventFilter 收到。注册时带 MOD_NOREPEAT，按住不放不会连发。
"""

import ctypes
import ctypes.wintypes
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, field_validator
from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray, QCoreApplication

from lst.contracts.config import AppConfig
from lst.contracts.events import HotkeyRegistrationFailed, StartCapture, ToggleBilingualWindow
from lst.contracts.messages import Command
from lst.kernel import ModuleContext
from lst.ui.i18n import register_catalog, tr

logger = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_MODIFIERS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "shift": MOD_SHIFT, "win": MOD_WIN}
_MODIFIER_ORDER = ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win"))
_NAMED_KEYS = {
    "space": (0x20, "Space"),
    "tab": (0x09, "Tab"),
    "enter": (0x0D, "Enter"),
    "esc": (0x1B, "Esc"),
    "escape": (0x1B, "Esc"),
    "printscreen": (0x2C, "PrintScreen"),
    "insert": (0x2D, "Insert"),
    "delete": (0x2E, "Delete"),
    "home": (0x24, "Home"),
    "end": (0x23, "End"),
    "pageup": (0x21, "PageUp"),
    "pagedown": (0x22, "PageDown"),
    "`": (0xC0, "`"),
}


@dataclass(frozen=True, slots=True)
class Hotkey:
    """解析后的热键。text 是规范写法，例如 "Ctrl+Alt+Q"。"""

    modifiers: int
    vk: int
    text: str


def parse_hotkey(text: str) -> Hotkey:
    """把 "alt+q"、"Ctrl + Shift + F1" 这类写法解析成热键；写法无效时抛 ValueError。

    至少要有一个修饰键：没有修饰键的全局热键会吞掉普通按键。F12 由系统保留给调试器，不能注册。
    """
    parts = [part.strip().lower() for part in text.split("+")]
    if not parts or any(not part for part in parts):
        raise ValueError(f"热键写法无效：{text!r}")
    *modifier_parts, key = parts
    modifiers = 0
    for part in modifier_parts:
        if part not in _MODIFIERS:
            raise ValueError(f"未知的修饰键：{part!r}")
        modifiers |= _MODIFIERS[part]
    if modifiers == 0:
        raise ValueError("热键至少要包含 Ctrl、Alt、Shift、Win 中的一个修饰键")
    if len(key) == 1 and ("a" <= key <= "z" or "0" <= key <= "9"):
        vk, key_name = ord(key.upper()), key.upper()
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        if key == "f12":
            raise ValueError("F12 由系统保留，不能注册为全局热键")
        vk, key_name = 0x70 + int(key[1:]) - 1, key.upper()
    elif key in _NAMED_KEYS:
        vk, key_name = _NAMED_KEYS[key]
    else:
        raise ValueError(f"不支持的按键：{key!r}")
    names = [name for flag, name in _MODIFIER_ORDER if modifiers & flag]
    return Hotkey(modifiers=modifiers, vk=vk, text="+".join([*names, key_name]))


class HotkeysConfig(BaseModel):
    """配置文件的 [hotkeys] 节。保存的是规范写法。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture: str = "Alt+Q"
    bilingual: str = "Alt+E"

    @field_validator("capture", "bilingual")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return parse_hotkey(value).text


CONFIG_SECTIONS: dict[str, type[BaseModel]] = {"hotkeys": HotkeysConfig}

register_catalog(
    {
        "hotkeys.error.in_use": {
            "zh-Hans": "已被其他程序占用",
            "en": "already used by another program",
        },
        "hotkeys.error.other": {"zh-Hans": "系统错误 {code}", "en": "system error {code}"},
    }
)


class Win32Hotkeys:
    """RegisterHotKey / UnregisterHotKey 的薄封装，测试时替换成假实现。"""

    def register(self, hotkey_id: int, hotkey: Hotkey) -> int:
        """注册成功返回 0，失败返回 GetLastError 的错误码。"""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if user32.RegisterHotKey(None, hotkey_id, hotkey.modifiers | MOD_NOREPEAT, hotkey.vk):
            return 0
        return ctypes.get_last_error() or -1

    def unregister(self, hotkey_id: int) -> None:
        ctypes.WinDLL("user32", use_last_error=True).UnregisterHotKey(None, hotkey_id)


class _HotkeyFilter(QAbstractNativeEventFilter):
    """在 Qt 主线程的原生消息里找 WM_HOTKEY。"""

    _TYPES = (b"windows_generic_MSG", b"windows_dispatcher_MSG")

    def __init__(self, on_hotkey: Callable[[int], bool]) -> None:
        super().__init__()
        self._on_hotkey = on_hotkey

    def nativeEventFilter(
        self, eventType: QByteArray | bytes | bytearray | memoryview, message: int, /
    ) -> object:
        kind = eventType.data() if isinstance(eventType, QByteArray) else bytes(eventType)
        if kind not in self._TYPES:
            return False, 0
        msg = ctypes.wintypes.MSG.from_address(int(message))
        if msg.message != WM_HOTKEY:
            return False, 0
        return self._on_hotkey(int(msg.wParam)), 0


class HotkeysModule:
    """注册配置里的全局热键，按下时发送对应的命令。"""

    name = "hotkeys"

    def __init__(self, backend: Win32Hotkeys | None = None) -> None:
        self._backend = backend or Win32Hotkeys()
        self._ctx: ModuleContext | None = None
        self._filter: _HotkeyFilter | None = None
        self._registered: dict[int, Command] = {}
        self._current: HotkeysConfig | None = None

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        app = QCoreApplication.instance()
        if app is not None and sys.platform == "win32":
            self._filter = _HotkeyFilter(self.dispatch)
            app.installNativeEventFilter(self._filter)
        ctx.subscribe(AppConfig, self._on_config)

    async def stop(self) -> None:
        self._unregister_all()
        app = QCoreApplication.instance()
        if app is not None and self._filter is not None:
            app.removeNativeEventFilter(self._filter)
        self._filter = None
        self._ctx = None

    def dispatch(self, hotkey_id: int) -> bool:
        """处理一次 WM_HOTKEY，返回这个热键是否属于本模块。"""
        command = self._registered.get(hotkey_id)
        if command is None or self._ctx is None:
            return False
        self._ctx.bus.send(command)
        return True

    def _on_config(self, config: AppConfig) -> None:
        raw = config.section("hotkeys")
        try:
            wanted = HotkeysConfig.model_validate(raw if isinstance(raw, dict) else {})
        except ValueError:
            logger.warning("hotkeys 配置无法解析，使用默认热键")
            wanted = HotkeysConfig()
        if wanted == self._current:
            return
        self._current = wanted
        self._unregister_all()
        for hotkey_id, (text, command) in enumerate(
            ((wanted.capture, StartCapture()), (wanted.bilingual, ToggleBilingualWindow())), start=1
        ):
            self._register(hotkey_id, text, command)

    def _register(self, hotkey_id: int, text: str, command: Command) -> None:
        hotkey = parse_hotkey(text)
        error = self._backend.register(hotkey_id, hotkey)
        if error == 0:
            self._registered[hotkey_id] = command
            logger.info("已注册热键 %s", hotkey.text)
            return
        reason = (
            tr("hotkeys.error.in_use")
            if error == ERROR_HOTKEY_ALREADY_REGISTERED
            else tr("hotkeys.error.other", code=error)
        )
        logger.warning("热键 %s 注册失败（错误码 %s）", hotkey.text, error)
        if self._ctx is not None:
            self._ctx.bus.publish(HotkeyRegistrationFailed(hotkey.text, reason))

    def _unregister_all(self) -> None:
        for hotkey_id in list(self._registered):
            self._backend.unregister(hotkey_id)
        self._registered.clear()
