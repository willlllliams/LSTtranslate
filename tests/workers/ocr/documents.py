"""合成测试文档的页面描述（render_document.py 的输入）与期望结果。

两份文档：

- SMALL_DOCUMENT：1280×900，一个标题、一段中文、一段英文、一个两行三列带边框的表格，用于检查识别内容；
- FULL_HD_DOCUMENT：1920×1080 的普通文档页面（需求 6.1 节的测量条件），用于性能测试。

本模块只用标准库，主环境和 OCR 环境都能导入。
"""

from dataclasses import dataclass
from pathlib import Path

FONT = Path(r"C:\Windows\Fonts\msyh.ttc")

TITLE = "屏幕翻译测试文档"
CHINESE_LINES = [
    "机器翻译把一种自然语言的文本自动转换为另一种语言。",
    "本段落用于检查中文识别的效果，包含常见的汉字与标点。",
]
ENGLISH_LINES = [
    "Machine translation converts text from one language to another.",
    "This paragraph checks English recognition with common words.",
]
TABLE_ROWS = [["名称", "数量", "价格"], ["苹果", "12", "3.50"]]


@dataclass(frozen=True, slots=True)
class TableSpec:
    """表格在页面上的位置，测试据此检查单元格包围盒。"""

    x: int
    y: int
    col_widths: tuple[int, ...]
    row_height: int
    rows: tuple[tuple[str, ...], ...]

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + sum(self.col_widths), self.y + self.row_height * len(self.rows))

    def cell_bbox(self, row: int, col: int) -> tuple[int, int, int, int]:
        left = self.x + sum(self.col_widths[:col])
        top = self.y + row * self.row_height
        return (left, top, left + self.col_widths[col], top + self.row_height)


SMALL_TABLE = TableSpec(80, 380, (260, 260, 260), 60, tuple(tuple(row) for row in TABLE_ROWS))


def _text(x: int, y: int, size: int, lines: list[str], line_height: int) -> dict[str, object]:
    return {"type": "text", "x": x, "y": y, "size": size, "line_height": line_height, "lines": lines}


def _table(spec: TableSpec, size: int) -> dict[str, object]:
    return {
        "type": "table",
        "x": spec.x,
        "y": spec.y,
        "size": size,
        "col_widths": list(spec.col_widths),
        "row_height": spec.row_height,
        "border": 2,
        "rows": [list(row) for row in spec.rows],
    }


def small_document() -> dict[str, object]:
    """1280×900：标题、中文段落、英文段落（与标题和段落之间留出空行）、两行三列表格。"""
    return {
        "width": 1280,
        "height": 900,
        "font": str(FONT),
        "items": [
            _text(80, 50, 40, [TITLE], 56),
            _text(80, 130, 24, CHINESE_LINES, 36),
            _text(80, 230, 24, ENGLISH_LINES, 36),
            _table(SMALL_TABLE, 24),
        ],
    }


FULL_HD_CHINESE = [
    "截屏翻译把屏幕上任意区域的文字识别出来，再交给多个翻译引擎同时翻译。",
    "最先返回的译文立即显示，优先级更高的引擎返回后再替换为更好的结果。",
    "识别模型在后台常驻，按下热键后只需要完成一次推理，不再重复加载模型。",
]
FULL_HD_ENGLISH = [
    "Screen translation recognizes the text inside a selected region of the screen.",
    "Several engines translate the same paragraphs at the same time, and the first",
    "result is shown immediately while better results replace it as they arrive.",
]
FULL_HD_TABLE = TableSpec(
    120,
    700,
    (360, 300, 300),
    56,
    (("引擎", "类型", "平均耗时"), ("本地 NMT", "离线", "0.2 秒"), ("在线 LLM", "网络", "1.5 秒")),
)


def full_hd_document() -> dict[str, object]:
    """1920×1080：标题、两段中文、两段英文、一个三行三列表格，字号接近常见的屏幕文档。"""
    return {
        "width": 1920,
        "height": 1080,
        "font": str(FONT),
        "items": [
            _text(120, 60, 44, ["竞速翻译与版面识别说明"], 60),
            _text(120, 150, 26, FULL_HD_CHINESE, 40),
            _text(120, 300, 26, FULL_HD_ENGLISH, 40),
            _text(120, 450, 26, list(reversed(FULL_HD_CHINESE)), 40),
            _text(120, 600, 26, FULL_HD_ENGLISH[:2], 40),
            _table(FULL_HD_TABLE, 24),
        ],
    }
