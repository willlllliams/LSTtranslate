"""用固定人工译文渲染真实覆盖窗口，输出截图和字号、截断记录。"""

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from lst.contracts.ids import EngineId
from lst.contracts.ocr import CaptureGeometry, RawImage
from lst.contracts.translation import SegmentView, SkipReason
from lst.services.race.langid import has_text
from lst.ui.overlay.layout import OverlayLayoutConfig
from lst.ui.overlay.window import OverlayWindow, layout_ink_bounds
from tests.fidelity.evaluate import associations, expected
from tests.fidelity.pipeline import (
    ENGINES,
    IMAGES,
    OUTPUT,
    Recognized,
    bgra,
    english_path,
    load_image,
    load_raw,
)


class Actions:
    def retry(self) -> None:
        pass

    def copy(self, text: str) -> None:
        pass

    def window_closed(self, window: OverlayWindow) -> None:
        pass


def render(name: str, image: str, directory: Path = OUTPUT) -> Path:
    result = load_raw(ENGINES[name], image)
    return render_result(result, directory)


def render_result(
    result: Recognized, directory: Path = OUTPUT, *, layout_config: OverlayLayoutConfig | None = None
) -> Path:
    name, image = result.engine, result.image
    target = expected(image)
    picture = load_image(english_path(image))
    data, stride = bgra(picture)
    frame = RawImage(result.width, result.height, stride, data)
    rect = (0, 0, result.width, result.height)
    geometry = CaptureGeometry("fidelity", rect, rect, 1.0)
    now = [0.0]
    window = OverlayWindow(frame, geometry, Actions(), clock=lambda: now[0])
    if layout_config is not None:
        window.set_layout(layout_config)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    try:
        window.show_result(result.blocks, result.segments)
        views: list[SegmentView] = []
        for segment, indices in zip(result.segments, associations(result, target), strict=True):
            units = [target.units[i] for i in indices]
            text = " ".join(unit.zh for unit in units)
            if not units or all(unit.role == "rating" for unit in units):
                text = segment.segment.text
            skipped = SkipReason.NO_TEXT if not has_text(segment.segment.text) else None
            views.append(
                SegmentView(
                    segment.segment.id,
                    segment.segment.text,
                    text,
                    None if skipped else EngineId("reference"),
                    True,
                    (),
                    skipped,
                )
            )
        window.apply_views(views)
        now[0] = 1.0
        window.fit_all_now()
        window.show()
        QApplication.processEvents()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}-{image}-overlay.png"
        assert window.grab().save(str(path))
        records: list[dict[str, object]] = []
        for item in window.items.values():
            box = window._text_box(item)
            last = box.layout.lineAt(box.layout.lineCount() - 1) if box.layout.lineCount() else None
            ink = layout_ink_bounds(box.layout)
            inner = window._inner_device(item)
            records.append(
                {
                    "id": item.segment_id,
                    "font_px": window.font_px(item),
                    "original_font_px": item.source_px * window.scale_device(),
                    "font_ratio": window.font_px(item) / max(1, item.source_px * window.scale_device()),
                    "bbox": [item.bbox.x0, item.bbox.y0, item.bbox.x1, item.bbox.y1],
                    "text": window.shown_text(item),
                    "lines": box.layout.lineCount(),
                    "clipped": last is None
                    or last.textStart() + last.textLength() < len(window.shown_text(item))
                    or ink.height() > inner.height + 0.5
                    or any(
                        box.layout.lineAt(i).naturalTextWidth() > inner.width + 0.5
                        for i in range(box.layout.lineCount())
                    ),
                    "preserved_pixels": item.view is not None and item.view.skip_reason is SkipReason.NO_TEXT,
                }
            )
        path.with_suffix(".json").write_text(json.dumps(records, ensure_ascii=False, indent=2), "utf-8")
        return path
    finally:
        window.close()


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "offscreen")
    _app = QApplication(["fidelity-render"])
    for engine in sys.argv[1:] or list(ENGINES):
        for page in IMAGES:
            print(render(engine, page), flush=True)
