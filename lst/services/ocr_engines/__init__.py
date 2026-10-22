"""OCR 引擎的安装管理（需求 OCRE-01 至 OCRE-09，lst/contracts/ocr_engines.py）。

- layout：需要下载的引擎的目录约定、运行环境版本与版本清单的文件名、安装标记；
- catalog：各引擎各环节的可选模型（PaddleX 模型名、RapidOCR 的 onnx 模型）与默认组合；
- gpu：用 DXGI 检测显卡，给出推荐的运行环境版本；
- installer：通用流程（状态、版本、进度、uv 安装运行环境、取消、失败清理、卸载、安装标记、模型下载与删除）；
- paddle：PaddleOCR 引擎，模型是 PaddleX 模型包；
- rapid：RapidOCR 引擎，默认模型在 wheel 里，其余是单个 onnx 文件；
- archive：模型包的安全解压与磁盘操作；
- runtime：用 uv 安装运行环境的命令行、环境变量与外部命令运行器；
- windows：Windows 自带 OCR 的语言读取；
- net：超时、错误分类与日志脱敏；
- module：OcrEnginesModule，实现 kernel 的 Module 协议。
"""

from lst.services._shared.gpu import GpuAdapter, GpuProbe, probe_adapters
from lst.services.ocr_engines.catalog import (
    PADDLE_AUXILIARY,
    PADDLE_MODELS,
    PADDLE_STAGE_DEFAULTS,
    RAPID_MODELS,
    RAPID_STAGE_DEFAULTS,
    ModelSpec,
)
from lst.services.ocr_engines.gpu import recommend_variant
from lst.services.ocr_engines.layout import (
    DEFAULT_MODEL_BASE_URL,
    ENGINE_VARIANTS,
    DevPaddleEnvironment,
    DevRapidEnvironment,
    PaddleInstallPaths,
    RapidInstallPaths,
    RuntimeInstallPaths,
    model_url,
    requirements_name,
)
from lst.services.ocr_engines.module import OcrEnginesModule
from lst.services.ocr_engines.runtime import CommandResult, CommandRunner, run_command
from lst.services.ocr_engines.windows import probe_windows_languages

__all__ = [
    "DEFAULT_MODEL_BASE_URL",
    "ENGINE_VARIANTS",
    "PADDLE_AUXILIARY",
    "PADDLE_MODELS",
    "PADDLE_STAGE_DEFAULTS",
    "RAPID_MODELS",
    "RAPID_STAGE_DEFAULTS",
    "CommandResult",
    "CommandRunner",
    "DevPaddleEnvironment",
    "DevRapidEnvironment",
    "GpuAdapter",
    "GpuProbe",
    "ModelSpec",
    "OcrEnginesModule",
    "PaddleInstallPaths",
    "RapidInstallPaths",
    "RuntimeInstallPaths",
    "model_url",
    "probe_adapters",
    "probe_windows_languages",
    "recommend_variant",
    "requirements_name",
    "run_command",
]
