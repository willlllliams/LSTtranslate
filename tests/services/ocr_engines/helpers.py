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

from lst.contracts.ocr_engines import OcrEngineKind, OcrRuntimeVariant, OcrStage
from lst.services.ocr_engines import (
    CommandResult,
    ModelSpec,
    PaddleInstallPaths,
    RapidInstallPaths,
    model_url,
    requirements_name,
)
from lst.services.ocr_engines.catalog import RAPID_BUNDLED_FILES
from lst.services.ocr_engines.layout import RUNTIME_STAMP

BASE_URL = "https://models.invalid/paddle3.0.0"
RAPID_URL = "https://models.invalid/rapid"

CPU = OcrRuntimeVariant.CPU
CUDA = OcrRuntimeVariant.CUDA
DML = OcrRuntimeVariant.DIRECTML

MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("PP-OCRv5_mobile_det", OcrStage.DETECTION, 3000, default=True),
    ModelSpec("PP-OCRv5_mobile_rec", OcrStage.RECOGNITION, 5000, languages=("zh-Hans", "en"), default=True),
    ModelSpec("SLANet_plus", OcrStage.TABLE, 2000, default=True),
)
"""测试用的 PaddleOCR 默认组合：安装时全部下载。"""

EXTRA_MODEL = ModelSpec("PP-OCRv5_server_det", OcrStage.DETECTION, 4000, "server")
"""不在默认组合里的可选模型，单独下载与删除。"""

AUX_MODEL = ModelSpec("SLANeXt_wired", None, 1500, default=True)
"""附属模型：随引擎安装，不列出，不能删除。"""

PADDLE_TEST_MODELS: tuple[ModelSpec, ...] = (*MODELS, EXTRA_MODEL)

RAPID_BUNDLED_DET = ModelSpec(
    "PP-OCRv6/small/multi/det",
    OcrStage.DETECTION,
    9,
    default=True,
    bundled=True,
    filename="PP-OCRv6_det_small.onnx",
)
RAPID_BUNDLED_REC = ModelSpec(
    "PP-OCRv6/small/multi/rec",
    OcrStage.RECOGNITION,
    21,
    languages=("zh-Hans", "en", "ja"),
    default=True,
    bundled=True,
    filename="PP-OCRv6_rec_small.onnx",
)
RAPID_BUNDLED_CLS = ModelSpec(
    "PP-OCRv4/mobile/ch/cls",
    OcrStage.TEXTLINE_ORIENTATION,
    1,
    default=True,
    bundled=True,
    filename="ch_ppocr_mobile_v2.0_cls_mobile.onnx",
)
RAPID_EXTRA_BODY = b"onnx-model-medium" * 100
RAPID_EXTRA = ModelSpec(
    "PP-OCRv6/medium/multi/rec",
    OcrStage.RECOGNITION,
    None,
    languages=("zh-Hans", "en", "ja", "de"),
    url=f"{RAPID_URL}/PP-OCRv6_rec_medium.onnx",
    sha256=hashlib.sha256(RAPID_EXTRA_BODY).hexdigest(),
    filename="PP-OCRv6_rec_medium.onnx",
)
RAPID_TEST_MODELS: tuple[ModelSpec, ...] = (
    RAPID_BUNDLED_DET,
    RAPID_BUNDLED_REC,
    RAPID_BUNDLED_CLS,
    RAPID_EXTRA,
)
RAPID_MODEL_FILES: tuple[str, ...] = tuple(f"rapidocr/models/{name}" for name in RAPID_BUNDLED_FILES)


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

    def add_url(self, url: str, route: Route) -> Route:
        self.routes[httpx.URL(url).path] = route
        return route

    def add_model(self, name: str, route: Route | None = None) -> Route:
        chosen = route if route is not None else Route(body=model_tar(name))
        if not chosen.body and chosen.status == 200 and chosen.error is None:
            chosen.body = model_tar(name)
        self.routes[httpx.URL(model_url(BASE_URL, name)).path] = chosen
        return chosen

    def add_models(self, models: Sequence[ModelSpec] = MODELS) -> None:
        for model in models:
            self.add_model(model.id)

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
    files 是写进目标目录的文件（相对路径 → 内容），默认是一个假的 paddle 包。
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
    files: dict[str, bytes] = field(default_factory=lambda: {"paddle/__init__.py": b"# fake\n"})

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
        write_files(target, self.files)
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


RAPID_UV_FILES: dict[str, bytes] = {
    "rapidocr/__init__.py": b"# fake\n",
    **dict.fromkeys(RAPID_MODEL_FILES, b"onnx"),
}
"""假 uv 装 RapidOCR 运行环境时写入的文件：包与两个自带模型。"""


def write_files(root: Path, files: Mapping[str, bytes]) -> None:
    """在 root 下写入 files（相对路径 → 内容）。"""
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def write_requirements(app: Path, kind: OcrEngineKind, variants: Sequence[OcrRuntimeVariant]) -> None:
    """在安装目录 app\\ 下为各版本写占位的版本清单（内容不同，哈希因此不同）。"""
    app.mkdir(parents=True, exist_ok=True)
    for index, variant in enumerate(variants):
        digit = str(index + (1 if kind is OcrEngineKind.RAPID else 0))
        package = "rapidocr==3.10.0" if kind is OcrEngineKind.RAPID else "paddlepaddle==3.3.1"
        (app / requirements_name(kind, variant)).write_text(
            f"{package} \\\n    --hash=sha256:" + digit * 64 + "\n", encoding="utf-8"
        )


def _tools(install: Path) -> None:
    for path in (install / "tools" / "uv.exe", install / "runtime" / "python.exe"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MZ")


def make_rapid_paths(
    root: Path, variants: Sequence[OcrRuntimeVariant] = (CPU, DML, CUDA)
) -> RapidInstallPaths:
    """RapidOCR 的安装布局，与 make_paths 共用安装目录与数据目录；各版本的版本清单是占位文件。"""
    install = root / "Programs" / "LSTtranslate"
    data = root / "LocalAppData" / "LSTtranslate"
    _tools(install)
    write_requirements(install / "app", OcrEngineKind.RAPID, variants)
    return RapidInstallPaths(
        engine_dir=data / "engines" / "rapid",
        model_dir=data / "models" / "ocr" / "rapid",
        uv=install / "tools" / "uv.exe",
        python=install / "runtime" / "python.exe",
        requirements_dir=install / "app",
    )


def install_rapid_layout(paths: RapidInstallPaths, variant: OcrRuntimeVariant = CPU) -> None:
    """按 RapidOCR 安装完成的样子摆好文件：带运行环境标记与自带模型的 site、安装标记。"""
    digest = hashlib.sha256(paths.requirements(variant).read_bytes()).hexdigest()
    write_files(paths.site_dir, RAPID_UV_FILES)
    (paths.site_dir / RUNTIME_STAMP).write_text(json.dumps({"requirements_sha256": digest}), encoding="utf-8")
    paths.marker.write_text(
        json.dumps({"runtime_requirements_sha256": digest, "variant": variant.value, "models": []}),
        encoding="utf-8",
    )


def make_paths(root: Path, variants: Sequence[OcrRuntimeVariant] = (CPU, CUDA)) -> PaddleInstallPaths:
    """安装布局：tools\\uv.exe、runtime\\python.exe、app\\ 下各版本的版本清单都是占位文件。"""
    install = root / "Programs" / "LSTtranslate"
    data = root / "LocalAppData" / "LSTtranslate"
    _tools(install)
    write_requirements(install / "app", OcrEngineKind.PADDLE, variants)
    return PaddleInstallPaths(
        engine_dir=data / "engines" / "paddle",
        model_cache_dir=data / "models" / "ocr",
        uv=install / "tools" / "uv.exe",
        python=install / "runtime" / "python.exe",
        requirements_dir=install / "app",
    )


def install_layout(
    paths: PaddleInstallPaths, models: Sequence[ModelSpec], variant: OcrRuntimeVariant = CPU
) -> None:
    """按安装完成的样子摆好文件：运行环境（带按当前版本清单写的运行环境标记）、全部模型、安装标记。"""
    digest = hashlib.sha256(paths.requirements(variant).read_bytes()).hexdigest()
    (paths.site_dir / "paddle").mkdir(parents=True)
    (paths.site_dir / RUNTIME_STAMP).write_text(json.dumps({"requirements_sha256": digest}), encoding="utf-8")
    for model in models:
        write_model(paths.model_dir(model.id))
    paths.marker.write_text(
        json.dumps(
            {
                "runtime_requirements_sha256": digest,
                "variant": variant.value,
                "models": [model.id for model in models],
            }
        ),
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
