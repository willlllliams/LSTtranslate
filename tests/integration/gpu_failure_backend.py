"""集成测试用的 OCR 后端：推理设备为 gpu 时像 CPU 版 PaddlePaddle 一样在加载时抛 ImportError（退出码 65）。"""

from collections.abc import Mapping

from lst.contracts.worker_protocol import JsonValue
from lst.workers._runtime import LoadResult, ProgressReporter
from lst.workers.ocr_worker.image import Pixels


class CpuOnlyBackend:
    def load(self, settings: Mapping[str, JsonValue], progress: ProgressReporter) -> LoadResult:
        progress("import")
        if settings.get("device") == "gpu":
            raise ImportError("推理设备设为显卡，但 PaddleOCR 运行环境是 CPU 版")
        return LoadResult({"mode": str(settings.get("mode", "structure"))}, {})

    def recognize(self, image: Pixels, options: Mapping[str, JsonValue]) -> list[JsonValue]:
        del image, options
        return []
