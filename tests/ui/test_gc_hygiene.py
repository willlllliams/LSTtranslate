"""测试进程的垃圾回收卫生：每个测试结束后，tests/ui/conftest.py 在主线程上把引用环里的 Qt 对象回收掉。

旧事件循环留下的 QObject 如果拖到下一个测试，可能被 qasync 执行器线程上触发的自动回收析构，
主线程随后访问已经释放的内存而崩溃。两个测试按文件里的顺序执行：第一个留下一个含 QObject 的引用环，
第二个检查它已经被回收，中间没有任何代码显式调用 gc。
"""

import asyncio
import gc
import weakref

import qasync  # pyright: ignore[reportMissingTypeStubs]
from PySide6.QtCore import QObject, QTimer

_left_behind: list[weakref.ref[QObject]] = []


class _Cycle:
    """自己引用自己的对象，只有循环垃圾回收能释放它，连带释放它持有的 QObject。"""

    def __init__(self) -> None:
        self.me = self
        self.timer = QTimer()


async def test_ui_tests_run_on_the_qasync_loop() -> None:
    assert isinstance(asyncio.get_running_loop(), qasync.QEventLoop)


def test_leave_a_qt_object_in_a_reference_cycle() -> None:
    gc.disable()  # 本测试里不让自动回收提前把它收掉；夹具拆除后 conftest 里的 gc.collect 仍然会执行
    try:
        cycle = _Cycle()
        _left_behind.append(weakref.ref(cycle.timer))
        del cycle
    finally:
        gc.enable()
    assert _left_behind[0]() is not None


def test_the_cycle_was_collected_after_the_previous_test() -> None:
    assert _left_behind, "上一个测试没有运行"
    assert _left_behind[0]() is None
