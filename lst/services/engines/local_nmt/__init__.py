"""本地 NMT 翻译引擎：经 WorkerChannel 端口把任务交给 NMT 进程。"""

from lst.services.engines.local_nmt.engine import LocalNmtSettings, create_local_nmt_engine

__all__ = ["LocalNmtSettings", "create_local_nmt_engine"]
