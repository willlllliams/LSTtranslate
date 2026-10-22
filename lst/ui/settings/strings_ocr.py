# ruff: noqa: E501  文案表里一条文案写在一行，方便对照中英文；折行后反而难读。
"""截屏与识别页 OCR 引擎管理部分的界面文案（需求 OCRE-07 至 OCRE-09、OCR-11、CFG-05）。导入本模块时登记文案。

本模块只登记新增的文案，引擎卡片原有的文案仍在 strings.py。键的约定：

- 运行环境版本：``settings.ocr_variant.<版本>``，推荐理由 ``settings.ocr_variant.reason.<版本>``；
- 流水线环节：``settings.ocr_stage.<环节>.title`` 与 ``.desc``；
- 各引擎配置节（``ocr_windows``、``ocr_rapid``、``ocr_paddle``、``ocr_normalize``）的参数按字段名取键：
  ``settings.ocrf.<字段>.title`` 与 ``.desc``，只登记英文（``PARAM_TEXTS``）。中文界面用模型里的
  title、description，后端改了中文说明界面跟着变；英文界面没有登记的字段也退回这两项。

界面不显示技术细节（架构文档 10.3 节），文案里不出现配置字段名与错误码。
"""

from typing import Final

from lst.ui.i18n import Catalog, register_catalog, tr
from lst.ui.settings import strings


def _pair(zh: str, en: str) -> dict[str, str]:
    return {"zh-Hans": zh, "en": en}


def _stage(stage: str, zh: tuple[str, str], en: tuple[str, str]) -> Catalog:
    """一个流水线环节的名称与说明。"""
    return {
        f"settings.ocr_stage.{stage}.title": _pair(zh[0], en[0]),
        f"settings.ocr_stage.{stage}.desc": _pair(zh[1], en[1]),
    }


CATALOG: Final[Catalog] = {
    # 运行环境版本（OCRE-07）
    "settings.ocr_variant.cpu": _pair("CPU", "CPU"),
    "settings.ocr_variant.directml": _pair("DirectML", "DirectML"),
    "settings.ocr_variant.cuda": _pair("CUDA", "CUDA"),
    "settings.ocr_variant.hint.cpu": _pair("不需要显卡", "no graphics card needed"),
    "settings.ocr_variant.hint.directml": _pair("NVIDIA、AMD、Intel 显卡", "NVIDIA, AMD or Intel graphics"),
    "settings.ocr_variant.hint.cuda": _pair("NVIDIA 显卡", "NVIDIA graphics"),
    "settings.ocr_variant.option": _pair("{variant} · {hint}", "{variant} · {hint}"),
    "settings.ocr_variant.reason.cpu": _pair(
        "没有检测到这个引擎能用的显卡", "no graphics card this engine can use was found"
    ),
    "settings.ocr_variant.reason.directml": _pair(
        "检测到支持 DirectX 12 的显卡", "a DirectX 12 graphics card was found"
    ),
    "settings.ocr_variant.reason.cuda": _pair("检测到 NVIDIA 显卡", "an NVIDIA graphics card was found"),
    "settings.ocr_variant.recommended": _pair(
        "推荐：{variant}（{reason}）", "Recommended: {variant} ({reason})"
    ),
    "settings.ocr_variant.option_recommended": _pair("{option} · 推荐", "{option} · recommended"),
    "settings.ocr_variant.label": _pair("运行环境", "Runtime"),
    "settings.ocr_variant.combo": _pair("{name}的运行环境版本", "{name} runtime version"),
    "settings.ocr_variant.help": _pair(
        "CPU 版在任何电脑上都能用；GPU 版识别更快，下载也更大。",
        "The CPU version works on any PC; the GPU versions recognize faster and are larger to download.",
    ),
    "settings.ocr_variant.recommend_help": _pair("{recommendation}。{help}", "{recommendation}. {help}"),
    "settings.ocr_variant.installed": _pair("{status}（{variant}）", "{status} ({variant})"),
    "settings.ocr_variant.change": _pair("更换版本(&V)…", "Change &version…"),
    "settings.ocr_variant.change.confirm": _pair(
        "把 {name} 的运行环境从 {old} 换成 {new}？\n\n这会重新下载并安装运行环境，已下载的模型保留。",
        "Switch the {name} runtime from {old} to {new}?\n\nThe runtime is downloaded and installed again; downloaded models are kept.",
    ),
    "settings.ocr_variant.change.confirm_current": _pair(
        "把 {name} 的运行环境从 {old} 换成 {new}？\n\n这会重新下载并安装运行环境，已下载的模型保留。它是当前引擎，重新安装期间截屏翻译暂时不可用。",
        "Switch the {name} runtime from {old} to {new}?\n\nThe runtime is downloaded and installed again; downloaded models are kept. It is the current engine, so screen translation is unavailable while it reinstalls.",
    ),
    "settings.ocr_engine.manage": _pair("管理(&M)", "&Manage"),
    "settings.ocr_engine.manage.name": _pair("管理{name}的流水线", "Manage the {name} pipeline"),
    # 流水线（OCRE-08）
    "settings.ocr_pipeline.title": _pair("{name} 的识别流水线", "{name} recognition pipeline"),
    "settings.ocr_pipeline.intro": _pair(
        "按环节选用模型。安装引擎时已下载每个环节的默认模型，其他模型单独下载；未下载的模型下载完成后才能选用。",
        "Choose a model for each step. Installing the engine downloads the default model of every step; other models are downloaded one by one, and a model can be chosen once it is downloaded.",
    ),
    "settings.ocr_pipeline.not_current": _pair(
        "{name} 不是当前引擎，这里的修改在它设为当前引擎后生效。",
        "{name} is not the current engine; changes here take effect once it is set as the current engine.",
    ),
    "settings.ocr_pipeline.not_installed": _pair(
        "{name} 尚未安装，安装后才能下载模型。",
        "{name} is not installed; models can be downloaded after it is installed.",
    ),
    "settings.ocr_pipeline.no_models": _pair(
        "这个环节没有可选的模型。", "This step has no models to choose from."
    ),
    "settings.ocr_pipeline.models": _pair("{stage}的可选模型", "Models for {stage}"),
    # 模型行
    "settings.ocr_model.default": _pair("默认", "Default"),
    "settings.ocr_model.bundled": _pair("随引擎自带", "Included with the engine"),
    "settings.ocr_model.languages": _pair("语言：{languages}", "Languages: {languages}"),
    "settings.ocr_model.separator": _pair(" · ", " · "),
    "settings.ocr_model.status.not_downloaded": _pair("未下载", "Not downloaded"),
    "settings.ocr_model.status.queued": _pair("等待下载", "Waiting to download"),
    "settings.ocr_model.status.downloading": _pair("正在下载 {percent}%", "Downloading {percent}%"),
    "settings.ocr_model.status.downloading_unknown": _pair("正在下载…", "Downloading…"),
    "settings.ocr_model.status.downloaded": _pair("已下载", "Downloaded"),
    "settings.ocr_model.status.error": _pair("下载失败：{reason}", "Download failed: {reason}"),
    "settings.ocr_model.download": _pair("下载", "Download"),
    "settings.ocr_model.retry": _pair("重试", "Retry"),
    "settings.ocr_model.cancel": _pair("取消", "Cancel"),
    "settings.ocr_model.delete": _pair("删除", "Delete"),
    "settings.ocr_model.action.name": _pair("{action}：{name}", "{action}: {name}"),
    "settings.ocr_model.progress": _pair("{name}的下载进度", "{name} download progress"),
    "settings.ocr_model.needs_download": _pair("下载完成后才能选用。", "Can be chosen once downloaded."),
    "settings.ocr_model.delete.confirm": _pair(
        "删除模型「{name}」？之后需要时可以重新下载。",
        "Delete the model “{name}”? You can download it again later.",
    ),
    "settings.ocr_model.delete.confirm_selected": _pair(
        "「{name}」是{stage}正在选用的模型。删除前先把该环节改回默认模型「{default}」。继续？",
        "“{name}” is the model in use for {stage}. Before it is deleted, the step goes back to the default model “{default}”. Continue?",
    ),
    "settings.ocr_model.delete.blocked": _pair(
        "正在选用，而且没有可改回的已下载默认模型，不能删除。",
        "In use, and there is no downloaded default model to go back to, so it cannot be deleted.",
    ),
    # 参数（OCRE-09）
    "settings.ocr_params.title": _pair("识别参数", "Recognition parameters"),
    "settings.ocr_params.advanced.title": _pair("高级参数", "Advanced parameters"),
    "settings.ocr_params.expanded": _pair("已展开", "Expanded"),
    "settings.ocr_params.collapsed": _pair("已收起", "Collapsed"),
    "settings.ocr_params.expand": _pair("展开(&A)", "Show (&A)"),
    "settings.ocr_params.collapse": _pair("收起(&A)", "Hide (&A)"),
    "settings.ocr_params.advanced.count": _pair(
        "{count} 项，一般不需要修改。", "{count} settings that rarely need changing."
    ),
    "settings.ocr_params.reset": _pair("恢复默认(&R)…", "&Restore defaults…"),
    "settings.ocr_params.reset.name": _pair(
        "把{name}的识别参数恢复为默认值", "Restore the {name} recognition parameters to their defaults"
    ),
    "settings.ocr_params.reset.confirm": _pair(
        "把 {name} 的识别参数全部恢复为默认值？选用的模型不变。",
        "Restore all {name} recognition parameters to their defaults? The chosen models stay as they are.",
    ),
    "settings.ocr_params.reset_one": _pair("恢复默认值", "Restore default"),
    "settings.ocr_params.reset_one.name": _pair("恢复默认值：{name}", "Restore default: {name}"),
    "settings.ocr_params.range": _pair(
        "取值范围 {low} 至 {high}，默认 {default}。", "Range {low} to {high}, default {default}."
    ),
    "settings.ocr_params.min": _pair("最小 {low}，默认 {default}。", "At least {low}, default {default}."),
    "settings.ocr_params.max": _pair("最大 {high}，默认 {default}。", "At most {high}, default {default}."),
    "settings.ocr_params.default": _pair("默认：{default}。", "Default: {default}."),
    "settings.ocr_params.default_none": _pair("默认不设置。", "Not set by default."),
    "settings.ocr_params.reload": _pair(
        "修改后自动重新加载识别组件。", "The recognition component reloads automatically after a change."
    ),
    # 结构归一化（OCR-11）
    "settings.ocr_normalize.title": _pair("结构归一化", "Layout normalization"),
    "settings.ocr_normalize.intro": _pair(
        "版面检测之后、翻译之前整理识别结果，依次做三步：清洗去掉空框、过小与重复的框，拆分横跨两栏的长行，把文本行合并成段落。全部关闭时版面块原样通过。",
        "Tidies the recognition result after layout detection and before translation, in three steps: cleaning removes empty, tiny and duplicate boxes, splitting breaks lines that span two columns, and merging joins text lines into paragraphs. With every step off, the layout blocks pass through unchanged.",
    ),
    "settings.ocr_normalize.step_off": _pair("先打开上面的开关。", "Turn on the switch above first."),
    # 流水线环节（OcrStage 的顺序）
    **_stage(
        "detection",
        ("文字检测", "找出截图里文字行所在的位置。"),
        ("Text detection", "Finds where the lines of text are in the screenshot."),
    ),
    **_stage(
        "recognition",
        ("文字识别", "把检测到的文字行转成文字；识别模型决定能识别哪些语言。"),
        (
            "Text recognition",
            "Turns the detected lines into text; the recognition model decides which languages can be read.",
        ),
    ),
    **_stage(
        "textline_orientation",
        ("文本行方向", "判断文字行是否上下颠倒，颠倒的先转正。"),
        ("Text line orientation", "Detects upside-down lines and turns them the right way up."),
    ),
    **_stage(
        "doc_orientation",
        ("文档方向校正", "整张截图旋转了 90°、180° 或 270° 时先转正。"),
        (
            "Document orientation",
            "Turns the whole screenshot upright when it is rotated by 90°, 180° or 270°.",
        ),
    ),
    **_stage(
        "doc_unwarping",
        ("文档去畸变", "把拍摄得弯曲、倾斜的纸面拉平。"),
        ("Document unwarping", "Flattens pages that were photographed curved or skewed."),
    ),
    **_stage(
        "layout",
        ("版面检测", "把页面分成标题、正文、表格、图片、公式等版面块。"),
        (
            "Layout detection",
            "Splits the page into blocks such as titles, body text, tables, images and formulas.",
        ),
    ),
    **_stage(
        "region",
        ("区域检测", "找出多栏、多篇文章等大区域，用来确定阅读顺序。"),
        (
            "Region detection",
            "Finds large regions such as columns or separate articles to work out the reading order.",
        ),
    ),
    **_stage(
        "table",
        ("表格识别", "识别表格的结构与单元格。"),
        ("Table recognition", "Recognizes the structure and cells of tables."),
    ),
    **_stage(
        "formula",
        ("公式识别", "把公式转成 LaTeX。"),
        ("Formula recognition", "Converts formulas to LaTeX."),
    ),
    **_stage(
        "chart",
        ("图表识别", "把图表转成数据表。"),
        ("Chart parsing", "Converts charts to data tables."),
    ),
}

PARAM_TEXTS: Final[dict[str, tuple[str, str]]] = {
    # ocr_rapid 与 ocr_paddle 的参数（两节同名的字段共用一条，说明写成两边都适用的意思）
    "det_thresh": ("Detection pixel threshold", "Threshold for binarizing the text probability map; lower values pick up faint and small text more easily."),
    "det_box_thresh": ("Detection box threshold", "Text boxes whose average score is below this value are dropped; higher values mean fewer false detections and more misses."),
    "det_unclip_ratio": ("Text box expansion", "How far detected boxes are expanded; larger values give looser boxes that are less likely to clip the edges of text."),
    "det_limit_side_len": ("Detection input size", "Before detection the image side is scaled to this value (pixels) according to the size limit mode."),
    "det_limit_type": ("Detection size limit mode", "min: the short side is at least the detection input size (enlarges small images); max: the long side is at most the detection input size (shrinks large images)."),
    "det_max_candidates": ("Maximum text boxes", "The largest number of candidate text boxes kept for one image."),
    "det_use_dilation": ("Dilate probability map", "Dilates the map once after binarization to join broken strokes."),
    "det_score_mode": ("Box scoring", "fast scores the bounding rectangle; slow scores the polygon, which is more accurate for curved text but slower."),
    "text_score": ("Recognition score threshold", "Text lines with a recognition confidence below this value are dropped."),
    "cls_thresh": ("Orientation threshold", "A text line is rotated only when the orientation classifier is more confident than this value."),
    "rec_batch_num": ("Recognition batch size", "Number of text lines sent to the recognition model at once."),
    "min_side_len": ("Minimum side length", "Images whose short side is below this value (pixels) are enlarged before recognition."),
    "max_side_len": ("Maximum side length", "Images whose long side is above this value (pixels) are shrunk before recognition."),
    "cpu_threads": ("CPU threads", "Number of CPU inference threads; 0 means automatic (at most 8)."),
    "enable_mkldnn": ("CPU acceleration (oneDNN)", "With CPU inference, enables oneDNN for the models that can run under it; when off, every model runs the plain way, which is slower but safest. No effect with GPU inference."),
    "text_det_limit_side_len": ("Detection input size", "Before detection the image side is scaled to this value (pixels); 0 uses the pipeline default (736 in layout mode, 64 in text mode)."),
    "text_det_limit_type": ("Detection size limit mode", "min: the short side is at least the detection input size; max: the long side is at most the detection input size; default: the pipeline default (min)."),
    "text_det_thresh": ("Detection pixel threshold", "Threshold for binarizing the text probability map; lower values pick up faint and small text more easily."),
    "text_det_box_thresh": ("Detection box threshold", "Text boxes whose average score is below this value are dropped."),
    "text_det_unclip_ratio": ("Text box expansion", "How far detected boxes are expanded."),
    "text_rec_score_thresh": ("Recognition score threshold", "Text lines with a recognition confidence below this value are dropped; 0 keeps everything."),
    "layout_threshold": ("Layout detection threshold", "Layout blocks with a confidence below this value are dropped; 0 uses each category's default threshold (0.3 to 0.5)."),
    "layout_nms": ("Remove duplicate layout boxes", "Applies non-maximum suppression to overlapping layout boxes."),
    "layout_unclip_ratio": ("Layout box expansion", "How far layout boxes are expanded."),
    "layout_merge_bboxes_mode": ("Overlapping layout boxes", "large keeps the larger box, small keeps the smaller one, union keeps both; default uses each category's default."),
    # ocr_normalize
    "clean": ("Clean", "Removes boxes that are empty, too small, low in confidence or duplicated."),
    "clean_min_score": ("Minimum confidence", "Text lines and layout blocks with a confidence below this value are dropped; 0 keeps everything."),
    "clean_min_area": ("Minimum area", "Boxes smaller than this area (square pixels) are dropped."),
    "clean_duplicate_iou": ("Duplicate box IoU", "Two boxes whose intersection over union reaches this value count as duplicates and only the more confident one is kept; above 1 disables the check."),
    "split": ("Split lines across columns", "A line that spans the gap between two columns is split at the spaces in its text into a left and a right line (text mode only)."),
    "split_min_gap": ("Column gap width", "Minimum width of the gap between columns (× line height)."),
    "split_window": ("Context range", "How far up and down to look when deciding whether there is a column gap (× line height)."),
    "aggregate": ("Merge into paragraphs", "In text mode, joins text lines into paragraphs; when off, each line is translated on its own."),
    "same_line_tolerance": ("Same-line midline tolerance", "Largest midline height difference between fragments of one line (× smaller text height)."),
    "fragment_gap": ("Same-line fragment gap", "Largest horizontal gap between fragments of one line (× smaller text height)."),
    "gutter_window": ("Column gap range", "How far up and down to look when deciding whether a gap between fragments is a column gap (× larger text height)."),
    "height_tolerance": ("Line height tolerance", "Largest line height difference between neighboring lines of one paragraph (× smaller line height)."),
    "line_gap": ("Maximum line spacing", "Largest vertical gap between neighboring lines of one paragraph (× smaller line height)."),
    "max_line_overlap": ("Maximum line overlap", "Vertical overlap allowed between neighboring lines of one paragraph (× smaller line height)."),
    "min_horizontal_overlap": ("Minimum horizontal overlap", "Smallest horizontal overlap between neighboring lines of one paragraph (× width of the narrower line)."),
    "left_align_tolerance": ("Left alignment tolerance", "Largest difference between left edges when lines are grouped by left alignment (× smaller line height)."),
    "min_column_gap": ("Minimum column gap", "Smallest gap treated as a column gap when ordering for reading (× median line height)."),
    "merge_blocks": ("Merge neighboring body blocks", "In layout mode, merges body text blocks that sit right above each other and line up left and right."),
    "merge_block_gap": ("Maximum block gap", "Vertical gap allowed when merging neighboring body blocks (× line height)."),
}  # fmt: skip
"""引擎配置节参数的英文标题与说明，键是字段名。"""

PARAM_CATALOG: Final[Catalog] = {
    key: {"en": text}
    for name, (title, desc) in PARAM_TEXTS.items()
    for key, text in ((f"settings.ocrf.{name}.title", title), (f"settings.ocrf.{name}.desc", desc))
}

register_catalog(CATALOG)
register_catalog(PARAM_CATALOG)


def has_text(key: str) -> bool:
    """文案键是否已经登记（本模块或 strings.py）。"""
    return key in CATALOG or key in PARAM_CATALOG or strings.has_text(key)


def text_or(key: str, fallback: str) -> str:
    """有文案时返回当前语言的文案，否则返回 fallback。"""
    return tr(key) if has_text(key) else fallback


__all__ = ["CATALOG", "PARAM_CATALOG", "PARAM_TEXTS", "has_text", "text_or"]
