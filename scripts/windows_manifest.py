"""为分发的 CPython 启用 UTF-8 ANSI 文件接口，保留原有程序清单。"""

import ctypes
import xml.etree.ElementTree as ET
from ctypes import wintypes
from pathlib import Path

_ASM = "urn:schemas-microsoft-com:asm.v1"
_ASM3 = "urn:schemas-microsoft-com:asm.v3"
_UTF8 = "http://schemas.microsoft.com/SMI/2019/WindowsSettings"


def utf8_manifest(data: bytes) -> bytes:
    """在现有清单中设置 activeCodePage，保留权限、兼容性和 DPI 设置。"""
    root = ET.fromstring(data)
    if root.tag != f"{{{_ASM}}}assembly":
        raise ValueError("不是 Windows 程序清单")
    application = root.find(f"{{{_ASM3}}}application")
    if application is None:
        application = ET.SubElement(root, f"{{{_ASM3}}}application")
    settings = application.find(f"{{{_ASM3}}}windowsSettings")
    if settings is None:
        settings = ET.SubElement(application, f"{{{_ASM3}}}windowsSettings")
    code_page = settings.find(f"{{{_UTF8}}}activeCodePage")
    if code_page is None:
        code_page = ET.SubElement(settings, f"{{{_UTF8}}}activeCodePage")
    code_page.text = "UTF-8"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def enable_utf8_code_page(executable: Path) -> None:
    """修改暂存的 EXE 清单；适用于 Windows 10 1903 及以上系统。"""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    # MAKEINTRESOURCE：资源类型 24（RT_MANIFEST）、可执行文件清单 ID 1。
    resource_type = ctypes.c_void_p(24)
    resource_name = ctypes.c_void_p(1)
    kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel.LoadLibraryExW.restype = wintypes.HMODULE
    kernel.FindResourceExW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, wintypes.WORD]
    kernel.FindResourceExW.restype = wintypes.HANDLE
    kernel.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    kernel.SizeofResource.restype = wintypes.DWORD
    kernel.LoadResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    kernel.LoadResource.restype = wintypes.HANDLE
    kernel.LockResource.argtypes = [wintypes.HANDLE]
    kernel.LockResource.restype = ctypes.c_void_p
    kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
    kernel.FreeLibrary.restype = wintypes.BOOL
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, wintypes.WORD, wintypes.LPARAM
    )
    kernel.EnumResourceLanguagesW.argtypes = [
        wintypes.HMODULE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        callback_type,
        wintypes.LPARAM,
    ]
    kernel.EnumResourceLanguagesW.restype = wintypes.BOOL
    kernel.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel.UpdateResourceW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.WORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.UpdateResourceW.restype = wintypes.BOOL
    kernel.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    kernel.EndUpdateResourceW.restype = wintypes.BOOL

    module = kernel.LoadLibraryExW(str(executable), None, 2 | 32)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    languages: list[int] = []

    @callback_type
    def collect(_module: int, _kind: int, _name: int, language: int, _param: int) -> bool:
        languages.append(language)
        return True

    manifests: dict[int, bytes] = {}
    try:
        if not kernel.EnumResourceLanguagesW(module, resource_type, resource_name, collect, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        for language in languages:
            resource = kernel.FindResourceExW(module, resource_type, resource_name, language)
            size = kernel.SizeofResource(module, resource)
            address = kernel.LockResource(kernel.LoadResource(module, resource))
            if not address or not size:
                raise ctypes.WinError(ctypes.get_last_error())
            manifests[language] = utf8_manifest(ctypes.string_at(address, size))
    finally:
        kernel.FreeLibrary(module)

    update = kernel.BeginUpdateResourceW(str(executable), False)
    if not update:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        for language, manifest in manifests.items():
            buffer = ctypes.create_string_buffer(manifest)
            if not kernel.UpdateResourceW(
                update, resource_type, resource_name, language, buffer, len(manifest)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.EndUpdateResourceW(update, True)
        raise
    if not kernel.EndUpdateResourceW(update, False):
        raise ctypes.WinError(ctypes.get_last_error())
