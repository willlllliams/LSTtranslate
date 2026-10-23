"""原图像素约束拼行：漏检词、图标、栏缝与字号变化。"""

from collections.abc import Sequence

import numpy as np
import pytest
from numpy.typing import NDArray

from lst.contracts.ocr import BBox, RawImage, TextLine
from lst.services.ocr.appearance import Appearance, ImageAppearance
from lst.services.ocr.paragraphs import GeometricMerger


def canvas() -> NDArray[np.uint8]:
    pixels = np.full((180, 320, 4), 20, dtype=np.uint8)
    pixels[:, :, 3] = 255
    return pixels


def letters(pixels: NDArray[np.uint8], box: BBox, value: int = 240) -> None:
    for x in range(box.x0 + 2, box.x1 - 3, 8):
        pixels[box.y0 + 5 : box.y1 - 5, x : x + 4, :3] = value


def appearance(pixels: NDArray[np.uint8]) -> ImageAppearance:
    height, width = pixels.shape[:2]
    return ImageAppearance(RawImage(width, height, width * 4, pixels.tobytes()))


def recognized(box: BBox, text: str) -> TextLine:
    return TextLine(box, text, 0.99)


def texts(lines: Sequence[TextLine], source: ImageAppearance) -> list[str]:
    merger = GeometricMerger(appearance=source.of, connects=source.connects, gutter=source.gutter)
    return [paragraph.text for paragraph in merger.merge(lines)]


def test_ascenders_and_descenders_do_not_change_the_font_size() -> None:
    lines = [recognized(BBox(10, 4, 90, 28), "scenes"), recognized(BBox(96, 0, 170, 30), "laying")]
    styles = {"scenes": Appearance((240, 240, 240), 13, 1.3), "laying": Appearance((240, 240, 240), 22, 1.3)}
    merger = GeometricMerger(appearance=lambda line: styles[line.text])
    assert [paragraph.text for paragraph in merger.merge(lines)] == ["scenes laying"]


def test_different_font_size_and_stroke_weight_remain_separate() -> None:
    lines = [recognized(BBox(10, 0, 90, 40), "Title"), recognized(BBox(96, 10, 170, 30), "label")]
    styles = {"Title": Appearance((240, 240, 240), 32, 3.0), "label": Appearance((240, 240, 240), 16, 1.2)}
    merger = GeometricMerger(appearance=lambda line: styles[line.text])
    assert len(merger.merge(lines)) == 2


@pytest.mark.parametrize("middle", ["text", "button", "blank", "gray-icon", "separator"])
def test_only_text_pixels_bridge_an_unrecognized_word(middle: str) -> None:
    pixels = canvas()
    lines = [recognized(BBox(10, 20, 80, 48), "alpha"), recognized(BBox(200, 20, 290, 48), "omega")]
    for line in lines:
        letters(pixels, line.bbox)
    if middle == "text":
        letters(pixels, BBox(85, 20, 195, 48))
    elif middle == "button":
        pixels[20:48, 85:195, :3] = 80
        letters(pixels, BBox(85, 20, 195, 48))
    elif middle == "gray-icon":
        letters(pixels, BBox(120, 20, 155, 48), 100)
    elif middle == "separator":
        pixels[25:43, 139:141, :3] = 240
    result = texts(lines, appearance(pixels))
    assert result == (["alpha omega"] if middle == "text" else ["alpha", "omega"])


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("right_dy,middle_dy", [(0, 0), (3, 9)])
def test_a_bridge_never_skips_an_existing_fragment(reverse: bool, right_dy: int, middle_dy: int) -> None:
    lines = [
        recognized(BBox(10, 10, 80, 34), "Development"),
        recognized(BBox(110, 10 + middle_dy, 170, 34 + middle_dy), "Mobile"),
        recognized(BBox(200, 10 + right_dy, 290, 34 + right_dy), "Community"),
    ]
    if reverse:
        lines.reverse()
    merger = GeometricMerger(
        appearance=lambda _line: Appearance((240, 240, 240), 18, 1.3),
        connects=lambda a, b: {a.text, b.text} == {"Development", "Community"},
    )
    assert [paragraph.text for paragraph in merger.merge(lines)] == ["Development", "Mobile", "Community"]


@pytest.mark.parametrize("ink_in_gutter", [False, True])
def test_geometric_gutters_require_continuous_pixel_whitespace(ink_in_gutter: bool) -> None:
    pixels = canvas()
    lines: list[TextLine] = []
    for row in range(3):
        y = 20 + row * 35
        for x, text in [(10, f"left{row}"), (125, f"right{row}")]:
            line = recognized(BBox(x, y, x + 100, y + 28), text)
            letters(pixels, line.bbox)
            lines.append(line)
        if ink_in_gutter and row == 1:
            letters(pixels, BBox(108, y, 128, y + 28))
    result = texts(lines, appearance(pixels))
    if ink_in_gutter:
        assert result == ["left0 right0 left1 right1 left2 right2"]
    else:
        assert result == ["left0 left1 left2", "right0 right1 right2"]


@pytest.mark.parametrize("text", ["HTTPServer", "XMLReader", "MOSTPOPULARAll"])
def test_acronym_boundary_without_control_spacing_keeps_one_label(text: str) -> None:
    pixels = canvas()
    line = recognized(BBox(10, 20, 300, 48), text)
    letters(pixels, line.bbox)
    assert [part.text for part in appearance(pixels).split(line)] == [text]
