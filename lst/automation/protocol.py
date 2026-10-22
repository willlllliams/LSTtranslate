"""自动化接口的线路协议：一行一个 JSON 请求，一行一个 JSON 回复，UTF-8 编码，行尾是 ``\\n``。

请求：``{"id": 1, "method": "status", "params": {...}}``。id 是客户端自选的整数或字符串，回复原样带回；
params 可以省略，省略时等于空对象。

回复二选一：

- 成功：``{"id": 1, "result": <任意 JSON>}``；
- 失败：``{"id": 1, "error": {"code": "<错误码>", "message": "<说明>", "data": <可选>}}``。

一个连接上的请求按顺序处理，前一个回复写出之后才读下一个请求。一行最长 1 MiB，超过时服务端回复
``invalid_request`` 并断开连接。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

PROTOCOL_VERSION: Final = 1
MAX_LINE_BYTES: Final = 1 << 20

type Json = bool | int | float | str | Sequence[Json] | Mapping[str, Json] | None
"""可以写成 JSON 的值。用只读的 Sequence 与 Mapping（协变），各处构造的 dict、list 不必逐层标注成 Json。"""


class ErrorCode(StrEnum):
    """回复里的错误码。"""

    PARSE_ERROR = "parse_error"
    """请求不是合法的 JSON。"""
    INVALID_REQUEST = "invalid_request"
    """JSON 合法，但不是 {"id", "method", "params"} 的形式，或者一行太长。"""
    UNKNOWN_METHOD = "unknown_method"
    INVALID_PARAMS = "invalid_params"
    UNAVAILABLE = "unavailable"
    """方法要操作的对象现在不存在，例如没有打开的覆盖窗口、程序没有装配截屏模块。"""
    TIMEOUT = "timeout"
    """等待超时；data 里是超时那一刻的状态。"""
    REJECTED = "rejected"
    """配置服务或存储位置模块拒绝了这次修改；data 里是原因。"""
    NOT_CONFIRMED = "not_confirmed"
    """会移动文件或重启程序的操作，params 里没有 confirm: true。"""
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class Request:
    id: int | str | None
    method: str
    params: dict[str, Json]


class ProtocolError(Exception):
    """请求无法解析。id 是能读出来的请求号，读不出时为 None。"""

    def __init__(self, code: ErrorCode, message: str, request_id: int | str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


def encode(message: Mapping[str, Json]) -> bytes:
    """一条消息编码成一行：紧凑的 JSON，不转义非 ASCII 字符，末尾加换行。"""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def request_line(request_id: int | str, method: str, params: Mapping[str, Json] | None = None) -> bytes:
    return encode({"id": request_id, "method": method, "params": params or {}})


def parse_request(line: bytes) -> Request:
    """解析一行请求。格式不对时抛 ProtocolError。"""
    try:
        raw: object = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(ErrorCode.PARSE_ERROR, f"请求不是合法的 UTF-8 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "请求应当是 JSON 对象")
    data = cast(dict[str, object], raw)
    request_id = data.get("id")
    if request_id is not None and (isinstance(request_id, bool) or not isinstance(request_id, int | str)):
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "id 应当是整数或字符串")
    method = data.get("method")
    if not isinstance(method, str) or not method:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "缺少 method", request_id)
    params = data.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "params 应当是 JSON 对象", request_id)
    return Request(request_id, method, cast(dict[str, Json], params))


def result_line(request_id: int | str | None, result: Json) -> bytes:
    return encode({"id": request_id, "result": result})


def error_line(request_id: int | str | None, code: ErrorCode, message: str, data: Json = None) -> bytes:
    error: dict[str, Json] = {"code": code.value, "message": message}
    if data is not None:
        error["data"] = data
    return encode({"id": request_id, "error": error})


__all__ = [
    "MAX_LINE_BYTES",
    "PROTOCOL_VERSION",
    "ErrorCode",
    "Json",
    "ProtocolError",
    "Request",
    "encode",
    "error_line",
    "parse_request",
    "request_line",
    "result_line",
]
