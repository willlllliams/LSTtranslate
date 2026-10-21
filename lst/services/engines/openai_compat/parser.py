"""流式输出的解析：OpenAI chunk 的拆包，以及模型输出的增量解析（架构文档 2.7.1 节）。

增量解析的判据：r 数组中的一项，在它后面已经出现下一项，或者整个输出已经能被完整解析时，
才算完整。只看 t 键是否出现不够：json_object 模式不保证键的顺序，t 可能先于 i 出现，
此时 i 的数字可能还没收全（例如 12 只收到了 1）。

jiter 的 partial 模式选用 "on"：末尾未闭合的字符串整个丢弃，对象里只剩已经闭合的值；
"trailing-strings" 会把未闭合的字符串按已收到的部分返回。按上面的判据，未完成的最后一项
不会被产出，两种模式的结果相同；选 "on" 是让半截译文在解析结果里根本不出现。

partial 模式还有一个特点：值与值之间出现乱码（例如 `{"i":1}x,`）时它不报错，而是把乱码处
当成输入结束。判据对此是安全的：乱码之后的项不会出现在解析结果里，严格解析也不会成功，
这些项最终按缺段退回逐段请求。只有少数错误（例如逗号后面不是值）会让 partial 解析抛出异常。
"""

from typing import Final, cast

import jiter

from lst.contracts.errors import ErrorCode
from lst.services.engines.http import EngineError, classify_status

# 错误消息字段的最大长度，与 http.py 的约定一致。
_DETAIL_LIMIT: Final = 200

# 流中错误对象的 code / type 字段到错误码的映射，覆盖 OpenAI 与常见兼容服务的取值。
_ERROR_CODES: Final[dict[str, ErrorCode]] = {
    "invalid_api_key": ErrorCode.AUTH,
    "authentication_error": ErrorCode.AUTH,
    "permission_error": ErrorCode.AUTH,
    "insufficient_quota": ErrorCode.QUOTA,
    "rate_limit_exceeded": ErrorCode.RATE_LIMITED,
    "rate_limit_error": ErrorCode.RATE_LIMITED,
    "server_error": ErrorCode.SERVER,
    "overloaded_error": ErrorCode.SERVER,
}


class ReplyParser:
    """累积模型输出的文本，增量确认 r 数组中已经完整的项。

    每次 feed 后把整个缓冲区重新解析一遍，返回新确认的项（原样的 JSON 值，由调用方校验）。
    缓冲区里第一个 `{` 之前的内容被跳过，用来容忍 Markdown 代码块的开头标记。
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._start: int | None = None  # 第一个 `{` 的位置
        self._taken = 0  # r 数组中已经确认的项数
        self.closed = False  # 输出已经被完整解析，之后到达的内容一律忽略
        self.malformed = False  # partial 解析抛出了语法错误，之后不可能再确认任何项

    @property
    def taken(self) -> int:
        return self._taken

    def feed(self, text: str) -> list[object]:
        """追加一段输出，返回新确认完整的项。"""
        self._buf += text.encode("utf-8")
        if self.closed or self.malformed:
            return []
        body = self._body()
        if body is None:
            return []
        # 只有缓冲区以 `}` 结尾时才可能被完整解析，其余时候省掉一次严格解析。
        if text.rstrip().endswith("}"):
            whole = _parse_strict(body)
            if whole is not _INVALID:
                self.closed = True
                return self._take(whole, complete=True)
        try:
            partial = jiter.from_json(body, partial_mode="on")
        except ValueError:
            self.malformed = True
            return []
        return self._take(partial, complete=False)

    def finish(self) -> list[object]:
        """流正常结束时调用，返回最后确认的项。

        除了整个缓冲区，还尝试截到最后一个 `}` 为止再解析，用来容忍代码块的结尾标记；
        仍然无法完整解析时，最后一项可能被截断，不予确认。
        """
        if self.closed:
            return []
        body = self._body()
        if body is None:
            return []
        for candidate in (body, body[: body.rfind(b"}") + 1]):
            whole = _parse_strict(candidate)
            if whole is not _INVALID:
                self.closed = True
                return self._take(whole, complete=True)
        return []

    def _body(self) -> bytes | None:
        if self._start is None:
            index = self._buf.find(b"{")
            if index < 0:
                return None
            self._start = index
        return bytes(self._buf[self._start :])

    def _take(self, value: object, *, complete: bool) -> list[object]:
        if not isinstance(value, dict):
            return []
        items = cast(dict[str, object], value).get("r")
        if not isinstance(items, list):
            return []
        items = cast(list[object], items)
        end = len(items) if complete else len(items) - 1
        if end <= self._taken:
            return []
        fresh = items[self._taken : end]
        self._taken = end
        return fresh


_INVALID: Final = object()


def _parse_strict(data: bytes) -> object:
    """完整解析；输入不完整或不合法时返回 _INVALID。"""
    if not data:
        return _INVALID
    try:
        return cast(object, jiter.from_json(data))
    except ValueError:
        return _INVALID


def parse_chunk(data: str) -> tuple[str, str | None]:
    """拆开一个 OpenAI 流式 chunk，返回 (choices[0].delta.content, finish_reason)。

    没有 content 的 chunk（只带 role 的首个 chunk、只带 usage 的末尾 chunk）返回空字符串。
    chunk 不是合法 JSON 对象时抛出 BAD_RESPONSE；chunk 里有 error 对象时按其内容抛出 EngineError。
    """
    try:
        chunk = cast(object, jiter.from_json(data.encode("utf-8")))
    except ValueError:
        raise EngineError(ErrorCode.BAD_RESPONSE, "流式数据块不是合法的 JSON") from None
    if not isinstance(chunk, dict):
        raise EngineError(ErrorCode.BAD_RESPONSE, "流式数据块不是 JSON 对象")
    fields = cast(dict[str, object], chunk)
    error = fields.get("error")
    if error is not None:
        raise stream_error(error)
    choices = fields.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", None
    first = cast(list[object], choices)[0]
    if not isinstance(first, dict):
        return "", None
    choice = cast(dict[str, object], first)
    delta = choice.get("delta")
    content = cast(dict[str, object], delta).get("content") if isinstance(delta, dict) else None
    finish = choice.get("finish_reason")
    return (
        content if isinstance(content, str) else "",
        finish if isinstance(finish, str) else None,
    )


def stream_error(error: object) -> EngineError:
    """把流中的 error 对象转成 EngineError。

    code 是 400 以上的整数时按 HTTP 状态码分类；code 或 type 是已知字符串时按映射表分类；
    其余情况记为 BAD_RESPONSE。细节只取 message 字段，并截断长度。
    """
    code = ErrorCode.BAD_RESPONSE
    message: str | None = None
    if isinstance(error, dict):
        fields = cast(dict[str, object], error)
        value = fields.get("message")
        message = value if isinstance(value, str) else None
        for key in ("code", "type"):
            value = fields.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 400:
                code = classify_status(value)
                break
            if isinstance(value, str) and value in _ERROR_CODES:
                code = _ERROR_CODES[value]
                break
    elif isinstance(error, str):
        message = error
    detail = "流中返回错误"
    if message:
        detail += f": {message[:_DETAIL_LIMIT]}"
    return EngineError(code, detail)
