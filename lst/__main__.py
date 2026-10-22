"""程序入口：`python -m lst [--data-dir 目录] [--log-level 级别] [--flavor normal|dev]`。

数据目录按优先级取：--data-dir → 环境变量 LST_DATA_DIR → 设置里选的目录（位置文件）→ 默认位置
（lst/app/paths.py）。设置里选的目录无法使用时本次退回默认位置，启动后通知用户。
程序因“立即重启”退出时，在释放单实例锁之后启动新进程（lst/app/restart.py）。
构建类型（lst/app/flavor.py）：安装版读 installed.json；源码版取 --flavor，其次是环境变量 LST_FLAVOR，
缺省为普通版。
"""

import argparse
import asyncio
import contextlib
import logging
import sys
from collections.abc import Callable
from typing import cast

import qasync
from PySide6.QtWidgets import QApplication

from lst.app.bootstrap import APP_NAME, Application, AppOptions, single_instance_name
from lst.app.flavor import choose_flavor
from lst.app.logging_setup import setup_logging
from lst.app.paths import (
    INSTALL_MARKER,
    AppPaths,
    DataDirChoice,
    choose_data_dir,
    default_data_dir,
    is_installed,
)
from lst.app.restart import relaunch
from lst.app.single_instance import RunningMutex, SingleInstance
from lst.contracts.storage import DataDirSource

logger = logging.getLogger("lst")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lst", description="LSTtranslate：多引擎竞速的屏幕翻译工具")
    parser.add_argument("--data-dir", help="数据目录，优先于环境变量 LST_DATA_DIR 与设置里选的目录")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument(
        "--flavor",
        choices=["normal", "dev"],
        help="构建类型（源码运行时有效）：dev 时自动化接口默认开启；优先于环境变量 LST_FLAVOR",
    )
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


def set_app_user_model_id() -> None:
    """给进程设置 AppUserModelID。

    安装版用 pythonw.exe 运行，不设置时 Windows 把窗口归到 Python 名下，
    任务栏和窗口切换器里显示 Python 的图标。
    设置后窗口归到本程序，显示窗口自己的图标（QApplication.setWindowIcon）。失败时只是图标不对，不影响运行。
    """
    if sys.platform != "win32":
        return
    import ctypes

    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LSTtranslate.App")


def prepare_data_dir(choice: DataDirChoice) -> tuple[AppPaths, DataDirChoice]:
    """创建数据目录里需要的子目录。设置里选的目录写不进去时退回默认位置，并在 fallback 里记下它。"""
    paths = AppPaths(choice.path)
    try:
        paths.ensure()
    except OSError:
        if choice.source is not DataDirSource.CHOSEN:
            raise
        fallback = DataDirChoice(default_data_dir(), DataDirSource.DEFAULT, choice.location, choice.path)
        paths = AppPaths(fallback.path)
        paths.ensure()
        return paths, fallback
    return paths, choice


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths, choice = prepare_data_dir(choose_data_dir(args.data_dir))
    logs = setup_logging(paths.logs_dir, level=getattr(logging, args.log_level))
    try:
        qt_app = QApplication.instance() or QApplication(sys.argv)
        assert isinstance(qt_app, QApplication)
        qt_app.setApplicationName(APP_NAME)
        set_app_user_model_id()
        qt_app.setQuitOnLastWindowClosed(False)  # 关掉双语窗口不能退出程序，托盘常驻（需求 TR-01）

        flavor = choose_flavor(args.flavor, installed=is_installed(), marker=INSTALL_MARKER)
        application = Application(AppOptions(paths=paths, data=choice, flavor=flavor), qt_app)
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
        if application.restart_requested:
            relaunch()
        return 0
    except Exception:
        logger.exception("程序异常退出")
        return 1
    finally:
        logs.stop()


if __name__ == "__main__":
    sys.exit(main())
