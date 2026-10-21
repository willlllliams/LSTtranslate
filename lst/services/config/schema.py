"""配置服务认识的两类模式与位置类型。

普通节（TOML 的表）每节一个 pydantic 模型；数组节（TOML 的 ``[[name]]``）由使用方模块提供一个
在结构上满足 ``ListSectionSchema`` 的对象。配置服务不 import 任何具体模块，两类模式都由组合根注入。
"""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from lst.contracts.worker_protocol import JsonValue

type Location = tuple[str | int, ...]
"""配置中的位置，例如 ("race", "mode") 或 ("engines", 2, "priority")。"""

type Issue = tuple[Location, str]
"""一处回退或拒绝：位置与原因。原因里不出现字段的原值，原值可能是机密。"""


class ListSectionSchema(Protocol):
    """数组节的校验模式，例如 ``[[engines]]``。

    配置服务对每个条目调用 ``validate_item``，失败的条目整条丢弃；留下的条目再整体交给
    ``normalize`` 处理跨条目的约束（例如实例号唯一、优先级两两不同）。
    """

    def validate_item(self, raw: Mapping[str, Any]) -> dict[str, JsonValue]:
        """校验一个条目，返回它的规范 JSON。失败时抛出 ``pydantic.ValidationError`` 或 ``ValueError``。"""
        ...

    def normalize(
        self, items: list[dict[str, JsonValue]]
    ) -> tuple[list[dict[str, JsonValue]], list[tuple[tuple[str | int, ...], str]]]:
        """处理跨条目约束，返回规范化后的条目和要报告的问题。

        问题的位置相对于该数组节，第一个元素是条目在 ``items`` 中的下标。
        """
        ...


@runtime_checkable
class ProvidesDefaultItems(Protocol):
    """可选能力：数组节在配置文件里整节缺失时使用的默认条目。

    没有实现它的数组节缺失时视为空数组。返回值必须是 ``validate_item`` 的输出。
    """

    def default_items(self) -> list[dict[str, JsonValue]]: ...
