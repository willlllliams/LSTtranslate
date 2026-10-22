# ruff: noqa: E501  文案表里一条文案写在一行，方便对照中英文。
"""关于与诊断页“自动化接口”一组与构建类型的文案（需求 DEV-01 至 DEV-03）。导入本模块时登记文案。"""

from typing import Final

from lst.ui.i18n import Catalog, register_catalog


def _pair(zh: str, en: str) -> dict[str, str]:
    return {"zh-Hans": zh, "en": en}


CATALOG: Final[Catalog] = {
    "settings.about.version.flavor": _pair("版本 {version}（{flavor}）", "Version {version} ({flavor})"),
    "settings.flavor.dev": _pair("开发版", "developer build"),
    "settings.flavor.normal": _pair("普通版", "standard build"),
    "settings.section.automation": _pair("自动化接口", "Automation interface"),
    "settings.automation.title": _pair("自动化接口", "Automation interface"),
    "settings.automation.desc": _pair(
        "开启后，同一台电脑上以你的账户运行的程序可以控制 LSTtranslate，包括截屏、读取识别与翻译结果、修改设置、更改数据目录和重启程序。供测试脚本与 AI agent 使用；平时不需要时请关闭。",
        "When on, programs running under your account on this PC can control LSTtranslate: take screenshots, read the recognized text and translations, change settings, move the data folder and restart the program. Meant for test scripts and AI agents; keep it off when you do not need it.",
    ),
    "settings.automation.desc.full": _pair("{desc}{default}", "{desc} {default}"),
    "settings.automation.default.dev": _pair("开发版默认开启。", "On by default in the developer build."),
    "settings.automation.default.normal": _pair("普通版默认关闭。", "Off by default in the standard build."),
    "settings.automation.listening": _pair("正在监听：{pipe}", "Listening on {pipe}"),
    "settings.automation.off": _pair("未开启。", "Off."),
    "settings.automation.error": _pair("无法开启：{reason}", "Could not turn on: {reason}"),
    "settings.automation.missing": _pair(
        "这次运行没有装配自动化接口。", "The automation interface is not available in this run."
    ),
}

register_catalog(CATALOG)
