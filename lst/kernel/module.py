"""模块生命周期协议与模块上下文（架构文档 4.3 节）。

组合根为每个模块创建一个 ``ModuleContext``，调用 ``await module.start(ctx)``；
模块在 ``start`` 里通过 ``ctx.subscribe`` 订阅消息。退出时组合根先 ``await module.stop()``，
再调用 ``ctx.close()`` 统一取消这个模块的全部订阅。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from lst.contracts.messages import Message
from lst.kernel.bus import EventBus, Subscription


class ModuleContext:
    """一个模块运行期间持有的上下文：事件总线、日志器，以及它的订阅句柄。"""

    def __init__(self, bus: EventBus, logger: logging.Logger) -> None:
        self._bus = bus
        self._logger = logger
        self._subscriptions: list[Subscription] = []
        self._closed = False

    @property
    def bus(self) -> EventBus:
        """模块使用的事件总线。"""
        return self._bus

    @property
    def logger(self) -> logging.Logger:
        """模块自己的日志器。"""
        return self._logger

    @property
    def closed(self) -> bool:
        """``close()`` 调用过之后为 True。"""
        return self._closed

    def subscribe[M: Message](
        self, kind: type[M], handler: Callable[[M], Awaitable[None] | None]
    ) -> Subscription:
        """经由总线订阅消息，并记下句柄，``close()`` 时统一取消。

        上下文关闭后再订阅会抛出 ``RuntimeError``，避免产生无人托管的订阅。
        """
        if self._closed:
            raise RuntimeError("模块上下文已关闭，不能再订阅")
        sub = self._bus.subscribe(kind, handler)
        self._subscriptions.append(sub)
        return sub

    def close(self) -> None:
        """取消本上下文登记的全部订阅。重复调用没有效果。"""
        if self._closed:
            return
        self._closed = True
        subs, self._subscriptions = self._subscriptions, []
        for sub in subs:
            sub.cancel()


class Module(Protocol):
    """每个模块对外暴露的唯一入口。"""

    name: str

    async def start(self, ctx: ModuleContext) -> None:
        """启动模块：订阅消息、准备资源。"""
        ...

    async def stop(self) -> None:
        """停止模块：释放资源。订阅由 ``ModuleContext.close`` 取消。"""
        ...
