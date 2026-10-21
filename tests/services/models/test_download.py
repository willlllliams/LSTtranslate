"""ModelsModule 的下载：进度节流、原子改名、校验失败、取消、重试、不重复下载、不阻塞事件循环、日志脱敏。

下载内容是合成的小模型包，由 MockTransport 扮演的假服务器分块流式返回，测试不访问任何真实网络。
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import BinaryIO, cast

import httpx
import pytest

from lst.contracts.ids import Lang
from lst.contracts.models import (
    CancelModelDownload,
    DownloadModels,
    DownloadState,
    ModelErrorCode,
    ModelsInstalled,
    RefreshModelCatalog,
)
from lst.kernel import EventBus
from lst.services.models import ModelsModule
from lst.services.models import module as models_module
from lst.services.models.argos import PackageMeta
from tests.services.models.conftest import Published, StartModule, wait_until
from tests.services.models.helpers import (
    DE_EN,
    EN_DE,
    EN_ZH,
    INDEX_URL,
    ZH_EN,
    FakeServer,
    Route,
    index_item,
    package_bytes,
    package_url,
    write_package,
)

D = DownloadState


async def start_with_index(
    bus: EventBus, start_module: StartModule, server: FakeServer, items: list[dict[str, object]]
) -> ModelsModule:
    """放好索引，启动模块并获取一次索引。"""
    server.add_index(items)
    module = await start_module()
    bus.send(RefreshModelCatalog())
    await bus.drain()
    return module


def files(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []


async def test_download_installs_package_atomically(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, models_dir: Path
) -> None:
    gate = asyncio.Event()
    route = server.add_package("en", "zh", "1.9", gate=gate)
    await start_with_index(bus, start_module, server, [index_item("en", "zh", "1.9")])

    bus.send(DownloadModels((EN_ZH,)))
    await asyncio.wait_for(route.reached.wait(), 5)
    # 下载期间只有临时文件，NMT 进程扫描不到半个模型包。
    assert files(models_dir) == ["translate-en_zh-1_9.part"]
    gate.set()
    await bus.drain()

    assert files(models_dir) == ["translate-en_zh-1_9.argosmodel"]
    assert published.states(EN_ZH) == [D.QUEUED, D.DOWNLOADING, D.VERIFYING, D.DONE]
    progress = published.progress(EN_ZH)
    assert progress is not None and progress.done_bytes == progress.total_bytes == len(route.body)
    model = published.catalog.model(EN_ZH)
    assert model is not None and model.installed and model.installed_version == "1.9"
    assert published.installed == [ModelsInstalled((EN_ZH,))]


async def test_progress_is_throttled(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer
) -> None:
    clock = [0.0]

    def advance() -> None:
        clock[0] += 0.05  # 每块数据之间过去 50 毫秒

    body = package_bytes("en", "de", payload=os.urandom(80 * 1024))
    server.add(package_url("en", "de"), Route(body=body, chunk_size=1024, on_chunk=advance))
    server.add_index([index_item("en", "de")])
    await start_module(clock=lambda: clock[0], write_chunk_bytes=8 * 1024)
    bus.send(RefreshModelCatalog())
    await bus.drain()
    bus.send(DownloadModels((EN_DE,)))
    await bus.drain()

    chunks = -(-len(body) // 1024)
    downloading = [
        progress
        for catalog in published.catalogs
        if (progress := catalog.downloads.get(EN_DE)) is not None and progress.state is D.DOWNLOADING
    ]
    # 不节流时每块发布一次（约 90 次）；按 200 毫秒节流，模拟的 4.5 秒内大约发布 22 次，另加开始时的两次。
    expected = chunks * 0.05 / 0.2
    assert expected - 3 <= len(downloading) <= expected + 3, (len(downloading), chunks)
    done = [progress.done_bytes for progress in downloading]
    assert done == sorted(done)
    assert all(progress.total_bytes == len(body) for progress in downloading[1:])
    assert published.states(EN_DE)[-1] is D.DONE


@pytest.mark.parametrize(
    ("body", "code", "fragment"),
    [
        (b"this is not a zip archive at all", ModelErrorCode.INVALID_PACKAGE, "zip"),
        (package_bytes("en", "fr"), ModelErrorCode.PAIR_MISMATCH, "en→fr"),
        (package_bytes("en", "de", sentencepiece=False), ModelErrorCode.INVALID_PACKAGE, "sentencepiece"),
    ],
)
async def test_verification_failure_removes_temporary_file(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    models_dir: Path,
    body: bytes,
    code: ModelErrorCode,
    fragment: str,
) -> None:
    server.add_package("en", "de", body=body)
    await start_with_index(bus, start_module, server, [index_item("en", "de")])
    bus.send(DownloadModels((EN_DE,)))
    await bus.drain()

    progress = published.progress(EN_DE)
    assert progress is not None and progress.state is D.FAILED
    assert progress.error_code is code
    assert progress.error is not None and fragment in progress.error
    assert files(models_dir) == []
    assert published.installed == []
    model = published.catalog.model(EN_DE)
    assert model is not None and not model.installed


@pytest.mark.parametrize(
    ("route", "code", "fragment"),
    [
        (Route(status=404, body=b"missing"), ModelErrorCode.HTTP, "HTTP 404"),
        (Route(error=httpx.ConnectError("connection reset")), ModelErrorCode.NETWORK, "ConnectError"),
        (Route(error=httpx.ConnectTimeout("timed out")), ModelErrorCode.TIMEOUT, "ConnectTimeout"),
    ],
)
async def test_network_failure(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    models_dir: Path,
    route: Route,
    code: ModelErrorCode,
    fragment: str,
) -> None:
    server.add(package_url("en", "de"), route)
    await start_with_index(bus, start_module, server, [index_item("en", "de")])
    bus.send(DownloadModels((EN_DE,)))
    await bus.drain()

    progress = published.progress(EN_DE)
    assert progress is not None and progress.state is D.FAILED and progress.error_code is code
    assert progress.error is not None and fragment in progress.error
    assert files(models_dir) == []


async def test_retry_after_failure(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, models_dir: Path
) -> None:
    server.add(package_url("en", "de"), Route(status=500, body=b"oops"))
    await start_with_index(bus, start_module, server, [index_item("en", "de")])
    bus.send(DownloadModels((EN_DE,)))
    await bus.drain()
    progress = published.progress(EN_DE)
    assert progress is not None and progress.state is D.FAILED

    server.add_package("en", "de")
    bus.send(DownloadModels((EN_DE,)))
    await bus.drain()
    assert published.states(EN_DE) == [
        D.QUEUED,
        D.DOWNLOADING,
        D.FAILED,
        D.QUEUED,
        D.DOWNLOADING,
        D.VERIFYING,
        D.DONE,
    ]
    assert files(models_dir) == ["translate-en_de-1_9.argosmodel"]
    assert published.installed == [ModelsInstalled((EN_DE,))]


async def test_cancel_running_download_continues_with_queue(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, models_dir: Path
) -> None:
    gate = asyncio.Event()
    first = server.add_package("en", "de", gate=gate)
    server.add_package("de", "en")
    await start_with_index(bus, start_module, server, [index_item("en", "de"), index_item("de", "en")])

    bus.send(DownloadModels((EN_DE, DE_EN)))
    await asyncio.wait_for(first.reached.wait(), 5)
    assert published.catalog.downloads[DE_EN].state is D.QUEUED
    bus.send(CancelModelDownload(EN_DE))
    await bus.drain()

    assert published.states(EN_DE) == [D.QUEUED, D.DOWNLOADING, D.CANCELLED]
    assert published.states(DE_EN)[-1] is D.DONE
    assert files(models_dir) == ["translate-de_en-1_9.argosmodel"]
    assert published.installed == [ModelsInstalled((DE_EN,))]


async def test_cancel_queued_download_never_requests_it(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, models_dir: Path
) -> None:
    gate = asyncio.Event()
    first = server.add_package("en", "de", gate=gate)
    server.add_package("de", "en")
    await start_with_index(bus, start_module, server, [index_item("en", "de"), index_item("de", "en")])

    bus.send(DownloadModels((EN_DE, DE_EN)))
    await asyncio.wait_for(first.reached.wait(), 5)
    bus.send(CancelModelDownload(DE_EN))
    await wait_until(lambda: published.catalog.downloads[DE_EN].state is D.CANCELLED)
    gate.set()
    await bus.drain()

    assert published.states(DE_EN) == [D.QUEUED, D.CANCELLED]
    assert published.states(EN_DE)[-1] is D.DONE
    assert server.requested("translate-de_en") == 0
    assert files(models_dir) == ["translate-en_de-1_9.argosmodel"]

    # 取消之后可以重新下载。
    bus.send(DownloadModels((DE_EN,)))
    await bus.drain()
    assert published.states(DE_EN)[-1] is D.DONE


async def test_same_or_higher_installed_version_is_not_downloaded_again(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, models_dir: Path
) -> None:
    write_package(models_dir, "en", "zh", version="1.9")
    write_package(models_dir, "zh", "en", version="2.0")
    server.add_package("en", "zh", "1.9")
    server.add_package("zh", "en", "1.9")
    await start_with_index(
        bus, start_module, server, [index_item("en", "zh", "1.9"), index_item("zh", "en", "1.9")]
    )

    bus.send(DownloadModels((EN_ZH, ZH_EN)))
    await bus.drain()
    assert server.requested(".argosmodel") == 0
    assert published.catalog.downloads == {}
    assert published.installed == []


async def test_newer_version_replaces_old_package(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, models_dir: Path
) -> None:
    old = write_package(models_dir, "en", "zh", version="1.0")
    server.add_package("en", "zh", "1.9")
    await start_with_index(bus, start_module, server, [index_item("en", "zh", "1.9")])
    model = published.catalog.model(EN_ZH)
    assert model is not None and model.update_available

    bus.send(DownloadModels((EN_ZH,)))
    await bus.drain()
    assert not old.exists()
    assert files(models_dir) == ["translate-en_zh-1_9.argosmodel"]
    model = published.catalog.model(EN_ZH)
    assert model is not None and model.installed_version == "1.9" and not model.update_available


async def test_unknown_model_fails_as_unavailable(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer
) -> None:
    await start_module()
    bus.send(DownloadModels((EN_DE,)))  # 还没有获取索引
    await bus.drain()
    progress = published.progress(EN_DE)
    assert progress is not None and progress.state is D.FAILED
    assert progress.error_code is ModelErrorCode.UNAVAILABLE
    assert server.requests == []


async def test_duplicate_request_downloads_once(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer
) -> None:
    gate = asyncio.Event()
    route = server.add_package("en", "de", gate=gate)
    await start_with_index(bus, start_module, server, [index_item("en", "de")])
    bus.send(DownloadModels((EN_DE, EN_DE)))
    await asyncio.wait_for(route.reached.wait(), 5)
    bus.send(DownloadModels((EN_DE,)))
    await asyncio.sleep(0.05)
    gate.set()
    await bus.drain()
    assert server.requested("translate-en_de") == 1
    assert published.installed == [ModelsInstalled((EN_DE,))]


async def test_stop_during_download_removes_temporary_file(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, models_dir: Path
) -> None:
    gate = asyncio.Event()
    route = server.add_package("en", "de", gate=gate)
    module = await start_with_index(bus, start_module, server, [index_item("en", "de")])
    bus.send(DownloadModels((EN_DE,)))
    await asyncio.wait_for(route.reached.wait(), 5)
    assert files(models_dir) == ["translate-en_de-1_9.part"]
    count = len(published.catalogs)

    await module.stop()
    await bus.drain()
    assert files(models_dir) == []
    assert len(published.catalogs) == count, "停止之后不再发布模型目录"
    assert published.installed == []


class SlowSink:
    """包装真实的文件对象，每次写入先睡 SLOW_WRITE_S 秒，模拟慢磁盘。"""

    def __init__(self, inner: BinaryIO) -> None:
        self._inner = inner
        self.writes = 0

    def write(self, data: bytes) -> int:
        time.sleep(SLOW_WRITE_S)
        self.writes += 1
        return self._inner.write(data)

    def close(self) -> None:
        self._inner.close()


SLOW_WRITE_S = 0.05
SLOW_VERIFY_S = 0.3


async def test_download_does_not_block_event_loop(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    models_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """下载约 8 MB 的包，同时让写盘和校验都变慢：它们若在事件循环线程上执行，计时协程会停顿数百毫秒。"""
    payload = os.urandom(8 * 1024 * 1024)  # 随机内容，ZIP_STORED 存放
    body = package_bytes("en", "de", payload=payload)
    server.add(package_url("en", "de"), Route(body=body, chunk_size=64 * 1024))
    sinks: list[SlowSink] = []
    real_open, real_verify = models_module._open_for_write, models_module.verify_download

    def slow_open(path: Path) -> BinaryIO:
        sink = SlowSink(real_open(path))
        sinks.append(sink)
        return cast(BinaryIO, sink)

    def slow_verify(path: Path, expected: tuple[Lang, Lang]) -> PackageMeta:
        time.sleep(SLOW_VERIFY_S)
        return real_verify(path, expected)

    monkeypatch.setattr(models_module, "_open_for_write", slow_open)
    monkeypatch.setattr(models_module, "verify_download", slow_verify)
    await start_with_index(bus, start_module, server, [index_item("en", "de")])

    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    tick_task = asyncio.create_task(ticker())
    started = time.perf_counter()
    bus.send(DownloadModels((EN_DE,)))
    await bus.drain()
    elapsed = time.perf_counter() - started
    stop.set()
    await tick_task

    assert published.states(EN_DE)[-1] is D.DONE
    assert files(models_dir) == ["translate-en_de-1_9.argosmodel"]
    assert sinks and sinks[0].writes >= 8  # 每攒够 1 MB 写一次
    assert elapsed >= SLOW_VERIFY_S + 8 * SLOW_WRITE_S
    # 下载持续了 0.7 秒以上，计时协程一直在运转：最长停顿远小于一次慢写入或慢校验。
    assert max(gaps) < SLOW_WRITE_S, f"事件循环最长停顿 {max(gaps) * 1000:.0f} 毫秒"


async def test_logs_and_errors_never_contain_url_query(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "SIGNATURE-SECRET-42"
    index_url = f"{INDEX_URL}?token={secret}"
    ok_url = package_url("en", "de", query=f"X-Amz-Signature={secret}")
    bad_url = package_url("de", "en", query=f"X-Amz-Signature={secret}")
    server.add(package_url("en", "de"), Route(body=package_bytes("en", "de")))
    server.add(
        package_url("de", "en"),
        Route(error=httpx.ConnectError(f"failed to connect to {bad_url}")),
    )
    caplog.set_level(logging.DEBUG, logger="lst")
    server.add(INDEX_URL, Route(error=httpx.ConnectError(f"cannot reach {index_url}")))
    await start_module(index_url=index_url)
    bus.send(RefreshModelCatalog())
    await bus.drain()
    first_error = published.catalog.index_error
    assert first_error is not None and "index.invalid" in first_error and secret not in first_error

    server.add_index([index_item("en", "de", url=ok_url), index_item("de", "en", url=bad_url)])
    bus.send(RefreshModelCatalog())
    await bus.drain()
    bus.send(DownloadModels((EN_DE, DE_EN)))
    await bus.drain()

    assert published.states(EN_DE)[-1] is D.DONE
    failed = published.progress(DE_EN)
    assert failed is not None and failed.state is D.FAILED and failed.error is not None
    assert secret not in failed.error
    # 请求本身确实带着查询参数，只是日志和错误详情里没有。
    assert any(secret in url for url in server.requests)

    messages = [record.getMessage() for record in caplog.records if record.name.startswith("lst")]
    assert any("packages.invalid/translate-en_de-1_9.argosmodel" in text for text in messages)
    assert not [text for text in messages if secret in text]
    # 记录里的异常文本同样不含查询参数。
    assert not [r for r in caplog.records if r.exc_text and secret in r.exc_text]
