"""OCR 后端协议。

OCR 进程把“怎么识别”交给后端，处理器（handler.py）只负责解包像素、把后端接到运行时上。默认后端是
PaddleOCR（paddle_backend.py），另有 Windows 自带 OCR 的后端（winocr_backend.py）；
测试通过 `--backend module:attr` 换成假后端，`attr` 是一个无参可调用对象，调用它得到后端实例。

后端要实现两个方法，都只在推理线程上调用：

- `load(settings, progress) -> LoadResult`：按 Configure 的设置加载（运行中再次收到 Configure 时重新加载）。
  设置无效抛 ConfigError；推理依赖导入失败抛 ImportError 或 OSError。
- `recognize(image, options) -> list[JsonValue]`：识别一张 BGR 图像（height × width × 3 的 uint8 数组，
  已去掉行尾填充），返回 OcrResult.blocks（格式见 blocks.py）。options 是请求的原样选项，无效时抛
  settings.BadOptions；请求用到的子能力缺模型时抛 ModelMissing；推理中的其他异常由处理器记为 engine.internal。
"""

from collections.abc import Mapping
from typing import Protocol

from lst.contracts.worker_protocol import JsonValue
from lst.workers._runtime import ConfigError, LoadResult, ProgressReporter
from lst.workers.ocr_worker.image import Pixels


class ModelMissing(ConfigError):
    """模型目录里缺少要用的模型。

    加载时抛出按配置无效处理（进程以 CONFIG_ERROR 退出）；请求补加载子能力时抛出，处理器回复
    engine.model_missing，进程照常处理之后的请求。
    """


class OcrBackend(Protocol):
    """OCR 后端，见模块说明。"""

    def load(self, settings: Mapping[str, JsonValue], progress: ProgressReporter) -> LoadResult: ...

    def recognize(self, image: Pixels, options: Mapping[str, JsonValue]) -> list[JsonValue]: ...
