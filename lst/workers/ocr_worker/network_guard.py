"""OCR 进程内的网络拦截（需求 PRV-01，架构文档 10.1 节）。

模型已在本地时 OCR 进程不应发起任何网络请求。PaddleX 在模型目录缺少模型时会去 HuggingFace、AIStudio、
ModelScope、BOS 下载，启动时还会检查这些站点的连通性（PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK 关掉）。
后端为每个模型显式给出本地目录，正常情况下走不到下载；这里再用审计钩子（sys.addaudithook）兜底：
解析域名、反向解析、建立连接、发送数据报、发起 urllib/http.client 请求时，目标不是本机就记一条 WARNING
并抛出 PermissionError，让这次访问失败。

判定规则（is_loopback 与 _allowed）：

- 字面 IP 地址用 ipaddress 判断：回环地址（127.0.0.0/8、::1 及其各种写法、映射到 127.0.0.0/8 的
  IPv4 映射地址）和未指定地址（0.0.0.0、::、空串，连接时指本机）放行。IPv6 的区域后缀（%1）先去掉。
- 域名只放行 localhost。以 127. 开头的域名（例如 127.evil.example.com）是普通域名，解析结果由 DNS 决定。
- socket.connect 与 socket.sendto 的地址不是字面 IP 时一律拒绝，localhost 也不例外：这两个调用在 C 层
  直接解析域名，不再触发 socket.getaddrinfo 事件，钩子看不到解析结果，无法判断最终连到哪里。

不拦截的途径：审计钩子只看本进程里 Python 代码触发的审计事件。

- subprocess、os.system 启动的外部程序（例如 curl）在另一个进程里联网，钩子看不到。PaddleX 3.7 只在
  生成公式结果的图片（formula_recognition/result.py 的 _to_img 调用 xelatex）和训练接口（repo_apis）里
  启动外部程序；OCR 后端只读取结果数据，不走这两条路径。
- 原生库（推理库的 C++ 代码）直接调用系统套接字接口，不经过审计事件。

钩子装上以后无法卸载，只在 OCR 进程入口调用 install()。与主进程的通信走命名管道，不经过套接字，不受影响。
"""

import ipaddress
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
        "socket.getnameinfo",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.gethostbyaddr",
        "http.client.connect",
        "urllib.Request",
    }
)
_ADDRESS_EVENTS = frozenset({"socket.connect", "socket.sendto"})
"""地址里的域名由 C 层直接解析的事件：只放行字面 IP 地址。"""
_LOOPBACK_NAMES = frozenset({"localhost", "localhost."})

_installed = False


def _host(event: str, args: tuple[object, ...]) -> str | None:
    """事件的目标主机名；目标不是网络地址（例如 AF_UNIX 路径）时返回 None。"""
    host: object
    if event in _ADDRESS_EVENTS:
        address = args[1] if len(args) > 1 else None
        if not isinstance(address, tuple) or not address:
            return None
        host = cast(tuple[object, ...], address)[0]
    elif event == "socket.getnameinfo":
        address = args[0] if args else None
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


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """host 是字面 IP 地址时返回它（去掉方括号与 IPv6 区域后缀，空串按 0.0.0.0），否则返回 None。"""
    name = host.strip().strip("[]")
    if not name:
        return ipaddress.IPv4Address(0)
    try:
        return ipaddress.ip_address(name.partition("%")[0])
    except ValueError:
        return None


def _local_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def is_loopback(host: str) -> bool:
    """host 指本机：字面 IP 是回环或未指定地址，或者域名是 localhost。"""
    ip = _literal_ip(host)
    if ip is not None:
        return _local_ip(ip)
    return host.strip().lower() in _LOOPBACK_NAMES


def _allowed(event: str, host: str) -> bool:
    if event in _ADDRESS_EVENTS:
        ip = _literal_ip(host)
        return ip is not None and _local_ip(ip)
    return is_loopback(host)


def _hook(event: str, args: tuple[object, ...]) -> None:
    if event not in _EVENTS:
        return
    host = _host(event, args)
    if host is None or _allowed(event, host):
        return
    log.warning("已拦截网络访问：%s → %s（OCR 进程只使用本地模型）", event, host)
    raise PermissionError(f"OCR 进程不允许访问网络：{event} {host}")


def install() -> None:
    """装上审计钩子。重复调用只装一次。"""
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True
