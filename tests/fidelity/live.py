"""真实 OCR 子进程 → OcrModule → 原生覆盖窗口，固定译文用于排版验收。"""

import asyncio
import json
import logging
import sys
from dataclasses import asdict
from typing import cast

import qasync
from PySide6.QtWidgets import QApplication

from lst.app.supervisor import WorkerSupervisor
from lst.contracts.config import AppConfig
from lst.contracts.ids import SessionId
from lst.contracts.ocr import CaptureCompleted, CaptureGeometry, OcrCompleted, OcrFailed, RawImage
from lst.contracts.ports import WorkerState
from lst.kernel import EventBus, ModuleContext
from lst.services.ocr.module import OcrModule
from tests.fidelity.evaluate import evaluate, expected
from tests.fidelity.pipeline import ENGINES, IMAGES, OUTPUT, ROOT, Recognized, bgra, english_path, load_image
from tests.fidelity.render import render_result


async def run(name: str) -> None:
    engine = ENGINES[name]
    bus = EventBus(asyncio.get_running_loop(), raise_errors=True)
    bus.publish(
        AppConfig(
            {
                "ocr": engine.config.model_dump(mode="json"),
                f"ocr_{engine.config.engine.value}": engine.pipeline.model_dump(mode="json"),
            },
            1,
        )
    )
    channel = WorkerSupervisor(
        "ocr",
        (str(engine.python), "-m", "lst.workers.ocr_worker", "--backend", engine.backend),
        settings=engine.settings(),
        cwd=ROOT,
    )
    module = OcrModule(channel)
    ctx = ModuleContext(bus, logging.getLogger("fidelity.live"))
    rows: dict[str, object] = {}
    try:
        await channel.start()
        await asyncio.wait_for(channel.wait_for_state(WorkerState.READY), 300)
        await module.start(ctx)
        for image in IMAGES:
            future: asyncio.Future[OcrCompleted] = asyncio.get_running_loop().create_future()

            def completed(event: OcrCompleted, pending: asyncio.Future[OcrCompleted] = future) -> None:
                if not pending.done():
                    pending.set_result(event)

            def failed(event: OcrFailed, pending: asyncio.Future[OcrCompleted] = future) -> None:
                if not pending.done():
                    pending.set_exception(AssertionError(event.error))

            subscriptions = [bus.subscribe(OcrCompleted, completed), bus.subscribe(OcrFailed, failed)]
            try:
                picture = load_image(english_path(image))
                data, stride = bgra(picture)
                frame = RawImage(picture.width(), picture.height(), stride, data)
                rect = (0, 0, frame.width, frame.height)
                bus.publish(
                    CaptureCompleted(
                        SessionId(f"live-{image}"), frame, CaptureGeometry("fidelity", rect, rect, 1.0)
                    )
                )
                event = await asyncio.wait_for(future, 120)
                result = Recognized(
                    name + "-live", image, frame.width, frame.height, [], event.blocks, event.segments, 0
                )
                metrics = evaluate(result, expected(image))
                rows[image] = asdict(metrics)
                assert metrics.content_accuracy >= 0.99, metrics
                assert metrics.unit_recall == 1.0, metrics
                assert metrics.merged_segments == metrics.split_units == 0, metrics
                path = render_result(result)
                print(
                    f"{name} {image}: {metrics.content_accuracy:.2%}, {metrics.separate_units:.1%}, {path}",
                    flush=True,
                )
            finally:
                for subscription in subscriptions:
                    subscription.cancel()
        (OUTPUT / f"{name}-live.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")
    finally:
        await module.stop()
        ctx.close()
        await channel.stop()
        await bus.aclose()


if __name__ == "__main__":
    _app = QApplication(["fidelity-live"])
    _loop = qasync.QEventLoop(_app)
    asyncio.set_event_loop(_loop)
    with _loop:
        cast(asyncio.AbstractEventLoop, _loop).run_until_complete(
            run(sys.argv[1] if len(sys.argv) > 1 else "rapid")
        )
