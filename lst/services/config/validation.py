"""校验与回退（需求 CFG-02，架构文档 2.9.1 节）。

三个入口：

- ``validate_section``：启动时校验一个普通节，出错的字段从原始数据里删掉后重新校验，落到默认值；
- ``validate_list_section``：校验一个数组节，出错的条目整条丢弃，再交给模式处理跨条目约束；
- ``patch_section``：把一次修改按字段合并进普通节，非法的字段被拒绝，其余字段照常生效。

所有原因说明都不包含字段的原值。pydantic 内置错误的消息本来就不带输入值，自定义校验器的消息
可能带，所以消息里出现的输入字符串一律替换成 ``***``。
"""

import copy
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails

from lst.contracts.worker_protocol import JsonValue
from lst.services.config.redaction import MIN_SECRET_LENGTH, PLACEHOLDER
from lst.services.config.schema import Issue, ListSectionSchema, Location

type JsonObject = dict[str, JsonValue]
type FieldPath = tuple[str, ...]
type Leaf = tuple[FieldPath, JsonValue]


class _Missing:
    """表示“这个位置没有值”的哨兵。"""


MISSING = _Missing()


def canonical(model: type[BaseModel], data: object) -> JsonObject:
    """用模型校验 data，返回规范 JSON（``model_dump(mode="json", by_alias=True)``）。

    规范 JSON 还要能被同一个模型再解析一次，因为各模块要用自己的模型解析 AppConfig 中的那一节。
    这一步失败时抛出的 ValidationError 指向规范 JSON 中的位置；规范 JSON 与原始数据的键相同，
    回退算法可以照常按位置删除原始数据。
    """
    dumped = model.model_validate(data).model_dump(mode="json", by_alias=True)
    model.model_validate(dumped)
    return cast(JsonObject, dumped)


def format_location(location: Sequence[str | int]) -> str:
    """把位置写成 ``engines[2].priority`` 这样的形式。"""
    parts: list[str] = []
    for key in location:
        if isinstance(key, int):
            parts.append(f"[{key}]")
        else:
            parts.append(f".{key}" if parts else key)
    return "".join(parts)


def scrub(text: str, value: object) -> str:
    """把 value 里的字符串（含嵌套的字典值与列表元素）从 text 中替换成 ``***``。"""
    for secret in sorted(_string_leaves(value), key=len, reverse=True):
        text = text.replace(secret, PLACEHOLDER)
    return text


def _string_leaves(value: object) -> set[str]:
    found: set[str] = set()
    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if len(current) >= MIN_SECRET_LENGTH:
                found.add(current)
        elif isinstance(current, Mapping):
            stack.extend(cast(Mapping[object, object], current).values())
        elif isinstance(current, list | tuple):
            stack.extend(cast(Sequence[object], current))
    return found


def error_reason(error: ErrorDetails, skip: int = 0) -> str:
    """一条 pydantic 错误的原因说明。

    skip 是错误位置中已经由 ConfigRejected.location 表达的前缀长度；更深的部分写进说明，
    例如位置 ("pair",) 上报告的错误 ("pair", 1) 写成 ``[1]：Input should be a valid string``。
    """
    kind = error["type"]
    if kind == "extra_forbidden":
        text = "未知字段"
    elif kind == "missing":
        text = "缺少必填字段"
    else:
        text = scrub(error["msg"], error["input"])
    rest = error["loc"][skip:]
    return f"{format_location(rest)}：{text}" if rest else text


def _join(reasons: Sequence[str]) -> str:
    unique: list[str] = []
    for reason in reasons:
        if reason not in unique:
            unique.append(reason)
    return "；".join(unique)


def _errors(exc: ValidationError) -> list[ErrorDetails]:
    return exc.errors(include_url=False)


# ---------------------------------------------------------------------- 普通节：启动时的逐字段回退


def validate_section(model: type[BaseModel], raw: Mapping[str, object]) -> tuple[JsonObject, list[Issue]]:
    """校验一个普通节，返回规范 JSON 和回退记录（位置相对于该节）。

    算法（架构文档 2.9.1 节）：整体校验；按错误位置从原始数据删掉出错的键，嵌套对象按路径删到
    最深的那一层；再校验，直到通过。每一轮至少删掉一个键，所以循环必然结束。

    某一轮的错误都无法对应到原始数据里存在的键时（例如模型校验器检查的跨字段约束，错误位置是整节），
    改为从默认值出发，按文件中的顺序逐个字段加入，能通过校验的保留，不能的回退并报告。
    这样只有破坏约束的那个字段回退，同一节里无关的字段不受牵连。
    """
    data = copy.deepcopy(dict(raw))
    issues: list[Issue] = []
    while True:
        try:
            return canonical(model, data), issues
        except ValidationError as exc:
            if not _drop_invalid(data, _errors(exc), issues):
                break
    defaults = canonical(model, {})
    remaining = list(_leaves(cast(Mapping[str, JsonValue], data), defaults, ()))
    result, _ = _one_by_one(model, defaults, remaining, issues)
    return result, issues


def _drop_invalid(data: dict[str, object], errors: Sequence[ErrorDetails], issues: list[Issue]) -> bool:
    """从 data 中删除每个错误对应的最深的已有键，返回是否删除了任何键。"""
    blamed: dict[FieldPath, list[str]] = {}
    for error in errors:
        path = _existing_prefix(data, error["loc"])
        if path:
            blamed.setdefault(path, []).append(error_reason(error, skip=len(path)))
    for path, reasons in blamed.items():
        # 祖先路径也要删除时，后代随它一起回退，不再单独报告。
        if any(path[:depth] in blamed for depth in range(1, len(path))):
            continue
        _delete(data, path)
        issues.append((path, _join(reasons)))
    return bool(blamed)


def _existing_prefix(data: Mapping[str, object], location: Sequence[str | int]) -> FieldPath:
    """错误位置中沿字典键在 data 里真实存在的最长前缀。

    列表下标和联合类型的分支名不是字典键，走到它们就停下：列表字段整体回退。
    """
    path: list[str] = []
    node = _as_dict(data)
    for key in location:
        if not isinstance(key, str) or node is None or key not in node:
            break
        path.append(key)
        node = _as_dict(node[key])
    return tuple(path)


def _as_dict(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _delete(data: dict[str, object], path: FieldPath) -> None:
    node = data
    for key in path[:-1]:
        node = cast(dict[str, object], node[key])
    del node[path[-1]]


# ---------------------------------------------------------------------- 数组节：逐条丢弃


def validate_list_section(
    schema: ListSectionSchema, items: Sequence[object]
) -> tuple[list[JsonObject], list[Issue]]:
    """校验一个数组节的全部条目，返回规范化后的条目和问题记录（位置相对于该数组节）。

    validate_item 失败的条目整条丢弃。留下的条目交给 normalize；它报告的位置用的是留下的条目中的下标，
    这里换算回原始数组（配置文件或 UpdateConfig 中的列表）的下标，与丢弃报告的下标一致。
    normalize 的原因说明由模式自己保证不含原值，原样转发。
    """
    issues: list[Issue] = []
    kept: list[JsonObject] = []
    origin: list[int] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            issues.append(((index,), "条目应为表，已整条丢弃"))
            continue
        raw = cast(Mapping[str, object], item)
        try:
            kept.append(schema.validate_item(raw))
        except ValidationError as exc:
            for error in _errors(exc):
                reason = error_reason(error, skip=len(error["loc"]))
                issues.append(((index, *error["loc"]), f"{reason}（该条目已整条丢弃）"))
            continue
        except ValueError as exc:
            issues.append(((index,), f"{scrub(str(exc), raw)}（该条目已整条丢弃）"))
            continue
        origin.append(index)
    normalized, problems = schema.normalize(kept)
    for location, reason in problems:
        issues.append((_to_origin(location, origin), reason))
    return normalized, issues


def _to_origin(location: Location, origin: Sequence[int]) -> Location:
    if location and isinstance(location[0], int) and 0 <= location[0] < len(origin):
        return (origin[location[0]], *location[1:])
    return location


# ---------------------------------------------------------------------- 修改：按字段合并


@dataclass(frozen=True, slots=True)
class SectionPatch:
    """一次修改作用到一个普通节的结果。

    data：合并后的规范 JSON。
    changes：生效并且规范值有变化的字段路径与新的规范值，写回文件时只写这些字段；值为 None 表示删除该键。
    issues：被拒绝的字段及原因（位置相对于该节）。
    """

    data: JsonObject
    changes: list[Leaf]
    issues: list[Issue]


def patch_section(
    model: type[BaseModel], current: JsonObject, patch: Mapping[str, JsonValue]
) -> SectionPatch:
    """把 patch 按字段合并进 current（当前的规范 JSON）并校验。

    嵌套对象递归合并：patch 中的字典落在当前值也是字典的位置时逐键合并，其余位置整体替换，
    列表也整体替换。每个被替换的位置称为一个叶子。

    1. 把全部叶子合并后整体校验。通过就全部生效，跨字段约束因此可以由同一次修改里的几个字段一起满足。
    2. 不通过时，错误位置落在某个叶子之内的，拒绝这个叶子，剩下的叶子回到第 1 步。
    3. 剩下的错误都无法对应到单个叶子时（例如整节的跨字段约束），按 patch 中的顺序逐个叶子尝试，
       能和已接受的叶子一起通过校验的生效，否则拒绝。

    每轮至少拒绝一个叶子或进入第 3 步，所以算法必然结束。
    """
    pending = list(_leaves(patch, current, ()))
    issues: list[Issue] = []
    data = current
    while pending:
        try:
            data = canonical(model, _merged(current, pending))
            break
        except ValidationError as exc:
            blamed = _blame(pending, _errors(exc))
        if not blamed:
            data, pending = _one_by_one(model, current, pending, issues)
            break
        for index, reasons in sorted(blamed.items()):
            issues.append((pending[index][0], _join(reasons)))
        pending = [leaf for index, leaf in enumerate(pending) if index not in blamed]
    changes: list[Leaf] = []
    for path, _ in pending:
        new = lookup(data, path)
        old = lookup(current, path)
        if isinstance(new, _Missing):
            continue
        if isinstance(old, _Missing) or new != old or type(new) is not type(old):
            changes.append((path, new))
    return SectionPatch(data, changes, issues)


def _leaves(
    patch: Mapping[str, JsonValue], current: Mapping[str, JsonValue] | None, prefix: FieldPath
) -> Iterator[Leaf]:
    for key, value in patch.items():
        path = (*prefix, key)
        existing = current.get(key) if current is not None else None
        if isinstance(value, dict) and isinstance(existing, dict):
            yield from _leaves(value, existing, path)
        else:
            yield path, value


def _merged(base: JsonObject, leaves: Sequence[Leaf]) -> JsonObject:
    data = copy.deepcopy(base)
    for path, value in leaves:
        node = data
        for key in path[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        node[path[-1]] = copy.deepcopy(value)
    return data


def _blame(leaves: Sequence[Leaf], errors: Sequence[ErrorDetails]) -> dict[int, list[str]]:
    """把错误归到叶子上：叶子路径是错误位置的前缀时，错误出在这个叶子的值里。"""
    blamed: dict[int, list[str]] = {}
    for error in errors:
        location = error["loc"]
        for index, (path, _) in enumerate(leaves):
            if tuple(location[: len(path)]) == path:
                blamed.setdefault(index, []).append(error_reason(error, skip=len(path)))
                break
    return blamed


def _one_by_one(
    model: type[BaseModel], current: JsonObject, leaves: Sequence[Leaf], issues: list[Issue]
) -> tuple[JsonObject, list[Leaf]]:
    data = current
    accepted: list[Leaf] = []
    for leaf in leaves:
        path = leaf[0]
        try:
            data = canonical(model, _merged(data, [leaf]))
        except ValidationError as exc:
            reasons = [
                error_reason(e, skip=len(path) if tuple(e["loc"][: len(path)]) == path else 0)
                for e in _errors(exc)
            ]
            issues.append((path, _join(reasons)))
            continue
        accepted.append(leaf)
    return data, accepted


def lookup(data: JsonValue, path: FieldPath) -> JsonValue | _Missing:
    """沿字典键取值，路径不存在时返回 ``MISSING``。"""
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return MISSING
        node = node[key]
    return node
