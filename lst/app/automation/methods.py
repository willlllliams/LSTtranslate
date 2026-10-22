"""自动化接口的方法（需求 DEV-04 至 DEV-07，架构文档 16.3 节的方法表）。

每个方法收到一个 JSON 对象形式的参数，返回 JSON，或者抛 MethodError。方法都在主线程的事件循环里执行，
直接读写模块与窗口；需要等待的方法（wait_result、settings.set、storage.*）订阅总线消息等结果，
时限由参数 timeout 给出。

会改动文件或进程的方法（storage.change_data_dir、storage.change_models_dir、storage.restart）要求参数里
显式写 ``confirm: true``：设置页在这里弹确认对话框，接口调用本身就是确认，但必须明确表态。
没有提供执行任意代码、读写任意文件的方法：grab 只写数据目录下的 tmp/automation，logs.tail 只读主日志。
"""

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

from lst.app.automation.interaction import (
    alive,
    describe_masks,
    grab_widgets,
    live_screens,
    mask_for_point,
    mask_local,
    overlay_state,
    parse_point,
    parse_rect,
    resolve_position,
)
from lst.app.automation.recorder import SessionRecorder, describe_record
from lst.app.automation.server import Deferred, MethodError
from lst.app.paths import AppPaths
from lst.automation.protocol import PROTOCOL_VERSION, ErrorCode, Json, Request
from lst.contracts.automation import AutomationStatus, BuildFlavor
from lst.contracts.config import AppConfig, ConfigUpdateCompleted, UpdateConfig
from lst.contracts.events import EngineHealth, EngineStatuses, OpenSettings, QuitRequested, WorkerStatuses
from lst.contracts.ids import SessionId
from lst.contracts.messages import State
from lst.contracts.ocr_engines import OcrEngineCatalog
from lst.contracts.storage import RestartApp, StorageKind, StorageLocations
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus
from lst.ui.capture import CaptureModule, MaskWindow
from lst.ui.common import inject
from lst.ui.common.bindings import ACTIONS, ACTIONS_BY_ID, Scope, bindings_from, parse_binding
from lst.ui.i18n import tr
from lst.ui.overlay import OverlayModule, OverlayWindow
from lst.ui.settings import SettingsModule
from lst.ui.settings.nav import NAV_GROUPS, nav_name, resolve_page_id
from lst.ui.settings.page_storage import StorageSection
from lst.ui.settings.pages import GeneralPage

DEFAULT_WAIT_S: Final = 60.0
CONFIG_WAIT_S: Final = 10.0
MAX_LOG_LINES: Final = 5000


@dataclass
class AutomationDeps:
    """组合根交给自动化接口的东西。各个界面模块没有装配时为 None，对应的方法回复 unavailable。"""

    flavor: BuildFlavor
    version: str
    installed: bool
    paths: Callable[[], AppPaths]
    capture: CaptureModule | None = None
    overlay: OverlayModule | None = None
    settings: SettingsModule | None = None


class Params:
    """参数的读取与校验。类型不对时抛 MethodError(invalid_params)。"""

    def __init__(self, raw: Mapping[str, Json]) -> None:
        self.raw = raw

    def has(self, name: str) -> bool:
        return self.raw.get(name) is not None

    def value(self, name: str) -> Json:
        return self.raw.get(name)

    def text(self, name: str, default: str | None = None) -> str:
        value = self.raw.get(name)
        if value is None:
            if default is None:
                raise MethodError(ErrorCode.INVALID_PARAMS, f"缺少参数 {name}")
            return default
        if not isinstance(value, str):
            raise MethodError(ErrorCode.INVALID_PARAMS, f"参数 {name} 应当是字符串")
        return value

    def optional_text(self, name: str) -> str | None:
        return self.text(name) if self.has(name) else None

    def flag(self, name: str, default: bool) -> bool:
        value = self.raw.get(name)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise MethodError(ErrorCode.INVALID_PARAMS, f"参数 {name} 应当是 true 或 false")
        return value

    def number(self, name: str, default: float, *, low: float, high: float) -> float:
        value = self.raw.get(name)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise MethodError(ErrorCode.INVALID_PARAMS, f"参数 {name} 应当是数字")
        if not low <= value <= high:
            raise MethodError(ErrorCode.INVALID_PARAMS, f"参数 {name} 应当在 {low:g} 到 {high:g} 之间")
        return float(value)

    def integer(self, name: str, default: int, *, low: int, high: int) -> int:
        value = self.raw.get(name)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise MethodError(ErrorCode.INVALID_PARAMS, f"参数 {name} 应当是整数")
        if not low <= value <= high:
            raise MethodError(ErrorCode.INVALID_PARAMS, f"参数 {name} 应当在 {low} 到 {high} 之间")
        return value

    def obj(self, name: str) -> dict[str, Json]:
        value = self.raw.get(name)
        if not isinstance(value, dict):
            raise MethodError(ErrorCode.INVALID_PARAMS, f"参数 {name} 应当是 JSON 对象")
        return value

    def confirmed(self) -> None:
        if self.raw.get("confirm") is not True:
            raise MethodError(
                ErrorCode.NOT_CONFIRMED,
                "这个操作会移动文件或重启程序，设置页在这里要求确认；接口调用请在 params 里写 confirm: true",
            )


def _unavailable(what: str) -> MethodError:
    return MethodError(ErrorCode.UNAVAILABLE, what)


def _storage_json(locations: StorageLocations | None) -> Json:
    if locations is None:
        return None
    task, failure = locations.task, locations.failure
    return {
        "data_dir": locations.data_dir,
        "data_source": locations.data_source.value,
        "models_dir": locations.models_dir,
        "models_default": locations.models_default,
        "pending_data_dir": locations.pending_data_dir,
        "task": None
        if task is None
        else {
            "kind": task.kind.value,
            "target": task.target,
            "done_bytes": task.done_bytes,
            "total_bytes": task.total_bytes,
        },
        "failure": None
        if failure is None
        else {
            "kind": failure.kind.value,
            "code": failure.code.value,
            "target": failure.target,
            "detail": failure.detail,
        },
    }


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


class AutomationMethods:
    """全部方法。dispatch 按方法名分发；方法表见 names()。"""

    def __init__(
        self,
        bus: EventBus,
        deps: AutomationDeps,
        recorder: SessionRecorder,
        status: Callable[[], AutomationStatus | None],
    ) -> None:
        self._bus = bus
        self._deps = deps
        self._recorder = recorder
        self._status = status
        self._held: tuple[MaskWindow, Qt.MouseButton, QPointF] | None = None
        self._held_binding = ""
        self._table: dict[str, Callable[[Params], Awaitable[Json | Deferred]]] = {
            "status": self.status,
            "capture": self.capture,
            "capture.release": self.capture_release,
            "capture.cancel": self.capture_cancel,
            "capture.state": self.capture_state,
            "capture.key": self.capture_key,
            "capture.mouse": self.capture_mouse,
            "wait_result": self.wait_result,
            "overlay.state": self.overlay_get,
            "overlay.select": self.overlay_select,
            "overlay.select_at": self.overlay_select_at,
            "overlay.copy": self.overlay_copy,
            "overlay.toggle_source": self._overlay_call("toggle_source"),
            "overlay.zoom_in": self._overlay_call("zoom_in"),
            "overlay.zoom_out": self._overlay_call("zoom_out"),
            "overlay.zoom_reset": self._overlay_call("zoom_reset"),
            "overlay.pin": self._overlay_call("pin"),
            "overlay.close": self._overlay_call("close"),
            "overlay.key": self.overlay_key,
            "overlay.mouse": self.overlay_mouse,
            "grab": self.grab,
            "settings.open": self.settings_open,
            "settings.get": self.settings_get,
            "settings.set": self.settings_set,
            "settings.pages": self.settings_pages,
            "bindings.get": self.bindings_get,
            "bindings.set": self.bindings_set,
            "bindings.reset": self.bindings_reset,
            "storage.status": self.storage_status,
            "storage.change_data_dir": self.storage_change_data_dir,
            "storage.change_models_dir": self.storage_change_models_dir,
            "storage.restart": self.storage_restart,
            "logs.tail": self.logs_tail,
            "quit": self.quit,
        }

    def names(self) -> list[str]:
        return sorted(self._table)

    async def dispatch(self, request: Request) -> Json | Deferred:
        method = self._table.get(request.method)
        if method is None:
            raise MethodError(
                ErrorCode.UNKNOWN_METHOD, f"没有方法 {request.method}", {"methods": self.names()}
            )
        return await method(Params(request.params))

    # ------------------------------------------------------------------ 状态

    def _latest[S: State](self, kind: type[S]) -> S | None:
        return self._bus.latest(kind)

    async def status(self, _params: Params) -> Json:
        deps = self._deps
        workers = self._latest(WorkerStatuses)
        catalog = self._latest(OcrEngineCatalog)
        engines = self._latest(EngineStatuses)
        automation = self._status()
        listed: list[tuple[int, dict[str, Json]]] = []
        if engines is not None:
            priorities = engines.priorities or {}
            for engine_id, status in engines.statuses.items():
                if status.health is EngineHealth.DISABLED:
                    continue
                priority = priorities.get(engine_id)
                listed.append(
                    (
                        priority if priority is not None else 0,
                        {
                            "id": engine_id,
                            "name": engines.names.get(engine_id, engine_id),
                            "health": status.health.value,
                            "priority": priority,
                            "error_code": status.error_code.value if status.error_code is not None else None,
                        },
                    )
                )
        engine_list: list[Json] = [entry for _, entry in sorted(listed, key=lambda pair: pair[0])]
        return {
            "app": "LSTtranslate",
            "protocol": PROTOCOL_VERSION,
            "version": deps.version,
            "flavor": deps.flavor.value,
            "installed": deps.installed,
            "pid": os.getpid(),
            "qt_platform": QGuiApplication.platformName(),
            "automation": None
            if automation is None
            else {"enabled": automation.enabled, "explicit": automation.explicit, "pipe": automation.pipe},
            "workers": {}
            if workers is None
            else {
                name: {
                    "state": status.state.value,
                    "detail": status.detail,
                    "versions": dict(status.versions),
                }
                for name, status in workers.statuses.items()
            },
            "ocr": None
            if catalog is None
            else {
                "active": catalog.active.value if catalog.active is not None else None,
                "running": catalog.running.value if catalog.running is not None else None,
                "engines": [
                    {
                        "kind": info.kind.value,
                        "state": info.state.value,
                        "needs_download": info.needs_download,
                        "language_count": len(info.languages),
                        "languages": list(info.languages[:12]),
                        "installed_variant": info.installed_variant.value
                        if info.installed_variant is not None
                        else None,
                        "error_code": info.error_code.value if info.error_code is not None else None,
                    }
                    for info in catalog.engines
                ],
            },
            "engines": engine_list,
            "storage": _storage_json(self._latest(StorageLocations)),
            "data_dir": str(deps.paths().data_dir),
            "models_dir": str(deps.paths().models_root),
            "screens": live_screens(),
            "methods": cast(list[Json], self.names()),
        }

    # ------------------------------------------------------------------ 截屏

    def _capture(self) -> CaptureModule:
        module = self._deps.capture
        if module is None or module.controller is None:
            raise _unavailable("截屏模块没有运行")
        if self._held is not None and not module.controller.active:
            self._held = None  # 按着的框选已经结束（键位取消、屏幕布局变化）
        return module

    async def capture(self, params: Params) -> Json:
        """按原生坐标的矩形截屏。mode=direct 不显示蒙版；mode=mask 打开真实蒙版并模拟拖动。"""
        controller = self._capture().controller
        assert controller is not None
        rect = parse_rect(params.value("rect"))
        mode = params.text("mode", "direct")
        if controller.active or self._held is not None:
            raise MethodError(ErrorCode.UNAVAILABLE, "正在框选；先调用 capture.release 或 capture.cancel")
        if mode == "direct":
            try:
                session = controller.capture_rect(rect)
            except ValueError as exc:
                raise MethodError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
            except RuntimeError as exc:
                raise MethodError(ErrorCode.UNAVAILABLE, str(exc)) from exc
            self._recorder.ensure(session)
            return {"session_id": session, "mode": mode, "rect": list(rect.as_tuple())}
        if mode != "mask":
            raise MethodError(ErrorCode.INVALID_PARAMS, "mode 应当是 direct 或 mask")
        selects = inject.current_bindings(controller.bindings, "capture.select")
        if not selects:
            raise _unavailable("“框选区域”没有绑定键位")
        before = controller.last_session
        controller.start()
        await asyncio.sleep(0)
        masks = controller.masks
        if not masks:
            raise _unavailable("截屏蒙版没有打开（冻结屏幕失败）")
        mask = mask_for_point(masks, rect.x, rect.y)
        if mask is None:
            controller.cancel()
            raise MethodError(ErrorCode.INVALID_PARAMS, "rect 的左上角不在任何屏幕上")
        start = mask_local(mask, rect.x, rect.y)
        end = mask_local(mask, rect.right, rect.bottom)
        if params.flag("hold", False):
            button, point = inject.press_and_move(mask, selects[0], start, end)
            self._held = (mask, button, point)
            self._held_binding = selects[0]
            selection = mask.selection
            return {
                "session_id": None,
                "mode": mode,
                "held": True,
                "binding": selects[0],
                "selection": None if selection is None else list(selection.pixels.as_tuple()),
                "masks": describe_masks(masks),
            }
        inject.send_mouse(mask, selects[0], start, end)
        return self._mask_finished(before, mode, selects[0])

    def _mask_finished(self, before: SessionId | None, mode: str, binding: str) -> Json:
        controller = self._capture().controller
        assert controller is not None
        session = controller.last_session
        if session is None or session == before:
            return {"session_id": None, "mode": mode, "binding": binding, "cancelled": True}
        self._recorder.ensure(session)
        return {"session_id": session, "mode": mode, "binding": binding}

    async def capture_release(self, _params: Params) -> Json:
        held, self._held = self._held, None
        binding = self._held_binding
        if held is None:
            raise _unavailable("没有按着的框选（capture 带 hold: true 才会留着）")
        controller = self._capture().controller
        assert controller is not None
        mask, button, point = held
        before = controller.last_session
        if alive(mask) and mask.isVisible():
            inject.release(mask, button, point)
        return self._mask_finished(before, "mask", binding)

    async def capture_cancel(self, _params: Params) -> Json:
        controller = self._capture().controller
        assert controller is not None
        active = controller.active
        self._held = None
        controller.cancel()
        return {"cancelled": active}

    async def capture_state(self, _params: Params) -> Json:
        controller = self._capture().controller
        assert controller is not None
        return {
            "active": controller.active,
            "held": self._held is not None,
            "last_session": controller.last_session,
            "masks": describe_masks(controller.masks),
        }

    def _binding_param(self, params: Params, scope: Scope, *, keyboard: bool) -> str:
        """key、mouse 的键位：直接给 binding，或给 action 取它当前的键位里第一个键盘键位（keyboard 为真）
        或第一个鼠标、滚轮键位（keyboard 为假）。"""
        binding = params.optional_text("binding")
        action = params.optional_text("action")
        if (binding is None) == (action is None):
            raise MethodError(ErrorCode.INVALID_PARAMS, "binding 与 action 恰好给一个")
        if binding is not None:
            try:
                return parse_binding(binding).text
            except ValueError as exc:
                raise MethodError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        assert action is not None
        spec = ACTIONS_BY_ID.get(action)
        if spec is None or spec.scope is not scope:
            raise MethodError(ErrorCode.INVALID_PARAMS, f"{scope.value} 里没有操作 {action}")
        config = self._latest(AppConfig)
        current = bindings_from(config.section("bindings") if config is not None else None)
        bound = [
            text
            for text in inject.current_bindings(current, action)
            if (parse_binding(text).kind == "key") == keyboard
        ]
        if not bound:
            kind = "键盘" if keyboard else "鼠标"
            raise _unavailable(f"操作 {action} 现在没有{kind}键位")
        return bound[0]

    async def capture_key(self, params: Params) -> Json:
        controller = self._capture().controller
        assert controller is not None
        masks = controller.masks
        if not masks:
            raise _unavailable("没有打开的截屏蒙版")
        binding = self._binding_param(params, Scope.CAPTURE, keyboard=True)
        target = next((m for m in masks if m.hasFocus()), masks[0])
        try:
            inject.send_key(target, binding)
        except ValueError as exc:
            raise MethodError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        await asyncio.sleep(0)
        return {"sent": binding, "active": controller.active}

    async def capture_mouse(self, params: Params) -> Json:
        """在蒙版上执行鼠标键位。at、to 是原生坐标（全局物理像素）。"""
        controller = self._capture().controller
        assert controller is not None
        masks = controller.masks
        if not masks:
            raise _unavailable("没有打开的截屏蒙版")
        binding = self._binding_param(params, Scope.CAPTURE, keyboard=False)
        at = parse_point(params.value("at"), "at")
        mask = mask_for_point(masks, round(at[0]), round(at[1]))
        if mask is None:
            raise MethodError(ErrorCode.INVALID_PARAMS, "at 不在任何屏幕上")
        to = parse_point(params.value("to"), "to") if params.has("to") else None
        before = controller.last_session
        try:
            inject.send_mouse(
                mask,
                binding,
                mask_local(mask, *at),
                None if to is None else mask_local(mask, *to),
                steps=params.integer("steps", 1, low=-20, high=20),
            )
        except ValueError as exc:
            raise MethodError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        await asyncio.sleep(0)
        session = controller.last_session
        return {
            "sent": binding,
            "active": controller.active,
            "session_id": session if session != before else None,
        }

    # ------------------------------------------------------------------ 结果

    def _overlay_window(self) -> OverlayWindow | None:
        module = self._deps.overlay
        window = module.window if module is not None else None
        return window if alive(window) else None

    async def wait_result(self, params: Params) -> Json:
        controller = self._capture().controller
        assert controller is not None
        text = params.optional_text("session_id")
        session = SessionId(text) if text is not None else controller.last_session
        if session is None:
            raise MethodError(ErrorCode.INVALID_PARAMS, "还没有截屏，也没有给出 session_id")
        record = self._recorder.get(session)
        if record is None:
            if session != controller.last_session:
                raise MethodError(ErrorCode.INVALID_PARAMS, f"没有会话 {session} 的记录")
            record = self._recorder.ensure(session)
        timeout = params.number("timeout", DEFAULT_WAIT_S, low=0, high=3600)
        deadline = time.monotonic() + timeout
        while not record.settled:
            record.changed.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MethodError(
                    ErrorCode.TIMEOUT, f"{timeout:g} 秒内会话没有全部定下来", self._result(record.session_id)
                )
            try:
                await asyncio.wait_for(record.changed.wait(), remaining)
            except TimeoutError:
                continue
        # 覆盖窗口在同一轮里处理同样的消息，让它先应用最后一次刷新，再读它的状态。
        await asyncio.sleep(0)
        return self._result(record.session_id)

    def _result(self, session: SessionId) -> Json:
        record = self._recorder.get(session)
        data: dict[str, Json] = describe_record(record) if record is not None else {"session_id": session}
        module = self._deps.overlay
        presenter = module.presenter if module is not None else None
        window = self._overlay_window()
        if presenter is not None and presenter.session_id == session:
            data["overlay"] = overlay_state(window)
        else:
            data["overlay"] = {"open": False, "session_id": presenter.session_id if presenter else None}
        return data

    # ------------------------------------------------------------------ 覆盖窗口

    def _window(self) -> OverlayWindow:
        window = self._overlay_window()
        if window is None:
            raise _unavailable("没有打开的覆盖窗口")
        return window

    async def overlay_get(self, _params: Params) -> Json:
        return overlay_state(self._overlay_window())

    async def overlay_select(self, params: Params) -> Json:
        window = self._window()
        start = resolve_position(window, params.value("start"), "start")
        end = resolve_position(window, params.value("end"), "end")
        window.select_range(start, end)
        return overlay_state(window)

    async def overlay_select_at(self, params: Params) -> Json:
        """按“选择文字”当前的键位，在窗口里从 from 拖到 to（窗口内逻辑坐标）。"""
        window = self._window()
        start = parse_point(params.value("from"), "from")
        end = parse_point(params.value("to"), "to")
        config = self._latest(AppConfig)
        bound = inject.current_bindings(
            bindings_from(config.section("bindings") if config is not None else None), "overlay.select"
        )
        if not bound:
            raise _unavailable("“选择文字”没有绑定键位")
        inject.send_mouse(window, bound[0], start, end)
        return {"binding": bound[0], **overlay_state(window)}

    async def overlay_copy(self, params: Params) -> Json:
        """返回选中的文字。clipboard 为真时另外按复制键的路径写系统剪贴板（覆盖用户剪贴板里的内容）。"""
        window = self._window()
        text = window.selected_text()
        clipboard = params.flag("clipboard", False)
        if clipboard and text:
            window.copy_selection()
        return {"text": text, "clipboard": clipboard and bool(text)}

    def _overlay_call(self, name: str) -> Callable[[Params], Awaitable[Json]]:
        async def call(_params: Params) -> Json:
            window = self._window()
            match name:
                case "toggle_source":
                    window.toggle_source()
                case "zoom_in":
                    window.zoom_step(1)
                case "zoom_out":
                    window.zoom_step(-1)
                case "zoom_reset":
                    window.zoom_reset()
                case "pin":
                    window.toggle_pinned()
                case _:
                    window.close()
            await asyncio.sleep(0)
            return overlay_state(self._overlay_window())

        return call

    async def overlay_key(self, params: Params) -> Json:
        window = self._window()
        binding = self._binding_param(params, Scope.OVERLAY, keyboard=True)
        try:
            inject.send_key(window, binding)
        except ValueError as exc:
            raise MethodError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        await asyncio.sleep(0)
        return {"sent": binding, **overlay_state(self._overlay_window())}

    async def overlay_mouse(self, params: Params) -> Json:
        window = self._window()
        binding = self._binding_param(params, Scope.OVERLAY, keyboard=False)
        at = parse_point(params.value("at"), "at") if params.has("at") else None
        to = parse_point(params.value("to"), "to") if params.has("to") else None
        try:
            inject.send_binding(window, binding, at, to, steps=params.integer("steps", 1, low=-20, high=20))
        except ValueError as exc:
            raise MethodError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        await asyncio.sleep(0)
        return {"sent": binding, **overlay_state(self._overlay_window())}

    # ------------------------------------------------------------------ 画面

    async def grab(self, params: Params) -> Json:
        target = params.text("target", "overlay")
        widgets: dict[str, QWidget] = {}
        if target == "overlay":
            widgets["overlay"] = self._window()
        elif target == "mask":
            controller = self._capture().controller
            assert controller is not None
            masks = controller.masks
            if not masks:
                raise _unavailable("没有打开的截屏蒙版")
            for mask in masks:
                widgets[f"mask-{mask.screen_geometry.name}"] = mask
        elif target == "settings":
            module = self._deps.settings
            window = module.window if module is not None else None
            if window is None:
                raise _unavailable("设置窗口没有创建")
            window.flush()
            widgets["settings"] = window
        else:
            raise MethodError(ErrorCode.INVALID_PARAMS, "target 应当是 overlay、mask 或 settings")
        directory = self._deps.paths().tmp_dir / "automation"
        saved = grab_widgets(widgets, directory, params.optional_text("name"))
        return {"files": saved, "directory": str(directory)}

    # ------------------------------------------------------------------ 设置

    def _settings(self) -> SettingsModule:
        module = self._deps.settings
        if module is None or module.window is None or module.presenter is None:
            raise _unavailable("设置窗口模块没有运行")
        return module

    async def settings_open(self, params: Params) -> Json:
        module = self._settings()
        window = module.window
        assert window is not None
        page = params.optional_text("page")
        resolved = resolve_page_id(page) if page is not None else None
        if page is not None and resolved is None:
            raise MethodError(ErrorCode.INVALID_PARAMS, f"没有页面 {page}", {"pages": self._page_ids()})
        self._bus.send(OpenSettings())
        deadline = time.monotonic() + 3.0
        while not window.isVisible() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if resolved is not None:
            window.show_page(resolved)
        return {"visible": window.isVisible(), "page": window.current_page.page_id}

    async def settings_get(self, params: Params) -> Json:
        config = self._latest(AppConfig)
        if config is None:
            raise _unavailable("配置还没有加载")
        section = params.optional_text("section")
        if section is None:
            return {"revision": config.revision, "sections": sorted(config.sections)}
        if section not in config.sections:
            raise MethodError(
                ErrorCode.INVALID_PARAMS, f"没有配置节 {section}", {"sections": sorted(config.sections)}
            )
        return {
            "revision": config.revision,
            "section": section,
            "values": cast(Json, config.section(section)),
        }

    async def settings_set(self, params: Params) -> Json:
        section = params.text("section")
        values = params.value("values")
        if not isinstance(values, dict | list):
            raise MethodError(ErrorCode.INVALID_PARAMS, "values 应当是 JSON 对象（数组节是条目数组）")
        timeout = params.number("timeout", CONFIG_WAIT_S, low=0.1, high=120)
        return await self.update_config(section, cast(JsonValue, values), timeout)

    async def update_config(self, section: str, values: JsonValue, timeout: float) -> Json:
        """等待本次请求的精确回执，成功、拒绝和无变化均由配置服务确认。"""
        request_id = uuid.uuid4().hex
        reply: asyncio.Future[ConfigUpdateCompleted] = asyncio.get_running_loop().create_future()

        def on_completed(event: ConfigUpdateCompleted) -> None:
            if event.request_id == request_id and not reply.done():
                reply.set_result(event)

        subscription = self._bus.subscribe(ConfigUpdateCompleted, on_completed)
        try:
            self._bus.send(UpdateConfig({section: values}, request_id))
            completed = await asyncio.wait_for(reply, timeout)
        except TimeoutError as exc:
            raise MethodError(ErrorCode.TIMEOUT, "等待配置修改回执超时") from exc
        finally:
            subscription.cancel()
        reasons: list[Json] = [
            {"location": cast(list[Json], list(event.location)), "reason": event.reason}
            for event in completed.rejected
        ]
        if completed.rejected and not completed.changed:
            raise MethodError(ErrorCode.REJECTED, "配置服务拒绝了这次修改", {"rejected": reasons})
        return {
            "changed": completed.changed,
            "revision": completed.config.revision,
            "rejected": reasons,
            "section": section,
            "values": cast(Json, completed.config.section(section)),
        }

    def _page_ids(self) -> list[Json]:
        return [leaf for group in NAV_GROUPS for leaf in (group.children or (group.id,))]

    async def settings_pages(self, _params: Params) -> Json:
        module = self._deps.settings
        window = module.window if module is not None else None
        tree: list[Json] = [
            {
                "id": group.id,
                "name": nav_name(group.id),
                "children": [{"id": child, "name": nav_name(child)} for child in group.children],
            }
            for group in NAV_GROUPS
        ]
        return {
            "tree": tree,
            "pages": self._page_ids(),
            "current": window.current_page.page_id if window is not None else None,
            "visible": window.isVisible() if window is not None else False,
        }

    # ------------------------------------------------------------------ 键位

    async def bindings_get(self, params: Params) -> Json:
        scope = params.optional_text("scope")
        config = self._latest(AppConfig)
        current = bindings_from(config.section("bindings") if config is not None else None)
        actions: list[Json] = []
        for spec in ACTIONS:
            if scope is not None and spec.scope.value != scope:
                continue
            actions.append(
                {
                    "id": spec.id,
                    "scope": spec.scope.value,
                    "kind": spec.kind.value,
                    "field": spec.field,
                    "name": tr(f"bindings.action.{spec.id}"),
                    "defaults": list(spec.defaults),
                    "bindings": list(current.bindings_of(spec)),
                }
            )
        return {"actions": actions}

    async def bindings_set(self, params: Params) -> Json:
        action = params.text("action")
        spec = ACTIONS_BY_ID.get(action)
        if spec is None:
            raise MethodError(
                ErrorCode.INVALID_PARAMS, f"没有操作 {action}", {"actions": sorted(ACTIONS_BY_ID)}
            )
        values = params.value("bindings")
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise MethodError(ErrorCode.INVALID_PARAMS, "bindings 应当是键位文字的数组")
        timeout = params.number("timeout", CONFIG_WAIT_S, low=0.1, high=120)
        return await self.update_config("bindings", {spec.field: cast(JsonValue, values)}, timeout)

    async def bindings_reset(self, params: Params) -> Json:
        scope = params.text("scope", "all")
        scopes = {s.value for s in Scope}
        if scope != "all" and scope not in scopes:
            raise MethodError(ErrorCode.INVALID_PARAMS, f"scope 应当是 all 或 {'、'.join(sorted(scopes))}")
        values: dict[str, JsonValue] = {
            spec.field: list(spec.defaults) for spec in ACTIONS if scope == "all" or spec.scope.value == scope
        }
        timeout = params.number("timeout", CONFIG_WAIT_S, low=0.1, high=120)
        return await self.update_config("bindings", values, timeout)

    # ------------------------------------------------------------------ 存储位置

    def _storage_section(self) -> StorageSection:
        window = self._settings().window
        assert window is not None
        page = window.page("general")
        if not isinstance(page, GeneralPage):
            raise _unavailable("常规页没有存储位置卡片")
        return page.storage

    async def storage_status(self, _params: Params) -> Json:
        return _storage_json(self._latest(StorageLocations))

    @staticmethod
    def _absolute(params: Params, name: str, *, allow_empty: bool = False) -> str:
        path = params.text(name)
        if allow_empty and not path:
            return ""
        if not Path(path).is_absolute():
            raise MethodError(ErrorCode.INVALID_PARAMS, f"{name} 应当是绝对路径")
        return os.path.normpath(path)

    async def storage_change_data_dir(self, params: Params) -> Json:
        target = self._absolute(params, "path")
        copy = params.flag("copy", True)
        params.confirmed()
        timeout = params.number("timeout", 600, low=1, high=7200)
        section = self._storage_section()
        before = self._latest(StorageLocations)
        plan = section.plan_data_dir(target)
        if plan in ("unavailable", "fixed", "busy"):
            raise MethodError(
                ErrorCode.REJECTED, f"现在不能更改数据目录（{plan}）", {"storage": _storage_json(before)}
            )
        if plan == "same":
            return {"plan": plan, "storage": _storage_json(before), "progress": []}

        def done(locations: StorageLocations) -> bool:
            failure = locations.failure
            if failure is not None and failure.kind is StorageKind.DATA:
                return True
            if plan == "revert":
                return locations.pending_data_dir is None
            pending = locations.pending_data_dir
            return pending is not None and _same_path(pending, target)

        return await self._storage_wait(
            lambda: section.submit_data_dir(target, copy=copy), plan, done, timeout
        )

    async def storage_change_models_dir(self, params: Params) -> Json:
        target = self._absolute(params, "path", allow_empty=True)
        move = params.flag("move", True)
        params.confirmed()
        timeout = params.number("timeout", 1800, low=1, high=7200)
        section = self._storage_section()
        before = self._latest(StorageLocations)
        plan = section.plan_models_dir(target)
        if plan in ("unavailable", "busy"):
            raise MethodError(
                ErrorCode.REJECTED, f"现在不能更改模型目录（{plan}）", {"storage": _storage_json(before)}
            )
        if plan == "same":
            return {"plan": plan, "storage": _storage_json(before), "progress": []}

        def done(locations: StorageLocations) -> bool:
            failure = locations.failure
            if failure is not None and failure.kind is StorageKind.MODELS:
                return True
            return locations.models_default if not target else _same_path(locations.models_dir, target)

        return await self._storage_wait(
            lambda: section.submit_models_dir(target, move=move), plan, done, timeout
        )

    async def _storage_wait(
        self,
        submit: Callable[[], str],
        plan: str,
        done: Callable[[StorageLocations], bool],
        timeout: float,
    ) -> Json:
        """提交之后跟着 StorageLocations 等更改结束：没有进行中的任务，并且 done 为真。记下途中的进度。"""
        before = self._latest(StorageLocations)
        progress: list[Json] = []
        finished: list[StorageLocations] = []
        signal = asyncio.Event()

        def on_locations(locations: StorageLocations) -> None:
            if locations is before:
                return  # 订阅时的回放
            task = locations.task
            if task is not None:
                sample: Json = [task.done_bytes, task.total_bytes]
                if not progress or progress[-1] != sample:
                    progress.append(sample)
            elif done(locations):
                finished.append(locations)
                signal.set()

        subscription = self._bus.subscribe(StorageLocations, on_locations)
        try:
            submit()
            try:
                await asyncio.wait_for(signal.wait(), timeout)
            except TimeoutError:
                raise MethodError(
                    ErrorCode.TIMEOUT,
                    f"{timeout:g} 秒内更改没有结束",
                    {"storage": _storage_json(self._latest(StorageLocations)), "progress": progress},
                ) from None
        finally:
            subscription.cancel()
        final = finished[-1]
        failure = final.failure
        result: dict[str, Json] = {"plan": plan, "storage": _storage_json(final), "progress": progress}
        if failure is not None:
            raise MethodError(
                ErrorCode.REJECTED, f"更改失败（{failure.code.value}）：{failure.detail}", result
            )
        return result

    async def storage_restart(self, params: Params) -> Json | Deferred:
        params.confirmed()
        module = self._deps.settings
        presenter = module.presenter if module is not None else None

        def restart() -> None:
            if presenter is not None and presenter.storage is not None:
                presenter.restart_app()  # 与常规页的“立即重启”相同
            else:
                self._bus.send(RestartApp())

        return Deferred({"restarting": True, "pid": os.getpid()}, restart)

    # ------------------------------------------------------------------ 日志与退出

    async def logs_tail(self, params: Params) -> Json:
        count = params.integer("n", 100, low=1, high=MAX_LOG_LINES)
        path = self._deps.paths().logs_dir / "main.log"
        lines = await asyncio.to_thread(_tail, path, count)
        return {"path": str(path), "lines": cast(list[Json], lines)}

    async def quit(self, _params: Params) -> Json | Deferred:
        return Deferred({"quitting": True, "pid": os.getpid()}, lambda: self._bus.publish(QuitRequested()))


def _tail(path: Path, count: int) -> list[str]:
    """文件最后 count 行。从文件末尾往前按块读，不把整个日志读进内存。"""
    try:
        with path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            block, data = 65536, b""
            position = size
            while position > 0 and data.count(b"\n") <= count:
                position = max(0, position - block)
                file.seek(position)
                data = file.read(size - position)
    except FileNotFoundError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-count:]


__all__ = ["AutomationDeps", "AutomationMethods", "Params"]
