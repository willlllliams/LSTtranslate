"""PP-StructureV3 版面标签到需求 OCR-01 类别的映射（架构文档 8.4 节）。

标签全集的来源：

- PP-DocLayout_plus-L（PP-StructureV3 默认的版面模型，20 类）、PP-DocLayout-L / PP-DocLayout-S（23 类）
  模型目录 inference.yml 中的 label_list；
- PP-StructureV3 解析阶段改写出的标签：footnote 在正文之上时改成 text，唯一的 paragraph_title 改成
  doc_title，图表旁的说明文字改成 vision_footnote（PaddleX 3.7 layout_parsing/xycut_enhanced）；
- PaddleX 3.7 版面解析代码（setting.BLOCK_LABEL_MAP、可视化配色表）中出现的其余标签，包括 PaddleOCR-VL
  的 PP-DocLayoutV2/V3 使用的 display_formula、inline_formula、vertical_text 等，换模型时也能覆盖。

归类原则（架构文档 8.4 节的表）：标题类归 title；页眉、页脚、页码归 header_footer；含文字的其余标签
（正文、摘要、目录、参考文献、脚注、算法、侧栏、图表标题、公式编号等）归 text；不含文字的视觉元素
（图片、印章、页眉页脚中的图片）归 image。图表标题（figure_title 等）是一段说明文字，归 text。
页码在 PP-DocLayout 系列里的标签是 number，page_number 是架构文档表中的写法，两者都映射到 header_footer。
"""

import logging
from typing import Final

from lst.contracts.ocr import BlockCategory

log = logging.getLogger(__name__)

LABEL_CATEGORIES: Final[dict[str, BlockCategory]] = {
    # 标题
    "doc_title": BlockCategory.TITLE,
    "paragraph_title": BlockCategory.TITLE,
    "abstract_title": BlockCategory.TITLE,
    "reference_title": BlockCategory.TITLE,
    "refer_title": BlockCategory.TITLE,
    "content_title": BlockCategory.TITLE,
    # 正文及其他含文字的标签
    "text": BlockCategory.TEXT,
    "abstract": BlockCategory.TEXT,
    "content": BlockCategory.TEXT,
    "reference": BlockCategory.TEXT,
    "reference_content": BlockCategory.TEXT,
    "footnote": BlockCategory.TEXT,
    "vision_footnote": BlockCategory.TEXT,
    "algorithm": BlockCategory.TEXT,
    "aside_text": BlockCategory.TEXT,
    "vertical_text": BlockCategory.TEXT,
    "formula_number": BlockCategory.TEXT,
    "figure_title": BlockCategory.TEXT,
    "table_title": BlockCategory.TEXT,
    "chart_title": BlockCategory.TEXT,
    "figure_table_chart_title": BlockCategory.TEXT,
    # 表格、公式、图表
    "table": BlockCategory.TABLE,
    "formula": BlockCategory.FORMULA,
    "display_formula": BlockCategory.FORMULA,
    "inline_formula": BlockCategory.FORMULA,
    "chart": BlockCategory.CHART,
    # 不含文字的视觉元素
    "image": BlockCategory.IMAGE,
    "figure": BlockCategory.IMAGE,
    "flowchart": BlockCategory.IMAGE,
    "seal": BlockCategory.IMAGE,
    "header_image": BlockCategory.IMAGE,
    "footer_image": BlockCategory.IMAGE,
    # 页眉页脚
    "header": BlockCategory.HEADER_FOOTER,
    "footer": BlockCategory.HEADER_FOOTER,
    "number": BlockCategory.HEADER_FOOTER,
    "page_number": BlockCategory.HEADER_FOOTER,
}

# 已经警告过的未知标签。同一个标签每个进程只警告一次，避免每次识别都刷一遍日志。
_warned: set[str] = set()


def category_for(label: str, has_text: bool) -> BlockCategory:
    """把一个版面标签映射到块类别。

    标签不区分大小写。表中没有的标签按“有文字归 text，没有文字归 image”处理，并记一条 WARNING。
    """
    category = LABEL_CATEGORIES.get(label.lower())
    if category is not None:
        return category
    fallback = BlockCategory.TEXT if has_text else BlockCategory.IMAGE
    if label not in _warned:
        _warned.add(label)
        log.warning("未知的版面标签 %r，按%s处理", label, "正文" if has_text else "图片")
    return fallback
