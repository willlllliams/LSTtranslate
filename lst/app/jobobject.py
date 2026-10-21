"""Windows 作业对象：主进程无论怎样退出，推理进程都随之结束（需求 REL-03，架构文档 8.2 节第 8 条）。

模块持有一个进程级的作业对象，限制标志为：

- ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``：作业对象的最后一个句柄关闭时，系统结束作业里的全部进程。
  句柄只由本模块持有、不可继承，主进程退出（包括崩溃和被任务管理器结束）时由系统关闭；
- ``JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION``：作业里的进程遇到未处理的异常（例如原生库访问违例）
  时直接结束，不弹出 Windows 错误报告对话框。对话框会让崩溃的进程一直挂着，监管器就等不到它退出。

作业对象在第一次使用时创建。作业里的进程再创建的子进程自动属于同一个作业。
非 Windows 平台上各函数直接返回 False 或空列表。
"""

import logging
import sys
import threading

logger = logging.getLogger(__name__)

_PID_LIST_CAPACITY = 512

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    from typing import ClassVar

    _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x0400
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class _BasicLimitInformation(ctypes.Structure):
        _fields_: ClassVar = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]
        LimitFlags: int

    class _IoCounters(ctypes.Structure):
        _fields_: ClassVar = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_: ClassVar = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]
        BasicLimitInformation: _BasicLimitInformation

    class _ProcessIdList(ctypes.Structure):
        _fields_: ClassVar = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * _PID_LIST_CAPACITY),
        ]
        NumberOfProcessIdsInList: int
        ProcessIdList: ctypes.Array[ctypes.c_size_t]

    # 单独加载一份 kernel32，设置 argtypes 不影响其他模块使用的 ctypes.windll.kernel32。
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    _kernel32.IsProcessInJob.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _lock = threading.Lock()
    _job: int | None = None
    _job_failed = False

    def _last_error() -> str:
        code = ctypes.get_last_error()
        return f"WinError {code}：{ctypes.FormatError(code).strip()}"

    def _create_job() -> int | None:
        handle: int | None = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            logger.error("创建作业对象失败（%s），主进程崩溃时推理进程不会随之结束", _last_error())
            return None
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )
        if not _kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            logger.error("设置作业对象的限制失败（%s），主进程崩溃时推理进程不会随之结束", _last_error())
            _kernel32.CloseHandle(handle)
            return None
        return handle

    def _job_handle() -> int | None:
        """返回进程级作业对象的句柄，第一次调用时创建。创建失败只记一次日志，之后直接返回 None。"""
        global _job, _job_failed
        with _lock:
            if _job is None and not _job_failed:
                _job = _create_job()
                _job_failed = _job is None
            return _job

    def assign_process(pid: int) -> bool:
        """把进程加入作业对象。成功返回 True；失败时记录警告并返回 False，进程照常运行。"""
        job = _job_handle()
        if job is None:
            return False
        process = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            logger.warning("无法打开进程 %d 以加入作业对象（%s）", pid, _last_error())
            return False
        try:
            if not _kernel32.AssignProcessToJobObject(job, process):
                logger.warning("进程 %d 加入作业对象失败（%s）", pid, _last_error())
                return False
            return True
        finally:
            _kernel32.CloseHandle(process)

    def contains_process(pid: int) -> bool:
        """进程是否属于本模块的作业对象。进程不存在或无法查询时返回 False。"""
        job = _job_handle()
        if job is None:
            return False
        process = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return False
        try:
            result = wintypes.BOOL(False)
            if not _kernel32.IsProcessInJob(process, job, ctypes.byref(result)):
                return False
            return bool(result.value)
        finally:
            _kernel32.CloseHandle(process)

    def process_ids() -> list[int]:
        """作业对象里仍在运行的进程号（最多列出 512 个），用于诊断和测试。"""
        job = _job_handle()
        if job is None:
            return []
        info = _ProcessIdList()
        if not _kernel32.QueryInformationJobObject(
            job, _JOB_OBJECT_BASIC_PROCESS_ID_LIST, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            logger.warning("查询作业对象的进程列表失败（%s）", _last_error())
            return []
        count = min(int(info.NumberOfProcessIdsInList), _PID_LIST_CAPACITY)
        return [int(info.ProcessIdList[i]) for i in range(count)]

else:

    def assign_process(pid: int) -> bool:
        """非 Windows 平台没有作业对象。"""
        return False

    def contains_process(pid: int) -> bool:
        """非 Windows 平台没有作业对象。"""
        return False

    def process_ids() -> list[int]:
        """非 Windows 平台没有作业对象。"""
        return []
