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
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from lst.app.bootstrap import Application, AppOptions
from lst.app.paths import AppPaths
from lst.contracts.config import UpdateConfig
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
from lst.contracts.ports import WorkerState
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
            ocr_command=OCR_FAKE,
            tray_icon=None,
            http_transport=httpx.MockTransport(network.handle),
            models_index_url=INDEX_URL,
        ),
        qapp,
    )
    await app.start()
    try:
        bus = app.bus
        assert bus is not None and app.nmt is not None

        # 配置文件自动生成，默认引擎是本地 NMT。
        config_file = tmp_path / "data" / "config" / "settings.toml"
        assert config_file.is_file()
        assert [e["kind"] for e in tomllib.loads(config_file.read_text(encoding="utf-8"))["engines"]] == [
            "local_nmt"
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
        bus.send(TranslateText(SessionId("s1"), "hello world", Lang("en"), Lang("zh-Hans")))

        async def translated() -> str:
            while True:
                for update in updates:
                    for view in update.views:
                        if view.finished and view.winner == EngineId("argos"):
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
        assert network.requests == [INDEX_URL]

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
            ocr_command=OCR_FAKE,
            tray_icon=None,
            http_transport=httpx.MockTransport(network.handle),
            models_index_url=INDEX_URL,
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
