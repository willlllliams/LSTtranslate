"""配置服务测试的公共夹具。"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.services.config.helpers import Launcher


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """配置文件路径。上级目录 data/config 还不存在，配置服务要自己创建。"""
    return tmp_path / "data" / "config" / "settings.toml"


@pytest.fixture
async def start() -> AsyncIterator[Launcher]:
    launcher = Launcher()
    yield launcher
    await launcher.close_all()
