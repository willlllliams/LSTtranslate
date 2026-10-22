"""在同一组人工标注上比较真实模型与检测参数，结果和原始输出写入 data/tmp/fidelity。"""

import json
import sys
from dataclasses import asdict, replace

from PySide6.QtGui import QGuiApplication

from lst.contracts.worker_protocol import JsonValue
from tests.fidelity.evaluate import evaluate, expected
from tests.fidelity.pipeline import ENGINES, IMAGES, OUTPUT, recognize, save_raw, start_worker

PRESETS: tuple[tuple[str, str, dict[str, JsonValue]], ...] = (
    ("rapid-default", "rapid", {}),
    ("rapid-tight", "rapid", {"det_unclip_ratio": 1.2, "det_use_dilation": False}),
    ("rapid-sharp", "rapid", {"det_thresh": 0.5, "det_unclip_ratio": 1.2, "det_use_dilation": False}),
    (
        "rapid-large",
        "rapid",
        {
            "det_limit_side_len": 1080,
            "max_side_len": 3000,
            "det_unclip_ratio": 1.4,
            "det_use_dilation": False,
        },
    ),
    ("paddle-default", "paddle-text", {}),
    ("paddle-rec-server", "paddle-text", {"recognition_model": "PP-OCRv5_server_rec"}),
    ("paddle-det-server", "paddle-text", {"detection_model": "PP-OCRv5_server_det"}),
    (
        "paddle-server-max960",
        "paddle-text",
        {
            "detection_model": "PP-OCRv5_server_det",
            "recognition_model": "PP-OCRv5_server_rec",
            "text_det_limit_side_len": 960,
            "text_det_limit_type": "max",
        },
    ),
    (
        "paddle-server-max1280",
        "paddle-text",
        {
            "detection_model": "PP-OCRv5_server_det",
            "recognition_model": "PP-OCRv5_server_rec",
            "text_det_limit_side_len": 1280,
            "text_det_limit_type": "max",
        },
    ),
    ("paddle-tight", "paddle-text", {"text_det_unclip_ratio": 1.2}),
    (
        "paddle-server",
        "paddle-text",
        {
            "detection_model": "PP-OCRv5_server_det",
            "recognition_model": "PP-OCRv5_server_rec",
        },
    ),
    (
        "paddle-server-tight",
        "paddle-text",
        {
            "detection_model": "PP-OCRv5_server_det",
            "recognition_model": "PP-OCRv5_server_rec",
            "text_det_unclip_ratio": 1.2,
        },
    ),
)


def main(families: list[str]) -> None:
    rows: dict[str, object] = {}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, base_name, updates in PRESETS:
        if families and not any(name.startswith(family) for family in families):
            continue
        base = ENGINES[base_name]
        missing = base.missing()
        if missing is not None:
            print(f"{name}: {missing}", flush=True)
            continue
        pipeline = type(base.pipeline).model_validate(
            {**base.pipeline.model_dump(), **updates, "cpu_threads": 4}
        )
        engine = replace(base, name=name, pipeline=pipeline)
        worker = start_worker(engine)
        try:
            for image in IMAGES:
                result = recognize(worker, engine, image)
                save_raw(result)
                metrics = evaluate(result, expected(image))
                rows[f"{name}-{image}"] = {
                    "settings": engine.settings(),
                    "options": engine.options(),
                    "seconds": result.seconds,
                    "metrics": asdict(metrics),
                }
                print(
                    f"{name:22} {image:6} text={metrics.content_accuracy:.2%} "
                    f"separate={metrics.separate_units:.1%} merge={metrics.merged_segments} "
                    f"split={metrics.split_units} extra={metrics.unexpected_segments} "
                    f"seconds={result.seconds:.2f}",
                    flush=True,
                )
                (OUTPUT / "tuning.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")
        finally:
            worker.close()


if __name__ == "__main__":
    _app = QGuiApplication(["fidelity-tuning"])
    main(sys.argv[1:])
