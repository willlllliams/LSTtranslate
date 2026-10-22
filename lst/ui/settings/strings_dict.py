# ruff: noqa: E501  文案表里一条文案写在一行，方便对照中英文；折行后反而难读。
"""模型页“词典”组与字典翻译引擎的界面文案（简体中文与英文）。导入本模块时登记文案。

词典组的文案单独成表，与设置窗口的主文案表（strings 模块）分开维护。错误码 ``engine.no_match``
的原因与处理建议是覆盖窗口和设置窗口共用的错误文案，登记在 lst.ui.i18n。

署名的写法依据数据包发布页与索引里的署名全文：数据包是对 Wiktionary 与 ECDICT 的改编（抽取、筛选和裁剪），
Wiktionary 部分以 CC BY-SA 4.0 发布，ECDICT 部分以 MIT 许可证发布。CC BY-SA 4.0 要求署名可见，
所以总署名写在词典列表上方，每个包的署名全文写在它的下一行。
"""

from typing import Final

from lst.ui.i18n import Catalog, register_catalog, tr

__all__ = ["CATALOG", "LICENSE_NAMES", "dict_credit", "license_name"]

LICENSE_NAMES: Final[dict[str, tuple[str, str]]] = {
    "CC-BY-SA-4.0": ("CC BY-SA 4.0", "CC BY-SA 4.0"),
    "MIT": ("MIT", "MIT"),
}
"""许可证 SPDX 标识 → （中文显示名，英文显示名）。表里没有的标识原样显示。"""

_LICENSE_PREFIX: Final = "settings.dicts.license"

CATALOG: Final[Catalog] = {
    "settings.dicts.title": {"zh-Hans": "词典", "en": "Dictionaries"},
    "settings.dicts.intro": {
        "zh-Hans": "“词典”翻译引擎用词典包离线逐词查词，几毫秒就给出粗略译文，更准确的译文到达后替换它。每个翻译方向一个包，大小 0.3 到 4 MB，下载一次即可离线使用。",
        "en": "The Dictionary engine looks words up offline in dictionary packs and gives a rough translation within milliseconds, replaced when a better translation arrives. There is one pack per direction, 0.3 to 4 MB each; once downloaded it works offline.",
    },
    "settings.dicts.attribution": {
        "zh-Hans": '词典数据改编（抽取、筛选和裁剪）自 <a href="https://www.wiktionary.org/">Wiktionary</a>（由 wiktextract 抽取、<a href="https://kaikki.org/">kaikki.org</a> 发布，<a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>）和 <a href="https://github.com/skywind3000/ECDICT">ECDICT</a>（<a href="https://opensource.org/license/mit">MIT</a>）。每个词典包的署名列在它的下一行。',
        "en": 'Dictionary data is adapted (extracted, filtered and trimmed) from <a href="https://www.wiktionary.org/">Wiktionary</a> (extracted by wiktextract and published by <a href="https://kaikki.org/">kaikki.org</a>, <a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>) and <a href="https://github.com/skywind3000/ECDICT">ECDICT</a> (<a href="https://opensource.org/license/mit">MIT</a>). The attribution of each pack is shown on the line below it.',
    },
    "settings.dicts.notices": {
        "zh-Hans": "数据来源与许可证全文见 THIRD-PARTY-NOTICES 文件。",
        "en": "The data sources and full license texts are in the THIRD-PARTY-NOTICES file.",
    },
    "settings.dicts.list": {"zh-Hans": "词典包", "en": "Dictionary packs"},
    "settings.dicts.col.dict": {"zh-Hans": "词典", "en": "Dictionary"},
    "settings.dicts.col.license": {"zh-Hans": "许可证", "en": "License"},
    "settings.dicts.row.attribution": {"zh-Hans": "署名：{attribution}", "en": "Attribution: {attribution}"},
    "settings.dicts.status.update": {
        "zh-Hans": "已安装（{installed}），发布页上有更新的版本",
        "en": "Installed ({installed}), a newer build is available",
    },
    "settings.dicts.download": {"zh-Hans": "下载词典(&L)", "en": "Down&load dictionaries"},
    "settings.dicts.cancel": {"zh-Hans": "取消词典下载(&N)", "en": "Ca&ncel dictionary download"},
    "settings.dicts.delete": {"zh-Hans": "删除词典(&T)", "en": "Dele&te dictionaries"},
    "settings.dicts.delete.confirm": {
        "zh-Hans": "删除词典“{name}”？之后需要重新下载才能使用。",
        "en": "Delete the dictionary “{name}”? You will need to download it again to use it.",
    },
    "settings.dicts.delete.confirm_many": {
        "zh-Hans": "删除选中的 {count} 个词典？之后需要重新下载才能使用。",
        "en": "Delete the {count} selected dictionaries? You will need to download them again to use them.",
    },
    "settings.dicts.index.loading": {
        "zh-Hans": "正在获取可下载的词典列表…",
        "en": "Fetching the list of available dictionaries…",
    },
    "settings.dicts.index.failed": {
        "zh-Hans": "无法获取词典列表：{reason}",
        "en": "Could not fetch the dictionary list: {reason}",
    },
    "settings.dicts.index.not_loaded": {
        "zh-Hans": "目前只列出了已安装的词典。点击“刷新列表”获取可下载的词典（需要联网）。",
        "en": "Only installed dictionaries are listed. Click Refresh list to fetch downloadable ones (needs a network connection).",
    },
    "settings.dicts.empty": {"zh-Hans": "没有符合条件的词典。", "en": "No dictionaries match."},
    **{f"{_LICENSE_PREFIX}.{spdx}": {"zh-Hans": zh, "en": en} for spdx, (zh, en) in LICENSE_NAMES.items()},
}

register_catalog(CATALOG)


def license_name(spdx: str) -> str:
    """许可证的显示名：CC-BY-SA-4.0 显示为 CC BY-SA 4.0；表里没有的 SPDX 标识原样显示，空串仍是空串。"""
    if not spdx:
        return ""
    return tr(f"{_LICENSE_PREFIX}.{spdx}") if spdx in LICENSE_NAMES else spdx


def dict_credit() -> str:
    """词典列表上方的总署名（富文本，许可证名链接到许可证全文），后面接第三方声明文件的说明。"""
    return f"{tr('settings.dicts.attribution')} {tr('settings.dicts.notices')}"
