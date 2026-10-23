"""显卡运行库的文件布局、校验与解压（需求 MOD-06，架构文档 8.3.1 节）。

运行库来自 PyPI 上 NVIDIA 发布的 ``nvidia-cublas-cu12`` wheel。wheel 是 zip 文件，只取其中两个 DLL 与许可证：

- ``nvidia/cublas/bin/cublasLt64_12.dll``（约 640 MB）与 ``nvidia/cublas/bin/cublas64_12.dll``（约 100 MB）；
- ``nvidia_cublas_cu12-<版本>.dist-info/licenses/License.txt``（NVIDIA 软件许可）。

目录布局（root 是模型目录下的 ``runtimes/cublas12``）::

    root/<wheel 文件名>.part      下载中的 wheel，校验、解压后删除
    root/<版本>.tmp/              解压中的目录，完成后原子改名
    root/<版本>/bin/*.dll         NMT 进程加载的 DLL
    root/<版本>/License.txt
    root/<版本>/installed.json    最后写入：版本与各文件大小。没有它或大小不符的目录视为未装好

wheel 的 SHA-256 与大小写死在 ``CUBLAS_WHEEL`` 里（取自 PyPI 的 JSON 接口），下载内容与之不符就不解压。
解压后再按 wheel 自带的 RECORD 核对两个 DLL 的 SHA-256。这里的函数都是同步的，由模块放进线程池执行。
"""

import base64
import hashlib
import json
import shutil
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from lst._fileops import publish_directory

PART_SUFFIX: Final = ".part"
TMP_SUFFIX: Final = ".tmp"
MARKER: Final = "installed.json"
DLL_NAMES: Final = ("cublasLt64_12.dll", "cublas64_12.dll")
_COPY_CHUNK: Final = 1 << 20


@dataclass(frozen=True, slots=True)
class WheelSpec:
    """一个 wheel 文件：版本、下载地址、大小（字节）与 SHA-256（十六进制小写）。"""

    version: str
    url: str
    size: int
    sha256: str

    @property
    def file_name(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    @property
    def dist_info(self) -> str:
        return f"nvidia_cublas_cu12-{self.version}.dist-info"


CUBLAS_WHEEL: Final = WheelSpec(
    version="12.9.2.10",
    url=(
        "https://files.pythonhosted.org/packages/20/e2/"
        "fc9a0e985249d873150276d5afb02e39a66817fedbf1a385724393e505ed/"
        "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl"
    ),
    size=553_162_896,
    sha256="623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661",
)
"""要下载的 cuBLAS。CUDA 12 的各个小版本对驱动的要求相同（CUDA 次版本兼容），选 12.9 的最新修订：
它支持 RTX 50 系列（Blackwell），wheel 也比 12.8 小。CTranslate2 4.x 的 Windows 版按 CUDA 12 编译，
只依赖 cublas64_12.dll；本机用 RTX 5080 实测可以翻译。"""


class InvalidRuntime(Exception):
    """下载的 wheel 或解压出的文件与预期不符。"""


@dataclass(frozen=True, slots=True)
class InstalledRuntime:
    """一个装好的运行库：版本、DLL 目录与全部文件的大小合计。"""

    version: str
    bin_dir: Path
    size: int


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) if part.isdigit() else -1 for part in version.split("."))


def _read_installed(directory: Path) -> InstalledRuntime | None:
    """读一个版本目录：说明文件存在、各文件大小与说明一致时返回它，否则返回 None。"""
    try:
        raw: object = json.loads((directory / MARKER).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        marker = cast(dict[str, object], raw)
        version, files = marker.get("version"), marker.get("files")
        if not isinstance(version, str) or not isinstance(files, dict):
            return None
        total = 0
        for name, size in cast(dict[object, object], files).items():
            if not isinstance(name, str) or not isinstance(size, int):
                return None
            if (directory / name).stat().st_size != size:
                return None
            total += size
    except (OSError, ValueError, KeyError, TypeError):
        return None
    bin_dir = directory / "bin"
    if not all((bin_dir / name).is_file() for name in DLL_NAMES):
        return None
    return InstalledRuntime(version, bin_dir, total)


def scan_installed(root: Path) -> InstalledRuntime | None:
    """root 下装好的最新版本；没有时返回 None。"""
    try:
        entries = [
            entry for entry in root.iterdir() if entry.is_dir() and not entry.name.endswith(TMP_SUFFIX)
        ]
    except OSError:
        return None
    found = [runtime for entry in entries if (runtime := _read_installed(entry)) is not None]
    return max(found, key=lambda runtime: _version_key(runtime.version), default=None)


def installed_bin_dir(root: Path) -> Path | None:
    """NMT 进程要加载的 DLL 目录（最新的已装版本），没有装好时为 None。组合根按它生成 NMT 进程的设置。"""
    runtime = scan_installed(root)
    return runtime.bin_dir if runtime is not None else None


def remove_leftovers(root: Path) -> int:
    """删除上次下载或解压中断留下的 .part 文件与 .tmp 目录，返回删除的个数。"""
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file() and entry.name.endswith(PART_SUFFIX):
                entry.unlink()
            elif entry.is_dir() and entry.name.endswith(TMP_SUFFIX):
                shutil.rmtree(entry)
            else:
                continue
        except OSError:
            continue
        removed += 1
    return removed


def verify_wheel(part: Path, spec: WheelSpec, sha256: str) -> None:
    """核对下载内容的大小与 SHA-256（sha256 是下载时边写边算的摘要）。"""
    size = part.stat().st_size
    if size != spec.size:
        raise InvalidRuntime(f"大小 {size} 与预期的 {spec.size} 不符")
    if sha256 != spec.sha256:
        raise InvalidRuntime("SHA-256 与预期不符")


def _record_digests(wheel: zipfile.ZipFile, spec: WheelSpec) -> dict[str, str]:
    """wheel 的 RECORD：成员名 → urlsafe base64 编码的 SHA-256（没有摘要的行不收）。"""
    try:
        text = wheel.read(f"{spec.dist_info}/RECORD").decode("utf-8")
    except KeyError as exc:
        raise InvalidRuntime("wheel 里没有 RECORD") from exc
    digests: dict[str, str] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(",")
        digest = rest.partition(",")[0]
        if digest.startswith("sha256="):
            digests[name] = digest.removeprefix("sha256=")
    return digests


def _extract_member(wheel: zipfile.ZipFile, member: str, target: Path, expected: str | None) -> int:
    """把一个成员流式写到 target，同时按 RECORD 核对 SHA-256，返回写入的字节数。"""
    digest = hashlib.sha256()
    written = 0
    try:
        source = wheel.open(member)
    except KeyError as exc:
        raise InvalidRuntime(f"wheel 里没有 {member}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    with source, target.open("wb") as sink:
        for block in iter(lambda: source.read(_COPY_CHUNK), b""):
            sink.write(block)
            digest.update(block)
            written += len(block)
    if expected is not None:
        actual = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")
        if actual != expected:
            raise InvalidRuntime(f"{member} 的 SHA-256 与 RECORD 不符")
    return written


def install_wheel(part: Path, root: Path, spec: WheelSpec) -> InstalledRuntime:
    """从校验过的 wheel 取出 DLL 与许可证，装到 root/<版本>，删除 wheel，返回装好的运行库。

    先解压到 root/<版本>.tmp，写好说明文件后原子改名；同一版本的旧目录先删除（NMT 进程已停止或没有加载它，
    否则删除失败，抛出 OSError）。
    """
    staging = root / f"{spec.version}{TMP_SUFFIX}"
    final = root / spec.version
    if staging.exists():
        shutil.rmtree(staging)
    members: dict[str, str] = {f"nvidia/cublas/bin/{name}": f"bin/{name}" for name in DLL_NAMES}
    members[f"{spec.dist_info}/licenses/License.txt"] = "License.txt"
    try:
        with zipfile.ZipFile(part) as wheel:
            digests = _record_digests(wheel, spec)
            sizes: dict[str, int] = {}
            for member, relative in members.items():
                sizes[relative] = _extract_member(wheel, member, staging / relative, digests.get(member))
        marker = {"version": spec.version, "source": spec.file_name, "files": sizes}
        (staging / MARKER).write_text(json.dumps(marker, indent=2), encoding="utf-8")
        if final.exists():
            shutil.rmtree(final)
        publish_directory(staging, final)
    except zipfile.BadZipFile as exc:
        raise InvalidRuntime("下载内容不是有效的 wheel（zip）文件") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    part.unlink(missing_ok=True)
    runtime = _read_installed(final)
    if runtime is None:
        raise InvalidRuntime("解压后的文件与说明不符")
    return runtime


def delete_all(root: Path, keep: Iterable[str] = ()) -> None:
    """删除 root 下全部版本目录（keep 里的版本除外）。DLL 仍被进程加载时抛 OSError。"""
    kept = set(keep)
    for entry in list(root.iterdir()) if root.is_dir() else []:
        if entry.is_dir() and entry.name not in kept:
            shutil.rmtree(entry)
