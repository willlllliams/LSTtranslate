r"""安装版的推理子进程与维护命令启动器（架构文档第 15 章）。

用法：`runtime\python.exe -I -X utf8 launcher\worker.py <命令> [启动器参数] [进程参数...]`
- nmt：NMT 进程，依赖在 site\main\；
- ocr：OCR 进程。依赖所在的目录由启动器参数 ``--ocr-site <目录>`` 指定，其余参数原样传给
  ``python -m lst.workers.ocr_worker``（包括 --backend、--address、--log-level）：
  - PaddleOCR：``--ocr-site <数据目录>\engines\paddle\site``，即 services.ocr_engines 用 uv 装好的运行环境；
    只把 app\ 与这个目录放进模块搜索路径，PaddlePaddle 与主进程的依赖不在同一个进程里加载；
  - Windows OCR：``--ocr-site main``，用主环境 site\main（pywinrt 装在主环境里），
    并传 ``--backend lst.workers.ocr_worker.winocr_backend:WinOcrBackend``；
  - 没有 ``--ocr-site`` 时取环境变量 LST_OCR_SITE；也没有时以退出码 65（ExitCode.DEPENDENCY_ERROR）退出，
    监管器不再重启；
- purge-secrets：卸载时删除凭据管理器中本程序保存的 API key。全部删掉（或本来就没有）时退出码为 0，
  枚举或删除失败时为 1，卸载程序把退出码记进日志。

启动器在设好模块搜索路径之前不能导入 lst，退出码直接写数字（值见 lst/contracts/worker_protocol.py）。
"""

import os
import runpy
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_COMMANDS = ("nmt", "ocr", "purge-secrets")
_MODULES = {"nmt": "lst.workers.nmt_worker", "ocr": "lst.workers.ocr_worker"}
OCR_SITE_OPTION = "--ocr-site"
OCR_SITE_ENV = "LST_OCR_SITE"
MAIN_SITE = "main"
EXIT_USAGE = 66  # 启动参数无效，与 ExitCode.CONFIG_ERROR 相同
EXIT_DEPENDENCY = 65
EXIT_PURGE_FAILED = 1


def split_ocr_site(args: list[str]) -> tuple[str | None, list[str]]:
    """从参数里取出 ``--ocr-site <值>`` 或 ``--ocr-site=<值>``，返回（值，其余参数）。"""
    rest: list[str] = []
    value: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == OCR_SITE_OPTION:
            if index + 1 >= len(args):
                raise ValueError(f"{OCR_SITE_OPTION} 缺少目录")
            value = args[index + 1]
            index += 2
            continue
        if arg.startswith(OCR_SITE_OPTION + "="):
            value = arg.split("=", 1)[1]
        else:
            rest.append(arg)
        index += 1
    return value, rest


def resolve_ocr_site(value: str | None, environ: Mapping[str, str], root: Path) -> Path | None:
    """OCR 进程的依赖目录：参数 → 环境变量；"main" 表示主环境 site\\main；都没有时返回 None。"""
    chosen = value or environ.get(OCR_SITE_ENV) or None
    if chosen is None:
        return None
    if chosen == MAIN_SITE:
        return root / "site" / "main"
    return Path(chosen)


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in _COMMANDS:
        sys.stderr.write(f"用法：worker.py {{{'|'.join(_COMMANDS)}}} [参数...]\n")
        sys.exit(EXIT_USAGE)
    command = sys.argv[1]
    args = sys.argv[2:]
    if command == "ocr":
        try:
            value, args = split_ocr_site(args)
        except ValueError as exc:
            sys.stderr.write(f"{exc}\n")
            sys.exit(EXIT_USAGE)
        site = resolve_ocr_site(value, os.environ, ROOT)
        if site is None or not site.is_dir():
            sys.stderr.write(f"OCR 运行环境不存在：{site or '未指定 --ocr-site'}\n")
            sys.exit(EXIT_DEPENDENCY)
    else:
        site = ROOT / "site" / "main"
    sys.path[:0] = [str(ROOT / "app"), str(site)]

    if command == "purge-secrets":
        from lst.app.uninstall import purge_secrets

        result = purge_secrets()
        sys.exit(0 if result.ok else EXIT_PURGE_FAILED)

    sys.argv = [_MODULES[command], *args]
    runpy.run_module(_MODULES[command], run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
