"""lst.services._shared 的网络与下载工具：脱敏、传输错误分类、流式写盘、文件摘要与目录大小。"""

import asyncio
import hashlib
import io
from pathlib import Path
from typing import BinaryIO, cast

import httpx
import pytest

from lst.services._shared.aio import blocking
from lst.services._shared.download import content_length, dir_size, file_sha256, stream_to_file
from lst.services._shared.net import classify_transport_error, describe_error, safe_url, scrub


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see https://alice:s3cr3t@mirror.example/simple/x/", "see https://mirror.example/simple/x/"),
        ("http://user@host.example/a?token=1#frag", "http://host.example/a"),
        ("`https://a:p@ss@host.example/p`", "`https://host.example/p`"),
        (
            "https://host.example/a?sig=abc and ftp://u:p@files.example",
            "https://host.example/a and ftp://files.example",
        ),
        ("no url here: user@example.com", "no url here: user@example.com"),
    ],
)
def test_scrub_removes_userinfo_query_and_fragment(text: str, expected: str) -> None:
    assert scrub(text) == expected


def test_scrub_truncates() -> None:
    assert scrub("x" * 50, limit=10) == "x" * 10 + "…"


def test_safe_url_drops_userinfo_even_when_unparsable() -> None:
    assert safe_url("https://u:p@host.example:8443/a?b=1") == "https://host.example:8443/a"
    assert "p@" not in safe_url("http://u:p@[bad/x?y=1")


def test_describe_error_scrubs_message() -> None:
    error = httpx.ConnectError("cannot reach https://u:secret@mirror.example/x?sig=1")
    detail = describe_error(error)
    assert detail == "ConnectError: cannot reach https://mirror.example/x"


def test_classify_transport_error_returns_caller_codes() -> None:
    assert classify_transport_error(httpx.ReadTimeout("slow"), timeout="T", network="N") == "T"
    assert classify_transport_error(httpx.ConnectError("down"), timeout="T", network="N") == "N"


def test_content_length() -> None:
    assert content_length(httpx.Response(200, headers={"Content-Length": "12"})) == 12
    assert content_length(httpx.Response(200, headers={"Content-Length": "0"})) is None
    assert content_length(httpx.Response(200, headers={"Content-Length": "x"})) is None
    assert content_length(httpx.Response(200)) is None


async def test_stream_to_file_buffers_writes_and_hashes() -> None:
    body = bytes(range(256)) * 40

    async def chunks():
        for offset in range(0, len(body), 100):
            yield body[offset : offset + 100]
            await asyncio.sleep(0)

    sink = io.BytesIO()
    writes: list[int] = []
    original = sink.write

    def counting_write(data: bytes) -> int:
        writes.append(len(data))
        return original(data)

    sink.write = counting_write  # type: ignore[method-assign]
    seen: list[int] = []
    digest = hashlib.sha256()
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=chunks()))
        ) as client,
        client.stream("GET", "https://files.invalid/x") as response,
    ):
        received = await stream_to_file(
            response, cast(BinaryIO, sink), chunk_bytes=1000, on_progress=seen.append, digest=digest
        )

    assert received == len(body)
    assert sink.getvalue() == body
    assert digest.hexdigest() == hashlib.sha256(body).hexdigest()
    # 攒够 1000 字节写一次，最后剩下的不足一块的再写一次。
    assert all(size >= 1000 for size in writes[:-1]) and sum(writes) == len(body)
    assert seen == sorted(seen) and seen[-1] == len(body)


def test_file_sha256_and_dir_size(tmp_path: Path) -> None:
    data = b"abc" * 1000
    (tmp_path / "a").write_bytes(data)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"12345")
    assert file_sha256(tmp_path / "a") == hashlib.sha256(data).hexdigest()
    assert dir_size(tmp_path) == len(data) + 5
    assert dir_size(tmp_path / "missing") == 0
    with pytest.raises(FileNotFoundError):
        file_sha256(tmp_path / "missing")


async def test_blocking_waits_for_thread_before_propagating_cancel() -> None:
    import threading

    started, release = threading.Event(), threading.Event()
    finished: list[bool] = []

    def work() -> None:
        started.set()
        release.wait(5)
        finished.append(True)

    task = asyncio.create_task(blocking(work))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == [True]
