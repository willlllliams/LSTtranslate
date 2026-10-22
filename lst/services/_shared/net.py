"""下载类模块共用的网络工具：超时、传输错误分类和日志脱敏（需求 PRV-01，架构文档 10.2 节）。

下载地址可能带签名之类的查询参数，镜像地址可能带 ``user:password@`` 形式的凭据。日志和错误详情里的
地址一律去掉用户信息、查询参数和片段。错误码由各业务模块自己定义，分类函数按参数返回调用方的错误码。
"""

import re
from typing import Final
from urllib.parse import urlsplit, urlunsplit

import httpx

DOWNLOAD_TIMEOUT: Final = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)
"""下载大文件的超时。没有总时长限制；连续 60 秒收不到数据才算超时。"""

DETAIL_LIMIT: Final = 200
"""错误详情的默认长度上限。"""

_SCHEME = r"[a-z][a-z0-9+.-]*://"
_USERINFO = re.compile(rf"(?i)\b({_SCHEME})[^\s/?#'\"<>]*@")
"""地址里 ``scheme://`` 之后、第一个斜杠之前的 ``用户信息@``。贪婪匹配到最后一个 @，密码里的 @ 也一起去掉。"""
_URL_TAIL = re.compile(rf"(?i)\b({_SCHEME}[^\s?#'\"<>]*)[?#][^\s'\"<>]*")


def safe_url(url: str) -> str:
    """去掉用户信息、查询参数和片段后的地址，用于写日志。无法解析时按文字规则脱敏。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port is not None:
            host = f"{host}:{parts.port}"
    except ValueError:
        return _USERINFO.sub(r"\1", url).split("?", 1)[0].split("#", 1)[0]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def scrub(text: str, limit: int = DETAIL_LIMIT) -> str:
    """去掉一段文字里所有地址的用户信息、查询参数和片段，超过 limit 个字符时截断。

    用于异常信息与外部命令的输出。
    """
    cleaned = _URL_TAIL.sub(r"\1", _USERINFO.sub(r"\1", text))
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


def describe_error(exc: BaseException, limit: int = DETAIL_LIMIT) -> str:
    """异常的技术细节：类名加脱敏后的信息。"""
    message = scrub(str(exc), limit)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def classify_transport_error[C](exc: Exception, *, timeout: C, network: C) -> C:
    """httpx 异常的分类：超时返回 timeout，其余传输错误返回 network。"""
    if isinstance(exc, httpx.TimeoutException):
        return timeout
    return network
