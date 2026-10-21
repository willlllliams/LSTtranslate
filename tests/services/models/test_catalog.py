"""ModelsModule 的模型目录：启动扫描、获取索引、合并、失败处理与删除。

网络全部由 MockTransport 扮演的假服务器提供；事件总线是真实的（raise_errors=True）。
"""

import logging
from pathlib import Path

import httpx
import pytest

from lst.contracts.models import DeleteModel, ModelErrorCode, ModelsInstalled, RefreshModelCatalog
from lst.kernel import EventBus
from tests.services.models.conftest import MODULE_LOGGER, Published, StartModule
from tests.services.models.helpers import (
    DE_EN,
    EN_DE,
    EN_ZH,
    INDEX_URL,
    ZH_EN,
    FakeServer,
    Route,
    index_item,
    package_url,
    write_package,
)


async def refresh(bus: EventBus) -> None:
    bus.send(RefreshModelCatalog())
    await bus.drain()


async def test_start_publishes_installed_models_without_network(
    start_module: StartModule, published: Published, server: FakeServer, models_dir: Path
) -> None:
    write_package(models_dir, "en", "zh", version="1.9")
    write_package(models_dir, "zh", "en", version="1.9")
    (models_dir / "broken.argosmodel").write_bytes(b"not a zip")
    (models_dir / "translate-en_de-1_9.part").write_bytes(b"left over from a crash")

    await start_module()

    catalog = published.catalog
    assert [(m.id, m.installed, m.installed_version, m.url) for m in catalog.models] == [
        (EN_ZH, True, "1.9", ""),
        (ZH_EN, True, "1.9", ""),
    ]
    assert all(m.size_bytes is not None and m.size_bytes > 0 for m in catalog.models)
    assert not catalog.index_loaded and not catalog.refreshing and catalog.index_error is None
    assert not (models_dir / "translate-en_de-1_9.part").exists()
    # 启动时不联网（需求 PRV-01）：获取索引要等界面发送 RefreshModelCatalog。
    assert server.requests == []


async def test_start_with_missing_directory(start_module: StartModule, published: Published) -> None:
    await start_module()
    assert published.catalog.models == ()


async def test_refresh_merges_index_with_installed(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, models_dir: Path
) -> None:
    write_package(models_dir, "en", "zh", version="1.9")
    write_package(models_dir, "de", "en", version="1.0")
    server.add_index(
        [
            index_item("en", "zh", "1.9", size=1000),
            index_item("zh", "en", "1.9", size=2000),
            index_item("de", "en", "1.1"),
            index_item("en", "de", "1.0"),
        ]
    )
    await start_module()
    await refresh(bus)

    catalog = published.catalog
    assert catalog.index_loaded and not catalog.refreshing and catalog.index_error is None
    by_id = {m.id: m for m in catalog.models}
    assert [m.id for m in catalog.models] == [DE_EN, EN_DE, EN_ZH, ZH_EN]
    assert (by_id[EN_ZH].installed, by_id[EN_ZH].version, by_id[EN_ZH].size_bytes) == (True, "1.9", 1000)
    assert by_id[EN_ZH].url == package_url("en", "zh", "1.9")
    assert not by_id[EN_ZH].update_available
    assert (by_id[ZH_EN].installed, by_id[ZH_EN].installed_version, by_id[ZH_EN].size_bytes) == (
        False,
        None,
        2000,
    )
    assert by_id[DE_EN].installed_version == "1.0" and by_id[DE_EN].version == "1.1"
    assert by_id[DE_EN].update_available
    assert by_id[EN_DE].size_bytes is None  # 索引没有给出大小，也没有安装
    assert server.requests == [INDEX_URL]


async def test_refresh_publishes_refreshing_state(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer
) -> None:
    server.add_index([index_item("en", "de")])
    await start_module()
    await refresh(bus)
    flags = [(c.refreshing, c.index_loaded) for c in published.catalogs]
    assert flags == [(False, False), (True, False), (False, True)]


async def test_concurrent_refreshes_share_one_request(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer
) -> None:
    server.add_index([index_item("en", "de")])
    await start_module()
    bus.send(RefreshModelCatalog())
    bus.send(RefreshModelCatalog())
    await bus.drain()
    assert server.requests == [INDEX_URL]
    assert published.catalog.index_loaded


async def test_refresh_skips_incomplete_items_with_warning(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server.add_index([index_item("en", "de"), {"from_code": "en", "to_code": "fr"}])
    await start_module()
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        await refresh(bus)
    assert [m.id for m in published.catalog.models] == [EN_DE]
    assert any("第 2 项" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    ("route", "code", "fragment"),
    [
        (Route(error=httpx.ConnectError("connection refused")), ModelErrorCode.NETWORK, "ConnectError"),
        (Route(error=httpx.ReadTimeout("read timed out")), ModelErrorCode.TIMEOUT, "ReadTimeout"),
        (Route(status=503, body=b"busy"), ModelErrorCode.HTTP, "HTTP 503"),
        (Route(body=b"<html>not json</html>"), ModelErrorCode.BAD_INDEX, "JSON"),
        (Route(body=b'{"packages": []}'), ModelErrorCode.BAD_INDEX, "数组"),
    ],
)
async def test_refresh_failure_keeps_installed_models(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    models_dir: Path,
    route: Route,
    code: ModelErrorCode,
    fragment: str,
) -> None:
    write_package(models_dir, "en", "zh")
    server.add(INDEX_URL, route)
    await start_module()
    await refresh(bus)

    catalog = published.catalog
    assert catalog.index_error_code is code
    assert catalog.index_error is not None and fragment in catalog.index_error
    assert not catalog.index_loaded and not catalog.refreshing
    assert [(m.id, m.installed) for m in catalog.models] == [(EN_ZH, True)]


async def test_failed_refresh_keeps_previous_index_and_success_clears_error(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer
) -> None:
    server.add_index([index_item("en", "de")])
    await start_module()
    await refresh(bus)

    server.add(INDEX_URL, Route(error=httpx.ConnectError("offline")))
    await refresh(bus)
    catalog = published.catalog
    assert catalog.index_loaded, "获取失败不应丢掉上一次成功获取的索引"
    assert catalog.index_error_code is ModelErrorCode.NETWORK
    assert [m.id for m in catalog.models] == [EN_DE]

    server.add_index([index_item("en", "de"), index_item("de", "en")])
    await refresh(bus)
    catalog = published.catalog
    assert catalog.index_error is None and catalog.index_error_code is None
    assert [m.id for m in catalog.models] == [DE_EN, EN_DE]


async def test_delete_removes_every_version_and_notifies(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, models_dir: Path
) -> None:
    old = write_package(models_dir, "en", "zh", version="1.0")
    new = write_package(models_dir, "en", "zh", version="1.9")
    other = write_package(models_dir, "zh", "en", version="1.9")
    server.add_index([index_item("en", "zh", "1.9")])
    await start_module()
    await refresh(bus)

    bus.send(DeleteModel(EN_ZH))
    await bus.drain()

    assert not old.exists() and not new.exists() and other.exists()
    by_id = {m.id: m for m in published.catalog.models}
    assert not by_id[EN_ZH].installed and by_id[EN_ZH].url  # 仍可重新下载
    assert by_id[ZH_EN].installed
    assert published.installed == [ModelsInstalled((), removed=(EN_ZH,))]


async def test_delete_of_model_that_is_not_installed_does_nothing(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server.add_index([index_item("en", "zh")])
    await start_module()
    await refresh(bus)
    count = len(published.catalogs)
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        bus.send(DeleteModel(EN_ZH))
        bus.send(DeleteModel("argos:xx:yy"))
        await bus.drain()
    assert len(published.catalogs) == count
    assert published.installed == []
    assert len([r for r in caplog.records if "没有安装" in r.getMessage()]) == 2
