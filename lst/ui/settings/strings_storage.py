# ruff: noqa: E501  文案表里一条文案写在一行，方便对照中英文。
"""常规页“存储位置”一组的文案（需求 CFG-06、CFG-07、MOD-04）。导入本模块时登记文案。

两张卡片的文案按 ``settings.storage.<data|models>.*`` 取键；更改失败的原因按
``settings.storage.error.<StorageErrorCode 的值>`` 取键，占位符 detail 是系统给出的细节。
"""

from typing import Final

from lst.ui.i18n import Catalog, register_catalog


def _pair(zh: str, en: str) -> dict[str, str]:
    return {"zh-Hans": zh, "en": en}


CATALOG: Final[Catalog] = {
    "settings.section.storage": _pair("存储位置", "Storage locations"),
    "settings.storage.change": _pair("更改(&H)…", "C&hange…"),
    "settings.storage.open": _pair("打开(&O)", "&Open"),
    "settings.storage.progress": _pair("已完成 {done} / {total}", "{done} of {total} done"),
    "settings.storage.unavailable": _pair(
        "这个版本没有提供存储位置的设置。", "This build does not provide storage location settings."
    ),
    # 数据目录
    "settings.storage.data.title": _pair("数据目录", "Data folder"),
    "settings.storage.data.desc": _pair(
        "配置、译文缓存、竞速统计、日志和窗口位置存放在这里。更改后重启程序才生效。",
        "Settings, the translation cache, racing statistics, logs and window positions are stored here. A change takes effect after the program restarts.",
    ),
    "settings.storage.data.default": _pair("当前是默认位置。", "This is the default location."),
    "settings.storage.data.fixed.argument": _pair(
        "这次启动用命令行参数 --data-dir 指定了数据目录，这里不能更改。",
        "The data folder was set with the --data-dir command-line option for this run and cannot be changed here.",
    ),
    "settings.storage.data.fixed.environment": _pair(
        "环境变量 LST_DATA_DIR 指定了数据目录，这里不能更改。要改用别的目录，先删除这个环境变量。",
        "The LST_DATA_DIR environment variable sets the data folder, so it cannot be changed here. Remove the variable to choose another folder.",
    ),
    "settings.storage.data.pending": _pair(
        "重启程序后改用：{path}", "After a restart the program uses: {path}"
    ),
    "settings.storage.data.choose": _pair("选择新的数据目录", "Choose a new data folder"),
    "settings.storage.data.confirm": _pair(
        "把数据目录改为：\n{path}\n\n更改在重启程序后生效，重启之前程序继续使用当前目录。不复制现有数据时，新目录里的配置从默认值开始，首次运行引导会再出现一次；API key 保存在 Windows 凭据管理器里，不受影响。",
        "Change the data folder to:\n{path}\n\nThe change takes effect after the program restarts; until then the current folder stays in use. Without copying the existing data, settings in the new folder start from their defaults and the first-run guide appears again. API keys are kept in Windows Credential Manager and are not affected.",
    ),
    "settings.storage.data.copy": _pair(
        "把现有数据复制到新位置", "Copy the existing data to the new location"
    ),
    "settings.storage.data.working": _pair("正在复制数据…", "Copying data…"),
    "settings.storage.restart": _pair("立即重启(&R)", "&Restart now"),
    # 模型目录
    "settings.storage.models.title": _pair("模型目录", "Model folder"),
    "settings.storage.models.desc": _pair(
        "本地翻译模型、OCR 模型与 OCR 引擎的运行环境、词典包和显卡运行库存放在这里，可能占用几 GB。更改时本地翻译与文字识别暂停，完成后自动恢复，不需要重启程序。",
        "Local translation models, OCR models and OCR engine runtimes, dictionary packs and the GPU runtime are stored here and can take several GB. During a change, local translation and text recognition pause and resume on their own afterwards; no restart is needed.",
    ),
    "settings.storage.models.default": _pair(
        "当前是默认位置：数据目录下的 models。", "This is the default location: models in the data folder."
    ),
    "settings.storage.models.reset": _pair("恢复默认位置(&D)", "&Default location"),
    "settings.storage.models.choose": _pair("选择新的模型目录", "Choose a new model folder"),
    "settings.storage.models.confirm": _pair(
        "把模型目录改为：\n{path}\n\n本地翻译与文字识别会先停下，完成后自动恢复；进行中的下载会被取消。不移动现有模型时，程序在新目录里重新查找模型，旧目录里的文件保留在原处。",
        "Change the model folder to:\n{path}\n\nLocal translation and text recognition stop first and resume when the change is done; downloads in progress are cancelled. Without moving the existing models, the program looks for models in the new folder and the files in the old folder stay where they are.",
    ),
    "settings.storage.models.move": _pair(
        "把现有模型移动到新目录", "Move the existing models to the new folder"
    ),
    "settings.storage.models.working": _pair("正在移动模型…", "Moving models…"),
    # 更改失败的原因
    "settings.storage.error.fixed": _pair(
        "数据目录由命令行参数或环境变量指定，不能在这里更改。",
        "The data folder is set by a command-line option or an environment variable and cannot be changed here.",
    ),
    "settings.storage.error.relative": _pair("请选择一个完整的路径。", "Choose a full path."),
    "settings.storage.error.nested": _pair(
        "新目录在当前目录里面，请选择当前目录之外的位置。",
        "The new folder is inside the current one. Choose a location outside it.",
    ),
    "settings.storage.error.not_writable": _pair(
        "新目录无法写入（{detail}）。可能是没有权限、介质只读，或程序以低完整性级别运行。",
        "The new folder cannot be written to ({detail}). The cause may be missing permissions, read-only media, or the program running at low integrity level.",
    ),
    "settings.storage.error.no_space": _pair(
        "新目录所在的磁盘空间不够（{detail}）。",
        "There is not enough space on the drive of the new folder ({detail}).",
    ),
    "settings.storage.error.occupied": _pair(
        "新目录里已经有同名的内容：{detail}。请选择空目录，或先把它们移走。",
        "The new folder already contains items with the same names: {detail}. Choose an empty folder or move them away first.",
    ),
    "settings.storage.error.copy_failed": _pair(
        "复制数据失败（{detail}），数据目录没有更改，新目录里复制了一半的内容已删除。",
        "Copying the data failed ({detail}). The data folder was not changed, and the partly copied items in the new folder were removed.",
    ),
    "settings.storage.error.move_failed": _pair(
        "移动模型失败（{detail}），模型已经移回原处，模型目录没有更改。",
        "Moving the models failed ({detail}). The models were moved back and the model folder was not changed.",
    ),
    "settings.storage.error.save_failed": _pair(
        "保存新位置失败（{detail}），目录没有更改。",
        "Saving the new location failed ({detail}). The folder was not changed.",
    ),
    "settings.storage.error.busy": _pair(
        "上一次更改还没有完成，请等它结束。",
        "The previous change has not finished yet. Wait until it is done.",
    ),
}

register_catalog(CATALOG)

__all__ = ["CATALOG"]
