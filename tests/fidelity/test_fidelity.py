"""真实识别输出的离线回归：人工标注约束内容、控件粘连和段落拆分。"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from tests.fidelity.evaluate import canonical, evaluate, expected, substring_distance
from tests.fidelity.pipeline import ENGINES, FIXTURES, IMAGES, load_raw


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
    if engine == "windows":
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


def test_server_scaled_blog_preserves_text_and_control_boundaries() -> None:
    engine = replace(ENGINES["paddle-text"], name="paddle-server-max960")
    metrics = evaluate(load_raw(engine, "blog", FIXTURES / "server"), expected("blog"))
    assert metrics.content_accuracy == metrics.unit_recall == metrics.separate_units == 1.0
    assert metrics.merged_segments == metrics.split_units == 0
