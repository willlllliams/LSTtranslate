"""设置窗口用到的新消息的类别与不可变性：命令、事件、状态各归其类，都是 frozen 的 slots 数据类。"""

import dataclasses

import pytest

from lst.contracts.events import (
    CacheCleared,
    CheckEngineConnection,
    ClearCache,
    EngineConnectionChecked,
    EngineStatsRow,
    ResetStats,
    StatsSnapshot,
)
from lst.contracts.ids import EngineId
from lst.contracts.messages import Command, Event, State

CASES: list[tuple[object, type]] = [
    (ResetStats(), Command),
    (ClearCache(), Command),
    (CheckEngineConnection(EngineId("x")), Command),
    (CacheCleared(3), Event),
    (EngineConnectionChecked(EngineId("x"), ok=True), Event),
    (StatsSnapshot(()), State),
]


@pytest.mark.parametrize(("message", "base"), CASES)
def test_message_category_and_immutability(message: object, base: type) -> None:
    assert isinstance(message, base)
    assert dataclasses.is_dataclass(message)
    assert not hasattr(message, "__dict__")  # slots=True
    with pytest.raises(dataclasses.FrozenInstanceError):
        message.__setattr__("anything", 1)  # type: ignore[misc]


def test_stats_row_has_numbers_only() -> None:
    row = EngineStatsRow(EngineId("x"), 1, 1)
    assert (row.p50_ms, row.p95_ms, row.failure_rate) == (None, None, None)
    # 只有实例号和数字，没有任何文本字段（需求 PRV-02）。
    assert [field.name for field in dataclasses.fields(row)] == [
        "engine_id",
        "executed",
        "wins",
        "p50_ms",
        "p95_ms",
        "failure_rate",
    ]


def test_connection_result_defaults() -> None:
    result = EngineConnectionChecked(EngineId("x"), ok=False)
    assert (result.error_code, result.latency_ms) == (None, None)
