"""设置窗口的外观测试：版式尺寸、卡片与导航的绘制、深浅色、Mica 不可用时的底色、错误提示不撑乱布局。"""

import os
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QEvent, QPoint, QSize, Qt
from PySide6.QtGui import QColor, QEnterEvent
from PySide6.QtWidgets import QApplication, QLabel, QStyle, QStyleOptionViewItem

from lst.contracts.config import ConfigRejected
from lst.ui.settings.controls import SettingCard, SwitchEditor, ThemedLabel
from lst.ui.settings.window import NavDelegate, SettingsWindow
from lst.ui.theme import TOKENS, apply_app_style, is_dark, theme_colors
from tests.ui.settings.conftest import Harness

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def restore_scheme(qapp: QApplication) -> Iterator[None]:
    yield
    apply_app_style(qapp, "system")


def require_dark(app: QApplication) -> None:
    """当前 Qt 平台不能设置深色时跳过（offscreen 平台不支持，原生 Windows 平台支持）。"""
    apply_app_style(app, "dark")
    if not is_dark(app):
        pytest.skip("当前 Qt 平台不支持设置深浅色")


def pixel(window: SettingsWindow, x: int, y: int) -> QColor:
    """窗口截图里逻辑坐标 (x, y) 处的像素（截图按设备像素比放大，坐标要换算）。"""
    pixmap = window.grab()
    ratio = pixmap.devicePixelRatio()
    return pixmap.toImage().pixelColor(int(x * ratio), int(y * ratio))


async def test_typography_sizes(harness: Harness) -> None:
    window = await harness.open()
    page = window.page("capture")
    assert page.heading.font().pixelSize() == TOKENS.page_title_px == 28
    card = page.bindings[0].card
    assert card.title_label.font().pixelSize() == TOKENS.title_px == 14
    assert card.desc_label.font().pixelSize() == TOKENS.caption_px == 12
    assert card.error_label.font().pixelSize() == TOKENS.caption_px
    assert page.heading.font().weight().value >= 600  # 半粗体
    assert window.nav.width() == TOKENS.nav_width == 280


async def test_page_margins_and_card_spacing(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("capture")
    window.resize(1200, 900)
    QApplication.processEvents()
    page = window.page("capture")
    margins = page.body.contentsMargins()
    assert margins.left() == margins.right() == TOKENS.page_margin
    assert page.body.spacing() == TOKENS.card_gap == 4
    first, second = page.bindings[0].card, page.bindings[1].card
    gap = second.geometry().top() - first.geometry().bottom() - 1
    assert gap == TOKENS.card_gap  # 同组卡片之间 4 像素
    assert first.geometry().left() >= TOKENS.page_margin - 1


async def test_cards_have_the_documented_geometry(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("capture")
    window.resize(1200, 900)
    QApplication.processEvents()
    for binding in window.page("capture").bindings:
        card = binding.card
        assert card.height() >= TOKENS.card_min_height
        assert card.layout() is not None
        margins = card.layout().contentsMargins()  # type: ignore[union-attr]
        assert margins.left() == margins.right() == TOKENS.card_padding
        assert not card.icon_label.isHidden()  # 每张卡片左侧都有图标
        assert card.icon_label.width() >= TOKENS.icon_px
        # 图标在左，标题与说明在中间，控件在右并且右对齐。
        editor = card.editor
        assert editor is not None
        editor_left = editor.mapTo(card, QPoint(0, 0)).x()
        editor_right = editor.mapTo(card, QPoint(editor.width(), 0)).x()
        title_left = card.title_label.mapTo(card, QPoint(0, 0)).x()
        icon_right = card.icon_label.mapTo(card, QPoint(card.icon_label.width(), 0)).x()
        assert icon_right < title_left < editor_left
        assert abs(editor_right - (card.width() - TOKENS.card_padding)) <= 2


async def test_group_titles_separate_the_groups(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("capture")
    window.resize(1200, 1100)
    QApplication.processEvents()
    page = window.page("capture")
    cards = [b.card for b in page.bindings]
    # ocr 的第 4 张卡片（识别语言）与“识别内容”分组的第一张卡片（表格识别）之间隔着分组标题。
    gap = cards[4].geometry().top() - cards[3].geometry().bottom()
    assert gap >= TOKENS.group_gap + 14  # 组间距加上一行标题的高度


async def test_every_card_has_an_icon_and_a_title(harness: Harness) -> None:
    window = await harness.open()
    for page in window.pages:
        for binding in page.all_bindings():
            assert binding.card.icon_label.text() != "", binding.location
            assert binding.card.title_label.text() != "", binding.location


async def test_card_hover_changes_the_background_state(harness: Harness) -> None:
    window = await harness.open()
    card = window.page("general").bindings[0].card
    assert not card._hover
    QApplication.sendEvent(card, QEnterEvent(QPoint(5, 5), QPoint(5, 5), QPoint(5, 5)))
    assert card._hover
    QApplication.sendEvent(card, QEvent(QEvent.Type.Leave))
    assert not card._hover


async def test_card_is_drawn_lighter_than_the_window_in_the_light_theme(
    harness: Harness, qapp: QApplication, restore_scheme: None
) -> None:
    apply_app_style(qapp, "light")
    window = await harness.open()
    window.resize(1200, 700)
    QApplication.processEvents()
    card = window.page("general").bindings[0].card
    inside = card.mapTo(window, QPoint(card.width() // 2, 3))
    background = theme_colors().window
    if window._mica:
        # Mica 生效：内容区背景透明，由系统绘制；卡片是半透明的白叠在上面。
        assert pixel(window, 600, 690).alpha() == 0
        assert 0 < pixel(window, inside.x(), inside.y()).alpha() < 255
        return
    # 没有 Mica（offscreen 平台）：窗口用 ui.theme 的底色填充，卡片比它更亮。
    assert pixel(window, 600, 690).name() == background.name()
    assert pixel(window, inside.x(), inside.y()).lightness() > background.lightness()


async def test_dark_theme_window_and_card(harness: Harness, qapp: QApplication, restore_scheme: None) -> None:
    require_dark(qapp)
    window = await harness.open()
    window.resize(1200, 700)
    QApplication.processEvents()
    background = theme_colors().window
    assert background.lightness() < 64
    card = window.page("general").bindings[0].card
    inside = card.mapTo(window, QPoint(card.width() // 2, 3))
    if window._mica:
        assert pixel(window, 600, 690).alpha() == 0
        assert 0 < pixel(window, inside.x(), inside.y()).alpha() < 255
        return
    assert pixel(window, 600, 690).name() == background.name()
    assert pixel(window, inside.x(), inside.y()).lightness() > background.lightness()  # 深色主题下略浅


async def test_labels_recolor_when_the_theme_changes(
    harness: Harness, qapp: QApplication, restore_scheme: None
) -> None:
    window = await harness.open()
    card = window.page("general").bindings[0].card
    require_dark(qapp)
    apply_app_style(qapp, "light")
    QApplication.processEvents()
    light_text = card.title_label.palette().windowText().color()
    apply_app_style(qapp, "dark")
    QApplication.processEvents()
    dark_text = card.title_label.palette().windowText().color()
    assert light_text.lightness() < dark_text.lightness()
    assert dark_text == theme_colors().text


async def test_error_text_goes_below_the_control_without_moving_it(harness: Harness) -> None:
    window = await harness.open()
    window.show_page("capture")
    window.resize(1200, 900)
    QApplication.processEvents()
    page = window.page("capture")
    binding = next(b for b in page.bindings if b.location == ("ocr", "resize_max_side"))
    card, editor = binding.card, binding.editor
    before = (editor.geometry().left(), editor.geometry().top(), card.title_label.geometry().topLeft())
    await harness.publish(ConfigRejected(("ocr", "resize_max_side"), "不小于 256"))
    QApplication.processEvents()
    after = (editor.geometry().left(), editor.geometry().top(), card.title_label.geometry().topLeft())
    assert before == after  # 控件和标题的位置不变
    assert card.error_label.geometry().top() >= card.desc_label.geometry().bottom()  # 在说明的下方
    assert card.error_label.palette().windowText().color() == theme_colors().critical
    assert card.error_label.font().pixelSize() == TOKENS.caption_px
    other = next(b for b in page.bindings if b.location == ("ocr", "table")).card
    assert other.error_text == ""


async def test_switch_shows_on_off_text_and_is_a_toggle_not_a_checkbox(harness: Harness) -> None:
    window = await harness.open()
    editor = next(b.editor for b in window.page("capture").bindings if b.location == ("ocr", "table"))
    assert isinstance(editor, SwitchEditor)
    assert editor.state_label.text() == "开"
    editor.switch.click()
    assert editor.state_label.text() == "关"
    assert (
        editor.switch.sizeHint().width() > editor.switch.sizeHint().height() * 1.5
    )  # 拨动开关，不是方形复选框


async def test_nav_delegate_metrics_and_painting(harness: Harness) -> None:
    window = await harness.open()
    delegate = window.nav.itemDelegate()
    assert isinstance(delegate, NavDelegate)
    option = QStyleOptionViewItem()
    option.rect.setSize(QSize(TOKENS.nav_width, 10))
    size = delegate.sizeHint(option, window.nav.model().index(0, 0))
    assert size.height() == TOKENS.nav_item_height and 36 <= size.height() <= 40
    assert window.nav.iconSize() == QSize(TOKENS.nav_icon_px, TOKENS.nav_icon_px)

    # 选中项左侧画着强调色竖条（3 像素宽），未选中的项没有。
    window.resize(1200, 700)
    window.show_page("general")
    QApplication.processEvents()
    selected_rect = window.nav.visualItemRect(window.nav.item(0))
    other_rect = window.nav.visualItemRect(window.nav.item(2))
    origin = window.nav.mapTo(window, QPoint(0, 0))
    accent = theme_colors().accent
    bar_x = origin.x() + TOKENS.spacing_s + 1
    selected_pixel = pixel(window, bar_x, origin.y() + selected_rect.center().y())
    other_pixel = pixel(window, bar_x, origin.y() + other_rect.center().y())
    assert (selected_pixel.red(), selected_pixel.green(), selected_pixel.blue()) == (
        accent.red(),
        accent.green(),
        accent.blue(),
    )
    assert other_pixel.name() != accent.name()


async def test_mica_unavailable_falls_back_to_an_opaque_window(harness: Harness) -> None:
    window = await harness.open()
    if QApplication.platformName() != "offscreen":
        pytest.skip("Mica 是否生效取决于系统版本；这里只检查 offscreen 平台上的回退")
    assert not window._mica
    assert not window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert pixel(window, 5, window.height() - 5).alpha() == 255


async def test_primary_buttons_use_the_accent_fill(harness: Harness) -> None:
    window = await harness.open()
    engines = window.page("engines")
    models = window.page("models")
    assert engines.add_button.property("primary") is True  # type: ignore[attr-defined]
    assert models.download_button.property("primary") is True  # type: ignore[attr-defined]
    assert "palette(highlight)" in engines.add_button.styleSheet()  # type: ignore[attr-defined]
    assert not engines.remove_button.property("primary")  # type: ignore[attr-defined]


async def test_themed_label_roles(qapp: QApplication) -> None:
    primary, secondary, critical = ThemedLabel("primary"), ThemedLabel("secondary"), ThemedLabel("critical")
    colors = theme_colors()
    assert primary.palette().windowText().color() == colors.text
    assert secondary.palette().windowText().color() == colors.text_secondary
    assert critical.palette().windowText().color() == colors.critical
    secondary.set_role("critical")
    assert secondary.palette().windowText().color() == colors.critical
    assert isinstance(secondary, QLabel) and QStyle  # 仍是普通标签
    assert SettingCard
