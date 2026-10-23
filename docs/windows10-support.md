# Windows 10 兼容方案

核实日期：2026 年 10 月 23 日。代码基线为 1.0.0 及当前翻译框布局改动；运行环境为 CPython 3.13、PySide6 Essentials 6.12.0、winrt-runtime 3.2.1、CTranslate2 4.8.3。

## 结论与支持范围

LSTtranslate 具备适配 Windows 10 的依赖基础。首个兼容目标应定为 Windows 10 22H2 x64（build 19045），继续使用同一套代码和安装包。当前阻止安装的直接原因是安装器把 Mica 的最低版本当成了整个程序的最低版本；主要界面缺口是只使用 Windows 11 的图标字体。

Python 和 Qt 可以保留现有版本。[Python 3.13 文档](https://docs.python.org/3.13/using/windows.html)列出的最低 Windows 版本为 8.1；[Qt 6.12 的支持平台](https://doc.qt.io/qt-6.12/windows.html)包含 Windows 10 1809 及以后。Qt 同时明确，6.12 是最后支持 Windows 10 的系列，仓库现有 `PySide6-Essentials>=6.12,<6.13` 约束应继续保留。

选择 22H2 作为首个验收目标，是为了统一普通桌面、DirectML 与 CUDA 的测试系统。Qt 的 1809 下限只约束 Qt；它无法证明所有 OCR 和 GPU wheel 都能在 1809 上运行。1809 与 LTSC 2019 应作为后续单独的兼容目标。

本次核实完成了代码审核、上游系统要求查询和当前 Windows 11 主机的图标字形检查。Windows 10 安装与运行验收尚未执行，当前发布包仍要求 Windows 11 22H2。

## 已确认的差异

| 部分 | 当前代码或依赖依据 | 适配方式 |
| --- | --- | --- |
| 安装器 | `installer/lsttranslate.iss` 设置 `MinVersion=10.0.22621` | 完成兼容验证后改为 `10.0.19045`，普通版与开发版使用同一限制 |
| Mica 背景 | `mica_supported()` 按 build 22621 检查；公共窗口已有失败回退 | Windows 10 使用现有不透明主题底色，Windows 11 保持 Mica |
| 界面正文 | `ui_font()` 已按 Segoe UI Variable、Segoe UI、Microsoft YaHei UI 回退 | 保留现有顺序，验证 Windows 10 的中英文排版 |
| 图标 | `fluent_icons_available()` 只检查 Segoe Fluent Icons；缺失时图标引擎直接跳过绘制 | 增加 Segoe MDL2 Assets 回退，并检查实际使用的码位 |
| Qt 控件样式 | `apply_app_style()` 已检查 `windows11` 样式是否存在 | 在样式缺失时明确选择可用回退样式，并验证深浅色；正文、卡片仍使用统一主题颜色 |
| 截屏 | 使用 `QScreen.grabWindow(0)` 和现有屏幕坐标转换 | 验证多屏、混合缩放和负坐标屏幕，无需另写 Windows 10 截屏后端 |
| OCR 与翻译 | Windows OCR、RapidOCR、PaddleOCR 和本地翻译采用独立后端或工作进程 | 分别验证，主界面可用与各推理后端可用分开判定 |

### 安装器与 Mica

[微软的 DWM_SYSTEMBACKDROP_TYPE 文档](https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ne-dwmapi-dwm_systembackdrop_type)规定，该背景接口最低要求 Windows 11 build 22621。这解释了现有安装器限制，但该接口只负责背景材质。

`lst/ui/theme/__init__.py` 中的 `mica_supported()` 在旧系统返回 `False`，不会调用该 DWM 属性。`lst/ui/common/window.py` 把 Mica 结果保存在窗口状态中；设置窗口在 Mica 未启用时使用 `theme_colors().window` 填充背景。失败路径还会撤销透明窗口属性。因此适配 Windows 10 时应复用这条回退路径，保留普通不透明窗口。

安装器只需调整一处最低版本，但这项调整应与图标适配及 Windows 10 实测一起交付。改变安装条件本身只能证明安装器放行，不能证明后端 DLL 和界面运行正常。

### 图标字体

微软说明，Windows 10 自带 [Segoe MDL2 Assets](https://learn.microsoft.com/en-us/windows/apps/design/iconography/segoe-ui-symbol-font)，Windows 11 推荐使用 [Segoe Fluent Icons](https://learn.microsoft.com/en-us/windows/apps/design/iconography/segoe-fluent-icons-font)。这些图标使用 Unicode 私用区码位，普通文本字体的自动回退无法保证图标含义和外形正确。

当前 `fluent_icons_available()` 在 Fluent 字体不存在时返回 `False`，`_GlyphIconEngine.paint()` 随后直接返回。因此缺少 Fluent 字体的 Windows 10 会出现空图标。直接使用 `fluent_icon_font()` 的标签和自绘区域也要覆盖，适配范围包括 `settings/controls.py`、`nav.py`、`flow.py`、状态卡片和关于页面。

在当前主机通过 `QRawFont.supportsCharacter()` 检查 `Glyph` 定义的全部 63 个码位，结果如下：

| 字体 | 有字形的码位数 | 缺失码位 |
| --- | --- | --- |
| Segoe Fluent Icons | 63 / 63 | 无 |
| Segoe MDL2 Assets | 61 / 63 | `LAYOUT`：U+E9A3；`BLOCKED`：U+E733 |

这项结果来自本机 Windows 11 安装的 MDL2 字体，Windows 10 字体仍须逐项检查。字体回退应按 Fluent、MDL2 的顺序选择；缺字时使用明确映射或程序绘制的矢量图标。布局可选已有网格图标，禁止状态可绘制圆圈与斜杠。图标按钮、标签与自绘区域应共用解析结果，避免只修复 `QIcon` 路径。

图标检查还应覆盖展开箭头等代码中的直接码位，不能只检查 `Glyph` 常量。验收标准为每个控件均显示正确含义的图标，并且缩放、禁用态和主题切换后仍可辨认。

## OCR、翻译与 GPU

### Windows 自带 OCR

[OcrEngine 的 API 要求](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine)为 Windows 10 build 10240，当前程序使用的创建引擎、语言枚举和图像识别属于这套 API。[PyWinRT 官方仓库](https://github.com/pywinrt/pywinrt/blob/main/projection/readme.md)列出的运行前提为 Windows 10 October 2018 Update 及以后。这两项版本要求均覆盖 22H2。

还需处理打包身份与 OCR 语言组件。[微软的 Windows.Media.Ocr 命名空间文档](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr)明确规定桌面程序受支持的调用方式要求包身份。当前 Inno Setup 安装方式没有注册 MSIX 包身份；Windows 11 上已有的成功测试只能证明相应测试环境能够运行。Windows 10 上必须通过安装版实际枚举语言、创建引擎并完成一次识别，才能给出当前 EXE 布局的兼容结论。

项目已经把读取语言失败、语言列表为空表示为引擎不可用状态。兼容版本应保留主程序可启动的行为，并向用户提供 RapidOCR CPU 作为独立后端。若交付要求包含微软文档规定的 Windows OCR 桌面支持方式，则需追加带包身份的打包方案，并验证 OCR 子进程的身份；只给界面入口增加一个清单不能代替工作进程验收。

### CPU 后端与运行库

RapidOCR CPU、PaddleOCR CPU 和 CTranslate2 本地翻译应先在干净 Windows 10 上验收。它们能把识别与翻译完整链路和显卡驱动条件分开验证。Paddle 官方提供 [Windows pip 安装说明](https://www.paddlepaddle.org.cn/documentation/docs/en/install/pip/windows-pip_en.html)，包括 Python 3.13、x64 环境；这份说明没有给出项目当前所有 wheel 的 Windows 10 实测结果。

[CTranslate2 安装文档](https://opennmt.net/CTranslate2/installation.html)列出 Windows x86-64 和 Visual C++ 运行库要求。当前安装暂存目录带有 CPython 的 `vcruntime140.dll`、`vcruntime140_1.dll`，PySide6、shiboken6 与 winrt 目录也带有 MSVC DLL。这证明安装包包含相关文件，DLL 是否被每个工作进程正确解析仍要在没有开发工具的干净系统上检查。

验收应直接启动安装目录中的主程序和 OCR、NMT 工作进程，检查模块导入及实际推理。若缺 DLL，先定位缺失模块与加载目录，再决定补充应用内运行库或安装先决条件。

按需下载运行环境所使用的 [uv 官方支持平台](https://docs.astral.sh/uv/reference/platforms/)包含 Windows x86-64，最低 Windows 版本为 Windows 10。仍需验证安装包所带 `uv.exe` 的实际下载、取消、重试和导入检查。

### GPU 后端

现有 GPU 路线可以继续保留，但要按后端分别验收。

| 后端 | 已核实的系统依据 | Windows 10 验收内容 |
| --- | --- | --- |
| RapidOCR DirectML | ONNX Runtime 文档要求 DirectX 12 设备，DirectML 自 Windows 10 1903 提供 | 确认实际加载 DML provider，完成检测和识别，并记录推理耗时 |
| RapidOCR CUDA | 当前安装暂存清单为 ONNX Runtime GPU 1.31.0、CUDA runtime 13.4.92、cuDNN 9.27.0.42 | 核对驱动和 GPU 架构，实际加载 CUDA provider |
| PaddleOCR CUDA | 当前清单使用 Paddle 官方 cu129 的 GPU wheel | 完成 GPU 环境安装、模型加载和真实推理，检查退出与取消 |
| 本地翻译 CUDA | 当前 NMT 运行库为 cuBLAS 12 | 使用实际下载的 DLL 和 CTranslate2 完成译文生成 |

[ONNX Runtime 的 DirectML 文档](https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html)给出了上述 DirectX 12 与系统要求。[CUDA 13.0 的 Windows 支持表](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-installation-guide-microsoft-windows/index.html)明确包含 Windows 10 22H2，当前 [CUDA Windows 文档](https://docs.nvidia.com/cuda/cuda-installation-guide-microsoft-windows/)也保留该系统。这说明 Windows 10 并不要求统一退回旧 CUDA；具体 GPU wheel 的兼容结论还取决于它实际捆绑的库与驱动。

## 实施顺序与验收

适配工作按以下顺序进行，先完成界面与 CPU 链路，再增加 GPU 组合。

1. 在主题层加入图标字体回退与缺字映射，覆盖图标引擎、直接字体标签和自绘控件。明确样式缺失时的回退，保留现有正文字体和纯色背景回退。
2. 将兼容构建的最低版本改为 `10.0.19045`，保留同一 AppId、安装目录和升级路径。需求、架构和 README 的系统要求在验收通过后同步更新。
3. 在干净 Windows 10 22H2 x64 上安装普通版与开发版，分别验证首次启动、升级、重启、退出、卸载保留数据和选择删除数据。
4. 验证设置、双语窗口、托盘、全局快捷键、复制、原文切换和覆盖窗口。在 100%、125%、150%、200% 缩放及混合缩放双屏下检查位置、字体、选区与翻译框。使用现有固定参考译文检查完整显示和字号。
5. 在系统 OCR 语言可用与缺失两种条件下验证状态反馈；对 Windows OCR、RapidOCR CPU、PaddleOCR CPU 和本地翻译执行实际工作进程推理。中文用户名与含空格路径纳入安装和模型下载测试。
6. 分别验证可用的 NVIDIA CUDA、AMD 或 Intel DirectML 设备。没有完成的 GPU 组合单独记录，不能由 CPU 测试结果代替。
7. 将 Windows 10 原生截图和安装版测试纳入发布验收，同时保留 Windows 11 的 Mica、主题和完整识别翻译回归。

预计代码修改集中在安装器和主题层，验证范围覆盖完整安装版。继续使用现有 Python、Qt、截屏链路和推理进程架构，可以减少分支维护，并让 Windows 10 与 Windows 11 共享后续识别和翻译框改进。
