"""模块依赖规则检查（架构文档 4.2 节）。

规则写在 pyproject.toml 的 [tool.importlinter] 中，这里调用 import-linter 执行，
让 `pytest` 一条命令就覆盖依赖规则。
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_import_contracts_hold() -> None:
    lint_imports = Path(sys.executable).with_name("lint-imports.exe")
    result = subprocess.run(
        [str(lint_imports), "--no-cache"],
        cwd=ROOT,
        # 契约名是中文；不开 UTF-8 模式时，子进程按 cp1252 输出会报编码错误。
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
