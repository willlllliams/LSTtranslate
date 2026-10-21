"""设置窗口的展示器（需求 UI-05、UI-06、CFG-01 至 CFG-05，架构文档 11.1、11.7 节）。

展示器订阅配置、引擎状态、统计、模型目录等状态，把用户的操作转换成命令：

- ``UpdateConfig``：按字段合并（数组节整体替换）；
- ``UpdateSecret``：API key 只经由它提交，不进入配置、日志和界面；
- ``ResetStats``、``ClearCache``、``CheckEngineConnection`` 与四个模型命令。

视图（pages、window）只负责绘制和收集输入：它们读展示器的属性，调用展示器的方法，
并通过 ``subscribe`` 登记回调，在 ``Topic`` 指明的数据变化时刷新自己。本模块不依赖 Qt，
画面之外的行为都可以在没有 QApplication 的测试里检查。

``ConfigRejected`` 记在 ``rejections`` 里，位置是配置里的位置，例如 ("race", "mode")；
引擎的位置 ("engines", 下标, 字段) 在这里换算成实例号，("engines", "deepseek", "base_url")，
因为下标会随排序变化。用户再次修改同一个设置项时（``clear_rejection``）提示消失。
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from pydantic import BaseModel

from lst.contracts.config import AppConfig, ConfigRejected, UpdateConfig, UpdateSecret
from lst.contracts.errors import ErrorCode
from lst.contracts.events import (
    CacheCleared,
    CheckEngineConnection,
    ClearCache,
    EngineConnectionChecked,
    EngineHealth,
    EngineStatsRow,
    EngineStatuses,
    ResetStats,
    StatsSnapshot,
    WorkerStatuses,
)
from lst.contracts.ids import EngineId
from lst.contracts.models import (
    CancelModelDownload,
    DeleteModel,
    DownloadModels,
    ModelCatalog,
    RefreshModelCatalog,
)
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus
from lst.ui import i18n
from lst.ui.settings.entries import (
    KEYLESS_KINDS,
    OPTIONAL_KEY_PROVIDERS,
    EngineDraft,
    EngineEntry,
    Entry,
    engine_entries,
    new_entry,
    raw_entries,
    renumber,
    reorder,
    secret_name_for,
    used_secret_names,
    validate_entry,
    with_field,
)
from lst.ui.settings.schema import (
    FieldSpec,
    field_spec,
    nested_patch,
    read_path,
    read_value,
)

type Location = tuple[str | int, ...]
type Listener = Callable[["Topic"], None]

API_KEY_FIELD: Final = "api_key"
"""引擎实例的 API key 卡片在 ConfigRejected 位置里的字段名。它不是配置里的字段，只用来定位提示。"""


class Topic(StrEnum):
    """展示器通知视图的数据类别。"""

    CONFIG = "config"
    ENGINES = "engines"
    """引擎列表、状态或 API key 状态有变化。"""
    REJECTIONS = "rejections"
    STATS = "stats"
    MODELS = "models"
    WORKERS = "workers"
    LANGUAGE = "language"
    CONNECTION = "connection"
    CACHE = "cache"
    OPEN = "open"
    """收到 OpenSettings：视图应当显示并激活窗口。"""


class KeyState(StrEnum):
    """一个引擎实例的 API key 状态。界面只显示这个状态，永远不读取、不显示已保存的 key。"""

    SET = "set"
    UNSET = "unset"
    UNKNOWN = "unknown"
    """没有启用的实例不会读取 key，无法判断。"""
    NOT_NEEDED = "not_needed"


class CheckState(StrEnum):
    CHECKING = "checking"
    OK = "ok"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ConnectionResult:
    """一个实例最近一次连接测试的状态。"""

    state: CheckState
    error_code: ErrorCode | None = None
    latency_ms: float | None = None


class SettingsPresenter:
    """设置窗口的展示器。所有方法都在主线程上调用，不 await 任何东西。

    sections 是“节名 → pydantic 模型”，engine_kinds 是“引擎类别 → 该类的配置模型”，都由组合根注入
    （界面模块不能 import services）。keyless_kinds 是不需要 API key 的引擎类别。
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        logger: logging.Logger,
        sections: Mapping[str, type[BaseModel]],
        engine_kinds: Mapping[str, type[BaseModel]],
        keyless_kinds: frozenset[str] = KEYLESS_KINDS,
        app_version: str = "",
    ) -> None:
        self._bus = bus
        self._logger = logger
        self._sections = dict(sections)
        self._engine_kinds = dict(engine_kinds)
        self._keyless_kinds = keyless_kinds
        self.app_version = app_version
        self._listeners: list[Listener] = []
        self._closed = False
        self._specs: dict[tuple[str, tuple[str, ...]], FieldSpec | None] = {}

        self.config: AppConfig | None = None
        self.engine_statuses: EngineStatuses | None = None
        self.worker_statuses: WorkerStatuses | None = None
        self.catalog: ModelCatalog | None = None
        self.stats: tuple[EngineStatsRow, ...] = ()
        self.stats_loaded = False
        self.last_cache_cleared: int | None = None
        self._rejections: dict[Location, str] = {}
        self._draft = EngineDraft()
        self._sent_ids: list[str] = []
        self._connections: dict[str, ConnectionResult] = {}
        self._keys_set: set[str] = set()
        self._keys_cleared: set[str] = set()
        self._language: str | None = None
        self._models_requested = False
        self.pending_select: str | None = None
        """刚添加的实例号：引擎列表出现它之后，视图选中它。"""

    # ------------------------------------------------------------------ 生命周期与通知

    def start(self) -> None:
        """用总线上已有的状态初始化。之后的变化由订阅的处理函数送来。"""
        self.config = self._bus.latest(AppConfig)
        self.engine_statuses = self._bus.latest(EngineStatuses)
        self.worker_statuses = self._bus.latest(WorkerStatuses)
        self.catalog = self._bus.latest(ModelCatalog)
        snapshot = self._bus.latest(StatsSnapshot)
        if snapshot is not None:
            self.stats, self.stats_loaded = snapshot.engines, True
        self._apply_language()

    def close(self) -> None:
        """之后不再响应任何消息和操作。重复调用没有效果。"""
        self._closed = True
        self._listeners.clear()

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """登记数据变化的回调，返回取消登记的函数。"""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self, *topics: Topic) -> None:
        for topic in topics:
            for listener in tuple(self._listeners):
                listener(topic)

    # ------------------------------------------------------------------ 配置

    def spec(self, section: str, path: Sequence[str]) -> FieldSpec | None:
        """配置里一个设置项的描述；组合根没有注入这一节或模型里没有这个字段时返回 None。"""
        key = (section, tuple(path))
        if key not in self._specs:
            model = self._sections.get(section)
            try:
                self._specs[key] = field_spec(model, path) if model is not None else None
            except KeyError:
                self._specs[key] = None
        return self._specs[key]

    def value(self, section: str, path: Sequence[str]) -> JsonValue:
        """设置项的当前值：配置里没有时是模型的默认值。设置项不存在时返回 None。"""
        spec = self.spec(section, path)
        if spec is None:
            return None
        return read_value(self.config, section, path, spec)

    def set_value(self, section: str, path: Sequence[str], value: JsonValue) -> None:
        """提交一个设置项的修改。其他设置项不受影响；非法的值由配置服务拒绝并通过 ConfigRejected 报告。"""
        if self._closed:
            return
        location = (section, *path)
        self.clear_rejection(location)
        self._logger.info("设置窗口：提交设置项 %s", ".".join(location))
        self._bus.send(UpdateConfig({section: nested_patch(path, value)}))

    def on_config(self, config: AppConfig) -> None:
        if self._closed:
            return
        self.config = config
        self._draft.confirm(config)
        self._apply_language()
        self._notify(Topic.CONFIG, Topic.ENGINES)

    def _apply_language(self) -> None:
        """界面语言。组合根也会设置它，但两个处理函数谁先执行没有保证，所以这里自己设置，保证文案即时刷新。"""
        raw = read_path(self.config, "ui", ("language",))
        if not isinstance(raw, str):
            return
        applied = i18n.set_language(raw)
        if applied != self._language:
            first = self._language is None
            self._language = applied
            if not first:
                self._notify(Topic.LANGUAGE)

    # ------------------------------------------------------------------ ConfigRejected

    @property
    def rejections(self) -> Mapping[Location, str]:
        return dict(self._rejections)

    def rejection_for(self, location: Location) -> str | None:
        """一个设置项旁边要显示的提示：位置落在该设置项之内的第一条 ConfigRejected 的原因。"""
        for rejected, reason in self._rejections.items():
            if rejected[: len(location)] == location:
                return reason
        return None

    def clear_rejection(self, location: Location) -> None:
        """用户重新修改了一个设置项：它之前的提示消失。"""
        stale = [rejected for rejected in self._rejections if rejected[: len(location)] == location]
        if not stale:
            return
        for rejected in stale:
            del self._rejections[rejected]
        self._notify(Topic.REJECTIONS)

    def on_rejected(self, event: ConfigRejected) -> None:
        if self._closed:
            return
        location = self._translate_location(event.location)
        if location[:1] == ("engines",):
            # 引擎列表被拒绝时，界面草稿作废，下一次修改回到配置里的列表。
            self._draft.discard()
        self._rejections[location] = event.reason
        self._logger.info("设置窗口：设置项 %s 没有被采用", ".".join(str(part) for part in location))
        self._notify(Topic.REJECTIONS)

    def _translate_location(self, location: tuple[str | int, ...]) -> Location:
        """引擎的下标换成实例号；密钥条目换成对应实例的 API key 卡片。"""
        if len(location) >= 2 and location[0] == "engines" and isinstance(location[1], int):
            index = location[1]
            if 0 <= index < len(self._sent_ids):
                return ("engines", self._sent_ids[index], *location[2:])
            return location
        if len(location) == 2 and location[0] == "secrets" and isinstance(location[1], str):
            for entry in engine_entries(self.config):
                if entry.secret == location[1]:
                    return ("engines", entry.id, API_KEY_FIELD)
        return location

    # ------------------------------------------------------------------ 翻译引擎

    def engines(self) -> list[EngineEntry]:
        """引擎实例，按优先级从高到低。"""
        return engine_entries(self.config)

    def engine(self, engine_id: str) -> EngineEntry | None:
        return next((entry for entry in self.engines() if entry.id == engine_id), None)

    def engine_name(self, engine_id: str) -> str:
        """实例的显示名：优先取引擎模块发布的名称，其次取配置里的名称，最后是实例号。"""
        if self.engine_statuses is not None:
            name = self.engine_statuses.names.get(EngineId(engine_id))
            if name:
                return name
        entry = self.engine(engine_id)
        return entry.name if entry is not None else engine_id

    def engine_health(self, engine_id: str) -> EngineHealth | None:
        if self.engine_statuses is None:
            return None
        status = self.engine_statuses.statuses.get(EngineId(engine_id))
        return status.health if status is not None else None

    def engine_error_code(self, engine_id: str) -> ErrorCode | None:
        """实例处于“出错”状态时的错误码，界面据此显示原因和处理建议。"""
        if self.engine_statuses is None:
            return None
        status = self.engine_statuses.statuses.get(EngineId(engine_id))
        if status is None or status.health is not EngineHealth.ERROR:
            return None
        return status.error_code or ErrorCode.INTERNAL

    def on_engine_statuses(self, statuses: EngineStatuses) -> None:
        if self._closed:
            return
        self.engine_statuses = statuses
        self._notify(Topic.ENGINES)

    def model_for(self, kind: str) -> type[BaseModel] | None:
        return self._engine_kinds.get(kind)

    @property
    def engine_kind_names(self) -> list[str]:
        return list(self._engine_kinds)

    @property
    def keyless_kinds(self) -> frozenset[str]:
        return self._keyless_kinds

    def check_new_engine(self, kind: str, values: Mapping[str, JsonValue]) -> dict[str, str]:
        """新建一个实例的参数是否合法：返回 {字段名: 说明}，为空表示可以添加。不发送任何命令。"""
        entries = self._ordered_working()
        taken = {str(entry.get("id")) for entry in entries}
        _, errors = new_entry(self._engine_kinds, kind, values, taken_ids=taken, priority=len(entries) + 1)
        return errors

    def _send_engines(self, entries: list[Entry]) -> None:
        self._draft.sent(entries)
        self._sent_ids = [str(entry.get("id")) for entry in entries]
        self._bus.send(UpdateConfig({"engines": cast(JsonValue, entries)}))

    def _ordered_working(self) -> list[Entry]:
        """修改的基础列表，按优先级排列，优先级相同按配置里的顺序。"""
        working = self._draft.working(self.config)
        indexed = sorted(enumerate(working), key=lambda item: (_priority_of(item[1]), item[0]))
        return [entry for _, entry in indexed]

    def set_engine_field(self, engine_id: str, key: str, value: JsonValue) -> bool:
        """修改一个实例的一个参数，返回是否发出了修改。

        先用该类引擎的模型校验整条记录：不合法时不发送（配置服务会把不合法的条目整条丢弃，
        实例会因此消失），说明记在 ``rejections`` 里，显示在对应的参数旁边。
        """
        if self._closed:
            return False
        entries = self._ordered_working()
        index = next((i for i, entry in enumerate(entries) if entry.get("id") == engine_id), None)
        if index is None:
            return False
        self.clear_rejection(("engines", engine_id, key))
        validated, errors = validate_entry(self._engine_kinds, with_field(entries[index], key, value))
        if validated is None:
            for name, reason in errors.items():
                self._rejections[("engines", engine_id, name)] = reason
            self._notify(Topic.REJECTIONS)
            return False
        if validated == entries[index]:
            return True
        entries[index] = validated
        self._logger.info("设置窗口：修改引擎实例 %s 的参数 %s", engine_id, key)
        self._send_engines(entries)
        return True

    def reorder_engines(self, ids: Sequence[str]) -> bool:
        """按列表里新的顺序把优先级重新编号为 1、2、3……，再发送（架构文档 11.7 节）。返回是否发出了修改。"""
        if self._closed:
            return False
        entries = self._ordered_working()
        reordered = reorder(entries, ids)
        if reordered is None:
            self._notify(Topic.ENGINES)  # 视图的顺序与配置对不上：让视图回到配置里的顺序
            return False
        if reordered == entries:
            return False
        self._logger.info("设置窗口：调整引擎优先级")
        self._send_engines(reordered)
        return True

    def add_engine(
        self, kind: str, values: Mapping[str, JsonValue], api_key: str | None = None
    ) -> dict[str, str]:
        """添加一个实例，放在列表最后。返回 {字段名: 说明}；为空表示已发出。

        实例号由名称生成；需要 API key 的类别同时分配 keyring 条目名（与实例号相同）。
        api_key 不为空时先发 UpdateSecret，再发 UpdateConfig。
        """
        entries = self._ordered_working()
        taken = {str(entry.get("id")) for entry in entries}
        entry, errors = new_entry(
            self._engine_kinds, kind, values, taken_ids=taken, priority=len(entries) + 1
        )
        if entry is None:
            return errors
        engine_id = str(entry["id"])
        secret: str | None = None
        if kind not in self._keyless_kinds:
            secret = secret_name_for(engine_id, used_secret_names(entries))
            entry["secret"] = secret
        entries.append(entry)
        self.pending_select = engine_id
        if api_key and secret is not None:
            self._keys_set.add(engine_id)
            self._logger.info("设置窗口：为新实例 %s 提交 API key", engine_id)
            self._bus.send(UpdateSecret(secret, api_key))
        self._logger.info("设置窗口：添加引擎实例 %s（%s）", engine_id, kind)
        self._send_engines(entries)
        return {}

    def remove_engine(self, engine_id: str) -> bool:
        """删除一个实例，剩下的重新编号；它独占的 API key 一并删除。返回是否发出了修改。"""
        if self._closed:
            return False
        entries = self._ordered_working()
        removed = next((entry for entry in entries if entry.get("id") == engine_id), None)
        if removed is None:
            return False
        remaining = renumber(entry for entry in entries if entry is not removed)
        secret = removed.get("secret")
        self._logger.info("设置窗口：删除引擎实例 %s", engine_id)
        self._send_engines(remaining)
        if isinstance(secret, str) and secret and secret not in used_secret_names(remaining):
            self._bus.send(UpdateSecret(secret, None))
        for rejected in [loc for loc in self._rejections if loc[:2] == ("engines", engine_id)]:
            del self._rejections[rejected]
        self._connections.pop(engine_id, None)
        self._keys_set.discard(engine_id)
        self._keys_cleared.discard(engine_id)
        self._notify(Topic.REJECTIONS)
        return True

    # ------------------------------------------------------------------ API key

    def key_state(self, engine_id: str) -> KeyState:
        """API key 的状态：已设置、未设置、无法判断或不需要。

        依据是引擎模块发布的状态：启用的实例缺少 key 时处于“出错”状态，错误码是 engine.auth。
        没有启用的实例不读取 key，只能按本次运行中的操作判断。
        """
        entry = self.engine(engine_id)
        if entry is None or entry.kind in self._keyless_kinds:
            return KeyState.NOT_NEEDED
        if entry.secret is None or engine_id in self._keys_cleared:
            return KeyState.UNSET
        if self.engine_error_code(engine_id) is ErrorCode.AUTH:
            return KeyState.UNSET
        if self.engine_health(engine_id) is EngineHealth.DISABLED:
            return KeyState.SET if engine_id in self._keys_set else KeyState.UNKNOWN
        return KeyState.SET

    def key_optional(self, engine_id: str) -> bool:
        """这个实例的 API key 是可选的（本地大语言模型服务）。"""
        entry = self.engine(engine_id)
        return entry is not None and entry.provider in OPTIONAL_KEY_PROVIDERS

    def set_api_key(self, engine_id: str, key: str) -> bool:
        """提交一个实例的 API key。key 只经由 UpdateSecret 发送，不写日志，不进配置。返回是否发出。"""
        if self._closed or not key.strip():
            return False
        entry = self.engine(engine_id)
        if entry is None or entry.kind in self._keyless_kinds:
            return False
        self.clear_rejection(("engines", engine_id, API_KEY_FIELD))
        name = entry.secret
        if name is None:
            name = secret_name_for(engine_id, used_secret_names(raw_entries(self.config)))
        self._keys_set.add(engine_id)
        self._keys_cleared.discard(engine_id)
        self._logger.info("设置窗口：提交实例 %s 的 API key", engine_id)
        self._bus.send(UpdateSecret(name, key))
        if entry.secret is None:
            self.set_engine_field(engine_id, "secret", name)
        self._notify(Topic.ENGINES)
        return True

    def clear_api_key(self, engine_id: str) -> bool:
        """删除一个实例保存的 API key。返回是否发出。"""
        if self._closed:
            return False
        entry = self.engine(engine_id)
        if entry is None or entry.secret is None:
            return False
        self.clear_rejection(("engines", engine_id, API_KEY_FIELD))
        self._keys_cleared.add(engine_id)
        self._keys_set.discard(engine_id)
        self._logger.info("设置窗口：删除实例 %s 的 API key", engine_id)
        self._bus.send(UpdateSecret(entry.secret, None))
        self._notify(Topic.ENGINES)
        return True

    # ------------------------------------------------------------------ 连接测试

    def connection(self, engine_id: str) -> ConnectionResult | None:
        return self._connections.get(engine_id)

    def check_connection(self, engine_id: str) -> None:
        """测试一个实例的连接。结果由 on_connection_checked 送回。"""
        if self._closed:
            return
        self._connections[engine_id] = ConnectionResult(CheckState.CHECKING)
        self._logger.info("设置窗口：测试实例 %s 的连接", engine_id)
        self._bus.send(CheckEngineConnection(EngineId(engine_id)))
        self._notify(Topic.CONNECTION)

    def on_connection_checked(self, event: EngineConnectionChecked) -> None:
        if self._closed:
            return
        state = CheckState.OK if event.ok else CheckState.FAILED
        self._connections[event.engine_id] = ConnectionResult(state, event.error_code, event.latency_ms)
        self._notify(Topic.CONNECTION)

    # ------------------------------------------------------------------ 统计与缓存

    def on_stats(self, snapshot: StatsSnapshot) -> None:
        if self._closed:
            return
        self.stats, self.stats_loaded = snapshot.engines, True
        self._notify(Topic.STATS)

    def reset_stats(self) -> None:
        if not self._closed:
            self._logger.info("设置窗口：重置竞速统计")
            self._bus.send(ResetStats())

    def clear_cache(self) -> None:
        if not self._closed:
            self._logger.info("设置窗口：清空译文缓存")
            self._bus.send(ClearCache())

    def on_cache_cleared(self, event: CacheCleared) -> None:
        if self._closed:
            return
        self.last_cache_cleared = event.removed
        self._notify(Topic.CACHE)

    # ------------------------------------------------------------------ 模型

    def on_catalog(self, catalog: ModelCatalog) -> None:
        if self._closed:
            return
        self.catalog = catalog
        self._notify(Topic.MODELS)

    def models_page_opened(self) -> None:
        """第一次打开模型页时获取一次模型索引（需要联网，只在用户打开模型相关的界面时发生，需求 PRV-01）。"""
        if self._closed or self._models_requested:
            return
        self._models_requested = True
        if self.catalog is None or not (self.catalog.index_loaded or self.catalog.refreshing):
            self._bus.send(RefreshModelCatalog())

    def refresh_models(self) -> None:
        if not self._closed:
            self._models_requested = True
            self._bus.send(RefreshModelCatalog())

    def download_models(self, ids: Sequence[str]) -> None:
        if not self._closed and ids:
            self._logger.info("设置窗口：下载 %d 个本地翻译模型", len(ids))
            self._bus.send(DownloadModels(tuple(ids)))

    def cancel_download(self, model_id: str) -> None:
        if not self._closed:
            self._bus.send(CancelModelDownload(model_id))

    def delete_model(self, model_id: str) -> None:
        if not self._closed:
            self._logger.info("设置窗口：删除本地翻译模型 %s", model_id)
            self._bus.send(DeleteModel(model_id))

    # ------------------------------------------------------------------ 推理进程

    def on_workers(self, statuses: WorkerStatuses) -> None:
        if self._closed:
            return
        self.worker_statuses = statuses
        self._notify(Topic.WORKERS)

    def on_open_settings(self) -> None:
        if not self._closed:
            self._notify(Topic.OPEN)


def _priority_of(entry: Mapping[str, JsonValue]) -> int:
    value = entry.get("priority")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = [
    "API_KEY_FIELD",
    "CheckState",
    "ConnectionResult",
    "KeyState",
    "Listener",
    "Location",
    "SettingsPresenter",
    "Topic",
]
