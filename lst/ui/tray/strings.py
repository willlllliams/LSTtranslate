"""托盘模块的界面文案（简体中文与英文，需求 CFG-05）。导入本模块时登记到 ui.i18n。

引擎与推理进程的状态名（health.*）、推理进程名（worker.*）和错误码文案是共用文案，定义在 ui.i18n 里。
菜单命令的文案带 Qt 助记符 ``&``，键盘打开托盘菜单后可以按字母直接选中。
"""

from typing import Final

from lst.ui.i18n import Catalog, register_catalog

LOCATION_SEPARATOR: Final = " › "
"""配置位置各层之间的分隔符，例如 ``engines › 第 3 项 › priority``。"""

CATALOG: Final[Catalog] = {
    # 状态区
    "tray.status.line": {"zh-Hans": "{name}：{state}", "en": "{name}: {state}"},
    "tray.status.none": {"zh-Hans": "暂无组件状态", "en": "No component status yet"},
    "tray.status.reason": {"zh-Hans": "原因：{text}", "en": "Reason: {text}"},
    "tray.status.suggestion": {"zh-Hans": "处理建议：{text}", "en": "What to do: {text}"},
    "tray.worker.crashed.reason": {
        "zh-Hans": "推理进程崩溃，正在自动重启",
        "en": "The process crashed and is restarting",
    },
    "tray.worker.crashed.suggestion": {
        "zh-Hans": "反复出现时导出诊断信息并检查日志",
        "en": "If it keeps happening, export diagnostics and check the logs",
    },
    "tray.worker.failed.reason": {
        "zh-Hans": "推理进程多次崩溃或无法启动，已停止自动重启",
        "en": "The process crashed repeatedly or could not start, so it is no longer restarted",
    },
    "tray.worker.failed.suggestion": {
        "zh-Hans": "在设置中检查配置后重载；仍然失败时导出诊断信息并检查日志",
        "en": "Check the configuration in Settings and reload; if it still fails, export diagnostics",
    },
    # 菜单命令
    "tray.action.capture": {"zh-Hans": "截屏翻译(&C)", "en": "&Capture and translate"},
    "tray.action.bilingual": {"zh-Hans": "显示/隐藏双语窗口(&B)", "en": "Show/hide &bilingual window"},
    "tray.action.settings": {"zh-Hans": "设置(&S)", "en": "&Settings"},
    "tray.action.models": {"zh-Hans": "下载翻译模型(&M)…", "en": "Download translation &models…"},
    "tray.action.open_logs": {"zh-Hans": "打开日志目录(&L)", "en": "Open &log folder"},
    "tray.action.export_diagnostics": {"zh-Hans": "导出诊断信息(&D)…", "en": "Export &diagnostics…"},
    "tray.action.quit": {"zh-Hans": "退出(&X)", "en": "E&xit"},
    # 托盘图标的悬停提示与整体状态
    "tray.tooltip": {"zh-Hans": "{app}\n{status}", "en": "{app}\n{status}"},
    "tray.overall.ready": {"zh-Hans": "全部就绪", "en": "All ready"},
    "tray.overall.loading": {"zh-Hans": "正在加载", "en": "Loading"},
    "tray.overall.error": {"zh-Hans": "有 {count} 项出错", "en": "Errors: {count}"},
    "tray.overall.idle": {"zh-Hans": "没有启用的组件", "en": "Nothing enabled"},
    # 通知
    "tray.notify.hotkey_failed.title": {"zh-Hans": "热键注册失败", "en": "Hotkey registration failed"},
    "tray.notify.hotkey_failed.message": {
        "zh-Hans": "热键 {hotkey} 注册失败：{reason}。请在设置 → 热键中换一个组合。",
        "en": "Hotkey {hotkey} could not be registered: {reason}. Choose another one in Settings → Hotkeys.",
    },
    "tray.notify.config_rejected.title": {"zh-Hans": "配置有误", "en": "Invalid settings"},
    "tray.notify.config_rejected.message_one": {
        "zh-Hans": "有 1 处配置无效，已使用默认值。请在设置中检查：",
        "en": "1 setting was invalid and has been reset to its default. Check it in Settings:",
    },
    "tray.notify.config_rejected.message": {
        "zh-Hans": "有 {count} 处配置无效，已使用默认值。请在设置中检查：",
        "en": "{count} settings were invalid and have been reset to defaults. Check them in Settings:",
    },
    "tray.notify.config_rejected.more": {"zh-Hans": "另有 {count} 处未列出", "en": "and {count} more"},
    "tray.location.index": {"zh-Hans": "第 {n} 项", "en": "item {n}"},
    "tray.location.root": {"zh-Hans": "整个配置文件", "en": "the whole configuration file"},
    "tray.notify.open_logs_failed.title": {
        "zh-Hans": "无法打开日志目录",
        "en": "Cannot open the log folder",
    },
    "tray.notify.open_logs_failed.message": {
        "zh-Hans": "{path}\n请确认该目录存在，并且当前用户可以访问。",
        "en": "{path}\nMake sure the folder exists and you have access to it.",
    },
}

register_catalog(CATALOG)
