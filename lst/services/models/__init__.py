"""本地模型管理：Argos 模型索引、下载、校验与删除（需求 MOD-01、MOD-04、INS-03、INS-07，架构文档 8.5 节）。

- argos：模型包的读取与校验、模型目录的磁盘操作；
- index：Argos 模型索引的解析；
- net：超时、错误分类与日志脱敏；
- module：ModelsModule，实现 kernel 的 Module 协议。

``default_model_ids`` 定义在 lst.contracts.models（界面模块也要用它，而界面不能依赖 services），这里一并导出。
"""

from lst.contracts.models import default_model_ids
from lst.services.models.dictionary import DEFAULT_DICT_INDEX_URL
from lst.services.models.index import DEFAULT_INDEX_URL, IndexEntry, parse_index
from lst.services.models.module import ModelsModule

__all__ = [
    "DEFAULT_DICT_INDEX_URL",
    "DEFAULT_INDEX_URL",
    "IndexEntry",
    "ModelsModule",
    "default_model_ids",
    "parse_index",
]
