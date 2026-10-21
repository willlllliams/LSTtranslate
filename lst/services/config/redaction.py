"""日志脱敏：把已知的密钥值在日志记录里替换成 ***（需求 CFG-04，架构文档 10.2 节）。

过滤器挂在 handler 上，而不是 logger 上：logger 的过滤器只作用于直接记到它上面的记录，
子 logger 传播上来的记录不经过它；handler 的过滤器对到达它的每条记录都生效。
"""

import logging
import re
import threading
from collections.abc import Mapping

PLACEHOLDER = "***"

MIN_SECRET_LENGTH = 4
"""短于这个长度的值不做替换。这种长度的值不可能是有效的 API key，替换它会把日志里的普通文字打成碎片。"""


class RedactingFilter(logging.Filter):
    """把已登记的密钥值替换成 ``***`` 的日志过滤器。可以在任何线程使用。

    处理范围：格式化后的消息（含 ``msg`` 与 ``args``）、字符串参数、异常文本和调用栈文本。
    格式化后的消息里出现密钥时，记录被改写成已经格式化好的消息（``args`` 置空），
    之后任何 handler 和 formatter 都拿不到原始参数。
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        # 替换用的正则在登记时整体重建；filter 只读这个属性一次，不需要加锁。
        self._pattern: re.Pattern[str] | None = None
        self._formatter = logging.Formatter()

    def add(self, value: str) -> None:
        """登记一个密钥值。重复登记没有效果。

        替换或删除后的旧值不注销：在途的日志仍可能带着它，多替换一个字符串没有代价。
        """
        if len(value) < MIN_SECRET_LENGTH:
            return
        with self._lock:
            if value in self._secrets:
                return
            self._secrets.add(value)
            # 长的在前：一个密钥是另一个的子串时，先整体替换长的那个。
            ordered = sorted(self._secrets, key=len, reverse=True)
            self._pattern = re.compile("|".join(re.escape(secret) for secret in ordered))

    def redact(self, text: str) -> str:
        """返回把已登记密钥替换成 ``***`` 之后的文本。"""
        pattern = self._pattern
        return text if pattern is None else pattern.sub(PLACEHOLDER, text)

    def filter(self, record: logging.LogRecord) -> bool:
        pattern = self._pattern
        if pattern is None:
            return True
        try:
            formatted: str | None = record.getMessage()
        except Exception:
            # 消息与参数对不上时 logging 自己会报告格式错误，这里仍然逐项脱敏。
            formatted = None
        if formatted is not None and pattern.search(formatted) is not None:
            record.msg = pattern.sub(PLACEHOLDER, formatted)
            record.args = None
        else:
            # 格式化结果里没有密钥，但 msg 或参数里仍可能有（例如没用到的字典参数、被精度截断的字符串）。
            if isinstance(record.msg, str):
                record.msg = pattern.sub(PLACEHOLDER, record.msg)
            record.args = self._redact_args(record.args, pattern)
        if record.exc_info is not None and record.exc_text is None:
            # 预先生成异常文本；Formatter 发现 exc_text 已有内容时直接使用它。
            record.exc_text = self._formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = pattern.sub(PLACEHOLDER, record.exc_text)
        if record.stack_info:
            record.stack_info = pattern.sub(PLACEHOLDER, record.stack_info)
        return True

    @staticmethod
    def _redact_args(
        args: tuple[object, ...] | Mapping[str, object] | None, pattern: re.Pattern[str]
    ) -> tuple[object, ...] | Mapping[str, object] | None:
        if args is None:
            return None
        if isinstance(args, Mapping):
            return {
                key: pattern.sub(PLACEHOLDER, value) if isinstance(value, str) else value
                for key, value in args.items()
            }
        return tuple(pattern.sub(PLACEHOLDER, value) if isinstance(value, str) else value for value in args)


def install_redacting_filter(redactor: RedactingFilter, logger: logging.Logger | None = None) -> None:
    """把过滤器装到 ``logger``（缺省为根 logger）当前的每个 handler 上。

    之后新增的 handler 不会自动带上它，组合根应在配置完日志之后调用。重复调用没有效果。
    """
    target = logger if logger is not None else logging.getLogger()
    for handler in target.handlers:
        handler.addFilter(redactor)
