"""下载与文件工具：响应长度、流式写盘、文件摘要和目录大小。

file_sha256 与 dir_size 只用标准库，构建脚本（scripts/build_installer.py）也直接导入它们；
httpx 只在类型标注里出现，导入本模块不需要 httpx。
"""

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final, Protocol

from lst.services._shared.aio import blocking

if TYPE_CHECKING:
    import httpx

HASH_CHUNK_BYTES: Final = 1 << 20
"""计算文件摘要时每次读入的字节数。"""


class Digest(Protocol):
    """hashlib 摘要对象中流式下载用到的部分。"""

    def update(self, data: bytes, /) -> None: ...


def content_length(response: "httpx.Response") -> int | None:
    """响应头里的 Content-Length；缺失、不是整数或不大于 0 时返回 None。"""
    value = response.headers.get("Content-Length")
    if value is None:
        return None
    try:
        length = int(value)
    except ValueError:
        return None
    return length if length > 0 else None


def open_for_write(path: Path) -> BinaryIO:
    """创建上级目录后以二进制写方式打开 path（已有内容清空）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("wb")


def _write(sink: BinaryIO, digest: Digest | None, data: bytes) -> None:
    sink.write(data)
    if digest is not None:
        digest.update(data)


async def stream_to_file(
    response: "httpx.Response",
    sink: BinaryIO,
    *,
    chunk_bytes: int,
    on_progress: Callable[[int], None] | None = None,
    digest: Digest | None = None,
) -> int:
    """把响应体流式写入 sink，返回收到的字节数。

    数据攒够 chunk_bytes 再交给线程写盘一次，减少线程切换；digest 不为 None 时同时更新摘要。
    每收到一块数据调用一次 on_progress(已收到的字节数)，节流由调用方负责。sink 由调用方关闭。
    """
    buffer = bytearray()
    async for chunk in response.aiter_bytes():
        buffer += chunk
        if len(buffer) >= chunk_bytes:
            data = bytes(buffer)
            buffer.clear()
            await blocking(_write, sink, digest, data)
        if on_progress is not None:
            on_progress(response.num_bytes_downloaded)
    if buffer:
        await blocking(_write, sink, digest, bytes(buffer))
    return response.num_bytes_downloaded


def file_sha256(path: Path) -> str:
    """整份文件的 SHA-256（十六进制小写）。文件不存在时抛出 FileNotFoundError。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def dir_size(path: Path) -> int:
    """目录下全部文件的大小合计。目录不存在或扫描过程中文件被删除时，按已扫描到的部分计算。"""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total
