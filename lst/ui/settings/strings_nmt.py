"""模型页“显卡加速”分组的界面文案（需求 MOD-06）：推理设备下拉框与 NVIDIA cuBLAS 12 运行库卡片。

导入本模块时登记文案。推理设备按设置项的约定取键：``settings.f.nmt.device.title`` 与 ``.desc``，
选项为 ``settings.choice.nmt.device.<取值>``。显卡运行库卡片的文案都在 ``settings.nmt_runtime.`` 之下；
下载失败的原因与处理建议是共用的错误文案 ``error.nmt_runtime.<原因>``，登记在 ui.i18n。

界面不显示技术细节（架构文档 10.3 节），文案里不出现配置字段名与错误码。
"""

from typing import Final

from lst.ui.i18n import Catalog, register_catalog


def _pair(zh: str, en: str) -> dict[str, str]:
    return {"zh-Hans": zh, "en": en}


CATALOG: Final[Catalog] = {
    # 分组
    "settings.section.nmt_gpu": _pair("显卡加速", "GPU acceleration"),
    "settings.nmt_gpu.intro": _pair(
        "本地翻译模型默认在 CPU 上运行。下载 NVIDIA 显卡运行库后，装有 NVIDIA 显卡的电脑可以改在显卡上运行，"
        "长文本翻译更快。",
        "Local translation models run on the CPU by default. After you download the NVIDIA GPU runtime, "
        "a PC with an NVIDIA graphics card can run them on the GPU, which translates long texts faster.",
    ),
    # 推理设备（[nmt].device）
    "settings.f.nmt.device.title": _pair("推理设备", "Inference device"),
    "settings.f.nmt.device.desc": _pair(
        "自动：装了显卡运行库且有 NVIDIA 显卡时用显卡，否则用 CPU。"
        "选“NVIDIA 显卡（CUDA）”而显卡运行库没有安装时，本地翻译进程会出错。修改后本地翻译进程自动重启。",
        "Auto uses the GPU when the GPU runtime is installed and an NVIDIA graphics card is present, "
        "otherwise the CPU. If you choose NVIDIA GPU (CUDA) without the GPU runtime installed, "
        "the local translation process fails. The local translation process restarts after a change.",
    ),
    "settings.choice.nmt.device.auto": _pair("自动", "Auto"),
    "settings.choice.nmt.device.cpu": _pair("仅 CPU", "CPU only"),
    "settings.choice.nmt.device.cuda": _pair("NVIDIA 显卡（CUDA）", "NVIDIA GPU (CUDA)"),
    # 显卡运行库卡片
    "settings.nmt_runtime.title": _pair("NVIDIA cuBLAS 12 运行库", "NVIDIA cuBLAS 12 runtime"),
    "settings.nmt_runtime.desc": _pair(
        "本地翻译模型在 NVIDIA 显卡上运行所需的数学运算库。安装包不带它，需要显卡加速时在这里下载。",
        "The math library that local translation models need to run on an NVIDIA graphics card. "
        "It is not part of the installer; download it here when you want GPU acceleration.",
    ),
    "settings.nmt_runtime.facts": _pair(
        "下载大小 {size} · 版本 {version}", "Download size {size} · version {version}"
    ),
    "settings.nmt_runtime.facts_no_version": _pair("下载大小 {size}", "Download size {size}"),
    "settings.nmt_runtime.source": _pair(
        "来源：PyPI 上 NVIDIA 发布的 nvidia-cublas-cu12，按 NVIDIA 软件许可分发。",
        "Source: nvidia-cublas-cu12 published by NVIDIA on PyPI, distributed under the NVIDIA software "
        "license.",
    ),
    "settings.nmt_runtime.status.unavailable": _pair(
        "暂时无法管理显卡运行库", "The GPU runtime cannot be managed right now"
    ),
    "settings.nmt_runtime.status.not_installed": _pair("未安装", "Not installed"),
    "settings.nmt_runtime.status.downloading": _pair("正在下载 {percent}%", "Downloading {percent}%"),
    "settings.nmt_runtime.status.downloading_unknown": _pair("正在下载", "Downloading"),
    "settings.nmt_runtime.status.installing": _pair("正在校验与解压", "Verifying and extracting"),
    "settings.nmt_runtime.status.installed": _pair(
        "已安装 {version}，占用 {size}", "Installed {version}, using {size}"
    ),
    "settings.nmt_runtime.status.update": _pair(
        "已安装 {installed}，占用 {size}；可以更新到 {version}",
        "Installed {installed}, using {size}; {version} is available",
    ),
    "settings.nmt_runtime.status.error": _pair("下载失败：{reason}", "Download failed: {reason}"),
    "settings.nmt_runtime.status.error_kept": _pair(
        "下载失败：{reason}。已安装的 {version} 仍然可用",
        "Download failed: {reason}. The installed {version} is still usable",
    ),
    "settings.nmt_runtime.suggestion": _pair("处理建议：{suggestion}", "What to do: {suggestion}"),
    "settings.nmt_runtime.gpu.found": _pair(
        "检测到 NVIDIA 显卡：{names}", "NVIDIA graphics card found: {names}"
    ),
    "settings.nmt_runtime.gpu.none": _pair(
        "没有检测到 NVIDIA 显卡，下载后也用不上",
        "No NVIDIA graphics card was found, so the runtime would not be used",
    ),
    "settings.nmt_runtime.gpu.separator": _pair("、", ", "),
    "settings.nmt_runtime.device.cuda": _pair(
        "本地翻译当前在显卡上运行", "Local translation is running on the GPU"
    ),
    "settings.nmt_runtime.device.cpu": _pair(
        "本地翻译当前在 CPU 上运行", "Local translation is running on the CPU"
    ),
    "settings.nmt_runtime.device.cpu_unexpected": _pair(
        "本地翻译当前在 CPU 上运行。显卡运行库已安装，也检测到了 NVIDIA 显卡，却没有用上显卡："
        "请导出诊断信息，在日志里查看本地翻译进程加载显卡运行库的记录。",
        "Local translation is running on the CPU. The GPU runtime is installed and an NVIDIA graphics card "
        "was found, but the GPU is not used: export diagnostics and check the log entries of the local "
        "translation process loading the GPU runtime.",
    ),
    "settings.nmt_runtime.download": _pair("下载（{size}）", "Download ({size})"),
    "settings.nmt_runtime.update": _pair("更新（{size}）", "Update ({size})"),
    "settings.nmt_runtime.cancel": _pair("取消", "Cancel"),
    "settings.nmt_runtime.retry": _pair("重试", "Retry"),
    "settings.nmt_runtime.delete": _pair("删除", "Delete"),
    "settings.nmt_runtime.progress": _pair("显卡运行库的下载进度", "GPU runtime download progress"),
    "settings.nmt_runtime.delete.confirm": _pair(
        "删除 NVIDIA cuBLAS 12 运行库（占用 {size}）？删除时本地翻译进程会短暂重启，"
        "之后本地翻译不再使用显卡。需要时可以重新下载。",
        "Delete the NVIDIA cuBLAS 12 runtime ({size})? The local translation process restarts briefly "
        "during deletion and stops using the GPU afterwards. You can download it again at any time.",
    ),
}

register_catalog(CATALOG)


__all__ = ["CATALOG"]
