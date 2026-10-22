"""OCR 引擎测试用的合成数据、假服务器与假 uv 运行器。

模型包是结构与 PaddleX 官方包一致的小 tar（顶层一个 ``<模型名>_infer/`` 目录），在内存里用 tarfile 生成。
网络由 httpx.MockTransport 扮演，主机名用 .invalid 结尾的保留域名，漏接假传输层的请求也到不了真实服务器。
uv 由假运行器扮演，不启动任何子进程。
"""

import asyncio
import hashlib
import io
import json
import tarfile
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from lst.services.ocr_engines import CommandResult, PaddleInstallPaths, PaddleModel, model_url
from lst.services.ocr_engines.layout import RUNTIME_STAMP

BASE_URL = "https://models.invalid/paddle3.0.0"

MODELS: tuple[PaddleModel, ...] = (
    PaddleModel("PP-OCRv5_mobile_det", 3000),
    PaddleModel("PP-OCRv5_mobile_rec", 5000),
    PaddleModel("SLANet_plus", 2000, "table"),
)


def model_tar(
    name: str,
    *,
    root: str | None = None,
    payload: bytes = b"params",
    extra: Mapping[str, bytes] | None = None,
    files: bool = True,
) -> bytes:
    """在内存里生成一个模型包。root 默认是 ``<模型名>_infer``；extra 是额外成员（名字 → 内容）。"""
    top = root if root is not None else f"{name}_infer"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:

        def add(member_name: str, data: bytes) -> None:
            info = tarfile.TarInfo(member_name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        directory = tarfile.TarInfo(top)
        directory.type = tarfile.DIRTYPE
        tar.addfile(directory)
        if files:
            add(f"{top}/inference.yml", b"Global:\n  model_name: " + name.encode())
            add(f"{top}/inference.json", b"{}")
            add(f"{top}/inference.pdiparams", payload)
        for member_name, data in (extra or {}).items():
            add(member_name, data)
    return buffer.getvalue()


def symlink_tar(name: str) -> bytes:
    """含有指向安装目录之外的符号链接的模型包。"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        link = tarfile.TarInfo(f"{name}_infer/inference.pdiparams")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../../outside.txt"
        tar.addfile(link)
    return buffer.getvalue()


@dataclass
class Route:
    """假服务器上的一个地址。body 按 chunk_size 分块返回；gate 不为 None 时第一块之后等 gate。"""

    body: bytes = b""
    status: int = 200
    chunk_size: int = 1024
    error: Exception | None = None
    gate: asyncio.Event | None = None
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    on_chunk: Callable[[], None] | None = None
    content_length: int | None = None


class FakeServer:
    """按路径返回预设的响应，记录每个请求的完整地址。没有预设的路径返回 404。"""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.requests: list[str] = []

    def add_model(self, name: str, route: Route | None = None) -> Route:
        chosen = route if route is not None else Route(body=model_tar(name))
        if not chosen.body and chosen.status == 200 and chosen.error is None:
            chosen.body = model_tar(name)
        self.routes[httpx.URL(model_url(BASE_URL, name)).path] = chosen
        return chosen

    def add_models(self, models: Sequence[PaddleModel] = MODELS) -> None:
        for model in models:
            self.add_model(model.name)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, content=b"not found")
        if route.error is not None:
            raise route.error
        length = route.content_length if route.content_length is not None else len(route.body)
        return httpx.Response(
            route.status, headers={"Content-Length": str(length)}, content=self._stream(route)
        )

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


@dataclass
class FakeUv:
    """假的 uv 运行器：记录命令行与环境变量，往 --target 指定的目录写一个假包。

    returncode 非零时不写文件，输出 output。gate 不为 None 时写完缓存后设置 reached，等 gate 再返回。
    cache_bytes 是写进 UV_CACHE_DIR 的字节数，用来驱动按缓存大小估计的进度。
    """

    returncode: int = 0
    output: str = ""
    gate: asyncio.Event | None = None
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    cache_bytes: int = 0
    error: OSError | None = None
    calls: list[list[str]] = field(default_factory=list[list[str]])
    envs: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    cancelled: int = 0

    async def __call__(self, args: Sequence[str], env: Mapping[str, str]) -> CommandResult:
        self.calls.append(list(args))
        self.envs.append(dict(env))
        if self.error is not None:
            raise self.error
        target = Path(args[args.index("--target") + 1])
        cache = Path(env["UV_CACHE_DIR"])
        cache.mkdir(parents=True, exist_ok=True)
        if self.cache_bytes:
            (cache / "wheel.bin").write_bytes(b"x" * self.cache_bytes)
        target.mkdir(parents=True, exist_ok=True)
        (target / "paddle").mkdir(exist_ok=True)
        (target / "paddle" / "__init__.py").write_text("# fake\n", encoding="utf-8")
        if self.gate is not None:
            self.reached.set()
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        if self.returncode != 0:
            return CommandResult(self.returncode, self.output)
        return CommandResult(0, self.output or "Installed 1 package")


def make_paths(root: Path) -> PaddleInstallPaths:
    """安装布局：tools\\uv.exe、runtime\\python.exe、app\\ 下的版本清单都是占位文件。"""
    install = root / "Programs" / "LSTtranslate"
    data = root / "LocalAppData" / "LSTtranslate"
    for path in (install / "tools" / "uv.exe", install / "runtime" / "python.exe"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MZ")
    requirements = install / "app" / "ocr-paddle-requirements.txt"
    requirements.parent.mkdir(parents=True, exist_ok=True)
    requirements.write_text("paddlepaddle==3.3.1 \\\n    --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")
    return PaddleInstallPaths(
        engine_dir=data / "engines" / "paddle",
        model_cache_dir=data / "models" / "ocr",
        uv=install / "tools" / "uv.exe",
        python=install / "runtime" / "python.exe",
        requirements=requirements,
    )


def install_layout(paths: PaddleInstallPaths, models: Sequence[PaddleModel]) -> None:
    """按安装完成的样子摆好文件：运行环境（带按当前版本清单写的运行环境标记）、全部模型、安装标记。"""
    digest = hashlib.sha256(paths.requirements.read_bytes()).hexdigest()
    (paths.site_dir / "paddle").mkdir(parents=True)
    (paths.site_dir / RUNTIME_STAMP).write_text(json.dumps({"requirements_sha256": digest}), encoding="utf-8")
    for model in models:
        write_model(paths.model_dir(model.name))
    paths.marker.write_text(
        json.dumps({"runtime_requirements_sha256": digest, "models": [model.name for model in models]}),
        encoding="utf-8",
    )


def write_model(directory: Path) -> None:
    """在 directory 下放一个可用的模型目录。"""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("inference.yml", "inference.json", "inference.pdiparams"):
        (directory / name).write_bytes(b"x")


def tree(path: Path) -> list[str]:
    """path 下全部文件与目录的相对路径（正斜杠），排序后返回。"""
    if not path.exists():
        return []
    return sorted(item.relative_to(path).as_posix() for item in path.rglob("*"))
