"""更新服务用到的网络工具：请求头、超时、错误分类和日志脱敏。

GitHub 的附件下载地址会重定向到带签名查询参数的地址。脱敏与分类的实现在 lst.services._shared.net，
这里只补上更新服务自己的请求头、超时和错误码。
"""

from typing import Final

import httpx

from lst.contracts.updates import UpdateErrorCode
from lst.services._shared import net as _net
from lst.services._shared.net import DOWNLOAD_TIMEOUT, describe_error, safe_url, scrub

__all__ = [
    "DOWNLOAD_TIMEOUT",
    "GITHUB_ACCEPT",
    "RELEASES_TIMEOUT",
    "classify_transport_error",
    "describe_error",
    "safe_url",
    "scrub",
    "user_agent",
]

RELEASES_TIMEOUT: Final = httpx.Timeout(20.0, connect=10.0)
"""获取版本列表与校验文件的超时。两者都只有几 KB 到几百 KB。"""

GITHUB_ACCEPT: Final = "application/vnd.github+json"


def user_agent(version: str) -> str:
    """GitHub API 要求每个请求带 User-Agent。"""
    return f"LSTtranslate/{version or 'unknown'}"


def classify_transport_error(exc: Exception) -> UpdateErrorCode:
    """httpx 异常的分类：超时归为 TIMEOUT，其余传输错误归为 NETWORK。"""
    return _net.classify_transport_error(
        exc, timeout=UpdateErrorCode.TIMEOUT, network=UpdateErrorCode.NETWORK
    )
