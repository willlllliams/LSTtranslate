# ruff: noqa: E501  文案表里一条文案写在一行，方便对照中英文。
"""设置窗口两级导航的文案，以及导航拆分后各子页新增的文案。导入本模块时登记文案。

菜单项的名称按页面标识取键：``settings.nav.<页面标识>``，一级项用 ``general`` 这样的名字，
二级项用 ``capture.paddle`` 这样的形式。
"""

from typing import Final

from lst.ui.i18n import Catalog, register_catalog


def _pair(zh: str, en: str) -> dict[str, str]:
    return {"zh-Hans": zh, "en": en}


CATALOG: Final[Catalog] = {
    "settings.nav.label": _pair("设置页面", "Settings pages"),
    "settings.nav.tip": _pair("{name}（Ctrl+{number}）", "{name} (Ctrl+{number})"),
    # 一级
    "settings.nav.general": _pair("常规", "General"),
    "settings.nav.hotkeys": _pair("热键与键位", "Hotkeys & bindings"),
    "settings.nav.capture": _pair("截屏与识别", "Capture & recognition"),
    "settings.nav.engines": _pair("翻译引擎", "Translation engines"),
    "settings.nav.race": _pair("竞速", "Racing"),
    "settings.nav.models": _pair("模型", "Models"),
    "settings.nav.about": _pair("关于与诊断", "About & diagnostics"),
    # 热键与键位的二级：全局热键，以及三个窗口各自的键位
    "settings.nav.hotkeys.global": _pair("全局热键", "Global hotkeys"),
    "settings.nav.hotkeys.capture": _pair("截屏蒙版", "Capture overlay"),
    "settings.nav.hotkeys.overlay": _pair("覆盖窗口", "Translation overlay"),
    "settings.nav.hotkeys.bilingual": _pair("双语窗口", "Bilingual window"),
    # 截屏与识别的二级
    "settings.nav.capture.engines": _pair("OCR 引擎", "OCR engines"),
    "settings.nav.capture.windows": _pair("Windows OCR", "Windows OCR"),
    "settings.nav.capture.rapid": _pair("RapidOCR", "RapidOCR"),
    "settings.nav.capture.paddle": _pair("PaddleOCR", "PaddleOCR"),
    "settings.nav.capture.normalize": _pair("结构归一化", "Structure normalization"),
    "settings.nav.capture.options": _pair("截图与识别选项", "Capture & recognition options"),
    # 模型的二级
    "settings.nav.models.nmt": _pair("本地翻译模型", "Local translation models"),
    "settings.nav.models.gpu": _pair("显卡加速", "GPU acceleration"),
    "settings.nav.models.dicts": _pair("词典", "Dictionaries"),
    # OCR 引擎子页
    "settings.section.ocr_runtime": _pair("运行环境", "Runtime"),
    "settings.section.ocr_pipeline": _pair("识别流水线", "Recognition pipeline"),
    "settings.ocr_engine_page.goto": _pair("转到 OCR 引擎页(&G)", "&Go to OCR engines"),
    "settings.ocr_engine_page.not_installed.title": _pair("{name} 尚未安装", "{name} is not installed"),
    "settings.ocr_engine_page.not_installed": _pair(
        "请先在“OCR 引擎”页下载 {name}。下载完成后回到这里选择模型、调整参数。",
        "Download {name} on the OCR engines page first. Then come back here to choose models and adjust parameters.",
    ),
    "settings.ocr_engine_page.empty": _pair(
        "{name} 没有可选的模型和参数。识别语言在“截图与识别选项”页设置。",
        "{name} has no models or parameters to choose. The recognition language is set on the Capture & recognition options page.",
    ),
    "settings.ocr_stage.off": _pair(
        "已关闭，识别时跳过这一步。", "Turned off; this step is skipped during recognition."
    ),
    "settings.ocr_stage.toggle": _pair("启用{stage}", "Enable {stage}"),
    "settings.ocr_normalize.missing": _pair(
        "没有可用的“{name}”设置。", "No “{name}” settings are available."
    ),
    "settings.f.ocr.region.title": _pair("版面区域检测", "Layout region detection"),
    "settings.f.ocr.region.desc": _pair(
        "先找出栏、独立文章这类大区域，用来确定阅读顺序。",
        "Finds large regions such as columns or separate articles to work out the reading order.",
    ),
}

register_catalog(CATALOG)
