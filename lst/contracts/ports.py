"""端口协议：模块之间需要返回值的调用，由组合根注入实现（架构文档 4.4 节）。"""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from lst.contracts.ids import EngineId, Lang
from lst.contracts.worker_protocol import JsonValue, OcrRequest, TranslateRequest, WorkerReply


@dataclass(frozen=True, slots=True)
class CacheKey:
    """译文缓存的键（需求 RACE-08）。

    fingerprint 是引擎实例中影响译文的配置（模型、提示词、温度等）的哈希，取自 EngineInfo.fingerprint；
    换模型或改提示词后，旧译文因键不同而不会被误用。
    """

    text: str
    source: Lang
    target: Lang
    engine_id: EngineId
    fingerprint: str


class TranslationCache(Protocol):
    """译文缓存。使用方是 services.race，实现方是 services.cache。"""

    async def get_many(self, keys: Sequence[CacheKey]) -> Mapping[CacheKey, str]:
        """批量查询，只返回命中的键。"""
        ...

    async def put(self, key: CacheKey, text: str) -> None:
        """写入一条译文。"""
        ...


class SecretReader(Protocol):
    """读取 API key。使用方是 services.engines，实现方是 services.config（keyring）。

    只读；写入走 UpdateSecret 命令（架构文档 4.4 节）。条目不存在时返回 None。
    """

    def get_secret(self, name: str) -> str | None: ...


class WorkerState(StrEnum):
    """监管器为每个推理进程维护的状态（架构文档 8.2 节）。"""

    STOPPED = "stopped"
    STARTING = "starting"
    LOADING = "loading"
    READY = "ready"
    BUSY = "busy"
    CRASHED = "crashed"
    FAILED = "failed"


class WorkerRequestHandle(Protocol):
    """一个已提交的推理请求。"""

    @property
    def request_id(self) -> str: ...

    def replies(self) -> AsyncIterator[WorkerReply]:
        """按到达顺序产出子进程的回复，以 RequestDone、RequestFailed 或 OcrResult 结束。

        子进程崩溃、请求超时、监管器进入 FAILED 时，监管器补发一条 RequestFailed，
        code 分别为 engine.worker_crashed、engine.timeout、engine.worker_crashed。
        """
        ...

    def cancel(self) -> None:
        """取消请求：还在监管器排队的直接结束；已发给子进程的发送 Cancel。可以重复调用。"""
        ...


class WorkerChannel(Protocol):
    """向一个推理进程发送请求（架构文档 4.4 节）。

    使用方是 services.ocr 与本地 NMT 引擎，实现方是 app 中的监管器。
    """

    @property
    def state(self) -> WorkerState: ...

    @property
    def capabilities(self) -> Mapping[str, JsonValue] | None:
        """最近一次 Ready 消息里的能力清单；子进程从未就绪时为 None。"""
        ...

    def submit(
        self, request: TranslateRequest | OcrRequest, *, payload: bytes | None = None, timeout_s: float
    ) -> WorkerRequestHandle:
        """提交请求。

        timeout_s 是子进程处理该请求的时限，从收到 Started 时开始计时；超时后监管器结束并重启进程。
        """
        ...
