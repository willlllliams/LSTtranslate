# Windows 10 兼容说明

## 支持范围

LSTtranslate 1.1.0 支持 Windows 10 22H2 x64（build 19045）和 Windows 11。两种系统共用普通版、开发版安装包及应用代码。2026 年 10 月 23 日的 Windows 10 验收使用干净安装的专业版、英文系统区域、中文和空格组成的工作目录；虚拟机配置为 4 个虚拟处理器、3 GB 内存。CPU 识别和翻译完成了真实推理。

运行环境为 CPython 3.13、PySide6 Essentials 6.12.0、winrt-runtime 3.2.1 和 CTranslate2 4.8.3。[Python 3.13](https://docs.python.org/3.13/using/windows.html)和 [Qt 6.12](https://doc.qt.io/qt-6.12/windows.html)的系统范围覆盖该目标。Qt 6.12 是最后支持 Windows 10 的系列，项目继续锁定 `PySide6-Essentials>=6.12,<6.13`。

本版验收范围为 build 19045。Windows 10 1809、LTSC 2019 和 ARM64 未进入本版支持矩阵。

## 窗口、字体与图标

安装器的最低版本为 `10.0.19045`。普通版和开发版保留相同 AppId，覆盖升级延续既有配置、数据位置和模型位置。

Windows 10 使用不透明主题背景。Windows 11 build 22621 起使用 Mica；系统不支持时，主题层清除窗口透明属性并绘制主题底色。[微软的背景接口说明](https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ne-dwmapi-dwm_systembackdrop_type)给出了 Mica 所用接口的版本要求，程序最低版本由完整功能的验收结果确定。

Qt 样式按 `windows11`、`Fusion`、`windowsvista`、`windows` 的顺序选择可用项。验收机器的 Qt 提供 `windows11` 样式，并在 Windows 10 上正常运行；缺少该样式的回退另有自动化测试。正文继续按 Segoe UI Variable、Segoe UI、Microsoft YaHei UI 选择字体。

图标优先使用 Segoe Fluent Icons，Windows 10 回退到系统自带的 [Segoe MDL2 Assets](https://learn.microsoft.com/en-us/windows/apps/design/iconography/segoe-ui-symbol-font)。主题层逐码位检查实际字形，图标引擎、文字标签、导航箭头和自绘控件共用解析函数。

| 检查项 | Windows 10 实测 |
| --- | --- |
| 系统 Fluent 字体 | 未安装 |
| 采用的图标字体 | Segoe MDL2 Assets |
| 项目定义的图标 | 63 个 |
| MDL2 原始缺字 | `LAYOUT`（U+E9A3）、`BLOCKED`（U+E733） |
| 缺字映射 | 布局使用网格图标；阻止状态使用错误图标 |
| 解析后的全部图标和导航箭头 | 均有有效字形 |

深色和浅色设置窗口均经过安装版原生截图检查。翻译覆盖框继续使用原图坐标；字号测量在实际设备像素中进行，再转换回原图坐标，窗口缩放和屏幕变化时重新排版。

## 应用内运行库与中文路径

独立 OCR、NMT 工作进程在导入 Qt 之前就会加载推理库，因此运行库必须能从解释器目录直接解析。构建从锁定的 `msvc-runtime==14.51.36231` 复制 12 个 MSVC DLL 到 `runtime`，包含 Paddle 的 `mkldnn.dll` 所需的 `vcomp140.dll`。包的许可证随主依赖保留。该布局采用微软说明的[应用内运行库分发方式](https://learn.microsoft.com/en-us/cpp/windows/choosing-a-deployment-method?view=msvc-160)。

中文路径还涉及 C++ 文件接口的字符编码。SentencePiece 改为接收 Python 从模型文件读取的字节；模型内容和分词行为保持原有实现。分发的 `python.exe` 与 `pythonw.exe` 在原有程序清单中加入 `activeCodePage=UTF-8`，保留权限、长路径与其余设置。构建自检断言新进程的 `GetACP()` 为 65001。

[微软的 UTF-8 代码页说明](https://learn.microsoft.com/en-us/windows/apps/design/globalizing/use-utf8-code-page)规定，Windows 10 1903 起可以通过程序清单为进程启用 UTF-8 ANSI 接口。该设置随程序生效，用户无需修改系统区域。Windows 10 验收已在英文系统区域和中文模型路径中完成 Paddle 模型加载、识别，以及 SentencePiece 与 CTranslate2 的真实翻译。

## 识别与翻译验证

| 后端 | Windows 10 实测范围 |
| --- | --- |
| Windows OCR | 真实截屏、英语与自动语言、段落整理、NMT 翻译、覆盖、选择和复制 |
| RapidOCR CPU | 按安装版精确清单部署环境，识别清晰 GitHub 弹窗 |
| PaddleOCR CPU | 按安装版精确清单部署环境，加载 PP-OCRv5 轻量检测与识别模型，识别同一弹窗 |
| CTranslate2 CPU | 冷进程导入、中英互译、中文与空格模型路径 |
| 翻译框排版 | 扩展列表、博客和 GitHub 弹窗三个样本，92 个翻译框全部显示完整，字号达到原文估算值 |

Windows OCR 的系统 API 要求和 PyWinRT 的系统要求均覆盖 22H2。[微软的桌面 OCR 文档](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr)仍要求受支持的桌面调用具有包身份。本版采用 Inno Setup EXE，成功结论限定于已实测的系统和调用链；RapidOCR 提供独立识别后端。

字号比较采用识别行框和字形测量得到的原文字号估计。92 个框的结果使用固定参考译文，分别在 Windows 10、Windows 11 原生字体下检查。窗口外边界、相邻内容和译文长度仍约束可用空间，空间不足时程序降低字号。布局参数与证据解释见 [识别质量说明](ocr-quality.md)。

## 安装和配置

普通版首次启动默认关闭自动化接口，开发版默认开启。两种安装包的交叉升级保留显式开启或关闭状态；翻译框与段落配置经过保存、实际进程重启和再次读取验证。

数据目录复制、模型目录移动及重启后的路径恢复均通过。卸载保留配置、模型和位置记录；选择性清理删除应用所属内容，并保留同目录的个人文件。安装验收使用隔离的数据目录。

## 验证边界

虚拟机使用 CPU 后端。NVIDIA CUDA 与 AMD、Intel DirectML 的实际硬件组合仍需分别验证。DirectML 的系统条件覆盖 Windows 10 1903 及以上，当前 [CUDA Windows 支持表](https://docs.nvidia.com/cuda/cuda-installation-guide-microsoft-windows/)包含 Windows 10 22H2；这些上游条件说明可以保留现有后端，不能代替项目的 GPU 推理验收。

原生字体、深浅色主题及单屏截图链路已验证。多屏混合缩放与负坐标的坐标算法由既有回归覆盖，Windows 10 的实际多显示器组合仍需补充硬件验证。

本版发布结果与测试矩阵见 [1.1 发布验收](1.1-readiness.md)。
