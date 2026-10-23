"""ui.theme 的 Fluent 图标工具测试：字体加载、按码位绘制、颜色与禁用状态。"""

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QIcon, QImage, QPalette, QRawFont
from PySide6.QtWidgets import QApplication, QPushButton

from lst.ui.theme import (
    FLUENT_ICONS_FAMILY,
    MDL2_ICONS_FAMILY,
    Glyph,
    fluent_icon,
    fluent_icon_font,
    fluent_icon_text,
    fluent_icons_available,
    style_primary_button,
)

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
    assert fluent_icon_font(16).family() in (FLUENT_ICONS_FAMILY, MDL2_ICONS_FAMILY)
    assert fluent_icon_font(16).pixelSize() == 16


def test_mdl2_fallback_covers_icons_labels_and_navigation(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lst.ui import theme
    from lst.ui.settings.controls import SettingCard

    with monkeypatch.context() as patch:
        patch.setattr(theme, "_ICON_FONT_CANDIDATES", (theme._ICON_FONT_CANDIDATES[1],))
        patch.setattr(theme, "_icon_font_ready", None)
        patch.setattr(theme, "_icon_family", FLUENT_ICONS_FAMILY)
        patch.setattr(theme, "_icon_raw_font", None)
        patch.setattr(theme, "_icon_glyph_cache", {})
        assert fluent_icons_available()
        assert fluent_icon_font(20).family() == MDL2_ICONS_FAMILY
        raw = QRawFont.fromFont(fluent_icon_font(20))
        for glyph in [*ALL_GLYPHS, "\ue76c", "\ue70d"]:
            resolved = fluent_icon_text(glyph)
            assert resolved and all(raw.supportsCharacter(ord(c)) for c in resolved), repr(glyph)
            assert ink(fluent_icon(glyph).pixmap(QSize(24, 24)).toImage()), repr(glyph)
        assert fluent_icon_text(Glyph.LAYOUT) == Glyph.GRID
        assert fluent_icon_text(Glyph.BLOCKED) == Glyph.ERROR
        card = SettingCard(None, icon=Glyph.LAYOUT)
        assert card.icon_label.text() == Glyph.GRID
        assert card.icon_label.font().family() == MDL2_ICONS_FAMILY
        card.close()


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


def _rgb(color: QColor) -> tuple[int, int, int]:
    return (color.red(), color.green(), color.blue())


def test_primary_button_icon_follows_the_button_text_colors(qapp: QApplication) -> None:
    """主要按钮的图标与按钮文字同色：可用时是强调色上的文字色，禁用时是禁用文字色，都在绘制时取。"""
    button = QPushButton()
    style_primary_button(button, Glyph.ADD)
    palette = qapp.palette()
    icon = button.icon()
    normal = icon.pixmap(QSize(32, 32), QIcon.Mode.Normal).toImage()
    disabled = icon.pixmap(QSize(32, 32), QIcon.Mode.Disabled).toImage()
    assert {_rgb(c) for c in ink(normal)} >= {_rgb(palette.color(QPalette.ColorRole.HighlightedText))}
    assert _rgb(palette.color(QPalette.ColorRole.Mid)) in {_rgb(c) for c in ink(disabled)}

    original = QPalette(palette)
    changed = QPalette(palette)
    changed.setColor(QPalette.ColorRole.HighlightedText, QColor("#123456"))
    qapp.setPalette(changed)
    try:
        repainted = icon.pixmap(QSize(32, 32), QIcon.Mode.Normal).toImage()
        assert (0x12, 0x34, 0x56) in {_rgb(c) for c in ink(repainted)}  # 换主题后不必重新设置图标
    finally:
        qapp.setPalette(original)
