"""组合根的配置应用与推理进程决策（lst/app/bootstrap.py）。

不启动真实的模块和子进程：监管器、OCR 引擎模块、模型管理模块都换成记录调用的替身，总线是真实的 EventBus。
总线把每个协程处理函数包成独立的任务，所以连续发布的几次 AppConfig 会并发执行 _on_config，
这里的测试检查组合根把它们串行化，并且总是应用总线上最新的配置。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
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
from lst.contracts.events import RestartWorker, WorkerStatuses
from lst.contracts.models import ModelsInstalled
from lst.contracts.nmt_runtime import NmtRuntimeChanged
from lst.contracts.ocr_engines import (
    OcrEngineCatalog,
    OcrEngineErrorCode,
    OcrEngineInfo,
    OcrEngineKind,
    OcrEngineState,
    OcrRuntimeVariant,
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
        self.versions: dict[str, str] = {}

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
    running: OcrEngineKind | None = None

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
            running=self.running,
        )

    def set_running(self, kind: OcrEngineKind | None) -> None:
        self.running = kind

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

    variants: dict[OcrEngineKind, str] = field(default_factory=lambda: dict[OcrEngineKind, str]())
    ready: dict[OcrEngineKind, tuple[tuple[str, bool], ...]] = field(
        default_factory=lambda: dict[OcrEngineKind, tuple[tuple[str, bool], ...]]()
    )

    def installed_variant(self, kind: OcrEngineKind) -> str:
        return self.variants.get(kind, "")

    def selected_models_ready(self, kind: OcrEngineKind) -> tuple[tuple[str, bool], ...]:
        return self.ready.get(kind, ())

    def worker_model_settings(self, kind: OcrEngineKind) -> dict[str, JsonValue]:
        return {"rec_model_path": "C:/models/rec.onnx"} if kind is OcrEngineKind.RAPID else {}

    # 换模型目录时组合根按 Module 协议停止、启动它。
    name = "ocr_engines"
    stopped: int = 0

    async def start(self, ctx: ModuleContext) -> None:
        del ctx
        self.publish()

    async def stop(self) -> None:
        self.stopped += 1


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

        def __init__(self, models_dir: Path, client: object, url: str, **kwargs: object) -> None:
            del client, url, kwargs  # dict_dir、dict_index_url
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


async def test_missing_engine_falls_back_to_windows_ocr_until_installed(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """选用的引擎没有装好时先用 Windows OCR，装好后换回（OCRE-04）。"""
    rig.engines.runnable = {W}
    with caplog.at_level(logging.WARNING, logger="lst.app.bootstrap"):
        rig.configure({"ocr": {"engine": "paddle", "device": "gpu", "language": "ja"}})
        await rig.bus.drain()
    # Windows OCR 的进程级字段只有语言：语言变了才重载，换成 paddle 本身不会停掉正在运行的 Windows OCR。
    assert rig.ocr.reloads("engine") == ["windows"]
    assert rig.ocr.reloads("language") == ["ja"]
    assert ("stop", None) not in rig.ocr.calls
    assert rig.engines.running is W
    assert caplog.text.count("先用 Windows OCR") == 1

    rig.configure({"ocr": {"engine": "paddle", "device": "gpu", "language": "ja"}, "ui": {"theme": "dark"}})
    await rig.bus.drain()
    assert rig.ocr.reloads("engine") == ["windows"]  # 无关的配置变化不重启
    assert caplog.text.count("先用 Windows OCR") == 1

    rig.engines.runnable = {W, P}
    rig.engines.publish()
    await rig.bus.drain()
    assert rig.ocr.reloads("engine") == ["windows", "paddle"]
    assert rig.engines.running is P


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


async def test_failed_process_restarts_only_when_the_ocr_decision_changes(rig: Rig) -> None:
    rig.configure({"ocr": {"language": "ja"}})
    await rig.bus.drain()
    rig.worker_state("ocr", WorkerState.FAILED, "确定性故障")
    await rig.bus.drain()
    # 进入 FAILED 时组合根合并进程状态、引擎目录随之重新发布；这不应触发重启，否则确定性故障会无限循环。
    assert rig.ocr.reloads("language") == ["ja"]
    assert rig.engines.overlays[-1] == (W, OcrEngineState.ERROR, OcrEngineErrorCode.WORKER_FAILED)

    # 与 OCR 无关的配置变化不重启：否则每保存一次设置，确定性故障的进程就被拉起一次，设置窗口一直显示加载中。
    rig.configure({"ocr": {"language": "ja"}, "ui": {"theme": "dark"}})
    await rig.bus.drain()
    assert rig.ocr.reloads("language") == ["ja"]
    assert rig.engines.overlays[-1] == (W, OcrEngineState.ERROR, OcrEngineErrorCode.WORKER_FAILED)

    # 决定进程的字段变了（这里是识别语言）才重新启动。
    rig.configure({"ocr": {"language": "en"}, "ui": {"theme": "dark"}})
    await rig.bus.drain()
    assert rig.ocr.reloads("language") == ["ja", "en"]


async def test_paddle_gpu_on_cpu_runtime_shows_error_and_stays_there(rig: Rig) -> None:
    """用户报告的问题：PaddleOCR 选了显卡推理，装的却是 CPU 版，进程以依赖错误（65）退出进入 FAILED。

    设置窗口应显示出错，之后其他设置的保存、引擎目录的重新发布都不应把进程重新拉起（那会让卡片回到
    “正在加载…”）。装好 CUDA 版（已装版本变化）后自动重新启动。
    """
    rig.engines.runnable = {W, P}
    rig.engines.variants = {P: "cpu"}
    rig.configure({"ocr": {"engine": "paddle", "device": "gpu"}})
    await rig.bus.drain()
    assert rig.ocr.reloads("device") == ["gpu"]
    rig.worker_state("ocr", WorkerState.STARTING)
    rig.worker_state("ocr", WorkerState.LOADING)
    rig.worker_state("ocr", WorkerState.FAILED, "确定性故障 DEPENDENCY_ERROR（退出码 65）")
    await rig.bus.drain()
    assert rig.engines.overlays[-1] == (P, OcrEngineState.ERROR, OcrEngineErrorCode.RUNTIME_MISMATCH)
    for revision_theme in ("dark", "light", "dark"):
        rig.configure({"ocr": {"engine": "paddle", "device": "gpu"}, "ui": {"theme": revision_theme}})
        rig.engines.publish()
        await rig.bus.drain()
    assert rig.ocr.reloads("device") == ["gpu"]
    assert rig.engines.overlays[-1] == (P, OcrEngineState.ERROR, OcrEngineErrorCode.RUNTIME_MISMATCH)

    rig.engines.variants = {P: "cuda"}
    rig.engines.publish()
    await rig.bus.drain()
    assert rig.ocr.reloads("device") == ["gpu", "gpu"]


async def test_downloaded_selected_model_restarts_a_failed_process(rig: Rig) -> None:
    rig.engines.runnable = {W, P}
    rig.engines.ready = {P: (("PP-OCRv5_server_det", False),)}
    rig.configure({"ocr": {"engine": "paddle"}, "ocr_paddle": {"detection_model": "PP-OCRv5_server_det"}})
    await rig.bus.drain()
    assert rig.ocr.reloads("detection_model") == ["PP-OCRv5_server_det"]
    rig.worker_state("ocr", WorkerState.FAILED, "模型目录中缺少 PP-OCRv5_server_det")
    rig.engines.ready = {P: (("PP-OCRv5_server_det", True),)}
    rig.engines.publish()
    await rig.bus.drain()
    assert rig.ocr.reloads("detection_model") == ["PP-OCRv5_server_det", "PP-OCRv5_server_det"]


async def test_pipeline_fields_reach_the_worker_and_reload_only_when_marked(rig: Rig) -> None:
    rig.engines.runnable = {W, OcrEngineKind.RAPID}
    rig.configure({"ocr": {"engine": "rapid"}, "ocr_rapid": {"det_box_thresh": 0.4}})
    await rig.bus.drain()
    [settings] = [s for name, s in rig.ocr.calls if name == "reload"]
    assert settings["det_box_thresh"] == 0.4 and settings["det_thresh"] == 0.3
    assert settings["rec_model_path"] == "C:/models/rec.onnx"
    # 请求级参数变化不重载，带 reload 标记的参数变化才重载。
    rig.configure({"ocr": {"engine": "rapid"}, "ocr_rapid": {"det_box_thresh": 0.6}})
    await rig.bus.drain()
    assert len(rig.ocr.reloads("det_thresh")) == 1
    rig.configure({"ocr": {"engine": "rapid"}, "ocr_rapid": {"det_box_thresh": 0.6, "det_thresh": 0.2}})
    await rig.bus.drain()
    assert rig.ocr.reloads("det_thresh") == [0.3, 0.2]


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


async def test_rolled_back_runtime_restarts_even_when_the_installed_variant_is_unchanged(rig: Rig) -> None:
    rig.engines.runnable = {W, P}
    rig.engines.variants = {P: "cpu"}
    rig.configure({"ocr": {"engine": "paddle", "device": "cpu"}})
    await rig.bus.drain()
    assert rig.ocr.reloads("device") == ["cpu"]
    # 更新前显式停机；发布失败并回滚，最终可用性与版本和更新前相同。
    await rig.app._before_ocr_uninstall(P)  # pyright: ignore[reportPrivateUsage]
    rig.engines.publish()
    await rig.bus.drain()
    assert rig.ocr.reloads("device") == ["cpu", "cpu"]


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


def install_fake_runtime(root: Path) -> Path:
    """在 root 下放一个结构完整的显卡运行库（DLL 是假内容），返回 DLL 目录。"""
    version_dir = root / "12.9.2.10"
    (version_dir / "bin").mkdir(parents=True)
    files: dict[str, int] = {}
    for name in ("bin/cublasLt64_12.dll", "bin/cublas64_12.dll"):
        (version_dir / name).write_bytes(b"dll")
        files[name] = 3
    marker = {"version": "12.9.2.10", "files": files}
    (version_dir / "installed.json").write_text(json.dumps(marker), encoding="utf-8")
    return version_dir / "bin"


async def test_gpu_runtime_install_and_delete_restart_nmt_with_the_dll_dir(rig: Rig, tmp_path: Path) -> None:
    """显卡运行库装好后 NMT 进程带上 cuda_dll_dir 重启；删除前先停进程，删除后不带它重启（MOD-06）。"""
    rig.configure({"nmt": {"beam_size": 2}})
    await rig.bus.drain()
    assert "cuda_dll_dir" not in rig.nmt.calls[-1][1]
    bin_dir = install_fake_runtime(AppPaths(tmp_path / "data").nmt_runtime_dir)
    rig.bus.publish(NmtRuntimeChanged(installed=True, bin_dir=str(bin_dir)))
    await rig.bus.drain()
    assert rig.nmt.calls[-1] == ("reload", {**rig.nmt.calls[-1][1], "cuda_dll_dir": str(bin_dir)})

    await rig.app._before_nmt_runtime_delete()  # pyright: ignore[reportPrivateUsage]
    assert rig.nmt.calls[-1] == ("stop", None)
    shutil.rmtree(bin_dir.parent)
    rig.bus.publish(NmtRuntimeChanged(installed=False))
    await rig.bus.drain()
    name, settings = rig.nmt.calls[-1]
    assert name == "reload" and "cuda_dll_dir" not in settings


async def test_worker_status_carries_the_reported_versions(rig: Rig) -> None:
    rig.nmt.versions = {"device": "cuda", "ctranslate2": "4.8.3"}
    rig.worker_state("nmt", WorkerState.READY)
    statuses = rig.bus.latest(WorkerStatuses)
    assert statuses is not None and statuses.statuses["nmt"].versions["device"] == "cuda"


async def test_dictionary_changes_do_not_reload_nmt(rig: Rig) -> None:
    rig.configure({"nmt": {"beam_size": 2}})
    await rig.bus.drain()
    rig.bus.publish(ModelsInstalled(("dict:en:zh-Hans",), removed=("dict:de:en",)))
    await rig.bus.drain()
    assert rig.app._nmt_reload is None  # pyright: ignore[reportPrivateUsage]
    assert rig.nmt.reloads("beam_size") == [2]


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
    assert "--backend" not in paddle
    rapid = bootstrap.ocr_worker_command(OcrEngineKind.RAPID, paths)
    assert rapid[:6] == (*prefix, "ocr")
    assert rapid[7] == str(paths.data_dir / "models" / "engines" / "rapid" / "site")
    assert rapid[8:10] == ("--backend", bootstrap.RAPID_BACKEND)
    layout = bootstrap.rapid_layout(paths)
    assert isinstance(layout, bootstrap.RapidInstallPaths)
    assert layout.model_dir == paths.ocr_models_dir / "rapid"
    assert layout.requirements(OcrRuntimeVariant.CUDA) == root / "app" / "ocr-rapid-cuda-requirements.txt"
    paddle_paths = bootstrap.paddle_layout(paths)
    assert isinstance(paddle_paths, bootstrap.PaddleInstallPaths)
    cpu = paddle_paths.requirements(OcrRuntimeVariant.CPU)
    assert cpu == root / "app" / "ocr-paddle-cpu-requirements.txt"


def test_source_commands_use_the_ocr_venv_with_the_right_backend(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path / "data")
    rapid = bootstrap.ocr_worker_command(OcrEngineKind.RAPID, paths)
    assert rapid[0].endswith("python.exe") and ".venv-ocr" in rapid[0]
    assert rapid[rapid.index("--backend") + 1] == bootstrap.RAPID_BACKEND
    assert "--backend" not in bootstrap.ocr_worker_command(P, paths)
    dev = bootstrap.rapid_layout(paths)
    assert isinstance(dev, bootstrap.DevRapidEnvironment)
    assert dev.model_dir == paths.ocr_models_dir / "rapid"


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


def test_ocr_failure_code_names_deterministic_failures() -> None:
    code = bootstrap.ocr_failure_code
    assert code("确定性故障 DEPENDENCY_ERROR（退出码 65）") is OcrEngineErrorCode.RUNTIME_MISMATCH
    assert code("确定性故障 CONFIG_ERROR（退出码 66）") is OcrEngineErrorCode.BAD_CONFIG
    assert code("5 分钟内崩溃 3 次") is OcrEngineErrorCode.WORKER_FAILED


# ---------------------------------------------------------------- 运行中更换模型目录


class FakeModule:
    """模型管理、显卡运行库模块的替身：记下构造时的目录与启停次数。"""

    def __init__(self, name: str, directory: Path) -> None:
        self.name = name
        self.directory = directory
        self.started = self.stopped = 0

    async def start(self, ctx: ModuleContext) -> None:
        del ctx
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


class FakeEnginesModule(FakeModule):
    """引擎模块的替身：只记下词典目录的更换。"""

    def __init__(self, directory: Path) -> None:
        super().__init__("engines", directory)
        self.dict_dirs: list[Path | None] = []

    async def set_dict_dir(self, directory: Path | None) -> None:
        self.dict_dirs.append(directory)


@dataclass
class Relocation:
    app: Application
    created: list[tuple[str, Path]]
    engines: FakeEnginesModule
    ocr_modules: list[FakeEngines]


@pytest.fixture
def relocation(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Relocation:
    """给组合根装上三个用到模型目录的模块的替身，构造新模块时记下它们拿到的目录。"""
    app = rig.app
    created: list[tuple[str, Path]] = []
    ocr_modules: list[FakeEngines] = []

    def models(models_dir: Path, client: object, url: str, **kwargs: object) -> FakeModule:
        del client, url
        created.append(("models", models_dir))
        created.append(("dict", cast(Path, kwargs["dict_dir"])))
        return FakeModule("models", models_dir)

    def ocr_engines() -> FakeEngines:
        paths = app.paths
        created.append(("ocr", paths.ocr_models_dir))
        created.append(("engines", paths.engines_dir))
        module = FakeEngines(bus=rig.bus)
        ocr_modules.append(module)
        return module

    def runtime() -> FakeModule:
        created.append(("runtime", app.paths.nmt_runtime_dir))
        return FakeModule("nmt_runtime", app.paths.nmt_runtime_dir)

    monkeypatch.setattr(bootstrap, "ModelsModule", models)
    # 源码版在模型目录没有模型时回退到仓库的 assets/argosmodel；这里的目录都是空的，关掉回退。
    monkeypatch.setattr(bootstrap, "LEGACY_NMT_MODELS", app.paths.data_dir / "no-legacy")
    monkeypatch.setattr(app, "_new_ocr_engines", ocr_engines)
    monkeypatch.setattr(app, "_new_nmt_runtime", runtime)
    engines = FakeEnginesModule(app.paths.dict_models_dir)
    app.client = cast(Any, object())
    app.models = cast(Any, FakeModule("models", app.paths.nmt_models_dir))
    app.nmt_runtime = cast(Any, FakeModule("nmt_runtime", app.paths.nmt_runtime_dir))
    app.engines = cast(Any, engines)
    context = ModuleContext(rig.bus, logging.getLogger("t"))
    for module in (app.models, app.engines, app.nmt_runtime, app.ocr_engines):
        app._started.append((cast(Any, module), context))  # pyright: ignore[reportPrivateUsage]
    return Relocation(app, created, engines, ocr_modules)


async def test_suspend_stops_processes_and_model_modules_and_pauses_decisions(
    rig: Rig, relocation: Relocation
) -> None:
    rig.configure({"nmt": {"beam_size": 2}, "ocr": {"language": "en"}})
    await rig.bus.drain()
    app = relocation.app
    old_models, old_runtime = cast(FakeModule, app.models), cast(FakeModule, app.nmt_runtime)
    rig.nmt.calls.clear()
    rig.ocr.calls.clear()

    await app.suspend_models()
    assert rig.nmt.calls == [("stop", None)] and rig.ocr.calls == [("stop", None)]
    assert (old_models.stopped, old_runtime.stopped) == (1, 1)

    # 停下期间配置变化、模型装好、RestartWorker 都不启动进程。
    rig.configure({"nmt": {"beam_size": 3}, "ocr": {"language": "ja"}})
    rig.bus.publish(ModelsInstalled(("argos:en:ja",)))
    rig.bus.send(RestartWorker("ocr"))
    await rig.bus.drain()
    if app._nmt_reload is not None:  # pyright: ignore[reportPrivateUsage]
        await app._nmt_reload  # pyright: ignore[reportPrivateUsage]
    assert rig.nmt.calls == [("stop", None)] and rig.ocr.calls == [("stop", None)]


async def test_resume_rebuilds_modules_in_the_new_dir_and_restarts_processes(
    rig: Rig, relocation: Relocation, tmp_path: Path
) -> None:
    """运行中更换模型目录：三个模块按新目录重建，字典引擎换词典目录，NMT 与 OCR 进程按最新配置重新启动，
    不需要重启程序（需求 MOD-04）。"""
    rig.configure({"nmt": {"beam_size": 2}, "ocr": {"language": "en"}})
    await rig.bus.drain()
    app = relocation.app
    order = [module.name for module, _ in app._started]  # pyright: ignore[reportPrivateUsage]
    await app.suspend_models()
    rig.configure({"nmt": {"beam_size": 3}, "ocr": {"language": "ja"}})
    await rig.bus.drain()
    rig.nmt.calls.clear()
    rig.ocr.calls.clear()
    relocation.created.clear()

    new_root = tmp_path / "big" / "models"
    await app.resume_models(new_root)
    await rig.bus.drain()

    assert app.paths.models_root == new_root
    assert sorted(relocation.created) == sorted(
        [
            ("models", new_root / "nmt"),
            ("dict", new_root / "dict"),
            ("ocr", new_root / "ocr"),
            ("engines", new_root / "engines"),
            ("runtime", new_root / "runtimes" / "cublas12"),
        ]
    )
    assert relocation.engines.dict_dirs == [new_root / "dict"]
    assert all((new_root / name).is_dir() for name in ("nmt", "ocr", "dict"))
    assert [module.name for module, _ in app._started] == order  # pyright: ignore[reportPrivateUsage]
    assert app.ocr_engines is relocation.ocr_modules[-1]
    # NMT 进程用新目录与停下期间的最新配置重新启动，OCR 进程同样按最新配置启动。
    [nmt_settings] = [settings for name, settings in rig.nmt.calls if name == "reload"]
    assert nmt_settings["model_dir"] == str(new_root / "nmt")
    assert nmt_settings["extract_dir"] == str(new_root / "nmt" / ".extracted")
    assert nmt_settings["beam_size"] == 3
    assert rig.ocr.reloads("language") == ["ja"]


async def test_hand_edited_models_dir_is_picked_up_without_moving_files(
    rig: Rig, relocation: Relocation, tmp_path: Path
) -> None:
    """用户直接改了配置文件里的 [storage].models_dir：按新目录重建模块、重启进程，不移动任何文件。"""
    rig.configure({"nmt": {"beam_size": 2}})
    await rig.bus.drain()
    rig.nmt.calls.clear()
    (relocation.app.paths.nmt_models_dir).mkdir(parents=True)
    (relocation.app.paths.nmt_models_dir / "keep.argosmodel").write_bytes(b"x")
    new_root = tmp_path / "edited"
    rig.configure({"nmt": {"beam_size": 2}, "storage": {"models_dir": str(new_root)}})
    await rig.bus.drain()
    assert relocation.app.paths.models_root == new_root
    assert ("stop", None) in rig.nmt.calls
    assert rig.nmt.reloads("model_dir")[-1] == str(new_root / "nmt")
    assert (tmp_path / "data" / "models" / "nmt" / "keep.argosmodel").is_file()

    rig.nmt.calls.clear()
    rig.configure({"nmt": {"beam_size": 2}, "storage": {"models_dir": str(new_root)}, "ui": {}})
    await rig.bus.drain()
    assert rig.nmt.calls == []  # 目录没变：不再重启


def test_storage_section_accepts_only_absolute_paths() -> None:
    assert bootstrap.StorageConfig().models_dir == ""
    assert bootstrap.StorageConfig(models_dir="D:\\models").models_dir == "D:\\models"
    with pytest.raises(ValueError):
        bootstrap.StorageConfig(models_dir="models")
