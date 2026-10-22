"""字典翻译引擎：用已安装的词典包离线逐词查词，毫秒级给出粗译。

- pack：词典包格式、key 归一化与解析；
- segment：分词、最长匹配与输出拼接；
- library：词典目录里已安装的包与读入内存的包的缓存；
- engine：DictionarySettings 与引擎实例。
"""

from lst.services.engines.dictionary.engine import (
    DictionaryEngine,
    DictionarySettings,
    create_dictionary_engine,
)

__all__ = ["DictionaryEngine", "DictionarySettings", "create_dictionary_engine"]
