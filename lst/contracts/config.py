"""配置相关的总线消息（架构文档第 9 章）。

contracts 只依赖标准库，放不了 pydantic 模型，所以配置这样传递：
1. 每个模块用 pydantic 定义自己那一节配置的模型，组合根把“节名 → 模型”交给配置服务；
2. 配置服务用这些模型校验，校验失败的字段或条目回退并通过 ConfigRejected 报告；
3. 配置服务发布 AppConfig，内容是校验后的规范 JSON；
4. 各模块用自己的模型解析自己那一节。数据已经校验过，这一步一定成功。
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from lst.contracts.messages import Command, Event, State
from lst.contracts.worker_protocol import JsonValue


@dataclass(frozen=True, slots=True)
class AppConfig(State):
    """当前生效的全部配置。

    sections：节名 → 校验后的规范 JSON（各模块用自己的模型解析）。
    revision：每次发布加一。secrets_revision：API key 每次变化加一，引擎模块据此重建实例。
    """

    sections: Mapping[str, JsonValue]
    revision: int
    secrets_revision: int = 0

    def section(self, name: str) -> JsonValue:
        """取一节配置；该节不存在时返回 None。"""
        return self.sections.get(name)


@dataclass(frozen=True, slots=True)
class UpdateConfig(Command):
    """修改配置。patch 是“节名 → 该节要修改的字段”，按字段合并，不是整节替换。

    数组类型的节（例如 engines）整体替换。
    """

    patch: Mapping[str, JsonValue]
    request_id: str | None = None
    """需要精确回执时填写；配置服务处理完后发布同号的 ConfigUpdateCompleted。"""


@dataclass(frozen=True, slots=True)
class UpdateSecret(Command):
    """写入或删除一个 API key。value 为 None 表示删除。

    事件总线和日志只记录消息的类名，value 不会被记录（需求 CFG-04）。repr 里也不显示 value。
    """

    name: str
    value: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class ConfigRejected(Event):
    """一处配置没有通过校验，已回退到默认值或被丢弃（需求 CFG-02）。

    location：出错的位置，例如 ("race", "mode") 或 ("engines", 2, "priority")。
    reason：给用户看的原因说明（技术性的，界面可以原样显示）。不包含字段的原值，原值可能是机密。
    """

    location: tuple[str | int, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ConfigUpdateCompleted(Event):
    """一次配置修改的回执，包含这次修改完成时的快照；无变化与全部拒绝也有回执。"""

    request_id: str
    config: AppConfig
    changed: bool
    rejected: tuple[ConfigRejected, ...] = ()
