"""用自动化接口驱动完整的截屏翻译：真实组合根、真实 Windows OCR 子进程与真实 NMT 子进程
（需求 DEV-05、DEV-06）。

环境与 test_full_interaction.py 相同（虚构的两屏布局、白底黑字的英文冻结画面、Argos 英中模型、
Windows OCR 分别使用英文与自动语言），
区别是交互全部经过命名管道上的自动化接口，不直接调用窗口与总线：

status → capture（mask，真实蒙版，模拟拖动）→ wait_result（两段英文、含汉字的译文）→ overlay.select 与 copy
→ 改“切换原文与译文”的键位并按新键位验证 → overlay.key 按当前键位关闭 → 再截一次并用蒙版上的取消键位取消。

跳过条件与 test_full_interaction.py 相同。
"""

import asyncio
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from PySide6.QtWidgets import QApplication

import lst.app.bootstrap as bootstrap
from lst.app.bootstrap import Application, AppOptions
from lst.app.paths import AppPaths
from lst.automation import AutomationClient
from lst.automation.winsec import normalize_pipe_name
from lst.contracts.automation import BuildFlavor
from lst.contracts.ports import WorkerState
from lst.services.config import MemorySecretStore
from lst.ui.capture import CaptureModule
from lst.ui.overlay import OverlayModule
from lst.workers.ocr_worker.winocr_backend import match_language
from tests.integration.screenshot.test_full_interaction import (
    DICT_INDEX_URL,
    INDEX_URL,
    LEFT,
    LEFT_LINES,
    MAIN,
    MAIN_LINES,
    SENTENCES,
    TextScreens,
    _windows_ocr_languages,  # pyright: ignore[reportPrivateUsage]
    _words,  # pyright: ignore[reportPrivateUsage]
    _write_config,  # pyright: ignore[reportPrivateUsage]
)
from tests.ui.capture.conftest import hide_from_desktop
from tests.workers.ocr.screens import FONT_FILE, ensure_font
from tests.workers.packages import REAL_EN_ZH

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="自动化接口只在 Windows 上实现"),
    pytest.mark.skipif(not REAL_EN_ZH.is_file(), reason="本机没有 assets/argosmodel 下的 Argos 英中模型包"),
    pytest.mark.skipif(
        match_language("en", _windows_ocr_languages()) is None,
        reason="Windows 没有英文 OCR 语言（或缺少 pywinrt），Windows OCR 无法识别英文截图",
    ),
]

_HAN = re.compile(r"[一-鿿]")


@pytest.mark.timeout(300)
@pytest.mark.parametrize("language", ["en", "auto"])
async def test_screenshot_translation_driven_through_the_interface(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    if not ensure_font():
        pytest.skip(f"没有用来画英文冻结画面的字体文件 {FONT_FILE}")
    screens = TextScreens([MAIN, LEFT], {"left": LEFT_LINES, "main": MAIN_LINES})
    copied: list[str] = []
    monkeypatch.setattr(
        bootstrap, "CaptureModule", lambda: CaptureModule(freezer=screens, prepare_window=hide_from_desktop)
    )
    monkeypatch.setattr(
        bootstrap,
        "OverlayModule",
        lambda: OverlayModule(clipboard=copied.append, prepare_window=hide_from_desktop),
    )
    paths = AppPaths(tmp_path / "data")
    _write_config(paths, language=language)
    pipe = normalize_pipe_name(f"lst-test-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    requests: list[str] = []

    def network(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404)

    app = Application(
        AppOptions(
            paths=paths,
            secrets=MemorySecretStore(),
            ocr_command="auto",
            tray_icon=None,
            updates_startup_delay=3600,
            http_transport=httpx.MockTransport(network),
            models_index_url=INDEX_URL,
            dict_index_url=DICT_INDEX_URL,
            flavor=BuildFlavor.DEV,
            automation_pipe=pipe,
        ),
        qapp,
    )
    await app.start()
    client: AutomationClient | None = None
    try:
        assert app.nmt is not None and app.ocr is not None
        assert await asyncio.wait_for(app.ocr.wait_for_state(WorkerState.READY, WorkerState.FAILED), 90) is (
            WorkerState.READY
        ), app.ocr.state_detail
        assert await asyncio.wait_for(app.nmt.wait_for_state(WorkerState.READY, WorkerState.FAILED), 120) is (
            WorkerState.READY
        ), app.nmt.state_detail
        client = await asyncio.to_thread(AutomationClient.connect, pipe, timeout=5)
        connection = client

        async def call(method: str, params: dict[str, Any] | None = None) -> Any:
            return await asyncio.to_thread(connection.call, method, params)

        # 名册里出现支持英→中的本地 NMT 实例后再截屏。
        status: Any = {}
        for _ in range(300):
            status = await call("status")
            if any(e["id"] == "argos" and e["health"] in ("ready", "busy") for e in status["engines"]):
                break
            await asyncio.sleep(0.1)
        assert status["flavor"] == "dev" and status["workers"]["ocr"]["versions"]["engine"] == "windows"

        # ---- 截屏：在 150% 的左屏上框出两行英文。原生坐标 = 左屏原点 + 冻结画面里的像素
        rect = [-2560 + 90, -200 + 210, 1260, 360]
        started = await call("capture", {"rect": rect, "mode": "mask"})
        assert started["session_id"] and started["binding"] == "LeftDrag"
        result = await call("wait_result", {"session_id": started["session_id"], "timeout": 120})
        assert result["phase"] == "done", result
        assert result["capture"]["selection"] == [-2500, -60, 840, 240]
        sources = [s["source"] for s in result["segments"]]
        assert [_words(t) for t in sources] == [_words(s) for s in SENTENCES], sources
        translations = [s["display"] for s in result["segments"]]
        assert all(_HAN.search(t) for t in translations), translations
        assert all(s["shown_engine"] == "argos" and s["finished"] for s in result["segments"])
        attempt = next(a for a in result["segments"][0]["attempts"] if a["engine"] == "argos")
        assert attempt["state"] == "done" and attempt["latency_ms"] is not None
        overlay = result["overlay"]
        assert overlay["phase"] == "result" and overlay["progress"] == [2, 2]
        assert [item["shown"] for item in overlay["items"]] == translations

        # ---- 选字与复制
        selected = await call("overlay.select", {"start": [0, 1], "end": [1, 2]})
        expected = translations[0][1:] + "\n" + translations[1][:2]
        assert selected["selection"]["text"] == expected
        assert (await call("overlay.copy", {"clipboard": True}))["text"] == expected
        assert copied == [expected]

        # ---- 键位：切换原文与译文改成 Ctrl+T，按新键位生效、旧键位失效
        assert (await call("bindings.set", {"action": "overlay.toggle_source", "bindings": ["Ctrl+T"]}))[
            "changed"
        ]
        assert (await call("overlay.mouse", {"binding": "RightClick"}))["showing"] == "translation"
        toggled = await call("overlay.key", {"action": "overlay.toggle_source"})
        assert toggled["sent"] == "Ctrl+T" and toggled["showing"] == "source"
        assert [item["shown"] for item in toggled["items"]] == sources
        closed = await call("overlay.key", {"action": "overlay.close"})
        assert closed["sent"] == "Esc" and closed["open"] is False

        # ---- 再打开蒙版，用“取消截屏”的键位取消：没有新会话
        held = await call(
            "capture", {"rect": [-2560 + 300, -200 + 300, 200, 200], "mode": "mask", "hold": True}
        )
        assert held["held"]
        cancelled = await call("capture.mouse", {"action": "capture.cancel", "at": [-2560 + 500, -200 + 500]})
        assert cancelled["sent"] == "RightClick" and cancelled["active"] is False
        assert cancelled["session_id"] is None
        assert requests == []
    finally:
        if client is not None:
            client.close()
        await app.stop()
