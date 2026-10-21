"""OCR 进程测试用的假后端，通过 `--backend tests.workers.ocr.fake_backend:FakeOcrBackend` 装入子进程。

不依赖 Paddle，在主环境里运行，用来测试处理器与运行时：像素帧解包、错误码映射、网络拦截。

Configure 的设置：

| 键 | 含义 |
|---|---|
| load_error | 加载时出错："import"、"oserror"（模拟 DLL 加载失败）、"config"、"crash" |
| 其余 | 按 OCR 进程的 OcrSettings 校验（mode 等），capabilities 里回显 mode |

请求 options：

| 键 | 含义 |
|---|---|
| raise | 为真时推理抛出 RuntimeError |
| missing | 为真时抛出 ModelMissing（模拟补加载子能力时缺模型） |
| network | 为真时尝试解析 example.com，应被网络拦截挡住 |
| 其余 | 按 RequestOptions 校验，类型不对时抛 BadOptions |

识别结果是一个覆盖整张图的 text 块，text 写出图像尺寸和左上、右下两个像素的 BGR 值，
测试据此检查 BGRA 帧（含行尾填充）的解包。
"""

import socket
from collections.abc import Mapping

from lst.contracts.worker_protocol import JsonValue
from lst.workers._runtime import ConfigError, LoadResult, ProgressReporter
from lst.workers.ocr_worker.backend import ModelMissing
from lst.workers.ocr_worker.image import Pixels
from lst.workers.ocr_worker.settings import OcrSettings, RequestOptions


class FakeOcrBackend:
    """假后端，实现 lst.workers.ocr_worker.backend.OcrBackend。"""

    def __init__(self) -> None:
        self._settings: OcrSettings | None = None

    def load(self, settings: Mapping[str, JsonValue], progress: ProgressReporter) -> LoadResult:
        match settings.get("load_error"):
            case "import":
                raise ImportError("DLL load failed while importing paddle: 找不到指定的模块。")
            case "oserror":
                raise OSError("[WinError 126] 找不到指定的模块。")
            case "config":
                raise ConfigError("假后端：配置无效")
            case "crash":
                raise RuntimeError("假后端：加载时出现未预期的异常")
            case _:
                pass
        rest = {key: value for key, value in settings.items() if key != "load_error"}
        self._settings = OcrSettings.parse(rest)
        progress("model_load", "fake")
        return LoadResult({"mode": self._settings.mode, "fake": True}, {"fake": "1.0"})

    def recognize(self, image: Pixels, options: Mapping[str, JsonValue]) -> list[JsonValue]:
        assert self._settings is not None
        if options.get("raise") is True:
            raise RuntimeError("假后端：推理失败")
        if options.get("missing") is True:
            raise ModelMissing("模型目录中缺少：PP-FormulaNet_plus-L")
        if options.get("network") is True:
            socket.getaddrinfo("example.com", 443)
        rest = {key: value for key, value in options.items() if key not in ("raise", "missing", "network")}
        RequestOptions.parse(rest, self._settings)
        height, width = image.shape[:2]
        first = "/".join(str(int(v)) for v in image[0, 0])
        last = "/".join(str(int(v)) for v in image[height - 1, width - 1])
        text = f"{width}x{height} {first} {last} contiguous={image.flags['C_CONTIGUOUS']}"
        return [
            {
                "id": "0",
                "category": "text",
                "bbox": [0, 0, width, height],
                "order": 0,
                "score": 1.0,
                "text": text,
                "lines": [],
            }
        ]
