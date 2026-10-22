"""设置窗口测试用的构造函数与测试替身。

各节配置的 pydantic 模型取自组合根的 ``all_config_sections()``，引擎类别的模型取自引擎注册表，
与正式装配一致：界面模块不能 import services，测试可以，这里用它们构造“配置服务会发布的规范 JSON”。
"""

from collections.abc import Mapping, Sequence
from typing import Any, cast

from pydantic import BaseModel

from lst.app.bootstrap import all_config_sections
from lst.contracts.config import AppConfig
from lst.contracts.worker_protocol import JsonValue
from lst.services.engines import ENGINE_KINDS, EngineListSchema
from lst.services.updates.config import CONFIG_SECTIONS as UPDATES_SECTIONS

SCHEMA = EngineListSchema()


def all_models() -> dict[str, type[BaseModel]]:
    """全部普通配置节的模型，与组合根交给配置服务的相同。

    组合根还没有登记 [updates] 节时在这里补上，关于页的更新设置项才有模型可读。
    """
    sections = all_config_sections()
    for name, model in UPDATES_SECTIONS.items():
        sections.setdefault(name, model)
    return sections


def engine_kinds() -> dict[str, type[BaseModel]]:
    """引擎类别 → 配置模型，组合根注入 SettingsModule 的参数。"""
    return {kind: spec.settings_model for kind, spec in ENGINE_KINDS.items()}


def default_sections() -> dict[str, JsonValue]:
    """配置服务在没有配置文件时发布的各节规范 JSON。"""
    return {
        name: cast(JsonValue, model().model_dump(mode="json", by_alias=True))
        for name, model in all_models().items()
    }


def _merge(base: dict[str, JsonValue], patch: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    merged = dict(base)
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge(existing, value)
        else:
            merged[key] = value
    return merged


def engine(kind: str, engine_id: str, priority: int, **fields: Any) -> dict[str, JsonValue]:
    """一条经过校验模式的引擎配置（规范 JSON）。缺少的必填字段按类别补上合理的值。"""
    defaults: dict[str, dict[str, Any]] = {
        "local_nmt": {"name": "Argos"},
        "openai_compat": {
            "name": "DeepSeek",
            "timeout_s": 60,
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-flash",
            "secret": engine_id,
        },
        "deepl": {"name": "DeepL", "secret": engine_id},
        "google": {"name": "Google", "secret": engine_id},
        "baidu": {"name": "百度翻译", "appid": "2015063000000001", "secret": engine_id},
    }
    raw = {"kind": kind, "id": engine_id, "priority": priority, **defaults[kind], **fields}
    return SCHEMA.validate_item(raw)


def default_engines() -> list[dict[str, JsonValue]]:
    """四个引擎：本地 NMT（优先级 1）、DeepSeek（2）、DeepL（3）、Ollama（4，不需要 key）。"""
    return [
        engine("local_nmt", "argos", 1),
        engine("openai_compat", "deepseek", 2),
        engine("deepl", "deepl", 3),
        engine(
            "openai_compat",
            "ollama",
            4,
            name="Ollama",
            provider="ollama",
            base_url="http://127.0.0.1:11434/v1",
            model="hy-mt2",
            secret=None,
        ),
    ]


def default_config(
    sections: Mapping[str, Mapping[str, JsonValue]] | None = None,
    *,
    engines: Sequence[dict[str, JsonValue]] | None = None,
    revision: int = 1,
    secrets_revision: int = 0,
) -> AppConfig:
    """各节取默认值，sections 里的字段覆盖默认值（嵌套的表逐键合并）；engines 缺省时用 default_engines()。"""
    data = default_sections()
    for name, patch in (sections or {}).items():
        base = data.get(name)
        data[name] = _merge(base if isinstance(base, dict) else {}, patch)
    data["engines"] = cast(JsonValue, list(engines) if engines is not None else default_engines())
    return AppConfig(sections=data, revision=revision, secrets_revision=secrets_revision)


class FakeDialogs:
    """记录调用、直接给出答案的对话框，测试不会弹出模态窗口。"""

    def __init__(self, *, confirm: bool = True, directory: str | None = None) -> None:
        self.confirm_answer = confirm
        self.directory = directory
        self.confirms: list[tuple[str, str]] = []
        self.directories: list[tuple[str, str]] = []
        self.urls: list[str] = []

    def confirm(self, title: str, text: str) -> bool:
        self.confirms.append((title, text))
        return self.confirm_answer

    def choose_directory(self, title: str, start: str) -> str | None:
        self.directories.append((title, start))
        return self.directory

    def open_url(self, url: str) -> bool:
        self.urls.append(url)
        return True
