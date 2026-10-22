"""模型页显卡运行库卡片的显示内容（需求 MOD-06）：把 ``NmtRuntimeInfo`` 与 NMT 进程的状态整理成文字与按钮。

不依赖 Qt，可以在没有 QApplication 的测试里检查。每种状态对应的文字与按钮：

| 状态 | 文字 | 按钮 |
| --- | --- | --- |
| 没有显卡运行库的状态 | 暂时无法管理 | 无 |
| 未安装 | 未安装 | 下载（标出大小） |
| 下载中 | 正在下载 xx%，进度条 | 取消 |
| 校验与解压 | 正在校验与解压，忙碌进度条 | 无 |
| 已安装 | 已安装的版本与占用空间；有新版本时注明 | 删除；有新版本时另有“更新” |
| 出错 | 原因与处理建议；之前装好的版本仍然可用时注明 | 重试；之前装好的版本还在时另有“删除” |

卡片下方还有两行：

- 显卡行：检测到的 NVIDIA 显卡名称；没有检测到时说明下载后也用不上，下载按钮照常可用；
- 实际设备行：NMT 进程报告的实际设备（显卡或 CPU）。显卡运行库已安装、有 NVIDIA 显卡、配置的推理设备
  不是“仅 CPU”，实际却在 CPU 上运行时，另起一句提示查看日志。没有 NMT 进程的设备信息时不显示这一行。
"""

from dataclasses import dataclass
from typing import Final

from lst.contracts.nmt_runtime import NmtRuntimeErrorCode, NmtRuntimeInfo, NmtRuntimeState
from lst.ui.common.text import clamp_fraction, format_size
from lst.ui.i18n import error_text, tr
from lst.ui.settings.ocr_engine_rows import Tone
from lst.ui.theme import Glyph

GPU_DEVICE: Final = "cuda"
CPU_DEVICE: Final = "cpu"


@dataclass(frozen=True, slots=True)
class RuntimeView:
    """显卡运行库卡片的显示内容。文字都已按当前界面语言填好，空串表示不显示那一行。

    progress 是进度条的取值：None 表示不显示进度条，-1 表示进度未知（忙碌动画），否则在 0 到 1 之间。
    device_warning 为真时实际设备行用错误色，表示该用显卡却在 CPU 上运行。
    """

    title: str
    description: str
    status: str
    tone: Tone
    glyph: str
    suggestion: str
    progress: float | None
    gpu: str
    gpu_glyph: str
    device: str
    device_warning: bool
    can_download: bool
    download_text: str
    can_cancel: bool
    can_retry: bool
    can_delete: bool


def size_text(size: int) -> str:
    """字节数的显示文字；0 或负数表示大小未知。"""
    return format_size(size if size > 0 else None)


def description_text(info: NmtRuntimeInfo | None) -> str:
    """卡片的说明：作用，下一行是下载大小与版本，再下一行是来源与许可。"""
    lines = [tr("settings.nmt_runtime.desc")]
    if info is not None:
        size = size_text(info.download_bytes)
        if info.version:
            lines.append(tr("settings.nmt_runtime.facts", size=size, version=info.version))
        else:
            lines.append(tr("settings.nmt_runtime.facts_no_version", size=size))
    lines.append(tr("settings.nmt_runtime.source"))
    return "\n".join(lines)


def _has_update(info: NmtRuntimeInfo) -> bool:
    return (
        bool(info.version) and info.installed_version is not None and info.installed_version != info.version
    )


def _status(info: NmtRuntimeInfo) -> tuple[str, Tone, str]:
    """状态文字、语气与图标。"""
    match info.state:
        case NmtRuntimeState.NOT_INSTALLED:
            return tr("settings.nmt_runtime.status.not_installed"), Tone.NORMAL, Glyph.DOWNLOAD
        case NmtRuntimeState.DOWNLOADING:
            if info.progress is None:
                return tr("settings.nmt_runtime.status.downloading_unknown"), Tone.ACTIVE, Glyph.DOWNLOAD
            percent = int(clamp_fraction(info.progress) * 100)
            return tr("settings.nmt_runtime.status.downloading", percent=percent), Tone.ACTIVE, Glyph.DOWNLOAD
        case NmtRuntimeState.INSTALLING:
            return tr("settings.nmt_runtime.status.installing"), Tone.ACTIVE, Glyph.SYNC
        case NmtRuntimeState.INSTALLED:
            size = size_text(info.installed_bytes)
            installed = info.installed_version or info.version
            if _has_update(info):
                text = tr(
                    "settings.nmt_runtime.status.update", installed=installed, size=size, version=info.version
                )
            else:
                text = tr("settings.nmt_runtime.status.installed", version=installed, size=size)
            return text, Tone.NORMAL, Glyph.COMPLETED
        case NmtRuntimeState.ERROR:
            reason, _ = error_text(info.error_code, NmtRuntimeErrorCode.INTERNAL)
            if info.installed_version is not None:
                text = tr(
                    "settings.nmt_runtime.status.error_kept", reason=reason, version=info.installed_version
                )
            else:
                text = tr("settings.nmt_runtime.status.error", reason=reason)
            return text, Tone.ERROR, Glyph.ERROR


def _progress(info: NmtRuntimeInfo) -> float | None:
    if info.state is NmtRuntimeState.INSTALLING:
        return -1.0
    if info.state is NmtRuntimeState.DOWNLOADING:
        return -1.0 if info.progress is None else clamp_fraction(info.progress)
    return None


def gpu_text(info: NmtRuntimeInfo) -> tuple[str, str]:
    """显卡行的文字与图标。"""
    if info.gpus:
        names = tr("settings.nmt_runtime.gpu.separator").join(info.gpus)
        return tr("settings.nmt_runtime.gpu.found", names=names), Glyph.CHIP
    return tr("settings.nmt_runtime.gpu.none"), Glyph.WARNING


def device_text(info: NmtRuntimeInfo | None, configured: str | None, actual: str | None) -> tuple[str, bool]:
    """实际设备行的文字，以及是否要提示查看日志。actual 不是 cuda 或 cpu 时不显示这一行。

    configured 是配置里的推理设备（auto、cpu、cuda），actual 是 NMT 进程报告的设备。
    """
    if actual == GPU_DEVICE:
        return tr("settings.nmt_runtime.device.cuda"), False
    if actual != CPU_DEVICE:
        return "", False
    unexpected = (
        info is not None
        and info.installed_version is not None
        and bool(info.gpus)
        and configured != CPU_DEVICE
    )
    if unexpected:
        return tr("settings.nmt_runtime.device.cpu_unexpected"), True
    return tr("settings.nmt_runtime.device.cpu"), False


def build_view(info: NmtRuntimeInfo | None, *, configured: str | None, actual: str | None) -> RuntimeView:
    """显卡运行库卡片的内容。info 为 None 表示总线上还没有显卡运行库的状态，此时没有任何按钮。"""
    title = tr("settings.nmt_runtime.title")
    device, warning = device_text(info, configured, actual)
    if info is None:
        return RuntimeView(
            title=title,
            description=description_text(None),
            status=tr("settings.nmt_runtime.status.unavailable"),
            tone=Tone.NORMAL,
            glyph=Glyph.BLOCKED,
            suggestion="",
            progress=None,
            gpu="",
            gpu_glyph="",
            device=device,
            device_warning=warning,
            can_download=False,
            download_text="",
            can_cancel=False,
            can_retry=False,
            can_delete=False,
        )
    status, tone, glyph = _status(info)
    state = info.state
    suggestion = ""
    if state is NmtRuntimeState.ERROR:
        _, advice = error_text(info.error_code, NmtRuntimeErrorCode.INTERNAL)
        suggestion = tr("settings.nmt_runtime.suggestion", suggestion=advice)
    update = state is NmtRuntimeState.INSTALLED and _has_update(info)
    size = size_text(info.download_bytes)
    download_key = "settings.nmt_runtime.update" if update else "settings.nmt_runtime.download"
    gpu, gpu_glyph = gpu_text(info)
    return RuntimeView(
        title=title,
        description=description_text(info),
        status=status,
        tone=tone,
        glyph=glyph,
        suggestion=suggestion,
        progress=_progress(info),
        gpu=gpu,
        gpu_glyph=gpu_glyph,
        device=device,
        device_warning=warning,
        can_download=state is NmtRuntimeState.NOT_INSTALLED or update,
        download_text=tr(download_key, size=size),
        can_cancel=state is NmtRuntimeState.DOWNLOADING,
        can_retry=state is NmtRuntimeState.ERROR,
        can_delete=info.installed_version is not None
        and state in (NmtRuntimeState.INSTALLED, NmtRuntimeState.ERROR),
    )


__all__ = [
    "CPU_DEVICE",
    "GPU_DEVICE",
    "RuntimeView",
    "build_view",
    "description_text",
    "device_text",
    "gpu_text",
    "size_text",
]
