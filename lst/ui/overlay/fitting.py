r"""覆盖窗口的字号适配（需求 OV-03，架构文档 11.2、11.5 节）。

字号按设备像素求：给定包围盒的设备像素宽高，找出能让换行后的文字放进包围盒的最大整数字号。
“放得进”的判据由调用方给出（覆盖窗口用 QFontMetricsF 测换行后的外接矩形），本模块的二分查找与估算
不依赖 Qt，可以单独测试。

二分查找的正确性依赖单调性：字号越大，每行容纳的字越少、行高越大，外接矩形的宽和高都不减。
所以 ``fits`` 在 $[lo, hi]$ 上是先真后假的，二分找到的就是最后一个为真的字号。
个别字体在相邻字号间的换行位置会跳动，单调性只是近似成立；偶尔得到的字号比真正的最大值小一两号，
不会出现放不下的情况，因为返回值本身一定通过了判据（或者是下限 lo）。

估算字号用于分帧测量时还没测到的段（架构文档 11.2 节）：把每个字按宽度折算成“字宽单位”
（汉字、假名、全角字符为 1，其余为 0.55），设字号为 $p$、行高为 $1.35p$，
包围盒宽 $w$、高 $h$，总字宽为 $u$ 个单位，则行数约为 $u p / w$，总高度约为 $1.35\,u\,p^2 / w$。
令它等于 $h$ 得到

$$
p \approx \sqrt{\frac{w h}{1.35\, u}},
$$

再限制在 $[lo, h / 1.35]$ 内（一行字也不能比包围盒高）。
"""

import math
from collections.abc import Callable
from typing import Final

MIN_FONT_PX: Final = 6
"""最小字号（设备像素）。更小的字在屏幕上已经无法阅读。"""

MAX_FONT_PX: Final = 160
LINE_HEIGHT: Final = 1.35


def char_units(text: str) -> float:
    """文字的总宽度，以“字宽单位”计：东亚宽字符为 1，其余为 0.55。"""
    total = 0.0
    for char in text:
        code = ord(char)
        wide = 0x2E80 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF or 0xF900 <= code <= 0xFAFF
        wide = wide or 0xFF00 <= code <= 0xFF60 or 0x3000 <= code <= 0x303F
        total += 1.0 if wide else 0.55
    return total


def estimate_font_size(text: str, width: float, height: float, lo: int = MIN_FONT_PX) -> int:
    """估算字号，见模块说明。"""
    units = max(char_units(text), 1.0)
    if width <= 0 or height <= 0:
        return lo
    guess = math.sqrt(width * height / (LINE_HEIGHT * units))
    upper = height / LINE_HEIGHT
    return int(max(lo, min(guess, upper, MAX_FONT_PX)))


def fit_font_size(fits: Callable[[int], bool], lo: int = MIN_FONT_PX, hi: int = MAX_FONT_PX) -> int:
    """[lo, hi] 中让 fits 为真的最大整数字号；全部为假时返回 lo（文字以最小字号显示，超出部分裁掉）。"""
    if hi < lo:
        hi = lo
    if not fits(lo):
        return lo
    good, bad = lo, hi + 1
    while bad - good > 1:
        middle = (good + bad) // 2
        if fits(middle):
            good = middle
        else:
            bad = middle
    return good
