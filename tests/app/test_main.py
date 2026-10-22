"""程序入口 lst.__main__.run：启动、等待退出、停止的收尾。"""

from typing import cast

import pytest

from lst.__main__ import run
from lst.app.bootstrap import Application


class FakeApplication:
    def __init__(self, *, fail_start: bool) -> None:
        self.fail_start = fail_start
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")
        if self.fail_start:
            raise RuntimeError("启动失败")

    async def wait_closed(self) -> None:
        self.calls.append("wait_closed")

    async def stop(self) -> None:
        self.calls.append("stop")


async def test_start_failure_still_stops_the_application() -> None:
    """start() 中途失败时已经启动的模块和推理进程也要停止，异常照常抛给调用方。"""
    app = FakeApplication(fail_start=True)
    with pytest.raises(RuntimeError):
        await run(cast(Application, app))
    assert app.calls == ["start", "stop"]


async def test_normal_run_waits_then_stops() -> None:
    app = FakeApplication(fail_start=False)
    await run(cast(Application, app))
    assert app.calls == ["start", "wait_closed", "stop"]


async def test_mutex_is_released_after_quit_request_before_stopping() -> None:
    """收到退出请求后先释放运行互斥体再停止各模块：应用内更新的安装程序在等这个互斥体。"""
    app = FakeApplication(fail_start=False)
    await run(cast(Application, app), on_quit=lambda: app.calls.append("release"))
    assert app.calls == ["start", "wait_closed", "release", "stop"]


async def test_mutex_is_not_released_early_when_start_fails() -> None:
    """启动失败时没有退出请求，互斥体由入口在最后统一释放。"""
    app = FakeApplication(fail_start=True)
    with pytest.raises(RuntimeError):
        await run(cast(Application, app), on_quit=lambda: app.calls.append("release"))
    assert app.calls == ["start", "stop"]
