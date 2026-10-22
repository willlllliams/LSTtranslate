r"""自动化接口的客户端一侧：线路协议、管道的安全设置与同步客户端（架构文档第 16 章，需求 DEV-01 至 DEV-08）。

本包只依赖标准库，不导入 Qt 与程序的其他部分：安装版用安装目录里的解释器运行
``launcher\worker.py automation <方法> [JSON 参数]``，源码版运行 ``python -m lst.automation``，
都只加载这里的几个模块。服务端在 ``lst.app.automation``。

- protocol：一行一个 JSON 的请求与回复；
- winsec：当前用户的 SID、管道名、管道的安全描述符与所有者检查；
- client：AutomationClient，以及等待程序重启后重新连上的辅助函数；
- __main__：命令行。
"""

from lst.automation.client import (
    AutomationClient,
    AutomationError,
    ConnectionLost,
    restart_and_reconnect,
    wait_for_server,
)
from lst.automation.protocol import PROTOCOL_VERSION, ErrorCode

__all__ = [
    "PROTOCOL_VERSION",
    "AutomationClient",
    "AutomationError",
    "ConnectionLost",
    "ErrorCode",
    "restart_and_reconnect",
    "wait_for_server",
]
