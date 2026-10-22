"""集成测试的公共钩子。

这些测试在主线程上创建 Qt 窗口（托盘、设置窗口、双语窗口），测试结束后它们的 Python 包装对象常常留在
引用环里。循环垃圾回收会在下一次分配触发它的线程上执行；如果那是监管器的后台线程或线程池线程，
Qt 对象就在非主线程上析构，与主线程的 Qt 事件处理并发，可能导致访问违例。
所以每个测试的夹具全部收尾、事件循环关闭之后，在主线程上主动回收一次。
"""

import gc
from collections.abc import Iterator

import pytest


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[None]:
    del item, nextitem
    yield
    gc.collect()
