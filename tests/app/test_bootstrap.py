"""组合根的配置应用与推理进程决策（lst/app/bootstrap.py）。

不启动真实的模块和子进程：监管器、OCR 引擎模块、模型管理模块都换成记录调用的替身，总线是真实的 EventBus。
总线把每个协程处理函数包成独立的任务，所以连续发布的几次 AppConfig 会并发执行 _on_config，
这里的测试检查组合根把它们串行化，并且总是应用总线上最新的配置。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

import lst.app.bootstrap as bootstrap
from lst.app.bootstrap import Application, AppOptions, ocr_model_cache
from lst.app.paths import AppPaths
from lst.app.supervisor import WorkerSupervisor
from lst.contracts.config import AppConfig
from lst.contracts.events import RestartWorker
from lst.contracts.models import ModelsInstalled
from lst.contracts.ocr_engines import (
    OcrEngineCatalog,
    OcrEngineErrorCode,
    OcrEngineInfo,
    OcrEngineKind,
    OcrEngineState,
)
from lst.contracts.ports import WorkerState
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus, ModuleContext
from lst.services.config import MemorySecretStore
from lst.services.ocr_engines import OcrEnginesModule

W, P = OcrEngineKind.WINDOWS, OcrEngineKind.PADDLE


class FakeSupervisor:
    """记录 reload、stop 调用的监管器替身。delay 是每次 reload 花的时间。"""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[tuple[str, Any]] = []
        self.running = False

    async def reload(self, settings: Mapping[str, JsonValue], *, command: Any = None) -> None:
        self.calls.append(("reload", dict(settings)))
        self.running = True
        await asyncio.sleep(self.delay)

    async def stop(self) -> None:
        self.calls.append(("stop", None))
        self.running = False

    def reloads(self, key: str) -> list[JsonValue]:
        return [settings[key] for name, settings in self.calls if name == "reload"]


@dataclass
class FakeEngines:
    """OCR 引擎模块的替身：can_run、catalog 与 set_process_state。"""

    runnable: set[OcrEngineKind] = field(default_factory=lambda: {W})
    languages: tuple[str, ...] = ("en",)
    overlays: list[tuple[OcrEngineKind, OcrEngineState | None, OcrEngineErrorCode | None]] = field(
        default_factory=lambda: list[tuple[OcrEngineKind, OcrEngineState | None, OcrEngineErrorCode | None]]()
    )
    bus: EventBus | None = None

    def can_run(self, kind: OcrEngineKind) -> bool:
        return kind in self.runnable

    def catalog(self) -> OcrEngineCatalog:
        def state(kind: OcrEngineKind) -> OcrEngineState:
            return OcrEngineState.INSTALLED if kind in self.runnable else OcrEngineState.NOT_INSTALLED

        return OcrEngineCatalog(
            engines=(
                OcrEngineInfo(W, state(W), needs_download=False, languages=self.languages),
                OcrEngineInfo(P, state(P), needs_download=True),
            ),
            active=W,
        )

    def set_process_state(
        self,
        kind: OcrEngineKind,
        state: OcrEngineState | None,
        error_code: OcrEngineErrorCode | None = None,
        detail: str = "",
    ) -> None:
        del detail
        self.overlays.append((kind, state, error_code))
        # 与真实模块相同：进程状态变化也重新发布引擎目录。
        if self.bus is not None:
            self.bus.publish(self.catalog())

    def publish(self) -> None:
        assert self.bus is not None
        self.bus.publish(self.catalog())


@dataclass
class Rig:
    app: Application
    bus: EventBus
    nmt: FakeSupervisor
    ocr: FakeSupervisor
    engines: FakeEngines
    revision: int = 0

    def configure(self, sections: dict[str, JsonValue]) -> None:
        self.revision += 1
        self.bus.publish(AppConfig(sections, self.revision))

    def worker_state(self, name: str, state: WorkerState, detail: str = "") -> None:
        self.app._on_worker_state(name)(state, detail)  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    app = Application(
        AppOptions(
            paths=AppPaths(tmp_path / "data"), secrets=MemorySecretStore(), ocr_command=lambda e: (e.value,)
        )
    )
    bus = EventBus(asyncio.get_running_loop())
    nmt, ocr, engines = FakeSupervisor(), FakeSupervisor(), FakeEngines(bus=bus)
    app.bus = bus
    app.nmt = cast(WorkerSupervisor, nmt)
    app.ocr = cast(WorkerSupervisor, ocr)
    app.ocr_engines = cast(OcrEnginesModule, engines)
    app._subscribe(bus)  # pyright: ignore[reportPrivateUsage]
    # 与真实的 OCR 引擎模块相同：启动时发布一次目录。组合根在目录发布之前不决定 OCR 进程。
    engines.publish()
    await bus.drain()
    # 按默认配置的首次启动属于夹具，各测试只看自己操作之后的调用。
    ocr.calls.clear()
    nmt.calls.clear()
    engines.overlays.clear()
    yield Rig(app, bus, nmt, ocr, engines)
    await bus.aclose()


# ---------------------------------------------------------------- 配置串行化


async def test_concurrent_configs_end_with_the_latest_ocr_settings(rig: Rig) -> None:
    """第一次配置改 [nmt]（重载要一段时间），紧接着第二次只改 [ocr] 语言：最终 OCR 用第二次的语言。"""
    rig.configure({"nmt": {"beam_size": 2}, "ocr": {"language": "en"}})
    await rig.bus.drain()
    rig.nmt.delay = 0.2
    rig.nmt.calls.clear()
    rig.ocr.calls.clear()

    rig.configure({"nmt": {"beam_size": 3}, "ocr": {"language": "en"}})
    await asyncio.sleep(0.05)
    rig.configure({"nmt": {"beam_size": 3}, "ocr": {"language": "ja"}})
    await rig.bus.drain()

    assert rig.ocr.reloads("language")[-1] == "ja"
    assert rig.ocr.reloads("language") == ["ja"]
    assert rig.nmt.reloads("beam_size") == [3]


async def test_concurrent_model_dir_changes_replace_the_module_once(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[FakeModels] = []

    class FakeModels:
        name = "models"

        def __init__(self, models_dir: Path, client: object, url: str) -> None:
            del client, url
            self.models_dir = models_dir
            self.started = self.stopped = 0
            created.append(self)

        async def start(self, ctx: ModuleContext) -> None:
            del ctx
            self.started += 1
            await asyncio.sleep(0.05)

        async def stop(self) -> None:
            self.stopped += 1
            await asyncio.sleep(0.05)

    monkeypatch.setattr(bootstrap, "ModelsModule", FakeModels)
    app = rig.app
    old = FakeModels(tmp_path / "m0", None, "")
    app.models = cast(Any, old)
    app._models_dir = tmp_path / "m0"  # pyright: ignore[reportPrivateUsage]
    app.client = cast(Any, object())
    app._started.append((cast(Any, old), ModuleContext(rig.bus, logging.getLogger("t"))))  # pyright: ignore[reportPrivateUsage]

    rig.configure({"nmt": {"model_dir": str(tmp_path / "m1")}})
    await asyncio.sleep(0)
    rig.configure({"nmt": {"model_dir": str(tmp_path / "m1")}, "ui": {}})
    await rig.bus.drain()

    assert old.stopped == 1
    assert [m.models_dir.name for m in created] == ["m0", "m1"]
    new = created[1]
    assert (new.started, new.stopped) == (1, 0)
    assert [m for m, _ in app._started] == [new]  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------- OCR 进程的决策


async def test_unavailable_engine_does_not_start_and_catalog_starts_it_later(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.engines.runnable = set()
    with caplog.at_level(logging.WARNING, logger="lst.app.bootstrap"):
        rig.configure({"ocr": {"engine": "paddle"}})
        await rig.bus.drain()
    assert rig.ocr.reloads("engine") == []
    assert ("stop", None) in rig.ocr.calls
    assert "不可用" in caplog.text

    # 引擎装好，引擎目录重新发布：这时启动。
    rig.engines.runnable = {W, P}
    rig.engines.publish()
    await rig.bus.drain()
    assert rig.ocr.reloads("engine") == ["paddle"]


async def test_same_configuration_does_not_reload_twice(rig: Rig) -> None:
    rig.configure({"ocr": {"language": "en"}})
    await rig.bus.drain()
    rig.configure({"ocr": {"language": "en", "table": False}})  # 子能力开关随请求发送，不重载
    rig.engines.publish()
    await rig.bus.drain()
    assert rig.ocr.reloads("language") == ["en"]


async def test_windows_language_list_change_restarts_the_process(rig: Rig) -> None:
    """Windows 新装了 OCR 语言（引擎目录的语言列表变化）时重新启动 OCR 进程。"""
    rig.configure({"ocr": {"language": "ja"}})
    await rig.bus.drain()
    rig.engines.languages = ("en", "ja")
    rig.engines.publish()
    await rig.bus.drain()
    assert rig.ocr.reloads("language") == ["ja", "ja"]


async def test_failed_process_restarts_on_next_configuration(rig: Rig) -> None:
    rig.configure({"ocr": {"language": "ja"}})
    await rig.bus.drain()
    rig.worker_state("ocr", WorkerState.FAILED, "确定性故障")
    await rig.bus.drain()
    # 进入 FAILED 时组合根合并进程状态、引擎目录随之重新发布；这不应触发重启，否则确定性故障会无限循环。
    assert rig.ocr.reloads("language") == ["ja"]
    assert rig.engines.overlays[-1] == (W, OcrEngineState.ERROR, OcrEngineErrorCode.WORKER_FAILED)

    rig.configure({"ocr": {"language": "ja"}, "ui": {"theme": "dark"}})
    await rig.bus.drain()
    assert rig.ocr.reloads("language") == ["ja", "ja"]


async def test_restart_worker_recovers_failed_ocr(rig: Rig) -> None:
    rig.configure({"ocr": {"language": "ja"}})
    await rig.bus.drain()
    rig.worker_state("ocr", WorkerState.FAILED, "确定性故障")
    rig.bus.send(RestartWorker("ocr"))
    await rig.bus.drain()
    assert rig.ocr.reloads("language") == ["ja", "ja"]


async def test_restart_worker_reloads_nmt_and_ignores_unknown(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.configure({"nmt": {"beam_size": 4}})
    await rig.bus.drain()
    rig.bus.send(RestartWorker("nmt"))
    with caplog.at_level(logging.WARNING, logger="lst.app.bootstrap"):
        rig.bus.send(RestartWorker("tts"))
        await rig.bus.drain()
    assert rig.nmt.reloads("beam_size") == [4, 4]
    assert "tts" in caplog.text


async def test_restart_worker_does_not_start_disabled_nmt(rig: Rig) -> None:
    rig.configure({"nmt": {"enabled": False}})
    await rig.bus.drain()
    rig.bus.send(RestartWorker("nmt"))
    await rig.bus.drain()
    assert rig.nmt.reloads("beam_size") == []


async def test_before_uninstall_stops_the_process_using_the_engine(rig: Rig) -> None:
    rig.configure({"ocr": {"language": "en"}})
    await rig.bus.drain()
    await rig.app._before_ocr_uninstall(P)  # pyright: ignore[reportPrivateUsage]
    assert rig.ocr.calls[-1][0] == "reload"  # 不是当前引擎，不动进程

    await rig.app._before_ocr_uninstall(W)  # pyright: ignore[reportPrivateUsage]
    await rig.bus.drain()
    assert ("stop", None) in rig.ocr.calls
    assert (W, None, None) in rig.engines.overlays
    # 去重键已清除：之后的判断会重新启动进程。
    rig.engines.languages = ("en",)
    rig.configure({"ocr": {"language": "en"}})
    await rig.bus.drain()
    assert rig.ocr.reloads("language") == ["en", "en"]


@pytest.mark.parametrize(
    ("state", "overlay"),
    [
        (WorkerState.STARTING, (W, OcrEngineState.LOADING, None)),
        (WorkerState.LOADING, (W, OcrEngineState.LOADING, None)),
        (WorkerState.CRASHED, (W, OcrEngineState.LOADING, None)),
        (WorkerState.READY, (W, OcrEngineState.READY, None)),
        (WorkerState.BUSY, (W, OcrEngineState.READY, None)),
        (WorkerState.FAILED, (W, OcrEngineState.ERROR, OcrEngineErrorCode.WORKER_FAILED)),
        (WorkerState.STOPPED, (W, None, None)),
    ],
)
async def test_ocr_process_state_is_merged_into_the_catalog(
    rig: Rig,
    state: WorkerState,
    overlay: tuple[OcrEngineKind, OcrEngineState | None, OcrEngineErrorCode | None],
) -> None:
    rig.configure({"ocr": {}})
    await rig.bus.drain()
    rig.worker_state("ocr", state, "detail")
    assert rig.engines.overlays[-1] == overlay


# ---------------------------------------------------------------- 模型变化后重载 NMT


async def test_models_installed_reloads_are_merged(rig: Rig) -> None:
    rig.configure({"nmt": {"beam_size": 2}})
    await rig.bus.drain()
    rig.nmt.delay = 0.1
    for _ in range(3):
        rig.bus.publish(ModelsInstalled(("argos:en:ja",)))
        await asyncio.sleep(0.01)
    await rig.bus.drain()
    task = rig.app._nmt_reload  # pyright: ignore[reportPrivateUsage]
    assert task is not None
    await task
    # 首次按配置启动一次；三次模型变化合并成两次（进行中的一次加结束后补的一次）。
    assert rig.nmt.reloads("beam_size") == [2, 2, 2]


async def test_models_installed_does_not_restart_disabled_nmt(rig: Rig) -> None:
    rig.configure({"nmt": {"beam_size": 2}})
    await rig.bus.drain()
    rig.nmt.delay = 0.1
    rig.bus.publish(ModelsInstalled(("argos:en:ja",)))
    await asyncio.sleep(0.02)
    rig.bus.publish(ModelsInstalled(("argos:en:de",)))  # 合并到进行中的重载之后
    rig.configure({"nmt": {"enabled": False}})
    await rig.bus.drain()
    task = rig.app._nmt_reload  # pyright: ignore[reportPrivateUsage]
    assert task is not None
    await task
    assert rig.nmt.calls[-1] == ("stop", None)
    assert not rig.nmt.running


# ---------------------------------------------------------------- 停止


async def test_configuration_after_stop_begins_is_ignored(rig: Rig) -> None:
    rig.configure({"ocr": {"language": "en"}})
    await rig.bus.drain()
    rig.ocr.delay = 0.1
    rig.configure({"ocr": {"language": "ja"}})
    await asyncio.sleep(0.02)  # 第二次配置的 OCR 重载进行中
    rig.configure({"ocr": {"language": "ko"}})
    await rig.app.stop()
    assert rig.ocr.calls[-1] == ("stop", None)
    assert "ko" not in rig.ocr.reloads("language")


# ---------------------------------------------------------------- 模型目录


def test_ocr_model_cache_needs_models_not_just_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """源码版回退到旧模型目录的条件与 OCR 引擎模块一致：official_models 里要有模型子目录。"""
    legacy = tmp_path / "legacy"
    (legacy / "official_models" / "PP-OCRv5_mobile_det").mkdir(parents=True)
    monkeypatch.setattr(bootstrap, "LEGACY_OCR_MODELS", legacy)
    paths = AppPaths(tmp_path / "data")
    (paths.ocr_models_dir / "official_models").mkdir(parents=True)
    assert ocr_model_cache(paths) == legacy  # 新目录只有空的 official_models

    (paths.ocr_models_dir / "official_models" / "PP-OCRv5_mobile_det").mkdir()
    assert ocr_model_cache(paths) == paths.ocr_models_dir


# ---------------------------------------------------------------- 启动命令


def test_installed_commands_share_the_launcher_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "install"
    monkeypatch.setattr(bootstrap, "is_installed", lambda: True)
    monkeypatch.setattr(bootstrap, "install_root", lambda: root)
    paths = AppPaths(tmp_path / "data")
    python, launcher = str(root / "runtime" / "python.exe"), str(root / "launcher" / "worker.py")
    prefix = (python, "-I", "-X", "utf8", launcher)
    assert bootstrap.worker_command("nmt") == (*prefix, "nmt", "--log-level", "WARNING")
    assert bootstrap.ocr_worker_command(W, paths) == (
        *prefix, "ocr", "--ocr-site", "main", "--backend", bootstrap.WINOCR_BACKEND, "--log-level", "WARNING",
    )  # fmt: skip
    paddle = bootstrap.ocr_worker_command(P, paths)
    assert paddle[:6] == (*prefix, "ocr")
    assert paddle[6:8] == ("--ocr-site", paddle[7]) and paddle[7].startswith(str(paths.data_dir))
    assert paddle[-2:] == ("--log-level", "WARNING")


async def test_ocr_waits_for_the_engine_catalog_before_deciding(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """引擎目录发布之前，配置变化不启停 OCR 进程、不记“引擎不可用”的警告；目录发布后才启动（安装版实测）。"""
    app = Application(
        AppOptions(
            paths=AppPaths(tmp_path / "data"), secrets=MemorySecretStore(), ocr_command=lambda e: (e.value,)
        )
    )
    bus = EventBus(asyncio.get_running_loop())
    ocr, engines = FakeSupervisor(), FakeEngines(bus=bus)
    app.bus = bus
    app.nmt = cast(WorkerSupervisor, FakeSupervisor())
    app.ocr = cast(WorkerSupervisor, ocr)
    app.ocr_engines = cast(OcrEnginesModule, engines)
    app._subscribe(bus)  # pyright: ignore[reportPrivateUsage]
    try:
        with caplog.at_level(logging.WARNING, logger="lst.app.bootstrap"):
            bus.publish(AppConfig({"ocr": {"language": "en"}}, 1))
            await bus.drain()
        assert ocr.calls == []
        assert "不可用" not in caplog.text

        engines.publish()
        await bus.drain()
        assert ocr.reloads("language") == ["en"]
    finally:
        await bus.aclose()
