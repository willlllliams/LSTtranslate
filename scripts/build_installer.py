r"""构建安装包（需求 INS-01 至 INS-08，架构文档第 15 章）。

用法（仓库根目录）：
    uv run python scripts/build_installer.py [--skip-ocr] [--skip-iscc]

步骤：
1. 复制 uv 管理的独立版 CPython 3.13 到 build\stage\runtime\，去掉 pip、tcl/tkinter、IDLE、头文件等；
2. 按 uv.lock 导出主进程依赖，安装到 build\stage\site\main\；
3. 按 .venv-ocr 当前的精确版本安装 OCR 依赖到 build\stage\site\ocr\（--skip-ocr 时跳过）；
4. 复制 lst 包、图标、启动器，预编译 .pyc，写入安装版标记 app\installed.json；
5. 在暂存目录里用安装布局自检：实际导入主进程的关键模块与 OCR 进程的第三方包；
6. 调用 Inno Setup 编译 installer\lsttranslate.iss，输出 dist\LSTtranslate-<版本>-setup.exe。

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
_SMOKE_IMPORTS = (
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
    "lst.__main__",
    "lst.workers.nmt_worker",
)

# OCR 进程的依赖：只检查第三方包，OCR 进程的代码随 app\lst 一起由主进程自检覆盖。
_SMOKE_IMPORTS_OCR = ("paddle", "paddleocr", "rapidocr", "onnxruntime")


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    log(" ".join(str(a) for a in args))
    return subprocess.run(args, check=True, text=True, encoding="utf-8", errors="replace", **kwargs)  # type: ignore[call-overload]


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def mb(size: int) -> str:
    return f"{size / 1024 / 1024:.0f} MB"


def project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


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


def export_ocr_requirements() -> Path:
    """OCR 环境按 .venv-ocr 当前安装的精确版本冻结，安装版与开发环境用同样的版本。"""
    ocr_python = ROOT / ".venv-ocr" / "Scripts" / "python.exe"
    if not ocr_python.is_file():
        sys.exit("找不到 .venv-ocr，按 ocr-requirements.txt 的说明创建，或用 --skip-ocr 跳过 OCR 环境")
    result = run(["uv", "pip", "freeze", "--python", str(ocr_python)], capture_output=True)
    check_single_opencv(result.stdout)
    path = BUILD / "requirements-ocr.txt"
    path.write_text(result.stdout, encoding="utf-8")
    return path


def check_single_opencv(frozen: str) -> None:
    """OpenCV 的几个发行包共用 cv2 目录，同时安装时文件互相覆盖，导入失败（见 ocr-overrides.txt）。"""
    names = [line.split("==")[0].strip().lower() for line in frozen.splitlines() if "==" in line]
    opencv = sorted(n for n in names if n.startswith("opencv-"))
    if len(opencv) > 1:
        sys.exit(
            f".venv-ocr 里装了多个 OpenCV 发行包：{', '.join(opencv)}。"
            "按 ocr-requirements.txt 的说明加 --override ocr-overrides.txt 重新安装"
        )


def stage_app(python: Path, version: str) -> None:
    app = STAGE / "app"
    shutil.copytree(ROOT / "lst", app / "lst", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    icons = app / "assets" / "icons"
    icons.mkdir(parents=True)
    shutil.copy2(ROOT / "assets" / "icons" / "lsttranslate.ico", icons / "lsttranslate.ico")
    (app / "installed.json").write_text(
        json.dumps({"version": version, "built": datetime.now(UTC).isoformat(timespec="seconds")}, indent=2),
        encoding="utf-8",
    )
    shutil.copytree(ROOT / "packaging" / "launcher", STAGE / "launcher")
    run([str(python), "-I", "-m", "compileall", "-q", "-j", "0", str(app / "lst")])


def smoke_test(python: Path, site: str, modules: tuple[str, ...]) -> None:
    """用安装布局导入一组模块；失败说明暂存目录不完整，构建中止。"""
    code = (
        "import sys, importlib;"
        f"sys.path[:0] = [{str(STAGE / 'app')!r}, {str(STAGE / 'site' / site)!r}];"
        f"[importlib.import_module(m) for m in {modules!r}];"
        "import lst.app.paths as p; assert p.is_installed(), 'installed.json 未生效';"
        f"print('smoke ok: {site}')"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))}
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"  # 导入 PaddleX 时不联网检查模型源
    run([str(python), "-I", "-X", "utf8", "-c", code], env=env)


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
        "--skip-ocr", action="store_true", help="不打包 OCR 环境（快速构建，安装后截屏 OCR 不可用）"
    )
    parser.add_argument("--skip-iscc", action="store_true", help="只准备暂存目录，不编译安装包")
    args = parser.parse_args()

    started = time.monotonic()
    version = project_version()
    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)

    python = stage_runtime()
    stage_site(python, "main", export_main_requirements())
    if not args.skip_ocr:
        stage_site(python, "ocr", export_ocr_requirements())
    stage_app(python, version)
    smoke_test(python, "main", _SMOKE_IMPORTS)
    if not args.skip_ocr:
        smoke_test(python, "ocr", _SMOKE_IMPORTS_OCR)

    for part in ("runtime", "site/main", "site/ocr", "app"):
        path = STAGE / part
        if path.exists():
            log(f"{part}: {mb(dir_size(path))}")
    log(f"暂存目录合计：{mb(dir_size(STAGE))}")

    if not args.skip_iscc:
        installer = build_installer(version)
        log(f"安装包：{installer}（{mb(installer.stat().st_size)}）")
    log(f"完成，用时 {time.monotonic() - started:.0f} 秒")


if __name__ == "__main__":
    main()
