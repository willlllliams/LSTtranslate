r"""覆盖窗口的坐标与缩放（需求 OV-01、OV-05、CAP-07、UI-04，架构文档 11.5 节）。本模块不依赖 Qt。

# 三种坐标

- 截图坐标：OCR 的包围盒，相对选区左上角的物理像素，选区宽 $W$、高 $H$；
- 窗口逻辑坐标：Qt 的窗口坐标；
- 设备像素：窗口当前所在屏幕的物理像素。

设截屏所在屏幕的设备像素比为 $r_c$，窗口当前所在屏幕的设备像素比为 $r_d$，用户滚轮缩放的倍数为 $z$。
窗口的逻辑尺寸是

$$
w = \operatorname{round}\Bigl(\frac{W z}{r_c}\Bigr),\qquad
h = \operatorname{round}\Bigl(\frac{H z}{r_c}\Bigr),
$$

$z = 1$ 时窗口在截屏的屏幕上恰好盖住选区。一个截图像素对应的逻辑长度与设备像素数分别是

$$
s = \frac{z}{r_c},\qquad k = s\, r_d = \frac{z\, r_d}{r_c}.
$$

窗口绘制时把画笔换到设备像素坐标（先按 $1/r_d$ 缩放），包围盒乘以 $k$ 就是它在屏幕上的物理像素位置，
字号也按设备像素求。这样窗口被拖到缩放比例不同的屏幕上时，$r_d$ 变化，$k$ 随之变化，
字号按新的设备像素重新适配，文字始终按屏幕的实际分辨率光栅化，不会先在低分辨率上画好再放大（UI-04）。
Qt 拖动窗口跨屏时保持逻辑尺寸不变，所以窗口占的逻辑大小不变，内容在新屏幕上同样清晰。

# 以光标为中心缩放

滚轮把倍数从 $z$ 改为 $z'$ 时，光标下的截图点要保持在原处。设光标在窗口内的逻辑位置为 $c$，
窗口左上角为 $p$，光标下的截图点是 $c / s$；缩放后它在窗口内的位置是 $c\, z'/z$，所以新的左上角为

$$
p' = p + c - c\,\frac{z'}{z}.
$$
"""

from dataclasses import dataclass
from typing import Final

from lst.contracts.ocr import BBox

MIN_ZOOM: Final = 0.25
MAX_ZOOM: Final = 4.0
ZOOM_STEP: Final = 1.1
"""滚轮每一格（120 个单位）把倍数乘以或除以这个数。"""


@dataclass(frozen=True, slots=True)
class RectF:
    """浮点矩形。"""

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


def window_size(
    pixel_width: int, pixel_height: int, capture_dpr: float, zoom: float = 1.0
) -> tuple[int, int]:
    """窗口的逻辑尺寸，见模块说明的第一个公式。至少 1×1。"""
    width = max(1, round(pixel_width * zoom / capture_dpr))
    height = max(1, round(pixel_height * zoom / capture_dpr))
    return width, height


def logical_per_pixel(capture_dpr: float, zoom: float) -> float:
    """一个截图像素对应的窗口逻辑长度 s。"""
    return zoom / capture_dpr


def device_per_pixel(capture_dpr: float, zoom: float, device_dpr: float) -> float:
    """一个截图像素对应的设备像素数 k。"""
    return zoom * device_dpr / capture_dpr


def bbox_to_view(bbox: BBox, scale: float) -> RectF:
    """截图坐标的包围盒乘以比例（s 得到窗口逻辑坐标，k 得到设备像素坐标）。"""
    return RectF(bbox.x0 * scale, bbox.y0 * scale, bbox.width * scale, bbox.height * scale)


def view_to_pixel(x: float, y: float, scale: float) -> tuple[float, float]:
    """窗口坐标（逻辑或设备，与 scale 对应）→ 截图坐标。"""
    return x / scale, y / scale


def clamp_zoom(zoom: float) -> float:
    return min(max(zoom, MIN_ZOOM), MAX_ZOOM)


def wheel_zoom(zoom: float, angle_delta: int) -> float:
    """滚轮转过 angle_delta（120 为一格）后的倍数，限制在 [MIN_ZOOM, MAX_ZOOM]。"""
    return clamp_zoom(zoom * ZOOM_STEP ** (angle_delta / 120))


def zoom_about(
    position: tuple[float, float], cursor: tuple[float, float], old_zoom: float, new_zoom: float
) -> tuple[float, float]:
    """以光标为中心缩放后窗口左上角的新位置，见模块说明的最后一个公式。position、cursor 都是逻辑坐标，
    cursor 相对窗口左上角。"""
    ratio = new_zoom / old_zoom
    return position[0] + cursor[0] - cursor[0] * ratio, position[1] + cursor[1] - cursor[1] * ratio


def luminance(red: float, green: float, blue: float) -> float:
    """ITU-R BT.601 亮度，输入与输出都在 0 到 255 之间。"""
    return 0.299 * red + 0.587 * green + 0.114 * blue


def is_light(mean_luminance: float) -> bool:
    """底图平均亮度不低于 128 算亮底：亮底调亮并用黑字，暗底调暗并用白字（需求 OV-02）。"""
    return mean_luminance >= 128
