"""引擎模块：按配置创建引擎实例，发布 EngineRoster 与 EngineStatuses（架构文档 4.3、4.4、6.5 节，第 9 章）。

模块订阅两种状态：

- AppConfig：engines 节是配置服务校验过的规范 JSON 列表。模块用注册表里的配置模型逐条解析，
  为每个启用的条目创建实例。secrets_revision 变化表示 API key 有变化，网络引擎实例随之重建。
- WorkerStatuses：本地 NMT 实例的状态跟随其中 "nmt" 进程的状态。NMT 进程就绪时，模块读取能力清单里的
  模型指纹，与创建本地 NMT 实例时的指纹不同就只重建本地 NMT 实例（架构文档 2.7.3 节末尾）。

实例只在它的输入变化时重建。输入包括配置条目本身；网络引擎还包括 secrets_revision，本地 NMT 还包括
模型指纹。重建得到的实例若 EngineInfo 与旧实例相等，仍沿用旧实例对象。EngineRoster 只在实例的
EngineInfo 序列变化时发布，调度器据此替换实例（架构文档 6.5 节第 4 条）；EngineStatuses 只在内容
变化时发布。

状态里不出现 API key：缺少 key 时 detail 只写 keyring 条目名。

API key 在线程池里读取（keyring 后端可能访问系统凭据服务，耗时不定），每次重建时每个条目只读一次：
读到的值经 _Preloaded 交给工厂，工厂不再访问 keyring。重建按消息到达的顺序串行执行。

连接测试：收到 CheckEngineConnection 命令后，模块按实例当前的配置翻译一个固定的短词，发布
EngineConnectionChecked。测试不经过调度器和译文缓存，也不记录任何文本。
"""

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Final

from pydantic import ValidationError

from lst.contracts.config import AppConfig
from lst.contracts.errors import ErrorCode
from lst.contracts.events import (
    CheckEngineConnection,
    EngineConnectionChecked,
    EngineHealth,
    EngineRoster,
    EngineStatus,
    EngineStatuses,
    WorkerStatus,
    WorkerStatuses,
)
from lst.contracts.ids import EngineId, Lang, SegmentId, SessionId
from lst.contracts.ports import SecretReader, WorkerState
from lst.contracts.translation import Engine, EngineInfo, Job, Segment
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus, ModuleContext
from lst.services._shared.aio import close_quietly
from lst.services.engines.common import EngineSettings
from lst.services.engines.local_nmt.capabilities import CapabilityError, read_fingerprint
from lst.services.engines.registry import (
    ENGINE_KINDS,
    EngineDeps,
    EngineKindSpec,
    EngineUnavailableError,
    parse_entry,
)

# WorkerStatuses 里 NMT 进程的键。
NMT_WORKER: Final = "nmt"

# 连接测试翻译的固定词与语言对：先试英译中，实例不支持时再试中译英。
_PROBE_TEXT: Final = "Hello"
_PROBE_PAIRS: Final = ((Lang("en"), Lang("zh-Hans")), (Lang("zh-Hans"), Lang("en")))
# 实例没有配置时限时，连接测试等待的上限；有时限时取较小的一个。
_PROBE_TIMEOUT_S: Final = 30.0

_READY: Final = EngineStatus(EngineHealth.READY)
_LOADING: Final = EngineStatus(EngineHealth.LOADING)
_DISABLED: Final = EngineStatus(EngineHealth.DISABLED)

# NMT 进程状态没有附带说明时，出错状态使用的说明。
_WORKER_ERROR_DETAIL: Final[Mapping[WorkerState, str]] = {
    WorkerState.CRASHED: "NMT 进程崩溃，正在重启",
    WorkerState.FAILED: "NMT 进程多次崩溃或无法启动，已停止自动重启",
}


class _Preloaded:
    """已经读出的 API key，实现 SecretReader。读取时抛出的异常原样保存，取值时再抛出。"""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str | Exception | None]) -> None:
        self._values = values

    def get_secret(self, name: str) -> str | None:
        value = self._values.get(name)
        if isinstance(value, Exception):
            raise value
        return value


def _read_secrets(reader: SecretReader, names: Iterable[str]) -> dict[str, str | Exception | None]:
    """在线程池里调用：逐个读取 keyring 条目。单个条目出错不影响其他条目。"""
    values: dict[str, str | Exception | None] = {}
    for name in names:
        try:
            values[name] = reader.get_secret(name)
        except Exception as exc:
            values[name] = exc
    return values


@dataclass(frozen=True, slots=True)
class _Entry:
    """AppConfig 中的一条引擎配置。index 是它在 engines 节中的下标，优先级相同时按它排序。"""

    index: int
    spec: EngineKindSpec
    settings: EngineSettings


@dataclass(frozen=True, slots=True)
class _BuildKey:
    """决定实例是否需要重建的输入。网络引擎的 nmt_fingerprint 与本地 NMT 的 secrets_revision 恒为 None。"""

    settings: EngineSettings
    secrets_revision: int | None
    nmt_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class _Built:
    """一个启用条目的创建结果。

    engine 为 None 表示实例无法创建，不进名册。fault 是创建时就确定的出错状态（缺少 API key、依赖缺失、
    工厂抛出异常）；为 None 时状态由运行情况决定。
    """

    key: _BuildKey
    engine: Engine | None
    fault: EngineStatus | None


class EnginesModule:
    """引擎模块（架构文档 4.3 节的 Module 协议）。

    deps 是创建实例所需的依赖，其中的 HTTP 客户端由组合根管理，stop 时不关闭。
    logger 缺省时使用 ModuleContext 提供的日志器。kinds 是引擎注册表，测试时可以替换。
    """

    name = "engines"

    def __init__(
        self,
        deps: EngineDeps,
        *,
        logger: logging.Logger | None = None,
        kinds: Mapping[str, EngineKindSpec] = ENGINE_KINDS,
    ) -> None:
        self._deps = deps
        self._kinds = kinds
        self._explicit_logger = logger
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._bus: EventBus | None = None
        self._running = False
        self._refreshing = asyncio.Lock()
        self._epoch = 0  # 每次 _reset 加一；重建期间模块被停止或重启时据此放弃
        self._reset()

    def _reset(self) -> None:
        self._epoch += 1
        # None 表示还没收到 AppConfig；收到之前不创建实例，也不发布任何状态。
        self._entries: tuple[_Entry, ...] | None = None
        self._secrets_revision = 0
        # 创建本地 NMT 实例时使用的模型指纹，NMT 进程从未就绪时为空串。
        self._nmt_fingerprint = ""
        self._nmt_status: WorkerStatus | None = None
        self._built: dict[str, _Built] = {}
        self._roster_infos: tuple[EngineInfo, ...] | None = None
        self._statuses: EngineStatuses | None = None

    async def start(self, ctx: ModuleContext) -> None:
        if self._explicit_logger is None:
            self._logger = ctx.logger
        self._bus = ctx.bus
        self._reset()
        self._running = True
        # 先订阅 WorkerStatuses：两个状态的回放按订阅顺序到达，处理 AppConfig 时 NMT 进程的状态已经就位，
        # 启动时只发布一次 EngineStatuses。
        ctx.subscribe(WorkerStatuses, self._on_worker_statuses)
        ctx.subscribe(AppConfig, self._on_config)
        ctx.subscribe(CheckEngineConnection, self._on_check_connection)

    async def stop(self) -> None:
        """停止响应消息并释放实例。订阅由 ModuleContext.close 取消；HTTP 客户端由组合根关闭。"""
        self._running = False
        self._reset()

    # ------------------------------------------------------------------ 消息处理

    async def _on_config(self, config: AppConfig) -> None:
        if not self._running:
            return
        self._entries = self._parse_entries(config.section("engines"))
        self._secrets_revision = config.secrets_revision
        self._nmt_fingerprint = self._read_nmt_fingerprint()
        await self._refresh()

    async def _on_worker_statuses(self, message: WorkerStatuses) -> None:
        if not self._running:
            return
        self._nmt_status = message.statuses.get(NMT_WORKER)
        if self._entries is None:
            return
        if self._nmt_status is not None and self._nmt_status.state is WorkerState.READY:
            # 指纹变化时，本地 NMT 条目的 _BuildKey 随之变化，_refresh 只重建这些条目。
            self._nmt_fingerprint = self._read_nmt_fingerprint()
        await self._refresh()

    async def _on_check_connection(self, command: CheckEngineConnection) -> None:
        bus = self._bus
        if not self._running or bus is None:
            return
        result = await self._probe(command.engine_id)
        if self._running:
            bus.publish(result)

    # ------------------------------------------------------------------ 连接测试

    async def _probe(self, engine_id: EngineId) -> EngineConnectionChecked:
        """翻译一个固定的短词，返回测试结果。日志里只有实例号与错误码，没有文本和 API key。"""

        def failed(code: ErrorCode, latency_ms: float | None = None) -> EngineConnectionChecked:
            return EngineConnectionChecked(engine_id, ok=False, error_code=code, latency_ms=latency_ms)

        entry = next((e for e in self._entries or () if e.settings.id == engine_id), None)
        if entry is None:
            self._logger.warning("连接测试：配置里没有实例 %s", engine_id)
            return failed(ErrorCode.INTERNAL)
        engine = await self._engine_for_probe(entry)
        if isinstance(engine, EngineStatus):
            return failed(engine.error_code or ErrorCode.INTERNAL)
        pair = next((pair for pair in _PROBE_PAIRS if engine.supports(*pair)), None)
        if pair is None:
            return failed(ErrorCode.UNSUPPORTED_LANGUAGE)
        job = Job(
            session_id=SessionId("connection-check"),
            engine_id=engine_id,
            segments=(Segment(SegmentId("0"), _PROBE_TEXT),),
            source_lang=pair[0],
            target_lang=pair[1],
        )
        limit = min(engine.info.timeout_s or _PROBE_TIMEOUT_S, _PROBE_TIMEOUT_S)
        started = time.perf_counter()
        code: ErrorCode | None = ErrorCode.MISSING_SEGMENT
        stream = engine.translate(job)
        try:
            async with asyncio.timeout(limit):
                async for outcome in stream:
                    code = None if outcome.ok else (outcome.code or ErrorCode.INTERNAL)
                    break
        except TimeoutError:
            code = ErrorCode.TIMEOUT
        except Exception as exc:
            self._logger.warning("连接测试：实例 %s 抛出异常 %s", engine_id, type(exc).__name__)
            code = ErrorCode.INTERNAL
        finally:
            if await close_quietly(stream):
                self._logger.warning("连接测试：关闭实例 %s 的结果流时出错", engine_id)
        latency_ms = (time.perf_counter() - started) * 1000
        outcome_text = "通过" if code is None else f"失败（{code.value}）"
        self._logger.info("连接测试：实例 %s %s", engine_id, outcome_text)
        if code is None:
            return EngineConnectionChecked(engine_id, ok=True, latency_ms=latency_ms)
        return failed(code, latency_ms)

    async def _engine_for_probe(self, entry: _Entry) -> Engine | EngineStatus:
        """测试用的实例：已创建的直接用；已停用的临时创建一个。无法创建时返回出错状态。"""
        built = self._built.get(entry.settings.id)
        if built is not None:
            if built.fault is not None:
                return built.fault
            if built.engine is not None:
                return built.engine
        names = self._secret_names([entry])
        secrets = _Preloaded(await asyncio.to_thread(_read_secrets, self._deps.secrets, names))
        try:
            fault = self._key_fault(entry, secrets)
            if fault is not None:
                return fault
            return entry.spec.build(entry.settings, replace(self._deps, secrets=secrets))
        except EngineUnavailableError as exc:
            return EngineStatus(EngineHealth.ERROR, exc.detail, exc.code)
        except Exception as exc:
            self._logger.warning("连接测试：创建实例 %s 失败（%s）", entry.settings.id, type(exc).__name__)
            return EngineStatus(EngineHealth.ERROR, "", ErrorCode.INTERNAL)

    # ------------------------------------------------------------------ 解析配置

    def _parse_entries(self, section: JsonValue) -> tuple[_Entry, ...]:
        """解析 engines 节。配置服务已校验过，解析失败的条目记 ERROR 并跳过；日志不含字段值。"""
        if section is None:
            return ()
        if not isinstance(section, list):
            self._logger.error(
                "AppConfig 的 engines 节不是数组（%s），按没有引擎处理", type(section).__name__
            )
            return ()
        entries: list[_Entry] = []
        seen: set[str] = set()
        for index, raw in enumerate(section):
            if not isinstance(raw, dict):
                self._logger.error("AppConfig 的 engines[%d] 不是对象，已跳过", index)
                continue
            try:
                spec, settings = parse_entry(raw, self._kinds)
            except ValidationError as exc:
                where = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
                self._logger.error("AppConfig 的 engines[%d] 无法解析（字段 %s），已跳过", index, where)
                continue
            except ValueError as exc:
                self._logger.error("AppConfig 的 engines[%d] 无法解析（%s），已跳过", index, exc)
                continue
            if settings.id in seen:
                self._logger.error("AppConfig 的 engines[%d] 的 id 与前面的条目重复，已跳过", index)
                continue
            seen.add(settings.id)
            entries.append(_Entry(index, spec, settings))
        return tuple(entries)

    def _read_nmt_fingerprint(self) -> str:
        """NMT 进程能力清单里的模型指纹。

        没有 NMT 进程、从未就绪或格式不对时为空串，与本地 NMT 引擎的处理一致。
        """
        channel = self._deps.nmt
        if channel is None:
            return ""
        capabilities = channel.capabilities
        if capabilities is None:
            return ""
        try:
            return read_fingerprint(capabilities)
        except CapabilityError:
            return ""

    # ------------------------------------------------------------------ 创建实例与发布

    async def _refresh(self) -> None:
        """按当前配置与 NMT 状态更新实例，内容有变化时发布名册与状态。

        要重建的网络引擎实例的 API key 先在线程池里读出。读取期间可能又到了新配置，所以读完后按当时的
        配置再核对一遍，缺的条目补读，直到不缺为止；然后同步完成重建与发布，中间不再让出事件循环。
        """
        epoch = self._epoch
        async with self._refreshing:
            values: dict[str, str | Exception | None] = {}
            while True:
                if not self._running or self._epoch != epoch:
                    return
                names = self._secret_names(self._stale_entries())
                missing = [name for name in names if name not in values]
                if not missing:
                    break
                values.update(await asyncio.to_thread(_read_secrets, self._deps.secrets, missing))
            self._rebuild(_Preloaded(values))

    def _stale_entries(self) -> list[_Entry]:
        """启用、且输入与上次创建时不同、需要重建的条目。"""
        stale: list[_Entry] = []
        for entry in self._entries or ():
            if not entry.settings.enabled:
                continue
            previous = self._built.get(entry.settings.id)
            if previous is None or previous.key != self._key_for(entry):
                stale.append(entry)
        return stale

    @staticmethod
    def _secret_names(entries: Iterable[_Entry]) -> list[str]:
        """这些条目要读取的 keyring 条目名，去重并保持顺序。本地 NMT 不读 key。"""
        names = (e.settings.secret for e in entries if not e.spec.uses_nmt and e.settings.secret is not None)
        return list(dict.fromkeys(names))

    def _rebuild(self, secrets: SecretReader) -> None:
        """_refresh 的同步部分。secrets 已包含全部要重建的条目所需的 key。"""
        entries = self._entries if self._entries is not None else ()
        built: dict[str, _Built] = {}
        ranked: list[tuple[int, int, Engine]] = []
        statuses: dict[EngineId, EngineStatus] = {}
        names: dict[EngineId, str] = {}
        colors: dict[EngineId, str] = {}
        for entry in entries:
            settings = entry.settings
            engine_id = EngineId(settings.id)
            names[engine_id] = settings.name
            colors[engine_id] = settings.color
            if not settings.enabled:
                statuses[engine_id] = _DISABLED
                continue
            key = self._key_for(entry)
            previous = self._built.get(settings.id)
            if previous is not None and previous.key == key:
                current = previous
            else:
                current = self._build(entry, key, previous, secrets)
            built[settings.id] = current
            if current.engine is not None:
                ranked.append((settings.priority, entry.index, current.engine))
            statuses[engine_id] = current.fault if current.fault is not None else self._live_status(entry)
        self._built = built
        ranked.sort(key=lambda item: (item[0], item[1]))
        self._publish(tuple(engine for _, _, engine in ranked), EngineStatuses(statuses, names, colors))

    def _key_for(self, entry: _Entry) -> _BuildKey:
        if entry.spec.uses_nmt:
            return _BuildKey(entry.settings, None, self._nmt_fingerprint)
        return _BuildKey(entry.settings, self._secrets_revision, None)

    def _build(self, entry: _Entry, key: _BuildKey, previous: _Built | None, secrets: SecretReader) -> _Built:
        """创建一个实例。新实例的 EngineInfo 与旧实例相等时沿用旧实例对象。

        secrets 是已经读出的 key；工厂经它取 key，不访问 keyring。
        """
        settings = entry.settings
        try:
            fault = self._key_fault(entry, secrets)
            engine = entry.spec.build(settings, replace(self._deps, secrets=secrets))
        except EngineUnavailableError as exc:
            self._logger.warning("引擎实例 %s 无法创建：%s", settings.id, exc.detail)
            return _Built(key, None, EngineStatus(EngineHealth.ERROR, exc.detail, exc.code))
        except Exception as exc:
            self._logger.exception("创建引擎实例 %s 失败", settings.id)
            detail = f"创建实例失败（{type(exc).__name__}）"
            return _Built(key, None, EngineStatus(EngineHealth.ERROR, detail, ErrorCode.INTERNAL))
        if previous is not None and previous.engine is not None and previous.engine.info == engine.info:
            engine = previous.engine
        else:
            self._logger.info("已创建引擎实例 %s（%s）", settings.id, entry.spec.kind)
        return _Built(key, engine, fault)

    @staticmethod
    def _key_fault(entry: _Entry, secrets: SecretReader) -> EngineStatus | None:
        """检查网络引擎的 API key。配置了条目名但读不到 key、或必须有 key 却没配置条目名时返回出错状态。"""
        if entry.spec.uses_nmt:
            return None
        settings = entry.settings
        name = settings.secret
        if name is None:
            if entry.spec.requires_key(settings):
                return EngineStatus(EngineHealth.ERROR, "没有配置 API key 的 keyring 条目名", ErrorCode.AUTH)
            return None
        value = secrets.get_secret(name)
        if value is None or not value.strip():
            detail = f"keyring 中没有 API key 条目 {name!r}，或该条目为空"
            return EngineStatus(EngineHealth.ERROR, detail, ErrorCode.AUTH)
        return None

    def _live_status(self, entry: _Entry) -> EngineStatus:
        """没有创建期故障的实例的状态。

        网络引擎为就绪；本地 NMT 跟随 NMT 进程，映射与架构文档 8.2 节的托盘状态一致。
        """
        if not entry.spec.uses_nmt:
            return _READY
        worker = self._nmt_status
        if worker is None:
            return _LOADING
        match worker.state:
            case WorkerState.READY:
                return _READY
            case WorkerState.BUSY:
                return EngineStatus(EngineHealth.BUSY)
            case WorkerState.STOPPED | WorkerState.STARTING | WorkerState.LOADING:
                return EngineStatus(EngineHealth.LOADING, worker.detail)
            case WorkerState.CRASHED | WorkerState.FAILED:
                detail = worker.detail or _WORKER_ERROR_DETAIL[worker.state]
                return EngineStatus(EngineHealth.ERROR, detail, ErrorCode.WORKER_CRASHED)

    def _publish(self, engines: tuple[Engine, ...], statuses: EngineStatuses) -> None:
        bus = self._bus
        if bus is None:
            return
        infos = tuple(engine.info for engine in engines)
        if infos != self._roster_infos:
            self._roster_infos = infos
            bus.publish(EngineRoster(engines))
        if statuses != self._statuses:
            self._statuses = statuses
            bus.publish(statuses)
