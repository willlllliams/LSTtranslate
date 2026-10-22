"""OCR 引擎下载用到的网络工具：超时、错误分类和日志脱敏。

脱敏与分类的实现在 lst.services._shared.net。这里的错误详情上限放宽到 300 字符：
uv 的失败输出要保留几行才看得出原因。
"""

from typing import Final

from lst.contracts.ocr_engines import OcrEngineErrorCode
from lst.services._shared import net as _net
from lst.services._shared.net import DOWNLOAD_TIMEOUT, safe_url

__all__ = ["DOWNLOAD_TIMEOUT", "classify_transport_error", "describe_error", "safe_url", "scrub"]

_DETAIL_LIMIT: Final = 300


def scrub(text: str, limit: int = _DETAIL_LIMIT) -> str:
    """去掉一段文字里所有地址的用户信息、查询参数和片段，并截断过长的文字。用于异常信息与 uv 的输出。"""
    return _net.scrub(text, limit)


def describe_error(exc: BaseException) -> str:
    """异常的技术细节：类名加脱敏后的信息。"""
    return _net.describe_error(exc, _DETAIL_LIMIT)


def classify_transport_error(exc: Exception) -> OcrEngineErrorCode:
    """httpx 异常的分类：超时归为 TIMEOUT，其余传输错误归为 NETWORK。"""
    return _net.classify_transport_error(
        exc, timeout=OcrEngineErrorCode.TIMEOUT, network=OcrEngineErrorCode.NETWORK
    )
