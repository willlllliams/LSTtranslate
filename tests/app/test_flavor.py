"""构建类型与自动化接口开关的默认值（需求 DEV-01 至 DEV-03）。"""

import json
import tomllib
from pathlib import Path

import pytest

from lst.app.automation.config import AutomationConfig, automation_config, effective_enabled
from lst.app.bootstrap import all_config_sections
from lst.app.flavor import FLAVOR_ENV, choose_flavor, installed_flavor, parse_flavor
from lst.contracts.automation import BuildFlavor
from lst.contracts.config import AppConfig
from lst.services.config import document
from lst.services.config.service import _default_section  # pyright: ignore[reportPrivateUsage]


def _marker(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "installed.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_installed_flavor_reads_the_marker(tmp_path: Path) -> None:
    assert installed_flavor(_marker(tmp_path, {"version": "1.0.0", "flavor": "dev"})) is BuildFlavor.DEV
    assert installed_flavor(_marker(tmp_path, {"version": "1.0.0", "flavor": "normal"})) is BuildFlavor.NORMAL
    # 旧版本的安装包没有 flavor 字段：普通版。
    assert installed_flavor(_marker(tmp_path, {"version": "1.0.0"})) is BuildFlavor.NORMAL
    assert installed_flavor(_marker(tmp_path, {"flavor": "beta"})) is BuildFlavor.NORMAL
    assert installed_flavor(tmp_path / "missing.json") is BuildFlavor.NORMAL


def test_source_run_takes_argument_then_environment(tmp_path: Path) -> None:
    marker = tmp_path / "none.json"
    assert choose_flavor(None, installed=False, marker=marker, environ={}) is BuildFlavor.NORMAL
    assert choose_flavor(None, installed=False, marker=marker, environ={FLAVOR_ENV: "dev"}) is BuildFlavor.DEV
    assert choose_flavor("normal", installed=False, marker=marker, environ={FLAVOR_ENV: "dev"}) is (
        BuildFlavor.NORMAL
    )
    assert choose_flavor("dev", installed=False, marker=marker, environ={}) is BuildFlavor.DEV
    assert (
        choose_flavor(None, installed=False, marker=marker, environ={FLAVOR_ENV: "DEV "}) is BuildFlavor.DEV
    )
    assert (
        choose_flavor(None, installed=False, marker=marker, environ={FLAVOR_ENV: "x"}) is BuildFlavor.NORMAL
    )


def test_installed_run_ignores_overrides(tmp_path: Path) -> None:
    """安装版的构建类型只看安装包写的标记：环境变量改不了它（它还决定应用内更新下载哪种安装包）。"""
    marker = _marker(tmp_path, {"version": "1.0.0", "flavor": "normal"})
    assert (
        choose_flavor("dev", installed=True, marker=marker, environ={FLAVOR_ENV: "dev"}) is BuildFlavor.NORMAL
    )
    dev = _marker(tmp_path, {"version": "1.0.0", "flavor": "dev"})
    assert choose_flavor(None, installed=True, marker=dev, environ={}) is BuildFlavor.DEV


def test_parse_flavor() -> None:
    assert parse_flavor("dev") is BuildFlavor.DEV
    assert parse_flavor(1) is None and parse_flavor("debug") is None


@pytest.mark.parametrize(
    ("configured", "flavor", "expected"),
    [
        (None, BuildFlavor.NORMAL, False),
        (None, BuildFlavor.DEV, True),
        (True, BuildFlavor.NORMAL, True),
        (False, BuildFlavor.DEV, False),
    ],
)
def test_flavor_decides_only_the_default(
    configured: bool | None, flavor: BuildFlavor, expected: bool
) -> None:
    assert effective_enabled(AutomationConfig(enabled=configured), flavor) is expected


def test_section_parsing_falls_back_to_default() -> None:
    assert automation_config(None).enabled is None
    assert automation_config(AppConfig({"automation": {"enabled": True}}, 1)).enabled is True
    assert automation_config(AppConfig({"automation": "bad"}, 1)).enabled is None


def test_default_config_file_leaves_the_switch_unset() -> None:
    """生成的默认配置文件里没有 [automation].enabled：没动过开关的用户换装另一种构建类型后，
    开关跟随新的默认值。"""
    sections = all_config_sections()
    assert sections["automation"] is AutomationConfig
    rendered = document.render_default(
        {name: _default_section(name, model) for name, model in sections.items()}, {}
    )
    parsed = tomllib.loads(rendered)
    assert parsed["automation"] == {}
    assert "[automation]" in rendered
