"""真实 OCR 测试用的合成截图：在内存里用 PySide6 的 QImage、QPainter 画白底黑字，得到 BGRA 像素与每行的范围。

offscreen 平台没有字体库，要显式加载系统的微软雅黑字体文件（只读），它同时覆盖中文和拉丁字母。
Windows 自带 OCR 与 RapidOCR 的真实测试共用这里的函数。
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QImage, QPainter

FONT_FILE = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"
FONT_FAMILY = "Microsoft YaHei"


@dataclass(frozen=True, slots=True)
class Drawn:
    """画在截图上的一行：文字与它在截图中的范围（x0, y0, x1, y1）。"""

    text: str
    box: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class Screenshot:
    width: int
    height: int
    stride: int
    data: bytes
    lines: tuple[Drawn, ...]


def ensure_font() -> bool:
    """确保微软雅黑可用；没有字体文件时返回 False（测试据此跳过）。需要已经创建 QApplication。"""
    if not FONT_FILE.is_file():
        return False
    if FONT_FAMILY not in QFontDatabase.families():
        return QFontDatabase.addApplicationFont(str(FONT_FILE)) >= 0
    return True


def render(
    lines: Sequence[tuple[str, int, int]], width: int, height: int, pixel_size: int = 32
) -> Screenshot:
    """白底黑字画出各行（文字，左边 x，基线 y），返回 BGRA 像素和每行的实际范围。"""
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    font = QFont(FONT_FAMILY)
    font.setPixelSize(pixel_size)
    metrics = QFontMetrics(font)
    painter = QPainter(image)
    painter.setFont(font)
    painter.setPen(QColor("black"))
    drawn: list[Drawn] = []
    for text, x, baseline in lines:
        painter.drawText(x, baseline, text)
        rect: QRect = metrics.tightBoundingRect(text).translated(x, baseline)
        drawn.append(Drawn(text, (rect.left(), rect.top(), rect.right() + 1, rect.bottom() + 1)))
    painter.end()
    stride = image.bytesPerLine()
    data = bytes(image.constBits())[: stride * height]  # pyright: ignore[reportArgumentType]
    return Screenshot(width, height, stride, data, tuple(drawn))


def assert_near(box: Sequence[int], expected: tuple[int, int, int, int], tolerance: int) -> None:
    assert all(abs(a - b) <= tolerance for a, b in zip(box, expected, strict=True)), (box, expected)
