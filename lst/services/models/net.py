"""模型下载用到的网络工具：超时、错误分类和日志脱敏（需求 PRV-01，架构文档 10.2 节）。

下载地址可能带签名之类的查询参数，所以日志和错误详情里的地址一律去掉查询参数、片段和用户信息。
"""

import re
from typing import Final
from urllib.parse import urlsplit, urlunsplit

import httpx

from lst.contracts.models import ModelErrorCode

INDEX_TIMEOUT: Final = httpx.Timeout(20.0, connect=10.0)
"""获取模型索引的超时：索引只有几十 KB。"""

DOWNLOAD_TIMEOUT: Final = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)
"""下载模型包的超时。没有总时长限制，模型包有几十到几百 MB；连续 60 秒收不到数据才算超时。"""

_URL_TAIL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s?#'\"<>]*)[?#][^\s'\"<>]*")
_DETAIL_LIMIT: Final = 200


def safe_url(url: str) -> str:
    """去掉查询参数、片段和用户信息后的地址，用于写日志。无法解析时只保留问号之前的部分。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port is not None:
            host = f"{host}:{parts.port}"
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def scrub(text: str) -> str:
    """去掉一段文字里所有地址的查询参数和片段，并截断过长的文字。用于异常信息。"""
    cleaned = _URL_TAIL.sub(r"\1", text)
    return cleaned if len(cleaned) <= _DETAIL_LIMIT else cleaned[:_DETAIL_LIMIT] + "…"


def classify_transport_error(exc: Exception) -> ModelErrorCode:
    """httpx 异常的分类：超时归为 TIMEOUT，其余传输错误归为 NETWORK。"""
    if isinstance(exc, httpx.TimeoutException):
        return ModelErrorCode.TIMEOUT
    return ModelErrorCode.NETWORK


def describe_error(exc: BaseException) -> str:
    """异常的技术细节：类名加脱敏后的信息。"""
    message = scrub(str(exc))
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
