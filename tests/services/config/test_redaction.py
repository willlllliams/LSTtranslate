"""日志脱敏过滤器（需求 CFG-04，架构文档 10.2 节）。

每个用例用自己的 logger 和 StringIO handler，不改动根 logger 上的 handler。
"""

import io
import itertools
import logging
from collections.abc import Iterator

import pytest

from lst.services.config import RedactingFilter, install_redacting_filter

KEY = "sk-live-9d8c7b6a5f4e3d2c"
SHORT_KEY = "sk-live-9d8c"  # 是 KEY 的前缀


class Capture:
    """一个只挂在测试 logger 上的 handler，记录格式化后的输出和原始记录。"""

    def __init__(self, name: str, redactor: RedactingFilter) -> None:
        self.stream = io.StringIO()
        self.records: list[logging.LogRecord] = []
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.logger = logging.getLogger(name)
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        install_redacting_filter(redactor, self.logger)
        # 过滤器之后再记下记录，看到的是脱敏之后的样子。
        self.handler.addFilter(self._keep)

    def _keep(self, record: logging.LogRecord) -> bool:
        self.records.append(record)
        return True

    @property
    def output(self) -> str:
        return self.stream.getvalue()

    def close(self) -> None:
        self.logger.removeHandler(self.handler)
        self.logger.propagate = True


@pytest.fixture
def redactor() -> RedactingFilter:
    instance = RedactingFilter()
    instance.add(KEY)
    return instance


_names = itertools.count()


@pytest.fixture
def capture(redactor: RedactingFilter) -> Iterator[Capture]:
    instance = Capture(f"tests.config.redaction.{next(_names)}", redactor)
    yield instance
    instance.close()


def test_secret_in_message(capture: Capture) -> None:
    capture.logger.info(f"请求失败，key={KEY}")
    assert capture.output == "INFO 请求失败，key=***\n"


def test_secret_in_args(capture: Capture) -> None:
    capture.logger.warning("实例 %s 使用 %s", "deepseek", KEY)
    assert capture.output == "WARNING 实例 deepseek 使用 ***\n"
    record = capture.records[0]
    assert record.args is None
    assert record.msg == "实例 deepseek 使用 ***"


def test_secret_inside_non_string_arg(capture: Capture) -> None:
    capture.logger.info("请求头 %s", {"Authorization": f"Bearer {KEY}"})
    assert KEY not in capture.output
    assert "Bearer ***" in capture.output


def test_mapping_args_including_unused_keys(capture: Capture) -> None:
    capture.logger.info("实例 %(name)s", {"name": "deepseek", "key": KEY})
    assert capture.output == "INFO 实例 deepseek\n"
    args = capture.records[0].args
    assert isinstance(args, dict)
    assert args == {"name": "deepseek", "key": "***"}


def test_truncated_secret_in_args(capture: Capture) -> None:
    # 格式化后只剩密钥的前缀，整串匹配不到；字符串参数先脱敏，输出里也就没有前缀。
    capture.logger.info("前缀 %.12s", KEY)
    assert capture.output == "INFO 前缀 ***\n"


def test_exception_text(capture: Capture) -> None:
    try:
        raise RuntimeError(f"401 for key {KEY}")
    except RuntimeError:
        capture.logger.exception("请求失败")
    assert KEY not in capture.output
    assert "401 for key ***" in capture.output


def test_stack_info(redactor: RedactingFilter) -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "调用栈", None, None, sinfo=f"call({KEY!r})")
    redactor.filter(record)
    assert record.stack_info == "call('***')"


def test_unknown_strings_untouched(capture: Capture) -> None:
    capture.logger.info("普通消息 %s %d", "sk-other-value", 3)
    assert capture.output == "INFO 普通消息 sk-other-value 3\n"
    record = capture.records[0]
    assert record.msg == "普通消息 %s %d"
    assert record.args == ("sk-other-value", 3)


def test_no_secrets_registered_is_a_no_op() -> None:
    redactor = RedactingFilter()
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "消息 %s", (KEY,), None)
    assert redactor.filter(record) is True
    assert (record.msg, record.args) == ("消息 %s", (KEY,))


def test_longer_secret_wins_over_its_prefix(redactor: RedactingFilter) -> None:
    redactor.add(SHORT_KEY)
    assert redactor.redact(f"[{KEY}] [{SHORT_KEY}]") == "[***] [***]"


def test_short_values_are_ignored(redactor: RedactingFilter) -> None:
    redactor.add("abc")
    redactor.add("")
    assert redactor.redact("abc label") == "abc label"


def test_broken_format_still_redacted(capture: Capture) -> None:
    # 参数个数不对，logging 会报告格式错误；msg 和参数里的密钥都已经替换掉。
    record = logging.LogRecord("x", logging.INFO, __file__, 1, f"{KEY} %s %s", (KEY,), None)
    capture.handler.filter(record)
    assert KEY not in str(record.msg)
    assert record.args == ("***",)


def test_install_on_root_handlers_is_idempotent(redactor: RedactingFilter) -> None:
    root = logging.getLogger()
    handler = logging.NullHandler()
    root.addHandler(handler)
    try:
        install_redacting_filter(redactor)
        install_redacting_filter(redactor)
        assert handler.filters.count(redactor) == 1
    finally:
        root.removeHandler(handler)
