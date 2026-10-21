"""卸载时的用户数据清理（需求 INS-06，架构文档第 15 章）。

卸载程序在用户选择“同时删除用户数据”时，先运行 `launcher\\worker.py purge-secrets` 删除凭据管理器中
本程序保存的 API key，再由安装程序删除数据目录。

keyring 在 Windows 凭据管理器里保存通用凭据时，目标名是服务名本身（LSTtranslate），
同一服务下有第二个条目时改用“条目名@服务名”。所以清理时删除这两种目标名的全部凭据。
"""

import ctypes
import ctypes.wintypes
import logging
import sys

logger = logging.getLogger(__name__)

SERVICE = "LSTtranslate"
_CRED_TYPE_GENERIC = 1
_ERROR_NOT_FOUND = 1168


class _Credential(ctypes.Structure):
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


def credential_targets(service: str = SERVICE) -> list[str]:
    """列出本程序在凭据管理器里的全部通用凭据目标名。"""
    if sys.platform != "win32":
        return []
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    targets: list[str] = []
    for pattern in (service, f"*@{service}"):
        count = ctypes.wintypes.DWORD()
        creds = ctypes.POINTER(ctypes.POINTER(_Credential))()
        if not advapi32.CredEnumerateW(pattern, 0, ctypes.byref(count), ctypes.byref(creds)):
            error = ctypes.get_last_error()
            if error != _ERROR_NOT_FOUND:
                logger.warning("枚举凭据失败（错误码 %s）", error)
            continue
        try:
            for index in range(count.value):
                credential = creds[index].contents
                if credential.Type == _CRED_TYPE_GENERIC and credential.TargetName:
                    targets.append(credential.TargetName)
        finally:
            advapi32.CredFree(creds)
    return sorted(set(targets))


def purge_secrets(service: str = SERVICE) -> int:
    """删除本程序在凭据管理器里保存的全部 API key，返回删除的条目数。日志只写数量，不写目标名。"""
    if sys.platform != "win32":
        return 0
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    deleted = 0
    for target in credential_targets(service):
        if advapi32.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
            deleted += 1
        else:
            logger.warning("删除一条凭据失败（错误码 %s）", ctypes.get_last_error())
    logger.info("已删除 %s 条凭据", deleted)
    return deleted
