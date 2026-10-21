"""NMT 进程能力清单的格式与解析。

能力清单是 NMT 进程 Ready 消息里的 capabilities 字段，格式由 NMT 进程与本引擎约定：

    {"pairs": [["en", "zh-Hans"], ["zh-Hans", "en"]], "pivot": "en", "fingerprint": "..."}

- pairs 只列已安装的直连模型，每项是（源语言，目标语言）；
- pivot 是中转语言，为 null 或缺失表示不中转（架构文档 2.5 节）；
- fingerprint 是已安装模型集合的指纹，模型增删或更新时改变，参与译文缓存键。
"""

from collections.abc import Mapping
from dataclasses import dataclass

from lst.contracts.worker_protocol import JsonValue


class CapabilityError(ValueError):
    """能力清单的格式不符合约定。异常信息只指出哪一项不对，不含其他内容。"""


@dataclass(frozen=True, slots=True)
class NmtCapabilities:
    """解析后的能力清单：直连语言对集合与中转语言。"""

    pairs: frozenset[tuple[str, str]]
    pivot: str | None = None

    def supports(self, source: str, target: str) -> bool:
        """源语言与目标语言不同，并且满足以下两条之一：

        - 有直连模型 source→target；
        - 有中转语言 pivot，源语言和目标语言都不是 pivot，并且 source→pivot 与 pivot→target 两个模型都在。

        中转的两跳都必须是跨语言模型，所以源语言或目标语言本身是中转语言时，只能靠直连模型。
        """
        if source == target:
            return False
        if (source, target) in self.pairs:
            return True
        pivot = self.pivot
        return (
            pivot is not None
            and pivot not in (source, target)
            and (source, pivot) in self.pairs
            and (pivot, target) in self.pairs
        )


def parse_capabilities(raw: Mapping[str, JsonValue]) -> NmtCapabilities:
    """解析能力清单中的 pairs 与 pivot，格式不对时抛出 CapabilityError。fingerprint 不在这里检查。"""
    items = raw.get("pairs")
    if not isinstance(items, list):
        raise CapabilityError("pairs 缺失或不是数组")
    pairs: set[tuple[str, str]] = set()
    for index, item in enumerate(items):
        if not isinstance(item, list) or len(item) != 2:
            raise CapabilityError(f"pairs[{index}] 不是两个元素的数组")
        source, target = item
        if not isinstance(source, str) or not isinstance(target, str) or not source or not target:
            raise CapabilityError(f"pairs[{index}] 的语言代码不是非空字符串")
        pairs.add((source, target))
    pivot = raw.get("pivot")
    if pivot is None:
        return NmtCapabilities(frozenset(pairs))
    if not isinstance(pivot, str) or not pivot:
        raise CapabilityError("pivot 不是非空字符串，也不是 null")
    return NmtCapabilities(frozenset(pairs), pivot)


def read_fingerprint(raw: Mapping[str, JsonValue]) -> str:
    """能力清单里的模型指纹；缺失或不是字符串时抛出 CapabilityError。"""
    value = raw.get("fingerprint")
    if not isinstance(value, str):
        raise CapabilityError("fingerprint 缺失或不是字符串")
    return value
