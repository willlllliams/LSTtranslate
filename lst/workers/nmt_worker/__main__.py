"""NMT 进程入口：`python -m lst.workers.nmt_worker --address <地址> [--backend module:attr]`。

authkey 以十六进制从标准输入第一行传入。--backend 默认是 CTranslate2 后端，测试用它换成假后端。
"""

import argparse
import logging
from typing import cast

from lst.workers._runtime import load_backend, parse_args, run_worker, setup_process
from lst.workers.nmt_worker.backend import NmtBackend
from lst.workers.nmt_worker.handler import TranslateHandler

DEFAULT_BACKEND = "lst.workers.nmt_worker.ct2_backend:Ct2Backend"


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", default=DEFAULT_BACKEND, help="后端工厂，格式为 module:attr")


def main() -> None:
    setup_process("nmt")
    args = parse_args(add_arguments=_add_arguments)
    logging.getLogger().setLevel(args.log_level)
    backend = cast(NmtBackend, load_backend(args.backend))
    run_worker(TranslateHandler(backend), args)


if __name__ == "__main__":
    main()
