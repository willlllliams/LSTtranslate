"""真实识别输出的离线回归：人工标注约束内容、控件粘连和段落拆分。"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from lst.contracts.ids import SegmentId
from lst.contracts.ocr import BBox, OcrSegment
from lst.contracts.translation import Segment
from tests.fidelity.evaluate import Expected, Unit, canonical, evaluate, expected, substring_distance
from tests.fidelity.pipeline import ENGINES, FIXTURES, IMAGES, Recognized, load_raw


def test_original_attachments_are_preserved_byte_for_byte() -> None:
    manifest = cast(
        dict[str, dict[str, str | int]], json.loads((FIXTURES / "sources.json").read_text("utf-8"))
    )
    for entry in manifest.values():
        path = FIXTURES / str(entry["file"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]


@pytest.mark.parametrize("image", IMAGES)
def test_annotations_have_unique_units_inside_the_source_image(image: str) -> None:
    page = expected(image)
    assert len({unit.id for unit in page.units}) == len(page.units)
    for unit in page.units:
        assert canonical(unit.text) and unit.zh
        assert 0 <= unit.box.x0 < unit.box.x1 <= page.width
        assert 0 <= unit.box.y0 < unit.box.y1 <= page.height


@pytest.mark.parametrize("engine", list(ENGINES))
@pytest.mark.parametrize("image", IMAGES)
def test_real_outputs_preserve_text_and_separate_controls(engine: str, image: str) -> None:
    result = load_raw(ENGINES[engine], image, FIXTURES / "raw")
    metrics = evaluate(result, expected(image))
    assert metrics.content_accuracy >= 0.99, metrics
    assert metrics.merged_segments == 0, metrics
    assert metrics.split_units == 0, metrics
    if engine in {"windows", "windows-auto"}:
        assert metrics.unit_recall >= 0.94, metrics
        assert metrics.separate_units >= (0.84 if image == "addons" else 0.94), metrics
    else:
        assert metrics.unit_recall == 1.0, metrics
        assert metrics.separate_units >= (0.97 if image == "addons" else 1.0), metrics


def test_content_scoring_does_not_hide_missing_middle_words() -> None:
    assert substring_distance("abcdef", "prefixabcdefsuf") == 0
    assert substring_distance("abcdef", "abdef") == 1
    assert substring_distance("abcdef", "abcZZdef") == 2
    assert substring_distance("abcdef", "") == 6


def test_cached_outputs_reject_a_different_source_image(tmp_path: Path) -> None:
    path = FIXTURES / "raw" / "rapid-blog.json"
    data = json.loads(path.read_text("utf-8"))
    data["source_sha256"] = "outdated-image"
    (tmp_path / path.name).write_text(json.dumps(data), "utf-8")
    with pytest.raises(ValueError, match="与当前原图不一致"):
        load_raw(ENGINES["rapid"], "blog", tmp_path)


@pytest.mark.parametrize("image", IMAGES)
def test_server_scaled_pages_preserve_text_and_control_boundaries(image: str) -> None:
    engine = replace(ENGINES["paddle-text"], name="paddle-server-max960")
    metrics = evaluate(load_raw(engine, image, FIXTURES / "server"), expected(image))
    assert metrics.content_accuracy == metrics.unit_recall == metrics.separate_units == 1.0
    assert metrics.merged_segments == metrics.split_units == 0


@pytest.mark.parametrize(
    "preset,body_loss",
    [
        ("paddle-det-server", 19),
        ("paddle-server", 18),
        ("paddle-server-tight", 18),
        ("paddle-server-max1280", 0),
    ],
)
def test_server_word_boxes_preserve_body_reading_order(preset: str, body_loss: int) -> None:
    engine = replace(ENGINES["paddle-text"], name=preset)
    metrics = evaluate(load_raw(engine, "blog", FIXTURES / "server"), expected("blog"))
    # 18/19 是原始检测、识别输出的漏字；正文顺序不能再增加损失。
    assert metrics.errors["body"] == body_loss


@pytest.mark.parametrize("reverse", [False, True])
def test_content_scoring_uses_production_order_and_detects_reversed_words(reverse: bool) -> None:
    target = Expected(
        width=200,
        height=100,
        units=(Unit(id="body", bbox=(0, 0, 200, 100), text="first second", zh="第一第二", role="body"),),
    )
    # 右侧词框上边缘高两像素；生产顺序仍从左向右。反转生产顺序必须扣分。
    segments = (
        OcrSegment(Segment(SegmentId("a"), "first"), "body", BBox(10, 22, 80, 42)),
        OcrSegment(Segment(SegmentId("b"), "second"), "body", BBox(85, 20, 170, 42)),
    )
    result = Recognized("test", "test", 200, 100, [], (), segments[::-1] if reverse else segments, 0)
    metrics = evaluate(result, target)
    assert (metrics.errors["body"] > 0) is reverse
    assert metrics.split_units == 1
