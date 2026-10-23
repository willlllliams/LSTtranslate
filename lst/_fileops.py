"""主进程与推理进程共用的目录发布操作，只依赖标准库。

调用者负责准备、校验目录和处理旧版本；这里仅使用原子改名，不复制、删除或改动目录内容。
Windows 的临时共享冲突采用有限重试，其他错误立即上抛，持续冲突保留原始异常。
"""

import os
import time
from pathlib import Path
from typing import Final

_RENAME_DELAYS: Final = (0.025, 0.05, 0.1, 0.2, 0.4, 0.8)
_TRANSIENT_WINDOWS_ERRORS: Final = frozenset({5, 32, 33})


def publish_directory(staging: Path, final: Path) -> None:
    """原子发布已校验目录；Windows 短暂拒绝改名时最多等待 1.575 秒。"""
    for attempt in range(len(_RENAME_DELAYS) + 1):
        try:
            os.replace(staging, final)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in _TRANSIENT_WINDOWS_ERRORS or attempt == len(
                _RENAME_DELAYS
            ):
                raise
            time.sleep(_RENAME_DELAYS[attempt])
