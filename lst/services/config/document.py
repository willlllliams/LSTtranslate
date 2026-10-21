"""配置文件的生成、修改与落盘（架构文档 2.9.1、2.9.5 节）。

- 读取用标准库 tomllib，修改用 tomlkit：在用户的原文档上只改动涉及的键，注释、空行和顺序原样保留。
- 写入先写同目录下的临时文件，刷到磁盘后再 ``os.replace``，写到一半断电也不会留下半个文件。
  临时文件名由进程号和随机后缀组成，不用 tempfile 模块（它在带 Low 完整性标签的目录树里会退回当前目录）。
- 无法解析的文件改名为 ``<原名>.broken-<UTC 时间戳>`` 保留，从不覆盖或删除用户的文件。

本模块的函数都会阻塞，配置服务在线程池里调用它们。
"""

import contextlib
import itertools
import os
import tomllib
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import tomlkit
from pydantic import BaseModel
from tomlkit.items import AoT, Item, Table
from tomlkit.toml_document import TOMLDocument

from lst.contracts.worker_protocol import JsonValue

type JsonObject = dict[str, JsonValue]
type Container = MutableMapping[str, Any]


# ---------------------------------------------------------------------- 读取


def read_bytes(path: Path) -> bytes | None:
    """读取文件内容；文件不存在时返回 None，其他错误照常抛出。"""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def decode(data: bytes) -> str:
    """按 UTF-8 解码，容忍记事本等编辑器写入的 BOM。失败时抛出 UnicodeDecodeError。"""
    return data.decode("utf-8-sig")


def parse(text: str) -> dict[str, Any]:
    """用 tomllib 解析。语法错误时抛出 tomllib.TOMLDecodeError。"""
    return tomllib.loads(text)


# ---------------------------------------------------------------------- 落盘


def atomic_write(path: Path, data: bytes) -> None:
    """原子地把 data 写到 path：先写同目录的临时文件并 fsync，再 ``os.replace``。

    任何一步失败时删除临时文件并重新抛出异常，原文件保持不变。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}-{os.urandom(4).hex()}.tmp")
    try:
        with temp.open("xb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise


def preserve_broken(path: Path) -> Path:
    """把无法解析的配置文件改名为 ``<原名>.broken-<UTC 时间戳>`` 保留，返回新路径。

    同一秒内已有同名文件时依次加 ``-2``、``-3`` 后缀，不覆盖任何文件。改名失败时抛出 OSError。
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for attempt in itertools.count(1):
        suffix = f".broken-{stamp}" if attempt == 1 else f".broken-{stamp}-{attempt}"
        target = path.with_name(path.name + suffix)
        if target.exists():
            continue
        try:
            # Windows 上目标已存在时 os.rename 抛出 FileExistsError，不会覆盖。
            os.rename(path, target)
        except FileExistsError:
            continue
        return target
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------- 生成默认文件


@dataclass(frozen=True, slots=True)
class DefaultSection:
    """默认文件里的一个普通节：模型（提供注释）和它默认值的规范 JSON。"""

    model: type[BaseModel]
    data: JsonObject


def render_default(sections: Mapping[str, DefaultSection], lists: Mapping[str, list[JsonObject]]) -> str:
    """生成带简短中文注释的默认配置文件。

    节的注释取模型文档字符串的第一行，字段的行尾注释取 ``Field(description=...)`` 的第一行。
    嵌套对象写成行内表，数组节写成 ``[[name]]`` 表数组。
    """
    doc = tomlkit.document()
    doc.add(tomlkit.comment("LSTtranslate 配置文件。可以直接编辑，重启程序后生效。"))
    doc.add(tomlkit.comment("设置窗口里的修改会自动写回本文件，文件中的注释会保留。"))
    doc.add(tomlkit.comment("取值不合法的字段在启动时回退到默认值，原因记在日志里。"))
    doc.add(tomlkit.comment("API key 不写在这里：secret 字段填写的是 Windows 凭据管理器中的条目名。"))
    for name in lists:
        doc.add(tomlkit.comment(f"{name}：每个条目写成一个 [[{name}]] 表。"))
        doc.add(tomlkit.comment(f"整节省略时使用默认条目，写成 {name} = [] 表示没有条目。"))
    for name, section in sections.items():
        table = tomlkit.table()
        summary = _first_line(section.model.__doc__)
        if summary:
            table.add(tomlkit.comment(summary))
        notes = _field_notes(section.model)
        for key, value in section.data.items():
            if value is None:
                continue
            item = to_item(value)
            note = notes.get(key)
            if note:
                item.comment(note)
            table.add(key, item)
        doc.add(name, table)
    for name, items in lists.items():
        if items:
            doc.add(name, _array_of_tables(items))
    return doc.as_string()


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.strip().splitlines():
        if line.strip():
            return line.strip()
    return ""


def _field_notes(model: type[BaseModel]) -> dict[str, str]:
    notes: dict[str, str] = {}
    for name, info in model.model_fields.items():
        key = info.serialization_alias or info.alias or name
        note = _first_line(info.description)
        if note:
            notes[key] = note
    return notes


# ---------------------------------------------------------------------- 修改已有文档


@dataclass(frozen=True, slots=True)
class SetField:
    """把普通节 section 中 path 处的字段设为 value；value 为 None 时删除该键（TOML 没有空值）。"""

    section: str
    path: tuple[str, ...]
    value: JsonValue


@dataclass(frozen=True, slots=True)
class ReplaceArray:
    """用 items 整体替换数组节 section。"""

    section: str
    items: list[JsonObject]


type Edit = SetField | ReplaceArray


def apply_edits(text: str, edits: Sequence[Edit]) -> str:
    """在 text 的文档上执行修改，返回新文本。

    只改动涉及的键：替换已有的值时 tomlkit 保留该行的行尾注释，其余行原样保留。
    生成的文本再用 tomllib 解析一次，确认仍是合法的 TOML，否则抛出 TOMLDecodeError。
    """
    doc = tomlkit.parse(text)
    for edit in edits:
        if isinstance(edit, SetField):
            _set_field(doc, edit)
        else:
            _replace_array(doc, edit)
    result = doc.as_string()
    tomllib.loads(result)
    return result


def _set_field(doc: TOMLDocument, edit: SetField) -> None:
    container = _child_table(cast(Container, doc), edit.section, inline=False)
    for key in edit.path[:-1]:
        container = _child_table(container, key, inline=True)
    last = edit.path[-1]
    if edit.value is None:
        if last in container:
            del container[last]
        return
    existing = container.get(last)
    if isinstance(existing, Table | AoT):
        # 原来是 [a.b] 这样的子表或表数组：删掉后作为行内值重新加入，避免在表头位置写普通键值。
        del container[last]
    container[last] = to_item(edit.value)


def _child_table(container: Container, key: str, *, inline: bool) -> Container:
    """取 container 中 key 处的表；不存在或不是表时新建一个（原来的非表值被替换）。"""
    existing = container.get(key)
    if isinstance(existing, MutableMapping):
        return cast(Container, existing)
    if key in container:
        del container[key]
    container[key] = tomlkit.inline_table() if inline else tomlkit.table()
    return cast(Container, container[key])


def _replace_array(doc: TOMLDocument, edit: ReplaceArray) -> None:
    root = cast(Container, doc)
    existing = root.get(edit.section)
    if edit.items and isinstance(existing, AoT):
        # 原地替换，表数组在文件中的位置不变；后面可能紧跟别的表头，末尾留一个空行隔开。
        root[edit.section] = _array_of_tables(edit.items, blank_after=True)
        return
    if edit.section in root:
        del root[edit.section]
    # 空数组写成 name = []，与“整节省略，使用默认条目”区分开；tomlkit 把根上的普通键值放在所有表之前。
    root[edit.section] = _array_of_tables(edit.items) if edit.items else tomlkit.array()


def _array_of_tables(items: Sequence[JsonObject], *, blank_after: bool = False) -> AoT:
    """条目之间的空行由 tomlkit 自动加；blank_after 时在最后一个条目后再加一个空行。"""
    array = tomlkit.aot()
    for index, entry in enumerate(items):
        table = tomlkit.table()
        for key, value in entry.items():
            if value is not None:
                table.add(key, to_item(value))
        if blank_after and index == len(items) - 1:
            table.add(tomlkit.nl())
        cast(list[Item], array).append(table)
    return array


def to_item(value: JsonValue) -> Item:
    """把 JSON 值转成 tomlkit 的值。字典写成行内表，字典里的 None 省略；列表里不能有 None。"""
    if isinstance(value, dict):
        table = tomlkit.inline_table()
        for key, sub in value.items():
            if sub is not None:
                table[key] = to_item(sub)
        return table
    if isinstance(value, list):
        array = tomlkit.array()
        for sub in value:
            if sub is None:
                raise ValueError("TOML 数组不能包含空值")
            cast(list[Item], array).append(to_item(sub))
        return array
    if value is None:
        raise ValueError("TOML 没有空值")
    return tomlkit.item(value)
