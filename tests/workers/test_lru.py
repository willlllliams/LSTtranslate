"""常驻模型的 LRU 缓存。"""

import pytest

from lst.workers.nmt_worker.lru import LruCache


class Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.fail: set[str] = set()

    def load(self, key: str) -> str:
        if key in self.fail:
            raise RuntimeError(f"无法加载 {key}")
        self.events.append(f"load {key}")
        return key.upper()

    def unload(self, key: str, value: str) -> None:
        assert value == key.upper()
        self.events.append(f"unload {key}")


def test_loads_once_and_evicts_least_recently_used() -> None:
    rec = Recorder()
    cache = LruCache(2, rec.load, rec.unload)
    assert cache.get("a") == "A"
    assert cache.get("b") == "B"
    assert cache.get("a") == "A"  # a 变成最近使用
    assert cache.get("c") == "C"  # 淘汰 b
    assert rec.events == ["load a", "load b", "unload b", "load c"]
    assert cache.keys() == ["a", "c"]


def test_unloads_before_loading_new_model() -> None:
    """先卸载再加载：任何时刻常驻的模型数都不超过上限。"""
    rec = Recorder()
    cache = LruCache(1, rec.load, rec.unload)
    cache.get("a")
    cache.get("b")
    assert rec.events == ["load a", "unload a", "load b"]


def test_failed_load_leaves_cache_without_the_key() -> None:
    rec = Recorder()
    rec.fail.add("bad")
    cache = LruCache(3, rec.load, rec.unload)
    cache.get("a")
    with pytest.raises(RuntimeError):
        cache.get("bad")
    assert cache.keys() == ["a"]


def test_clear_unloads_everything_in_lru_order() -> None:
    rec = Recorder()
    cache = LruCache(3, rec.load, rec.unload)
    for key in ("a", "b", "c"):
        cache.get(key)
    cache.get("a")
    rec.events.clear()
    cache.clear()
    assert rec.events == ["unload b", "unload c", "unload a"]
    assert cache.keys() == []


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        LruCache[str, str](0, str.upper, lambda k, v: None)
