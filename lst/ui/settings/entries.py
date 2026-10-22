"""翻译引擎实例的数据处理（需求 ENG-02、UI-05，架构文档 11.7 节）：读取、排序、新建与校验。

配置里的 ``[[engines]]`` 是一个数组节，``UpdateConfig`` 对它整体替换，而且配置服务会把校验失败的条目整条丢弃。
所以界面修改一个实例的参数时，先用该类引擎的 pydantic 模型校验整条记录，通过了才发送；
否则输入一个非法的接口地址就会让整个实例从配置里消失。

本模块不依赖 Qt，可以在没有 QApplication 的测试里使用。
"""

import re
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

from pydantic import BaseModel, ValidationError

from lst.contracts.config import AppConfig
from lst.contracts.worker_protocol import JsonValue
from lst.ui.i18n import tr
from lst.ui.settings.schema import describe_validation

type Entry = dict[str, JsonValue]

KEYLESS_KINDS: Final = frozenset({"local_nmt", "dictionary", "google_free", "microsoft_free"})
"""不需要 API key 的引擎类别。"""

OPTIONAL_KEY_PROVIDERS: Final = frozenset({"ollama", "llamacpp"})
"""API key 可选的 OpenAI 兼容服务（本地服务）。"""

HIDDEN_FIELDS: Final = frozenset({"id", "priority", "secret"})
"""引擎参数表单里不显示的字段：实例号与优先级由界面自己管理，keyring 条目名是技术细节。"""

COMMON_FIELDS: Final = ("name", "enabled")
ADVANCED_FIELDS: Final = ("color", "timeout_s", "max_concurrency", "max_retries")


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """新建 OpenAI 兼容引擎时，各个服务的预填内容。名称等用户可以在添加后修改。"""

    provider: str
    name: str
    base_url: str
    model: str
    timeout_s: float = 60.0


PROVIDER_PRESETS: Final = (
    ProviderPreset("deepseek", "DeepSeek", "https://api.deepseek.com", "deepseek-flash"),
    ProviderPreset("openai", "OpenAI", "https://api.openai.com/v1", "gpt-4.1-mini"),
    ProviderPreset("ollama", "Ollama", "http://127.0.0.1:11434/v1", ""),
    ProviderPreset("llamacpp", "llama.cpp", "http://127.0.0.1:8080/v1", "local"),
    ProviderPreset("generic", "", "", ""),
)

KIND_DEFAULT_NAMES: Final = {
    "openai_compat": "",
    "deepl": "DeepL",
    "google": "Google",
    "baidu": "百度翻译",
    "local_nmt": "Argos",
    "dictionary": "词典",
    "google_free": "谷歌翻译（免费）",
    "microsoft_free": "微软翻译（免费）",
}
"""新建实例时各类引擎的默认名称；openai_compat 由所选服务决定。"""


def preset_for(provider: str) -> ProviderPreset:
    return next((preset for preset in PROVIDER_PRESETS if preset.provider == provider), PROVIDER_PRESETS[-1])


# ---------------------------------------------------------------- 读取


@dataclass(frozen=True, slots=True)
class EngineEntry:
    """配置里的一个引擎实例。data 是完整的规范 JSON（含 kind），界面只读它，修改走 ``with_field``。"""

    id: str
    kind: str
    name: str
    enabled: bool
    priority: int
    color: str
    secret: str | None
    data: Mapping[str, JsonValue]

    @property
    def provider(self) -> str | None:
        value = self.data.get("provider")
        return value if isinstance(value, str) else None


def parse_entry(raw: JsonValue) -> EngineEntry | None:
    """配置里的一条记录 → EngineEntry；结构不对时返回 None（配置服务已校验，这里只是防御）。"""
    if not isinstance(raw, dict):
        return None
    engine_id, kind, name = raw.get("id"), raw.get("kind"), raw.get("name")
    priority = raw.get("priority")
    if not isinstance(engine_id, str) or not isinstance(kind, str) or not isinstance(name, str):
        return None
    if not isinstance(priority, int) or isinstance(priority, bool):
        return None
    color, secret, enabled = raw.get("color"), raw.get("secret"), raw.get("enabled")
    return EngineEntry(
        id=engine_id,
        kind=kind,
        name=name,
        enabled=enabled is not False,
        priority=priority,
        color=color if isinstance(color, str) else "",
        secret=secret if isinstance(secret, str) and secret else None,
        data=raw,
    )


def raw_entries(config: AppConfig | None) -> list[Entry]:
    """配置里 engines 节的记录，按配置文件里的顺序（深拷贝一层，可以随意修改）。"""
    section = config.section("engines") if config is not None else None
    if not isinstance(section, list):
        return []
    return [dict(item) for item in section if isinstance(item, dict)]


def engine_entries(config: AppConfig | None) -> list[EngineEntry]:
    """引擎实例，按优先级从高到低排列；优先级相同时按配置里的顺序。"""
    parsed = [entry for raw in raw_entries(config) if (entry := parse_entry(raw)) is not None]
    return [entry for _, entry in sorted(enumerate(parsed), key=lambda item: (item[1].priority, item[0]))]


# ---------------------------------------------------------------- 修改


def renumber(entries: Iterable[Entry]) -> list[Entry]:
    """按列表顺序把优先级重新编号为 1、2、3……（架构文档 11.7 节）。"""
    result: list[Entry] = []
    for rank, entry in enumerate(entries, start=1):
        updated = dict(entry)
        updated["priority"] = rank
        result.append(updated)
    return result


def reorder(entries: Sequence[Entry], ids: Sequence[str]) -> list[Entry] | None:
    """按 ids 给出的顺序排列并重新编号；ids 与现有实例不是同一组时返回 None。"""
    by_id = {str(entry.get("id")): entry for entry in entries}
    if len(ids) != len(entries) or set(ids) != set(by_id) or len(set(ids)) != len(ids):
        return None
    return renumber(by_id[engine_id] for engine_id in ids)


def validate_entry(
    models: Mapping[str, type[BaseModel]], raw: Mapping[str, JsonValue]
) -> tuple[Entry | None, dict[str, str]]:
    """用该类引擎的模型校验一整条记录。

    通过时返回 (规范的记录, {})，记录里补全了默认值，与配置服务发布的格式一致；
    不通过时返回 (None, {字段名: 说明})。kind 不在 models 里时，错误记在字段 "kind" 下。
    """
    kind = raw.get("kind")
    model = models.get(kind) if isinstance(kind, str) else None
    if not isinstance(kind, str) or model is None:
        return None, {"kind": tr("settings.validation.choice")}
    fields = {key: value for key, value in raw.items() if key != "kind"}
    try:
        validated = model.model_validate(fields)
    except ValidationError as exc:
        return None, describe_validation(exc.errors(include_url=False, include_input=False))
    dumped = cast(Entry, validated.model_dump(mode="json"))
    return {"kind": kind, **dumped}, {}


def with_field(entry: Mapping[str, JsonValue], key: str, value: JsonValue) -> Entry:
    updated = dict(entry)
    updated[key] = value
    return updated


# ---------------------------------------------------------------- 新建

_SLUG = re.compile(r"[^a-z0-9]+")


def unique_id(base: str, taken: Collection[str]) -> str:
    """由名称生成不与现有实例重复的实例号：只含小写字母、数字和连字符，重复时加 -2、-3……"""
    slug = _SLUG.sub("-", base.lower()).strip("-") or "engine"
    candidate, number = slug, 2
    while candidate in taken:
        candidate = f"{slug}-{number}"
        number += 1
    return candidate


def new_entry(
    models: Mapping[str, type[BaseModel]],
    kind: str,
    values: Mapping[str, JsonValue],
    *,
    taken_ids: Collection[str],
    priority: int,
    secret_name: str | None = None,
) -> tuple[Entry | None, dict[str, str]]:
    """新建一条记录：实例号由名称生成，优先级放在最后，其余字段取 values，缺省的取模型的默认值。

    secret_name 不为 None 时写进 secret 字段，值是 keyring 条目名；为 None 时由调用方在设置 API key 时再补。
    返回值同 ``validate_entry``。
    """
    name = values.get("name")
    base = name if isinstance(name, str) and name.strip() else kind
    engine_id = unique_id(base, taken_ids)
    raw: Entry = {"kind": kind, **values, "id": engine_id, "priority": priority}
    if secret_name is not None:
        raw["secret"] = secret_name
    return validate_entry(models, raw)


def secret_name_for(engine_id: str, taken: Collection[str]) -> str:
    """给一个没有 keyring 条目名的实例生成条目名：与实例号相同；已被别的实例占用时加后缀。"""
    return unique_id(engine_id, taken)


def used_secret_names(
    entries: Iterable[Mapping[str, JsonValue]], *, except_id: str | None = None
) -> set[str]:
    """配置里被引用的 keyring 条目名（可以排除某个实例）。"""
    names: set[str] = set()
    for entry in entries:
        if except_id is not None and entry.get("id") == except_id:
            continue
        secret = entry.get("secret")
        if isinstance(secret, str) and secret:
            names.add(secret)
    return names


# ---------------------------------------------------------------- 本地草稿


class EngineDraft:
    """尚未被配置服务确认的引擎列表。

    界面连续修改两处时（例如先拖动排序，紧接着改名称），第二次修改不能基于还没更新的 AppConfig 构造，
    否则会把第一次修改覆盖掉。草稿记下最近一次发送的列表：下一次修改在它的基础上进行，
    直到 AppConfig 里的列表与它相同（已被确认），或者收到引擎相关的 ConfigRejected，或者超过
    ``max_age_s`` 仍没有被确认（配置服务做了规范化，永远不会相等）。过期在读取草稿时和收到 AppConfig 时
    都会检查，两条路径的规则相同。
    """

    def __init__(self, *, max_age_s: float = 2.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._pending: list[Entry] | None = None
        self._sent_at = 0.0
        self._max_age_s = max_age_s
        self._clock = clock

    def _expire(self) -> None:
        if self._pending is not None and self._clock() - self._sent_at > self._max_age_s:
            self._pending = None

    @property
    def pending(self) -> bool:
        self._expire()
        return self._pending is not None

    def working(self, config: AppConfig | None) -> list[Entry]:
        """修改的基础：草稿存在且没有过期时用草稿，否则用 AppConfig 里的列表。返回的是副本。"""
        self._expire()
        source = self._pending if self._pending is not None else raw_entries(config)
        return [dict(entry) for entry in source]

    def sent(self, entries: Sequence[Entry]) -> None:
        self._pending = [dict(entry) for entry in entries]
        self._sent_at = self._clock()

    def confirm(self, config: AppConfig | None) -> None:
        """收到新的 AppConfig：与草稿相同或草稿已过期时丢弃草稿。"""
        self._expire()
        if self._pending is not None and raw_entries(config) == self._pending:
            self._pending = None

    def discard(self) -> None:
        self._pending = None


class SentEngineOrders:
    """最近几次发送的引擎列表里实例号的顺序，用来把 ConfigRejected 的下标换算成实例号。

    配置服务按到达顺序逐条处理 UpdateConfig，但 ConfigRejected 只带下标，不说明属于哪一次发送。
    换算规则取最保守的做法：

    - 还没被确认的每一次发送都可能是被拒绝的那一次，所以只有它们在这个下标上是同一个实例时才换算；
      顺序在两次发送之间变过（例如拖动排序后紧接着又改了参数）就无法确定，位置保持下标，
      由窗口底部的提示条显示，不会把原因写在另一个实例旁边；
    - AppConfig 里的顺序与某一次发送相同，说明这一次和它之前的发送都已经处理完，不再参与；
    - 超过 ``max_age_s`` 的发送也不再参与，规则与 ``EngineDraft`` 相同。
    """

    def __init__(self, *, max_age_s: float = 2.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._sent: list[tuple[float, list[str]]] = []
        self._max_age_s = max_age_s
        self._clock = clock

    def _expire(self) -> None:
        now = self._clock()
        self._sent = [(at, ids) for at, ids in self._sent if now - at <= self._max_age_s]

    def add(self, entries: Sequence[Mapping[str, JsonValue]]) -> None:
        self._expire()
        self._sent.append((self._clock(), [str(entry.get("id")) for entry in entries]))

    def confirm(self, config: AppConfig | None) -> None:
        """收到 AppConfig：与它顺序相同的那次发送及更早的发送都已处理完。"""
        self._expire()
        ids = [str(entry.get("id")) for entry in raw_entries(config)]
        for position in range(len(self._sent) - 1, -1, -1):
            if self._sent[position][1] == ids:
                del self._sent[: position + 1]
                return

    def resolve(self, index: int) -> str | None:
        """下标对应的实例号；没有待确认的发送，或各次发送在这个下标上不是同一个实例时返回 None。"""
        self._expire()
        candidates = {ids[index] if 0 <= index < len(ids) else None for _, ids in self._sent}
        if len(candidates) != 1:
            return None
        return candidates.pop()
