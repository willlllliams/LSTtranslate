"""显卡运行库模块：下载、校验、解压、取消、删除与显卡检测（需求 MOD-06）。

wheel 是在内存里构造的小 zip，结构与 nvidia-cublas-cu12 相同；网络用 httpx.MockTransport，不联网。
"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from lst.contracts.nmt_runtime import (
    CancelNmtRuntimeInstall,
    DeleteNmtRuntime,
    InstallNmtRuntime,
    NmtRuntimeChanged,
    NmtRuntimeErrorCode,
    NmtRuntimeInfo,
    NmtRuntimeState,
)
from lst.kernel import EventBus, ModuleContext
from lst.services._shared.gpu import NVIDIA_VENDOR_ID, GpuAdapter
from lst.services.nmt_runtime import NmtRuntimeModule, WheelSpec, installed_bin_dir
from lst.services.nmt_runtime.package import MARKER, PART_SUFFIX, TMP_SUFFIX, scan_installed

VERSION = "12.9.2.10"
URL = f"https://pypi.invalid/nvidia_cublas_cu12-{VERSION}-py3-none-win_amd64.whl"
DIST = f"nvidia_cublas_cu12-{VERSION}.dist-info"
RTX = GpuAdapter("NVIDIA GeForce RTX 5080", NVIDIA_VENDOR_ID, 0x2C02, 16 << 30, d3d12=True)
INTEL = GpuAdapter("Intel(R) Graphics", 0x8086, 0x7D67, 128 << 20, d3d12=True)


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def make_wheel(*, record_ok: bool = True, with_lt: bool = True) -> bytes:
    """结构与 nvidia-cublas-cu12 相同的小 wheel：两个 DLL、nvblas、头文件、许可证与 RECORD。"""
    files = {
        "nvidia/cublas/bin/cublas64_12.dll": b"cublas" * 100,
        "nvidia/cublas/bin/nvblas64_12.dll": b"nvblas",
        "nvidia/cublas/include/cublas.h": b"/* header */",
        f"{DIST}/licenses/License.txt": b"NVIDIA License",
    }
    if with_lt:
        files["nvidia/cublas/bin/cublasLt64_12.dll"] = b"cublasLt" * 1000
    record = [f"{name},{_record_hash(data)},{len(data)}" for name, data in files.items()]
    if not record_ok:
        record = [line.replace("sha256=", "sha256=x") if "cublas64" in line else line for line in record]
    record.append(f"{DIST}/RECORD,,")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as wheel:
        for name, data in files.items():
            wheel.writestr(name, data)
        wheel.writestr(f"{DIST}/RECORD", "\n".join(record))
    return buffer.getvalue()


def spec_for(data: bytes) -> WheelSpec:
    return WheelSpec(VERSION, URL, len(data), hashlib.sha256(data).hexdigest())


@dataclass
class Server:
    """假的 PyPI 文件服务器：返回 body；gate 不为 None 时先等它（用来在下载中途取消）。"""

    body: bytes
    status: int = 200
    gate: asyncio.Event | None = None
    error: Exception | None = None
    requests: list[str] = field(default_factory=list[str])

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if self.error is not None:
            raise self.error
        gate = self.gate

        async def stream() -> AsyncIterator[bytes]:
            yield self.body[:10]
            if gate is not None:
                await gate.wait()
            yield self.body[10:]

        return httpx.Response(self.status, stream=_Stream(stream()))


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: AsyncIterator[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            yield chunk


@dataclass
class Rig:
    bus: EventBus
    module: NmtRuntimeModule
    root: Path
    server: Server
    infos: list[NmtRuntimeInfo]
    changes: list[NmtRuntimeChanged]
    deletes: list[str]
    ctx: ModuleContext

    @property
    def info(self) -> NmtRuntimeInfo:
        return self.infos[-1]

    async def settle(self) -> None:
        """等命令分发完、后台的下载或删除任务结束，再把它发布的消息分发完。"""
        await self.bus.drain()
        task = self.module._task  # pyright: ignore[reportPrivateUsage]
        if task is not None:
            done, _ = await asyncio.wait({task}, timeout=10)
            assert done, "后台任务没有结束"
        await self.bus.drain()


type Start = Callable[..., Awaitable[Rig]]


@pytest.fixture
async def start(tmp_path: Path) -> AsyncIterator[Start]:
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    bus = EventBus(loop, logger=logging.getLogger("tests.nmt_runtime.bus"), raise_errors=True)
    clients: list[httpx.AsyncClient] = []
    modules: list[NmtRuntimeModule] = []

    async def make(
        *,
        body: bytes | None = None,
        spec: WheelSpec | None = None,
        gpus: tuple[GpuAdapter, ...] = (RTX, INTEL),
    ) -> Rig:
        data = make_wheel() if body is None else body
        server = Server(data)
        client = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))
        clients.append(client)
        deletes: list[str] = []

        async def before_delete() -> None:
            deletes.append("stop-nmt")

        root = tmp_path / "runtimes" / "cublas12"
        module = NmtRuntimeModule(
            root,
            client,
            spec=spec if spec is not None else spec_for(data),
            before_delete=before_delete,
            probe=lambda: list(gpus),
            progress_interval=0,
        )
        modules.append(module)
        infos: list[NmtRuntimeInfo] = []
        changes: list[NmtRuntimeChanged] = []
        bus.subscribe(NmtRuntimeInfo, infos.append)
        bus.subscribe(NmtRuntimeChanged, changes.append)
        ctx = ModuleContext(bus, logging.getLogger("lst.nmt_runtime"))
        await module.start(ctx)
        await bus.drain()
        return Rig(bus, module, root, server, infos, changes, deletes, ctx)

    try:
        yield make
    finally:
        for module in modules:
            await module.stop()
        for client in clients:
            await client.aclose()
        await bus.drain()
        await bus.aclose()
        loop.set_exception_handler(None)
    assert not errors, [context.get("exception") or context.get("message") for context in errors]


async def test_starts_without_network_and_reports_nvidia_gpus(start: Start) -> None:
    rig = await start()
    assert rig.info.state is NmtRuntimeState.NOT_INSTALLED
    assert rig.info.gpus == ("NVIDIA GeForce RTX 5080",)
    assert rig.info.version == VERSION and rig.info.download_bytes == len(make_wheel())
    assert rig.server.requests == []  # 启动时不联网（PRV-01）


async def test_install_downloads_verifies_and_extracts_only_the_dlls(start: Start) -> None:
    rig = await start()
    rig.bus.send(InstallNmtRuntime())
    await rig.settle()
    assert rig.info.state is NmtRuntimeState.INSTALLED
    assert rig.info.installed_version == VERSION and rig.info.installed_bytes > 0
    bin_dir = rig.root / VERSION / "bin"
    assert sorted(p.name for p in bin_dir.iterdir()) == ["cublas64_12.dll", "cublasLt64_12.dll"]
    assert (rig.root / VERSION / "License.txt").read_bytes() == b"NVIDIA License"
    assert json.loads((rig.root / VERSION / MARKER).read_text(encoding="utf-8"))["version"] == VERSION
    assert not list(rig.root.glob(f"*{PART_SUFFIX}"))  # wheel 解压后删除
    assert rig.changes == [NmtRuntimeChanged(installed=True, bin_dir=str(bin_dir))]
    assert installed_bin_dir(rig.root) == bin_dir
    states = [info.state for info in rig.infos]
    assert NmtRuntimeState.DOWNLOADING in states and NmtRuntimeState.INSTALLING in states


async def test_installed_runtime_is_found_after_restart(start: Start) -> None:
    rig = await start()
    rig.bus.send(InstallNmtRuntime())
    await rig.settle()
    await rig.module.stop()
    rig.ctx.close()
    again = await start()
    assert again.info.state is NmtRuntimeState.INSTALLED
    again.bus.send(InstallNmtRuntime())  # 已装同一版本：不再下载
    await again.settle()
    assert again.server.requests == []


async def test_wrong_hash_fails_without_leaving_files(start: Start) -> None:
    data = make_wheel()
    rig = await start(body=data, spec=WheelSpec(VERSION, URL, len(data), "0" * 64))
    rig.bus.send(InstallNmtRuntime())
    await rig.settle()
    assert rig.info.state is NmtRuntimeState.ERROR
    assert rig.info.error_code is NmtRuntimeErrorCode.INVALID_PACKAGE
    assert list(rig.root.iterdir()) == []
    assert rig.changes == []


@pytest.mark.parametrize(
    ("body", "reason"),
    [(make_wheel(record_ok=False), "RECORD 摘要不符"), (make_wheel(with_lt=False), "缺少 cublasLt"),
     (b"not a zip" * 10, "不是 zip")],
    ids=["record", "missing-lt", "not-zip"],
)  # fmt: skip
async def test_bad_wheel_content_is_rejected(start: Start, body: bytes, reason: str) -> None:
    rig = await start(body=body)
    rig.bus.send(InstallNmtRuntime())
    await rig.settle()
    assert rig.info.error_code is NmtRuntimeErrorCode.INVALID_PACKAGE, reason
    assert scan_installed(rig.root) is None
    assert not list(rig.root.glob(f"*{TMP_SUFFIX}"))


async def test_http_and_network_errors_are_classified(start: Start) -> None:
    rig = await start()
    rig.server.status = 404
    rig.bus.send(InstallNmtRuntime())
    await rig.settle()
    assert rig.info.error_code is NmtRuntimeErrorCode.HTTP
    rig.server.status, rig.server.error = 200, httpx.ConnectError("offline")
    rig.bus.send(InstallNmtRuntime())  # 出错后可以重试
    await rig.settle()
    assert rig.info.error_code is NmtRuntimeErrorCode.NETWORK
    rig.server.error = None
    rig.bus.send(InstallNmtRuntime())
    await rig.settle()
    assert rig.info.state is NmtRuntimeState.INSTALLED and rig.info.error_code is None


async def test_cancel_during_download_removes_the_partial_file(start: Start) -> None:
    rig = await start()
    rig.server.gate = asyncio.Event()
    rig.bus.send(InstallNmtRuntime())
    await rig.bus.drain()
    for _ in range(200):
        if list(rig.root.glob(f"*{PART_SUFFIX}")):
            break
        await asyncio.sleep(0.01)
    await rig.bus.drain()
    assert rig.info.state is NmtRuntimeState.DOWNLOADING
    assert list(rig.root.glob(f"*{PART_SUFFIX}"))
    rig.bus.send(CancelNmtRuntimeInstall())
    await rig.settle()
    rig.server.gate.set()
    assert rig.info.state is NmtRuntimeState.NOT_INSTALLED and rig.info.progress is None
    assert list(rig.root.iterdir()) == []


async def test_delete_stops_nmt_first_and_announces_removal(start: Start) -> None:
    rig = await start()
    rig.bus.send(InstallNmtRuntime())
    await rig.settle()
    rig.bus.send(DeleteNmtRuntime())
    await rig.settle()
    assert rig.deletes == ["stop-nmt"]
    assert rig.info.state is NmtRuntimeState.NOT_INSTALLED and rig.info.installed_version is None
    assert rig.changes[-1] == NmtRuntimeChanged(installed=False)
    assert installed_bin_dir(rig.root) is None


async def test_leftovers_are_removed_on_start(start: Start, tmp_path: Path) -> None:
    root = tmp_path / "runtimes" / "cublas12"
    (root / f"{VERSION}{TMP_SUFFIX}" / "bin").mkdir(parents=True)
    (root / f"x.whl{PART_SUFFIX}").write_bytes(b"half")
    rig = await start()
    assert list(rig.root.iterdir()) == []


async def test_no_nvidia_gpu_is_reported_but_download_stays_possible(start: Start) -> None:
    rig = await start(gpus=(INTEL,))
    assert rig.info.gpus == ()
    rig.bus.send(InstallNmtRuntime())
    await rig.settle()
    assert rig.info.state is NmtRuntimeState.INSTALLED


async def test_tampered_install_is_not_used(start: Start) -> None:
    rig = await start()
    rig.bus.send(InstallNmtRuntime())
    await rig.settle()
    (rig.root / VERSION / "bin" / "cublas64_12.dll").write_bytes(b"short")
    assert installed_bin_dir(rig.root) is None  # 大小与说明不符，按未装好处理
