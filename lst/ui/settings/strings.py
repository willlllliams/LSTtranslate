# ruff: noqa: E501  文案表里一条文案写在一行，方便对照中英文；折行后反而难读。
"""设置窗口的界面文案（简体中文与英文，需求 CFG-05）与显示用的格式化函数。导入本模块时登记文案。

按钮文案带 Qt 助记符 ``&``，按 Alt 加字母即可触发（需求 UI-07）。设置卡片的标题与说明按“设置项的位置”取键：

- 配置节里的设置项：``settings.f.<节>.<字段>.title`` 与 ``.desc``，嵌套字段用点连接，例如
  ``settings.f.bilingual.debounce_ms.title``；
- 引擎实例的参数：``settings.ef.<字段>.title`` 与 ``.desc``；
- 取值的显示名：``settings.choice.<节>.<字段>.<取值>``，引擎参数用 ``settings.choice.engine.<字段>.<取值>``。

没有登记文案的设置项显示字段名，不会因此崩溃。界面不显示技术细节（架构文档 10.3 节），所以文案里
不出现配置字段名、错误码和状态码。
"""

from datetime import UTC, datetime
from typing import Final

from lst.ui.i18n import Catalog, is_registered, register_catalog, tr


def _field(key: str, zh: tuple[str, str], en: tuple[str, str]) -> Catalog:
    """一个设置项的标题与说明。zh、en 都是 (标题, 说明)。"""
    return {
        f"{key}.title": {"zh-Hans": zh[0], "en": en[0]},
        f"{key}.desc": {"zh-Hans": zh[1], "en": en[1]},
    }


def _choices(prefix: str, values: dict[str, tuple[str, str]]) -> Catalog:
    return {f"{prefix}.{value}": {"zh-Hans": zh, "en": en} for value, (zh, en) in values.items()}


CATALOG: Final[Catalog] = {
    # 窗口与导航
    "settings.window.title": {"zh-Hans": "设置", "en": "Settings"},
    "settings.notice.rejected": {
        "zh-Hans": "有设置没有保存：{where}：{reason}",
        "en": "A setting was not saved: {where}: {reason}",
    },
    "settings.location.index": {"zh-Hans": "第 {n} 项", "en": "item {n}"},
    "settings.location.root": {"zh-Hans": "配置", "en": "settings"},
    # 分组标题
    "settings.section.language": {"zh-Hans": "语言", "en": "Language"},
    "settings.section.appearance": {"zh-Hans": "外观", "en": "Appearance"},
    "settings.section.ocr": {"zh-Hans": "文字识别", "en": "Text recognition"},
    "settings.section.ocr_features": {"zh-Hans": "识别内容", "en": "What to recognize"},
    "settings.section.preprocess": {"zh-Hans": "截图预处理", "en": "Screenshot preprocessing"},
    "settings.section.translation": {"zh-Hans": "翻译", "en": "Translation"},
    "settings.section.bilingual": {"zh-Hans": "双语窗口", "en": "Bilingual window"},
    "settings.section.models_dir": {"zh-Hans": "存放位置", "en": "Location"},
    "settings.section.stats": {"zh-Hans": "竞速统计", "en": "Race statistics"},
    # 通用控件
    "settings.default_value": {"zh-Hans": "默认", "en": "Default"},
    "settings.switch.on": {"zh-Hans": "开", "en": "On"},
    "settings.switch.off": {"zh-Hans": "关", "en": "Off"},
    "settings.error.prefix": {"zh-Hans": "未保存：{reason}", "en": "Not saved: {reason}"},
    "settings.validation.min": {"zh-Hans": "不能小于 {limit}", "en": "Must not be less than {limit}"},
    "settings.validation.gt": {"zh-Hans": "必须大于 {limit}", "en": "Must be greater than {limit}"},
    "settings.validation.max": {"zh-Hans": "不能大于 {limit}", "en": "Must not be greater than {limit}"},
    "settings.validation.lt": {"zh-Hans": "必须小于 {limit}", "en": "Must be less than {limit}"},
    "settings.validation.empty": {"zh-Hans": "不能为空", "en": "Must not be empty"},
    "settings.validation.required": {"zh-Hans": "必须填写", "en": "Required"},
    "settings.validation.number": {"zh-Hans": "应为数字", "en": "Must be a number"},
    "settings.validation.choice": {"zh-Hans": "取值不在可选范围内", "en": "Not one of the available values"},
    "settings.validation.text": {"zh-Hans": "应为文字", "en": "Must be text"},
    "settings.validation.other": {"zh-Hans": "取值无效", "en": "Invalid value"},
    "settings.browse": {"zh-Hans": "浏览(&B)…", "en": "&Browse…"},
    "settings.browse.title": {"zh-Hans": "选择目录", "en": "Choose a folder"},
    "settings.dir.placeholder": {"zh-Hans": "默认位置", "en": "Default location"},
    "settings.confirm.yes": {"zh-Hans": "确定(&Y)", "en": "&Yes"},
    "settings.confirm.no": {"zh-Hans": "取消(&N)", "en": "&No"},
    "settings.confirm.continue": {"zh-Hans": "继续(&C)", "en": "&Continue"},
    "settings.confirm.cancel": {"zh-Hans": "取消", "en": "Cancel"},
    "settings.pair.second": {"zh-Hans": "第二种语言", "en": "Second language"},
    # 热键录入
    "settings.hotkey.record": {
        "zh-Hans": "按下新的快捷键…（Esc 取消）",
        "en": "Press the new shortcut… (Esc cancels)",
    },
    "settings.hotkey.tip": {
        "zh-Hans": "点击后按下新的组合键，需要包含 Ctrl、Alt、Shift 或 Win 中的至少一个。键盘操作：按 Enter 或空格开始录入。",
        "en": "Click, then press the new combination; it needs at least one of Ctrl, Alt, Shift or Win. "
        "With the keyboard: press Enter or Space to start recording.",
    },
    "settings.hotkey.empty": {"zh-Hans": "未设置", "en": "Not set"},
    # 常规
    **_field(
        "settings.f.ui.language",
        ("界面语言", "切换后立即生效。"),
        ("Interface language", "Takes effect immediately."),
    ),
    **_choices(
        "settings.choice.ui.language",
        {
            "auto": ("跟随系统", "Follow system"),
            "zh-Hans": ("简体中文", "简体中文"),
            "en": ("English", "English"),
        },
    ),
    **_field(
        "settings.f.ui.theme",
        ("主题", "深色、浅色，或跟随系统设置，切换后立即生效。"),
        ("Theme", "Dark, light, or follow the system setting. Takes effect immediately."),
    ),
    **_choices(
        "settings.choice.ui.theme",
        {"system": ("跟随系统", "Follow system"), "light": ("浅色", "Light"), "dark": ("深色", "Dark")},
    ),
    # 热键
    "settings.hotkeys.intro": {
        "zh-Hans": "热键保存后立即生效，不需要重启。热键被其他程序占用时，托盘会弹出通知。",
        "en": "Hotkeys take effect as soon as they are saved. If another program already uses a hotkey, "
        "a tray notification tells you.",
    },
    **_field(
        "settings.f.hotkeys.capture",
        ("截屏翻译", "按下后框选屏幕区域并翻译。"),
        ("Capture and translate", "Select a screen region and translate it."),
    ),
    **_field(
        "settings.f.hotkeys.bilingual",
        ("显示/隐藏双语窗口", "按下后显示双语互译窗口，再按一次隐藏。"),
        ("Show/hide bilingual window", "Shows the bilingual translator; press again to hide it."),
    ),
    # 截屏与识别：OCR 引擎（需求 OCRE-01 至 OCRE-06）
    "settings.section.ocr_engines": {"zh-Hans": "OCR 引擎", "en": "OCR engines"},
    "settings.ocr_engines.intro": {
        "zh-Hans": "截屏翻译先用 OCR 引擎识别截图里的文字。每个引擎单独下载、卸载，同一时间只使用一个引擎。",
        "en": "Screenshot translation first reads the text in the screenshot with an OCR engine. "
        "Each engine is downloaded and removed separately, and one engine is used at a time.",
    },
    "settings.ocr_engines.missing": {
        "zh-Hans": "暂时读不到 OCR 引擎的状态，引擎的下载与切换不可用。",
        "en": "The OCR engine status is not available yet, so engines cannot be downloaded or switched.",
    },
    "settings.ocr_engine.windows.title": {"zh-Hans": "Windows 自带 OCR", "en": "Windows built-in OCR"},
    "settings.ocr_engine.windows.desc": {
        "zh-Hans": "随系统提供，识别速度快。只识别文字，不分析版面；可识别的语言取决于系统安装的 OCR 语言。",
        "en": "Comes with Windows and is fast. Reads text only, without layout; "
        "the languages depend on the OCR languages installed in Windows.",
    },
    "settings.ocr_engine.rapid.title": {"zh-Hans": "RapidOCR", "en": "RapidOCR"},
    "settings.ocr_engine.rapid.desc": {
        "zh-Hans": "轻量，按行给出位置，只有文本模式。",
        "en": "Lightweight. Gives the position of each line; text mode only.",
    },
    "settings.ocr_engine.paddle.title": {"zh-Hans": "PaddleOCR", "en": "PaddleOCR"},
    "settings.ocr_engine.paddle.desc": {
        "zh-Hans": "开源识别引擎，能分析标题、段落、表格和公式，译文按原有版面排列。",
        "en": "An open-source engine that analyses headings, paragraphs, tables and formulas, "
        "so the translation keeps the original layout.",
    },
    "settings.ocr_engine.features": {"zh-Hans": "能力：{features}", "en": "Capabilities: {features}"},
    "settings.ocr_engine.feature.text": {"zh-Hans": "文本", "en": "text"},
    "settings.ocr_engine.feature.layout": {"zh-Hans": "版面", "en": "layout"},
    "settings.ocr_engine.feature.table": {"zh-Hans": "表格", "en": "tables"},
    "settings.ocr_engine.feature.formula": {"zh-Hans": "公式", "en": "formulas"},
    "settings.ocr_engine.feature.separator": {"zh-Hans": "、", "en": ", "},
    "settings.ocr_engine.no_download": {"zh-Hans": "不需要下载", "en": "no download needed"},
    "settings.ocr_engine.download_size": {"zh-Hans": "需要下载约 {size}", "en": "about {size} to download"},
    "settings.ocr_engine.download_unknown": {"zh-Hans": "需要下载", "en": "needs a download"},
    "settings.ocr_engine.summary": {"zh-Hans": "{features}；{download}", "en": "{features}; {download}"},
    "settings.ocr_engine.languages": {
        "zh-Hans": "可识别的语言：{languages}",
        "en": "Recognized languages: {languages}",
    },
    "settings.ocr_engine.status.unavailable": {"zh-Hans": "不可用：{reason}", "en": "Unavailable: {reason}"},
    "settings.ocr_engine.status.not_installed": {"zh-Hans": "未安装", "en": "Not installed"},
    "settings.ocr_engine.status.queued": {"zh-Hans": "等待安装", "en": "Waiting to install"},
    "settings.ocr_engine.status.downloading": {
        "zh-Hans": "正在下载 {percent}%",
        "en": "Downloading {percent}%",
    },
    "settings.ocr_engine.status.downloading_unknown": {"zh-Hans": "正在下载…", "en": "Downloading…"},
    "settings.ocr_engine.status.installing": {
        "zh-Hans": "正在安装和校验…",
        "en": "Installing and verifying…",
    },
    "settings.ocr_engine.status.installed": {"zh-Hans": "已安装", "en": "Installed"},
    "settings.ocr_engine.status.builtin": {"zh-Hans": "可以使用", "en": "Available"},
    "settings.ocr_engine.status.loading": {"zh-Hans": "正在加载…", "en": "Loading…"},
    "settings.ocr_engine.status.ready": {"zh-Hans": "就绪", "en": "Ready"},
    "settings.ocr_engine.status.error": {"zh-Hans": "出错：{reason}", "en": "Error: {reason}"},
    "settings.ocr_engine.suggestion": {
        "zh-Hans": "处理建议：{suggestion}。",
        "en": "What to do: {suggestion}.",
    },
    "settings.ocr_engine.no_language.help": {
        "zh-Hans": "添加 OCR 语言：打开 Windows 设置 → 时间和语言 → 语言和区域，点要识别的语言右侧的“…”→“语言选项”，"
        "在“语言功能”里安装“光学字符识别”。装好后重新启动 LSTtranslate。",
        "en": "To add an OCR language, open Windows Settings → Time & language → Language & region, "
        "select “…” next to the language → Language options, and install “Optical character recognition” "
        "under Language features. Then restart LSTtranslate.",
    },
    "settings.ocr_engine.current": {"zh-Hans": "当前引擎", "en": "Current engine"},
    "settings.ocr_engine.current.name": {"zh-Hans": "{name}，当前引擎", "en": "{name}, current engine"},
    "settings.ocr_engine.use": {"zh-Hans": "设为当前引擎(&S)", "en": "&Set as current engine"},
    "settings.ocr_engine.download": {"zh-Hans": "下载(&D)", "en": "&Download"},
    "settings.ocr_engine.cancel": {"zh-Hans": "取消(&C)", "en": "&Cancel"},
    "settings.ocr_engine.retry": {"zh-Hans": "重试(&R)", "en": "&Retry"},
    "settings.ocr_engine.uninstall": {"zh-Hans": "卸载(&U)", "en": "&Uninstall"},
    "settings.ocr_engine.progress": {"zh-Hans": "{name}的安装进度", "en": "{name} installation progress"},
    "settings.ocr_engine.uninstall.confirm": {
        "zh-Hans": "卸载 {name}？它的运行环境与模型会被删除，之后需要重新下载才能使用。",
        "en": "Uninstall {name}? Its runtime and models will be deleted, and you will need to download it again to use it.",
    },
    "settings.ocr_engine.uninstall.confirm_current": {
        "zh-Hans": "卸载 {name}？它是当前引擎，卸载后截屏翻译不可用，直到另选一个引擎。"
        "它的运行环境与模型会被删除，之后需要重新下载才能使用。",
        "en": "Uninstall {name}? It is the current engine, so screenshot translation stops working until "
        "you choose another engine. Its runtime and models will be deleted, and you will need to download it again.",
    },
    "settings.ocr.paddle_only": {"zh-Hans": "仅 PaddleOCR 可用。", "en": "Only available with PaddleOCR."},
    "settings.ocr.language.unsupported": {
        "zh-Hans": "{name}（当前引擎不支持）",
        "en": "{name} (not supported by the current engine)",
    },
    "settings.ocr.language.windows": {
        "zh-Hans": "Windows 自带 OCR 只能识别系统已安装 OCR 功能的语言，这里只列出这些语言。"
        "自动：按 Windows 的显示语言选择。",
        "en": "Windows built-in OCR only reads languages whose OCR feature is installed, so only those are listed. "
        "Auto picks one from the Windows display language.",
    },
    "settings.ocr.language.rapid": {
        "zh-Hans": "RapidOCR 的识别模型覆盖中文、日文、英文和常见的拉丁字母语言，这里只列出这些语言。"
        "这些语言用同一个识别模型，自动与指定语言的识别效果相同。",
        "en": "RapidOCR's recognition model covers Chinese, Japanese, English and common Latin-script languages, "
        "so only those are listed. They share one model, so Auto works as well as choosing a language.",
    },
    # 截屏与识别
    **_field(
        "settings.f.ocr.enabled",
        ("启用文字识别", "关闭后不加载识别模型，截屏翻译不可用，可以节省内存。"),
        (
            "Enable text recognition",
            "When off, recognition models are not loaded and screenshot translation is unavailable. "
            "This saves memory.",
        ),
    ),
    **_field(
        "settings.f.ocr.mode",
        ("识别模式", "版面模式分析标题、表格、公式等结构；文本模式只识别文字，速度更快。"),
        (
            "Recognition mode",
            "Layout mode analyses headings, tables and formulas; text mode only reads text and is faster.",
        ),
    ),
    **_choices(
        "settings.choice.ocr.mode",
        {"structure": ("版面模式", "Layout mode"), "text": ("文本模式", "Text mode")},
    ),
    **_field(
        "settings.f.ocr.device",
        ("推理设备", "自动：有可用的 NVIDIA 显卡时使用显卡，否则使用 CPU。修改后识别组件会自动重新加载。"),
        (
            "Inference device",
            "Auto uses an NVIDIA GPU when one is available, otherwise the CPU. "
            "The recognizer reloads automatically after a change.",
        ),
    ),
    **_choices(
        "settings.choice.ocr.device",
        {"auto": ("自动", "Auto"), "cpu": ("CPU", "CPU"), "gpu": ("显卡（GPU）", "GPU")},
    ),
    **_field(
        "settings.f.ocr.language",
        (
            "识别语言",
            "自动使用同时覆盖中文、英文、日文的通用模型；指定语言时使用该语言的专用模型。",
        ),
        (
            "Recognition language",
            "Auto uses a general model covering Chinese, English and Japanese; "
            "a specific language uses its dedicated model.",
        ),
    ),
    "settings.choice.ocr.language.auto": {"zh-Hans": "自动", "en": "Auto"},
    **_field(
        "settings.f.ocr.table",
        ("表格识别", "还原表格的行列结构，逐个单元格翻译。会增加识别耗时。"),
        (
            "Table recognition",
            "Rebuilds table rows and columns and translates each cell. Adds recognition time.",
        ),
    ),
    **_field(
        "settings.f.ocr.formula",
        ("公式识别", "把公式识别为 LaTeX。会明显增加识别耗时。"),
        ("Formula recognition", "Reads formulas as LaTeX. Adds noticeable recognition time."),
    ),
    **_field(
        "settings.f.ocr.chart",
        ("图表识别", "把图表转换成数据表。只在显卡上可用，耗时很长。"),
        ("Chart recognition", "Converts charts to data tables. Only available on a GPU and very slow."),
    ),
    **_field(
        "settings.f.ocr.doc_orientation",
        ("文档方向校正", "识别前自动把旋转的页面转正。"),
        ("Document orientation", "Rotates a turned page upright before recognition."),
    ),
    **_field(
        "settings.f.ocr.doc_unwarping",
        ("文档去畸变", "识别前矫正拍摄造成的弯曲和透视变形。"),
        ("Document unwarping", "Corrects curvature and perspective distortion before recognition."),
    ),
    **_field(
        "settings.f.ocr.textline_orientation",
        ("文本行方向判断", "判断每行文字是否倒置。"),
        ("Text line orientation", "Detects whether each line of text is upside down."),
    ),
    **_field(
        "settings.f.ocr.grayscale",
        ("灰度化", "送入识别前转为灰度图。只影响识别，覆盖窗口的底图仍用原图。"),
        (
            "Grayscale",
            "Converts the image to grayscale before recognition. The overlay keeps the original image.",
        ),
    ),
    **_field(
        "settings.f.ocr.resize_max_side",
        (
            "缩放上限",
            "截图的宽或高超过这个值时，等比缩小后再识别。0 表示不缩放，其他取值不小于 256。",
        ),
        (
            "Maximum side length",
            "Screenshots wider or taller than this are scaled down before recognition. "
            "0 means no scaling; other values must be at least 256.",
        ),
    ),
    "settings.suffix.pixels": {"zh-Hans": " 像素", "en": " px"},
    "settings.special.no_scaling": {"zh-Hans": "不缩放", "en": "No scaling"},
    **_field(
        "settings.f.screenshot.target_lang",
        ("截屏翻译的目标语言", "截屏识别出的文字翻译成这种语言。源语言自动检测。"),
        (
            "Screenshot target language",
            "Recognized text is translated into this language. The source language is detected.",
        ),
    ),
    # 竞速
    **_field(
        "settings.f.bilingual.pair",
        ("互译语言", "检测到第一种语言就译成第二种，检测到第二种就译成第一种。两种语言必须不同。"),
        (
            "Language pair",
            "Text detected as the first language is translated into the second, and the other way round. "
            "The two languages must differ.",
        ),
    ),
    **_field(
        "settings.f.bilingual.debounce_ms",
        ("自动翻译的停顿时间", "输入停顿多久后自动开始翻译。"),
        ("Pause before translating", "How long to wait after typing stops before translating automatically."),
    ),
    "settings.suffix.ms": {"zh-Hans": " 毫秒", "en": " ms"},
    **_field(
        "settings.f.bilingual.layout",
        ("窗口排列", "输入区与译文区上下排列，或左右排列。"),
        ("Layout", "Place the input and translation areas one above the other, or side by side."),
    ),
    **_choices(
        "settings.choice.bilingual.layout",
        {"vertical": ("上下排列", "Stacked"), "horizontal": ("左右排列", "Side by side")},
    ),
    # 竞速统计
    "settings.stats.intro": {
        "zh-Hans": "统计只计入执行完并给出结果的请求，命中缓存和被取消的不计入，取消也不算失败。用它判断各引擎的快慢与可靠程度，调整优先级。",
        "en": "Only requests that ran to a result are counted; cache hits and cancelled requests are "
        "not, and a cancellation is not a failure. Use it to judge how fast and reliable each engine is and to adjust priorities.",
    },
    "settings.stats.table": {"zh-Hans": "各引擎的竞速统计", "en": "Race statistics per engine"},
    "settings.stats.col.engine": {"zh-Hans": "引擎", "en": "Engine"},
    "settings.stats.col.executed": {"zh-Hans": "执行段数", "en": "Segments run"},
    "settings.stats.col.wins": {"zh-Hans": "胜出次数", "en": "Wins"},
    "settings.stats.col.win_rate": {"zh-Hans": "胜出率", "en": "Win rate"},
    "settings.stats.col.p50": {"zh-Hans": "耗时中位数", "en": "Median time"},
    "settings.stats.col.p95": {"zh-Hans": "耗时 P95", "en": "P95 time"},
    "settings.stats.col.failure": {"zh-Hans": "失败率", "en": "Failure rate"},
    "settings.stats.empty": {
        "zh-Hans": "还没有统计数据。完成几次翻译后这里会显示各引擎的表现。",
        "en": "No statistics yet. Engine results appear here after a few translations.",
    },
    "settings.stats.none": {"zh-Hans": "—", "en": "—"},
    "settings.stats.ms": {"zh-Hans": "{value} 毫秒", "en": "{value} ms"},
    "settings.stats.seconds": {"zh-Hans": "{value} 秒", "en": "{value} s"},
    "settings.stats.reset": {"zh-Hans": "重置统计(&R)", "en": "&Reset statistics"},
    "settings.stats.reset.confirm": {
        "zh-Hans": "清空全部竞速统计？这不会影响翻译功能。",
        "en": "Clear all race statistics? Translation is not affected.",
    },
    "settings.cache.clear": {"zh-Hans": "清空译文缓存(&C)", "en": "&Clear translation cache"},
    "settings.cache.clear.confirm": {
        "zh-Hans": "清空译文缓存？之后相同的文字需要重新翻译，计费引擎会重新产生费用。",
        "en": "Clear the translation cache? The same text must then be translated again, which costs money on paid engines.",
    },
    "settings.cache.cleared": {
        "zh-Hans": "已清空译文缓存（{count} 条）。",
        "en": "Translation cache cleared ({count} entries).",
    },
    "settings.confirm.title": {"zh-Hans": "请确认", "en": "Please confirm"},
    # 翻译引擎
    "settings.engines.intro": {
        "zh-Hans": "靠前的引擎优先级更高：同一段文字有多个译文时，显示优先级最高的那个。拖动列表项，或选中后用“上移”“下移”调整顺序。勾选表示启用。",
        "en": "Engines higher in the list have higher priority: when several engines translate the same text, "
        "the highest-priority result is shown. Drag items, or select one and use Move up / Move down. "
        "Tick an engine to enable it.",
    },
    "settings.engines.list": {"zh-Hans": "翻译引擎实例", "en": "Translation engine instances"},
    "settings.engines.add": {"zh-Hans": "添加(&A)…", "en": "&Add…"},
    "settings.engines.remove": {"zh-Hans": "删除(&D)", "en": "&Delete"},
    "settings.engines.up": {"zh-Hans": "上移(&U)", "en": "Move &up"},
    "settings.engines.down": {"zh-Hans": "下移(&W)", "en": "Move do&wn"},
    "settings.engines.test": {"zh-Hans": "测试连接(&T)", "en": "&Test connection"},
    "settings.engines.row": {"zh-Hans": "{rank}. {name}", "en": "{rank}. {name}"},
    "settings.engines.error.tip": {"zh-Hans": "{reason}。{suggestion}", "en": "{reason}. {suggestion}"},
    "settings.engines.row.status": {
        "zh-Hans": "{rank}. {name}（{status}）",
        "en": "{rank}. {name} ({status})",
    },
    "settings.engines.none": {
        "zh-Hans": "还没有翻译引擎。点击“添加”创建一个。",
        "en": "There are no engines yet. Click Add to create one.",
    },
    "settings.engines.select": {
        "zh-Hans": "选中一个引擎实例后，可以在这里修改它的参数。",
        "en": "Select an engine to edit its settings here.",
    },
    "settings.engines.editor": {"zh-Hans": "{name} 的参数", "en": "Settings of {name}"},
    "settings.engines.remove.confirm": {
        "zh-Hans": "删除引擎“{name}”？它保存的 API key 也会一并删除。",
        "en": "Delete the engine “{name}”? Its saved API key is deleted too.",
    },
    "settings.engines.test.running": {"zh-Hans": "正在测试…", "en": "Testing…"},
    "settings.engines.test.ok": {"zh-Hans": "连接正常（{ms} 毫秒）", "en": "Connection works ({ms} ms)"},
    "settings.engines.test.ok_plain": {"zh-Hans": "连接正常", "en": "Connection works"},
    "settings.engines.test.failed": {
        "zh-Hans": "连接失败：{reason}。{suggestion}",
        "en": "Connection failed: {reason}. {suggestion}",
    },
    "settings.engines.test.tip": {
        "zh-Hans": "用当前保存的设置翻译一个很短的词。在线引擎会产生一次很小的请求。",
        "en": "Translates one short word with the saved settings. Online engines receive one tiny request.",
    },
    "settings.engines.group.basic": {"zh-Hans": "基本", "en": "Basics"},
    "settings.engines.group.params": {"zh-Hans": "参数", "en": "Parameters"},
    "settings.engines.group.advanced": {"zh-Hans": "高级", "en": "Advanced"},
    "settings.engines.apikey.title": {"zh-Hans": "API key", "en": "API key"},
    "settings.engines.apikey.desc": {
        "zh-Hans": "保存在 Windows 凭据管理器里，这里不会显示已保存的 key。输入新的 key 会替换旧的。",
        "en": "Stored in Windows Credential Manager. The saved key is never shown here; entering a new one replaces it.",
    },
    "settings.engines.apikey.placeholder": {"zh-Hans": "输入新的 API key", "en": "Enter a new API key"},
    "settings.engines.apikey.set": {"zh-Hans": "已设置", "en": "Set"},
    "settings.engines.apikey.unset": {"zh-Hans": "未设置", "en": "Not set"},
    "settings.engines.apikey.unknown": {
        "zh-Hans": "启用后显示状态",
        "en": "Shown once the engine is enabled",
    },
    "settings.engines.apikey.optional": {"zh-Hans": "（可选）", "en": " (optional)"},
    "settings.engines.apikey.clear": {"zh-Hans": "清除 key(&K)", "en": "Clear &key"},
    "settings.engines.apikey.name": {"zh-Hans": "{name} 的 API key", "en": "API key of {name}"},
    "settings.engines.status.line": {"zh-Hans": "状态：{status}", "en": "Status: {status}"},
    **_choices(
        "settings.kind",
        {
            "openai_compat": ("OpenAI 兼容的大语言模型", "OpenAI-compatible language model"),
            "deepl": ("DeepL", "DeepL"),
            "google": ("Google 翻译", "Google Translate"),
            "baidu": ("百度翻译", "Baidu Translate"),
            "local_nmt": ("本地翻译模型", "Local translation models"),
            "dictionary": ("词典（逐词粗译）", "Dictionary (word-by-word)"),
        },
    ),
    **_choices(
        "settings.choice.engine.provider",
        {
            "deepseek": ("DeepSeek", "DeepSeek"),
            "openai": ("OpenAI", "OpenAI"),
            "ollama": ("Ollama（本地）", "Ollama (local)"),
            "llamacpp": ("llama.cpp 服务（本地）", "llama.cpp server (local)"),
            "generic": ("其他兼容服务", "Other compatible service"),
        },
    ),
    # 添加引擎对话框
    "settings.add.title": {"zh-Hans": "添加翻译引擎", "en": "Add a translation engine"},
    "settings.add.kind": {"zh-Hans": "引擎类型", "en": "Engine type"},
    "settings.add.provider": {"zh-Hans": "服务", "en": "Service"},
    "settings.add.ok": {"zh-Hans": "添加(&A)", "en": "&Add"},
    "settings.add.cancel": {"zh-Hans": "取消(&C)", "en": "&Cancel"},
    "settings.add.hint": {
        "zh-Hans": "添加后可以在引擎列表里继续修改所有参数。API key 可以现在填写，也可以稍后填写。",
        "en": "You can change every parameter in the engine list afterwards. The API key can be entered now or later.",
    },
    "settings.add.invalid": {"zh-Hans": "{field}：{reason}", "en": "{field}: {reason}"},
    # 引擎实例的参数
    **_field(
        "settings.ef.name",
        ("名称", "显示在引擎列表、托盘和双语窗口里。"),
        ("Name", "Shown in the engine list, the tray and the bilingual window."),
    ),
    **_field(
        "settings.ef.enabled",
        ("启用", "关闭后这个实例不参与竞速，双语窗口的服务列表里也不再列出它。"),
        ("Enabled", "A disabled engine does not join the race and is not listed in the bilingual window."),
    ),
    **_field(
        "settings.ef.color",
        ("标识颜色", "引擎设置列表图标使用的颜色，写成 #RRGGBB；留空则自动分配。"),
        (
            "Engine icon colour",
            "Colour used for icons in the engine settings list, written as #RRGGBB. Leave empty to assign one automatically.",
        ),
    ),
    **_field(
        "settings.ef.timeout_s",
        ("单个任务的时限", "超过这个时间还没有结果，该任务记为超时失败。单位秒。"),
        ("Time limit per job", "A job without a result after this long counts as timed out. In seconds."),
    ),
    **_field(
        "settings.ef.max_concurrency",
        ("最大并发数", "同时发给这个引擎的任务数。"),
        ("Maximum concurrency", "How many jobs are sent to this engine at the same time."),
    ),
    **_field(
        "settings.ef.max_retries",
        ("重试次数", "网络错误、限流或服务端错误时最多重试几次。"),
        ("Retries", "How many times to retry after a network error, rate limit or server error."),
    ),
    **_field(
        "settings.ef.provider",
        ("服务", "决定默认的接口行为和是否计费。"),
        ("Service", "Decides default behaviour and whether the engine is billed."),
    ),
    **_field(
        "settings.ef.base_url",
        ("接口地址", "OpenAI 兼容接口的地址，例如 https://api.deepseek.com。"),
        ("Endpoint", "Address of the OpenAI-compatible API, for example https://api.deepseek.com."),
    ),
    **_field("settings.ef.model", ("模型名", "要使用的模型。"), ("Model", "The model to use.")),
    **_field(
        "settings.ef.temperature",
        ("温度", "数值越低译文越稳定，越高越有变化。"),
        ("Temperature", "Lower values give steadier translations, higher values more variety."),
    ),
    **_field(
        "settings.ef.system_prompt",
        ("提示词", "给模型的翻译指示；留空使用内置提示词。"),
        ("Prompt", "Instructions for the model. Leave empty to use the built-in prompt."),
    ),
    **_field(
        "settings.ef.max_batch_segments",
        ("每批最多段数", "一次请求最多包含的段数。"),
        ("Segments per request", "The most segments one request may contain."),
    ),
    **_field(
        "settings.ef.max_batch_chars",
        ("每批最多字符数", "一次请求最多包含的字符数。"),
        ("Characters per request", "The most characters one request may contain."),
    ),
    **_field(
        "settings.ef.first_token_timeout_s",
        ("首个结果时限", "等待第一段结果的时间；留空按服务类型取默认值。单位秒。"),
        (
            "First-result time limit",
            "How long to wait for the first result; empty uses the service default. In seconds.",
        ),
    ),
    **_field(
        "settings.ef.structured_output",
        ("结构化输出", "让模型按固定格式返回译文。自动即可，服务不支持时再改。"),
        ("Structured output", "Makes the model answer in a fixed format. Auto works for most services."),
    ),
    **_choices(
        "settings.choice.engine.structured_output",
        {
            "auto": ("自动", "Auto"),
            "json_schema": ("严格模式", "Strict schema"),
            "json_object": ("JSON 对象", "JSON object"),
        },
    ),
    **_field(
        "settings.ef.send_image",
        (
            "附带截图",
            "截屏翻译时把截图连同识别出的文字一起发给模型，帮助它理解上下文、纠正识别错误。"
            "开启后，截图会发送给该服务。模型不支持图片输入时，该实例的翻译会失败。双语窗口不发送图片。",
        ),
        (
            "Send screenshot",
            "Sends the screenshot together with the recognized text so the model can use the context and "
            "fix recognition errors. When on, screenshots are sent to this service. Translation fails if "
            "the model does not accept images. The bilingual window never sends images.",
        ),
    ),
    **_field(
        "settings.ef.image_max_side",
        (
            "截图尺寸上限",
            "发送前把截图等比缩小，长边不超过这个像素数，以节省费用。只在开启“附带截图”时可用。",
        ),
        (
            "Screenshot size limit",
            "The screenshot is scaled down so its longer side is at most this many pixels, which saves cost. "
            "Only available when “Send screenshot” is on.",
        ),
    ),
    **_field(
        "settings.ef.languages",
        ("支持的语言", "用逗号分隔的语言代码，例如 zh-Hans, en；留空表示不限制。"),
        (
            "Supported languages",
            "Comma-separated language codes, for example zh-Hans, en. Leave empty for no limit.",
        ),
    ),
    **_field(
        "settings.ef.formality",
        ("正式程度", "目标语言支持时，让译文更正式或更随意。"),
        ("Formality", "Makes the translation more or less formal where the target language supports it."),
    ),
    **_choices(
        "settings.choice.engine.formality",
        {
            "default": ("默认", "Default"),
            "more": ("更正式", "More formal"),
            "less": ("更随意", "Less formal"),
            "prefer_more": ("倾向正式", "Prefer formal"),
            "prefer_less": ("倾向随意", "Prefer informal"),
        },
    ),
    **_field(
        "settings.ef.appid",
        ("应用 ID", "百度翻译开放平台里的应用 ID（APP ID）。"),
        ("App ID", "The app ID from the Baidu Translate open platform."),
    ),
    **_field(
        "settings.ef.tier",
        ("服务版本", "决定请求的限速。"),
        ("Plan", "Decides the request rate limit."),
    ),
    **_choices(
        "settings.choice.engine.tier",
        {
            "standard": ("标准版", "Standard"),
            "advanced": ("高级版", "Advanced"),
            "premium": ("尊享版", "Premium"),
        },
    ),
    **_field(
        "settings.ef.processing_timeout_s",
        ("单批处理时限", "本地模型处理一批文字的时限，超时会重启翻译组件。单位秒。"),
        (
            "Processing limit per batch",
            "If the local model takes longer, the translation component restarts. In seconds.",
        ),
    ),
    **_field(
        "settings.ef.pivot_via_english",
        (
            "经英语中转",
            "两种语言都不是英语时，先用“源语言→英语”的词典，再用“英语→目标语言”的词典。"
            "两次逐词替换的误差会叠加，译文更粗糙。简体与繁体中文之间不中转。",
        ),
        (
            "Pivot through English",
            "When neither language is English, translates with the source → English dictionary and then the "
            "English → target dictionary. The errors of both passes add up, so the result is rougher. "
            "Simplified and Traditional Chinese are never pivoted.",
        ),
    ),
    # 模型
    **_field(
        "settings.f.nmt.model_dir",
        (
            "本地翻译模型的单独目录",
            "通常留空：本地翻译模型放在模型目录（常规 › 存储位置）下的 nmt 子目录里，输入框里的灰字就是这个位置。"
            "要直接使用别处已有的 .argosmodel 文件时，在这里指定它们所在的目录；更改模型目录时不会移动这里的文件。",
        ),
        (
            "Separate folder for local translation models",
            "Usually left empty: local translation models are kept in the nmt subfolder of the model folder "
            "(General › Storage locations), shown in grey in the box. To use .argosmodel files that already exist "
            "elsewhere, set their folder here; changing the model folder does not move the files in it.",
        ),
    ),
    "settings.models.intro": {
        "zh-Hans": "本地翻译模型只需联网下载一次，之后翻译不再联网。下载和删除立即生效。",
        "en": "Local translation models are downloaded once; translating with them needs no network afterwards. "
        "Downloads and deletions take effect immediately.",
    },
    "settings.models.list": {"zh-Hans": "本地翻译模型", "en": "Local translation models"},
    "settings.models.filter": {"zh-Hans": "筛选语言", "en": "Filter languages"},
    "settings.models.filter.placeholder": {
        "zh-Hans": "输入语言名称筛选…",
        "en": "Type a language to filter…",
    },
    "settings.models.installed_only": {
        "zh-Hans": "只显示已安装的模型(&I)",
        "en": "Show &installed models only",
    },
    "settings.models.col.model": {"zh-Hans": "模型", "en": "Model"},
    "settings.models.col.size": {"zh-Hans": "大小", "en": "Size"},
    "settings.models.col.status": {"zh-Hans": "状态", "en": "Status"},
    "settings.models.download": {"zh-Hans": "下载(&D)", "en": "&Download"},
    "settings.models.cancel": {"zh-Hans": "取消下载(&C)", "en": "&Cancel download"},
    "settings.models.delete": {"zh-Hans": "删除(&E)", "en": "D&elete"},
    "settings.models.refresh": {"zh-Hans": "刷新列表(&R)", "en": "&Refresh list"},
    "settings.models.delete.confirm": {
        "zh-Hans": "删除模型“{name}”？之后需要重新下载才能使用。",
        "en": "Delete the model “{name}”? You will need to download it again to use it.",
    },
    "settings.models.delete.confirm_many": {
        "zh-Hans": "删除选中的 {count} 个模型？之后需要重新下载才能使用。",
        "en": "Delete the {count} selected models? You will need to download them again to use them.",
    },
    "settings.models.index.loading": {
        "zh-Hans": "正在获取可下载的模型列表…",
        "en": "Fetching the list of available models…",
    },
    "settings.models.index.failed": {
        "zh-Hans": "无法获取模型列表：{reason}",
        "en": "Could not fetch the model list: {reason}",
    },
    "settings.models.index.not_loaded": {
        "zh-Hans": "目前只列出了已安装的模型。点击“刷新列表”获取可下载的模型（需要联网）。",
        "en": "Only installed models are listed. Click Refresh list to fetch downloadable models (needs a network connection).",
    },
    "settings.models.empty": {"zh-Hans": "没有符合条件的模型。", "en": "No models match."},
    # 关于与诊断
    "settings.about.name": {"zh-Hans": "LSTtranslate", "en": "LSTtranslate"},
    "settings.about.version": {"zh-Hans": "版本 {version}", "en": "Version {version}"},
    "settings.about.tagline": {
        "zh-Hans": "多引擎竞速的屏幕翻译工具",
        "en": "Racing screen translation across multiple engines",
    },
    "settings.about.workers": {"zh-Hans": "后台组件", "en": "Background components"},
    "settings.about.workers.none": {"zh-Hans": "暂无组件状态", "en": "No component status yet"},
    "settings.about.worker.line": {"zh-Hans": "{name}：{state}", "en": "{name}: {state}"},
    "settings.about.worker.crashed": {
        "zh-Hans": "进程崩溃，正在自动重启",
        "en": "The process crashed and is restarting",
    },
    "settings.about.worker.failed": {
        "zh-Hans": "多次崩溃或无法启动，已停止自动重启。检查设置后点“重新启动”，仍然失败时导出诊断信息",
        "en": "It crashed repeatedly or could not start and is no longer restarted. Check the settings and "
        "click Restart; if it still fails, export diagnostics",
    },
    "settings.about.worker.detail": {"zh-Hans": "{state}：{detail}", "en": "{state}: {detail}"},
    "settings.about.worker.restart": {"zh-Hans": "重新启动", "en": "Restart"},
    "settings.about.worker.restart.name": {"zh-Hans": "重新启动{name}", "en": "Restart {name}"},
    # 关于与诊断：应用更新（需求 UPD-01 至 UPD-06）
    "settings.section.updates": {"zh-Hans": "更新", "en": "Updates"},
    "settings.update.current": {"zh-Hans": "当前版本 {version}", "en": "Current version {version}"},
    "settings.update.current.unknown": {"zh-Hans": "当前版本未知", "en": "Current version unknown"},
    "settings.update.missing": {
        "zh-Hans": "更新功能暂时不可用。",
        "en": "Updates are not available right now.",
    },
    "settings.update.status.idle": {"zh-Hans": "还没有检查过新版本。", "en": "Not checked for updates yet."},
    "settings.update.status.checking": {"zh-Hans": "正在检查新版本…", "en": "Checking for updates…"},
    "settings.update.status.up_to_date": {"zh-Hans": "已是最新版本。", "en": "You are up to date."},
    "settings.update.status.available": {
        "zh-Hans": "发现新版本 {version}，发布于 {date}。",
        "en": "Version {version} is available, released on {date}.",
    },
    "settings.update.status.available_pre": {
        "zh-Hans": "发现新版本 {version}（预发布版本），发布于 {date}。",
        "en": "Version {version} (pre-release) is available, released on {date}.",
    },
    "settings.update.status.downloading": {
        "zh-Hans": "正在下载 {version}：{percent}%",
        "en": "Downloading {version}: {percent}%",
    },
    "settings.update.status.downloading_unknown": {
        "zh-Hans": "正在下载 {version}…",
        "en": "Downloading {version}…",
    },
    "settings.update.status.ready": {
        "zh-Hans": "{version} 已下载并通过校验。安装时程序会退出，装完后自动重新启动。",
        "en": "Version {version} is downloaded and verified. The program exits during installation "
        "and restarts afterwards.",
    },
    "settings.update.status.installing": {
        "zh-Hans": "正在启动安装程序，程序即将退出…",
        "en": "Starting the installer; the program is about to exit…",
    },
    "settings.update.status.error": {
        "zh-Hans": "出错：{reason}。处理建议：{suggestion}。",
        "en": "Error: {reason}. What to do: {suggestion}.",
    },
    "settings.update.details.separator": {"zh-Hans": "；", "en": "; "},
    "settings.update.last_checked": {"zh-Hans": "上次检查：{time}", "en": "Last checked: {time}"},
    "settings.update.size": {"zh-Hans": "安装包约 {size}", "en": "Installer about {size}"},
    "settings.update.source": {
        "zh-Hans": "从源码运行时只检查新版本，不下载也不安装。更新源码后重新运行即可。",
        "en": "When running from source, updates are only checked, never downloaded or installed. "
        "Update the source code and run again.",
    },
    "settings.update.feedback.up_to_date": {
        "zh-Hans": "检查完成：{version} 已是最新版本。",
        "en": "Check finished: {version} is the latest version.",
    },
    "settings.update.feedback.done": {"zh-Hans": "检查完成：", "en": "Check finished: "},
    "settings.update.feedback.failed": {"zh-Hans": "检查失败：{reason}。", "en": "Check failed: {reason}."},
    "settings.update.check": {"zh-Hans": "检查更新(&K)", "en": "Chec&k for updates"},
    "settings.update.download": {"zh-Hans": "下载(&D)", "en": "&Download"},
    "settings.update.install": {"zh-Hans": "安装并重启(&I)", "en": "&Install and restart"},
    "settings.update.notes": {"zh-Hans": "查看发布说明(&N)", "en": "Release &notes"},
    "settings.update.progress": {"zh-Hans": "更新的下载进度", "en": "Update download progress"},
    "settings.update.install.confirm": {
        "zh-Hans": "安装 {version}？程序会立即退出，安装完成后自动重新启动。",
        "en": "Install version {version}? The program exits now and restarts after installation.",
    },
    **_field(
        "settings.f.updates.auto_check",
        (
            "自动检查更新",
            "启动时以及之后按下面的间隔，向 GitHub 查询有没有新版本。只查询，不会自动下载。",
        ),
        (
            "Check for updates automatically",
            "Asks GitHub for a new version at startup and then at the interval below. "
            "It only checks and never downloads by itself.",
        ),
    ),
    **_field(
        "settings.f.updates.check_interval_hours",
        ("检查间隔", "两次自动检查之间至少相隔多久。"),
        ("Check interval", "The minimum time between two automatic checks."),
    ),
    "settings.suffix.hours": {"zh-Hans": " 小时", "en": " h"},
    **_field(
        "settings.f.updates.include_prerelease",
        (
            "接收预发布版本",
            "把预发布版本也算作可用的新版本。目前发布的版本都是预发布版本，关闭后检查不到新版本。",
        ),
        (
            "Include pre-releases",
            "Counts pre-release versions as available updates. All current releases are pre-releases, "
            "so with this off no update is found.",
        ),
    ),
    "settings.about.diagnostics": {"zh-Hans": "诊断", "en": "Diagnostics"},
    "settings.about.export": {"zh-Hans": "导出诊断信息(&E)…", "en": "&Export diagnostics…"},
    "settings.about.export.tip": {
        "zh-Hans": "此功能尚未提供。诊断信息会包含去除 API key 的配置、版本信息和最近的日志。",
        "en": "Not available yet. Diagnostics will contain the settings without API keys, version information and recent logs.",
    },
    "settings.worker.stopped": {"zh-Hans": "未启用", "en": "Disabled"},
    "settings.worker.loading": {"zh-Hans": "加载中", "en": "Loading"},
    "settings.worker.ready": {"zh-Hans": "就绪", "en": "Ready"},
    "settings.worker.busy": {"zh-Hans": "忙碌", "en": "Busy"},
    "settings.worker.error": {"zh-Hans": "出错", "en": "Error"},
}

register_catalog(CATALOG)


def has_text(key: str) -> bool:
    """文案键是否已经登记：查全部界面模块登记的文案，设置窗口的子文案表（strings_free、strings_ocr 等）也算在内。"""
    return is_registered(key)


def text_or(key: str, fallback: str) -> str:
    """有文案时返回当前语言的文案，否则返回 fallback。"""
    return tr(key) if has_text(key) else fallback


def format_duration(milliseconds: float | None) -> str:
    """耗时的显示文本：不足 1 秒用毫秒，否则用秒；没有数据时显示“—”。"""
    if milliseconds is None:
        return tr("settings.stats.none")
    if milliseconds >= 1000:
        return tr("settings.stats.seconds", value=f"{milliseconds / 1000:.2f}")
    return tr("settings.stats.ms", value=f"{milliseconds:.0f}")


def format_percent(fraction: float | None) -> str:
    """比例的显示文本，例如 0.625 → “62.5%”；没有数据时显示“—”。"""
    if fraction is None:
        return tr("settings.stats.none")
    return f"{fraction * 100:.1f}%".replace(".0%", "%")


def format_timestamp(value: str | None, *, with_time: bool = True) -> str:
    """ISO 8601 时间的显示文本，换算成本机时区，例如 2026-10-22 14:05；只要日期时为 2026-10-22。

    没有时区的时间按 UTC 处理（GitHub 与更新服务给出的都是 UTC）。无法解析时原样返回。
    """
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    local = moment.astimezone()
    return local.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")
