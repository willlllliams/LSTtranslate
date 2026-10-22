"""自动化接口的各个方法，走真实的组合根（需求 DEV-01 至 DEV-08，架构文档第 16 章）。

lst.app.bootstrap.Application 按产品的方式启动全部模块，NMT 与 OCR 进程用假后端
（与 tests/integration/test_app.py 相同），屏幕换成虚构的两屏布局（左侧副屏 150% 缩放、坐标为负，
冻结画面按坐标编码颜色），蒙版与覆盖窗口带 WA_DontShowOnScreen，覆盖窗口的剪贴板换成记录列表。
数据目录按“设置里选的目录”处理，可以更改。
构建类型是开发版，自动化接口默认开启，管道名每个测试各不相同。客户端是阻塞的，放在线程里调用。

假 OCR 后端把整张截图识别成一个文本块，文字写出截图尺寸与两角像素的颜色；假 NMT 后端的译文是
``[源>目标]原句``。
"""

import asyncio
import contextlib
import os
import sys
import tomllib
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

import lst.app.bootstrap as bootstrap
from lst.app.bootstrap import Application, AppOptions
from lst.app.logging_setup import setup_logging
from lst.app.paths import AppPaths, DataDirChoice
from lst.automation import AutomationClient, AutomationError
from lst.automation.winsec import normalize_pipe_name
from lst.contracts.automation import AutomationStatus, BuildFlavor
from lst.contracts.config import UpdateConfig
from lst.contracts.events import NotificationRequested
from lst.contracts.ocr import CaptureCompleted, RawImage
from lst.contracts.ocr_engines import OcrEngineKind
from lst.contracts.ports import WorkerState
from lst.contracts.storage import DataDirSource
from lst.services.config import MemorySecretStore
from lst.ui.capture import CaptureModule, Rect, to_raw_image
from lst.ui.overlay import OverlayModule
from tests.ui.capture.conftest import FakeScreens, coded_color, hide_from_desktop, pixel_at

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="自动化接口只在 Windows 上实现")

NMT_FAKE = (
    sys.executable,
    "-m",
    "lst.workers.nmt_worker",
    "--backend",
    "tests.workers.fake_backend:FakeBackend",
)
OCR_FAKE = (
    sys.executable,
    "-m",
    "lst.workers.ocr_worker",
    "--backend",
    "tests.workers.ocr.fake_backend:FakeOcrBackend",
)
LAYOUT = [("main", Rect(0, 0, 1920, 1080), 1.0), ("left", Rect(-2560, -200, 2560, 1440), 1.5)]


class Remote:
    """线程里调用的阻塞客户端。"""

    def __init__(self, client: AutomationClient) -> None:
        self.client = client

    async def __call__(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self.client.call, method, params)

    async def error(self, method: str, params: dict[str, Any] | None = None) -> AutomationError:
        with pytest.raises(AutomationError) as caught:
            await self(method, params)
        return caught.value


@dataclass
class Rig:
    app: Application
    pipe: str
    screens: FakeScreens
    copied: list[str]
    captures: list[CaptureCompleted] = field(default_factory=lambda: list[CaptureCompleted]())
    notices: list[NotificationRequested] = field(default_factory=lambda: list[NotificationRequested]())

    async def connect(self, timeout: float = 5.0) -> Remote:
        client = await asyncio.to_thread(AutomationClient.connect, self.pipe, timeout=timeout)
        return Remote(client)


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"测试期间不应有网络请求：{request.url}")


async def _until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.02)
    except TimeoutError:
        raise AssertionError(f"等待超时：{what}") from None


def _make_app(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    flavor: BuildFlavor,
    config: str = "",
) -> Rig:
    screens = FakeScreens(list(LAYOUT))
    copied: list[str] = []
    monkeypatch.setattr(
        bootstrap, "CaptureModule", lambda: CaptureModule(freezer=screens, prepare_window=hide_from_desktop)
    )
    monkeypatch.setattr(
        bootstrap,
        "OverlayModule",
        lambda: OverlayModule(clipboard=copied.append, prepare_window=hide_from_desktop),
    )
    data = tmp_path / "data"
    paths = AppPaths(data)
    paths.ensure()
    paths.config_file.write_text(
        '[updates]\nauto_check = false\n\n[hotkeys]\ncapture = "Ctrl+Alt+Shift+F20"\n'
        'bilingual = "Ctrl+Alt+Shift+F21"\n' + config,
        encoding="utf-8",
    )
    paths.ui_state_file.write_text("[onboarding]\ncompleted=true\n", encoding="utf-8")
    pipe = normalize_pipe_name(f"lst-test-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    app = Application(
        AppOptions(
            paths=paths,
            secrets=MemorySecretStore(),
            nmt_command=NMT_FAKE,
            ocr_command=lambda _engine: OCR_FAKE,
            tray_icon=None,
            updates_startup_delay=3600,
            http_transport=httpx.MockTransport(_no_network),
            models_index_url="https://index.invalid/index.json",
            dict_index_url="https://dicts.invalid/dict-index.json",
            data=DataDirChoice(data, DataDirSource.CHOSEN, tmp_path / "roaming" / "location.json"),
            flavor=flavor,
            automation_pipe=pipe,
        ),
        qapp,
    )
    return Rig(app, pipe, screens, copied)


@contextlib.asynccontextmanager
async def running(rig: Rig) -> AsyncGenerator[Rig]:
    """在测试函数里启动与停止组合根。

    不能放进异步夹具：pytest-asyncio 在夹具与测试之间退出 run_until_complete，qasync 这时还会处理排队的事件，
    组合根刚创建的监管任务在那个空档里执行，取不到正在运行的循环
    （与 lst/__main__.py 的 run 说明的是同一件事）。
    """
    logs = setup_logging(rig.app.paths.logs_dir)
    await rig.app.start()
    try:
        bus = rig.app.bus
        assert bus is not None
        bus.subscribe(CaptureCompleted, rig.captures.append)
        bus.subscribe(NotificationRequested, rig.notices.append)
        engines = rig.app.ocr_engines
        assert engines is not None
        if not engines.can_run(OcrEngineKind.WINDOWS):
            pytest.skip("本机没有 Windows OCR 语言，OCR 进程不启动")
        yield rig
    finally:
        await rig.app.stop()
        logs.stop()


def rgb(color: QColor) -> tuple[int, int, int]:
    return color.red(), color.green(), color.blue()


def _crop(screens: FakeScreens, name: str, pixels: Rect) -> RawImage:
    pixmap = screens._cache[name]  # pyright: ignore[reportPrivateUsage]
    return to_raw_image(pixmap, pixels)


@pytest.mark.timeout(180)
async def test_capture_result_and_overlay_methods(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running(_make_app(qapp, tmp_path, monkeypatch, flavor=BuildFlavor.DEV)) as rig:
        await _capture_result_and_overlay(rig)


async def _capture_result_and_overlay(rig: Rig) -> None:
    app = rig.app
    assert app.nmt is not None and app.ocr is not None
    assert await asyncio.wait_for(app.ocr.wait_for_state(WorkerState.READY, WorkerState.FAILED), 60) is (
        WorkerState.READY
    )
    assert await asyncio.wait_for(app.nmt.wait_for_state(WorkerState.READY, WorkerState.FAILED), 60) is (
        WorkerState.READY
    )
    call = await rig.connect()
    try:
        # ---- status
        status = await call("status")
        assert status["flavor"] == "dev" and status["pid"] == os.getpid() and not status["installed"]
        assert status["automation"] == {"enabled": True, "explicit": False, "pipe": rig.pipe}
        assert status["workers"]["ocr"]["state"] in ("ready", "busy")
        assert status["workers"]["nmt"]["versions"]["fake"] == "1.0"
        assert status["ocr"]["active"] == "windows"
        assert status["engines"][0]["id"] == "argos"
        assert status["data_dir"] == str(app.paths.data_dir)
        assert {"capture", "wait_result", "overlay.select", "storage.restart"} <= set(status["methods"])

        # ---- capture direct：150% 副屏上的矩形，截图与冻结画面逐字节相同，覆盖窗口按逻辑坐标摆放
        rect = [-2560 + 90, -200 + 210, 1260, 360]
        started = await call("capture", {"rect": rect})
        session = started["session_id"]
        result = await call("wait_result", {"session_id": session, "timeout": 60})
        assert result["settled"] and result["phase"] == "done"
        assert result["capture"] == {
            "width": 1260,
            "height": 360,
            "screen": "left",
            "selection": [-2500, -60, 840, 240],
            "dpr": 1.5,
        }
        assert rig.captures[-1].image.data == _crop(rig.screens, "left", Rect(90, 210, 1260, 360)).data
        [segment] = result["segments"]
        assert segment["source"].startswith("1260x360 ")
        assert segment["finished"] and segment["attempts"]
        assert all({"engine", "state", "latency_ms", "cached"} <= set(a) for a in segment["attempts"])
        overlay = result["overlay"]
        assert overlay["open"] and overlay["phase"] == "result" and overlay["showing"] == "translation"
        assert overlay["geometry"] == [-2500, -60, 840, 240]
        assert overlay["items"][0]["shown"] == segment["display"]
        shown = segment["display"]

        # ---- capture direct 跨屏：左屏的最右 100 列与主屏的最左 100 列拼在一起
        crossing = await call("capture", {"rect": [-100, 10, 200, 50]})
        await call("wait_result", {"session_id": crossing["session_id"], "timeout": 60})
        image = rig.captures[-1].image
        assert (image.width, image.height) == (200, 50)
        assert pixel_at(image, 0, 0) == rgb(coded_color(2460, 210))
        assert pixel_at(image, 150, 0) == rgb(coded_color(50, 10))
        assert rig.captures[-1].geometry.screen_name == "main"  # 两边面积相同，取屏幕列表里靠前的主屏

        # ---- capture mask：真实蒙版，按“框选区域”的键位拖动；选区与矩形一致
        held = await call("capture", {"rect": [100, 100, 400, 200], "mode": "mask", "hold": True})
        assert held["held"] and held["binding"] == "LeftDrag" and held["selection"] == [100, 100, 400, 200]
        state = await call("capture.state")
        assert state["active"] and state["held"] and len(state["masks"]) == 2
        grabbed = await call("grab", {"target": "mask", "name": "mask-check"})
        assert len(grabbed["files"]) == 2 and all(Path(f["path"]).is_file() for f in grabbed["files"])
        released = await call("capture.release")
        masked = released["session_id"]
        assert masked and released["binding"] == "LeftDrag"
        await call("wait_result", {"timeout": 60})
        assert rig.captures[-1].session_id == masked
        assert rig.captures[-1].image.data == _crop(rig.screens, "main", Rect(100, 100, 400, 200)).data
        result = await call("wait_result", {"session_id": masked})
        shown = result["segments"][0]["display"]

        # ---- overlay：选字、复制、切换原文、缩放、固定
        selected = await call("overlay.select", {"start": [0, 0], "end": [0, 4]})
        assert selected["selection"]["text"] == shown[:4]
        copied = await call("overlay.copy")
        assert copied == {"text": shown[:4], "clipboard": False} and rig.copied == []
        assert (await call("overlay.copy", {"clipboard": True}))["clipboard"] and rig.copied == [shown[:4]]
        assert (await call("overlay.toggle_source"))["showing"] == "source"
        assert (await call("overlay.toggle_source"))["showing"] == "translation"
        assert (await call("overlay.zoom_in"))["zoom"] > 1
        assert (await call("overlay.zoom_out"))["zoom"] == pytest.approx(1.0)
        await call("overlay.zoom_in")
        assert (await call("overlay.zoom_reset"))["zoom"] == 1.0
        assert (await call("overlay.pin"))["pinned"]
        item = (await call("overlay.state"))["items"][0]
        x, y, w, h = item["rect"]
        dragged = await call("overlay.select_at", {"from": [x + 2, y + h / 2], "to": [x + w - 2, y + h / 2]})
        assert dragged["binding"] == "LeftDrag" and dragged["selection"]["text"]
        error = await call.error("overlay.select", {"start": [5, 0], "end": [0, 1]})
        assert error.code == "invalid_params"

        # ---- 键位：默认右键单击切换原文；改成 Ctrl+T 之后右键单击不再切换，Ctrl+T 切换
        middle = [x + w / 2, y + h / 2]
        toggled = await call("overlay.mouse", {"action": "overlay.toggle_source", "at": middle})
        assert toggled["sent"] == "RightClick" and toggled["showing"] == "source"
        changed = await call("bindings.set", {"action": "overlay.toggle_source", "bindings": ["Ctrl+T"]})
        assert changed["changed"] and changed["values"]["overlay_toggle_source"] == ["Ctrl+T"]
        assert (await call("overlay.mouse", {"binding": "RightClick", "at": middle}))["showing"] == "source"
        by_key = await call("overlay.key", {"binding": "Ctrl+T"})
        assert by_key["showing"] == "translation"
        by_action = await call("overlay.key", {"action": "overlay.toggle_source"})
        assert by_action["sent"] == "Ctrl+T" and by_action["showing"] == "source"
        listed = await call("bindings.get", {"scope": "overlay"})
        toggle = next(a for a in listed["actions"] if a["id"] == "overlay.toggle_source")
        assert toggle["bindings"] == ["Ctrl+T"] and toggle["defaults"] == ["RightClick"]
        rejected = await call.error(
            "bindings.set", {"action": "overlay.toggle_source", "bindings": ["LeftDrag"]}
        )
        assert rejected.code == "rejected"
        assert (await call("bindings.reset", {"scope": "overlay"}))["changed"]
        restored = await call("overlay.mouse", {"binding": "RightClick", "at": middle})
        assert restored["showing"] == "translation"
        # 蒙版上的“取消截屏”同样按当前键位：Esc 关闭蒙版，不产生会话。
        await call("capture", {"rect": [100, 100, 50, 50], "mode": "mask", "hold": True})
        cancelled = await call("capture.key", {"action": "capture.cancel"})
        assert cancelled == {"sent": "Esc", "active": False}
        assert (await call("capture.state"))["held"] is False
        assert (await call("capture.cancel")) == {"cancelled": False}

        # ---- grab：文件只写在数据目录的 tmp/automation 下，name 不接受路径
        saved = await call("grab", {"target": "overlay", "name": "after"})
        path = Path(saved["files"][0]["path"])
        assert path.parent == app.paths.tmp_dir / "automation" and path.read_bytes()[:4] == b"\x89PNG"
        assert (await call.error("grab", {"name": "../evil"})).code == "invalid_params"
        assert (await call.error("grab", {"target": "desktop"})).code == "invalid_params"

        # ---- 覆盖窗口关闭后，覆盖窗口的方法回复 unavailable
        assert (await call("overlay.close")) == {"open": False}
        assert (await call.error("overlay.copy")).code == "unavailable"

        # ---- 其余：未知方法、日志只记方法名与耗时
        unknown = await call.error("no.such.method")
        assert unknown.code == "unknown_method"
        lines: list[str] = []
        tail: dict[str, Any] = {"lines": []}
        for _ in range(50):
            tail = await call("logs.tail", {"n": 1000})
            lines = [line for line in tail["lines"] if "自动化接口：" in line]
            if any("no.such.method → unknown_method" in line for line in lines):
                break
            await asyncio.sleep(0.1)
        assert any("capture → ok" in line for line in lines), lines[-5:]
        assert not any(shown[:4] in line for line in tail["lines"])
    finally:
        call.client.close()


@pytest.mark.timeout(180)
async def test_settings_storage_and_restart_methods(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running(_make_app(qapp, tmp_path, monkeypatch, flavor=BuildFlavor.DEV)) as rig:
        await _settings_storage_and_restart(rig, tmp_path)


async def _settings_storage_and_restart(rig: Rig, tmp_path: Path) -> None:
    app = rig.app
    call = await rig.connect()
    try:
        # ---- settings
        pages = await call("settings.pages")
        assert pages["pages"][0] == "general" and "about" in pages["pages"]
        assert any(group["id"] == "hotkeys" and group["children"] for group in pages["tree"])
        opened = await call("settings.open", {"page": "hotkeys.overlay"})
        assert opened == {"visible": True, "page": "hotkeys.overlay"}
        assert (await call.error("settings.open", {"page": "nowhere"})).code == "invalid_params"
        assert (await call("settings.get", {"section": "ui"}))["values"]["theme"] == "system"
        assert "automation" in (await call("settings.get"))["sections"]
        changed = await call("settings.set", {"section": "ui", "values": {"theme": "dark"}})
        assert changed["changed"] and changed["values"]["theme"] == "dark"
        same = await call("settings.set", {"section": "ui", "values": {"theme": "dark"}})
        assert same["changed"] is False
        rejected = await call.error("settings.set", {"section": "ocr", "values": {"mode": "hologram"}})
        assert rejected.code == "rejected" and rejected.data == {
            "rejected": [{"location": ["ocr", "mode"], "reason": rejected.data["rejected"][0]["reason"]}]  # type: ignore[index]
        }
        grabbed = await call("grab", {"target": "settings"})
        assert Path(grabbed["files"][0]["path"]).is_file()

        # ---- storage：确认语义与路径校验
        models = tmp_path / "other models"
        assert (await call.error("storage.change_models_dir", {"path": str(models)})).code == "not_confirmed"
        relative = await call.error("storage.change_models_dir", {"path": "models", "confirm": True})
        assert relative.code == "invalid_params"
        moved = await call("storage.change_models_dir", {"path": str(models), "confirm": True})
        assert moved["plan"] == "change" and moved["storage"]["models_dir"] == str(models)
        assert moved["storage"]["failure"] is None and app.paths.models_root == models
        config = tomllib.loads(app.paths.config_file.read_text(encoding="utf-8"))
        assert config["storage"]["models_dir"] == str(models)
        again = await call("storage.change_models_dir", {"path": str(models), "confirm": True})
        assert again["plan"] == "same"

        new_data = tmp_path / "new data"
        copied = await call("storage.change_data_dir", {"path": str(new_data), "confirm": True})
        assert copied["storage"]["pending_data_dir"] == str(new_data) and copied["progress"]
        assert (new_data / "config" / "settings.toml").is_file()
        assert (await call("storage.status"))["pending_data_dir"] == str(new_data)

        # ---- restart：先回复，再退出；组合根记下“退出后重新启动”
        assert (await call.error("storage.restart")).code == "not_confirmed"
        assert not app.restart_requested
        reply = await call("storage.restart", {"confirm": True})
        assert reply == {"restarting": True, "pid": os.getpid()}
        await asyncio.wait_for(app.wait_closed(), 5)
        assert app.restart_requested
    finally:
        call.client.close()


@pytest.mark.timeout(120)
async def test_switch_at_runtime_and_quit(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """普通版默认关闭：没有管道。运行中打开立即可连，第一次开启发一条通知；经由接口自己关掉时，这次请求照常收到
    回复，之后连不上；配置文件里写下了显式的 false。再打开后 quit 让程序退出。"""
    rig = _make_app(qapp, tmp_path, monkeypatch, flavor=BuildFlavor.NORMAL)
    async with running(rig):
        bus, module = rig.app.bus, rig.app.automation
        assert bus is not None and module is not None
        await bus.drain()
        assert module.status is not None and not module.status.enabled and not module.status.listening
        with pytest.raises(FileNotFoundError):
            await rig.connect(timeout=0.3)

        def listening() -> bool:
            status = bus.latest(AutomationStatus)
            return status is not None and status.listening

        bus.send(UpdateConfig({"automation": {"enabled": True}}))
        await _until(listening, 10, "自动化接口开启")
        call = await rig.connect()
        status = await call("status")
        assert status["flavor"] == "normal" and status["automation"]["explicit"] is True
        await _until(lambda: bool(rig.notices), 5, "开启通知")
        assert [n.title_key for n in rig.notices] == ["automation.notice.title"]

        off = await call("settings.set", {"section": "automation", "values": {"enabled": False}})
        assert off["changed"] and off["values"] == {"enabled": False}
        call.client.close()
        await _until(lambda: not listening(), 10, "自动化接口关闭")
        with pytest.raises(FileNotFoundError):
            await rig.connect(timeout=0.3)
        config = tomllib.loads(rig.app.paths.config_file.read_text(encoding="utf-8"))
        assert config["automation"] == {"enabled": False}

        bus.send(UpdateConfig({"automation": {"enabled": True}}))
        await _until(listening, 10, "自动化接口再次开启")
        call = await rig.connect()
        assert (await call("quit"))["quitting"] is True
        await asyncio.wait_for(rig.app.wait_closed(), 5)
        assert not rig.app.restart_requested
        call.client.close()
        await asyncio.sleep(0.1)
        assert [n.title_key for n in rig.notices] == ["automation.notice.title"]  # 本次运行只通知一次
    assert rig.app.automation is not None and rig.app.automation.server is None


@pytest.mark.timeout(120)
async def test_explicit_config_wins_over_the_dev_default(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _make_app(
        qapp, tmp_path, monkeypatch, flavor=BuildFlavor.DEV, config="\n[automation]\nenabled = false\n"
    )
    async with running(rig):
        assert rig.app.bus is not None
        await rig.app.bus.drain()
        status = rig.app.bus.latest(AutomationStatus)
        assert status is not None and status.flavor is BuildFlavor.DEV
        assert (status.enabled, status.explicit, status.listening) == (False, True, False)
        with pytest.raises(FileNotFoundError):
            await rig.connect(timeout=0.3)
