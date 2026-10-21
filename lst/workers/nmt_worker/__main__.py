"""NMT 进程入口：`python -m lst.workers.nmt_worker --address <地址> [--backend module:attr]`。

authkey 以十六进制从标准输入第一行传入。--backend 默认是 CTranslate2 后端，测试用它换成假后端。
"""

import argparse
import importlib
import logging
from typing import cast

from lst.contracts.worker_protocol import ExitCode
from lst.workers._runtime import exit_process, parse_args, run_worker, setup_process
from lst.workers.nmt_worker.backend import NmtBackend
from lst.workers.nmt_worker.handler import TranslateHandler

DEFAULT_BACKEND = "lst.workers.nmt_worker.ct2_backend:Ct2Backend"

log = logging.getLogger("lst.workers.nmt")


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", default=DEFAULT_BACKEND, help="后端工厂，格式为 module:attr")


def load_backend(spec: str) -> NmtBackend:
    """按 `module:attr` 导入后端工厂并调用它。

    格式不对或找不到属性时以 CONFIG_ERROR 退出；导入模块失败时以 DEPENDENCY_ERROR 退出。
    """
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        log.error("--backend 应为 module:attr 格式，实际为 %r", spec)
        exit_process(ExitCode.CONFIG_ERROR)
    try:
        module = importlib.import_module(module_name)
    except (ImportError, OSError) as exc:
        log.error("无法导入后端模块 %s：%s", module_name, exc)
        exit_process(ExitCode.DEPENDENCY_ERROR)
    factory = getattr(module, attr, None)
    if not callable(factory):
        log.error("后端模块 %s 中没有可调用的 %s", module_name, attr)
        exit_process(ExitCode.CONFIG_ERROR)
    return cast(NmtBackend, factory())


def main() -> None:
    setup_process("nmt")
    args = parse_args(add_arguments=_add_arguments)
    logging.getLogger().setLevel(args.log_level)
    backend = load_backend(args.backend)
    run_worker(TranslateHandler(backend), args)


if __name__ == "__main__":
    main()
