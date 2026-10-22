"""在事件循环里调用阻塞函数的工具。"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable


async def blocking[**P, T](fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """在线程池里执行一个同步调用。

    等待期间被取消时，先等线程里的调用结束，再把取消传出去。线程里的调用无法中断，提前返回的话，
    随后的清理（关闭、删除临时文件）会与仍在进行的读写同时发生，Windows 上还会因为文件被占用而删不掉。
    """
    future = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        await asyncio.wait({future})
        if not future.cancelled():
            future.exception()  # 取走结果，避免“异常没有被取回”的警告
        raise


async def close_quietly(stream: AsyncIterator[object]) -> bool:
    """关闭异步生成器，让它的 finally 及时执行；返回关闭时是否出错。

    普通异步迭代器没有 aclose，直接返回 False。关闭时抛出的 Exception 被吞掉（返回 True，
    由调用方决定是否记日志）；CancelledError 照常传播。
    """
    if not isinstance(stream, AsyncGenerator):
        return False
    try:
        await stream.aclose()
    except Exception:
        return True
    return False
