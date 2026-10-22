r"""构建安装包（需求 INS-01 至 INS-08，架构文档第 15 章）。

用法（仓库根目录）：
    uv run python scripts/build_installer.py [--allow-unhashed] [--skip-iscc]

安装包不捆绑 PaddleOCR 的运行环境与模型：两者由 services.ocr_engines 在用户点“安装”时下载，
运行环境用随安装包分发的 tools\uv.exe 按 app\ocr-paddle-requirements.txt 安装。

步骤：
1. 复制 uv 管理的独立版 CPython 3.13 到 build\stage\runtime\，去掉 pip、tcl/tkinter、IDLE、头文件等；
2. 按 uv.lock 导出主进程依赖，安装到 build\stage\site\main\；
3. 按 .venv-ocr 当前的精确版本生成 PaddleOCR 运行环境的版本清单 app\ocr-paddle-requirements.txt：
   ``uv pip freeze`` 冻结版本，再用 ``uv pip compile --no-deps --generate-hashes`` 补上每个包的 SHA-256。
   compile 加 ``--no-config``（不读构建机上的 uv 配置）与 ``--only-binary :all:``（只收录 wheel 的哈希，
   与运行时安装的 ``--no-build`` 对应）。
   补哈希要访问包索引；不联网时这一步失败，构建中止。加 --allow-unhashed 时改为写入不带哈希的清单，
   运行时安装因此不加 --require-hashes；
4. 复制开发机上的 uv.exe 与它的许可证到 build\stage\tools\；
5. 复制 lst 包、图标、启动器，预编译 .pyc，写入安装版标记 app\installed.json；
6. 在暂存目录里用安装布局自检：实际导入主进程的关键模块（含 Windows OCR 的 winrt 与后端），确认 keyring
   的后端是 Windows 凭据管理器，通过真实的 launcher\worker.py 启动一次 nmt 与 ocr 进程入口（不带 --address，
   进程在解析参数时退出，不联网），检查版本清单带不带哈希与构建参数一致，并运行 ``tools\uv.exe --version``；
7. 调用 Inno Setup 编译 installer\lsttranslate.iss，输出 dist\LSTtranslate-<版本>-setup.exe，
   并在旁边写 sha256sum 格式的校验文件 LSTtranslate-<版本>-setup.exe.sha256（应用内更新用它校验下载）。

构建开头用更新服务的 parse_version 校验 pyproject.toml 里的版本号：必须是 SemVer 规范写法
（例如 0.2.0、0.2.0-beta.1），否则中止。PEP 440 写法（0.2.0b1）会让安装包名与更新服务按 tag 拼出的
文件名对不上，应用内更新因此找不到安装包。

暂存目录与输出目录都不纳入版本库。不使用 tempfile（见架构文档 2.9.5 节）。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # 在仓库之外运行（例如 python C:\repo\scripts\build_installer.py）时也能导入 lst。
    sys.path.insert(0, str(ROOT))

from lst.services._shared.download import dir_size, file_sha256  # noqa: E402
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

OCR_REQUIREMENTS_NAME = "ocr-paddle-requirements.txt"
"""PaddleOCR 运行环境的版本清单在安装目录 app\\ 下的文件名，组合根按它构造 services.ocr_engines 的路径。"""

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


def requirements_header(hashed: bool) -> str:
    """版本清单开头的注释。"""
    if hashed:
        note = "带 SHA-256，运行时以 --require-hashes 安装"
    else:
        note = "不带哈希（构建时用了 --allow-unhashed）"
    return (
        "# PaddleOCR 运行环境的版本清单，由 scripts/build_installer.py 按 .venv-ocr 的精确版本生成，\n"
        f"# {note}。\n"
        "# 安装：tools\\uv.exe pip install --python runtime\\python.exe --target <site> --no-deps\n"
        "#       -r <本文件>\n"
    )


def export_ocr_requirements(allow_unhashed: bool) -> tuple[Path, bool]:
    """按 .venv-ocr 当前安装的精确版本生成运行环境的版本清单，写到 build\\ 下，返回（路径，是否带哈希）。"""
    ocr_python = ROOT / ".venv-ocr" / "Scripts" / "python.exe"
    if not ocr_python.is_file():
        sys.exit("找不到 .venv-ocr，按 ocr-requirements.txt 的说明创建；版本清单按它的精确版本生成")
    result = run(["uv", "pip", "freeze", "--python", str(ocr_python)], capture_output=True)
    try:
        pinned = pinned_requirements(result.stdout)
    except ValueError as exc:
        sys.exit(str(exc))
    pinned_path = BUILD / "requirements-ocr-pinned.txt"
    pinned_path.write_text(pinned, encoding="utf-8")
    output = BUILD / OCR_REQUIREMENTS_NAME
    compiled = BUILD / "requirements-ocr-hashed.txt"
    try:
        run(ocr_compile_args(pinned_path, compiled), capture_output=True)
    except subprocess.CalledProcessError as exc:
        if not allow_unhashed:
            sys.exit(
                "无法为 OCR 运行环境的版本清单生成哈希（需要访问包索引）：\n"
                f"{exc.stdout or ''}{exc.stderr or ''}\n联网后重试，或加 --allow-unhashed 写入不带哈希的清单"
            )
        log("无法生成哈希，按 --allow-unhashed 写入不带哈希的版本清单")
        text = requirements_header(False) + pinned
        check_requirements(text, hashed=False)
        output.write_text(text, encoding="utf-8")
        return output, False
    text = requirements_header(True) + compiled.read_text(encoding="utf-8")
    check_requirements(text, hashed=True)
    output.write_text(text, encoding="utf-8")
    return output, True


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
    """
    entries = _requirement_entries(text)
    if not entries:
        sys.exit("OCR 运行环境的版本清单里没有任何包")
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


def stage_app(python: Path, version: str, ocr_requirements: Path) -> None:
    app = STAGE / "app"
    shutil.copytree(ROOT / "lst", app / "lst", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    icons = app / "assets" / "icons"
    icons.mkdir(parents=True)
    shutil.copy2(ROOT / "assets" / "icons" / "lsttranslate.ico", icons / "lsttranslate.ico")
    (app / "installed.json").write_text(
        json.dumps({"version": version, "built": datetime.now(UTC).isoformat(timespec="seconds")}, indent=2),
        encoding="utf-8",
    )
    shutil.copy2(ocr_requirements, app / OCR_REQUIREMENTS_NAME)
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


def smoke_test_requirements(hashed: bool) -> None:
    """暂存目录里的版本清单带不带哈希与构建参数一致。"""
    text = (STAGE / "app" / OCR_REQUIREMENTS_NAME).read_text(encoding="utf-8")
    count = check_requirements(text, hashed=hashed)
    log(f"smoke ok: {OCR_REQUIREMENTS_NAME}（{count} 个包，{'带' if hashed else '不带'}哈希）")


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
    ocr_requirements, hashed = export_ocr_requirements(args.allow_unhashed)
    uv = stage_tools(find_uv())
    stage_app(python, version, ocr_requirements)
    smoke_test(python, "main", SMOKE_IMPORTS)
    smoke_test_launcher(python)
    smoke_test_requirements(hashed)
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
