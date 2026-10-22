r"""构建类型：普通版或开发版（需求 DEV-02，架构文档 16.1 节）。

- 安装版：读安装目录 ``app\installed.json`` 的 flavor 字段，由构建脚本按 ``--flavor`` 写入。
  字段缺失（旧版本的安装包）或取值不认识时按普通版处理。安装版不接受命令行与环境变量的覆盖：
  构建类型还决定应用内更新下载哪一种安装包，随手改一个环境变量不应把普通版升级成开发版；
- 源码版：命令行 ``--flavor dev`` → 环境变量 ``LST_FLAVOR`` → 普通版。
"""

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

from lst.contracts.automation import BuildFlavor

FLAVOR_ENV: Final = "LST_FLAVOR"

logger = logging.getLogger(__name__)


def parse_flavor(value: object) -> BuildFlavor | None:
    """flavor 字段或参数的取值 → 构建类型；不认识时返回 None。"""
    if not isinstance(value, str):
        return None
    try:
        return BuildFlavor(value.strip().lower())
    except ValueError:
        return None


def installed_flavor(marker: Path) -> BuildFlavor:
    """安装版标记文件里的构建类型。"""
    try:
        data: object = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("无法读取安装版标记 %s，按普通版运行", marker)
        return BuildFlavor.NORMAL
    raw = cast(dict[str, object], data).get("flavor") if isinstance(data, dict) else None
    if raw is None:
        return BuildFlavor.NORMAL
    flavor = parse_flavor(raw)
    if flavor is None:
        logger.warning("安装版标记里的构建类型 %r 无法识别，按普通版运行", raw)
        return BuildFlavor.NORMAL
    return flavor


def choose_flavor(
    argument: str | None,
    *,
    installed: bool,
    marker: Path,
    environ: Mapping[str, str] | None = None,
) -> BuildFlavor:
    """本次运行的构建类型，规则见模块说明。"""
    env = os.environ if environ is None else environ
    if installed:
        if argument or env.get(FLAVOR_ENV):
            logger.warning("安装版的构建类型由安装包决定，忽略 --flavor 与 %s", FLAVOR_ENV)
        return installed_flavor(marker)
    for source, value in (("--flavor", argument), (FLAVOR_ENV, env.get(FLAVOR_ENV))):
        if not value:
            continue
        flavor = parse_flavor(value)
        if flavor is None:
            logger.warning("%s 的取值 %r 无法识别，按普通版运行", source, value)
            return BuildFlavor.NORMAL
        return flavor
    return BuildFlavor.NORMAL


__all__ = ["FLAVOR_ENV", "choose_flavor", "installed_flavor", "parse_flavor"]
