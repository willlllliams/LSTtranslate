"""OCR 进程内的网络拦截（需求 PRV-01，架构文档 10.1 节）。

模型已在本地时 OCR 进程不应发起任何网络请求。PaddleX 在模型目录缺少模型时会去 HuggingFace、AIStudio、
ModelScope、BOS 下载，启动时还会检查这些站点的连通性（PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK 关掉）。
后端为每个模型显式给出本地目录，正常情况下走不到下载；这里再用审计钩子（sys.addaudithook）兜底：
解析域名、建立连接、发起 urllib/http.client 请求时，目标不是本机回环地址就记一条 WARNING 并抛出
PermissionError，让这次访问失败。

钩子装上以后无法卸载，只在 OCR 进程入口调用 install()。与主进程的通信走命名管道，不经过套接字，不受影响。
"""

import logging
import sys
from typing import cast
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

_EVENTS = frozenset(
    {
        "socket.connect",
        "socket.sendto",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.gethostbyaddr",
        "http.client.connect",
        "urllib.Request",
    }
)
_LOOPBACK_NAMES = frozenset({"", "localhost", "::1", "0.0.0.0"})

_installed = False


def _host(event: str, args: tuple[object, ...]) -> str | None:
    """事件的目标主机名；目标不是网络地址（例如 AF_UNIX 路径）时返回 None。"""
    host: object
    if event in ("socket.connect", "socket.sendto"):
        address = args[1] if len(args) > 1 else None
        if not isinstance(address, tuple) or not address:
            return None
        host = cast(tuple[object, ...], address)[0]
    elif event == "http.client.connect":
        host = args[1] if len(args) > 1 else None
    elif event == "urllib.Request":
        url = args[0] if args else None
        host = urlsplit(url).hostname if isinstance(url, str) else None
    else:
        host = args[0] if args else None
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    return host if isinstance(host, str) else None


def is_loopback(host: str) -> bool:
    name = host.strip().lower().strip("[]")
    return name in _LOOPBACK_NAMES or name.startswith("127.")


def _hook(event: str, args: tuple[object, ...]) -> None:
    if event not in _EVENTS:
        return
    host = _host(event, args)
    if host is None or is_loopback(host):
        return
    log.warning("已拦截网络访问：%s → %s（OCR 进程只使用本地模型）", event, host)
    raise PermissionError(f"OCR 进程不允许访问网络：{event} {host}")


def install() -> None:
    """装上审计钩子。重复调用只装一次。"""
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True
