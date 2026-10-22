"""全局热键模块的测试。注册与注销用假后端，不碰真实的系统热键；另有一个测试在原生平台上真实注册一次。"""

import asyncio
import ctypes
import logging
import sys

import pytest
from pydantic import ValidationError

from lst.contracts.config import AppConfig
from lst.contracts.events import HotkeyRegistrationFailed, StartCapture, ToggleBilingualWindow
from lst.kernel import EventBus, ModuleContext
from lst.ui import i18n
from lst.ui.common import HotkeyRecordingChanged
from lst.ui.hotkeys import (
    ERROR_HOTKEY_ALREADY_REGISTERED,
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    Hotkey,
    HotkeysConfig,
    HotkeysModule,
    Win32Hotkeys,
    parse_hotkey,
)


@pytest.mark.parametrize(
    ("text", "modifiers", "vk", "normalized"),
    [
        ("alt+q", MOD_ALT, ord("Q"), "Alt+Q"),
        ("Ctrl + Shift + F1", MOD_CONTROL | MOD_SHIFT, 0x70, "Ctrl+Shift+F1"),
        ("shift+ctrl+alt+1", MOD_CONTROL | MOD_ALT | MOD_SHIFT, ord("1"), "Ctrl+Alt+Shift+1"),
        ("control+space", MOD_CONTROL, 0x20, "Ctrl+Space"),
        ("alt+`", MOD_ALT, 0xC0, "Alt+`"),
    ],
)
def test_parse_hotkey(text: str, modifiers: int, vk: int, normalized: str) -> None:
    assert parse_hotkey(text) == Hotkey(modifiers, vk, normalized)


@pytest.mark.parametrize("text", ["", "q", "alt+", "alt+foo", "hyper+q", "ctrl+f12", "alt++q", "alt+f25"])
def test_parse_hotkey_rejects_invalid(text: str) -> None:
    with pytest.raises(ValueError):
        parse_hotkey(text)


def test_config_normalizes_and_rejects() -> None:

    assert HotkeysConfig.model_validate({"capture": "alt+w"}).capture == "Alt+W"
    with pytest.raises(ValidationError):
        HotkeysConfig.model_validate({"capture": "w"})


class FakeBackend(Win32Hotkeys):
    def __init__(self, fail: dict[str, int] | None = None) -> None:
        self.fail = fail or {}
        self.registered: dict[int, str] = {}
        self.log: list[str] = []

    def register(self, hotkey_id: int, hotkey: Hotkey) -> int:
        self.log.append(f"+{hotkey.text}")
        if hotkey.text in self.fail:
            return self.fail[hotkey.text]
        self.registered[hotkey_id] = hotkey.text
        return 0

    def unregister(self, hotkey_id: int) -> None:
        self.log.append(f"-{self.registered.pop(hotkey_id)}")


async def started(backend: FakeBackend) -> tuple[HotkeysModule, EventBus, list[object]]:
    bus = EventBus(asyncio.get_running_loop(), raise_errors=True)
    received: list[object] = []
    for kind in (StartCapture, ToggleBilingualWindow, HotkeyRegistrationFailed):
        bus.subscribe(kind, received.append)
    module = HotkeysModule(backend)
    await module.start(ModuleContext(bus, logging.getLogger("test.hotkeys")))
    return module, bus, received


def config(**hotkeys: str) -> AppConfig:
    return AppConfig(sections={"hotkeys": dict(hotkeys)}, revision=1)


async def test_registers_defaults_and_dispatches_commands() -> None:
    backend = FakeBackend()
    module, bus, received = await started(backend)
    bus.publish(config())
    await bus.drain()
    assert sorted(backend.registered.values()) == ["Alt+E", "Alt+Q"]
    ids = {text: hotkey_id for hotkey_id, text in backend.registered.items()}
    assert module.dispatch(ids["Alt+Q"])
    assert module.dispatch(ids["Alt+E"])
    assert not module.dispatch(99)
    await bus.drain()
    assert received == [StartCapture(), ToggleBilingualWindow()]
    await module.stop()
    assert backend.registered == {}


async def test_reregisters_only_when_configuration_changes() -> None:
    backend = FakeBackend()
    module, bus, _ = await started(backend)
    bus.publish(config(capture="alt+q"))
    await bus.drain()
    bus.publish(AppConfig(sections={"hotkeys": {"capture": "Alt+Q"}}, revision=2))
    await bus.drain()
    assert backend.log == ["+Alt+Q", "+Alt+E"]
    bus.publish(config(capture="ctrl+alt+t"))
    await bus.drain()
    assert backend.log[2:] == ["-Alt+Q", "-Alt+E", "+Ctrl+Alt+T", "+Alt+E"]
    await module.stop()


async def test_failure_publishes_event_with_reason() -> None:
    backend = FakeBackend(fail={"Alt+Q": ERROR_HOTKEY_ALREADY_REGISTERED, "Alt+E": 5})
    module, bus, received = await started(backend)
    bus.publish(config())
    await bus.drain()
    failures = [m for m in received if isinstance(m, HotkeyRegistrationFailed)]
    assert [f.hotkey for f in failures] == ["Alt+Q", "Alt+E"]
    assert all(f.reason for f in failures)
    assert failures[0].reason != failures[1].reason
    assert not module.dispatch(1)
    await module.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上注册真实热键")
def test_real_register_and_unregister_roundtrip() -> None:
    """用一个几乎不会被占用的组合真实注册再注销，确认 ctypes 调用本身正确。"""
    backend = Win32Hotkeys()
    hotkey = parse_hotkey("ctrl+alt+shift+f23")
    error = backend.register(0x7FFF, hotkey)
    try:
        assert error in (0, ERROR_HOTKEY_ALREADY_REGISTERED)
    finally:
        if error == 0:
            backend.unregister(0x7FFF)
    assert ctypes.sizeof(ctypes.c_void_p) in (4, 8)


# ---------------------------------------------------------------------- 重复的组合、录入时暂停、文案


def test_config_rejects_the_same_hotkey_twice() -> None:
    with pytest.raises(ValidationError) as caught:
        HotkeysConfig.model_validate({"capture": "Alt+Q", "bilingual": "alt+q"})
    assert "Alt+Q" in caught.value.errors()[0]["msg"]


def test_rejection_reasons_follow_the_interface_language() -> None:
    i18n.set_language("en")
    for raw in ({"capture": "q"}, {"capture": "ctrl+f12"}, {"capture": "alt+!"}, {"bilingual": "Alt+Q"}):
        with pytest.raises(ValidationError) as caught:
            HotkeysConfig.model_validate(raw)
        message = caught.value.errors()[0]["msg"]
        assert message.isascii(), message


async def test_conflict_with_its_own_hotkey_is_named_as_such() -> None:
    backend = FakeBackend()
    module, bus, _ = await started(backend)
    bus.publish(config())
    await bus.drain()
    own = module._failure_reason(parse_hotkey("alt+q"), ERROR_HOTKEY_ALREADY_REGISTERED)
    other = module._failure_reason(parse_hotkey("ctrl+alt+z"), ERROR_HOTKEY_ALREADY_REGISTERED)
    assert own == "与本程序的另一个热键（Alt+Q）相同"
    assert other == "已被其他程序占用"
    await module.stop()


async def test_recording_pauses_and_resumes_hotkeys() -> None:
    backend = FakeBackend(fail={"Alt+E": ERROR_HOTKEY_ALREADY_REGISTERED})
    module, bus, received = await started(backend)
    bus.publish(config())
    await bus.drain()
    assert sorted(backend.registered.values()) == ["Alt+Q"]
    bus.publish(HotkeyRecordingChanged(True))
    await bus.drain()
    assert module.paused and backend.registered == {}
    bus.publish(AppConfig(sections={"hotkeys": {"capture": "Ctrl+Alt+T"}}, revision=2))  # 录入的结果
    await bus.drain()
    assert backend.registered == {}  # 暂停期间不注册
    bus.publish(HotkeyRecordingChanged(False))
    await bus.drain()
    assert not module.paused
    assert sorted(backend.registered.values()) == ["Ctrl+Alt+T"]
    failures = [m for m in received if isinstance(m, HotkeyRegistrationFailed)]
    assert [f.hotkey for f in failures] == ["Alt+E"]  # 恢复时同一个失败不再通知
    await module.stop()
