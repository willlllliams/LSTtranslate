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
from collections.abc import Callable, Mapping, MutableMapping, Sequence
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


def _accept_all(_raw: Mapping[str, object]) -> bool:
    return True


ITEM_KEY = "id"
"""数组节条目的标识字段。写回时按它把内存中的条目与文件里的表一一对应。"""


@dataclass(frozen=True, slots=True)
class ReplaceArray:
    """把数组节 section 的内容改为 items。

    previous 是本次修改之前内存中的条目，accepts 判断文件里的一张表能否通过条目校验。
    文件里已经是表数组时以它为底稿逐条更新（规则见 ``_update_array``），否则整体写入 items。
    """

    section: str
    items: list[JsonObject]
    previous: list[JsonObject]
    accepts: Callable[[Mapping[str, object]], bool] = _accept_all


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
    if isinstance(existing, AoT):
        _update_array(root, existing, edit)
        return
    if edit.section in root:
        del root[edit.section]
    # 空数组写成 name = []，与“整节省略，使用默认条目”区分开；tomlkit 把根上的普通键值放在所有表之前。
    root[edit.section] = _array_of_tables(edit.items) if edit.items else tomlkit.array()


def _update_array(root: Container, existing: AoT, edit: ReplaceArray) -> None:
    """以文件里的表数组为底稿，逐条把它更新成 edit.items。

    文件里的每张表按以下规则处理：

    1. 校验不过的表（启动时被整条丢弃的条目）原样保留，包括其中的注释和写错的字段。
    2. 校验通过且标识第一次出现的表与同标识的条目对应。标识在 edit.items 中的，按“修改前的条目 →
       新条目”的字段差异就地改写：界面没有改动的字段保留文件里的值，运行期间手工改过的内容不被冲掉；
       修改前就没有这个条目（运行期间手工加的）时以表的现有内容为修改前的值。
       标识只在 previous 中的，说明界面删除了它，删掉这张表。
    3. 其余校验通过的表（运行期间手工加的、标识重复的）原样保留。没有标识的表视为已对应到旧条目，
       由 edit.items 中没有标识的条目重新生成。

    输出顺序：edit.items 的顺序决定对应表的先后，原样保留的表跟在文件里位于它前面的最近一张保留下来的
    对应表之后。edit.items 中找不到对应表的条目新建一张表。
    """
    previous = {key: item for item in edit.previous if (key := _key(item)) is not None}
    wanted = {key for item in edit.items if (key := _key(item)) is not None}
    matched: dict[str, Table] = {}
    # 原样保留的表，按它前面最近一张保留下来的对应表的标识分组；None 表示排在最前面。
    kept_after: dict[str | None, list[Table]] = {}
    anchor: str | None = None
    for table in existing.body:
        raw = cast(dict[str, object], table.unwrap())
        valid = _safe_accepts(edit.accepts, raw)
        key = _key(raw) if valid else None
        if valid and key is None:
            continue
        if key is not None and key not in matched:
            if key in wanted:
                matched[key] = table
                anchor = key
                continue
            if key in previous:
                continue
        kept_after.setdefault(anchor, []).append(table)

    tables: list[Table] = list(kept_after.get(None, []))
    for item in edit.items:
        key = _key(item)
        table = matched.get(key) if key is not None else None
        if table is None:
            table = _new_table(item)
        else:
            before = previous.get(key) if key is not None else None
            _patch_table(table, before if before is not None else cast(JsonObject, table.unwrap()), item)
        tables.append(table)
        if key is not None:
            tables.extend(kept_after.get(key, []))
    if not tables:
        del root[edit.section]
        root[edit.section] = tomlkit.array()
        return
    # 每张表末尾自带与下一张表之间的空行。按“已解析”的方式组装，tomlkit 不再额外插入换行。
    # 表数组在文件末尾时最后一张表不补空行。
    at_end = list(root.keys())[-1] == edit.section
    for table in tables[:-1] if at_end else tables:
        _ensure_blank_after(table)
    root[edit.section] = AoT(tables, parsed=True)


def _key(item: Mapping[str, object]) -> str | None:
    value = item.get(ITEM_KEY)
    return value if isinstance(value, str) else None


def _safe_accepts(accepts: Callable[[Mapping[str, object]], bool], raw: Mapping[str, object]) -> bool:
    try:
        return accepts(raw)
    except Exception:
        # 模式是注入的依赖，判断失败按校验不过处理，表原样保留。
        return False


def _patch_table(table: Table, before: Mapping[str, JsonValue], after: Mapping[str, JsonValue]) -> None:
    """把 table 中 before 与 after 不同的字段改成 after 的值；after 中缺失或为 None 的字段删除。"""
    for key in list(before):
        if after.get(key) is None and key in table:
            del table[key]
    for key, value in after.items():
        if value is None or before.get(key) == value:
            continue
        table[key] = to_item(value)


def _new_table(entry: Mapping[str, JsonValue]) -> Table:
    table = tomlkit.table()
    for key, value in entry.items():
        if value is not None:
            table.add(key, to_item(value))
    return table


def _ensure_blank_after(table: Table) -> None:
    """表数组后面可能紧跟别的表头，最后一张表的末尾留一个空行隔开。"""
    if not table.as_string().endswith("\n\n"):
        table.add(tomlkit.nl())


def _array_of_tables(items: Sequence[JsonObject]) -> AoT:
    """条目之间的空行由 tomlkit 自动加。"""
    array = tomlkit.aot()
    for entry in items:
        cast(list[Item], array).append(_new_table(entry))
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
