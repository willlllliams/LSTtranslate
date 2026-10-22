"""用人工已知的正文基线分离检测漏字与生产后处理的顺序误差。"""

from dataclasses import replace

from lst.contracts.ocr import TextLine
from lst.services.ocr.blocks import decode_blocks
from tests.fidelity.evaluate import canonical, evaluate, expected, overlaps, substring_distance
from tests.fidelity.pipeline import ENGINES, FIXTURES, load_raw

SERVER_RAW = FIXTURES / "server"
PRESETS = (
    "paddle-default",
    "paddle-rec-server",
    "paddle-det-server",
    "paddle-server",
    "paddle-server-tight",
    "paddle-server-max1280",
    "paddle-server-max960",
)
# 博客原始 PNG 上人工核对的五条正文行中心，仅用于诊断，不进入生产分段。
BODY_CENTERS = (519, 553, 587, 621, 655)


def _position(line: TextLine) -> tuple[int, int]:
    box = line.bbox
    center = (box.y0 + box.y1) / 2
    row = min((abs(reference - center), index) for index, reference in enumerate(BODY_CENTERS))[1]
    return row, box.x0


def main() -> None:
    page = expected("blog")
    body = next(unit for unit in page.units if unit.id == "body")
    for name in PRESETS:
        result = load_raw(replace(ENGINES["paddle-text"], name=name), "blog", SERVER_RAW)
        # result.blocks 已经过归一化；原始行从跨进程 JSON 重新解码。
        blocks = decode_blocks(result.raw, width=result.width, height=result.height)
        lines = [line for block in blocks for line in block.lines if overlaps(line.bbox, body.box)]
        reconstructed = " ".join(line.text for line in sorted(lines, key=_position))
        raw_error = substring_distance(canonical(body.text), canonical(reconstructed))
        metrics = evaluate(result, page)
        print(
            f"{name:23} boxes={len(lines):2} raw-body-edit={raw_error:3} "
            f"production-body-edit={metrics.errors['body']:3} text={metrics.content_accuracy:.2%}"
        )


if __name__ == "__main__":
    main()
