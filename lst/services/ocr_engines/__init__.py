"""OCR 引擎的安装管理（需求 OCRE-01 至 OCRE-06，lst/contracts/ocr_engines.py）。

- layout：PaddleOCR 引擎的目录约定与模型清单；
- archive：模型包的安全解压与磁盘操作；
- runtime：用 uv 安装运行环境的命令行、环境变量与外部命令运行器；
- windows：Windows 自带 OCR 的语言读取；
- net：超时、错误分类与日志脱敏；
- module：OcrEnginesModule，实现 kernel 的 Module 协议。
"""

from lst.services.ocr_engines.layout import (
    DEFAULT_MODEL_BASE_URL,
    DEFAULT_PADDLE_MODELS,
    FORMULA_MODEL,
    DevPaddleEnvironment,
    PaddleInstallPaths,
    PaddleModel,
    model_url,
)
from lst.services.ocr_engines.module import OcrEnginesModule
from lst.services.ocr_engines.runtime import CommandResult, CommandRunner, run_command
from lst.services.ocr_engines.windows import probe_windows_languages

__all__ = [
    "DEFAULT_MODEL_BASE_URL",
    "DEFAULT_PADDLE_MODELS",
    "FORMULA_MODEL",
    "CommandResult",
    "CommandRunner",
    "DevPaddleEnvironment",
    "OcrEnginesModule",
    "PaddleInstallPaths",
    "PaddleModel",
    "model_url",
    "probe_windows_languages",
    "run_command",
]
