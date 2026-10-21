"""密钥存储：API key 存放在 Windows 凭据管理器，配置文件里只写条目名（需求 CFG-04，架构文档 2.9.2 节）。

``SecretStore`` 是配置服务使用的读写接口，同时在结构上满足 ``contracts.ports.SecretReader``，
组合根可以把同一个对象注入给引擎模块。任何错误信息都不包含密钥的值。
"""

import logging
import threading
from collections.abc import Mapping
from typing import Protocol, cast

import keyring
from keyring.backend import KeyringBackend

_logger = logging.getLogger(__name__)


class _Backend(Protocol):
    """KeyringSecretStore 用到的 keyring 后端方法。KeyringBackend 的元类包装了 set_password，类型不完整。"""

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class SecretStoreError(Exception):
    """写入或删除密钥失败。消息里只有异常类型和经过脱敏的说明，不含密钥的值。"""


class SecretStore(Protocol):
    """密钥的读写接口。三个方法都可能阻塞，配置服务在线程池里调用它们。"""

    def get_secret(self, name: str) -> str | None:
        """读取一个条目；条目不存在时返回 None。"""
        ...

    def set_secret(self, name: str, value: str) -> None:
        """写入或覆盖一个条目。失败时抛出 ``SecretStoreError``。"""
        ...

    def delete_secret(self, name: str) -> bool:
        """删除一个条目，返回删除前它是否存在。失败时抛出 ``SecretStoreError``。"""
        ...


def describe_error(exc: BaseException, secret: str | None = None) -> str:
    """异常的简短说明：类型名加消息，消息里出现的 secret 替换成 ***。"""
    text = str(exc)
    if secret:
        text = text.replace(secret, "***")
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


class MemorySecretStore:
    """内存里的密钥存储，供测试和组合根的无持久化场合使用。可以在任何线程调用。"""

    def __init__(self, entries: Mapping[str, str] | None = None) -> None:
        self._entries = dict(entries or {})
        self._lock = threading.Lock()

    def get_secret(self, name: str) -> str | None:
        with self._lock:
            return self._entries.get(name)

    def set_secret(self, name: str, value: str) -> None:
        with self._lock:
            self._entries[name] = value

    def delete_secret(self, name: str) -> bool:
        with self._lock:
            return self._entries.pop(name, None) is not None


class KeyringSecretStore:
    """基于 keyring 的密钥存储。服务名默认为 ``LSTtranslate``，条目名作为 keyring 的用户名。

    ``backend`` 缺省时每次调用都使用 keyring 当前的全局后端（Windows 上是凭据管理器）；
    传入时只使用这个后端，测试借此完全避开真实的凭据管理器。
    """

    def __init__(self, service: str = "LSTtranslate", *, backend: KeyringBackend | None = None) -> None:
        self._service = service
        self._backend = backend

    def _keyring(self) -> _Backend:
        backend = self._backend if self._backend is not None else keyring.get_keyring()
        return cast(_Backend, backend)

    def get_secret(self, name: str) -> str | None:
        """读取一个条目。后端出错时记 ERROR 并返回 None：调用方只关心有没有可用的值。"""
        try:
            return self._keyring().get_password(self._service, name)
        except Exception as exc:
            _logger.error("读取密钥 %s 失败：%s", name, describe_error(exc))
            return None

    def set_secret(self, name: str, value: str) -> None:
        try:
            self._keyring().set_password(self._service, name, value)
        except Exception as exc:
            # from None：原异常的消息没有脱敏，不能随异常链进入日志。
            raise SecretStoreError(describe_error(exc, value)) from None

    def delete_secret(self, name: str) -> bool:
        existing: str | None = None
        try:
            backend = self._keyring()
            existing = backend.get_password(self._service, name)
            if existing is None:
                return False
            backend.delete_password(self._service, name)
        except Exception as exc:
            raise SecretStoreError(describe_error(exc, existing)) from None
        return True
