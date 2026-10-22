"""引擎注册表与 `[[engines]]` 配置节的校验模式（架构文档 4.4 节、第 9 章）。

注册表把配置里的 kind 字符串映射到该类引擎的配置模型与工厂。各类引擎的工厂签名不同：需要 key 的
网络引擎要 SecretReader 和 httpx.AsyncClient，谷歌、微软的免费接口只要 httpx.AsyncClient，
本地 NMT 要 WorkerChannel，字典翻译要词典目录。注册表把它们统一成
``build(settings, deps) -> Engine``，deps 是组合根提供的 EngineDeps。

EngineListSchema 是架构文档 4.4 节 EngineSettingsSchemas 端口的实现：配置服务用它校验
`[[engines]]` 的每一条，并保证跨条目的约束（实例号唯一、优先级两两不同）。配置服务与本模块互不
import，EngineListSchema 只在结构上满足配置服务的 ListSectionSchema 协议，由组合根交给配置服务。
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, cast

import httpx

from lst.contracts.errors import ErrorCode
from lst.contracts.ports import SecretReader, WorkerChannel
from lst.contracts.translation import Engine
from lst.contracts.worker_protocol import JsonValue
from lst.services.engines.baidu import BaiduSettings, create_baidu_engine
from lst.services.engines.common import EngineSettings
from lst.services.engines.deepl import DeepLSettings, create_deepl_engine
from lst.services.engines.dictionary import DictionarySettings, create_dictionary_engine
from lst.services.engines.google import GoogleSettings, create_google_engine
from lst.services.engines.google_free import GoogleFreeSettings, create_google_free_engine
from lst.services.engines.local_nmt import LocalNmtSettings, create_local_nmt_engine
from lst.services.engines.microsoft_free import MicrosoftFreeSettings, create_microsoft_free_engine
from lst.services.engines.openai_compat import OpenAICompatSettings, create_openai_compat_engine

# 配置服务报告问题时使用的位置与原因，位置相对于数组节，例如 (2, "id")。
type Problem = tuple[tuple[str | int, ...], str]


@dataclass(frozen=True, slots=True)
class EngineDeps:
    """创建引擎实例所需的外部依赖，由组合根提供。

    - secrets：读取 API key，实现方是 services.config；
    - client：网络引擎共用的 HTTP 客户端，生命周期由组合根管理，引擎模块不关闭它；
    - nmt：NMT 进程的通道，实现方是 app 中的监管器。为 None 时本地 NMT 实例无法创建；
    - dict_dir：词典目录（模型目录下的 dict），与模型管理模块安装词典包的目录相同。
      为 None 时字典翻译实例无法创建。
    """

    secrets: SecretReader
    client: httpx.AsyncClient
    nmt: WorkerChannel | None = None
    dict_dir: Path | None = None


class EngineUnavailableError(RuntimeError):
    """创建实例所需的依赖不存在。code 与 detail 直接用作该实例的 EngineStatus。"""

    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class EngineKindSpec:
    """一类引擎在注册表里的条目。用 ``define_kind`` 创建。

    - settings_model：该类引擎一条 `[[engines]]` 配置的 pydantic 模型；
    - build(settings, deps)：创建实例。settings 必须是 settings_model 的实例，否则抛出 TypeError；
      依赖缺失时抛出 EngineUnavailableError；
    - requires_key(settings)：该实例没有 API key 就无法工作（例如在线 LLM、DeepL）；
    - uses_nmt：实例经由 NMT 进程执行，状态跟随 NMT 进程，模型指纹变化时要重建。
    """

    kind: str
    settings_model: type[EngineSettings]
    build: Callable[[EngineSettings, EngineDeps], Engine]
    requires_key: Callable[[EngineSettings], bool]
    uses_nmt: bool = False


def define_kind[S: EngineSettings](
    kind: str,
    settings_model: type[S],
    factory: Callable[[S, EngineDeps], Engine],
    *,
    requires_key: Callable[[S], bool],
    uses_nmt: bool = False,
) -> EngineKindSpec:
    """把一类引擎的配置模型与工厂登记成注册表条目。

    工厂与 requires_key 只接受本类的配置模型；包装后的函数先检查类型，再调用它们。
    """

    def narrow(settings: EngineSettings) -> S:
        if not isinstance(settings, settings_model):
            raise TypeError(f"{kind} 引擎需要 {settings_model.__name__}，收到 {type(settings).__name__}")
        return settings

    return EngineKindSpec(
        kind=kind,
        settings_model=settings_model,
        build=lambda settings, deps: factory(narrow(settings), deps),
        requires_key=lambda settings: requires_key(narrow(settings)),
        uses_nmt=uses_nmt,
    )


def _build_local_nmt(settings: LocalNmtSettings, deps: EngineDeps) -> Engine:
    if deps.nmt is None:
        raise EngineUnavailableError(ErrorCode.WORKER_CRASHED, "没有可用的 NMT 进程，本地 NMT 实例无法创建")
    return create_local_nmt_engine(settings, deps.nmt)


def _build_dictionary(settings: DictionarySettings, deps: EngineDeps) -> Engine:
    if deps.dict_dir is None:
        raise EngineUnavailableError(ErrorCode.MODEL_MISSING, "组合根没有提供词典目录，字典翻译实例无法创建")
    return create_dictionary_engine(settings, deps.dict_dir)


ENGINE_KINDS: Final[Mapping[str, EngineKindSpec]] = MappingProxyType(
    {
        spec.kind: spec
        for spec in (
            define_kind(
                "openai_compat",
                OpenAICompatSettings,
                lambda settings, deps: create_openai_compat_engine(settings, deps.secrets, deps.client),
                requires_key=lambda settings: settings.needs_api_key,
            ),
            define_kind(
                "deepl",
                DeepLSettings,
                lambda settings, deps: create_deepl_engine(settings, deps.secrets, deps.client),
                requires_key=lambda _: True,
            ),
            define_kind(
                "google",
                GoogleSettings,
                lambda settings, deps: create_google_engine(settings, deps.secrets, deps.client),
                requires_key=lambda _: True,
            ),
            define_kind(
                "baidu",
                BaiduSettings,
                lambda settings, deps: create_baidu_engine(settings, deps.secrets, deps.client),
                requires_key=lambda _: True,
            ),
            define_kind(
                "google_free",
                GoogleFreeSettings,
                lambda settings, deps: create_google_free_engine(settings, deps.client),
                requires_key=lambda _: False,
            ),
            define_kind(
                "microsoft_free",
                MicrosoftFreeSettings,
                lambda settings, deps: create_microsoft_free_engine(settings, deps.client),
                requires_key=lambda _: False,
            ),
            define_kind(
                "local_nmt",
                LocalNmtSettings,
                _build_local_nmt,
                requires_key=lambda _: False,
                uses_nmt=True,
            ),
            define_kind(
                "dictionary",
                DictionarySettings,
                _build_dictionary,
                requires_key=lambda _: False,
            ),
        )
    }
)
"""kind → 注册表条目。新增一类引擎时，在这里加一项（需求 MNT-02）。"""


def parse_entry(
    raw: Mapping[str, Any], kinds: Mapping[str, EngineKindSpec] = ENGINE_KINDS
) -> tuple[EngineKindSpec, EngineSettings]:
    """按 kind 选出配置模型，用它校验其余字段。

    kind 缺失、不是字符串或不在注册表里时抛出 ValueError，异常信息列出可选值；其余字段不合法时抛出
    pydantic.ValidationError，错误位置相对于这一条，例如 ("priority",)。异常信息不含字段原值。
    """
    options = "、".join(sorted(kinds))
    kind = raw.get("kind")
    if kind is None:
        raise ValueError(f"缺少 kind 字段，可选值：{options}")
    if not isinstance(kind, str) or kind not in kinds:
        raise ValueError(f"kind 不是已知的引擎类别，可选值：{options}")
    spec = kinds[kind]
    fields = {key: value for key, value in raw.items() if key != "kind"}
    return spec, spec.settings_model.model_validate(fields)


def dump_entry(spec: EngineKindSpec, settings: EngineSettings) -> dict[str, JsonValue]:
    """一条配置的规范 JSON：kind 加上配置模型的全部字段（含默认值）。"""
    fields = cast(dict[str, JsonValue], settings.model_dump(mode="json"))
    return {"kind": spec.kind, **fields}


# 配置文件不存在时生成的引擎列表。需要 API key 的网络引擎默认不放。
#
# 谷歌与微软的免费接口不需要 key，默认列出，但不启用：文本只发给用户自己启用的在线引擎（需求 PRV-01），
# 用户在设置里勾选后才会联网。它们的译文好于逐词粗译，排在本地 NMT 之后、字典翻译之前；
# 两者都启用时谷歌在前。字典翻译最快、译文最粗，优先级排在最后（数值最大）：它先给出结果，
# 其他引擎的译文到达后替换它。
_DEFAULT_ENTRIES: Final[tuple[Mapping[str, JsonValue], ...]] = (
    {"kind": "local_nmt", "id": "argos", "name": "Argos", "priority": 1},
    {"kind": "google_free", "id": "google_free", "name": "谷歌翻译（免费）", "priority": 2, "enabled": False},
    {
        "kind": "microsoft_free",
        "id": "microsoft_free",
        "name": "微软翻译（免费）",
        "priority": 3,
        "enabled": False,
    },
    {"kind": "dictionary", "id": "dictionary", "name": "词典", "priority": 4},
)


class EngineListSchema:
    """`[[engines]]` 数组节的校验模式，在结构上实现配置服务的 ListSectionSchema 协议。

    - validate_item 校验一条，返回规范 JSON；
    - normalize 处理跨条目的约束：实例号唯一、优先级两两不同（架构文档 6.2 节前提 1、第 9 章）；
    - default_items 给出配置文件不存在时的默认列表。
    """

    def __init__(self, kinds: Mapping[str, EngineKindSpec] = ENGINE_KINDS) -> None:
        self._kinds = kinds

    def validate_item(self, raw: Mapping[str, Any]) -> dict[str, JsonValue]:
        """校验一条 `[[engines]]`，返回 {"kind": kind, **配置模型的规范 JSON}。

        失败时抛出 ValueError（kind 缺失或未知）或 pydantic.ValidationError（其他字段）。
        输出再次交给 validate_item 时得到相同的结果。
        """
        spec, settings = parse_entry(raw, self._kinds)
        return dump_entry(spec, settings)

    def normalize(
        self, items: list[dict[str, JsonValue]]
    ) -> tuple[list[dict[str, JsonValue]], list[Problem]]:
        """保证实例号唯一、优先级两两不同。items 必须是 validate_item 的输出，函数不修改它们。

        1. 实例号重复时保留第一条，丢弃后面的，每丢弃一条报告一次，位置为 (下标, "id")。
        2. 剩下的条目中出现重复的优先级时，按（原优先级，列表顺序）排序，把全部优先级重新编号为
           1、2、3……，只报告一次，位置为第一条与前面重复的条目的 (下标, "priority")。
           原优先级严格不同的两条保持原来的先后，优先级相同的按列表顺序排列。
        3. 优先级本来就两两不同时原样保留，数值之间可以有间隔。

        位置里的下标是条目在 items 中的下标。
        """
        problems: list[Problem] = []
        kept: list[tuple[int, dict[str, JsonValue]]] = []
        first_seen: dict[str, int] = {}
        for index, item in enumerate(items):
            engine_id = _require_str(item, "id")
            earlier = first_seen.get(engine_id)
            if earlier is not None:
                problems.append(((index, "id"), f"id 与位置 {earlier} 的条目重复，已丢弃这一条"))
                continue
            first_seen[engine_id] = index
            kept.append((index, dict(item)))

        priorities = [_require_int(item, "priority") for _, item in kept]
        conflict: int | None = None
        seen: set[int] = set()
        for (index, _), priority in zip(kept, priorities, strict=True):
            if priority in seen:
                conflict = index
                break
            seen.add(priority)
        if conflict is not None:
            order = sorted(range(len(kept)), key=lambda position: (priorities[position], position))
            for rank, position in enumerate(order, start=1):
                kept[position][1]["priority"] = rank
            count = len(kept)
            reason = f"优先级与前面的条目重复，已按原优先级与列表顺序把 {count} 个实例重新编号为 1 至 {count}"
            problems.append(((conflict, "priority"), reason))
        return [item for _, item in kept], problems

    def default_items(self) -> list[dict[str, JsonValue]]:
        """配置文件不存在时的默认引擎列表，按优先级：

        1. 本地 NMT 实例（id 为 argos），已启用；
        2. 谷歌免费翻译（id 为 google_free），未启用；
        3. 微软免费翻译（id 为 microsoft_free），未启用；
        4. 字典翻译实例（id 为 dictionary），已启用。

        配置服务只在配置文件不存在时使用它；已有配置文件的用户不会自动得到字典翻译与两个免费接口的实例，
        要在设置的翻译引擎页自己添加。
        """
        return [self.validate_item(raw) for raw in _DEFAULT_ENTRIES]


def _require_str(item: Mapping[str, JsonValue], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise ValueError(f"条目的 {key} 不是字符串，normalize 只接受 validate_item 的输出")
    return value


def _require_int(item: Mapping[str, JsonValue], key: str) -> int:
    value = item.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"条目的 {key} 不是整数，normalize 只接受 validate_item 的输出")
    return value
