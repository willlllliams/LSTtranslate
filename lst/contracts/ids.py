"""标识符类型。"""

from enum import StrEnum
from typing import NewType

SessionId = NewType("SessionId", str)
SegmentId = NewType("SegmentId", str)
EngineId = NewType("EngineId", str)

# 语言代码，统一用 BCP 47 形式，例如 "zh-Hans"、"en"、"ja"。
Lang = NewType("Lang", str)


class Entry(StrEnum):
    """会话入口。每个入口同一时刻最多有一个当前会话。"""

    SCREENSHOT = "screenshot"
    BILINGUAL = "bilingual"
