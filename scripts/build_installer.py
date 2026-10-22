r"""构建安装包（需求 INS-01 至 INS-08，架构文档第 15 章）。

用法（仓库根目录）：
    uv run python scripts/build_installer.py [--allow-unhashed] [--skip-iscc]

安装包不捆绑 PaddleOCR、RapidOCR 的运行环境与模型：它们由 services.ocr_engines 在用户点“安装”时下载，
运行环境用随安装包分发的 tools\uv.exe 按 app\ocr-<引擎>-<版本>-requirements.txt 安装（需求 OCRE-07：
PaddleOCR 有 cpu、cuda 两个版本，RapidOCR 有 cpu、directml、cuda 三个版本）。

步骤：
1. 复制 uv 管理的独立版 CPython 3.13 到 build\stage\runtime\，去掉 pip、tcl/tkinter、IDLE、头文件等；
2. 按 uv.lock 导出主进程依赖，安装到 build\stage\site\main\；
3. 按 .venv-ocr 当前的精确版本生成各 OCR 引擎各版本的运行环境清单（VARIANT_SWAPS）：
   - PaddleOCR CPU 版：app\ocr-paddle-cpu-requirements.txt，``uv pip freeze`` 冻结的 .venv-ocr 全部包；
   - RapidOCR CPU 版：app\ocr-rapid-cpu-requirements.txt，只含 rapidocr、onnxruntime 及其依赖的闭包
     （RAPID_ROOTS）。闭包按 .venv-ocr 里各包元数据的 Requires-Dist 求出，环境标记按构建机（与用户机器同为
     Windows x64、Python 3.13）求值，可选依赖（extra）不计入。rapidocr 声明依赖 opencv-python-headless，
     .venv-ocr 按 ocr-overrides.txt 只装了 opencv-contrib-python（它包含 headless 版的全部模块），闭包里用
     .venv-ocr 实际装的那一个 OpenCV 发行包代替，版本与开发环境测过的一致；
   - GPU 版：在 CPU 版清单上只替换推理库——paddlepaddle 换成 Paddle 官方源 cu129 的 paddlepaddle-gpu
     （同版本，用 wheel 的直接地址，免得 uv 在两个索引之间挑包），onnxruntime 换成 onnxruntime-directml 或
     onnxruntime-gpu[cuda,cudnn]（extra 带上 CUDA 13 与 cuDNN 9 运行库）。再用 ``uv pip compile`` 以其余各包的
     精确版本为准解析一次，补上新推理库的依赖（CUDA 运行库、DirectML 版的 sympy），加
     ``--override ocr-overrides.txt``，与创建 .venv-ocr 时一样去掉 opencv-python-headless；
   全部清单都再用 ``uv pip compile --no-deps --generate-hashes`` 补上每个包的 SHA-256。
   compile 加 ``--no-config``（不读构建机上的 uv 配置）与 ``--only-binary :all:``（只收录 wheel 的哈希，
   与运行时安装的 ``--no-build`` 对应）。
   解析依赖与补哈希都要访问包索引；不联网时这一步失败，构建中止。加 --allow-unhashed 时改为写入不带哈希的
   清单，运行时安装因此不加 --require-hashes；GPU 版的依赖这时也解析不了，清单里写上
   ``# lst: resolve-dependencies``（lst/services/ocr_engines/runtime.py 的 RESOLVE_MARKER），运行时安装不加
   --no-deps，由 uv 在用户机器上补齐清单没列出的依赖；
4. 复制开发机上的 uv.exe 与它的许可证到 build\stage\tools\；
5. 复制 lst 包、图标、启动器，预编译 .pyc，写入安装版标记 app\installed.json；
6. 在暂存目录里用安装布局自检：实际导入主进程的关键模块（含 Windows OCR 的 winrt 与后端），确认 keyring
   的后端是 Windows 凭据管理器，通过真实的 launcher\worker.py 启动一次 nmt 与 ocr 进程入口（不带 --address，
   进程在解析参数时退出，不联网），检查每份版本清单的格式、带不带哈希与构建结果一致，
   并运行 ``tools\uv.exe --version``；
7. 调用 Inno Setup 编译 installer\lsttranslate.iss，输出 dist\LSTtranslate-<版本>-setup.exe，
   并在旁边写 sha256sum 格式的校验文件 LSTtranslate-<版本>-setup.exe.sha256（应用内更新用它校验下载）。

构建开头用更新服务的 parse_version 校验 pyproject.toml 里的版本号：必须是 SemVer 规范写法
（例如 0.2.0、0.2.0-beta.1），否则中止。PEP 440 写法（0.2.0b1）会让安装包名与更新服务按 tag 拼出的
文件名对不上，应用内更新因此找不到安装包。

暂存目录与输出目录都不纳入版本库。不使用 tempfile（见架构文档 2.9.5 节）。
"""

import argparse
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # 在仓库之外运行（例如 python C:\repo\scripts\build_installer.py）时也能导入 lst。
    sys.path.insert(0, str(ROOT))

from lst.contracts.ocr_engines import OcrEngineKind, OcrRuntimeVariant  # noqa: E402
from lst.services._shared.download import dir_size, file_sha256  # noqa: E402
from lst.services.ocr_engines.layout import ENGINE_VARIANTS, requirements_name  # noqa: E402
from lst.services.ocr_engines.runtime import RESOLVE_MARKER  # noqa: E402
from lst.services.updates.version import parse_version  # noqa: E402

BUILD = ROOT / "build"
STAGE = BUILD / "stage"
DIST = ROOT / "dist"
ISCC = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe"

# 运行时里用不到的部分：pip 等自带包、Tk 图形库、IDLE、测试、头文件与链接库。
_RUNTIME_EXCLUDES = (
    "Lib/site-packages",
    "Lib/tkinter",
    "Lib/idlelib",
    "Lib/test",
    "Lib/turtledemo",
    "tcl",
    "include",
    "libs",
    "Scripts",
    "DLLs/_tkinter.pyd",
    "DLLs/tcl86t.dll",
    "DLLs/tk86t.dll",
)

# 主进程用到、自检时必须能导入的模块。
SMOKE_IMPORTS = (
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "qasync",
    "httpx",
    "pydantic",
    "ctranslate2",
    "sentencepiece",
    "lingua",
    "tomlkit",
    "keyring",
    "winrt.windows.media.ocr",
    "lst.__main__",
    "lst.workers.nmt_worker",
    "lst.workers.ocr_worker.winocr_backend",
)

LAUNCHER_SMOKE_EXIT = 66
"""推理进程缺少 --address 时的退出码（ExitCode.CONFIG_ERROR）。启动器自检据此判断进程入口已经导入成功。"""

PADDLE = OcrEngineKind.PADDLE
RAPID = OcrEngineKind.RAPID
CPU = OcrRuntimeVariant.CPU

OCR_REQUIREMENTS_NAME = requirements_name(PADDLE, CPU)
"""PaddleOCR CPU 版运行环境的版本清单在安装目录 app\\ 下的文件名（ocr-paddle-cpu-requirements.txt）。"""

RAPID_REQUIREMENTS_NAME = requirements_name(RAPID, CPU)
"""RapidOCR CPU 版运行环境的版本清单在安装目录 app\\ 下的文件名（ocr-rapid-cpu-requirements.txt）。"""

PADDLE_GPU_INDEX = "https://paddle-whl.cdn.bcebos.com/stable/cu129/paddlepaddle-gpu"
"""Paddle 官方 cu129 源里 wheel 的地址前缀（www.paddlepaddle.org.cn/packages/stable/cu129/paddlepaddle-gpu/
列出的链接）。选 cu129 而不是 cu126：CUDA 12.6 不支持 RTX 50 系列（Blackwell，需要 CUDA 12.8 及以上）。"""


@dataclass(frozen=True, slots=True)
class VariantSwap:
    """GPU 版清单相对 CPU 版的替换：去掉 remove，加上 add（可以带 {version} 占位，
    取 CPU 版里被去掉的包的版本）。"""

    remove: str
    add: tuple[str, ...]


VARIANT_SWAPS: dict[tuple[OcrEngineKind, OcrRuntimeVariant], VariantSwap] = {
    (PADDLE, OcrRuntimeVariant.CUDA): VariantSwap(
        "paddlepaddle",
        (f"paddlepaddle-gpu @ {PADDLE_GPU_INDEX}/paddlepaddle_gpu-{{version}}-cp313-cp313-win_amd64.whl",),
    ),
    (RAPID, OcrRuntimeVariant.DIRECTML): VariantSwap("onnxruntime", ("onnxruntime-directml==1.24.4",)),
    (RAPID, OcrRuntimeVariant.CUDA): VariantSwap("onnxruntime", ("onnxruntime-gpu[cuda,cudnn]=={version}",)),
}
"""各 GPU 版的推理库替换。版本核实于 PyPI 与 Paddle 官方源（cp313 win_amd64 wheel）：onnxruntime-directml 最高
1.24.4，onnxruntime-gpu 与 CPU 版同为 1.31.0，paddlepaddle-gpu cu129 有 3.3.1（与 CPU 版相同）。"""

OCR_OVERRIDES = ROOT / "ocr-overrides.txt"

RAPID_ROOTS = ("rapidocr", "onnxruntime")
"""RapidOCR 运行环境的根包：rapidocr 本身不声明推理库依赖，onnxruntime 要单独列出。"""

OCR_SITE_PACKAGES = ROOT / ".venv-ocr" / "Lib" / "site-packages"

UV_LICENSE_FILES = ("LICENSE-MIT", "LICENSE-APACHE")
"""uv 以 MIT 或 Apache-2.0 双许可发布。"""

UV_LICENSE_FALLBACK = ROOT / "packaging" / "licenses" / "uv"
"""uv.exe 旁边没有许可证文本时（独立安装脚本装的 uv 只有可执行文件），使用仓库里保存的一份。"""


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    log(" ".join(str(a) for a in args))
    return subprocess.run(args, check=True, text=True, encoding="utf-8", errors="replace", **kwargs)  # type: ignore[call-overload]


def mb(size: int) -> str:
    return f"{size / 1024 / 1024:.0f} MB"


def project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def checked_version(text: str) -> str:
    """校验版本号：必须能被更新服务的 parse_version 解析，并且原样就是规范写法。不合格时中止构建。

    规范写法不带前导 v，也不带构建元数据（parse_version 会去掉它们，安装包名就和版本号对不上了）。
    """
    parsed = parse_version(text)
    if parsed is None or parsed.text != text:
        hint = f"，规范写法是 {parsed.text}" if parsed is not None else ""
        sys.exit(
            f"pyproject.toml 的版本号 {text!r} 不是 SemVer 规范写法{hint}。"
            "预发布版本写成 0.2.0-beta.1 这样，不要写 PEP 440 的 0.2.0b1：应用内更新按 SemVer 匹配安装包"
        )
    return text


def managed_python_dir() -> Path:
    """uv 管理的独立版 CPython 3.13 的目录（取版本号最高的一个）。"""
    python_dir = Path(
        subprocess.run(["uv", "python", "dir"], check=True, capture_output=True, text=True).stdout.strip()
    )
    candidates = sorted(
        (d for d in python_dir.glob("cpython-3.13.*-windows-x86_64-none") if (d / "python.exe").is_file()),
        key=lambda d: tuple(int(x) for x in d.name.split("-")[1].split(".")),
    )
    if not candidates:
        sys.exit("找不到 uv 管理的 CPython 3.13，请先运行：uv python install 3.13")
    return candidates[-1]


def stage_runtime() -> Path:
    source = managed_python_dir()
    target = STAGE / "runtime"
    log(f"复制运行时 {source.name}")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    for relative in _RUNTIME_EXCLUDES:
        path = target / relative
        if path.is_dir():
            shutil.rmtree(path)
        elif path.is_file():
            path.unlink()
    (target / "Lib" / "site-packages").mkdir()
    return target / "python.exe"


def stage_site(python: Path, name: str, requirements: Path) -> None:
    target = STAGE / "site" / name
    target.mkdir(parents=True)
    run(["uv", "pip", "install", "--python", str(python), "--target", str(target), "--no-deps",
         "--link-mode", "copy", "-r", str(requirements)])  # fmt: skip
    # 可执行脚本（*.exe 包装器）指向构建机上的解释器路径，安装后无用。
    shutil.rmtree(target / "bin", ignore_errors=True)
    shutil.rmtree(target / "Scripts", ignore_errors=True)


def export_main_requirements() -> Path:
    path = BUILD / "requirements-main.txt"
    result = run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes",
                  "--format", "requirements-txt"], capture_output=True)  # fmt: skip
    path.write_text(result.stdout, encoding="utf-8")
    return path


def pinned_requirements(frozen: str) -> str:
    """把 ``uv pip freeze`` 的输出整理成只含 ``名称==版本`` 的清单，按名称排序。

    可编辑安装（-e）、直接引用（``名称 @ 地址``）没有办法从包索引取哈希，也不能在用户机器上重现，
    遇到就抛 ValueError。
    """
    check_single_opencv(frozen)
    pins: dict[str, str] = {}
    for raw in frozen.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-e") or " @ " in line or "==" not in line:
            raise ValueError(f".venv-ocr 里有无法按版本号重现的包：{line}")
        name, version = (part.strip() for part in line.split("==", 1))
        if not name or not version or any(c in version for c in " ;,<>!~="):
            raise ValueError(f"无法解析的版本行：{line}")
        pins[name.lower()] = f"{name}=={version}"
    if not pins:
        raise ValueError(".venv-ocr 里没有任何包")
    return "".join(f"{pins[key]}\n" for key in sorted(pins))


def requirements_header(hashed: bool, engine: str = "PaddleOCR") -> str:
    """版本清单开头的注释。engine 是引擎名，写进第一行。"""
    if hashed:
        note = "带 SHA-256，运行时以 --require-hashes 安装"
    else:
        note = "不带哈希（构建时用了 --allow-unhashed）"
    return (
        f"# {engine} 运行环境的版本清单，由 scripts/build_installer.py 按 .venv-ocr 的精确版本生成，\n"
        f"# {note}。\n"
        "# 安装：tools\\uv.exe pip install --python runtime\\python.exe --target <site> --no-deps\n"
        "#       -r <本文件>\n"
    )


def ocr_python() -> Path:
    """.venv-ocr 的解释器；不存在时中止构建。"""
    python = ROOT / ".venv-ocr" / "Scripts" / "python.exe"
    if not python.is_file():
        sys.exit("找不到 .venv-ocr，按 ocr-requirements.txt 的说明创建；版本清单按它的精确版本生成")
    return python


def write_requirements(pinned: str, name: str, engine: str, allow_unhashed: bool) -> tuple[Path, bool]:
    """给整理好的清单（pinned_requirements 的输出）补哈希，写到 build 下的 name，返回（路径，是否带哈希）。

    补哈希失败（不联网）时：allow_unhashed 为真就写入不带哈希的清单，否则中止构建。
    """
    stem = Path(name).stem
    pinned_path = BUILD / f"{stem}-pinned.txt"
    pinned_path.write_text(pinned, encoding="utf-8")
    output = BUILD / name
    compiled = BUILD / f"{stem}-hashed.txt"
    try:
        run(ocr_compile_args(pinned_path, compiled), capture_output=True)
    except subprocess.CalledProcessError as exc:
        if not allow_unhashed:
            sys.exit(
                f"无法为 {engine} 运行环境的版本清单生成哈希（需要访问包索引）：\n"
                f"{exc.stdout or ''}{exc.stderr or ''}\n联网后重试，或加 --allow-unhashed 写入不带哈希的清单"
            )
        log(f"无法生成哈希，按 --allow-unhashed 写入不带哈希的 {engine} 版本清单")
        text = requirements_header(False, engine) + pinned
        check_requirements(text, hashed=False)
        output.write_text(text, encoding="utf-8")
        return output, False
    text = requirements_header(True, engine) + compiled.read_text(encoding="utf-8")
    check_requirements(text, hashed=True)
    output.write_text(text, encoding="utf-8")
    return output, True


def paddle_pinned_requirements() -> str:
    """PaddleOCR CPU 版的清单（未补哈希）：.venv-ocr 的全部包。"""
    result = run(["uv", "pip", "freeze", "--python", str(ocr_python())], capture_output=True)
    try:
        return pinned_requirements(result.stdout)
    except ValueError as exc:
        sys.exit(str(exc))


def export_ocr_requirements(allow_unhashed: bool) -> tuple[Path, bool]:
    """按 .venv-ocr 当前安装的精确版本生成 PaddleOCR CPU 版运行环境的版本清单，返回（路径，是否带哈希）。"""
    return write_requirements(
        paddle_pinned_requirements(), OCR_REQUIREMENTS_NAME, "PaddleOCR", allow_unhashed
    )


def swap_requirements(pinned: str, swap: VariantSwap) -> tuple[str, list[str]]:
    """在 CPU 版清单上按 swap 替换推理库，返回（其余各包的清单，新加的需求行）。
    CPU 版里没有被替换的包时抛 ValueError。"""
    kept: list[str] = []
    version: str | None = None
    for line in pinned.splitlines():
        name, _, pin = line.partition("==")
        if normalize_name(name) == normalize_name(swap.remove):
            version = pin.strip()
            continue
        if line.strip():
            kept.append(line.strip())
    if version is None:
        raise ValueError(f"CPU 版清单里没有 {swap.remove}")
    added = [item.format(version=version) for item in swap.add]
    return "".join(f"{line}\n" for line in kept), added


def resolve_args(source: Path, output: Path) -> list[str]:
    """解析 GPU 版依赖的 uv pip compile 命令行：其余各包按精确版本、去掉 opencv-python-headless。"""
    return ["uv", "pip", "compile", str(source), "--no-config", "--only-binary", ":all:", "--no-header",
            "--no-annotate", "--override", str(OCR_OVERRIDES),
            "--python-version", "3.13", "--python-platform", "x86_64-pc-windows-msvc",
            "--output-file", str(output)]  # fmt: skip


def variant_requirements(
    cpu_pinned: str, kind: OcrEngineKind, variant: OcrRuntimeVariant, allow_unhashed: bool
) -> tuple[Path, bool]:
    """一个 GPU 版的清单：替换推理库、解析新依赖、补哈希，见模块说明第 3 步。返回（路径，是否带哈希）。"""
    name = requirements_name(kind, variant)
    title = f"{'PaddleOCR' if kind is PADDLE else 'RapidOCR'} {variant.value} 版"
    try:
        kept, added = swap_requirements(cpu_pinned, VARIANT_SWAPS[(kind, variant)])
    except ValueError as exc:
        sys.exit(str(exc))
    stem = Path(name).stem
    source = BUILD / f"{stem}-in.txt"
    source.write_text("".join(f"{line}\n" for line in added) + kept, encoding="utf-8")
    resolved = BUILD / f"{stem}-resolved.txt"
    try:
        run(resolve_args(source, resolved), capture_output=True)
    except subprocess.CalledProcessError as exc:
        if not allow_unhashed:
            sys.exit(
                f"无法解析 {title}运行环境的依赖（需要访问包索引）：\n{exc.stdout or ''}{exc.stderr or ''}\n"
                "联网后重试，或加 --allow-unhashed 写入运行时再解析依赖的清单"
            )
        log(f"无法解析依赖，按 --allow-unhashed 写入运行时再解析依赖的 {title}清单")
        text = requirements_header(False, title) + RESOLVE_MARKER + "\n" + source.read_text(encoding="utf-8")
        check_requirements(text, hashed=False)
        output = BUILD / name
        output.write_text(text, encoding="utf-8")
        return output, False
    pinned = resolved_requirements(resolved.read_text(encoding="utf-8"))
    return write_requirements(pinned, name, title, allow_unhashed)


def resolved_requirements(text: str) -> str:
    """uv pip compile 解析结果整理成每行一个包（``名称==版本`` 或 ``名称 @ 地址``），去掉注释与选项行。"""
    lines = [
        line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith(("#", "-"))
    ]
    if not lines:
        sys.exit("解析出的 GPU 版清单是空的")
    return "".join(
        f"{line}\n"
        for line in sorted(
            lines, key=lambda entry: normalize_name(entry.split()[0].split("==")[0].split("[")[0])
        )
    )


def export_variant_requirements(allow_unhashed: bool) -> dict[str, bool]:
    """生成全部引擎全部版本的清单，返回 文件名 → 是否带哈希。CPU 版先生成，GPU 版在它的基础上替换。"""
    results: dict[str, bool] = {}
    cpu_pins = {PADDLE: paddle_pinned_requirements(), RAPID: rapid_cpu_pinned()}
    for kind in (PADDLE, RAPID):
        title = "PaddleOCR" if kind is PADDLE else "RapidOCR"
        path, hashed = write_requirements(
            cpu_pins[kind], requirements_name(kind, CPU), f"{title} cpu 版", allow_unhashed
        )
        results[path.name] = hashed
        for variant in ENGINE_VARIANTS[kind]:
            if variant is CPU:
                continue
            path, hashed = variant_requirements(cpu_pins[kind], kind, variant, allow_unhashed)
            results[path.name] = hashed
    return results


@dataclass(frozen=True, slots=True)
class InstalledDistribution:
    """环境里装好的一个发行包：名称、版本与元数据里的 Requires-Dist。"""

    name: str
    version: str
    requires: tuple[str, ...] = ()


def normalize_name(name: str) -> str:
    """发行包名的规范写法（PEP 503）：小写，连续的 -、_、. 换成一个 -。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_distributions(site: Path) -> dict[str, InstalledDistribution]:
    """读取 site 目录里全部发行包的元数据（*.dist-info），键是规范包名。只读文件，不导入任何包。"""
    found: dict[str, InstalledDistribution] = {}
    for distribution in importlib.metadata.distributions(path=[str(site)]):
        name = distribution.metadata["Name"]
        if not name:
            continue
        found[normalize_name(name)] = InstalledDistribution(
            name, distribution.version, tuple(distribution.requires or ())
        )
    return found


def _resolve(
    installed: Mapping[str, InstalledDistribution], name: str, parent: str | None
) -> InstalledDistribution:
    """按包名找到装好的发行包。OpenCV 的几个发行包互相替代（见模块说明第 3 步），找不到时用装了的那一个。"""
    key = normalize_name(name)
    found = installed.get(key)
    if found is not None:
        return found
    if key.startswith("opencv-"):
        opencv = [dist for other, dist in installed.items() if other.startswith("opencv-")]
        if len(opencv) == 1:
            log(f"{parent} 依赖 {name}，.venv-ocr 装的是 {opencv[0].name}，用它代替")
            return opencv[0]
    if parent is None:
        raise ValueError(f".venv-ocr 里没有安装 {name}")
    raise ValueError(f".venv-ocr 里没有 {parent} 依赖的 {name}")


def dependency_closure(
    installed: Mapping[str, InstalledDistribution], roots: Iterable[str]
) -> list[InstalledDistribution]:
    """roots 及其运行时依赖的闭包，按规范包名排序。依赖缺失或元数据无法解析时抛 ValueError。

    环境标记按当前解释器求值，extra 视为空（可选依赖不计入）。
    """
    pending: list[tuple[str, str | None]] = [(root, None) for root in roots]
    chosen: dict[str, InstalledDistribution] = {}
    while pending:
        name, parent = pending.pop(0)
        dist = _resolve(installed, name, parent)
        key = normalize_name(dist.name)
        if key in chosen:
            continue
        chosen[key] = dist
        for text in dist.requires:
            try:
                requirement = Requirement(text)
            except InvalidRequirement as exc:
                raise ValueError(f"{dist.name} 的依赖 {text!r} 无法解析：{exc}") from exc
            if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
                continue
            pending.append((requirement.name, dist.name))
    return [chosen[key] for key in sorted(chosen)]


def rapid_pinned_requirements(installed: Mapping[str, InstalledDistribution]) -> str:
    """RapidOCR 运行环境的清单（未补哈希）：RAPID_ROOTS 的依赖闭包，格式与 pinned_requirements 相同。"""
    closure = dependency_closure(installed, RAPID_ROOTS)
    return pinned_requirements("".join(f"{dist.name}=={dist.version}\n" for dist in closure))


def rapid_cpu_pinned() -> str:
    """RapidOCR CPU 版的清单（未补哈希），见 rapid_pinned_requirements。"""
    ocr_python()
    try:
        pinned = rapid_pinned_requirements(installed_distributions(OCR_SITE_PACKAGES))
    except ValueError as exc:
        sys.exit(str(exc))
    log(f"RapidOCR 运行环境：{len(_requirement_entries(pinned))} 个包")
    return pinned


def export_rapid_requirements(allow_unhashed: bool) -> tuple[Path, bool]:
    """按 .venv-ocr 的精确版本生成 RapidOCR CPU 版运行环境的版本清单，返回（路径，是否带哈希）。"""
    return write_requirements(rapid_cpu_pinned(), RAPID_REQUIREMENTS_NAME, "RapidOCR", allow_unhashed)


def ocr_compile_args(pinned: Path, output: Path) -> list[str]:
    """给版本清单补哈希的 uv pip compile 命令行，见模块说明第 3 步。"""
    return ["uv", "pip", "compile", str(pinned), "--no-config", "--no-deps", "--generate-hashes",
            "--only-binary", ":all:", "--no-header",
            "--python-version", "3.13", "--python-platform", "x86_64-pc-windows-msvc",
            "--output-file", str(output)]  # fmt: skip


def _requirement_entries(text: str) -> list[str]:
    """版本清单里的每个包一项：去掉注释与空行，把行尾反斜杠续行的部分接回同一项。"""
    entries: list[str] = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        current += " " + line.removesuffix("\\").strip()
        if not line.endswith("\\"):
            entries.append(current.strip())
            current = ""
    if current:
        entries.append(current.strip())
    return entries


def check_requirements(text: str, *, hashed: bool) -> int:
    """检查版本清单与构建参数一致，返回包的个数；不一致时中止构建。

    hashed 为真时每个包都要带 ``--hash=sha256:``：运行时看到任何一个哈希就加 --require-hashes，
    缺哈希的包会让整个安装失败。hashed 为假时一个哈希也不能有，否则运行时会按带哈希的方式安装。
    每个包都要钉住版本：``名称==版本``，或指向 wheel 的直接地址 ``名称 @ https://….whl``。
    带哈希的清单不能带 RESOLVE_MARKER（安装时解析出的依赖没有哈希，--require-hashes 会失败）。
    """
    entries = _requirement_entries(text)
    if not entries:
        sys.exit("OCR 运行环境的版本清单里没有任何包")
    for entry in entries:
        head = entry.split(" --hash=")[0].strip()
        pinned = "==" in head or (" @ " in head and head.endswith(".whl"))
        if not pinned or head.startswith("-"):
            sys.exit(f"版本清单里这一项没有钉住版本：{head}")
    if hashed and RESOLVE_MARKER in text:
        sys.exit("带哈希的版本清单不能要求安装时解析依赖")
    with_hash = [entry for entry in entries if "--hash=sha256:" in entry]
    if hashed and len(with_hash) != len(entries):
        missing = [entry.split()[0] for entry in entries if entry not in with_hash]
        sys.exit(f"版本清单里这些包没有哈希：{', '.join(missing)}")
    if not hashed and with_hash:
        sys.exit("按不带哈希构建，版本清单里却出现了哈希")
    return len(entries)


def find_uv() -> Path:
    """开发机上 ``uv`` 命令对应的可执行文件。"""
    found = shutil.which("uv")
    if found is None:
        sys.exit("找不到 uv")
    return Path(found).resolve()


def uv_license_dir(uv: Path) -> Path:
    """uv 的许可证文本所在的目录：优先用 uv.exe 旁边的，没有时用仓库里保存的一份。"""
    if any((uv.parent / name).is_file() for name in UV_LICENSE_FILES):
        return uv.parent
    return UV_LICENSE_FALLBACK


def stage_tools(uv: Path) -> Path:
    """复制 uv.exe 与它的许可证到 tools\\，返回暂存目录里 uv.exe 的路径。"""
    tools = STAGE / "tools"
    licenses = tools / "licenses" / "uv"
    licenses.mkdir(parents=True)
    target = tools / "uv.exe"
    shutil.copy2(uv, target)
    source = uv_license_dir(uv)
    copied = [name for name in UV_LICENSE_FILES if (source / name).is_file()]
    if not copied:
        sys.exit(f"找不到 uv 的许可证文本（{', '.join(UV_LICENSE_FILES)}）")
    for name in copied:
        shutil.copy2(source / name, licenses / name)
    log(f"复制 {uv}（许可证来自 {source}：{', '.join(copied)}）")
    return target


def sha256_line(path: Path) -> str:
    """sha256sum 格式的一行：64 位小写十六进制、两个空格、文件名，以换行结尾。"""
    return f"{file_sha256(path)}  {path.name}\n"


def write_sha256_file(path: Path) -> Path:
    """在 path 旁边写 ``<文件名>.sha256``，返回校验文件的路径。"""
    checksum = path.with_name(path.name + ".sha256")
    checksum.write_text(sha256_line(path), encoding="utf-8", newline="\n")
    return checksum


def check_single_opencv(frozen: str) -> None:
    """OpenCV 的几个发行包共用 cv2 目录，同时安装时文件互相覆盖，导入失败（见 ocr-overrides.txt）。"""
    names = [line.split("==")[0].strip().lower() for line in frozen.splitlines() if "==" in line]
    opencv = sorted(n for n in names if n.startswith("opencv-"))
    if len(opencv) > 1:
        sys.exit(
            f".venv-ocr 里装了多个 OpenCV 发行包：{', '.join(opencv)}。"
            "按 ocr-requirements.txt 的说明加 --override ocr-overrides.txt 重新安装"
        )


def stage_app(python: Path, version: str, requirements: Iterable[str]) -> None:
    app = STAGE / "app"
    shutil.copytree(ROOT / "lst", app / "lst", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    icons = app / "assets" / "icons"
    icons.mkdir(parents=True)
    shutil.copy2(ROOT / "assets" / "icons" / "lsttranslate.ico", icons / "lsttranslate.ico")
    # 词典数据的第三方声明：界面上的署名指向这两项（CC BY-SA 4.0 要求署名与许可证随数据一起分发）
    shutil.copy2(ROOT / "THIRD-PARTY-NOTICES", app / "THIRD-PARTY-NOTICES")
    shutil.copytree(ROOT / "licenses", app / "licenses")
    (app / "installed.json").write_text(
        json.dumps({"version": version, "built": datetime.now(UTC).isoformat(timespec="seconds")}, indent=2),
        encoding="utf-8",
    )
    for name in requirements:
        shutil.copy2(BUILD / name, app / name)
    shutil.copytree(
        ROOT / "packaging" / "launcher", STAGE / "launcher", ignore=shutil.ignore_patterns("__pycache__")
    )
    run([str(python), "-I", "-m", "compileall", "-q", "-j", "0", str(app / "lst")])


def smoke_code(app: Path, site: Path, modules: tuple[str, ...]) -> str:
    """自检脚本：按安装布局设好模块搜索路径，导入 modules，确认安装版标记生效、keyring 用 Windows 凭据管理器。

    只读取 keyring 选中的后端类型，不读写任何凭据。
    """
    return (
        "import sys, importlib\n"
        f"sys.path[:0] = [{str(app)!r}, {str(site)!r}]\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "import lst.app.paths as p\n"
        "assert p.is_installed(), 'installed.json 未生效'\n"
        "import keyring\n"
        "from keyring.backends.Windows import WinVaultKeyring\n"
        "backend = keyring.get_keyring()\n"
        "assert isinstance(backend, WinVaultKeyring), f'keyring 后端是 {type(backend).__name__}'\n"
        f"print('smoke ok: {site.name}')\n"
    )


def _smoke_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV", "LST_"))}
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"  # 导入 PaddleX 时不联网检查模型源
    return env


def smoke_test(python: Path, site: str, modules: tuple[str, ...]) -> None:
    """用安装布局导入一组模块；失败说明暂存目录不完整，构建中止。"""
    code = smoke_code(STAGE / "app", STAGE / "site" / site, modules)
    run([str(python), "-I", "-X", "utf8", "-c", code], env=_smoke_env())


def launcher_smoke_commands() -> list[list[str]]:
    """通过启动器运行的自检命令。都不带 --address：进程入口导入完成、解析参数时以 CONFIG_ERROR 退出。"""
    return [["nmt"], ["ocr", "--ocr-site", "main"]]


def smoke_test_launcher(python: Path) -> None:
    """用真实的 launcher\\worker.py 启动推理进程入口，检查启动器设的模块搜索路径能导入进程入口。"""
    worker = STAGE / "launcher" / "worker.py"
    for command in launcher_smoke_commands():
        args = [str(python), "-I", "-X", "utf8", str(worker), *command]
        log(" ".join(args))
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_smoke_env(),
            check=False,
        )
        output = result.stdout + result.stderr
        if result.returncode != LAUNCHER_SMOKE_EXIT or "--address" not in output:
            sys.exit(f"启动器自检失败（{' '.join(command)}，退出码 {result.returncode}）：\n{output}")
        log(f"smoke ok: launcher {command[0]}")


def smoke_test_requirements(name: str, hashed: bool) -> None:
    """暂存目录里的版本清单格式正确（每个包一项 ``名称==版本``），带不带哈希与构建结果一致。"""
    text = (STAGE / "app" / name).read_text(encoding="utf-8")
    count = check_requirements(text, hashed=hashed)
    log(f"smoke ok: {name}（{count} 个包，{'带' if hashed else '不带'}哈希）")


def smoke_test_uv(uv: Path) -> None:
    """安装布局里的 uv.exe 能运行。"""
    result = run([str(uv), "--version"], capture_output=True)
    log(f"smoke ok: {result.stdout.strip()}")


def build_installer(version: str) -> Path:
    if not ISCC.is_file():
        sys.exit(f"找不到 Inno Setup 编译器：{ISCC}")
    DIST.mkdir(exist_ok=True)
    run([str(ISCC), "/Q", f"/DAppVersion={version}", f"/DStageDir={STAGE}", f"/DOutputDir={DIST}",
         str(ROOT / "installer" / "lsttranslate.iss")])  # fmt: skip
    return DIST / f"LSTtranslate-{version}-setup.exe"


def main() -> None:
    # 输出重定向到管道或文件时，Windows 上标准输出默认用 ANSI 代码页，打印中文会抛 UnicodeEncodeError。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description="构建 LSTtranslate 安装包")
    parser.add_argument(
        "--allow-unhashed",
        action="store_true",
        help="无法联网生成哈希时，写入不带哈希的 OCR 运行环境版本清单",
    )
    parser.add_argument("--skip-iscc", action="store_true", help="只准备暂存目录，不编译安装包")
    args = parser.parse_args()

    started = time.monotonic()
    version = checked_version(project_version())
    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)

    python = stage_runtime()
    stage_site(python, "main", export_main_requirements())
    manifests = export_variant_requirements(args.allow_unhashed)
    uv = stage_tools(find_uv())
    stage_app(python, version, manifests)
    smoke_test(python, "main", SMOKE_IMPORTS)
    smoke_test_launcher(python)
    for name, hashed in manifests.items():
        smoke_test_requirements(name, hashed)
    smoke_test_uv(uv)

    for part in ("runtime", "site/main", "tools", "app"):
        path = STAGE / part
        if path.exists():
            log(f"{part}: {mb(dir_size(path))}")
    log(f"暂存目录合计：{mb(dir_size(STAGE))}")

    if not args.skip_iscc:
        installer = build_installer(version)
        log(f"安装包：{installer}（{mb(installer.stat().st_size)}）")
        log(f"校验文件：{write_sha256_file(installer)}")
    log(f"完成，用时 {time.monotonic() - started:.0f} 秒")


if __name__ == "__main__":
    main()
