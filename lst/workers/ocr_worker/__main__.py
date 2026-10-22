"""OCR 进程入口。

命令行：`python -m lst.workers.ocr_worker --address <地址> [--backend module:attr] [--log-level ...]`。

PaddleOCR 后端的进程运行在独立环境 .venv-ocr 中（架构文档 2.4 节），工作目录为仓库根目录，
lst 包从工作目录导入。
authkey 以十六进制从标准输入第一行传入。--backend 默认是 PaddleOCR 后端；
Windows OCR 用 `--backend lst.workers.ocr_worker.winocr_backend:WinOcrBackend`，由主环境的解释器运行；
RapidOCR 用 `--backend lst.workers.ocr_worker.rapid_backend:RapidBackend`，由装有 rapidocr 的环境运行
（安装版是数据目录里的 RapidOCR 运行环境，源码版是 .venv-ocr）；测试用它换成假后端。
本模块的导入链不含推理库，后端模块由 _runtime.load_backend 按名字导入。

进程启动后先装上网络拦截（network_guard），之后任何代码都不能访问本机以外的网络（需求 PRV-01）。
"""

import argparse
import logging
from typing import cast

from lst.workers._runtime import load_backend, parse_args, run_worker, setup_process
from lst.workers.ocr_worker import network_guard
from lst.workers.ocr_worker.backend import OcrBackend
from lst.workers.ocr_worker.handler import OcrHandler

DEFAULT_BACKEND = "lst.workers.ocr_worker.paddle_backend:PaddleBackend"


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", default=DEFAULT_BACKEND, help="后端工厂，格式为 module:attr")


def main() -> None:
    setup_process("ocr")
    network_guard.install()
    args = parse_args(add_arguments=_add_arguments)
    logging.getLogger().setLevel(args.log_level)
    backend = cast(OcrBackend, load_backend(args.backend))
    run_worker(OcrHandler(backend), args)


if __name__ == "__main__":
    main()
