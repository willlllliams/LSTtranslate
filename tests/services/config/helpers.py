"""配置服务测试共用的节模型、假的数组节模式与启动工具。

节模型模拟真实模块的配置：热键（扁平）、竞速（嵌套对象、列表、自定义校验器）、
OCR（整节的跨字段约束）。FakeEngineSchema 模拟引擎模块的 ``[[engines]]`` 校验：按 kind 路由到
不同的模型，实例号唯一，优先级重复时按列表顺序重新编号。
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lst.contracts.config import AppConfig, ConfigRejected
from lst.contracts.messages import Command
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus, ModuleContext
from lst.services.config import ConfigService, MemorySecretStore, RedactingFilter, SecretStore

MODULE_LOGGER = "lst.config"

# 像 API key 的值：用来检查原因说明和日志里不出现原值。
SECRET_LIKE = "sk-live-7f3a9c2e5b1d4f60"


class HotkeysSettings(BaseModel):
    """全局热键。"""

    model_config = ConfigDict(extra="forbid")

    capture: str = Field(default="Alt+Q", min_length=1, description="截屏翻译")
    bilingual: str = Field(default="Alt+E", min_length=1, description="打开或关闭双语窗口")


class Marker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overlay: bool = False
    bilingual: bool = True


class RaceSettings(BaseModel):
    """竞速翻译。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["saver", "full"] = Field(default="saver", description="saver 或 full")
    debounce_ms: int = Field(default=400, ge=0, le=5000, description="双语窗口的输入防抖，毫秒")
    marker: Marker = Field(default_factory=Marker, description="是否显示译文来源")
    pair: list[str] = Field(default_factory=lambda: ["zh-Hans", "en"], min_length=2, max_length=2)
    note: str = ""
    font: str | None = Field(default=None, description="覆盖窗口的字体，不填时跟随系统")

    @field_validator("note")
    @classmethod
    def _no_api_key(cls, value: str) -> str:
        # 故意把原值写进错误消息，检查配置服务会把它从原因说明里去掉。
        if value.startswith("sk-"):
            raise ValueError(f"这里不能填 API key：{value}")
        return value


class OcrSettings(BaseModel):
    """OCR。"""

    model_config = ConfigDict(extra="forbid")

    device: Literal["cpu", "gpu"] = "gpu"
    min_box: int = Field(default=4, ge=0)
    max_box: int = Field(default=4000, ge=1)

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.min_box > self.max_box:
            raise ValueError("min_box 不能大于 max_box")
        return self


SECTIONS: dict[str, type[BaseModel]] = {
    "hotkeys": HotkeysSettings,
    "race": RaceSettings,
    "ocr": OcrSettings,
}


class LocalEngine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: Literal["local"]
    priority: int = Field(ge=1)
    timeout_s: float = Field(default=5, gt=0, le=600)


class OnlineEngine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: Literal["online"]
    priority: int = Field(ge=1)
    timeout_s: float = Field(default=60, gt=0, le=600)
    base_url: str
    secret: str | None = None


ENGINE_MODELS: dict[str, type[BaseModel]] = {"local": LocalEngine, "online": OnlineEngine}


class FakeEngineSchema:
    """``[[engines]]`` 的假模式，在结构上实现 ListSectionSchema。"""

    def validate_item(self, raw: Mapping[str, Any]) -> dict[str, JsonValue]:
        kind = raw.get("kind")
        model = ENGINE_MODELS.get(kind) if isinstance(kind, str) else None
        if model is None:
            # 故意把原值写进消息，检查配置服务会去掉它。
            raise ValueError(f"未知的引擎类型 {kind!r}")
        return cast(dict[str, JsonValue], model.model_validate(raw).model_dump(mode="json"))

    def normalize(
        self, items: list[dict[str, JsonValue]]
    ) -> tuple[list[dict[str, JsonValue]], list[tuple[tuple[str | int, ...], str]]]:
        problems: list[tuple[tuple[str | int, ...], str]] = []
        kept: list[tuple[int, dict[str, JsonValue]]] = []
        seen: set[JsonValue] = set()
        for index, item in enumerate(items):
            if item["id"] in seen:
                problems.append(((index, "id"), "实例号重复，已丢弃"))
                continue
            seen.add(item["id"])
            kept.append((index, item))
        priorities: set[JsonValue] = set()
        duplicate: int | None = None
        for index, item in kept:
            if item["priority"] in priorities and duplicate is None:
                duplicate = index
            priorities.add(item["priority"])
        if duplicate is None:
            return [item for _, item in kept], problems
        problems.append(((duplicate, "priority"), "优先级重复，已按列表顺序重新编号"))
        return [{**item, "priority": rank} for rank, (_, item) in enumerate(kept, start=1)], problems


class DefaultedEngineSchema(FakeEngineSchema):
    """带默认条目的假模式：配置文件里没有 engines 时使用一个本地实例。"""

    def default_items(self) -> list[dict[str, JsonValue]]:
        return [self.validate_item({"id": "argos", "kind": "local", "priority": 1})]


def engine(id_: str, priority: int, *, kind: str = "local", **extra: JsonValue) -> dict[str, JsonValue]:
    """一条引擎配置。online 类型缺省带上 base_url。"""
    item: dict[str, JsonValue] = {"id": id_, "kind": kind, "priority": priority}
    if kind == "online":
        item["base_url"] = "https://api.example.com"
    item.update(extra)
    return item


@dataclass
class Harness:
    """一个已启动的配置服务，以及它发布过的 AppConfig 与 ConfigRejected。"""

    bus: EventBus
    service: ConfigService
    ctx: ModuleContext
    path: Path
    store: SecretStore
    configs: list[AppConfig] = field(default_factory=lambda: list[AppConfig]())
    rejections: list[ConfigRejected] = field(default_factory=lambda: list[ConfigRejected]())

    @property
    def config(self) -> AppConfig:
        assert self.configs, "还没有发布过 AppConfig"
        return self.configs[-1]

    def section(self, name: str) -> dict[str, JsonValue]:
        value = self.config.section(name)
        assert isinstance(value, dict)
        return value

    def engines(self) -> list[JsonValue]:
        value = self.config.section("engines")
        assert isinstance(value, list)
        return value

    def locations(self) -> list[tuple[str | int, ...]]:
        return [rejection.location for rejection in self.rejections]

    async def send(self, command: Command) -> None:
        self.bus.send(command)
        await self.bus.drain()

    async def close(self) -> None:
        await self.service.stop()
        self.ctx.close()
        await self.bus.aclose()


async def launch(
    path: Path,
    *,
    store: SecretStore | None = None,
    schema: FakeEngineSchema | None = None,
    redactor: RedactingFilter | None = None,
) -> Harness:
    """在一条新的事件总线（raise_errors=True）上启动配置服务，等启动时的消息全部分发完。"""
    bus = EventBus(asyncio.get_running_loop(), raise_errors=True)
    secrets = store if store is not None else MemorySecretStore()
    service = ConfigService(
        path,
        sections=SECTIONS,
        list_sections={"engines": schema if schema is not None else FakeEngineSchema()},
        secrets=secrets,
        redactor=redactor,
    )
    ctx = ModuleContext(bus, logging.getLogger(MODULE_LOGGER))
    harness = Harness(bus, service, ctx, path, secrets)
    bus.subscribe(AppConfig, harness.configs.append)
    bus.subscribe(ConfigRejected, harness.rejections.append)
    await service.start(ctx)
    await bus.drain()
    return harness


class Launcher:
    """测试用的启动器：记下启动过的配置服务，测试结束时统一关闭。"""

    def __init__(self) -> None:
        self.started: list[Harness] = []

    async def __call__(
        self,
        path: Path,
        *,
        store: SecretStore | None = None,
        schema: FakeEngineSchema | None = None,
        redactor: RedactingFilter | None = None,
    ) -> Harness:
        harness = await launch(path, store=store, schema=schema, redactor=redactor)
        self.started.append(harness)
        return harness

    async def close_all(self) -> None:
        for harness in self.started:
            await harness.close()


def write(path: Path, text: str) -> None:
    """按原样写入 UTF-8 文本（不做换行转换）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")
