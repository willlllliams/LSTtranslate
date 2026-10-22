"""模型下载用到的网络工具：超时、错误分类和日志脱敏（需求 PRV-01，架构文档 10.2 节）。

脱敏与分类的实现在 lst.services._shared.net，这里只补上模型模块自己的超时和错误码。
"""

from typing import Final

import httpx

from lst.contracts.models import ModelErrorCode
from lst.services._shared import net as _net
from lst.services._shared.net import DOWNLOAD_TIMEOUT, describe_error, safe_url, scrub

__all__ = [
    "DOWNLOAD_TIMEOUT",
    "INDEX_TIMEOUT",
    "classify_transport_error",
    "describe_error",
    "safe_url",
    "scrub",
]

INDEX_TIMEOUT: Final = httpx.Timeout(20.0, connect=10.0)
"""获取模型索引的超时：索引只有几十 KB。"""


def classify_transport_error(exc: Exception) -> ModelErrorCode:
    """httpx 异常的分类：超时归为 TIMEOUT，其余传输错误归为 NETWORK。"""
    return _net.classify_transport_error(exc, timeout=ModelErrorCode.TIMEOUT, network=ModelErrorCode.NETWORK)
