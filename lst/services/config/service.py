"""配置服务：读取、校验、写回配置，管理密钥（需求 CFG-02 至 CFG-04，架构文档 2.9.1、2.9.2、第 9 章）。"""

import asyncio
import copy
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ValidationError
from tomlkit.exceptions import TOMLKitError

from lst.contracts.config import AppConfig, ConfigRejected, ConfigUpdateCompleted, UpdateConfig, UpdateSecret
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus, ModuleContext
from lst.services.config import document
from lst.services.config.document import DefaultSection, Edit, ReplaceArray, SetField
from lst.services.config.redaction import RedactingFilter
from lst.services.config.schema import Issue, ListSectionSchema, Location, ProvidesDefaultItems
from lst.services.config.secret_store import SecretStore, describe_error
from lst.services.config.validation import (
    JsonObject,
    canonical,
    format_location,
    patch_section,
    validate_list_section,
    validate_section,
)

SECRET_FIELD = "secret"
"""约定：任何节里名为 secret 的字符串字段都是 keyring 条目名。"""

# 写回文件时可能出现的错误：磁盘与权限（OSError）、编码与 TOML 语法（ValueError）、tomlkit 的内部错误。
_WRITE_ERRORS = (OSError, ValueError, TOMLKitError)


class ConfigService:
    """配置服务模块，实现 kernel 的 Module 协议。

    path 是配置文件路径（例如 ``data/config/settings.toml``）。sections 是“节名 → pydantic 模型”，
    每个模型的全部字段都要有默认值；list_sections 是“节名 → 数组节模式”。两者都由组合根注入。
    secrets 是密钥存储；redactor 是日志脱敏过滤器，缺省时自建一个，组合根通过 ``redacting_filter``
    取出并装到日志 handler 上。

    启动时发布 ``AppConfig``（revision=1），之后处理 ``UpdateConfig`` 与 ``UpdateSecret``。
    两种命令在一把 asyncio 锁里依次处理，修改按到达顺序生效。
    """

    name = "config"

    def __init__(
        self,
        path: Path,
        *,
        sections: Mapping[str, type[BaseModel]],
        list_sections: Mapping[str, ListSectionSchema] | None = None,
        secrets: SecretStore,
        redactor: RedactingFilter | None = None,
    ) -> None:
        schemas = dict(list_sections or {})
        overlap = sorted(sections.keys() & schemas.keys())
        if overlap:
            raise ValueError(f"节名同时出现在普通节和数组节中：{', '.join(overlap)}")
        self._path = path
        self._models = dict(sections)
        self._schemas = schemas
        self._default_sections = {name: _default_section(name, model) for name, model in self._models.items()}
        self._default_lists = {name: _default_items(name, schema) for name, schema in self._schemas.items()}
        self._secrets = secrets
        self._redactor = redactor if redactor is not None else RedactingFilter()
        self._logger = logging.getLogger(__name__)
        self._bus: EventBus | None = None
        self._lock = asyncio.Lock()
        self._running = False
        # 当前生效的配置（规范 JSON）与配置文件的文本。文本是最近一次读到或写入的内容，
        # 写回时如果磁盘上的文件仍可解析，以磁盘上的为准（保留运行期间用户手工做的修改）。
        self._sections: dict[str, JsonObject] = {}
        self._lists: dict[str, list[JsonObject]] = {}
        self._text = ""
        self._revision = 0
        self._secrets_revision = 0
        self._checked_secret_names: set[str] = set()
        self._startup_rejections: list[ConfigRejected] = []

    @property
    def redacting_filter(self) -> RedactingFilter:
        """登记了当前全部已知密钥值的日志过滤器。"""
        return self._redactor

    @property
    def startup_rejections(self) -> tuple[ConfigRejected, ...]:
        """启动时发布过的 ConfigRejected。

        它们发布时界面模块还没有订阅（配置服务最先启动），组合根可以在界面启动后用它们补报。
        """
        return tuple(self._startup_rejections)

    # ------------------------------------------------------------------ 生命周期

    async def start(self, ctx: ModuleContext) -> None:
        self._bus = ctx.bus
        self._logger = ctx.logger
        issues, file_error = await self._load()
        await self._register_secrets()
        self._running = True
        if file_error is not None:
            self._startup_rejections.append(self._reject((), file_error, log=False))
        for location, reason in issues:
            self._startup_rejections.append(self._reject(location, reason))
        self._publish_config()
        ctx.subscribe(UpdateConfig, self._on_update_config)
        ctx.subscribe(UpdateSecret, self._on_update_secret)

    async def stop(self) -> None:
        # 等正在处理的修改写完再停止，之后到达的命令直接丢弃。
        async with self._lock:
            self._running = False

    # ------------------------------------------------------------------ 启动加载

    async def _load(self) -> tuple[list[Issue], str | None]:
        """读取并校验配置文件，返回回退记录和整个文件层面的错误说明（已记 ERROR）。"""
        try:
            data = await asyncio.to_thread(document.read_bytes, self._path)
        except OSError as exc:
            self._logger.error("无法读取配置文件 %s，本次运行使用默认配置：%s", self._path, exc)
            self._use_defaults()
            self._text = self._render_default()
            return [], f"无法读取配置文件，本次运行使用默认配置（{describe_error(exc)}）"
        if data is None:
            self._use_defaults()
            self._text = self._render_default()
            await self._write_default()
            return [], None
        try:
            text = document.decode(data)
            raw = document.parse(text)
        except ValueError as exc:
            # UnicodeDecodeError 与 TOMLDecodeError 都是 ValueError。
            return [], await self._recover_from_broken(exc)
        self._text = text
        return self._validate_all(raw), None

    async def _recover_from_broken(self, exc: ValueError) -> str:
        """配置文件无法解析：使用默认配置，坏文件改名保留，再写一份默认文件。返回给用户的说明。"""
        problem = "文件不是 UTF-8 编码" if isinstance(exc, UnicodeDecodeError) else str(exc)
        self._logger.error("配置文件 %s 无法解析，改用默认配置：%s", self._path, problem)
        self._use_defaults()
        self._text = self._render_default()
        try:
            broken = await asyncio.to_thread(document.preserve_broken, self._path)
        except OSError as rename_exc:
            # 改名失败时原文件保持原样，也不写默认文件，免得覆盖用户的内容。
            self._logger.error("无法改名保留坏的配置文件，原文件保持不变：%s", rename_exc)
            return f"配置文件无法解析，本次运行使用默认配置：{problem}"
        self._logger.warning("坏的配置文件已改名保留为 %s", broken)
        await self._write_default()
        return f"配置文件无法解析，已改名保留为 {broken.name} 并改用默认配置：{problem}"

    def _validate_all(self, raw: Mapping[str, object]) -> list[Issue]:
        issues: list[Issue] = []
        for name in raw:
            if name not in self._models and name not in self._schemas:
                issues.append(((name,), "未知的配置节，已忽略"))
        for name, model in self._models.items():
            value = raw.get(name)
            if value is None:
                self._sections[name] = copy.deepcopy(self._default_sections[name].data)
            elif not isinstance(value, Mapping):
                issues.append(((name,), f"应为表 [{name}]，该节使用默认值"))
                self._sections[name] = copy.deepcopy(self._default_sections[name].data)
            else:
                data, section_issues = validate_section(model, cast(Mapping[str, object], value))
                self._sections[name] = data
                issues.extend(((name, *location), reason) for location, reason in section_issues)
        for name, schema in self._schemas.items():
            value = raw.get(name)
            if value is None:
                self._lists[name] = copy.deepcopy(self._default_lists[name])
            elif not isinstance(value, list):
                issues.append(((name,), f"应为表数组 [[{name}]]，该节使用默认条目"))
                self._lists[name] = copy.deepcopy(self._default_lists[name])
            else:
                items, item_issues = validate_list_section(schema, cast(list[object], value))
                self._lists[name] = items
                issues.extend(((name, *location), reason) for location, reason in item_issues)
        return issues

    def _use_defaults(self) -> None:
        self._sections = {
            name: copy.deepcopy(default.data) for name, default in self._default_sections.items()
        }
        self._lists = {name: copy.deepcopy(items) for name, items in self._default_lists.items()}

    def _render_default(self) -> str:
        return document.render_default(self._default_sections, self._default_lists)

    async def _write_default(self) -> None:
        try:
            await asyncio.to_thread(document.atomic_write, self._path, self._text.encode("utf-8"))
        except OSError as exc:
            self._logger.error("无法写入默认配置文件 %s：%s", self._path, exc)
            return
        self._logger.info("已生成默认配置文件 %s", self._path)

    # ------------------------------------------------------------------ UpdateConfig

    async def _on_update_config(self, command: UpdateConfig) -> None:
        async with self._lock:
            if self._running:
                revision = self._revision
                rejected = await self._apply_patch(command.patch)
                if command.request_id is not None:
                    bus = self._require_bus()
                    config = bus.latest(AppConfig)
                    assert config is not None
                    bus.publish(
                        ConfigUpdateCompleted(
                            command.request_id, config, self._revision != revision, rejected
                        )
                    )

    async def update(self, patch: Mapping[str, JsonValue]) -> tuple[ConfigRejected, ...]:
        """直接应用一次修改，等它写进配置文件后返回被拒绝的位置（空元组表示全部生效）。

        与 UpdateConfig 走同一把锁、同一套校验与写回，同样发布 AppConfig 与 ConfigRejected。
        组合根在需要确认修改已经落盘时用它，例如移动模型目录之后写 [storage].models_dir：
        写不进去时要把模型移回原处。
        """
        async with self._lock:
            if not self._running:
                return (ConfigRejected((), "配置服务没有运行"),)
            return await self._apply_patch(patch)

    async def _apply_patch(self, patch: Mapping[str, JsonValue]) -> tuple[ConfigRejected, ...]:
        rejected: list[Issue] = []
        sections = dict(self._sections)
        lists = dict(self._lists)
        edits: list[Edit] = []
        for name, value in patch.items():
            if name in self._models:
                if not isinstance(value, dict):
                    rejected.append(((name,), "修改应为对象：字段名 → 新值"))
                    continue
                result = patch_section(self._models[name], self._sections[name], value)
                rejected.extend(((name, *location), reason) for location, reason in result.issues)
                if result.changes:
                    sections[name] = result.data
                    edits.extend(SetField(name, path, new) for path, new in result.changes)
            elif name in self._schemas:
                if not isinstance(value, list):
                    rejected.append(((name,), "修改应为条目数组，数组节整体替换"))
                    continue
                items, issues = validate_list_section(self._schemas[name], value)
                rejected.extend(((name, *location), reason) for location, reason in issues)
                if items != self._lists[name]:
                    lists[name] = items
                    edits.append(
                        ReplaceArray(name, items, self._lists[name], _item_acceptor(self._schemas[name]))
                    )
            else:
                rejected.append(((name,), "未知的配置节"))
        if edits:
            try:
                self._text = await asyncio.to_thread(self._persist, self._text, edits)
            except _WRITE_ERRORS as exc:
                self._logger.error("写入配置文件 %s 失败，本次修改未生效：%s", self._path, exc)
                reason = f"写入配置文件失败，修改未生效（{type(exc).__name__}）"
                rejected.extend((_edit_location(edit), reason) for edit in edits)
                edits = []
            else:
                self._sections = sections
                self._lists = lists
        events = tuple(self._reject(location, reason) for location, reason in rejected)
        if edits:
            await self._register_secrets()
            self._publish_config()
        return events

    def _persist(self, known_text: str, edits: Sequence[Edit]) -> str:
        """在线程池中执行：把修改写进配置文件，返回写入的文本。

        以磁盘上的当前内容为底稿，运行期间用户手工做的修改因此不会被冲掉（它们下次启动时生效）；
        文件不存在时用 known_text；磁盘上的文件无法解析时先改名保留，再用 known_text。
        """
        base = known_text
        data = document.read_bytes(self._path)
        if data is not None:
            try:
                disk_text = document.decode(data)
                document.parse(disk_text)
            except ValueError:
                broken = document.preserve_broken(self._path)
                self._logger.warning("配置文件在运行期间被改坏，已改名保留为 %s", broken)
            else:
                base = disk_text
        text = document.apply_edits(base, edits)
        document.atomic_write(self._path, text.encode("utf-8"))
        return text

    # ------------------------------------------------------------------ UpdateSecret

    async def _on_update_secret(self, command: UpdateSecret) -> None:
        async with self._lock:
            if self._running:
                await self._apply_secret(command.name, command.value)

    async def _apply_secret(self, name: str, value: str | None) -> None:
        """写入或删除一个密钥。日志和事件里只出现条目名，不出现 value。

        value 为 None、空串或只有空白时删除条目：清空输入框表示不再使用这个 key。
        """
        location: Location = ("secrets", name)
        if not name.strip():
            self._reject(location, "条目名不能为空")
            return
        delete = value is None or not value.strip()
        action = "删除" if delete else "写入"
        try:
            if value is None or delete:
                changed = await asyncio.to_thread(self._secrets.delete_secret, name)
            else:
                # 先登记再写入：存储层出错时记下的日志里也不会出现这个值。
                self._redactor.add(value)
                await asyncio.to_thread(self._secrets.set_secret, name, value)
                changed = True
        except Exception as exc:
            # 存储是注入的依赖，任何异常都在这里截住：异常若交给总线记录，消息里可能带着 value。
            detail = describe_error(exc, value)
            self._logger.error("%s密钥 %s 失败：%s", action, name, detail)
            self._reject(location, f"{action}密钥失败：{detail}", log=False)
            return
        self._checked_secret_names.add(name)
        if not changed:
            self._logger.info("密钥 %s 不存在，无需删除", name)
            return
        self._logger.info("已%s密钥 %s", action, name)
        self._secrets_revision += 1
        self._publish_config()

    # ------------------------------------------------------------------ 密钥脱敏

    async def _register_secrets(self) -> None:
        """读取配置中新出现的密钥条目，把它们的值登记到脱敏过滤器。"""
        names = sorted(_secret_names(self._all_values()) - self._checked_secret_names)
        if not names:
            return
        values = await asyncio.to_thread(self._read_secrets, names)
        for value in values:
            self._redactor.add(value)
        self._checked_secret_names.update(names)

    def _read_secrets(self, names: Sequence[str]) -> list[str]:
        values: list[str] = []
        for name in names:
            try:
                value = self._secrets.get_secret(name)
            except Exception as exc:
                self._logger.error("读取密钥 %s 失败：%s", name, describe_error(exc))
                continue
            if value is not None:
                values.append(value)
        return values

    def _all_values(self) -> list[JsonValue]:
        values: list[JsonValue] = list(self._sections.values())
        for items in self._lists.values():
            values.extend(items)
        return values

    # ------------------------------------------------------------------ 发布

    def _publish_config(self) -> None:
        self._revision += 1
        sections: dict[str, JsonValue] = {}
        for name in self._models:
            sections[name] = copy.deepcopy(self._sections[name])
        for name in self._schemas:
            items: list[JsonValue] = [copy.deepcopy(item) for item in self._lists[name]]
            sections[name] = items
        self._require_bus().publish(
            AppConfig(sections=sections, revision=self._revision, secrets_revision=self._secrets_revision)
        )

    def _reject(self, location: Location, reason: str, *, log: bool = True) -> ConfigRejected:
        if log:
            where = format_location(location) or "配置文件"
            self._logger.warning("配置项 %s 未采用：%s", where, reason)
        event = ConfigRejected(location=location, reason=reason)
        self._require_bus().publish(event)
        return event

    def _require_bus(self) -> EventBus:
        if self._bus is None:
            raise RuntimeError("配置服务尚未启动")
        return self._bus


def _default_section(name: str, model: type[BaseModel]) -> DefaultSection:
    try:
        return DefaultSection(model, canonical(model, {}))
    except ValidationError as exc:
        raise ValueError(
            f"配置节 {name} 的模型 {model.__name__} 必须每个字段都有默认值，且默认值的规范 JSON 能解析回去"
        ) from exc


def _default_items(name: str, schema: ListSectionSchema) -> list[JsonObject]:
    if not isinstance(schema, ProvidesDefaultItems):
        return []
    items, problems = schema.normalize(schema.default_items())
    if problems:
        raise ValueError(f"数组节 {name} 的默认条目没有通过跨条目约束：{problems}")
    return items


def _item_acceptor(schema: ListSectionSchema) -> Callable[[Mapping[str, object]], bool]:
    """写回数组节时判断文件里的一张表能否通过条目校验；校验不过的表原样保留在文件里。"""

    def accepts(raw: Mapping[str, object]) -> bool:
        try:
            schema.validate_item(raw)
        except (ValidationError, ValueError):
            return False
        return True

    return accepts


def _edit_location(edit: Edit) -> Location:
    if isinstance(edit, SetField):
        return (edit.section, *edit.path)
    return (edit.section,)


def _secret_names(values: Iterable[JsonValue]) -> set[str]:
    """收集名为 secret 的字符串字段的值（keyring 条目名）。"""
    names: set[str] = set()
    stack = list(values)
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, sub in value.items():
                if key == SECRET_FIELD and isinstance(sub, str) and sub:
                    names.add(sub)
                else:
                    stack.append(sub)
        elif isinstance(value, list):
            stack.extend(value)
    return names
