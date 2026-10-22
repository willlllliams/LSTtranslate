"""模型管理测试用的合成数据与假服务器。

模型包是结构与真实 Argos 包一致的小 zip，模型文件是占位内容。网络由 httpx.MockTransport 扮演，
主机名都用 .invalid 结尾的保留域名：即使哪里漏接了假传输层，请求也不可能到达真实服务器。
"""

import asyncio
import io
import json
import zipfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from lst.contracts.models import nmt_model_id

INDEX_URL = "https://index.invalid/argospm-index/index.json"
PACKAGE_HOST = "https://packages.invalid"
DICT_INDEX_URL = "https://dicts.invalid/releases/download/dicts-v1/dict-index.json"
"""词典索引的地址。词典包与它在同一个目录下：``https://dicts.invalid/releases/download/dicts-v1/<文件名>``。"""

EN_ZH = nmt_model_id("en", "zh-Hans")
ZH_EN = nmt_model_id("zh-Hans", "en")
EN_DE = nmt_model_id("en", "de")
DE_EN = nmt_model_id("de", "en")


def package_bytes(
    from_code: str,
    to_code: str,
    *,
    version: str = "1.9",
    root: str | None = None,
    sentencepiece: bool = True,
    payload: bytes = b"model",
    metadata: dict[str, object] | None = None,
) -> bytes:
    """在内存里生成一个 .argosmodel。payload 是 model/model.bin 的内容，用 ZIP_STORED 存放。"""
    name = root if root is not None else f"translate-{from_code}_{to_code}-{version.replace('.', '_')}"
    meta: dict[str, object] = {"package_version": version, "from_code": from_code, "to_code": to_code}
    if metadata is not None:
        meta = metadata
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(f"{name}/metadata.json", json.dumps(meta))
        archive.writestr(f"{name}/README.md", "test package")
        if sentencepiece:
            archive.writestr(f"{name}/sentencepiece.model", b"spm")
        archive.writestr(f"{name}/model/model.bin", payload)
        archive.writestr(f"{name}/model/config.json", "{}")
    return buffer.getvalue()


def write_package(
    directory: Path,
    from_code: str,
    to_code: str,
    *,
    version: str = "1.9",
    file_name: str | None = None,
    sentencepiece: bool = True,
) -> Path:
    """把合成的模型包写到 directory 下，文件名默认沿用 Argos 的命名，返回路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    name = file_name or f"translate-{from_code}_{to_code}-{version.replace('.', '_')}.argosmodel"
    path = directory / name
    path.write_bytes(package_bytes(from_code, to_code, version=version, sentencepiece=sentencepiece))
    return path


def package_url(from_code: str, to_code: str, version: str = "1.9", query: str = "") -> str:
    url = f"{PACKAGE_HOST}/translate-{from_code}_{to_code}-{version.replace('.', '_')}.argosmodel"
    return f"{url}?{query}" if query else url


def index_item(
    from_code: str, to_code: str, version: str = "1.9", *, url: str | None = None, **extra: object
) -> dict[str, object]:
    """索引里的一项，字段与 argospm-index 的 index.json 相同（按文档推断）。"""
    item: dict[str, object] = {
        "package_version": version,
        "argos_version": "1.9.0",
        "from_code": from_code,
        "from_name": from_code,
        "to_code": to_code,
        "to_name": to_code,
        "links": [url if url is not None else package_url(from_code, to_code, version)],
        "type": "translate",
    }
    item.update(extra)
    return item


@dataclass
class Route:
    """假服务器上的一个地址。

    body 按 chunk_size 分块流式返回，每块之后让出一次事件循环，模拟逐块到达的网络数据。
    gate 不为 None 时，第一块发出后设置 reached，然后等 gate 被设置才继续发送。
    on_chunk 在每块发出之前调用（测试用它推进假时钟）。error 不为 None 时传输层直接抛出它。
    """

    body: bytes = b""
    status: int = 200
    chunk_size: int = 64 * 1024
    error: Exception | None = None
    gate: asyncio.Event | None = None
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    on_chunk: Callable[[], None] | None = None
    content_length: bool = True


class FakeServer:
    """按地址的路径返回预设的响应，并记录每个请求的完整地址。没有预设的路径返回 404。"""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.requests: list[str] = []

    def add(self, url: str, route: Route) -> Route:
        self.routes[httpx.URL(url).path] = route
        return route

    def add_index(self, items: object, *, url: str = INDEX_URL) -> Route:
        return self.add(url, Route(body=json.dumps(items).encode("utf-8")))

    def add_package(
        self,
        from_code: str,
        to_code: str,
        version: str = "1.9",
        *,
        gate: asyncio.Event | None = None,
        body: bytes | None = None,
    ) -> Route:
        """在 package_url(from_code, to_code, version) 上放一个模型包，默认是对应语言对的合成包。"""
        content = body if body is not None else package_bytes(from_code, to_code, version=version)
        return self.add(package_url(from_code, to_code, version), Route(body=content, gate=gate))

    def requested(self, path_fragment: str) -> int:
        return sum(1 for url in self.requests if path_fragment in url)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, content=b"not found")
        if route.error is not None:
            raise route.error
        headers = {"Content-Length": str(len(route.body))} if route.content_length else {}
        return httpx.Response(route.status, headers=headers, content=self._stream(route))

    @staticmethod
    async def _stream(route: Route) -> AsyncIterator[bytes]:
        for offset in range(0, len(route.body), route.chunk_size):
            if route.on_chunk is not None:
                route.on_chunk()
            yield route.body[offset : offset + route.chunk_size]
            await asyncio.sleep(0)
            if offset == 0 and route.gate is not None:
                route.reached.set()
                await route.gate.wait()
