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
        "zh-Hans": "可以选择“重新启动”立即重启；反复出现时导出诊断信息并检查日志",
        "en": "Choose Restart to restart it now; if it keeps happening, "
        "export diagnostics and check the logs",
    },
    "tray.worker.failed.reason": {
        "zh-Hans": "推理进程多次崩溃或无法启动，已停止自动重启",
        "en": "The process crashed repeatedly or could not start, so it is no longer restarted",
    },
    "tray.worker.failed.suggestion": {
        "zh-Hans": "在设置中检查配置后选择“重新启动”；仍然失败时导出诊断信息并检查日志",
        "en": "Check the configuration in Settings, then choose Restart; "
        "if it still fails, export diagnostics",
    },
    "tray.worker.restart": {"zh-Hans": "重新启动(&R)", "en": "&Restart"},
    # 菜单命令
    "tray.action.capture": {"zh-Hans": "截屏翻译(&C)", "en": "&Capture and translate"},
    "tray.action.bilingual": {"zh-Hans": "显示/隐藏双语窗口(&B)", "en": "Show/hide &bilingual window"},
    "tray.action.settings": {"zh-Hans": "设置(&S)", "en": "&Settings"},
    "tray.action.models": {"zh-Hans": "下载翻译模型(&M)…", "en": "Download translation &models…"},
    "tray.action.open_logs": {"zh-Hans": "打开日志目录(&L)", "en": "Open &log folder"},
    "tray.action.export_diagnostics": {"zh-Hans": "导出诊断信息(&D)…", "en": "Export &diagnostics…"},
    "tray.action.update": {"zh-Hans": "检查更新(&U)…", "en": "Check for &updates…"},
    "tray.action.quit": {"zh-Hans": "退出(&X)", "en": "E&xit"},
    # “检查更新”菜单项随更新状态变化的文字
    "tray.update.checking": {"zh-Hans": "正在检查更新…", "en": "Checking for updates…"},
    "tray.update.install": {"zh-Hans": "安装更新 {version}(&U)…", "en": "Install &update {version}…"},
    "tray.update.downloading": {
        "zh-Hans": "正在下载更新 {version}（{percent}%）",
        "en": "Downloading update {version} ({percent}%)",
    },
    "tray.update.downloading_unknown": {
        "zh-Hans": "正在下载更新 {version}…",
        "en": "Downloading update {version}…",
    },
    "tray.update.installing": {"zh-Hans": "正在启动安装程序…", "en": "Starting the installer…"},
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
    # 用户在托盘菜单里检查、下载、安装更新后的结果通知
    "tray.notify.update_latest.title": {"zh-Hans": "已是最新版本", "en": "You're up to date"},
    "tray.notify.update_latest.message": {
        "zh-Hans": "当前版本 {version} 已是最新。",
        "en": "Version {version} is the latest.",
    },
    "tray.notify.update_ready.title": {"zh-Hans": "更新已下载", "en": "Update downloaded"},
    "tray.notify.update_ready.message": {
        "zh-Hans": "{version} 已下载并通过校验。点托盘菜单中的“安装更新 {version}…”开始安装，"
        "程序会先退出，安装完成后自动重新启动。",
        "en": "{version} has been downloaded and verified. Choose “Install update {version}…” in the tray "
        "menu to install it; the program exits and restarts after installation.",
    },
    "tray.notify.update_check_failed.title": {"zh-Hans": "检查更新失败", "en": "Update check failed"},
    "tray.notify.update_download_failed.title": {"zh-Hans": "下载更新失败", "en": "Update download failed"},
    "tray.notify.update_install_failed.title": {"zh-Hans": "无法安装更新", "en": "Cannot install the update"},
}
"""托盘自己的文案。"""

UPDATE_CATALOG: Final[Catalog] = {
    # services.updates 发布的 NotificationRequested 用到的文案键（自动检查发现新版本时）。
    # 托盘在手动检查发现新版本时也用这两条。
    "update.notify.available.title": {"zh-Hans": "发现新版本", "en": "Update available"},
    "update.notify.available.message": {
        "zh-Hans": "LSTtranslate {version} 已发布。点托盘菜单中的“安装更新 {version}…”下载并安装。",
        "en": "LSTtranslate {version} is available. Choose “Install update {version}…” in the tray menu "
        "to download and install it.",
    },
    "update.notify.available_source.message": {
        "zh-Hans": "LSTtranslate {version} 已发布。源码运行不能自动安装，请更新源码后重新运行。",
        "en": "LSTtranslate {version} is available. Running from source cannot install it automatically; "
        "update the source code and run again.",
    },
    # 更新错误码（lst.contracts.updates.UpdateErrorCode）的说明，键为 update.error.<错误码去掉 update. 前缀>。
    "update.error.network": {
        "zh-Hans": "网络连接失败，请检查网络连接或代理设置后重试。",
        "en": "Network connection failed. Check your network connection or proxy settings and try again.",
    },
    "update.error.timeout": {
        "zh-Hans": "连接 GitHub 超时，请稍后重试。",
        "en": "Connecting to GitHub timed out. Try again later.",
    },
    "update.error.http": {
        "zh-Hans": "GitHub 拒绝了请求，可能是访问过于频繁，请稍后重试。",
        "en": "GitHub rejected the request, possibly because of rate limiting. Try again later.",
    },
    "update.error.bad_release": {
        "zh-Hans": "发布信息不完整或格式不对，请稍后重试，或到发布页面手动下载。",
        "en": "The release information is incomplete or malformed. Try again later or download it manually.",
    },
    "update.error.checksum": {
        "zh-Hans": "下载的安装包没有通过 SHA-256 校验，已经删除，请重试。",
        "en": "The downloaded installer failed the SHA-256 check and was deleted. Try again.",
    },
    "update.error.disk": {
        "zh-Hans": "无法写入下载目录，请检查磁盘空间和权限。",
        "en": "Cannot write to the download folder. Check free disk space and permissions.",
    },
    "update.error.not_installed": {
        "zh-Hans": "源码运行不能自动安装，请更新源码。",
        "en": "Running from source cannot install updates automatically. Update the source code instead.",
    },
    "update.error.launch_failed": {
        "zh-Hans": "无法启动安装程序，请到发布页面手动下载安装。",
        "en": "The installer could not be started. Download and install it manually from the release page.",
    },
    "update.error.internal": {
        "zh-Hans": "内部错误，请导出诊断信息并检查日志。",
        "en": "Internal error. Export diagnostics and check the logs.",
    },
}
"""更新服务的通知文案与更新错误码说明。业务模块不含界面文案，托盘负责弹出这些通知，所以登记在这里。"""

register_catalog(CATALOG)
register_catalog(UPDATE_CATALOG)
