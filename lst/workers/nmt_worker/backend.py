"""NMT 后端协议。

NMT 进程把“怎么翻译”交给后端，自己只负责把后端接到运行时上（handler.py）。默认后端是 CTranslate2
（ct2_backend.py）；测试通过 `--backend module:attr` 换成假后端。`attr` 是一个无参可调用对象（通常就是类），
调用它得到一个后端实例。

后端要实现两个方法，都只在推理线程上调用：

- `load(settings, progress) -> LoadResult`：按 Configure 的设置加载（运行中再次收到 Configure 时重新加载）。
  设置无效抛 ConfigError；推理依赖导入失败抛 ImportError 或 OSError。返回的 capabilities 用
  make_capabilities 构造，格式见该函数。
- `translate(source, target, segments) -> Iterable[SegmentOutcome]`：逐段产出结果，每段恰好一个。
  单段失败用 SegmentOutcome.failed 表示，不影响其他段。处理器对漏掉的段补发 engine.internal 错误。
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, Self

from lst.contracts.errors import ErrorCode
from lst.contracts.worker_protocol import JsonValue, TranslateSegment
from lst.workers._runtime import LoadResult, ProgressReporter

type Pair = tuple[str, str]


@dataclass(frozen=True, slots=True)
class SegmentOutcome:
    """一段的翻译结果：text 与 error 恰好一个不为 None；code 是 ErrorCode 的取值。"""

    segment_id: str
    text: str | None = None
    error: str | None = None
    code: str | None = None

    @classmethod
    def ok(cls, segment_id: str, text: str) -> Self:
        return cls(segment_id, text=text)

    @classmethod
    def failed(cls, segment_id: str, code: ErrorCode, error: str) -> Self:
        return cls(segment_id, error=error, code=code.value)


class NmtBackend(Protocol):
    """NMT 后端，见模块说明。"""

    def load(self, settings: Mapping[str, JsonValue], progress: ProgressReporter) -> LoadResult: ...

    def translate(
        self, source: str, target: str, segments: Sequence[TranslateSegment]
    ) -> Iterable[SegmentOutcome]: ...


def make_capabilities(pairs: Iterable[Pair], pivot: str | None, fingerprint: str) -> dict[str, JsonValue]:
    """构造 Ready 消息里的能力清单。

    格式：`{"pairs": [["en", "zh-Hans"], ...], "pivot": "en", "fingerprint": "..."}`。

    - pairs 只列直连模型，语言码用规范写法，按字典序排列；
    - pivot 是中转语言，None 表示不做中转；
    - fingerprint 是影响译文的设置与已安装模型版本的稳定哈希，主进程用它区分译文缓存。
    """
    listed: list[JsonValue] = [[source, target] for source, target in sorted(set(pairs))]
    return {"pairs": listed, "pivot": pivot, "fingerprint": fingerprint}


def stable_fingerprint(data: JsonValue) -> str:
    """JSON 数据的稳定哈希：键排序后序列化再取 SHA-256，与进程、字典插入顺序和 PYTHONHASHSEED 无关。"""
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
