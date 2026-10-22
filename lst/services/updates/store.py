"""更新服务的持久化记录：最近一次检查的时间和已经通知过的版本。

记录保存在数据目录下的一个小 JSON 文件里，例如::

    {"last_checked": "2026-10-21T08:00:00+00:00", "last_attempt": "2026-10-21T08:00:00+00:00",
     "notified_version": "0.2.0"}

- last_checked：最近一次成功检查的时间，决定下一次自动检查何时到期；
- last_attempt：最近一次检查（无论成败）的时间，网络不通时据此限制重试频率；
- notified_version：最近一次弹出“发现新版本”通知的版本，同一版本只通知一次。

文件读不出或内容不对时按空记录处理；写入先写临时文件再用 ``os.replace`` 改名，中途断电不会留下半个文件。
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast


@dataclass(frozen=True, slots=True)
class UpdateRecord:
    last_checked: datetime | None = None
    last_attempt: datetime | None = None
    notified_version: str | None = None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # 没有时区的时间无法与当前时间比较，按无效处理。
    return parsed if parsed.tzinfo is not None else None


def load_record(path: Path, logger: logging.Logger) -> UpdateRecord:
    """读取记录。文件不存在时返回空记录；内容无效时记一条警告并返回空记录。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return UpdateRecord()
    except OSError:
        logger.warning("无法读取更新记录 %s", path, exc_info=True)
        return UpdateRecord()
    try:
        data: object = json.loads(text)
    except ValueError:
        logger.warning("更新记录 %s 不是合法的 JSON，按空记录处理", path)
        return UpdateRecord()
    if not isinstance(data, dict):
        logger.warning("更新记录 %s 不是 JSON 对象，按空记录处理", path)
        return UpdateRecord()
    items = cast(dict[str, object], data)
    notified = items.get("notified_version")
    return UpdateRecord(
        last_checked=_parse_time(items.get("last_checked")),
        last_attempt=_parse_time(items.get("last_attempt")),
        notified_version=notified if isinstance(notified, str) and notified else None,
    )


def save_record(path: Path, record: UpdateRecord) -> None:
    """写入记录。失败时抛出 OSError，由调用方记日志。"""
    data = {
        "last_checked": record.last_checked.isoformat() if record.last_checked else None,
        "last_attempt": record.last_attempt.isoformat() if record.last_attempt else None,
        "notified_version": record.notified_version,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
