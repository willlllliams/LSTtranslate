"""卸载时的用户数据清理（需求 INS-06，架构文档第 15 章）。

卸载程序在用户选择“同时删除用户数据”时，先运行 `launcher\\worker.py purge-secrets` 删除凭据管理器中
本程序保存的 API key，再由安装程序删除数据目录。

keyring 的 Windows 后端（WinVaultKeyring）把通用凭据的目标名设为服务名本身（LSTtranslate）。
同一服务下再写入另一个条目时，它先把原来占着服务名的旧条目改存到“旧条目名@服务名”，新条目占用服务名。
所以本程序的凭据有两种目标名：服务名本身，以及以“@服务名”结尾的组合名，清理时两种都删。

CredEnumerateW 的过滤器只支持末尾的星号，``*@LSTtranslate`` 这样的前导星号匹配不到任何条目。
这里不带过滤器列出当前用户的全部凭据，再在 Python 里按目标名筛选（不区分大小写，与凭据管理器一致）。
"""

import ctypes
import ctypes.wintypes
import logging
import sys
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SERVICE = "LSTtranslate"
_CRED_TYPE_GENERIC = 1
_ERROR_NOT_FOUND = 1168


class Credential(ctypes.Structure):
    """Win32 的 CREDENTIALW 结构。"""

    _fields_ = (
        ("Flags", ctypes.wintypes.DWORD),
        ("Type", ctypes.wintypes.DWORD),
        ("TargetName", ctypes.wintypes.LPWSTR),
        ("Comment", ctypes.wintypes.LPWSTR),
        ("LastWritten", ctypes.wintypes.FILETIME),
        ("CredentialBlobSize", ctypes.wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
        ("Persist", ctypes.wintypes.DWORD),
        ("AttributeCount", ctypes.wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.wintypes.LPWSTR),
        ("UserName", ctypes.wintypes.LPWSTR),
    )


class Advapi32(Protocol):
    """本模块用到的 advapi32 函数。测试注入假实现，从不触碰真实的凭据管理器。"""

    def CredEnumerateW(self, filter_: str | None, flags: int, count: Any, credentials: Any) -> int: ...

    def CredFree(self, buffer: Any) -> None: ...

    def CredDeleteW(self, target: str, kind: int, flags: int) -> int: ...


class CredentialError(Exception):
    """枚举凭据失败（ERROR_NOT_FOUND 表示没有凭据，不算失败）。"""


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """清理结果：删除的条目数与删除失败的条目数；enumerated 为假表示连枚举都失败了。"""

    deleted: int = 0
    failed: int = 0
    enumerated: bool = True

    @property
    def ok(self) -> bool:
        return self.enumerated and self.failed == 0


def _load_advapi32() -> Advapi32 | None:
    if sys.platform != "win32":
        return None
    return ctypes.WinDLL("advapi32", use_last_error=True)  # pyright: ignore[reportReturnType]


def belongs_to(target: str, service: str = SERVICE) -> bool:
    """目标名是否属于本程序：等于服务名，或以“@服务名”结尾。"""
    name, suffix = target.casefold(), service.casefold()
    return name == suffix or name.endswith("@" + suffix)


def credential_targets(service: str = SERVICE, *, advapi32: Advapi32 | None = None) -> list[str]:
    """列出本程序在凭据管理器里的全部通用凭据目标名，排序后返回。

    枚举失败（ERROR_NOT_FOUND 之外的错误）时记 WARNING 并抛出 CredentialError。
    """
    api = advapi32 if advapi32 is not None else _load_advapi32()
    if api is None:
        return []
    count = ctypes.wintypes.DWORD()
    creds = ctypes.POINTER(ctypes.POINTER(Credential))()
    if not api.CredEnumerateW(None, 0, ctypes.byref(count), ctypes.byref(creds)):
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            return []
        logger.warning("枚举凭据失败（错误码 %s）", error)
        raise CredentialError(error)
    targets: set[str] = set()
    try:
        for index in range(count.value):
            credential = creds[index].contents
            name = credential.TargetName
            if credential.Type == _CRED_TYPE_GENERIC and name and belongs_to(name, service):
                targets.add(name)
    finally:
        api.CredFree(creds)
    return sorted(targets)


def purge_secrets(service: str = SERVICE, *, advapi32: Advapi32 | None = None) -> PurgeResult:
    """删除本程序在凭据管理器里保存的全部 API key。日志只写数量和错误码，不写目标名。

    删除时报 ERROR_NOT_FOUND（条目在枚举之后已被删掉）不算失败。
    """
    api = advapi32 if advapi32 is not None else _load_advapi32()
    if api is None:
        return PurgeResult()
    try:
        targets = credential_targets(service, advapi32=api)
    except CredentialError:
        return PurgeResult(enumerated=False)
    deleted = failed = 0
    for target in targets:
        if api.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
            deleted += 1
            continue
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            continue
        failed += 1
        logger.warning("删除一条凭据失败（错误码 %s）", error)
    logger.info("已删除 %s 条凭据，%s 条删除失败", deleted, failed)
    return PurgeResult(deleted, failed)
