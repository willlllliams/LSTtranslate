"""自动化接口的服务端（需求 DEV-01 至 DEV-08，架构文档第 16 章）。

- config：配置节 [automation]，开关的默认值随构建类型；
- server：主进程事件循环里的命名管道服务端；
- recorder：截屏会话的识别与翻译结果；
- interaction：截屏蒙版、覆盖窗口与设置窗口的操作和画面；
- methods：方法表；
- module：AutomationModule，按配置开启或关闭服务端，发布 AutomationStatus。

客户端与线路协议在 ``lst.automation``，只依赖标准库。
"""

from lst.app.automation.config import CONFIG_SECTIONS, AutomationConfig, effective_enabled
from lst.app.automation.methods import AutomationDeps, AutomationMethods
from lst.app.automation.module import AutomationModule
from lst.app.automation.server import Deferred, MethodError, PipeServer

__all__ = [
    "CONFIG_SECTIONS",
    "AutomationConfig",
    "AutomationDeps",
    "AutomationMethods",
    "AutomationModule",
    "Deferred",
    "MethodError",
    "PipeServer",
    "effective_enabled",
]
