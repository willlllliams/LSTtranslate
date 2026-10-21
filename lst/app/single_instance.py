"""单实例（需求 TR-06，架构文档 2.9.6 节）。

Windows 上两个 QLocalServer 可以同时监听同一个名称，listen() 成功不能证明没有别的实例。启动流程分三步：
1. 先用 QLocalSocket 连接；连上了说明已有实例在运行，调用方退出；
2. 连不上时，用 QLockFile 加锁；加锁失败说明另一个实例正在启动，同样退出；
3. 加锁成功后再 listen，之后的重复启动会在第 1 步连上我们。

通知不传任何数据，连接本身就是通知：已运行的实例每收到一个新连接，就认为有新实例启动了。
实测 Windows 上客户端刚连上就写入时，数据有约四成概率到不了服务端（写入方等不到对端读取），
而“连接成功”这件事本身是可靠的。本机其他程序连上这个管道，最多多弹一次无害的通知。
"""

import ctypes
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_MS = 300


class SingleInstance(QObject):
    """持有单实例锁与本地服务。acquire() 返回 False 时，调用方应当退出。"""

    def __init__(self, server_name: str, lock_path: Path, on_second_instance: Callable[[], None]) -> None:
        super().__init__()
        self._server_name = server_name
        self._lock = QLockFile(str(lock_path))
        # 长期持有的锁不能被当成过期锁抢走（QLockFile 文档）。
        self._lock.setStaleLockTime(0)
        self._on_second_instance = on_second_instance
        self._server: QLocalServer | None = None

    def acquire(self) -> bool:
        if self._running_instance_exists():
            logger.info("已有实例在运行")
            return False
        if not self._lock.tryLock(0):
            logger.info("另一个实例正在启动（锁文件被占用）")
            return False
        QLocalServer.removeServer(self._server_name)  # 清理上次异常退出遗留的同名服务
        server = QLocalServer(self)
        if not server.listen(self._server_name):
            logger.warning("单实例服务监听失败：%s", server.errorString())
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        return True

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._lock.isLocked():
            self._lock.unlock()

    def _running_instance_exists(self) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self._server_name)
        connected = socket.waitForConnected(_CONNECT_TIMEOUT_MS)
        if connected:
            socket.disconnectFromServer()
        return connected

    def _on_new_connection(self) -> None:
        if self._server is None:
            return
        notified = False
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            socket.disconnectFromServer()
            socket.deleteLater()
            notified = True
        if notified:
            self._on_second_instance()


RUNNING_MUTEX = "LSTtranslate.Running"
"""运行中的命名互斥体。安装程序的 AppMutex 指向它，据此判断程序是否正在运行（需求 INS-05）。"""


class RunningMutex:
    """程序运行期间持有一个命名互斥体，退出时释放。非 Windows 平台不做任何事。"""

    def __init__(self, name: str = RUNNING_MUTEX) -> None:
        self._handle: int | None = None
        if sys.platform == "win32":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            handle = kernel32.CreateMutexW(None, False, name)
            if handle:
                self._handle = handle
            else:
                logger.warning("创建运行互斥体失败（错误码 %s）", ctypes.get_last_error())

    def release(self) -> None:
        if self._handle is not None:
            ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None
