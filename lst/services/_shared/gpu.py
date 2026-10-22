r"""本机显卡检测（需求 OCRE-07、MOD-06）：OCR 引擎按它推荐运行环境版本，本地翻译按它说明显卡运行库是否有用。

检测方法
========

用 DXGI（DirectX Graphics Infrastructure）枚举显卡：``CreateDXGIFactory1`` 创建 IDXGIFactory1，
``EnumAdapters1`` 逐个取出适配器，``GetDesc1`` 读出 DXGI_ADAPTER_DESC1（名称、VendorId、DeviceId、显存、
Flags）。带 DXGI_ADAPTER_FLAG_SOFTWARE 标志的适配器与 Microsoft Basic Render Driver（VendorId 0x1414）
是软件渲染，不算显卡。是否支持 DirectX 12 用 ``D3D12CreateDevice(adapter, D3D_FEATURE_LEVEL_11_0,
IID_ID3D12Device, NULL)`` 判断：输出参数为 NULL 时这个函数只检查能否创建设备，不真正创建，返回 S_FALSE
（1）表示可以。DirectML 要求的就是能创建 D3D12 设备、特性级别不低于 11_0 的显卡。

整个过程只调用本机的 dxgi.dll 与 d3d12.dll，不联网、不读写注册表、不安装任何东西；COM 接口用 ctypes 按
虚函数表的位置调用（IDXGIFactory1::EnumAdapters1 是第 12 项，IDXGIAdapter1::GetDesc1 是第 10 项，
IUnknown::Release 是第 2 项，顺序取自 Windows SDK 的 dxgi.h）。任何一步失败（不是 Windows、DLL 缺失、
接口返回错误）都按“没有显卡”处理。
"""

import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

NVIDIA_VENDOR_ID: Final = 0x10DE
MICROSOFT_VENDOR_ID: Final = 0x1414
"""Microsoft Basic Render Driver（软件渲染）的 VendorId。"""

DXGI_ADAPTER_FLAG_SOFTWARE: Final = 2
DXGI_ERROR_NOT_FOUND: Final = 0x887A0002
D3D_FEATURE_LEVEL_11_0: Final = 0xB000
MAX_ADAPTERS: Final = 16


@dataclass(frozen=True, slots=True)
class GpuAdapter:
    """一块显卡：名称、厂商与设备号、专用显存（字节），以及是否支持 DirectX 12。"""

    name: str
    vendor_id: int
    device_id: int = 0
    dedicated_memory: int = 0
    d3d12: bool = False

    @property
    def nvidia(self) -> bool:
        return self.vendor_id == NVIDIA_VENDOR_ID


type GpuProbe = Callable[[], Sequence[GpuAdapter]]
"""返回本机的硬件显卡（已去掉软件渲染适配器）。失败时可以抛出异常，调用方按没有显卡处理。"""


def safe_probe(probe: GpuProbe, logger: logging.Logger) -> tuple[GpuAdapter, ...]:
    """调用 probe，失败时记 WARNING 并返回空元组（按没有显卡处理）。"""
    try:
        found = tuple(probe())
    except Exception as exc:
        logger.warning("无法检测显卡，按没有显卡处理：%s: %s", type(exc).__name__, exc)
        return ()
    # 同一块显卡在 DXGI 里可能以 LUID 不同的多个适配器出现（本机实测 RTX 5080 出现三次），
    # 推荐只看厂商与 DirectX 12 支持，按型号去重即可。
    unique: dict[tuple[int, int, str], GpuAdapter] = {}
    for adapter in found:
        unique.setdefault((adapter.vendor_id, adapter.device_id, adapter.name), adapter)
    adapters = tuple(unique.values())
    for adapter in adapters:
        logger.info(
            "检测到显卡：%s（VendorId 0x%04X，专用显存 %d MB，DirectX 12：%s）",
            adapter.name,
            adapter.vendor_id,
            adapter.dedicated_memory // (1024 * 1024),
            "支持" if adapter.d3d12 else "不支持",
        )
    return adapters


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = (
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        )

    def _guid(text: str) -> _GUID:
        parts = text.split("-")
        tail = bytes.fromhex(parts[3] + parts[4])
        return _GUID(int(parts[0], 16), int(parts[1], 16), int(parts[2], 16), (ctypes.c_ubyte * 8)(*tail))

    class _LUID(ctypes.Structure):
        _fields_ = (("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG))

    class _AdapterDesc1(ctypes.Structure):
        _fields_ = (
            ("Description", wintypes.WCHAR * 128),
            ("VendorId", wintypes.UINT),
            ("DeviceId", wintypes.UINT),
            ("SubSysId", wintypes.UINT),
            ("Revision", wintypes.UINT),
            ("DedicatedVideoMemory", ctypes.c_size_t),
            ("DedicatedSystemMemory", ctypes.c_size_t),
            ("SharedSystemMemory", ctypes.c_size_t),
            ("AdapterLuid", _LUID),
            ("Flags", wintypes.UINT),
        )

    _IID_FACTORY1 = "770aae78-f26f-4dba-a829-253c83d1b387"
    _IID_D3D12_DEVICE = "189819f1-1db6-4b57-be54-1821339b85f7"

    def _method(pointer: ctypes.c_void_p, index: int, *argtypes: object) -> Callable[..., int]:
        """COM 对象虚函数表第 index 项，按 HRESULT 返回值与给出的参数类型包装。"""
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)  # type: ignore[arg-type]
        function = prototype(vtable[index])
        return lambda *args: int(function(pointer, *args))

    def _release(pointer: ctypes.c_void_p) -> None:
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])(pointer)

    def _d3d12_support() -> Callable[[ctypes.c_void_p], bool]:
        try:
            d3d12 = ctypes.WinDLL("d3d12")
        except OSError:
            return lambda _adapter: False
        create = d3d12.D3D12CreateDevice
        create.restype = ctypes.c_long
        create.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(_GUID), ctypes.c_void_p)
        iid = _guid(_IID_D3D12_DEVICE)

        def supported(adapter: ctypes.c_void_p) -> bool:
            # ppDevice 为 NULL：只检查能否创建，成功时返回 S_FALSE（1）。
            return create(adapter, D3D_FEATURE_LEVEL_11_0, ctypes.byref(iid), None) >= 0

        return supported

    def probe_adapters() -> list[GpuAdapter]:
        """用 DXGI 枚举硬件显卡，见模块说明。失败时抛 OSError。"""
        dxgi = ctypes.WinDLL("dxgi")
        create_factory = dxgi.CreateDXGIFactory1
        create_factory.restype = ctypes.c_long
        create_factory.argtypes = (ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
        factory = ctypes.c_void_p()
        result = create_factory(ctypes.byref(_guid(_IID_FACTORY1)), ctypes.byref(factory))
        if result < 0 or not factory.value:
            raise OSError(f"CreateDXGIFactory1 失败（HRESULT 0x{result & 0xFFFFFFFF:08X}）")
        d3d12 = _d3d12_support()
        adapters: list[GpuAdapter] = []
        seen: set[tuple[int, int]] = set()
        try:
            enum_adapters = _method(factory, 12, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
            for index in range(MAX_ADAPTERS):
                adapter = ctypes.c_void_p()
                result = enum_adapters(index, ctypes.byref(adapter))
                if (result & 0xFFFFFFFF) == DXGI_ERROR_NOT_FOUND or result < 0 or not adapter.value:
                    break
                try:
                    desc = _AdapterDesc1()
                    if _method(adapter, 10, ctypes.POINTER(_AdapterDesc1))(ctypes.byref(desc)) < 0:
                        continue
                    if desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE or desc.VendorId == MICROSOFT_VENDOR_ID:
                        continue
                    # 同一块显卡可能以多个适配器出现（混合显卡、远程桌面），按 LUID 去重。
                    luid = (int(desc.AdapterLuid.LowPart), int(desc.AdapterLuid.HighPart))
                    if luid in seen:
                        continue
                    seen.add(luid)
                    adapters.append(
                        GpuAdapter(
                            name=desc.Description,
                            vendor_id=int(desc.VendorId),
                            device_id=int(desc.DeviceId),
                            dedicated_memory=int(desc.DedicatedVideoMemory),
                            d3d12=d3d12(adapter),
                        )
                    )
                finally:
                    _release(adapter)
        finally:
            _release(factory)
        return adapters

else:

    def probe_adapters() -> list[GpuAdapter]:
        """不是 Windows：没有 DXGI，按没有显卡处理。"""
        return []
