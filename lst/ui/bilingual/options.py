"""从 AppConfig 中读出双语窗口用到的几项配置。

[bilingual] 节的 pydantic 模型属于竞速模块，界面模块不能依赖它（架构文档 4.2 节），
所以这里只按键读取需要的值：键缺失、类型不对或超出范围时取默认值，其余键一概不看。
"""

from dataclasses import dataclass
from typing import Literal

from lst.contracts.config import AppConfig
from lst.contracts.worker_protocol import JsonValue

type Layout = Literal["vertical", "horizontal"]

DEFAULT_DEBOUNCE_MS = 400
MAX_DEBOUNCE_MS = 10_000


@dataclass(frozen=True, slots=True)
class BilingualOptions:
    """双语窗口的配置。

    debounce_ms：输入停顿多久后自动翻译（需求 BI-04），取自 bilingual.debounce_ms。
    layout：输入区与译文区上下排列还是左右排列（需求 BI-02），取自 bilingual.layout。
    """

    debounce_ms: int = DEFAULT_DEBOUNCE_MS
    layout: Layout = "vertical"


def _table(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def parse_options(config: AppConfig) -> BilingualOptions:
    """解析配置。任何一项不合法都只让这一项回到默认值。"""
    bilingual = _table(config.section("bilingual"))
    defaults = BilingualOptions()

    debounce = bilingual.get("debounce_ms")
    # bool 是 int 的子类，要单独排除。
    if not (
        isinstance(debounce, int) and not isinstance(debounce, bool) and 0 <= debounce <= MAX_DEBOUNCE_MS
    ):
        debounce = defaults.debounce_ms

    layout_value = bilingual.get("layout")
    layout: Layout = "horizontal" if layout_value == "horizontal" else defaults.layout

    return BilingualOptions(debounce_ms=debounce, layout=layout)
