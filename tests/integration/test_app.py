"""M3.5 验收：启动整个应用（组合根），在双语入口完成一次竞速翻译，再正常退出。

外部依赖都替换掉：临时数据目录、内存密钥存储（不碰 Windows 凭据管理器）、NMT 进程用假后端、
Qt 用 offscreen 平台（不弹窗口、不出托盘图标）、HTTP 用 MockTransport（不发出真实网络请求）。

首次运行引导在启动时弹出并自动获取模型索引，MockTransport 只应答索引地址，其他请求一律报错并记录。
"""

import asyncio
import json
import os
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import lst.app.bootstrap as bootstrap
from lst.app.bootstrap import Application, AppOptions
from lst.app.paths import AppPaths, DataDirChoice
from lst.contracts.config import ConfigRejected, UpdateConfig
from lst.contracts.events import (
    EngineRoster,
    OpenSettings,
    QuitRequested,
    SegmentsUpdated,
    TranslateText,
    WorkerStatuses,
)
from lst.contracts.ids import EngineId, Lang, SessionId
from lst.contracts.models import ModelCatalog, ModelsInstalled
from lst.contracts.ocr_engines import OcrEngineCatalog, OcrEngineErrorCode, OcrEngineKind, OcrEngineState
from lst.contracts.ports import WorkerState
from lst.contracts.storage import ChangeDataDir, ChangeModelsDir, DataDirSource, RestartApp, StorageLocations
from lst.kernel import ModuleContext
from lst.services.config import MemorySecretStore
from lst.ui.settings.window import SettingsWindow

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
INDEX_URL = "https://index.invalid/index.json"
DICT_INDEX_URL = "https://dicts.invalid/dict-index.json"
INDEX = [
    {
        "from_code": "en",
        "to_code": "ja",
        "package_version": "1.0",
        "links": ["https://models.invalid/translate-en_ja-1_0.argosmodel"],
    }
]


class _Network:
    """假网络：只应答模型索引，记录全部请求。"""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if str(request.url) == DICT_INDEX_URL:
            return httpx.Response(200, content=b'{"version": 1, "packs": []}')
        if str(request.url) == INDEX_URL:
            return httpx.Response(200, content=json.dumps(INDEX).encode())
        return httpx.Response(404)


@pytest.mark.timeout(120)
async def test_app_starts_translates_in_bilingual_entry_and_quits(qapp: QApplication, tmp_path: Path) -> None:
    network = _Network()
    app = Application(
        AppOptions(
            paths=AppPaths(tmp_path / "data"),
            secrets=MemorySecretStore(),
            nmt_command=NMT_FAKE,
            ocr_command=lambda _engine: OCR_FAKE,
            tray_icon=None,
            http_transport=httpx.MockTransport(network.handle),
            models_index_url=INDEX_URL,
            dict_index_url=DICT_INDEX_URL,
        ),
        qapp,
    )
    await app.start()
    try:
        bus = app.bus
        assert bus is not None and app.nmt is not None

        # 配置文件自动生成，默认引擎是本地 NMT、两个未启用的免费接口与词典（词典优先级最低）。
        config_file = tmp_path / "data" / "config" / "settings.toml"
        assert config_file.is_file()
        engines = tomllib.loads(config_file.read_text(encoding="utf-8"))["engines"]
        assert [(e["kind"], e["enabled"]) for e in engines] == [
            ("local_nmt", True),
            ("google_free", False),
            ("microsoft_free", False),
            ("dictionary", True),
        ]

        assert await asyncio.wait_for(app.nmt.wait_for_state(WorkerState.READY, WorkerState.FAILED), 60) is (
            WorkerState.READY
        )
        # OCR 进程按 [ocr] 节在后台预加载（OCR-07）。
        assert app.ocr is not None
        assert await asyncio.wait_for(app.ocr.wait_for_state(WorkerState.READY, WorkerState.FAILED), 60) is (
            WorkerState.READY
        )
        statuses = bus.latest(WorkerStatuses)
        assert statuses is not None and statuses.statuses["nmt"].state in (
            WorkerState.READY,
            WorkerState.BUSY,
        )

        # NMT 就绪后引擎模块按模型指纹重建本地 NMT 实例，名册里的实例支持 en→zh-Hans。
        async def roster_ready() -> None:
            while True:
                roster = bus.latest(EngineRoster)
                if roster is not None and any(
                    e.supports(Lang("en"), Lang("zh-Hans")) for e in roster.engines
                ):
                    return
                await asyncio.sleep(0.05)

        await asyncio.wait_for(roster_ready(), 30)

        updates: list[SegmentsUpdated] = []
        bus.subscribe(SegmentsUpdated, updates.append)
        bus.send(
            TranslateText(SessionId("s1"), "hello world", Lang("en"), Lang("zh-Hans"), EngineId("argos"))
        )

        async def translated() -> str:
            while True:
                for update in updates:
                    for view in update.views:
                        if view.finished and view.shown_engine == EngineId("argos"):
                            return view.display_text
                await asyncio.sleep(0.05)

        assert await asyncio.wait_for(translated(), 30) == "[en>zh-Hans]hello world"

        # 首次运行引导弹出，并自动获取了模型索引；全程只请求过索引地址。
        assert app.onboarding is not None and app.onboarding.window is not None
        assert app.onboarding.window.isVisible()

        async def index_loaded() -> ModelCatalog:
            while True:
                catalog = bus.latest(ModelCatalog)
                if catalog is not None and catalog.index_loaded:
                    return catalog
                await asyncio.sleep(0.05)

        catalog = await asyncio.wait_for(index_loaded(), 10)
        assert any((str(m.source), str(m.target)) == ("en", "ja") for m in catalog.models)
        assert sorted(network.requests) == sorted([INDEX_URL, DICT_INDEX_URL])

        # 托盘菜单的“设置”发送 OpenSettings，设置窗口显示出来（需求 UI-05）。
        bus.send(OpenSettings())
        await bus.drain()
        assert any(isinstance(w, SettingsWindow) and w.isVisible() for w in QApplication.topLevelWidgets())

        # 模型装好后 NMT 进程重启并重新就绪，重新扫描模型目录。
        nmt = app.nmt
        old_pid = nmt.pid
        bus.publish(ModelsInstalled(("argos:en:ja",)))

        async def restarted() -> None:
            while nmt.pid in (None, old_pid) or nmt.state is not WorkerState.READY:
                await asyncio.sleep(0.05)

        await asyncio.wait_for(restarted(), 30)

        bus.publish(QuitRequested())
        await asyncio.wait_for(app.wait_closed(), 5)
    finally:
        await app.stop()
    assert app.nmt.pid is None or app.nmt.state is WorkerState.STOPPED


@pytest.mark.timeout(120)
async def test_changing_model_dir_replaces_models_module(qapp: QApplication, tmp_path: Path) -> None:
    """运行中修改 [nmt].model_dir：模型管理模块换成指向新目录的实例，NMT 进程用同一目录重载（MOD-04）。"""
    network = _Network()
    app = Application(
        AppOptions(
            paths=AppPaths(tmp_path / "data"),
            secrets=MemorySecretStore(),
            nmt_command=NMT_FAKE,
            ocr_command=lambda _engine: OCR_FAKE,
            tray_icon=None,
            http_transport=httpx.MockTransport(network.handle),
            models_index_url=INDEX_URL,
            dict_index_url=DICT_INDEX_URL,
        ),
        qapp,
    )
    await app.start()
    try:
        bus = app.bus
        assert bus is not None and app.models is not None
        old = app.models
        new_dir = tmp_path / "other-models"
        new_dir.mkdir()
        bus.send(UpdateConfig({"nmt": {"model_dir": str(new_dir)}}))

        async def replaced() -> None:
            while app.models is old:
                await asyncio.sleep(0.05)

        await asyncio.wait_for(replaced(), 10)
        assert app.models is not None and app.models.models_dir == new_dir
        assert sum(1 for module, _ in app._started if module is app.models) == 1  # pyright: ignore[reportPrivateUsage]
        assert all(module is not old for module, _ in app._started)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.stop()


@pytest.mark.timeout(120)
async def test_switching_ocr_engine_restarts_ocr_process_with_new_command(
    qapp: QApplication, tmp_path: Path
) -> None:
    """改 [ocr].engine：OCR 进程用新引擎的命令重启（OCRE-04）。

    源码运行时 PaddleOCR 是否可用取决于 .venv-ocr 与本地模型。
    """
    requested: list[OcrEngineKind] = []

    def command(engine: OcrEngineKind) -> Sequence[str]:
        requested.append(engine)
        return OCR_FAKE

    app = Application(
        AppOptions(
            paths=AppPaths(tmp_path / "data"),
            secrets=MemorySecretStore(),
            nmt_command=NMT_FAKE,
            ocr_command=command,
            tray_icon=None,
            http_transport=httpx.MockTransport(_Network().handle),
            models_index_url=INDEX_URL,
            dict_index_url=DICT_INDEX_URL,
        ),
        qapp,
    )
    await app.start()
    try:
        bus, ocr, engines = app.bus, app.ocr, app.ocr_engines
        assert bus is not None and ocr is not None and engines is not None
        if not engines.can_run(OcrEngineKind.WINDOWS):
            pytest.skip("本机没有 Windows OCR 语言")
        assert await asyncio.wait_for(ocr.wait_for_state(WorkerState.READY), 60) is WorkerState.READY
        assert requested[-1] is OcrEngineKind.WINDOWS
        if not engines.can_run(OcrEngineKind.PADDLE):
            pytest.skip("本机没有可用的 PaddleOCR 开发环境")
        first_pid = ocr.pid

        bus.send(UpdateConfig({"ocr": {"engine": "paddle"}}))

        async def switched() -> None:
            while ocr.pid in (None, first_pid) or ocr.state is not WorkerState.READY:
                await asyncio.sleep(0.05)

        await asyncio.wait_for(switched(), 30)
        assert requested[-1] is OcrEngineKind.PADDLE
        catalog = bus.latest(OcrEngineCatalog)
        assert catalog is not None and catalog.active is OcrEngineKind.PADDLE
    finally:
        await app.stop()


class _RejectionRecorder:
    """附加模块：记录启动后收到的 ConfigRejected。它晚于托盘启动，与设置窗口的位置相同。"""

    name = "rejection-recorder"

    def __init__(self) -> None:
        self.rejections: list[ConfigRejected] = []

    async def start(self, ctx: ModuleContext) -> None:
        ctx.subscribe(ConfigRejected, self.rejections.append)

    async def stop(self) -> None:
        pass


@pytest.mark.timeout(120)
async def test_startup_config_errors_reach_modules_started_after_the_tray(
    qapp: QApplication, tmp_path: Path
) -> None:
    """配置文件里的错误在全部模块启动之后补发一次，晚于托盘启动的模块（设置窗口）也能收到（U6）。"""
    config_file = tmp_path / "data" / "config" / "settings.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text('[ocr]\nmode = "hologram"\n', encoding="utf-8")
    recorder = _RejectionRecorder()
    app = Application(
        AppOptions(
            paths=AppPaths(tmp_path / "data"),
            secrets=MemorySecretStore(),
            nmt_command=NMT_FAKE,
            ocr_command=None,
            tray_icon=None,
            extra_modules=(recorder,),
            http_transport=httpx.MockTransport(_Network().handle),
            models_index_url=INDEX_URL,
            dict_index_url=DICT_INDEX_URL,
        ),
        qapp,
    )
    try:
        await app.start()
        assert app.bus is not None
        await app.bus.drain()
        assert [r.location for r in recorder.rejections] == [("ocr", "mode")]
    finally:
        await app.stop()


@pytest.mark.timeout(120)
async def test_paddle_gpu_on_cpu_runtime_shows_error_in_the_catalog(
    qapp: QApplication, tmp_path: Path
) -> None:
    """用户报告的问题：PaddleOCR 选了显卡推理而装的是 CPU 版。进程以依赖错误退出进入 FAILED，引擎目录里
    PaddleOCR 显示出错与原因；之后保存与 OCR 无关的设置不会把进程重新拉起（卡片不会回到“正在加载”）。"""
    failing = (
        sys.executable,
        "-m",
        "lst.workers.ocr_worker",
        "--backend",
        "tests.integration.gpu_failure_backend:CpuOnlyBackend",
    )

    def command(engine: OcrEngineKind) -> Sequence[str]:
        return failing if engine is OcrEngineKind.PADDLE else OCR_FAKE

    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    paths.config_file.write_text('[ocr]\nengine = "paddle"\ndevice = "gpu"\n', encoding="utf-8")
    app = Application(
        AppOptions(
            paths=paths,
            secrets=MemorySecretStore(),
            nmt_command=NMT_FAKE,
            ocr_command=command,
            tray_icon=None,
            http_transport=httpx.MockTransport(_Network().handle),
            models_index_url=INDEX_URL,
            dict_index_url=DICT_INDEX_URL,
        ),
        qapp,
    )
    await app.start()
    try:
        bus, ocr, engines = app.bus, app.ocr, app.ocr_engines
        assert bus is not None and ocr is not None and engines is not None
        if not engines.can_run(OcrEngineKind.PADDLE):
            pytest.skip("本机没有可用的 PaddleOCR 开发环境")
        assert await asyncio.wait_for(ocr.wait_for_state(WorkerState.FAILED), 60) is WorkerState.FAILED

        def paddle_state() -> tuple[OcrEngineState, OcrEngineErrorCode | None, str]:
            catalog = bus.latest(OcrEngineCatalog)
            info = catalog.engine(OcrEngineKind.PADDLE) if catalog is not None else None
            assert info is not None
            return info.state, info.error_code, info.detail

        await bus.drain()
        state, code, detail = paddle_state()
        assert state is OcrEngineState.ERROR and code is OcrEngineErrorCode.RUNTIME_MISMATCH
        assert "DEPENDENCY_ERROR" in detail
        bus.send(UpdateConfig({"ui": {"theme": "dark"}}))
        await asyncio.sleep(1.0)
        await bus.drain()
        assert ocr.state is WorkerState.FAILED, "无关的设置变化不应重新启动确定性故障的进程"
        assert paddle_state()[0] is OcrEngineState.ERROR
    finally:
        await app.stop()


def _storage_app(qapp: QApplication, tmp_path: Path, network: _Network) -> Application:
    data = tmp_path / "data"
    return Application(
        AppOptions(
            paths=AppPaths(data),
            secrets=MemorySecretStore(),
            nmt_command=NMT_FAKE,
            ocr_command=lambda _engine: OCR_FAKE,
            tray_icon=None,
            http_transport=httpx.MockTransport(network.handle),
            models_index_url=INDEX_URL,
            dict_index_url=DICT_INDEX_URL,
            data=DataDirChoice(data, DataDirSource.CHOSEN, tmp_path / "roaming" / "location.json"),
        ),
        qapp,
    )


async def _settled(app: Application, predicate: Callable[[StorageLocations], bool]) -> StorageLocations:
    assert app.bus is not None
    while True:
        state = app.bus.latest(StorageLocations)
        if state is not None and state.task is None and predicate(state):
            return state
        await asyncio.sleep(0.05)


@pytest.mark.timeout(120)
async def test_moving_the_models_dir_at_runtime(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """设置里更改模型目录并移动现有模型：NMT 与 OCR 进程停下再按新目录启动，模型管理、OCR 引擎、显卡运行库
    模块与字典引擎都换到新目录，配置写入 [storage].models_dir，不需要重启程序（需求 MOD-04、CFG-07）。"""
    # 源码版在模型目录里没有模型时回退到仓库的 assets 目录；这里检查的是新目录本身，关掉回退。
    monkeypatch.setattr(bootstrap, "LEGACY_NMT_MODELS", tmp_path / "no-legacy")
    monkeypatch.setattr(bootstrap, "LEGACY_OCR_MODELS", tmp_path / "no-legacy")
    app = _storage_app(qapp, tmp_path, _Network())
    await app.start()
    try:
        bus = app.bus
        assert bus is not None and app.nmt is not None and app.ocr is not None
        assert await asyncio.wait_for(app.nmt.wait_for_state(WorkerState.READY, WorkerState.FAILED), 60) is (
            WorkerState.READY
        )
        old_root = tmp_path / "data" / "models"
        (old_root / "dict" / "dict-en-ja.tsv.gz").write_bytes(b"not really a pack")
        (old_root / "engines" / "rapid").mkdir(parents=True)
        (old_root / "engines" / "rapid" / "installed.json").write_text("{}", encoding="utf-8")
        old_pid, old_ocr_engines, old_models = app.nmt.pid, app.ocr_engines, app.models

        new_root = tmp_path / "second-drive" / "LST models"
        bus.send(ChangeModelsDir(str(new_root), move=True))
        state = await asyncio.wait_for(_settled(app, lambda s: s.models_dir == str(new_root)), 60)

        assert state.failure is None and not state.models_default
        assert (new_root / "dict" / "dict-en-ja.tsv.gz").is_file()
        assert (new_root / "engines" / "rapid" / "installed.json").is_file()
        assert not (old_root / "dict").exists() and not (old_root / "engines").exists()
        config = tomllib.loads((tmp_path / "data" / "config" / "settings.toml").read_text(encoding="utf-8"))
        assert config["storage"]["models_dir"] == str(new_root)
        assert app.models is not old_models and app.models is not None
        assert app.models.models_dir == new_root / "nmt" and app.models.dict_dir == new_root / "dict"
        assert app.ocr_engines is not old_ocr_engines
        assert app.nmt_runtime is not None and app.nmt_runtime.root == new_root / "runtimes" / "cublas12"
        assert app.engines is not None and app.engines.dict_dir == new_root / "dict"
        catalog = bus.latest(OcrEngineCatalog)
        assert catalog is not None

        nmt = app.nmt

        async def nmt_restarted() -> None:
            while nmt.pid in (None, old_pid) or nmt.state is not WorkerState.READY:
                await asyncio.sleep(0.05)

        await asyncio.wait_for(nmt_restarted(), 60)
        assert await asyncio.wait_for(app.ocr.wait_for_state(WorkerState.READY, WorkerState.FAILED), 60) is (
            WorkerState.READY
        )
        # 停止时三个新模块按启动的逆序停止，旧模块不在启动记录里。
        started = [module for module, _ in app._started]  # pyright: ignore[reportPrivateUsage]
        assert app.models in started and old_models not in started and old_ocr_engines not in started
    finally:
        await app.stop()


@pytest.mark.timeout(120)
async def test_changing_the_data_dir_copies_then_restarts(qapp: QApplication, tmp_path: Path) -> None:
    """设置里更改数据目录：在后台复制现有数据（缓存库正被使用），写位置文件，重启前继续用旧目录；
    RestartApp 让组合根退出并记下“退出后重新启动”（需求 CFG-06）。"""
    app = _storage_app(qapp, tmp_path, _Network())
    await app.start()
    try:
        bus = app.bus
        assert bus is not None
        target = tmp_path / "new data"
        bus.send(ChangeDataDir(str(target), copy=True))
        state = await asyncio.wait_for(_settled(app, lambda s: s.pending_data_dir is not None), 60)
        assert state.failure is None and state.pending_data_dir == str(target)
        assert state.data_dir == str(tmp_path / "data")
        location = json.loads((tmp_path / "roaming" / "location.json").read_text(encoding="utf-8"))
        assert location == {"data_dir": str(target)}
        assert (target / "config" / "settings.toml").is_file()
        assert (target / "cache.sqlite3").is_file() and (target / "stats.sqlite3").is_file()
        assert not (target / "tmp").exists() and not (target / "lsttranslate.lock").exists()

        assert not app.restart_requested
        bus.send(RestartApp())
        await asyncio.wait_for(app.wait_closed(), 5)
        assert app.restart_requested
    finally:
        await app.stop()
