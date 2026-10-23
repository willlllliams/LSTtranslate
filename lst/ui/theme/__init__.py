"""Fluent 风格的主题：应用样式、字体、Mica 背景、设计常量（需求 UI-01 至 UI-04，架构文档 2.2 节）。

QFluentWidgets 是 GPLv3 许可，在用户确认之前不引入；这里用 Qt 自带的 windows11 样式，
Mica 背景用 ctypes 调用 DWM 实现，这是架构文档 2.2 节写明的替代方案。
"""

import ctypes
import hashlib
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QIcon,
    QIconEngine,
    QPainter,
    QPalette,
    QPixmap,
    QRawFont,
)
from PySide6.QtWidgets import QApplication, QPushButton, QStyleFactory, QWidget

logger = logging.getLogger(__name__)

type ColorSchemeSetting = Literal["system", "light", "dark"]


class UiConfig(BaseModel):
    """配置文件的 [ui] 节。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    language: Literal["auto", "zh-Hans", "en"] = "auto"
    theme: ColorSchemeSetting = "system"


CONFIG_SECTIONS: dict[str, type[BaseModel]] = {"ui": UiConfig}


@dataclass(frozen=True, slots=True)
class Tokens:
    """设计常量，所有界面模块从这里取值以保持一致（需求 UI-01）。单位为逻辑像素或毫秒。"""

    spacing_xs: int = 4
    spacing_s: int = 8
    spacing_m: int = 12
    spacing_l: int = 16
    spacing_xl: int = 24
    radius_s: int = 4
    radius_m: int = 8
    fade_ms: int = 150  # 译文替换的淡入时长（需求 RACE-06）

    # 字号（像素）。页标题用 Segoe UI Variable Display，其余用 Segoe UI Variable Text。
    page_title_px: int = 28
    title_px: int = 14
    caption_px: int = 12
    # 设置窗口一类“导航加卡片”界面的版式。
    page_margin: int = 40  # 页面内容的左右留白
    card_gap: int = 4  # 同组卡片之间的间距
    group_gap: int = 24  # 卡片分组之间的间距
    card_padding: int = 16
    card_min_height: int = 68
    card_radius: int = 8
    icon_px: int = 20  # 卡片左侧图标
    nav_width: int = 280
    nav_item_height: int = 40
    nav_icon_px: int = 18
    nav_indicator_width: int = 3  # 选中项左侧的强调色竖条
    # Mica 不可用（系统版本过低、offscreen 平台）时窗口使用的不透明底色。
    window_light: str = "#F3F3F3"
    window_dark: str = "#202020"


TOKENS = Tokens()


@dataclass(frozen=True, slots=True)
class ThemeColors:
    """当前深浅色下的颜色。强调色取自系统（QPalette.Accent），其余随深浅色切换。

    card、card_hover 是半透明色，叠在 Mica 或 ``window`` 之上，比底色略亮（深色主题下略浅）。
    """

    dark: bool
    window: QColor
    card: QColor
    card_hover: QColor
    card_border: QColor
    text: QColor
    text_secondary: QColor
    accent: QColor
    accent_text: QColor
    accent_strong: QColor
    """用作文字和细线的强调色：深色主题下调亮、浅色主题下调暗，在卡片上看得清（例如“当前引擎”标识）。"""
    critical: QColor
    nav_hover: QColor
    nav_selected: QColor


def theme_colors(app: QApplication | None = None, *, dark: bool | None = None) -> ThemeColors:
    """当前的颜色。没有 QApplication 时按浅色处理；dark 不为 None 时强制取深色或浅色那一套（测试和预览用）。

    每次绘制时调用，切换主题后立即生效。
    """
    instance = app if app is not None else QApplication.instance()
    if dark is None:
        dark = is_dark(instance) if isinstance(instance, QApplication) else False
    palette = QGuiApplication.palette()
    accent = palette.color(QPalette.ColorRole.Accent)
    accent_text = QColor("#FFFFFF") if accent.lightnessF() < 0.62 else QColor("#000000")
    accent_strong = _readable_accent(accent, dark=dark)
    if dark:
        return ThemeColors(
            dark=True,
            window=QColor(TOKENS.window_dark),
            card=QColor(255, 255, 255, 13),
            card_hover=QColor(255, 255, 255, 21),
            card_border=QColor(255, 255, 255, 24),
            text=QColor("#FFFFFF"),
            text_secondary=QColor(255, 255, 255, 190),
            accent=accent,
            accent_text=accent_text,
            accent_strong=accent_strong,
            critical=QColor("#FF99A4"),
            nav_hover=QColor(255, 255, 255, 15),
            nav_selected=QColor(255, 255, 255, 20),
        )
    return ThemeColors(
        dark=False,
        window=QColor(TOKENS.window_light),
        card=QColor(255, 255, 255, 179),
        card_hover=QColor(255, 255, 255, 235),
        card_border=QColor(0, 0, 0, 15),
        text=QColor("#1A1A1A"),
        text_secondary=QColor(0, 0, 0, 158),
        accent=accent,
        accent_text=accent_text,
        accent_strong=accent_strong,
        critical=QColor("#C42B1C"),
        nav_hover=QColor(0, 0, 0, 10),
        nav_selected=QColor(0, 0, 0, 15),
    )


def _readable_accent(accent: QColor, *, dark: bool) -> QColor:
    """强调色用作文字时的颜色：深色主题下亮度至少 0.68，浅色主题下亮度至多 0.42，色相与饱和度不变。"""
    lightness = accent.lightnessF()
    lightness = max(lightness, 0.68) if dark else min(lightness, 0.42)
    return QColor.fromHslF(accent.hslHueF(), accent.hslSaturationF(), lightness, accent.alphaF())


def ui_font(pixel_size: int, *, semibold: bool = False, display: bool = False) -> QFont:
    """界面字体：按像素大小取 Segoe UI Variable（大标题用 Display 光学尺寸），缺失时依次回退。"""
    families = set(QFontDatabase.families())
    candidates = ("Segoe UI Variable Display", *_PREFERRED_FONTS) if display else _PREFERRED_FONTS
    font = QFont()
    # 英文字体里没有汉字时，按列表顺序回退：最后是微软雅黑，避免落到宋体一类的衬线字体。
    available = [family for family in (*candidates, "Microsoft YaHei UI") if family in families]
    if available:
        font.setFamilies(available)
    font.setPixelSize(pixel_size)
    font.setWeight(QFont.Weight.DemiBold if semibold else QFont.Weight.Normal)
    return font


_PRIMARY_BUTTON_QSS: Final = (
    'QPushButton[primary="true"] {'
    " background: palette(highlight); color: palette(highlighted-text);"
    " border: 1px solid palette(highlight); border-radius: %(radius)dpx; padding: 5px 16px; }"
    'QPushButton[primary="true"]:hover { border-color: palette(highlighted-text); }'
    'QPushButton[primary="true"]:disabled {'
    " background: palette(button); color: palette(mid); border-color: palette(mid); }"
)


def style_primary_button(button: QPushButton, glyph: str | None = None) -> None:
    """把按钮画成主要按钮：强调色填充、强调色上的文字色。强调色随系统，样式表里只引用调色板角色。

    图标与按钮文字用同一组调色板角色，在每次绘制时取色：可用时是 highlighted-text，禁用时是 mid，
    与样式表一致。创建时固定颜色的话，切换深浅色后图标不变色，禁用时（灰底）白色图标也看不清。
    """
    button.setProperty("primary", True)
    button.setStyleSheet(_PRIMARY_BUTTON_QSS % {"radius": TOKENS.radius_s})
    if glyph is not None:
        button.setIcon(fluent_icon(glyph, role="on_accent"))


# 引擎实例的标识颜色在配置里没有指定时，按实例号稳定地取下面一种（引擎设置列表的图标）。
_ENGINE_PALETTE = ("#0F6CBD", "#C239B3", "#0E7A0D", "#CA5010", "#8764B8", "#038387", "#986F0B", "#D13438")

_PREFERRED_FONTS = ("Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI")


def apply_app_style(app: QApplication, scheme: ColorSchemeSetting = "system") -> None:
    """设置应用样式、字体与深浅色（需求 UI-02）。可以重复调用，主题配置变化时立即生效。"""
    available_styles = QStyleFactory.keys()
    styles = {style.lower(): style for style in available_styles}
    for candidate in ("windows11", "fusion", "windowsvista", "windows"):
        if candidate in styles:
            app.setStyle(styles[candidate])
            break
    families = set(QFontDatabase.families())
    for family in _PREFERRED_FONTS:
        if family in families:
            font = QFont(family)
            font.setPointSizeF(10.0)
            app.setFont(font)
            break
    hints = app.styleHints()
    hints.setColorScheme(
        {
            "system": Qt.ColorScheme.Unknown,
            "light": Qt.ColorScheme.Light,
            "dark": Qt.ColorScheme.Dark,
        }[scheme]
    )


def is_dark(app: QApplication) -> bool:
    return app.styleHints().colorScheme() == Qt.ColorScheme.Dark


def engine_color(engine_id: str, configured: str = "") -> QColor:
    """实例的标识颜色：配置里给了合法颜色就用它，否则按实例号稳定地从调色板里取。"""
    if configured:
        color = QColor(configured)
        if color.isValid():
            return color
    index = hashlib.sha256(engine_id.encode("utf-8")).digest()[0] % len(_ENGINE_PALETTE)
    return QColor(_ENGINE_PALETTE[index])


class _Margins(ctypes.Structure):
    _fields_ = (
        ("left", ctypes.c_int),
        ("right", ctypes.c_int),
        ("top", ctypes.c_int),
        ("bottom", ctypes.c_int),
    )


_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMSBT_MAINWINDOW = 2  # Mica
_MICA_MIN_BUILD = 22621


def mica_supported() -> bool:
    """Mica 要求 Windows 11 build 22621 及以上（需求 6.6 节）。"""
    if sys.platform != "win32":
        return False
    return sys.getwindowsversion().build >= _MICA_MIN_BUILD


def enable_mica(widget: QWidget, *, dark: bool) -> bool:
    """给顶层窗口加 Mica 背景，返回是否成功。

    要同时满足三项：WA_TranslucentBackground、DwmExtendFrameIntoClientArea(-1)、写入 backdrop 属性，
    缺一项窗口会渲染成黑色。切换 WA_TranslucentBackground 会让 Qt 重建窗口句柄，所以先设属性再取句柄；
    窗口句柄之后再被重建时需要重新调用。
    """
    if not mica_supported():
        widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        return False
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    if _apply_dwm_backdrop(int(widget.winId()), dark=dark):
        return True
    # 只剩透明属性而没有 Mica 时窗口是黑的，撤掉属性回到不透明背景。
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    return False


def _apply_dwm_backdrop(hwnd: int, *, dark: bool) -> bool:
    try:
        dwmapi = ctypes.windll.dwmapi
        margins = _Margins(-1, -1, -1, -1)
        if dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)) != 0:
            return False
        dark_value = ctypes.c_int(1 if dark else 0)
        dwmapi.DwmSetWindowAttribute(
            hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark_value), ctypes.sizeof(dark_value)
        )
        backdrop = ctypes.c_int(_DWMSBT_MAINWINDOW)
        result = dwmapi.DwmSetWindowAttribute(
            hwnd, _DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(backdrop), ctypes.sizeof(backdrop)
        )
    except (AttributeError, OSError):
        logger.warning("调用 DWM 设置 Mica 背景失败", exc_info=True)
        return False
    return result == 0


# ---------------------------------------------------------------- Fluent 图标（需求 UI-01、UI-03）

FLUENT_ICONS_FAMILY: Final = "Segoe Fluent Icons"
"""Windows 11 自带的图标字体的字体族名。"""

_FLUENT_ICONS_FILE: Final = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "SegoeIcons.ttf"
"""字体文件。Qt 的平台插件没有把系统字体全部登记时（例如 offscreen 平台），直接从这个文件加载。"""

MDL2_ICONS_FAMILY: Final = "Segoe MDL2 Assets"
_ICON_FONT_CANDIDATES = (
    (FLUENT_ICONS_FAMILY, _FLUENT_ICONS_FILE),
    (MDL2_ICONS_FAMILY, _FLUENT_ICONS_FILE.with_name("segmdl2.ttf")),
)


class Glyph:
    """Segoe Fluent Icons 里本程序用到的图标码位。图标按码位绘制，是矢量图，在任何缩放比例下都清晰。"""

    SETTINGS: Final = "\ue713"
    KEYBOARD: Final = "\ue765"
    SCREEN_CAPTURE: Final = "\ue7c5"
    TRANSLATE: Final = "\uf2b7"
    LIGHTNING: Final = "\ue945"
    PACKAGE: Final = "\ue7b8"
    INFO: Final = "\ue946"
    ADD: Final = "\ue710"
    DELETE: Final = "\ue74d"
    UP: Final = "\ue74a"
    DOWN: Final = "\ue74b"
    REFRESH: Final = "\ue72c"
    SEARCH: Final = "\ue71e"
    WARNING: Final = "\ue7ba"
    DOWNLOAD: Final = "\ue896"
    CIRCLE_FILL: Final = "\ue91f"
    FOLDER: Final = "\ue8b7"
    SWITCH: Final = "\ue8ab"
    GLOBE: Final = "\ue774"
    PALETTE: Final = "\ue790"
    CAMERA: Final = "\ue722"
    CHAT: Final = "\ue8bd"
    FONT: Final = "\ue8d2"
    LAYOUT: Final = "\ue9a3"
    CHIP: Final = "\ue950"
    TABLE: Final = "\ue8a1"
    CALCULATOR: Final = "\ue8ef"
    CHART: Final = "\ue9d2"
    UNWARP: Final = "\ue8e6"
    ITALIC: Final = "\ue8db"
    PICTURE: Final = "\ue91b"
    SLIDERS: Final = "\ue9e9"
    STOPWATCH: Final = "\ue916"
    PIN: Final = "\ue7b7"
    POWER: Final = "\ue7e8"
    LOCK: Final = "\ue72e"
    LINK: Final = "\ue71b"
    KEY: Final = "\ue8d7"
    PENCIL: Final = "\ue70f"
    PUZZLE: Final = "\uea86"
    LIST: Final = "\ue8fd"
    LINES: Final = "\ue8e4"
    TAG: Final = "\ue8ec"
    LIBRARY: Final = "\ue8f1"
    TRANSLATE_AA: Final = "\ue8c1"
    SYSTEM: Final = "\ue770"  # 电脑：Windows 自带 OCR
    DOCUMENT: Final = "\ue8a5"  # 文档：PaddleOCR
    COMPLETED: Final = "\ue930"  # 带圆圈的对勾：已安装、就绪
    CHECK: Final = "\ue73e"  # 对勾：当前使用的引擎
    ERROR: Final = "\ue783"  # 带圆圈的感叹号：出错
    BLOCKED: Final = "\ue733"  # 禁止符号：不可用
    SYNC: Final = "\ue895"  # 循环箭头：安装中、加载中
    RECENT: Final = "\ue823"  # 时钟：排队中
    UPDATE: Final = "\ue777"  # 更新
    OPEN_IN_NEW: Final = "\ue8a7"  # 在新窗口中打开：发布说明
    CANCEL: Final = "\ue711"  # 叉号：取消
    FILTER: Final = "\ue71c"  # 漏斗：清洗
    CUT: Final = "\ue8c6"  # 剪刀：拆分、分句
    CODE: Final = "\ue943"  # 花括号：结果转换
    MERGE: Final = "\uea3c"  # 汇合箭头：聚合、拼回
    HISTORY: Final = "\ue81c"  # 带箭头的时钟：译文缓存
    GRID: Final = "\ue8a9"  # 四个方块：分批
    SEND: Final = "\ue724"  # 纸飞机：提交请求


_icon_font_ready: bool | None = None
_icon_family = FLUENT_ICONS_FAMILY
_icon_raw_font: QRawFont | None = None
_icon_glyph_cache: dict[str, str] = {}
_ICON_ALIASES = {Glyph.LAYOUT: Glyph.GRID, Glyph.BLOCKED: Glyph.ERROR}


def fluent_icons_available() -> bool:
    """图标字体是否可用。第一次调用时尝试从系统字体目录加载，之后沿用结果。需要先创建 QGuiApplication。"""
    global _icon_font_ready, _icon_family, _icon_raw_font
    if _icon_font_ready is None:
        _icon_font_ready = False
        _icon_raw_font = None
        _icon_glyph_cache.clear()
        for family, path in _ICON_FONT_CANDIDATES:
            available = family in QFontDatabase.families()
            if not available and path.is_file():
                font_id = QFontDatabase.addApplicationFont(str(path))
                available = font_id >= 0 and family in QFontDatabase.applicationFontFamilies(font_id)
            if available:
                _icon_family = family
                _icon_raw_font = QRawFont.fromFont(QFont(family, 16))
                _icon_font_ready = True
                break
        if not _icon_font_ready:
            logger.warning("找不到 Segoe Fluent Icons 或 Segoe MDL2 Assets，界面图标不显示")
    return _icon_font_ready


def fluent_icon_font(pixel_size: int) -> QFont:
    """按 Fluent、MDL2 顺序选择系统图标字体；文字由 fluent_icon_text 解析缺失码位。"""
    fluent_icons_available()
    font = QFont(_icon_family)
    font.setPixelSize(pixel_size)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


def fluent_icon_text(glyph: str) -> str:
    """把缺失的私用区码位映射到同一字体中的替代图标，供按钮、标签与自绘区域共用。"""
    fluent_icons_available()
    if glyph not in _icon_glyph_cache:
        raw = _icon_raw_font
        result = ""
        for character in glyph:
            if raw is None or raw.supportsCharacter(ord(character)):
                result += character
                continue
            alias = _ICON_ALIASES.get(character, Glyph.WARNING)
            result += alias if raw.supportsCharacter(ord(alias)) else ""
        _icon_glyph_cache[glyph] = result
    return _icon_glyph_cache[glyph]


type IconRole = Literal["text", "on_accent"]
"""图标跟随调色板时用哪组颜色：text 是普通文字色，on_accent 是强调色底上的文字色（主要按钮）。"""


class _GlyphIconEngine(QIconEngine):
    """按码位绘制字体图标。图标在每次绘制时才光栅化，所以任何尺寸和缩放比例下都是矢量清晰度。

    color 为 None 时跟随调色板，每次绘制时取色：
    - role 为 text：普通状态用文字色，禁用时用禁用文字色，选中时用高亮文字色；
    - role 为 on_accent：普通状态用高亮文字色（强调色底上的文字），禁用时用 mid（主要按钮禁用时的文字色）。
    """

    def __init__(self, glyph: str, color: QColor | None = None, role: IconRole = "text") -> None:
        super().__init__()
        self._glyph = glyph
        self._color = color
        self._role: IconRole = role

    def clone(self) -> QIconEngine:
        return _GlyphIconEngine(self._glyph, self._color, self._role)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        del state
        if not fluent_icons_available():
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setFont(fluent_icon_font(max(1, rect.height())))
        painter.setPen(self._pen_color(mode))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), fluent_icon_text(self._glyph))
        painter.restore()

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return pixmap

    def _pen_color(self, mode: QIcon.Mode) -> QColor:
        if self._color is not None:
            color = QColor(self._color)
            if mode == QIcon.Mode.Disabled:
                color.setAlphaF(color.alphaF() * 0.4)
            return color
        palette = QGuiApplication.palette()
        if self._role == "on_accent":
            if mode == QIcon.Mode.Disabled:
                return palette.color(QPalette.ColorRole.Mid)
            return palette.color(QPalette.ColorRole.HighlightedText)
        if mode == QIcon.Mode.Disabled:
            return palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        if mode == QIcon.Mode.Selected:
            return palette.color(QPalette.ColorRole.HighlightedText)
        return palette.color(QPalette.ColorRole.Text)


def fluent_icon(glyph: str, color: QColor | str | None = None, *, role: IconRole = "text") -> QIcon:
    """Fluent 图标。glyph 取 ``Glyph`` 里的码位；color 为 None 时按 role 跟随调色板（深浅色主题下都清晰）。"""
    return QIcon(_GlyphIconEngine(glyph, None if color is None else QColor(color), role))
