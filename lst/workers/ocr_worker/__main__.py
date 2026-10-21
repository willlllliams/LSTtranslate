"""OCR 进程入口。

命令行：`python -m lst.workers.ocr_worker --address <地址> [--backend module:attr] [--log-level ...]`。

OCR 进程运行在独立环境 .venv-ocr 中（架构文档 2.4 节），工作目录为仓库根目录，lst 包从工作目录导入。
authkey 以十六进制从标准输入第一行传入。--backend 默认是 PaddleOCR 后端，测试用它换成假后端。

进程启动后先装上网络拦截（network_guard），之后任何代码都不能访问本机以外的网络（需求 PRV-01）。
"""

import argparse
import importlib
import logging
from typing import cast

from lst.contracts.worker_protocol import ExitCode
from lst.workers._runtime import exit_process, parse_args, run_worker, setup_process
from lst.workers.ocr_worker import network_guard
from lst.workers.ocr_worker.backend import OcrBackend
from lst.workers.ocr_worker.handler import OcrHandler

DEFAULT_BACKEND = "lst.workers.ocr_worker.paddle_backend:PaddleBackend"

log = logging.getLogger("lst.workers.ocr")


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", default=DEFAULT_BACKEND, help="后端工厂，格式为 module:attr")


def load_backend(spec: str) -> OcrBackend:
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
    return cast(OcrBackend, factory())


def main() -> None:
    setup_process("ocr")
    network_guard.install()
    args = parse_args(add_arguments=_add_arguments)
    logging.getLogger().setLevel(args.log_level)
    backend = load_backend(args.backend)
    run_worker(OcrHandler(backend), args)


if __name__ == "__main__":
    main()
