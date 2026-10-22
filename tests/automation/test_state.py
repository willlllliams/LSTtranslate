"""调参所需的配置回执和识别重试状态，使用真实配置服务。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel

from lst.app.automation.methods import AutomationDeps, AutomationMethods
from lst.app.automation.recorder import SessionRecorder, describe_record
from lst.app.automation.server import MethodError
from lst.app.paths import AppPaths
from lst.automation.protocol import ErrorCode
from lst.contracts.automation import BuildFlavor
from lst.contracts.errors import ErrorCode as OcrErrorCode
from lst.contracts.ids import SessionId
from lst.contracts.ocr import OcrCompleted, OcrFailed, OcrStarted
from lst.kernel import EventBus, ModuleContext
from lst.services.config import ConfigService, MemorySecretStore


class Options(BaseModel):
    value: Literal["old", "new"] = "old"


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[AutomationMethods]:
    bus = EventBus(asyncio.get_running_loop())
    service = ConfigService(
        tmp_path / "settings.toml", sections={"a": Options, "b": Options}, secrets=MemorySecretStore()
    )
    ctx = ModuleContext(bus, logging.getLogger("test.config"))
    await service.start(ctx)
    deps = AutomationDeps(BuildFlavor.DEV, "test", False, lambda: AppPaths(tmp_path))
    try:
        yield AutomationMethods(bus, deps, SessionRecorder(), lambda: None)
    finally:
        await bus.drain()
        await service.stop()
        ctx.close()
        await bus.aclose()


async def test_concurrent_changes_return_their_own_applied_snapshots(api: AutomationMethods) -> None:
    first, second = await asyncio.gather(
        api.update_config("a", {"value": "new"}, 2),
        api.update_config("b", {"value": "new"}, 2),
    )
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first["values"] == second["values"] == {"value": "new"}
    assert first["revision"] == 2 and second["revision"] == 3


async def test_rejection_is_not_satisfied_by_another_change(api: AutomationMethods) -> None:
    changed, rejected = await asyncio.gather(
        api.update_config("a", {"value": "new"}, 2),
        api.update_config("b", {"value": "invalid"}, 2),
        return_exceptions=True,
    )
    assert isinstance(changed, dict) and changed["changed"] is True
    assert isinstance(rejected, MethodError) and rejected.code is ErrorCode.REJECTED


async def test_repeated_change_is_acknowledged_without_waiting_for_timeout(api: AutomationMethods) -> None:
    first, second = await asyncio.gather(
        api.update_config("a", {"value": "new"}, 2),
        api.update_config("a", {"value": "new"}, 2),
    )
    assert isinstance(first, dict) and first["changed"] is True
    assert isinstance(second, dict) and second["changed"] is False and second["revision"] == 2


def test_retry_clears_failed_and_settled_state() -> None:
    recorder = SessionRecorder()
    recorder.enabled = True
    session = SessionId("retry")
    recorder.on_ocr_failed(OcrFailed(session, OcrErrorCode.INTERNAL, "first attempt failed"))
    record = recorder.get(session)
    assert record is not None and record.settled and record.settled_at is not None
    recorder.on_ocr_started(OcrStarted(session))
    assert record.phase == "recognizing" and not record.settled and record.settled_at is None
    recorder.on_ocr_completed(OcrCompleted(session, (), ()))
    assert record.phase == "done" and describe_record(record)["error"] is None


def test_success_replaces_an_older_failure() -> None:
    recorder = SessionRecorder()
    recorder.enabled = True
    session = SessionId("retry")
    recorder.on_ocr_failed(OcrFailed(session, OcrErrorCode.INTERNAL, "first attempt failed"))
    recorder.on_ocr_completed(OcrCompleted(session, (), ()))
    record = recorder.get(session)
    assert record is not None and record.phase == "done" and record.failure is None
