"""ui.theme 的 Fluent 图标工具测试：字体加载、按码位绘制、颜色与禁用状态。"""

import os

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QIcon, QImage
from PySide6.QtWidgets import QApplication, QPushButton

from lst.ui.theme import FLUENT_ICONS_FAMILY, Glyph, fluent_icon, fluent_icon_font, fluent_icons_available

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ALL_GLYPHS = [value for name, value in vars(Glyph).items() if name.isupper() and isinstance(value, str)]


@pytest.fixture(autouse=True)
def _require_icon_font(qapp: QApplication) -> None:
    if not fluent_icons_available():
        pytest.skip("本机没有 Segoe Fluent Icons 字体")


def ink(image: QImage) -> list[QColor]:
    """图像里不透明度明显大于 0 的像素。"""
    colors: list[QColor] = []
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.alpha() > 128:
                colors.append(color)
    return colors


def test_font_family_is_registered() -> None:
    assert fluent_icon_font(16).family() == FLUENT_ICONS_FAMILY
    assert fluent_icon_font(16).pixelSize() == 16


def test_every_glyph_draws_something() -> None:
    assert ALL_GLYPHS, "没有取到 Glyph 常量"
    for glyph in ALL_GLYPHS:
        image = fluent_icon(glyph).pixmap(QSize(24, 24)).toImage()
        assert ink(image), f"码位 {glyph!r} 没有画出任何像素"


def test_icon_is_vector_and_scales_with_device_pixel_ratio() -> None:
    icon = fluent_icon(Glyph.SETTINGS)
    small = icon.pixmap(QSize(16, 16), 1.0)
    large = icon.pixmap(QSize(16, 16), 2.0)
    assert small.size() == QSize(16, 16)
    assert large.size() == QSize(32, 32)
    assert large.devicePixelRatio() == 2.0
    # 大图是重新绘制的，不是把小图放大：它的边缘更细，同样的笔画占用的像素数明显更多。
    assert len(ink(large.toImage())) > 2 * len(ink(small.toImage()))


def test_explicit_color_is_used() -> None:
    image = fluent_icon(Glyph.CIRCLE_FILL, QColor("#C42B1C")).pixmap(QSize(32, 32)).toImage()
    centre = image.pixelColor(16, 16)
    assert (centre.red(), centre.green(), centre.blue()) == (0xC4, 0x2B, 0x1C)


def test_disabled_mode_is_fainter() -> None:
    icon = fluent_icon(Glyph.CIRCLE_FILL, QColor("#000000"))
    normal = icon.pixmap(QSize(32, 32), QIcon.Mode.Normal).toImage().pixelColor(16, 16)
    disabled = icon.pixmap(QSize(32, 32), QIcon.Mode.Disabled).toImage().pixelColor(16, 16)
    assert normal.alpha() == 255
    assert 0 < disabled.alpha() < 255


def test_default_color_follows_the_palette(qapp: QApplication) -> None:
    image = fluent_icon(Glyph.CIRCLE_FILL).pixmap(QSize(32, 32)).toImage()
    text = qapp.palette().color(qapp.palette().ColorRole.Text)
    centre = image.pixelColor(16, 16)
    assert (centre.red(), centre.green(), centre.blue()) == (text.red(), text.green(), text.blue())


def test_icon_can_be_set_on_widgets() -> None:
    button = QPushButton()
    button.setIcon(fluent_icon(Glyph.ADD))
    assert not button.icon().isNull()
    assert ink(button.icon().pixmap(QSize(16, 16)).toImage())
