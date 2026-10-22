r"""packaging/launcher/worker.py：OCR 子命令按 --ocr-site 选择依赖目录，其余参数原样透传。

端到端的测试在临时目录里搭一个安装布局：launcher\worker.py 是仓库里的副本，app\lst 是一个假的
lst.workers.ocr_worker，它把收到的命令行与模块搜索路径打印出来。子进程以 -I 隔离模式运行，与安装版相同。
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "packaging" / "launcher" / "worker.py"


def _load_launcher() -> ModuleType:
    """按文件路径导入启动器。PyPI 上的 packaging 包会遮住仓库的 packaging 目录，不能按包名导入。"""
    spec = importlib.util.spec_from_file_location("lst_launcher_worker", LAUNCHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_launcher = _load_launcher()
split_ocr_site = cast(Callable[[list[str]], tuple[str | None, list[str]]], _launcher.split_ocr_site)
resolve_ocr_site = cast(
    Callable[[str | None, Mapping[str, str], Path], Path | None], _launcher.resolve_ocr_site
)
EXIT_DEPENDENCY = cast(int, _launcher.EXIT_DEPENDENCY)
EXIT_USAGE = cast(int, _launcher.EXIT_USAGE)
EXIT_PURGE_FAILED = cast(int, getattr(_launcher, "EXIT_PURGE_FAILED", 1))

_FAKE_WORKER = """\
import json, sys
print(json.dumps({"argv": sys.argv[1:], "path": sys.path[:2]}))
"""


def test_split_ocr_site() -> None:
    assert split_ocr_site(["--ocr-site", "D:/s", "--backend", "x:y"]) == ("D:/s", ["--backend", "x:y"])
    assert split_ocr_site(["--log-level", "INFO", "--ocr-site=main"]) == ("main", ["--log-level", "INFO"])
    assert split_ocr_site(["--address", "a"]) == (None, ["--address", "a"])
    with pytest.raises(ValueError):
        split_ocr_site(["--ocr-site"])


def test_resolve_ocr_site(tmp_path: Path) -> None:
    assert resolve_ocr_site("main", {}, tmp_path) == tmp_path / "site" / "main"
    assert resolve_ocr_site("D:/x", {}, tmp_path) == Path("D:/x")
    assert resolve_ocr_site(None, {"LST_OCR_SITE": "E:/y"}, tmp_path) == Path("E:/y")
    assert resolve_ocr_site(None, {}, tmp_path) is None
    # 旧版安装包捆绑的 site\ocr 不再作为回退：升级时 [InstallDelete] 已经删掉整个 site 目录。
    (tmp_path / "site" / "ocr").mkdir(parents=True)
    assert resolve_ocr_site(None, {}, tmp_path) is None


@pytest.fixture
def layout(tmp_path: Path) -> Path:
    install = tmp_path / "LSTtranslate"
    (install / "launcher").mkdir(parents=True)
    shutil.copy2(LAUNCHER, install / "launcher" / "worker.py")
    package = install / "app" / "lst" / "workers" / "ocr_worker"
    package.mkdir(parents=True)
    for directory in (install / "app" / "lst", install / "app" / "lst" / "workers", package):
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(_FAKE_WORKER, encoding="utf-8")
    (install / "site" / "main").mkdir(parents=True)
    return install


def run_launcher(
    install: Path, *args: str, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environ = {k: v for k, v in os.environ.items() if k != "LST_OCR_SITE"}
    environ.update(env or {})
    return subprocess.run(
        [sys.executable, "-I", "-X", "utf8", str(install / "launcher" / "worker.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environ,
        check=False,
        timeout=30,
    )


def test_ocr_with_paddle_site(layout: Path, tmp_path: Path) -> None:
    site = tmp_path / "data" / "engines" / "paddle" / "site"
    site.mkdir(parents=True)
    result = run_launcher(layout, "ocr", "--ocr-site", str(site), "--log-level", "WARNING", "--address", "p")
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["argv"] == ["--log-level", "WARNING", "--address", "p"]
    assert output["path"] == [str(layout / "app"), str(site)]


def test_ocr_windows_backend_uses_main_site(layout: Path) -> None:
    backend = "lst.workers.ocr_worker.winocr_backend:WinOcrBackend"
    result = run_launcher(layout, "ocr", "--ocr-site", "main", "--backend", backend)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["argv"] == ["--backend", backend]
    assert output["path"] == [str(layout / "app"), str(layout / "site" / "main")]


def test_ocr_without_runtime_exits_with_dependency_error(layout: Path, tmp_path: Path) -> None:
    assert run_launcher(layout, "ocr").returncode == EXIT_DEPENDENCY
    missing = tmp_path / "missing"
    assert run_launcher(layout, "ocr", "--ocr-site", str(missing)).returncode == EXIT_DEPENDENCY


def test_unknown_command_is_usage_error(layout: Path) -> None:
    assert run_launcher(layout, "bogus").returncode == EXIT_USAGE


_FAKE_UNINSTALL = """\
import os


class _Result:
    ok = os.environ.get("FAKE_PURGE_OK") == "1"


def purge_secrets():
    print("purge called")
    return _Result()
"""


@pytest.mark.parametrize(("ok", "code"), [("1", 0), ("0", EXIT_PURGE_FAILED)])
def test_purge_secrets_exit_code_follows_result(layout: Path, ok: str, code: int) -> None:
    # app\lst\app\uninstall.py 换成假的：不碰真实的凭据管理器，只检查启动器按清理结果给出退出码。
    package = layout / "app" / "lst" / "app"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "uninstall.py").write_text(_FAKE_UNINSTALL, encoding="utf-8")
    result = run_launcher(layout, "purge-secrets", env={"FAKE_PURGE_OK": ok})
    assert "purge called" in result.stdout
    assert result.returncode == code
    assert EXIT_PURGE_FAILED != 0
