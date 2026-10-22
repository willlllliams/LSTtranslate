"""运行环境版本的推荐（需求 OCRE-07）。显卡检测在 lst.services._shared.gpu。

推荐规则（OcrEngineInfo.recommended_variant）：有 NVIDIA 显卡（VendorId 0x10DE）且引擎有 CUDA 版时推荐
CUDA；否则有支持 DirectX 12 的显卡且引擎有 DirectML 版时推荐 DirectML；否则 CPU。检测只说明硬件存在，
驱动是否支持 CUDA 12.9（PaddleOCR）或 CUDA 13（RapidOCR）要到 OCR 进程加载时才知道，不支持时进程以依赖
错误退出，界面显示原因。
"""

from collections.abc import Sequence

from lst.contracts.ocr_engines import OcrRuntimeVariant
from lst.services._shared.gpu import GpuAdapter


def recommend_variant(
    adapters: Sequence[GpuAdapter], variants: Sequence[OcrRuntimeVariant]
) -> OcrRuntimeVariant | None:
    """按推荐规则（见模块说明）从 variants 里选一个；variants 为空时返回 None。"""
    if not variants:
        return None
    if OcrRuntimeVariant.CUDA in variants and any(adapter.nvidia for adapter in adapters):
        return OcrRuntimeVariant.CUDA
    if OcrRuntimeVariant.DIRECTML in variants and any(adapter.d3d12 for adapter in adapters):
        return OcrRuntimeVariant.DIRECTML
    if OcrRuntimeVariant.CPU in variants:
        return OcrRuntimeVariant.CPU
    return variants[0]
