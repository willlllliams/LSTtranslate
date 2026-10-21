"""主进程日志（需求 OBS-01，架构文档 2.9.4 节）。

每个进程写自己的按天滚动的日志文件：Windows 上 TimedRotatingFileHandler 滚动时要重命名文件，
多个进程共用一个文件会报 WinError 32。主进程用 QueueHandler 入队，由 QueueListener 线程落盘，
主线程不做文件读写（需求 REL-04）。脱敏过滤器挂在落盘的 handler 上（架构文档 10.2 节）。
"""

import logging
import logging.handlers
import queue
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"

# 这些第三方库的 INFO 日志会记录完整 URL（可能带查询参数），只保留 WARNING 及以上（架构文档 10.2 节）。
_QUIET_LOGGERS = ("httpx", "httpcore")


@dataclass(slots=True)
class LoggingHandle:
    """日志系统的句柄。stop() 刷新并关闭后台线程，程序退出前调用。"""

    listener: logging.handlers.QueueListener
    queue_handler: logging.handlers.QueueHandler
    file_handler: logging.Handler
    previous_levels: dict[str, int]
    """setup_logging 改动之前各 logger 的级别，键为 logger 名，根 logger 用空串。"""

    def add_filter(self, log_filter: logging.Filter) -> None:
        """给落盘的 handler 加过滤器，例如脱敏过滤器。"""
        self.file_handler.addFilter(log_filter)

    def stop(self) -> None:
        self.listener.stop()
        logging.getLogger().removeHandler(self.queue_handler)
        self.file_handler.close()
        # 还原级别：同一进程里再次配置日志（例如测试）时，不受上一次配置的影响。
        for name, previous in self.previous_levels.items():
            logging.getLogger(name or None).setLevel(previous)


def setup_logging(
    logs_dir: Path,
    *,
    process_name: str = "main",
    level: int = logging.INFO,
    retention_days: int = 14,
    extra_handlers: Iterable[logging.Handler] = (),
) -> LoggingHandle:
    """配置根 logger：记录先进入队列，由后台线程写入 logs_dir/<process_name>.log，每天午夜切换新文件。"""
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        logs_dir / f"{process_name}.log",
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    records: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    queue_handler = logging.handlers.QueueHandler(records)
    listener = logging.handlers.QueueListener(
        records, file_handler, *extra_handlers, respect_handler_level=True
    )
    root = logging.getLogger()
    previous_levels = {"": root.level} | {name: logging.getLogger(name).level for name in _QUIET_LOGGERS}
    root.setLevel(level)
    root.addHandler(queue_handler)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    listener.start()
    return LoggingHandle(listener, queue_handler, file_handler, previous_levels)
