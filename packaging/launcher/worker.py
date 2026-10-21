r"""安装版的推理子进程与维护命令启动器（架构文档第 15 章）。

用法：`runtime\python.exe -I -X utf8 launcher\worker.py <命令> [参数...]`
- nmt：NMT 进程，依赖在 site\main\；
- ocr：OCR 进程，依赖在 site\ocr\（PaddlePaddle 等，与主进程的依赖分开，避免 DLL 冲突）；
- purge-secrets：卸载时删除凭据管理器中本程序保存的 API key。
"""

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SITES = {"nmt": "main", "ocr": "ocr", "purge-secrets": "main"}
_MODULES = {"nmt": "lst.workers.nmt_worker", "ocr": "lst.workers.ocr_worker"}

if len(sys.argv) < 2 or sys.argv[1] not in _SITES:
    sys.stderr.write(f"用法：worker.py {{{'|'.join(_SITES)}}} [参数...]\n")
    sys.exit(66)

command = sys.argv[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "site" / _SITES[command])]

if command == "purge-secrets":
    from lst.app.uninstall import purge_secrets

    purge_secrets()
    sys.exit(0)

sys.argv = [_MODULES[command], *sys.argv[2:]]
runpy.run_module(_MODULES[command], run_name="__main__", alter_sys=True)
