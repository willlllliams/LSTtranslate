"""按人工标注测量内容与分段，避免把网页视口变化计入 OCR 误差。"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from pydantic import BaseModel

from lst.contracts.ocr import BBox
from lst.services.ocr.normalize import area, iou
from tests.fidelity.pipeline import ENGINES, IMAGES, OUTPUT, Recognized, expected_path, load_raw


class Unit(BaseModel):
    id: str
    bbox: tuple[int, int, int, int]
    text: str
    zh: str
    role: str

    @property
    def box(self) -> BBox:
        return BBox(*self.bbox)


class Expected(BaseModel):
    width: int
    height: int
    units: tuple[Unit, ...]


def expected(image: str) -> Expected:
    return Expected.model_validate_json(expected_path(image).read_text("utf-8"))


def canonical(text: str) -> str:
    """比较字母与数字，忽略大小写、空格、标点和评分星形。"""
    return "".join(c for c in text.casefold() if c.isalnum())


def substring_distance(reference: str, text: str) -> int:
    """reference 与 text 任意连续子串的最小编辑距离；前后其他单元的字不计内容错误。

    分段粘连由独立指标计分。这里允许自由跳过候选串的前后缀，用于比较正文、菜单等文字是否识别完整。
    """
    previous = [0] * (len(text) + 1)
    for i, char in enumerate(reference, 1):
        current = [i]
        for j, other in enumerate(text, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (char != other)))
        previous = current
    return min(previous)


def overlaps(a: BBox, b: BBox) -> bool:
    width = max(0, min(a.x1, b.x1) - max(a.x0, b.x0))
    height = max(0, min(a.y1, b.y1) - max(a.y0, b.y0))
    return width * height >= 0.5 * min(area(a), area(b)) and width > 0 and height > 0


def associations(result: Recognized, target: Expected) -> list[list[int]]:
    """每个输出段关联的标注单元下标：交集至少覆盖较小框的一半。"""
    return [
        [i for i, unit in enumerate(target.units) if overlaps(segment.bbox, unit.box)]
        for segment in result.segments
    ]


@dataclass(frozen=True)
class Metrics:
    units: int
    segments: int
    content_accuracy: float
    unit_recall: float
    separate_units: float
    merged_segments: int
    split_units: int
    unexpected_segments: int
    mean_iou: float
    errors: dict[str, int]


def evaluate(result: Recognized, target: Expected) -> Metrics:
    assert (result.width, result.height) == (target.width, target.height)
    graph = associations(result, target)
    reverse = [[j for j, units in enumerate(graph) if i in units] for i in range(len(target.units))]
    errors: dict[str, int] = {}
    characters = mistakes = recalled = separate = 0
    positions: list[float] = []
    for i, unit in enumerate(target.units):
        reference = canonical(unit.text)
        candidates = sorted(
            reverse[i], key=lambda j: (result.segments[j].bbox.y0, result.segments[j].bbox.x0)
        )
        text = canonical(" ".join(result.segments[j].segment.text for j in candidates))
        distance = substring_distance(reference, text)
        errors[unit.id] = distance
        characters += len(reference)
        mistakes += distance
        present = distance <= 0.15 * len(reference)
        recalled += present
        if len(candidates) == 1 and len(graph[candidates[0]]) == 1:
            overlap = iou(result.segments[candidates[0]].bbox, unit.box)
            positions.append(overlap)
            separate += present and overlap >= 0.5
    count = len(target.units)
    return Metrics(
        units=count,
        segments=len(result.segments),
        content_accuracy=1 - mistakes / max(1, characters),
        unit_recall=recalled / max(1, count),
        separate_units=separate / max(1, count),
        merged_segments=sum(len(units) > 1 for units in graph),
        split_units=sum(len(segments) > 1 for segments in reverse),
        unexpected_segments=sum(not units for units in graph),
        mean_iou=sum(positions) / max(1, len(positions)),
        errors=errors,
    )


def main(directory: Path | None = None) -> None:
    rows: dict[str, object] = {}
    for engine in ENGINES.values():
        for name in IMAGES:
            result = load_raw(engine, name) if directory is None else load_raw(engine, name, directory)
            metrics = evaluate(result, expected(name))
            key = f"{engine.name}-{name}"
            rows[key] = asdict(metrics)
            print(
                f"{key:25} text={metrics.content_accuracy:.1%} recall={metrics.unit_recall:.1%} "
                f"separate={metrics.separate_units:.1%} merge={metrics.merged_segments} "
                f"split={metrics.split_units} extra={metrics.unexpected_segments} iou={metrics.mean_iou:.3f}"
            )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "metrics.json").write_text(
        json.dumps(cast(object, rows), ensure_ascii=False, indent=2), "utf-8"
    )


if __name__ == "__main__":
    main()
