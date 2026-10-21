"""从 PaddleX 的结果对象中取出转换要用的数据，整理成只含 Python 基本类型的结构（RawPage）。

PaddleX 3.7 的结果对象都是 dict 的子类，数组字段是 numpy 数组，版面块是 LayoutBlock 对象。
这里按名字取字段，不导入 paddlex，所以主环境的单元测试可以用 dict 和简单对象伪造结果。

版面模式（PP-StructureV3，LayoutParsingResultV2）用到的字段：

| 字段 | 内容 |
|---|---|
| width、height | 产线输出坐标所在图像（文档预处理之后）的宽高 |
| doc_preprocessor_res | 文档预处理结果；做了方向分类时有 angle（0/90/180/270，没做时为 -1） |
| parsing_res_list | 按阅读顺序（xycut 排序）排好的版面块：label、bbox（整数）、content |
| layout_det_res.boxes | 版面检测框：label、coordinate（浮点）、score，用来给版面块取置信度 |
| overall_ocr_res | 整页 OCR：rec_boxes（N×4）、rec_texts、rec_scores、rec_labels（"text" 或 "formula"）|
| table_res_list | 每个表格的 pred_html 与 cell_box_list（单元格框，整页坐标）|
| formula_res_list | 每个公式的 dt_polys（[x0, y0, x1, y1]）与 rec_formula（LaTeX）|
| model_settings | 本次实际开启的子能力，use_chart_recognition 决定图表块的 content 是不是数据表 |

parsing_res_list 中表格块的 content 就是该表格的 pred_html，图表块（开了图表识别时）的 content 是
PP-Chart2Table 输出的数据表文本，其他块的 content 是块内文本行的拼接。

文本模式（OCR 产线，OCRResult）用到 rec_boxes、rec_texts、rec_scores 与 doc_preprocessor_res。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from lst.workers.ocr_worker.geometry import Box, box_from


@dataclass(frozen=True, slots=True)
class RawLine:
    """整页 OCR 的一行。is_text 为 False 的是 PaddleX 并入的公式结果，不当作文本行。"""

    box: Box
    text: str
    score: float
    is_text: bool = True


@dataclass(frozen=True, slots=True)
class RawRegion:
    """版面检测框。"""

    label: str
    box: Box
    score: float


@dataclass(frozen=True, slots=True)
class RawBlock:
    """parsing_res_list 中的一个版面块。"""

    label: str
    box: Box
    content: str


@dataclass(frozen=True, slots=True)
class RawTable:
    html: str
    cell_boxes: tuple[Box, ...]


@dataclass(frozen=True, slots=True)
class RawFormula:
    box: Box
    latex: str


@dataclass(frozen=True, slots=True)
class RawPage:
    """一张图的产线输出，坐标都在 output_size 的图像里。文本模式只有 lines。"""

    output_size: tuple[int, int]
    angle: int | None
    lines: tuple[RawLine, ...]
    blocks: tuple[RawBlock, ...] = ()
    regions: tuple[RawRegion, ...] = ()
    tables: tuple[RawTable, ...] = ()
    formulas: tuple[RawFormula, ...] = ()
    chart_recognized: bool = False


# ---------------------------------------------------------------- 取值工具


def _plain(value: object) -> object:
    """numpy 数组与标量转成 Python 列表与数值，其他值原样返回。

    np.float32 不是 float 的子类，列表里的 numpy 标量（例如 layout_det_res 的 coordinate）也要逐个转换。
    """
    tolist = getattr(value, "tolist", None)
    return tolist() if callable(tolist) else value


def _field(obj: object, key: str) -> object:
    if isinstance(obj, Mapping):
        return cast(Mapping[str, object], obj).get(key)
    return getattr(obj, key, None)


def _list(value: object) -> list[object]:
    plain = _plain(value)
    if isinstance(plain, list | tuple):
        return list(cast(Sequence[object], plain))
    return []


def _float(value: object, default: float = 0.0) -> float:
    plain = _plain(value)
    if isinstance(plain, int | float) and not isinstance(plain, bool):
        return float(plain)
    return default


def _int(value: object) -> int | None:
    plain = _plain(value)
    if isinstance(plain, int) and not isinstance(plain, bool):
        return plain
    if isinstance(plain, float) and plain.is_integer():
        return int(plain)
    return None


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _box(value: object) -> Box | None:
    """[x0, y0, x1, y1]、四个角点的扁平列表或 4×2 的角点数组都转成包围盒；格式不对时返回 None。"""
    items = [_plain(item) for item in _list(value)]
    if items and all(isinstance(item, list | tuple) for item in items):
        items = [_plain(coord) for point in items for coord in cast(Sequence[object], point)]
    if len(items) not in (4, 8) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) for v in items
    ):
        return None
    return box_from(cast(list[float], items))


def _lines(ocr: object) -> tuple[RawLine, ...]:
    boxes = _list(_field(ocr, "rec_boxes"))
    texts = _list(_field(ocr, "rec_texts"))
    scores = _list(_field(ocr, "rec_scores"))
    labels = _list(_field(ocr, "rec_labels"))
    out: list[RawLine] = []
    for index, (raw_box, text) in enumerate(zip(boxes, texts, strict=False)):
        box = _box(raw_box)
        if box is None:
            continue
        score = _float(scores[index]) if index < len(scores) else 0.0
        label = labels[index] if index < len(labels) else "text"
        out.append(RawLine(box, _str(text), score, label == "text"))
    return tuple(out)


def _preprocess_angle(result: object) -> int | None:
    return _int(_field(_field(result, "doc_preprocessor_res"), "angle"))


def _output_size(result: object) -> tuple[int, int]:
    width, height = _int(_field(result, "width")), _int(_field(result, "height"))
    if width is not None and height is not None:
        return (width, height)
    shape = _list(getattr(_field(_field(result, "doc_preprocessor_res"), "output_img"), "shape", None))
    if len(shape) >= 2 and isinstance(shape[0], int) and isinstance(shape[1], int):
        return (shape[1], shape[0])
    raise ValueError("产线结果里没有图像尺寸")


# ---------------------------------------------------------------- 两种结果


def extract_structure(result: object) -> RawPage:
    """取出 PP-StructureV3 一张图的结果。"""
    blocks: list[RawBlock] = []
    for item in _list(_field(result, "parsing_res_list")):
        box = _box(_field(item, "bbox"))
        if box is not None:
            blocks.append(RawBlock(_str(_field(item, "label")), box, _str(_field(item, "content"))))
    regions: list[RawRegion] = []
    for item in _list(_field(_field(result, "layout_det_res"), "boxes")):
        box = _box(_field(item, "coordinate"))
        if box is not None:
            regions.append(RawRegion(_str(_field(item, "label")), box, _float(_field(item, "score"))))
    tables: list[RawTable] = []
    for item in _list(_field(result, "table_res_list")):
        cells = tuple(
            b for b in (_box(cell) for cell in _list(_field(item, "cell_box_list"))) if b is not None
        )
        tables.append(RawTable(_str(_field(item, "pred_html")), cells))
    formulas: list[RawFormula] = []
    for item in _list(_field(result, "formula_res_list")):
        box = _box(_field(item, "dt_polys"))
        if box is not None:
            formulas.append(RawFormula(box, _str(_field(item, "rec_formula"))))
    settings = _field(result, "model_settings")
    return RawPage(
        output_size=_output_size(result),
        angle=_preprocess_angle(result),
        lines=_lines(_field(result, "overall_ocr_res")),
        blocks=tuple(blocks),
        regions=tuple(regions),
        tables=tuple(tables),
        formulas=tuple(formulas),
        chart_recognized=_field(settings, "use_chart_recognition") is True,
    )


def extract_text(result: object, input_size: tuple[int, int]) -> RawPage:
    """取出 OCR 产线（文本模式）一张图的结果。

    没有文档预处理结果（没开方向校正和去畸变）时，输出坐标就在输入图像里，尺寸取 input_size。
    """
    preprocessed = _field(result, "doc_preprocessor_res")
    shape = _list(getattr(_field(preprocessed, "output_img"), "shape", None))
    if len(shape) >= 2 and isinstance(shape[0], int) and isinstance(shape[1], int):
        size = (shape[1], shape[0])
    else:
        size = input_size
    return RawPage(output_size=size, angle=_preprocess_angle(result), lines=_lines(result))
