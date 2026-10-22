"""程序入口：`python -m lst [--data-dir 目录] [--log-level 级别]`。"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable
from typing import cast

import qasync
from PySide6.QtWidgets import QApplication

from lst.app.bootstrap import APP_NAME, Application, AppOptions, single_instance_name
from lst.app.logging_setup import setup_logging
from lst.app.paths import AppPaths
from lst.app.single_instance import RunningMutex, SingleInstance

logger = logging.getLogger("lst")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lst", description="LSTtranslate：多引擎竞速的屏幕翻译工具")
    parser.add_argument("--data-dir", help="数据目录，默认是程序目录下的 data/")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


async def run(application: Application, on_quit: Callable[[], None] | None = None) -> None:
    """启动、等待退出请求、停止，全部在一次 run_until_complete 里完成。

    不能拆成几次 run_until_complete：qasync 的 run_until_complete 在 Qt 事件循环退出、已经注销
    “正在运行的循环”之后，还会调用一次 processEvents 处理剩余事件。这时排队的任务照样执行，
    任务里的 asyncio.create_task 因为没有正在运行的循环而抛 RuntimeError。安装版实测：start() 刚结束时，
    首次运行引导发出的 RefreshModelCatalog 就在这个空档里执行，模型列表取不到。

    start() 也放在 try 里：启动中途失败时，已经启动的模块、推理进程与缓存同样要停止和关闭。

    on_quit 在收到退出请求后、停止各模块之前调用。入口用它释放运行互斥体：应用内更新先启动新安装包
    再请求退出，安装程序等待互斥体释放才继续（架构文档 15.4 节），关停推理进程可能要几秒，不必让它干等。
    """
    try:
        await application.start()
        await application.wait_closed()
        if on_quit is not None:
            on_quit()
    finally:
        await application.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths = AppPaths.resolve(args.data_dir)
    paths.ensure()
    logs = setup_logging(paths.logs_dir, level=getattr(logging, args.log_level))
    try:
        qt_app = QApplication.instance() or QApplication(sys.argv)
        assert isinstance(qt_app, QApplication)
        qt_app.setApplicationName(APP_NAME)
        qt_app.setQuitOnLastWindowClosed(False)  # 关掉双语窗口不能退出程序，托盘常驻（需求 TR-01）

        application = Application(AppOptions(paths=paths), qt_app)
        logs.add_filter(application.redactor)
        instance = SingleInstance(
            single_instance_name(paths), paths.lock_file, application.notify_second_instance
        )
        if not instance.acquire():
            return 0
        running = RunningMutex()  # 安装程序据此判断程序是否正在运行（需求 INS-05）

        loop = qasync.QEventLoop(qt_app)
        asyncio.set_event_loop(loop)
        runner = cast(asyncio.AbstractEventLoop, loop)  # qasync 的 run_until_complete 没有类型标注
        try:
            with loop:
                runner.run_until_complete(run(application, on_quit=running.release))
        finally:
            running.release()
            instance.release()
        return 0
    except Exception:
        logger.exception("程序异常退出")
        return 1
    finally:
        logs.stop()


if __name__ == "__main__":
    sys.exit(main())
