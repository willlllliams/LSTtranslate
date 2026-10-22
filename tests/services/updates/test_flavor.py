"""应用内更新按构建类型选安装包（需求 DEV-08）：同一个 Release 同时放普通版与开发版两个安装包，
普通版只取不带 -dev 的那个，开发版只取 -dev-setup.exe；下载目录的清理认得两种文件名。"""

import logging
from pathlib import Path

import pytest

from lst.contracts.automation import BuildFlavor
from lst.contracts.updates import CheckForUpdates, DownloadUpdate, UpdateState
from lst.kernel import EventBus
from lst.services.updates.module import _cleanup_downloads  # pyright: ignore[reportPrivateUsage]
from lst.services.updates.releases import installer_name, parse_installer_name, parse_releases
from lst.services.updates.version import parse_version
from tests.services.updates.conftest import Env, Published, StartModule
from tests.services.updates.helpers import DOWNLOAD_HOST, FakeGitHub, installer_bytes, release

LOGGER = logging.getLogger("tests.updates.flavor")
BOTH = ("normal", "dev")


def test_installer_names() -> None:
    assert installer_name("0.3.0") == "LSTtranslate-0.3.0-setup.exe"
    assert installer_name("0.3.0", BuildFlavor.DEV) == "LSTtranslate-0.3.0-dev-setup.exe"
    assert installer_name("0.3.0-beta.1", BuildFlavor.DEV) == "LSTtranslate-0.3.0-beta.1-dev-setup.exe"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("LSTtranslate-0.3.0-setup.exe", ("0.3.0", BuildFlavor.NORMAL)),
        ("LSTtranslate-0.3.0-dev-setup.exe", ("0.3.0", BuildFlavor.DEV)),
        ("LSTtranslate-0.3.0-beta.1-setup.exe", ("0.3.0-beta.1", BuildFlavor.NORMAL)),
        ("LSTtranslate-0.3.0-beta.1-dev-setup.exe", ("0.3.0-beta.1", BuildFlavor.DEV)),
        ("LSTtranslate-0.3.0-setup.exe.sha256", None),
        ("other.exe", None),
    ],
)
def test_parse_installer_name(name: str, expected: tuple[str, BuildFlavor] | None) -> None:
    assert parse_installer_name(name) == expected


@pytest.mark.parametrize("flavor", list(BuildFlavor))
def test_each_flavor_takes_only_its_own_assets(flavor: BuildFlavor) -> None:
    [candidate] = parse_releases([release("v0.3.0-beta.1", prerelease=True, flavors=BOTH)], LOGGER, flavor)
    expected = installer_name("0.3.0-beta.1", flavor)
    assert candidate.installer_name == expected
    assert candidate.installer_url == f"{DOWNLOAD_HOST}/v0.3.0-beta.1/{expected}"
    assert candidate.checksum_url == candidate.installer_url + ".sha256"


def test_release_without_the_flavor_is_skipped() -> None:
    """只放了普通版安装包的版本，开发版不认它（不会下载普通版把自己“降级”成普通版），反过来也一样。"""
    only_normal = [release("v0.3.0")]
    only_dev = [release("v0.3.0", flavors=("dev",))]
    assert parse_releases(only_normal, LOGGER, BuildFlavor.DEV) == []
    assert parse_releases(only_dev, LOGGER, BuildFlavor.NORMAL) == []
    assert len(parse_releases(only_dev, LOGGER, BuildFlavor.DEV)) == 1
    assert len(parse_releases(only_normal, LOGGER)) == 1  # 缺省是普通版


async def test_dev_build_downloads_the_dev_installer(
    start_module: StartModule, bus: EventBus, published: Published, github: FakeGitHub, env: Env
) -> None:
    github.releases = [release("v0.2.0", flavors=BOTH)]
    await start_module(flavor=BuildFlavor.DEV)
    bus.send(CheckForUpdates(manual=True))
    await bus.drain()
    assert published.status.state is UpdateState.AVAILABLE
    bus.send(DownloadUpdate())
    await bus.drain()
    assert published.status.state is UpdateState.READY
    name = "LSTtranslate-0.2.0-dev-setup.exe"
    assert [p.name for p in env.download_dir.iterdir()] == [name]
    assert (env.download_dir / name).read_bytes() == installer_bytes("0.2.0-dev")
    downloaded = {path.rsplit("/", 1)[-1] for path in github.paths()[1:]}
    assert downloaded == {name, name + ".sha256"}


def test_cleanup_understands_both_names(tmp_path: Path) -> None:
    directory = tmp_path / "updates"
    directory.mkdir()
    names = [
        "LSTtranslate-0.1.0-setup.exe",
        "LSTtranslate-0.1.0-dev-setup.exe",
        "LSTtranslate-0.3.0-setup.exe",
        "LSTtranslate-0.3.0-dev-setup.exe",
        "LSTtranslate-0.3.0-dev-setup.exe.part",
    ]
    for name in names:
        (directory / name).write_bytes(b"x")
    current = parse_version("0.2.0")
    assert current is not None
    removed = _cleanup_downloads(directory, current, LOGGER)
    # 不高于当前版本的两种安装包与临时文件都删掉；0.3.0 的两种都留着（版本号按去掉 -dev 之后比较）。
    assert removed == 3
    assert sorted(p.name for p in directory.iterdir()) == [
        "LSTtranslate-0.3.0-dev-setup.exe",
        "LSTtranslate-0.3.0-setup.exe",
    ]
    _cleanup_downloads(directory, current, LOGGER, keep="LSTtranslate-0.3.0-dev-setup.exe")
    assert [p.name for p in directory.iterdir()] == ["LSTtranslate-0.3.0-dev-setup.exe"]
