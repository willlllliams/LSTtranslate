"""首次运行引导测试的公共夹具与构造函数。

Qt 平台在本文件导入时设为 offscreen（环境里已经指定的不覆盖），QApplication 在之后才创建，
窗口不会出现在桌面上。
事件总线是真实的（raise_errors=True），模型管理模块由命令记录扮演：引导发出的三个命令各登记一个处理者。
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lst.contracts.config import AppConfig
from lst.contracts.ids import Lang
from lst.contracts.models import (
    CancelModelDownload,
    DownloadModels,
    DownloadProgress,
    DownloadState,
    ModelCatalog,
    ModelErrorCode,
    ModelInfo,
    ModelKind,
    RefreshModelCatalog,
    nmt_model_id,
)
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus
from lst.ui import i18n

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

EN_ZH = nmt_model_id("en", "zh-Hans")
ZH_EN = nmt_model_id("zh-Hans", "en")
MB = 1024 * 1024


# ---------------------------------------------------------------- 消息构造


def info(
    source: str,
    target: str,
    *,
    installed: bool = False,
    size: int | None = 90 * MB,
    version: str = "1.9",
    installed_version: str | None = None,
) -> ModelInfo:
    return ModelInfo(
        id=nmt_model_id(source, target),
        kind=ModelKind.NMT,
        source=Lang(source),
        target=Lang(target),
        version=version,
        size_bytes=size,
        installed=installed,
        url=f"https://packages.invalid/{source}-{target}.argosmodel",
        installed_version=(installed_version or version) if installed else None,
    )


def catalog(
    *models: ModelInfo,
    downloads: Mapping[str, DownloadProgress] | None = None,
    index_loaded: bool = True,
    refreshing: bool = False,
    error: tuple[ModelErrorCode, str] | None = None,
) -> ModelCatalog:
    return ModelCatalog(
        models=models,
        downloads=dict(downloads or {}),
        index_loaded=index_loaded,
        refreshing=refreshing,
        index_error=error[1] if error else None,
        index_error_code=error[0] if error else None,
    )


def zh_en_catalog(**kwargs: Any) -> ModelCatalog:
    """zh-Hans ↔ en 两个方向都可以下载，另有一个无关的语言对。"""
    return catalog(
        info("en", "zh-Hans", size=100 * MB), info("zh-Hans", "en", size=50 * MB), info("en", "de"), **kwargs
    )


def progress(
    state: DownloadState,
    done: int = 0,
    total: int | None = None,
    *,
    code: ModelErrorCode | None = None,
    error: str | None = None,
) -> DownloadProgress:
    return DownloadProgress(state, done, total, error=error, error_code=code)


def app_config(pair: Sequence[JsonValue] | None = None, *, revision: int = 1) -> AppConfig:
    sections: dict[str, JsonValue] = {}
    if pair is not None:
        sections["bilingual"] = {"pair": list(pair)}
    return AppConfig(sections=sections, revision=revision)


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def zh_language() -> Iterator[None]:
    previous = i18n.current_language()
    i18n.set_language("zh-Hans")
    yield
    i18n.set_language(previous)


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    """真实的事件总线。同步处理函数的异常由这里收集，测试结束时有记录就判失败。"""
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    instance = EventBus(loop, logger=logging.getLogger("tests.onboarding.bus"), raise_errors=True)
    try:
        yield instance
        await instance.drain()
    finally:
        await instance.aclose()
        loop.set_exception_handler(None)
    assert not errors, [context.get("exception") or context.get("message") for context in errors]


@dataclass
class Commands:
    """引导发出的模型命令。"""

    refresh: list[RefreshModelCatalog] = field(default_factory=list[RefreshModelCatalog])
    download: list[DownloadModels] = field(default_factory=list[DownloadModels])
    cancel: list[CancelModelDownload] = field(default_factory=list[CancelModelDownload])


@pytest.fixture
async def commands(bus: EventBus) -> Commands:
    record = Commands()
    bus.subscribe(RefreshModelCatalog, record.refresh.append)
    bus.subscribe(DownloadModels, record.download.append)
    bus.subscribe(CancelModelDownload, record.cancel.append)
    return record


@pytest.fixture
def settings_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "ui_state.ini"
