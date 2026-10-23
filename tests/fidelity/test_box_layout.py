"""固定参考译文在原生字体下保持原文估算字号，并完整落在扩大的绘制框内。"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from tests.fidelity.pipeline import ENGINES, FIXTURES, load_raw
from tests.fidelity.render import render_result


@pytest.mark.parametrize("image", ["addons", "blog", "github-dialog"])
def test_native_translation_boxes_keep_source_size_without_clipping(
    qapp: QApplication, tmp_path: Path, image: str
) -> None:
    if qapp.platformName() != "windows":
        pytest.skip("原生字体排版在 Windows 平台验收")
    result = load_raw(ENGINES["rapid"], image, FIXTURES / "raw")
    path = render_result(replace(result, engine="bbox"), tmp_path)
    records = json.loads(path.with_suffix(".json").read_text("utf-8"))
    translated = [record for record in records if not record["preserved_pixels"]]
    assert translated
    assert all(not record["clipped"] for record in translated), translated
    assert all(record["font_px"] >= round(record["original_font_px"]) for record in translated), translated
