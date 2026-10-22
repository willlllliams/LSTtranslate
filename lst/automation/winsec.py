r"""自动化接口用到的 Windows 安全接口：当前用户的 SID、管道的安全描述符、按 SID 检查管道的所有者。

只用标准库的 ctypes，服务端（主进程）与客户端（``python -m lst.automation``）共用。

管道的安全描述符用 SDDL（Security Descriptor Definition Language，安全描述符定义语言）写成
``O:<SID>D:P(A;;GA;;;<SID>)``：

- ``O:<SID>``：所有者是当前用户；
- ``D:P``：DACL（Discretionary Access Control List，自主访问控制列表）带保护标志，不从上级继承任何条目；
- ``(A;;GA;;;<SID>)``：唯一的一条允许条目，把全部权限给当前用户。

SYSTEM、Administrators 与其他用户都不在列表里。Windows 创建管道时把 GA（GENERIC_ALL）映射成 FA
（FILE_ALL_ACCESS），所以读回来的 DACL 是 ``D:P(A;;FA;;;<SID>)``。没有显式写完整性标签时，
内核对象按 Medium 完整性的 No-Write-Up 规则处理，Low 完整性的进程打不开管道写入。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import sys
from ctypes import wintypes
from typing import Final

PIPE_PREFIX: Final = "\\\\.\\pipe\\"
PIPE_BASENAME: Final = "lsttranslate-automation-"
PIPE_ENV: Final = "LST_AUTOMATION_PIPE"
r"""环境变量：覆盖管道名（完整的 ``\\.\pipe\...`` 或只写最后一段）。测试与同时运行两个实例时用。"""

_TOKEN_QUERY: Final = 0x0008
_TOKEN_USER: Final = 1
_SDDL_REVISION_1: Final = 1
_SE_KERNEL_OBJECT: Final = 6
_OWNER_SECURITY_INFORMATION: Final = 0x1
_DACL_SECURITY_INFORMATION: Final = 0x4

PIPE_ACCESS_DUPLEX: Final = 0x00000003
FILE_FLAG_OVERLAPPED: Final = 0x40000000
FILE_FLAG_FIRST_PIPE_INSTANCE: Final = 0x00080000
PIPE_TYPE_BYTE: Final = 0x0
PIPE_READMODE_BYTE: Final = 0x0
PIPE_WAIT: Final = 0x0
PIPE_REJECT_REMOTE_CLIENTS: Final = 0x00000008
PIPE_UNLIMITED_INSTANCES: Final = 255
INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value

_GENERIC_READ: Final = 0x80000000
_GENERIC_WRITE: Final = 0x40000000
_OPEN_EXISTING: Final = 3
_SECURITY_SQOS_PRESENT: Final = 0x00100000
_SECURITY_IDENTIFICATION: Final = 0x00010000
ERROR_FILE_NOT_FOUND: Final = 2
ERROR_ACCESS_DENIED: Final = 5
ERROR_PIPE_BUSY: Final = 231


class SecurityAttributes(ctypes.Structure):
    _fields_ = (
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    )


class _SidAndAttributes(ctypes.Structure):
    _fields_ = (("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD))


def _check_windows() -> None:
    if sys.platform != "win32":
        raise OSError("自动化接口只支持 Windows")


def _kernel32() -> ctypes.WinDLL:
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.GetCurrentProcess.restype = wintypes.HANDLE
    dll.CloseHandle.argtypes = (wintypes.HANDLE,)
    dll.LocalFree.argtypes = (ctypes.c_void_p,)
    dll.LocalFree.restype = ctypes.c_void_p
    dll.CreateNamedPipeW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SecurityAttributes),
    )
    dll.CreateNamedPipeW.restype = wintypes.HANDLE
    dll.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    dll.CreateFileW.restype = wintypes.HANDLE
    dll.WaitNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    dll.WaitNamedPipeW.restype = wintypes.BOOL
    dll.GetNamedPipeServerProcessId.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
    dll.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    return dll


def _advapi32() -> ctypes.WinDLL:
    dll = ctypes.WinDLL("advapi32", use_last_error=True)
    dll.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    dll.OpenProcessToken.restype = wintypes.BOOL
    dll.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    dll.GetTokenInformation.restype = wintypes.BOOL
    dll.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    dll.ConvertSidToStringSidW.restype = wintypes.BOOL
    dll.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    )
    dll.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    dll.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG),
    )
    dll.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    dll.GetSecurityInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    dll.GetSecurityInfo.restype = wintypes.DWORD
    return dll


def _error(what: str, code: int | None = None) -> OSError:
    number = ctypes.get_last_error() if code is None else code
    return OSError(number, f"{what}失败：{ctypes.FormatError(number).strip()}", None, number)


def _sid_text(sid: int | None) -> str:
    advapi = _advapi32()
    text = wintypes.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise _error("ConvertSidToStringSidW")
    try:
        return str(text.value)
    finally:
        _kernel32().LocalFree(ctypes.cast(text, ctypes.c_void_p))


def current_user_sid() -> str:
    """当前进程令牌里的用户 SID，例如 ``S-1-5-21-…-1001``。"""
    _check_windows()
    kernel, advapi = _kernel32(), _advapi32()
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _error("OpenProcessToken")
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, _TOKEN_USER, buffer, needed, ctypes.byref(needed)):
            raise _error("GetTokenInformation")
        user = _SidAndAttributes.from_buffer(buffer)
        return _sid_text(user.Sid)
    finally:
        kernel.CloseHandle(token)


def pipe_name_for(sid: str) -> str:
    """当前用户的管道名：前缀加 SID 的 SHA-256 前 12 位十六进制。同一用户的两个会话用同一个名字。"""
    digest = hashlib.sha256(sid.encode("ascii")).hexdigest()[:12]
    return f"{PIPE_PREFIX}{PIPE_BASENAME}{digest}"


def normalize_pipe_name(value: str) -> str:
    r"""把 ``\\.\pipe\x`` 或只有最后一段的 ``x`` 写成完整的管道名。"""
    text = value.strip()
    if text.lower().startswith(PIPE_PREFIX.lower()):
        return text
    if not text or "\\" in text or "/" in text:
        raise ValueError(f"管道名无效：{value!r}")
    return PIPE_PREFIX + text


def default_pipe_name() -> str:
    """服务端与客户端默认使用的管道名：环境变量 LST_AUTOMATION_PIPE，没有时按当前用户的 SID 生成。"""
    override = os.environ.get(PIPE_ENV, "").strip()
    if override:
        return normalize_pipe_name(override)
    return pipe_name_for(current_user_sid())


def pipe_sddl(sid: str) -> str:
    """管道的安全描述符：所有者是 sid，受保护的 DACL 里只有一条把全部权限给 sid 的允许条目。"""
    return f"O:{sid}D:P(A;;GA;;;{sid})"


class PipeSecurity:
    """由 SDDL 转换出的安全描述符与 SECURITY_ATTRIBUTES。用完调用 close() 释放内存。"""

    def __init__(self, sddl: str) -> None:
        _check_windows()
        descriptor = ctypes.c_void_p()
        size = wintypes.ULONG()
        converted = _advapi32().ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), ctypes.byref(size)
        )
        if not converted:
            raise _error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        self._descriptor: ctypes.c_void_p | None = descriptor
        self.attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor.value, False)

    def close(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            _kernel32().LocalFree(descriptor)


def create_pipe_instance(name: str, security: PipeSecurity, *, first: bool, buffer_size: int = 65536) -> int:
    """创建一个重叠 I/O 的字节流管道实例，返回句柄。

    first 为真时带 FILE_FLAG_FIRST_PIPE_INSTANCE：同名管道已经存在（另一个实例占着，或别的程序抢先建了同名
    管道）时失败，不会接到别人的管道上。远程客户端一律拒绝（PIPE_REJECT_REMOTE_CLIENTS）。
    """
    flags = PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED | (FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0)
    mode = PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS
    handle = _kernel32().CreateNamedPipeW(
        name,
        flags,
        mode,
        PIPE_UNLIMITED_INSTANCES,
        buffer_size,
        buffer_size,
        0,
        ctypes.byref(security.attributes),
    )
    if handle is None or handle == INVALID_HANDLE_VALUE:
        raise _error("CreateNamedPipeW")
    return int(handle)


def close_handle(handle: int) -> None:
    _kernel32().CloseHandle(handle)


def _security_sddl(handle: int, what: int) -> str:
    """读句柄所指对象的安全信息（what 是 *_SECURITY_INFORMATION 的组合），写成 SDDL。"""
    advapi = _advapi32()
    descriptor = ctypes.c_void_p()
    status = advapi.GetSecurityInfo(
        handle, _SE_KERNEL_OBJECT, what, None, None, None, None, ctypes.byref(descriptor)
    )
    if status != 0:
        raise _error("GetSecurityInfo", status)
    try:
        text = wintypes.LPWSTR()
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, _SDDL_REVISION_1, what, ctypes.byref(text), None
        ):
            raise _error("ConvertSecurityDescriptorToStringSecurityDescriptorW")
        try:
            return str(text.value)
        finally:
            _kernel32().LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        _kernel32().LocalFree(descriptor)


def handle_sddl(handle: int) -> str:
    """句柄所指对象的所有者与 DACL，写成 SDDL。测试用它核对管道的权限。"""
    return _security_sddl(handle, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION)


def handle_owner(handle: int) -> str:
    """句柄所指对象的所有者 SID。"""
    sddl = _security_sddl(handle, _OWNER_SECURITY_INFORMATION)
    match = re.match(r"^O:([^:]+?)(?:[GDS]:|$)", sddl)
    if match is None:
        raise OSError(f"读不到管道的所有者：{sddl}")
    return match.group(1)


def open_pipe_client(name: str) -> int:
    """以客户端身份打开管道，返回句柄。

    带 SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION：服务端最多只能识别客户端身份，不能冒用它的权限。
    管道不存在时抛 FileNotFoundError，所有实例都忙时抛 winerror 为 231 的 OSError。
    """
    _check_windows()
    handle = _kernel32().CreateFileW(
        name,
        _GENERIC_READ | _GENERIC_WRITE,
        0,
        None,
        _OPEN_EXISTING,
        _SECURITY_SQOS_PRESENT | _SECURITY_IDENTIFICATION,
        None,
    )
    if handle is None or handle == INVALID_HANDLE_VALUE:
        code = ctypes.get_last_error()
        if code == ERROR_FILE_NOT_FOUND:
            raise FileNotFoundError(code, f"管道 {name} 不存在（程序没有运行或自动化接口没有开启）")
        raise _error(f"打开管道 {name} ", code)
    return int(handle)


def wait_pipe(name: str, timeout_ms: int) -> bool:
    """等管道有空闲实例；超时或管道不存在时返回 False。"""
    return bool(_kernel32().WaitNamedPipeW(name, timeout_ms))


def pipe_server_pid(handle: int) -> int:
    """客户端句柄连着的服务端进程号。"""
    pid = wintypes.ULONG()
    if not _kernel32().GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
        raise _error("GetNamedPipeServerProcessId")
    return int(pid.value)


__all__ = [
    "ERROR_ACCESS_DENIED",
    "ERROR_PIPE_BUSY",
    "PIPE_ENV",
    "PipeSecurity",
    "close_handle",
    "create_pipe_instance",
    "current_user_sid",
    "default_pipe_name",
    "handle_owner",
    "handle_sddl",
    "normalize_pipe_name",
    "open_pipe_client",
    "pipe_name_for",
    "pipe_sddl",
    "pipe_server_pid",
    "wait_pipe",
]
