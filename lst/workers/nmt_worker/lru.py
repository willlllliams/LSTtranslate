"""常驻模型的最近最少使用（Least Recently Used，LRU）缓存（架构文档 8.3 节）。"""

from collections import OrderedDict
from collections.abc import Callable, Hashable


class LruCache[K: Hashable, V]:
    """最多常驻 capacity 个条目；装入新条目前，先卸载最近最少使用的条目，把内存腾出来再加载。

    load(key) 加载一个条目，抛出的异常原样传给调用方，缓存不变；unload(key, value) 在条目被淘汰或清空时调用。
    只在推理线程上使用，不加锁。
    """

    def __init__(self, capacity: int, load: Callable[[K], V], unload: Callable[[K, V], None]) -> None:
        if capacity < 1:
            raise ValueError("capacity 至少为 1")
        self._capacity = capacity
        self._load = load
        self._unload = unload
        self._items: OrderedDict[K, V] = OrderedDict()

    def get(self, key: K) -> V:
        """取出条目并标记为最近使用；不在缓存里时加载。"""
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
        while len(self._items) >= self._capacity:
            old_key, old_value = self._items.popitem(last=False)
            self._unload(old_key, old_value)
        value = self._load(key)
        self._items[key] = value
        return value

    def keys(self) -> list[K]:
        """当前常驻的键，从最近最少使用到最近使用排列。"""
        return list(self._items)

    def clear(self) -> None:
        """卸载全部条目。"""
        while self._items:
            key, value = self._items.popitem(last=False)
            self._unload(key, value)
