"""事件总线上的三类消息的基类。

- Command：祈使句命名，恰好一个处理者。
- Event：过去时命名，任意个处理者。
- State：名词命名，总线保存最新值，新订阅者立即收到一次。

所有具体消息都是 ``@dataclass(frozen=True, slots=True)``，消息类本身就是订阅的键。
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Message:
    """全部消息的公共基类，只用于类型约束。"""


@dataclass(frozen=True, slots=True)
class Command(Message):
    """命令：必须恰好有一个处理者。"""


@dataclass(frozen=True, slots=True)
class Event(Message):
    """事件：可以有任意个处理者，包括零个。"""


@dataclass(frozen=True, slots=True)
class State(Message):
    """状态：总线保存每个状态类的最新值，新订阅者订阅时立即收到一次。"""
