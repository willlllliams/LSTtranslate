"""更新服务测试用的假 GitHub、假时钟、可控的 sleep 与假启动器。

网络由 httpx.MockTransport 扮演，主机名都以保留域名 .invalid 结尾：即使哪里漏接了假传输层，
请求也不可能到达真实服务器。附件地址模仿 GitHub 的行为，先 302 重定向到带签名查询参数的地址。
"""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

RELEASES_URL = "https://api.github.invalid/repos/willlllliams/LSTtranslate/releases"
DOWNLOAD_HOST = "https://github.invalid/willlllliams/LSTtranslate/releases/download"
OBJECTS_HOST = "https://objects.invalid/release-assets"
SECRET = "X-Amz-Signature=topsecret"

START = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)


def installer_bytes(version: str, size: int = 5000) -> bytes:
    """合成的安装包内容：按版本号区分，长度为 size。"""
    seed = f"installer {version} ".encode()
    return (seed * (size // len(seed) + 1))[:size]


def sha256_line(data: bytes, name: str, *, star: bool = False) -> str:
    """sha256sum 格式的一行。"""
    return f"{hashlib.sha256(data).hexdigest()} {'*' if star else ' '}{name}\n"


def setup_name(version: str) -> str:
    return f"LSTtranslate-{version}-setup.exe"


def asset(name: str, tag: str, size: int) -> dict[str, object]:
    return {
        "name": name,
        "size": size,
        "browser_download_url": f"{DOWNLOAD_HOST}/{tag}/{name}",
    }


def release(
    tag: str,
    *,
    prerelease: bool = False,
    draft: bool = False,
    with_installer: bool = True,
    with_checksum: bool = True,
    size: int = 5000,
) -> dict[str, object]:
    """版本列表里的一项，字段与 GitHub REST API 的 release 对象相同（只取用到的字段）。"""
    version = tag.removeprefix("v")
    name = setup_name(version)
    assets: list[dict[str, object]] = []
    if with_installer:
        assets.append(asset(name, tag, size))
    if with_checksum:
        assets.append(asset(name + ".sha256", tag, 90))
    return {
        "tag_name": tag,
        "name": f"LSTtranslate {version}",
        "draft": draft,
        "prerelease": prerelease,
        "published_at": "2026-10-01T00:00:00Z",
        "body": f"notes for {version}",
        "html_url": f"https://github.invalid/willlllliams/LSTtranslate/releases/tag/{tag}",
        "assets": assets,
    }


@dataclass
class FakeGitHub:
    """假的 GitHub：版本列表、附件下载（经过一次重定向）。

    files 是附件名 → 内容；未登记的附件按 sha256sum 规则自动生成（安装包用 installer_bytes）。
    releases_status 不为 200 时版本列表返回该状态码；fail_with 给出时版本列表请求抛出该异常。
    """

    releases: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    files: dict[str, bytes] = field(default_factory=dict[str, bytes])
    releases_status: int = 200
    fail_with: Exception | None = None
    chunk_size: int = 1000
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])
    installer_gate: "Gate | None" = None

    def hold_installer(self) -> "Gate":
        """让安装包的正文发出第一块之后停住，直到测试设置 gate.release。"""
        self.installer_gate = Gate()
        return self.installer_gate

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def asset_requests(self, suffix: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path.endswith(suffix)]

    def content(self, name: str) -> bytes:
        if name in self.files:
            return self.files[name]
        if name.endswith(".sha256"):
            installer = name.removesuffix(".sha256")
            return sha256_line(self.content(installer), installer).encode()
        version = name.removeprefix("LSTtranslate-").removesuffix("-setup.exe")
        return installer_bytes(version)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith(RELEASES_URL):
            if self.fail_with is not None:
                raise self.fail_with
            if self.releases_status != 200:
                return httpx.Response(self.releases_status, json={"message": "rate limited"})
            return httpx.Response(200, json=self.releases)
        if url.startswith(DOWNLOAD_HOST):
            name = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(302, headers={"Location": f"{OBJECTS_HOST}/{name}?{SECRET}"})
        if url.startswith(OBJECTS_HOST):
            name = request.url.path.rsplit("/", 1)[-1]
            data = self.content(name)
            chunks = [data[i : i + self.chunk_size] for i in range(0, len(data), self.chunk_size)]
            gate = self.installer_gate if name.endswith("-setup.exe") else None
            return httpx.Response(
                200, headers={"Content-Length": str(len(data))}, stream=_Chunks(chunks, gate)
            )
        return httpx.Response(404)


@dataclass
class Gate:
    """拦住一次响应：发出第一块后设置 reached，等 release 被设置再继续。"""

    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: Sequence[bytes], gate: Gate | None = None) -> None:
        self._chunks = chunks
        self._gate = gate

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self._chunks):
            yield chunk
            if index == 0 and self._gate is not None:
                self._gate.reached.set()
                await self._gate.release.wait()


class FakeClock:
    """可以手动拨动的墙上时钟（带时区）。"""

    def __init__(self, start: datetime = START) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **delta: float) -> None:
        self.value += timedelta(**delta)


class FakeSleep:
    """由测试决定何时返回的 sleep：每次调用记下秒数并挂起，release 让最早的一次返回。"""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self._waiters: list[asyncio.Future[None]] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        future = asyncio.get_running_loop().create_future()
        self._waiters.append(future)
        try:
            await future
        finally:
            if future in self._waiters:
                self._waiters.remove(future)

    @property
    def pending(self) -> int:
        return len(self._waiters)

    def release(self) -> None:
        assert self._waiters, "没有正在等待的 sleep"
        self._waiters.pop(0).set_result(None)


@dataclass
class FakeLauncher:
    """记录启动安装程序的调用；error 不为 None 时抛出它。"""

    calls: list[tuple[Path, tuple[str, ...]]] = field(default_factory=list[tuple[Path, tuple[str, ...]]])
    error: Exception | None = None

    def __call__(self, installer: Path, args: Sequence[str]) -> None:
        self.calls.append((installer, tuple(args)))
        if self.error is not None:
            raise self.error
