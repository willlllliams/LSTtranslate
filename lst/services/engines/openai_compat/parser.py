"""流式输出的解析：OpenAI chunk 的拆包，以及模型输出的增量解析（架构文档 2.7.1 节）。

增量解析的判据：r 数组中的一项，在它后面已经开始下一项，或者整个输出已经闭合并能被完整解析时，
才算完整。只看 t 键是否出现不够：json_object 模式不保证键的顺序，t 可能先于 i 出现，
此时 i 的数字可能还没收全（例如 12 只收到了 1）。

ReplyParser 逐字节扫描新到的输出，只记录 JSON 的结构（容器栈、字符串边界、根对象的键），
不重复解析已经扫描过的部分：一项完整时只严格解析这一项的字节，根对象闭合时才严格解析一次全文。
总耗时与输出长度成正比。JSON 的结构字符都是 ASCII，UTF-8 多字节字符的每个字节都不小于 0x80，
所以按字节扫描不会把多字节字符误认成结构字符。

输出出现语法错误（例如逗号后面不是值、项与项之间夹着乱码）时，解析器不再确认任何项，
剩下的段按缺段退回逐段请求。
"""

from typing import Final, cast

import jiter

from lst.contracts.errors import ErrorCode
from lst.services.engines.http import DETAIL_LIMIT, EngineError, classify_status

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

_QUOTE: Final = ord('"')
_BACKSLASH: Final = ord("\\")
_OPEN_OBJECT: Final = ord("{")
_OPEN_ARRAY: Final = ord("[")
_CLOSE_OBJECT: Final = ord("}")
_CLOSE_ARRAY: Final = ord("]")
_COMMA: Final = ord(",")
_COLON: Final = ord(":")
_WHITESPACE: Final = frozenset(b" \t\r\n")
# 一个 JSON 值可能的首字符：对象、数组、字符串、数字、true、false、null。
_VALUE_START: Final = frozenset(b'{["-0123456789tfn')


class ReplyParser:
    """累积模型输出的文本，增量确认 r 数组中已经完整的项。

    feed 返回新确认的项（原样的 JSON 值，由调用方校验）。缓冲区里第一个 `{` 之前的内容被跳过，
    用来容忍 Markdown 代码块的开头标记；根对象闭合之后的内容（代码块的结尾标记）被忽略。
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._start: int | None = None  # 第一个 `{` 的位置，即根对象的开头
        self._pos = 0  # 下一个要扫描的字节
        self._stack: list[int] = []  # 未闭合的容器，元素是 `{` 或 `[`
        self._in_string = False
        self._escape = False
        self._string_start = 0
        self._expect_key = False  # 根对象里下一个字符串是键
        self._root_key = b""  # 根对象里最近一个键
        self._in_r = False  # 正处在根对象的 r 数组里（栈深为 2）
        self._item_start: int | None = None  # r 数组里当前这一项的开头
        self._finished_item: tuple[int, int] | None = None  # 已经遇到逗号、等待下一项开始的一项
        self._taken = 0  # r 数组中已经确认的项数
        self.closed = False  # 输出已经被完整解析，之后到达的内容一律忽略
        self.malformed = False  # 输出有语法错误，之后不可能再确认任何项

    @property
    def taken(self) -> int:
        return self._taken

    def feed(self, text: str) -> list[object]:
        """追加一段输出，返回新确认完整的项。"""
        self._buf += text.encode("utf-8")
        if self.closed or self.malformed:
            return []
        if self._start is None:
            index = self._buf.find(b"{")
            if index < 0:
                return []
            self._start = self._pos = index
        return self._scan()

    def finish(self) -> list[object]:
        """流正常结束时调用，返回最后确认的项。

        根对象没有闭合时，仍尝试把整个缓冲区和截到最后一个 `}` 为止的部分各严格解析一次；
        都无法完整解析时，最后一项可能被截断，不予确认。
        """
        if self.closed or self._start is None:
            return []
        body = bytes(self._buf[self._start :])
        for candidate in (body, body[: body.rfind(b"}") + 1]):
            whole = _parse_strict(candidate)
            if whole is not _INVALID:
                self.closed = True
                return self._take(whole)
        return []

    # ---- 扫描 ----

    def _scan(self) -> list[object]:
        fresh: list[object] = []
        buf = self._buf
        stack = self._stack
        for i in range(self._pos, len(buf)):
            c = buf[i]
            if self._in_string:
                if self._escape:
                    self._escape = False
                elif c == _BACKSLASH:
                    self._escape = True
                elif c == _QUOTE:
                    self._in_string = False
                    if len(stack) == 1 and self._expect_key:
                        self._root_key = bytes(buf[self._string_start + 1 : i])
                continue
            if c in _WHITESPACE:
                continue
            depth = len(stack)
            if self._in_r and depth == 2 and self._item_start is None and c != _CLOSE_ARRAY:
                # r 数组里一项开始：上一项因此完整。
                if c not in _VALUE_START or not self._confirm_finished(fresh):
                    return self._stop(fresh)
                self._item_start = i
            if c == _QUOTE:
                self._in_string = True
                self._string_start = i
            elif c in (_OPEN_OBJECT, _OPEN_ARRAY):
                if depth == 1 and c == _OPEN_ARRAY and not self._expect_key and self._root_key == b"r":
                    self._in_r = True
                if depth == 0:
                    self._expect_key = True
                stack.append(c)
            elif c in (_CLOSE_OBJECT, _CLOSE_ARRAY):
                expected = _OPEN_OBJECT if c == _CLOSE_OBJECT else _OPEN_ARRAY
                if not stack or stack.pop() != expected:
                    return self._stop(fresh)
                if depth == 2 and self._in_r:
                    # r 数组闭合。最后一项留到根对象闭合、全文严格解析之后再确认。
                    self._in_r = False
                    self._root_key = b""
                elif depth == 1:
                    return self._close(fresh, i + 1)
            elif c == _COMMA:
                if depth == 1:
                    self._expect_key = True
                elif depth == 2 and self._in_r:
                    if self._item_start is None:  # 逗号前面没有值
                        return self._stop(fresh)
                    self._finished_item = (self._item_start, i)
                    self._item_start = None
            elif c == _COLON and depth == 1:
                self._expect_key = False
        self._pos = len(buf)
        return fresh

    def _confirm_finished(self, fresh: list[object]) -> bool:
        """严格解析等待确认的一项并加入 fresh；它不是合法的 JSON 值时返回 False。"""
        span = self._finished_item
        if span is None:
            return True
        self._finished_item = None
        value = _parse_strict(bytes(self._buf[span[0] : span[1]]))
        if value is _INVALID:
            return False
        fresh.append(value)
        self._taken += 1
        return True

    def _close(self, fresh: list[object], end: int) -> list[object]:
        """根对象闭合：严格解析全文，确认剩下的项。"""
        assert self._start is not None
        whole = _parse_strict(bytes(self._buf[self._start : end]))
        if whole is _INVALID:
            return self._stop(fresh)
        self.closed = True
        return fresh + self._take(whole)

    def _stop(self, fresh: list[object]) -> list[object]:
        self.malformed = True
        return fresh

    def _take(self, value: object) -> list[object]:
        """完整解析结果中 r 数组还没确认的项。"""
        if not isinstance(value, dict):
            return []
        items = cast(dict[str, object], value).get("r")
        if not isinstance(items, list):
            return []
        items = cast(list[object], items)
        fresh = items[self._taken :]
        self._taken = max(self._taken, len(items))
        return fresh


_INVALID: Final = object()


def _parse_strict(data: bytes) -> object:
    """完整解析；输入不完整或不合法时返回 _INVALID。"""
    data = data.strip()
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
        detail += f": {message[:DETAIL_LIMIT]}"
    return EngineError(code, detail)
