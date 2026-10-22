r"""截屏选区的坐标换算（需求 CAP-02、CAP-03、CAP-04、CAP-07，架构文档 11.4 节）。本模块不依赖 Qt。

# Qt 6 的两套坐标

程序按 Per-Monitor DPI Aware V2 运行（Qt 6 在 Windows 上的默认值）。系统给出的是物理像素（原生坐标），
Qt 换算成逻辑坐标交给程序。每块屏幕各自换算：设屏幕的原生左上角为 $(o_x, o_y)$、设备像素比为 $r$，
原生坐标 $(n_x, n_y)$ 对应的逻辑坐标是

$$
l_x = o_x + \frac{n_x - o_x}{r},\qquad l_y = o_y + \frac{n_y - o_y}{r}.
$$

所以屏幕在逻辑坐标里的左上角与原生左上角相同，宽高是原生宽高除以 $r$ 后取整。缩放比例不同的屏幕拼在一起时，
逻辑坐标里会出现缝隙（例如左侧 150% 的 2560 宽副屏，逻辑范围是 $[-2560, -853)$，与主屏的 $0$ 之间空出 853），
这正是每块屏幕要单独一个蒙版窗口的原因。副屏在主屏左侧或上方时 $o_x$、$o_y$ 为负，公式不变。
屏幕旋转只改变原生宽高（竖屏的宽小于高），冻结画面也按旋转后的方向给出，公式同样不变。

# 选区换算

蒙版窗口收到的鼠标位置是逻辑坐标的浮点数，它本身就是 Qt 从整数原生坐标按上式除出来的。反过来，
选区边界相对屏幕左上角的物理像素是

$$
p_x = \operatorname{round}\bigl((l_x - o_x)\, r\bigr),\qquad
p_y = \operatorname{round}\bigl((l_y - o_y)\, r\bigr).
$$

鼠标位置由整数原生坐标精确换算而来时，$(l_x - o_x)\,r$ 等于原生偏移，取整误差只来自浮点舍入，
结果与鼠标所在的物理像素完全相同；即使逻辑坐标带有不超过 $0.5/r$ 的偏差，
取整后的误差也不超过 1 物理像素（CAP-03）。
换算用设备像素比而不用“冻结画面宽度 ÷ 逻辑宽度”：逻辑宽度是取整后的值，175% 下 2560 宽的屏幕逻辑宽度是
1463，$2560 / 1463 \approx 1.7498$，用它换算右边缘会差出零点几个像素，用 $r$ 换算没有这个误差。
结果最后限制在冻结画面的范围 $[0, W] \times [0, H]$ 内。
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

MIN_SELECTION: Final = 5
"""选区的宽或高小于这个逻辑像素数时视为取消（需求 CAP-04）。"""


@dataclass(frozen=True, slots=True)
class Rect:
    """整数矩形，左上闭、右下开。"""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)


@dataclass(frozen=True, slots=True)
class ScreenGeometry:
    """一块屏幕。

    - logical：屏幕在 Qt 全局逻辑坐标里的矩形（QScreen.geometry）；
    - dpr：设备像素比（QScreen.devicePixelRatio），例如 150% 缩放为 1.5；
    - pixel_width、pixel_height：冻结画面的物理像素尺寸（grabWindow 得到的图像大小）。
    """

    name: str
    logical: Rect
    dpr: float
    pixel_width: int
    pixel_height: int

    def __post_init__(self) -> None:
        if self.dpr <= 0 or self.pixel_width <= 0 or self.pixel_height <= 0:
            raise ValueError("屏幕的设备像素比与像素尺寸必须为正")

    def contains(self, x: float, y: float) -> bool:
        """逻辑坐标的点是否在屏幕内（左上闭、右下开）。"""
        rect = self.logical
        return rect.x <= x < rect.right and rect.y <= y < rect.bottom


@dataclass(frozen=True, slots=True)
class Selection:
    """一次框选的结果。

    - pixels：选区在该屏冻结画面里的物理像素矩形，截图按它裁切；
    - logical：选区在全局逻辑坐标里的整数矩形，由 pixels 换算回来后取整，覆盖窗口按它摆放（OV-01）。
    """

    screen: ScreenGeometry
    pixels: Rect
    logical: Rect


def screen_from_native(name: str, native: Rect, dpr: float) -> ScreenGeometry:
    """按 Qt 6 的规则由原生矩形与设备像素比得到屏幕：左上角不变，宽高除以设备像素比后取整。

    正式代码直接读 QScreen；这个函数供测试构造虚构的多屏布局，也记录了 Qt 的换算方式。
    """
    logical = Rect(native.x, native.y, round(native.width / dpr), round(native.height / dpr))
    return ScreenGeometry(name, logical, dpr, native.width, native.height)


def native_to_logical(screen: ScreenGeometry, nx: float, ny: float) -> tuple[float, float]:
    """原生坐标（物理像素，全局）→ 逻辑坐标，即 Qt 交给程序的鼠标位置。见模块说明的第一个公式。"""
    rect = screen.logical
    return rect.x + (nx - rect.x) / screen.dpr, rect.y + (ny - rect.y) / screen.dpr


def clamp_to_screen(screen: ScreenGeometry, x: float, y: float) -> tuple[float, float]:
    """把逻辑坐标的点限制在屏幕范围内（需求 CAP-02）。右、下边界取闭区间：选区可以一直拉到屏幕边缘。"""
    rect = screen.logical
    right = rect.x + screen.pixel_width / screen.dpr
    bottom = rect.y + screen.pixel_height / screen.dpr
    return min(max(x, rect.x), right), min(max(y, rect.y), bottom)


def logical_to_pixel(screen: ScreenGeometry, x: float, y: float) -> tuple[int, int]:
    """逻辑坐标 → 相对屏幕左上角的物理像素，限制在冻结画面范围内。见模块说明的第二个公式。"""
    rect = screen.logical
    px = _round_half_up((x - rect.x) * screen.dpr)
    py = _round_half_up((y - rect.y) * screen.dpr)
    return min(max(px, 0), screen.pixel_width), min(max(py, 0), screen.pixel_height)


def pixel_to_logical(screen: ScreenGeometry, px: float, py: float) -> tuple[float, float]:
    """相对屏幕左上角的物理像素 → 全局逻辑坐标。"""
    rect = screen.logical
    return rect.x + px / screen.dpr, rect.y + py / screen.dpr


def selection_from_drag(
    screen: ScreenGeometry, start: tuple[float, float], end: tuple[float, float]
) -> Selection | None:
    """鼠标按下点与当前点（全局逻辑坐标）→ 选区。两点先限制在按下时所在的屏幕内（CAP-02）。

    宽或高小于 MIN_SELECTION 个逻辑像素时返回 None，表示视为取消（CAP-04）。
    """
    x0, y0 = clamp_to_screen(screen, *start)
    x1, y1 = clamp_to_screen(screen, *end)
    left, right = min(x0, x1), max(x0, x1)
    top, bottom = min(y0, y1), max(y0, y1)
    if right - left < MIN_SELECTION or bottom - top < MIN_SELECTION:
        return None
    px0, py0 = logical_to_pixel(screen, left, top)
    px1, py1 = logical_to_pixel(screen, right, bottom)
    if px1 <= px0 or py1 <= py0:
        return None
    pixels = Rect(px0, py0, px1 - px0, py1 - py0)
    return Selection(screen, pixels, logical_rect(screen, pixels))


def logical_rect(screen: ScreenGeometry, pixels: Rect) -> Rect:
    """物理像素矩形 → 全局逻辑坐标的整数矩形。四条边各自换算后取整，宽高由取整后的边相减得到。"""
    left, top = pixel_to_logical(screen, pixels.x, pixels.y)
    right, bottom = pixel_to_logical(screen, pixels.right, pixels.bottom)
    x, y = _round_half_up(left), _round_half_up(top)
    return Rect(x, y, max(1, _round_half_up(right) - x), max(1, _round_half_up(bottom) - y))


def native_rect(screen: ScreenGeometry) -> Rect:
    """屏幕在原生坐标（全局物理像素）里的矩形：左上角与逻辑左上角相同（见模块说明），宽高是冻结画面的像素尺寸。"""
    return Rect(screen.logical.x, screen.logical.y, screen.pixel_width, screen.pixel_height)


def intersect(a: Rect, b: Rect) -> Rect | None:
    """两个矩形的交集；不相交时返回 None。"""
    left, top = max(a.x, b.x), max(a.y, b.y)
    right, bottom = min(a.right, b.right), min(a.bottom, b.bottom)
    if right <= left or bottom <= top:
        return None
    return Rect(left, top, right - left, bottom - top)


@dataclass(frozen=True, slots=True)
class NativePiece:
    """原生坐标矩形落在一块屏幕上的部分。

    - index：屏幕在列表里的序号；
    - source：这一部分在该屏冻结画面里的物理像素矩形；
    - target：这一部分在截图（以矩形左上角为原点）里的位置，宽高与 source 相同。
    """

    index: int
    source: Rect
    target: Rect


def native_pieces(screens: Sequence[ScreenGeometry], rect: Rect) -> list[NativePiece]:
    """原生坐标的矩形按屏幕切开，返回落在各屏上的部分，用于不经过蒙版、直接按矩形截取（可以跨屏）。

    缩放比例不同的屏幕在原生坐标里首尾相接、互不重叠，所以各部分拼起来就是矩形里有屏幕的那部分；
    矩形里不属于任何屏幕的地方（屏幕之间的空隙）没有对应的部分，截图里那里留黑。
    """
    pieces: list[NativePiece] = []
    for index, screen in enumerate(screens):
        bounds = native_rect(screen)
        overlap = intersect(bounds, rect)
        if overlap is None:
            continue
        source = Rect(overlap.x - bounds.x, overlap.y - bounds.y, overlap.width, overlap.height)
        target = Rect(overlap.x - rect.x, overlap.y - rect.y, overlap.width, overlap.height)
        pieces.append(NativePiece(index, source, target))
    return pieces


def anchor_piece(pieces: Sequence[NativePiece]) -> NativePiece | None:
    """覆盖窗口按哪块屏幕摆放：矩形在它上面的面积最大的那块，面积相同时取靠前的。"""
    return max(pieces, key=lambda p: p.source.width * p.source.height, default=None)


def physical_size_text(selection: Selection | None) -> str:
    """蒙版上显示的选区尺寸（物理像素，也就是截图的尺寸）。"""
    if selection is None:
        return ""
    return f"{selection.pixels.width} × {selection.pixels.height}"


def screens_signature(screens: Sequence[ScreenGeometry]) -> tuple[tuple[str, Rect, float, int, int], ...]:
    """屏幕布局的指纹：截屏期间指纹变化（插拔、改缩放、改分辨率、旋转）就取消这次截屏（CAP-07）。"""
    return tuple((s.name, s.logical, s.dpr, s.pixel_width, s.pixel_height) for s in screens)


def _round_half_up(value: float) -> int:
    """四舍五入到整数。先吸收 1e-6 以内的浮点误差：(n / r) × r 可能得到 n − 1e-13 这样的值。"""
    return math.floor(value + 0.5 + 1e-6)
