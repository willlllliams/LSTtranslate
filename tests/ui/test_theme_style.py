"""ui.theme 的外观工具测试：颜色随深浅色切换、字体、主要按钮样式、版式 token。"""

import os
from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from lst.ui.theme import TOKENS, apply_app_style, is_dark, style_primary_button, theme_colors, ui_font

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def scheme(qapp: QApplication) -> Iterator[None]:
    yield
    apply_app_style(qapp, "system")  # 还原：其他测试不受深浅色设置影响


def test_layout_tokens_follow_the_windows_11_settings_proportions() -> None:
    assert TOKENS.page_title_px == 28 and TOKENS.title_px == 14 and TOKENS.caption_px == 12
    assert 36 <= TOKENS.page_margin <= 40
    assert TOKENS.card_gap == 4 and TOKENS.group_gap == 24
    assert TOKENS.card_padding == 16 and TOKENS.card_min_height >= 68
    assert 6 <= TOKENS.card_radius <= 8 and TOKENS.icon_px == 20
    assert TOKENS.nav_width == 280 and 36 <= TOKENS.nav_item_height <= 40
    assert TOKENS.nav_indicator_width == 3


def test_light_and_dark_colors() -> None:
    light, dark = theme_colors(dark=False), theme_colors(dark=True)
    assert not light.dark and dark.dark
    assert light.window.name().upper() == TOKENS.window_light
    assert dark.window.name().upper() == TOKENS.window_dark
    # 卡片比窗口底色略亮：浅色主题是半透明的白，深色主题是半透明的浅色（比深色底略浅）。
    for colors in (light, dark):
        assert 0 < colors.card.alpha() < 255
        assert colors.card_hover != colors.card
        assert colors.card_border.alpha() > 0
        assert colors.nav_selected.alpha() > 0 and colors.nav_hover.alpha() > 0
    assert light.text.lightness() < 64 and dark.text.lightness() > 192
    assert light.text_secondary.alpha() < 255 and dark.text_secondary.alpha() < 255
    assert light.critical != dark.critical  # 错误色在深色主题下更亮
    assert light.accent == dark.accent  # 强调色跟随系统，不随深浅色变


def test_the_color_scheme_follows_the_application_setting(qapp: QApplication, scheme: None) -> None:
    apply_app_style(qapp, "dark")
    if not is_dark(qapp):
        pytest.skip("当前 Qt 平台不支持设置深浅色")
    assert theme_colors().dark
    apply_app_style(qapp, "light")
    assert not theme_colors().dark


def test_accent_follows_the_system_palette(qapp: QApplication) -> None:
    from PySide6.QtGui import QPalette

    assert theme_colors().accent == qapp.palette().color(QPalette.ColorRole.Accent)
    assert theme_colors().accent_text.name() in ("#ffffff", "#000000")


def test_ui_font_uses_pixel_sizes_and_weights(qapp: QApplication) -> None:
    title = ui_font(28, semibold=True, display=True)
    body = ui_font(14)
    assert title.pixelSize() == 28 and body.pixelSize() == 14
    assert title.weight().value > body.weight().value


def test_primary_button_style_uses_the_accent(qapp: QApplication) -> None:
    button = QPushButton("添加")
    style_primary_button(button)
    assert button.property("primary") is True
    assert "palette(highlight)" in button.styleSheet()  # 强调色随系统，样式表只引用调色板角色
    assert "#" not in button.styleSheet()  # 样式里没有写死的颜色
    assert button.icon().isNull()
    from lst.ui.theme import Glyph

    style_primary_button(button, Glyph.ADD)
    assert not button.icon().isNull()
