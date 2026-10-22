# LSTtranslate 新版架构设计

## 1 设计目标

本文档根据 [需求规格说明](requirements.md) 给出新版本的技术选型、进程模型、模块划分和核心算法。文中的需求编号（例如 `RACE-03`）指向需求文档的对应条目，第 13 章列出每条需求在本文档中的落点。

架构由四条需求驱动，后面各章的决定都能追溯到其中一条或几条：

1. 竞速翻译（`RACE-01` 至 `RACE-10`）是产品核心。合并规则必须可证明正确，并且能脱离界面测试。
2. 推理崩溃不能带走界面（`REL-01`、`REL-02`）。旧版本靠“启动崩溃标记”在下次启动时降级运行，说明推理代码在同一进程里出错时界面无法自保，所以推理必须和界面分进程运行。
3. 竞速过程中界面保持流畅（`REL-04`），引擎结果到界面开始显示新译文不超过 50 毫秒（需求 6.1 节）。
4. 模块边界由工具检查（`MNT-01` 至 `MNT-03`）：业务模块之间、界面模块之间互不 import，跨层依赖只能沿第 4.2 节规定的方向。

## 2 技术选型

本章的版本号、许可证和性能数据都在 2026-10-21 按官方文档、PyPI 和代码仓库查证。标“本机实测”的数据来自开发机：Windows 11 build 26300，缩放 125%。

### 2.1 选型结果

| 关注点 | 选型 | 版本 |
|---|---|---|
| 语言与包管理 | Python 3.13，uv | 3.13.15，uv 0.12 |
| 界面框架 | PySide6，全部使用 Qt Widgets | 6.12.0（Qt 6.12 LTS） |
| Fluent 外观 | 自行实现（`lst/ui/theme`）：Qt `windows11` 样式、DWM Mica、Segoe Fluent Icons 字体 | 随 Qt 与 Windows 11 |
| Qt 与 asyncio 整合 | qasync | 0.28.0 |
| HTTP 客户端 | httpx；流式响应按服务器发送事件（Server-Sent Events，SSE）格式自行解析 | 0.28.1 |
| 流式 JSON 解析 | 自写的增量结构扫描，单项用 jiter 严格解析 | 0.17.0 |
| 推理进程通信 | subprocess 启动，`multiprocessing.connection` 命名管道加 authkey | 标准库 |
| OCR 引擎（默认） | Windows 自带 OCR（Windows.Media.Ocr），经 pywinrt 调用，装在主环境 | 随 Windows 11 |
| OCR 引擎（轻量） | RapidOCR，onnxruntime 运行 PP-OCR 模型，按需安装的独立运行环境 | rapidocr 3.10.0；onnxruntime 1.31.0、onnxruntime-directml 1.24.4、onnxruntime-gpu 1.31.0 |
| OCR 与版面分析 | PaddleOCR PP-StructureV3，按需安装的独立运行环境 | PaddleOCR 3.7.0，PaddlePaddle 3.3.1（CPU 版与 cu129 的 GPU 版） |
| OCR 文本模式（PaddleOCR） | PaddleOCR 检测加识别产线（PP-OCRv5 mobile） | PaddleOCR 3.7.0 |
| 字典翻译 | 自写的逐词查词，词典包来自 thunderbird-translate 的 dicts-v1 发布 | 标准库 |
| 本地 NMT | CTranslate2 直接加载 Argos 模型包 | CTranslate2 4.8.3 |
| 语言检测 | lingua 高精度模式，前置文字系统规则 | lingua-language-detector 2.2.0 |
| 配置 | pydantic BaseModel，tomllib 读、tomlkit 写回 | pydantic 2.14，tomlkit 0.15 |
| 密钥 | keyring（Windows 凭据管理器） | 25.7.0 |
| 缓存与统计 | sqlite3，预写日志（Write-Ahead Logging，WAL）模式 | 标准库 |
| 截屏 | `QScreen.grabWindow(0)`，每块屏幕单独截取 | Qt 内置 |
| 全局热键 | `RegisterHotKey` 加 `QAbstractNativeEventFilter` | Win32 API |
| 测试 | pytest、pytest-asyncio、hypothesis；界面测试加 pytest-qt | pytest 9.1 |
| 静态检查 | basedpyright（strict）、ruff、import-linter | basedpyright 1.40，import-linter 2.15 |

### 2.2 界面：全部使用 Widgets

界面有 Widgets 和 Qt Quick（QML）两条路线。本项目全部使用 Widgets，理由按重要性排列：

1. 托盘只能用 Widgets。QML 只有实验性的 `Qt.labs.platform`，在 Windows 上也会回退到 Widgets 实现。
2. 截屏蒙版和覆盖窗口要做到物理像素级控制。两者都是自绘的 `QWidget`：蒙版把冻结画面的设备像素比设成屏幕的设备像素比，按 1:1 画到物理像素上；覆盖窗口在 `paintEvent` 里把画笔换到设备像素坐标后绘制。坐标换算全部由不依赖 Qt 的纯函数完成（11.4、11.5 节）。
3. 本机实测，最小窗口首帧 Widgets 为 0.59–0.76 秒，QML 为 0.96–1.75 秒；私有内存 Widgets 为 25 MB，QML 约为 122 MB。
4. 两条路线混用时，要同时加载两套界面栈，样式也要分别对齐两遍。

Qt 自 6.7 起提供 `windows11` 样式，它只覆盖基础控件。左侧导航、Mica 背景这类 Fluent 外观要另外实现。

现成的组件库是 QFluentWidgets，它提供导航栏、设置卡片等控件，但许可证是 GPLv3（另有付费商业授权）。界面直接调用它的控件，本程序向他人分发时就必须以 GPLv3 公开全部源码。本程序不引入它，Fluent 外观由 `ui.theme` 自己实现，分发方式因此不受约束：

- 基础控件用 Qt 的 `windows11` 样式；
- 颜色、圆角、间距、字体集中定义在 `ui.theme.TOKENS`，强调色跟随系统；
- Mica 背景由 `enable_mica` 调用 DWM 设置，失败时回到不透明背景；
- 图标用 Windows 11 自带的 Segoe Fluent Icons 字体（`C:\Windows\Fonts\SegoeIcons.ttf`），按码位绘制，不随程序分发图标文件；
- 设置窗口的左侧导航和设置卡片由 `ui.settings` 用普通 Widgets 组合实现（11.7 节）。

代价是导航栏、设置卡片这类控件要自己写；双语窗口和首次运行引导已经按这套做法实现。

Mica 背景通过 `DwmSetWindowAttribute` 设置 `DWMWA_SYSTEMBACKDROP_TYPE`（属性值 38），要求 Windows 11 build 22621 及以上，这就是需求 6.6 节最低系统版本的来源。Widgets 窗口还要同时设置 `WA_TranslucentBackground` 和 `DwmExtendFrameIntoClientArea(-1)`，缺少任何一项窗口都会渲染成黑色。

深浅色跟随系统用 `QStyleHints.colorScheme` 及其变化信号，强调色从 `QPalette.Accent` 读取。

### 2.3 asyncio 整合与 Python 版本

PySide6 自带的 QtAsyncio 在 6.12 中仍是技术预览。它的事件循环没有实现 `getaddrinfo`、`create_connection` 和管道相关方法，本机用它运行 httpx 请求时直接报错 `getaddrinfo() is not implemented yet`。qasync 在 Windows 上继承自 `ProactorEventLoop`，套接字、子进程、管道都能用，所以选 qasync。

本机用 qasync 0.28.0、PySide6 6.12.0、Python 3.13.15 验证过以下几点：

- 事件循环类型是 `QIOCPEventLoop`；
- `getaddrinfo`、本地 TCP 连接、httpx 请求、`asyncio.to_thread` 都正常；
- 从其他线程向事件总线发布的消息，全部在主线程上被处理；
- `asyncio.sleep(1/60)` 实测 15.7 至 17.2 毫秒，平均 16.5 毫秒；`call_soon` 调度一次 0.2 毫秒。

qasync 的 `run_until_complete` 有一个行为必须避开：Qt 事件循环退出、已经注销“正在运行的循环”之后，它还会调用一次 `processEvents` 处理剩余事件。这时排队的任务照样执行，任务里调用 `asyncio.create_task` 会因为没有正在运行的循环而抛 `RuntimeError`。所以程序入口只调用一次 `run_until_complete`，启动、等待退出、停止都在同一个协程里完成（`lst/__main__.py` 的 `run()`）。这个问题是在安装版上实测发现的：入口最初分三次调用，`start()` 刚结束时首次运行引导发出的“获取模型索引”命令正好落在空档里，模型列表取不到。测试用的是普通 asyncio 事件循环，没有暴露它。

主进程环境的 Python 版本由两个依赖夹定：

- qasync 0.28.0 声明支持到 Python 3.13；
- lingua 2.2.0 要求 Python 3.12 及以上。

所以主进程用 Python 3.13。OCR 进程的独立环境也用 3.13，因为 PaddlePaddle 的 Windows wheel 覆盖 3.9 至 3.13。两个环境用同一个解释器版本，开发者只需要安装一个 Python。

HTTP 客户端选 httpx 0.28.1，这是 OpenAI 官方 Python SDK 依赖的版本。原作者参与的分支 httpx2 与它 API 相同，以后需要迁移时只改 import。

### 2.4 OCR：PP-StructureV3 与文本模式

需求要求还原完整版面结构（`OCR-01`、`OCR-02`）。在本地方案里，PaddleOCR 的 PP-StructureV3 是唯一开箱同时提供以下全部能力的产线：

- 单元格级表格：HTML 带 rowspan/colspan，外加单元格框；
- 公式：输出 LaTeX；
- 图表：转成数据表；
- 版面：20 类版面块，并给出阅读顺序。

它的许可证是 Apache-2.0。其他候选的情况如下：

| 方案 | 不选的原因 |
|---|---|
| Docling | 表格单元格结构最贴合需求，但本地模型依赖 PyTorch，速度没有公开数据。列为第二候选 |
| MinerU 4.0 | 许可证有附加条款；4.0 是大重构，发布才一个月 |
| Surya / Marker | 模型权重有商用限制；Surya 2 去掉了跨行跨列信息 |
| docTR | 没有公式和图表识别 |
| Windows 自带 OCR | 只有行和词级别的框，没有版面信息 |

PaddleOCR 与 RapidOCR 的 OCR 进程使用与主进程分开的 Python 环境：源码运行时是独立的 venv `.venv-ocr`，环境里只装 PaddlePaddle、PaddleOCR、RapidOCR 及其依赖，不装 PyTorch；安装版是模型目录下各引擎自己的运行环境（8.6 节），按 `.venv-ocr` 冻结的版本清单安装。Windows 自带 OCR 只依赖 pywinrt，OCR 进程直接用主环境。旧版本遇到的 Paddle 与 PyTorch DLL 冲突（`shm.dll` WinError 127）只在两者同进程加载时出现，这个环境里没有 PyTorch，冲突的前提就不成立。

识别模型的选择：

- 默认用 `PP-OCRv5_mobile_rec`，文本检测用 `PP-OCRv5_mobile_det`，版面检测用 `PP-DocLayout_plus-L`。识别语言为“自动”、简体中文、繁体中文、英文、日文时都用这个通用识别模型。
- 韩文、拉丁、西里尔、阿拉伯、天城等文字系统映射到 `<文字系统>_PP-OCRv5_mobile_rec`。这些多语言模型目前不在模型缓存里，选中后 OCR 进程以配置错误退出。

最初的方案是默认用 PP-OCRv6，文本模式走 RapidOCR。实现时改了两处：RapidOCR 首次使用要联网下载模型，本机模型缓存里也没有 PP-OCRv6。文本模式因此改用 PaddleOCR 自带的检测加识别产线，与版面模式共用一套模型。RapidOCR 后来作为独立的轻量引擎接入（8.4、8.6 节）：rapidocr 3.10.0 的 wheel 自带 PP-OCRv6 small 的检测与识别模型和一个文本行方向分类模型，装好运行环境就能识别，OCR 进程总是显式给出本地模型路径，不经过 RapidOCR 的下载逻辑。

PaddlePaddle 3.3.1 的 CPU 版有一个 oneDNN 缺陷：开 oneDNN 时，含插值算子的模型报 `ConvertPirAttribute2RuntimeAttribute not support`。本机逐个实测，文本检测、版面检测、表格结构、单元格检测、去畸变模型都会失败，三个文本识别模型和三个 PP-LCNet 分类模型正常。全部关闭 oneDNN 时 1920×1080 的文本模式要 9–10 秒，所以 OCR 进程按模型单独配置运行方式，只给能用的模型开 oneDNN（`lst/workers/ocr_worker/paddle_config.py`），文本模式降到约 2.7 秒。

本机实测（Intel Core Ultra 7 270K Plus，CPU，进程优先级最低，同时有其他测试在运行）：

| 场景 | 耗时 |
|---|---|
| 文本模式 1920×1080 | 中位数 2.73 秒，满足需求 6.1 节“CPU ≤ 4 秒” |
| 版面模式只开表格 1920×1080 | 中位数 8.18 秒 |
| 版面模式加公式 | 每页 12–14 秒 |
| 图表识别（PP-Chart2Table） | 每图 560–600 秒，输出含乱码 |
| 版面模式从启动到就绪 | 17–19 秒（导入 Paddle 8–9 秒，创建产线 4 秒，预热 4.7 秒） |
| 文本模式从启动到就绪 | 10.5–11.6 秒 |

图表识别在 CPU 上既慢又不可用，所以设备是 CPU 时 OCR 进程不提供图表识别：能力清单里标为不可用，请求里开着也忽略，只记一次 WARNING。

### 2.5 本地 NMT：CTranslate2 直接加载 Argos 模型

Argos 的模型包里是一个 CTranslate2 模型目录加一个 SentencePiece 分词模型，argostranslate 本身也是用 `ctranslate2.Translator` 加载它。本项目直接用 CTranslate2 加载这些模型包，不安装 argostranslate，理由有三点：

1. 依赖：argostranslate 1.11 硬依赖 stanza（会带进 PyTorch）、spacy 和 AGPL-3.0 许可的 minisbd。直接用 CTranslate2 后，这条依赖链全部去掉。
2. 速度：本机实测（模型已加载，含进程间通信），CPU int8 下约 200 字中文译成英文用 149–192 毫秒，约 200 词英文译成中文用 235–267 毫秒；RTX 5080 float16 下分别为 89 毫秒和 111–138 毫秒。只用 CPU 就满足双语窗口 500 毫秒的目标。
3. 覆盖：Argos 的 100 个模型里有 98 个包含英语。非英语语言对由 NMT 进程按“源语言→英语→目标语言”两跳翻译（`ENG-03`）。

中转要翻译两遍，按上面的实测，200 字的中转翻译约 0.4 至 0.5 秒，处在 500 毫秒目标的边缘。所以需求 6.1 节的双语窗口指标只针对有直连模型的语言对，中转语言对的第一版译文由竞速中其他更快的实例提供，或者接受略超 0.5 秒的延迟。

句子切分用 NMT 进程内的规则切分器，按句末标点切分，因为 Argos 模型按句子训练，整段输入会降低译文质量。

GPU 方面，CTranslate2 的 Windows wheel 用 CUDA 12.8 构建，运行时只需要 cuBLAS 的两个 DLL（`cublas64_12.dll`、`cublasLt64_12.dll`）。CTranslate2 在 Windows 上只把自己的包目录加入 DLL 搜索路径，所以 NMT 进程自己把 site-packages 下的 `nvidia/*/bin` 加进去，用 pip 安装 `nvidia-cublas-cu12` 即可启用 GPU（这条路径还没有用真实的 pip 包验证）。

几条 GPU 相关的实测结论：

- 本机 RTX 5080（计算能力 12.0）上，CTranslate2 4.8.3 报告支持 int8、int8_float16、float16、bfloat16，有 cuBLAS 时 int8 也能正常翻译；计算类型为 auto 时在 GPU 上取 float16。
- 缺少 cuBLAS 时，float16 第一次翻译时报 DLL 找不到，int8 则卡住超过 400 秒不返回。所以 NMT 进程选择设备前先预加载这两个 DLL：设备为 auto 且加载失败时回退到 CPU；显式指定 cuda 却加载失败时，以退出码 65 退出。
- 第一次在 CUDA 上翻译要即时编译内核，本机约 6.8 秒，超过监管器“每批 5 秒”的处理时限。所以设备是 cuda 时，NMT 进程在加载阶段（`ready` 之前）先翻译一句短句完成编译；监管器等待 `ready` 的上限是 300 秒，足够容纳。编译结果缓存在模型目录的 `nmt/.cuda-cache` 下，命中缓存后首次调用约 0.13 秒。

CTranslate2 的调用有几处细节，都用本机的两个 Argos 模型验证过：

- 目标语言标记（例如英→中模型的 `>>cmn_Hans<<`）由 CTranslate2 按模型配置自动添加，代码里手动再加一次会让译文变差。
- 解码不能用包里的 SentencePiece 模型：它只覆盖源语言，目标词表是 65000 个词元的共享词表。做法是把输出词元直接拼接，再把 `▁` 换成空格。
- 两个模型的词表里各有 512 个私用区字符词元，不加限制时短句会译出乱码（例如 "OK" 译成私用区字符加“罢”）。解码时用 `suppress_sequences` 禁止这些词元和全部语言标记。
- 英→中模型输出的中文使用英文标点，句号前还会留空格；输出后把紧跟汉字的英文标点换成中文标点，并去掉汉字之间多余的空格。
- `max_decoding_length` 设为“本批最长源句的词元数 × 3 + 20”，防止模型在个别输入上无限重复。

NMT 进程使用主进程的 venv，因为 CTranslate2 与 PySide6 之间没有依赖冲突。它单独成为一个进程，是为了崩溃隔离（`REL-01`）。

两个本地模型作为可选项：

- NLLB-200 distilled 600M 支持 200 种语言任意互译，用来覆盖 Argos 没有的语言。它的许可证是 CC-BY-NC 4.0，禁止商用，设置界面上要标明。
- 腾讯的 Hy-MT2-1.8B（Apache-2.0）是翻译专用的小模型，推荐在“本地 LLM”类引擎里通过 Ollama 或 llama.cpp 使用。它是生成式模型，纯 CPU 下出一整段译文需要数秒，所以在竞速里承担“质量升级”的角色。

### 2.6 语言检测：lingua 加文字系统规则

`RACE-09` 和 `RACE-10` 要对每个段检测语言，段的长度常常只有十几个字符。在 lingua 作者公布的测试集上，lingua 高精度模式的单词级准确率为 74%，词对级为 89%，在所列各库中最高；中文和日文的单词级准确率都是 100%。

检测分三步：

1. 先按文字系统判断：出现假名判为日文，出现谚文判为韩文。
2. 其余文本交给 lingua，候选语言限定为已安装模型和已启用引擎覆盖的语言，既提高准确率，也加快速度。
3. lingua 不支持的语言回退到 fast-langdetect。

检测在主进程中通过 `asyncio.to_thread` 执行。本机实测（13 种候选语言）：构建检测器不到 1 毫秒，首次检测有约 5 毫秒的一次性初始化，之后中文短句约 0.01 毫秒、英文短句约 0.07 毫秒，100 段一批（含线程切换）约 14 毫秒。初始化在后台线程里预热，不落在事件循环线程上。

实现与上面的设计有两处差距，列为后续工作：

- 候选语言目前是固定的 13 种（中、英、日、韩、法、德、西、俄、葡、意、阿、越、泰），还没有按已安装模型和已启用引擎动态限定，因为引擎协议只有 `supports(source, target)`，无法枚举一个实例支持哪些语言。
- fast-langdetect 回退没有接入。候选集合之外的文字（例如希腊文、希伯来文）检测不出，按“检测不出”处理。

没有可翻译文字的段不送去翻译，标为“无需翻译”（`RACE-09`）。判据是 `langid.has_text`：段里没有字母（“•”“☆4”、页码、日期、金额），或者只有一个字母且它不是汉字、假名、谚文（“v1.2”里的 v）。这类段以前被当成“检测不出”，按“源语言与目标语言相同”跳过，悬停提示因此写着“与目标语言相同”，与事实不符。

单个拉丁字母单词的检测不可靠。实测 lingua 把 GitHub 页面上的 Star 判成葡萄牙语、Code 判成德语、Actions 判成法语、Wiki 判成越南语，最高置信度都不到 0.55。判错的源语言常常没有对应的翻译实例，这些段就被标成“没有支持这个语言对的引擎”，显示原文。

同一张截屏里的界面文字通常属于同一种语言，所以一批文本（同一张截屏的各段，或双语窗口的各段落）检测完之后，再按上下文修正拉丁字母文本的结果（`langid.resolve_with_context`）。设一段文本的字母数为 $n$，lingua 给出的最高置信度为 $c$，规则如下：

1. 文字不全是拉丁字母的段（中文、日文、韩文、俄文等）保留自己的结果。这些文字系统各自只对应候选集合里的一种语言，单词也能判对。
2. 拉丁字母段在 $n \ge 20$ 且 $c \ge 0.5$ 时算可信，保留自己的结果。整段真正的外语长文本由此按自己的语言翻译，例如英文页面上的一段德语。
3. 其余拉丁字母段（$n < 20$ 或 $c < 0.5$）取这一批里可信的拉丁字母段检测结果的众数，票数相同时取先出现的语言。
4. 这一批里没有可信的拉丁字母段时，第 3 条里的段取英语。

投票只在拉丁字母段之间进行。中文页面上的一个英文单词不会因为周围都是中文而被当成中文；它所在的批次没有可信的拉丁字母长文本，按第 4 条取英语。阈值 20 个字母大约是四五个英文单词，实测这个长度以上 lingua 对英、法、德、西、葡、意、越的判断稳定；0.5 的置信度下限挡住了长度够但混杂多种语言的文本。

这条规则的代价是：英文页面上一句独立的德语短句（实测“Wo ist der Bahnhof?”置信度 0.77）会被当成英语。截屏翻译以界面文字为主，这种情况远少于界面短词被误判；双语窗口里用户可以手动指定源语言。

调度器按字符串比较语言代码，`zh-Hans` 和 `zh` 会被当成两种语言，`RACE-09` 的“源语言与目标语言相同”也就判断不出来。所以语言代码在两个入口处统一规范化为同一套 BCP 47 写法（中文区分 `zh-Hans` 和 `zh-Hant`，其他语言只用主语言子标签，例如 `en`、`ja`）：

- 检测结果在检测模块输出时规范化；
- 配置里的目标语言和互译对由配置服务在校验时规范化。

各引擎再把规范写法映射成自己接口的语言码，例如百度的 `zh`、`cht`、`jp`。

### 2.7 翻译接口

#### 2.7.1 LLM 引擎

在线 LLM 和本地 LLM 共用一个 OpenAI 兼容接口的实现。要求模型输出的格式固定为：

```json
{"r": [{"i": 1, "t": "第 1 段译文"}, {"i": 2, "t": "第 2 段译文"}]}
```

各服务对输出格式的约束能力不同：

- OpenAI、Ollama、llama.cpp 支持 `json_schema`，服务端按 schema 做约束解码。
- DeepSeek 只支持 `json_object`，只保证输出是合法 JSON，所以提示词里要给出同样格式的示例。

流式返回时，引擎增量扫描 JSON 结构，只从上次停下的位置继续：一项的括号闭合时，单独用 jiter 严格解析这一项；根对象闭合时再解析一次全文。总耗时与输出长度成正比。最初的做法是每收到一块数据就用 jiter 的 partial 模式重新解析整个缓冲区，输出越长越慢，复杂度是平方级。判断数组中的一项已经完整的依据是：它的括号已经闭合，或者数组已经闭合。只看 `t` 键是否出现不够可靠，因为 `json_object` 模式不保证键的顺序，`t` 可能出现在 `i` 之前，此时段号可能还没收全。

流结束后，引擎核对收到的段号集合。缺失的段、以及整个输出无法解析时的全部段，退回逐段请求（`ENG-04`）。原文非空而译文去空白后为空，按缺段处理：多段时逐段退回，单段时以 `engine.bad_response` 失败；调度器也不把空译文写进缓存。空译文曾作为成功结果胜出并写进缓存，之后这一段一直显示为空。

LLM 请求只有两条消息：一条 system 消息是系统提示；一条 user 消息先放会话全文，再用固定的分隔标记接上本批段落和语言对。请求开头不放时间戳和批号，同一会话后续批次的请求开头（系统提示加会话全文）完全相同，能命中 DeepSeek 的上下文缓存。会话全文和本批段落合在一条 user 消息里，是为了兼容要求 user 与 assistant 严格交替的对话模板（llama.cpp 用 `--jinja` 加载 Gemma 等模型时就是这样）；缓存按 token 前缀匹配，合并后不影响命中。按 deepseek-flash 的峰时价格（每百万 token：命中缓存的输入 0.04 元，未命中的输入 2 元，输出 8 元）估算，上下文 20k token、本批 1k token、输出 1.5k token 时：

$$
C_{\text{未命中}} = \frac{21000 \times 2 + 1500 \times 8}{10^6} = 0.054\ \text{元}
$$

$$
C_{\text{命中}} = \frac{20000 \times 0.04 + 1000 \times 2 + 1500 \times 8}{10^6} = 0.0148\ \text{元}
$$

命中后费用降到未命中时的 27%。

DeepSeek 的模型名为 `deepseek-flash` 和 `deepseek-v4-pro`，`deepseek-chat` 已于 2026-07-24 停用。两个模型默认开启思考模式，翻译请求必须关闭它，否则首个 token 会被推迟。

流式请求的计时有三层：

- 首 token 时限：从请求发出到收到第一个非空内容为止。
- 空闲时限：之后两块数据之间不超过 10 秒，由引擎自己的计时器保证。
- httpx 读超时：取 10 秒与首 token 时限中的较大值，因为 httpx 的读超时也约束等待响应头的时间。本地服务加载模型时可能长时间一个字节都不发，读超时取 10 秒会让 60 秒的首 token 时限失效。

本地服务首 token 超时后，下一次请求恢复 60 秒的时限，因为 Ollama 默认空闲 5 分钟就卸载模型，之后的请求要重新加载。

逐段退回时，一旦遇到服务级错误（例如 401、5xx、超时），剩下的段直接记为同一错误，不再发请求；只有请求被拒绝或返回内容无法解析的错误，才只让当前这一段失败。

#### 2.7.2 传统在线翻译接口与免费接口

| 接口 | 实现要点 |
|---|---|
| DeepL | 认证只用请求头 `Authorization: DeepL-Auth-Key`。官方已取消单请求 50 条 text 的限制，只剩请求体 128 KiB 的上限；默认仍按 50 条打包，控制单次请求的延迟。会话原文放进 `context` 参数，这部分不计费，超出请求体上限时截取最长的可容纳前缀。正式程度用 `prefer_more`、`prefer_less`，对不支持正式程度的目标语言传 `more`、`less` 会让整个请求失败。返回 456 表示配额耗尽，该实例进入“出错”状态，直到用户处理 |
| Google Cloud Translation | 用 v2 接口，API key 放在请求头 `X-Goog-Api-Key` 里，不放在 URL 中，避免出现在日志和异常信息里（`CFG-04`）。单请求最多 128 条；官方建议 v2 单次不超过 5000 码位，所以默认按 5000 字符打包。Google 用 403 同时表示无权限和限流，按错误响应里的 reason 区分：`rateLimitExceeded` 等记为限流并重试，`dailyLimitExceeded` 记为配额耗尽，其余记为认证错误 |
| 百度翻译开放平台 | 用 POST 提交，原文不进入 URL（`PRV-02`）。签名为 `MD5(appid+q+salt+密钥)`。多段用 `\n` 拼接，段内换行先替换为空格，否则返回的段数对不上。单次请求不超过 6000 字节（UTF-8），超出时按字节拆成多次请求顺序发送。百度在 HTTP 200 里用 `error_code` 报错：超时 52001、系统错误 52002 等 1 秒重试，访问频率受限 54003 等 1 秒、长请求过于频繁 54005 等 3 秒重试，重试次数与 HTTP 层共用。用令牌桶按所在版本的每秒请求数（Queries Per Second，QPS）限速，重试也要先领令牌 |
| 谷歌免费接口（`google_free`） | `translate.googleapis.com` 的 `client=gtx` 接口，不需要 API key。批量请求是 `POST /translate_a/t?client=gtx&sl=<源>&tl=<目标>&format=text`，表单请求体里每段一个 `q` 参数，一个任务只发一次请求。响应有三种形状：给出 `sl` 时是译文字符串的数组；`sl=auto` 时是 `[译文, 检测出的语言]` 的数组；只有一个 `q` 时有时直接是字符串。响应是合法 JSON、但形状或条数对不上时，改为逐段请求 `/translate_a/single`（`dt=t`），把各分句的译文拼成整段，最多 4 个请求同时进行，一段失败不影响其他段。响应不是 JSON（例如被拦截时返回的 HTML 页面）时整批以 `engine.bad_response` 失败，不再逐段请求，免得对一个拒绝服务的地址再发几十次请求。源语言不在语言表里时传 `auto` |
| 微软免费接口（`microsoft_free`） | Edge 浏览器使用的 `edge.microsoft.com/translate/translatetext`，不需要 API key。请求是 `POST <地址>?from=<源>&to=<目标>&isEnterpriseClient=false`，JSON 请求体是段落字符串的数组，响应与请求一一对应，取每项的 `translations[0].text`。这条路由会对文本做 HTML 标签对齐，原文里单独的 `<` 会被当成标签，所以发送前把 `&`、`<`、`>` 转义成 HTML 实体，收到译文后再解码。旧的免费路由要先从 `/translate/auth` 取 token，参考项目 thunderbird-translate 的文档写明微软在 2026 年 7 月停用了这个接口，所以这里只用直连路由，不取 token |

两个免费接口的共同点：

- 都没有公开文档，随时可能变动或限流；谷歌翻译在中国大陆无法访问。设置里接口地址参数的说明写明这几点，地址可以改成转发同一接口的镜像；
- 单次请求最多 100 段、5000 个字符，与参考项目的设置一致，也是 `max_batch_segments`、`max_batch_chars` 的上限；
- 查询串里只有语言码，原文只出现在请求体里（`PRV-02`）；地址不允许带查询参数、片段或用户名密码，因为地址会出现在 httpx 的日志里；
- 接口没有 key，401、403 表示请求被拒绝，不是 key 无效。默认的错误分类会把它们记为 `engine.auth`，界面建议“检查 API key”，对免费接口没有意义，所以改记为 `engine.bad_request`，不重试；429 与 5xx 仍按默认分类重试；
- 计费标记为假，双语窗口的服务列表里不带“计费”标注（`BI-04`）；
- 默认列在引擎列表里但不启用（第 9 章），用户勾选后才联网（`PRV-01`）。

#### 2.7.3 超时与重试

| 引擎类别 | 超时 | 重试 | 默认并发 |
|---|---|---|---|
| 本地 NMT | 单批处理 5 秒，从 NMT 进程开始处理时计时；任务总时限 10 秒 | 不重试 | 1 |
| 在线 LLM | 连接 3 秒；首 token 8 秒；两次数据之间空闲 10 秒；总计 60 秒 | 429、5xx、断流最多重试 1 次 | 2 |
| 本地 LLM | 首次请求首 token 60 秒（含模型加载），之后 15 秒；总计 60 秒 | 503 最多重试 1 次 | 取服务端并行数，Ollama 默认 1 |
| DeepL、Google、百度 | 总计 8 秒 | 限流、5xx、网络错误最多重试 1 次 | 2；百度按 QPS 限速 |
| 谷歌、微软免费接口 | 连接 3 秒；总计 8 秒 | 限流、5xx、网络错误最多重试 1 次；401、403 不重试 | 2；谷歌逐段回退时同时 4 个请求 |
| 字典翻译 | 总计 5 秒 | 不重试 | 2 |

重试次数是每个实例的配置项 `max_retries`（`ENG-05`），取值 0 至 3，默认 1。网络类引擎默认最多重试 1 次。竞速中其他引擎本身就是冗余，一个引擎反复重试只会拖长它处于“仍可能改变显示”状态的时间，推迟竞速结束（6.3 节）。重试前的等待按指数退避加随机抖动计算，响应带 `Retry-After` 时以它为准（`ENG-05`），等待时间计入任务的总时限，不计入首 token 时限：首 token 时限从每次发送开始算，只包住单次发送。最初的实现让退避等待处在上一次请求设下的首 token 时限里，较慢的 5xx 或 429 之后重试根本没发出，还被误报成超时。

LLM 引擎的整批阶段，HTTP 层重试与断流重发共用一份额度，合计最多 `max_retries + 1` 个请求；逐段退回时每段各有同样的额度。一个任务的请求数上限是 $(\text{max\_retries} + 1)(1 + m)$，$m$ 是整批阶段之后仍缺的段数。默认配置下整批最多 2 个请求，每个退回段最多再 2 个。

本地 NMT 不重试，它的失败只有两种来源：模型缺失这类确定性错误，重试结果相同；进程崩溃，由监管器处理（8.2 节）。本地 NMT 的配置里 `max_retries` 固定为 0。

本地 NMT 有两个时限，计时范围不同：

- 单批处理时限（默认 5 秒）交给监管器，从 NMT 进程发出 `started` 时计时，超时说明进程卡死，监管器结束并重启它。
- 任务总时限（默认 10 秒）交给调度器，从任务开始执行算起，包括在 NMT 进程里排队的时间。NMT 进程只有一个推理线程，被取消的批次仍会跑完，新请求最坏要先等它跑完，再算自己的处理时间，所以总时限取处理时限的两倍。

本地 NMT 引擎的 `supports` 依据 NMT 进程最近一次 `ready` 消息里的能力清单判断。NMT 进程首次就绪之前（程序启动后的一两秒内），本地 NMT 不参加竞速；首次就绪之后，即使进程崩溃重启，能力清单仍然保留，请求在监管器中排队等待（8.2 节第 6 条）。能力清单里的模型指纹参与缓存键，`services.engines` 在每次收到指纹不同的 `ready` 时重建本地 NMT 实例，并发布新的 `EngineRoster`，模型更新后旧译文因此不会被误用。

#### 2.7.4 字典翻译

字典翻译引擎（`lst/services/engines/dictionary`，`ENG-08`）用已安装的词典包离线逐词查词，在竞速里是最快的一档：本机实测 1000 字的中英文都在几毫秒内译完。译文只是逐词替换的结果，不处理语序、冠词和一词多义，所以默认优先级排在最后，它先出结果，优先级更高的译文到达后替换它。引擎在主进程里运行，不经过推理进程：查词是纯 Python 的字典查找，不需要原生推理库，也就没有需要隔离的崩溃风险。解析词典包与逐段翻译放在线程里执行（`asyncio.to_thread`），不占用事件循环。

词典包来自 GitHub 仓库 `willlllliams/thunderbird-translate` 的发布标签 `dicts-v1`：22 个 `dict-<src>-<tgt>.tsv.gz`（英语与 11 种语言之间的双向互译），外加列出全部包的 `dict-index.json`。格式与构建规则以那个仓库的 `docs/dictionary-packs.md` 为准。一个包对应一个翻译方向，内容是 gzip 压缩的 UTF-8 文本，字段之间用制表符分隔：

- 第 1 行是包头：`#dict`、源语言、目标语言、包版本（构建日期）、数据源名称、许可证；
- 词条行 `w <key> <译文>`；
- 变形行 `f <key> <原形 key>`，例如 `f went go`。查词时词条优先，没有词条时经变形行找到原形，再取原形的译文，变形不做链式解析。

key 的归一化与构建脚本相同：Unicode NFC，去掉首尾空白并合并内部连续空白，俄语去掉重音符号（U+0301、U+0300），按源语言转小写（土耳其语与阿塞拜疆语把 `I` 转成 `ı`、`İ` 转成 `i`）。

一段原文先切成三类记号：词（字母、数字和组合附加符号连成的串，中间可以夹一个撇号，例如 `don't`）、空白和其他字符（每个标点、符号单独一个记号）。之后按源语言是否用空格分词分两种处理：

1. 用空格分词的语言（英语、法语、德语、俄语、韩语等）以词为单位查词。在每个词处，从当前词起向后拼接至多 3 个词（相邻两词之间只能隔着不含换行的空白），从最长的试起，查到就整体替换；都查不到时查单个词，单个词也查不到时原样保留。数字不查词典。
2. 中文和日文以字为单位做正向最长匹配，词表就是这个词典包的全部 key。在位置 $i$，从“包内以这个字开头的最长 key 的字数”与剩余字数中较小的一个开始，逐次缩短一个字，取第一个查得到的片段作为一个词，跳到片段之后继续；一个字也查不到时这个字原样保留。最长尝试字数另有 16 字的上限，切分耗时因此有上界。夹在中文里的拉丁字母和数字串作为整体，不会被切开，但可以与后面的汉字连成一个 key（例如 `T恤`）。

输出保留原文的空白与标点，有两处例外，让译文符合目标语言的书写习惯：

- 目标语言是中文或日文时，两侧都是中日文字符的行内空白去掉（换行保留），`The cat` 译作 `那猫`；
- 源语言是中文或日文、目标语言用空格分词时，两个相邻的词之间至少有一个查到了译文，就补一个空格，`我爱你` 译作 `I love you`。

一段的结果分三种：

| 情况 | 结果 |
|---|---|
| 空白段，或只有数字、标点、没有任何词的段 | 原样作为译文返回 |
| 有词，至少查到一个 | 逐词替换后的译文 |
| 有词，却一个都查不到；或替换后译文为空 | 以 `engine.no_match` 失败 |

查不到时失败而不返回原文，是为了不把原文冒充成译文：作为成功结果返回时，没有其他引擎成功的段会以字典实例为胜出者，界面把原文当作译文显示并标出来源；作为失败返回时，其他引擎的译文照常胜出，都失败时这一段按 `RACE-04` 显示原文和各实例的失败原因。

可选的“经英语中转”：两端都不是英语、并且装有“源语言→英语”和“英语→目标语言”两个包时，依次使用两个包。两次逐词替换的误差叠加，译文更差，所以默认关闭。简体与繁体中文之间不中转，因为经英语只会得到按中文语序排列的英语释义。

词典目录是模型目录下的 `dict`，由模型管理模块安装（8.5.2 节），字典引擎自己发现目录的变化，不订阅总线消息：

- `installed_pairs` 每次先取词典目录的修改时间，变了才重新列目录。新建、改名（模型管理模块用 `os.replace` 原子安装）或删除文件都会更新目录的修改时间，装好或删掉一个包之后，下一次 `supports` 就能看到。取一次目录属性只要几十微秒，可以在事件循环里直接调用；
- 读入内存的包按文件的（修改时间，大小）记下状态，每次使用前核对，包被更新后重新读取，被删除后从缓存里去掉；
- 内存里最多保留 4 个包，超出时丢掉最久没用的。一个全量的包读入后占几十 MB 内存；
- 一个词典目录只对应一个共享的包缓存，配置变化导致引擎实例重建时不必重新读包。

所以只增删词典包时，组合根不重载 NMT 进程：`ModelsInstalled` 里只有词典包时直接忽略（8.5 节）。

已知限制：字典实例的配置指纹由引擎类别、算法版本号和“经英语中转”开关组成，不含词典包的版本。词典包更新后，已经写进译文缓存的旧译文仍会命中，直到缓存条目被淘汰或用户清空缓存。

### 2.8 推理进程通信

本机实测的通信开销如下：

- 命名管道一次往返 35 微秒；
- 通过命名管道发送 8 MB 数据并收到确认，共 4.85 毫秒；
- 共享内存方案：写入 8 MB 用 0.38 毫秒，子进程挂载并拷出用 3.96 毫秒。

两种方案传一张截图只差几毫秒，而且发生在后台读写线程里，所以第一版不使用共享内存，截图直接通过管道传输。

进程用 `subprocess` 启动，因为 OCR 进程要用另一个 venv 的解释器，而 `multiprocessing` 的子进程只能使用与父进程相同的解释器。通信使用标准库 `multiprocessing.connection` 的 `Listener` 和 `Client`：主进程创建命名管道，通过命令行参数把管道名交给子进程，一次性 authkey 通过标准输入传入（8.1 节）。

消息编码用 JSON，图像数据作为紧随其后的一个原始字节帧。主进程和 OCR 进程是两个 venv，pickle 对两边的类定义版本敏感，所以不用 pickle。

### 2.9 存储与日志

#### 2.9.1 配置

配置用 pydantic `BaseModel` 描述，文件格式为 TOML。读取用标准库 tomllib，写回用 tomlkit，用户写在配置文件里的注释原样保留。写入时先写临时文件，再用 `os.replace` 替换，写到一半断电也不会留下损坏的文件。

pydantic 没有内建“字段校验失败就回退默认值”的选项（`CFG-02`），做法如下：

1. 对原始数据整体校验一次。
2. 按错误信息中的字段路径处理出错的位置：普通字段从原始字典里删除该键；`[[engines]]` 数组中的条目整条删除，因为删掉条目里的必填键后，这一条仍然无法通过校验。
3. 再校验一次，被删除的字段落到默认值，被删除的引擎条目不再加载。
4. 每处回退记一条警告，并通过 `ConfigRejected` 事件报告给设置窗口。启动时发现的问题在全部模块启动后补发一次，晚于托盘启动的设置窗口也能收到。

“不再加载”只发生在内存里，配置文件里的这一条原样保留。界面修改引擎列表时，配置服务以磁盘上的表数组为底稿，按 `id` 逐条更新：校验不过的表、运行期间手工添加的表原样保留，连同注释；对应上的表只写有差异的字段；界面删除的条目删掉对应的表，新增的条目新建表，顺序跟随界面提交的列表。最初的实现用内存里剩下的条目整节替换，启动时被丢弃的条目在下一次修改引擎时从文件里永久消失。

#### 2.9.2 密钥

API key 用 keyring 存入 Windows 凭据管理器，单条上限 2560 字节，存放 API key 足够（`CFG-04`）。配置文件里只写条目名。

#### 2.9.3 缓存与统计

缓存和统计各用一个 SQLite 文件（`cache.sqlite3`、`stats.sqlite3`），都由主进程独占，开启 WAL 模式。分成两个文件，是因为缓存按设计要明文保存译文，而统计库要保证文件里没有任何文本（10.2 节）；放在同一个文件里，第二条就无法检查。

每个库由一个专用后台线程持有唯一的连接，读写都排队交给它执行，主线程 `await` 结果。sqlite3 连接不能跨线程共享，单条操作耗时在亚毫秒到毫秒级，串行执行没有实际代价；先进先出的顺序还让缓存可以先返回查询结果，再在后台刷新条目的最近使用序号。

缓存条目的主键是缓存键五个字段的 SHA-256。原文只以哈希形式存在于库中，译文明文保存。条目数超过容量上限时，按最近使用序号淘汰到上限的 90%。清空缓存时执行 `VACUUM` 重写文件，已删除的译文不会残留在空闲页里。

数据库文件损坏或版本号不符时，把它连同 `-wal`、`-shm` 文件改名为带 `.corrupt-时间戳` 后缀的文件保留，再在原路径新建一个库。改名失败时，本次运行改用内存数据库。缓存和统计都只是辅助功能，任何情况下都不能让程序起不来。

统计库的结构有版本号（`PRAGMA user_version`）。`_shared.sqlite.ensure_schema` 新建库时按当前结构建表；打开旧库时按迁移表逐级升级，每一级在一个事务里执行并写入新版本号，出错时这一级整体回滚，库按“文件不可用”处理。统计库从版本 1 升到版本 2 时，`segments` 表的 `winner`、`winner_executed` 两列改名为 `shown_engine`、`shown_executed`，与契约里的叫法一致，已有的胜出记录不丢。

统计库会随使用无限增长，实测每个尝试约 188 字节。保留策略（例如把 90 天前的明细折算成按实例累计的计数）放在 M7 里程碑实施。

#### 2.9.4 日志

每个进程写自己的按天滚动的日志文件。Windows 上 `TimedRotatingFileHandler` 滚动时要重命名文件，多个进程共用一个文件会报 WinError 32。

- 主进程用 `QueueHandler` 入队，由 `QueueListener` 线程落盘，主线程不做文件读写（`REL-04`）。
- 子进程启用 `faulthandler`，原生库崩溃时的堆栈写到标准错误，由主进程读取后记入日志，用于定位 `REL-01` 中的崩溃原因。

日志的脱敏规则见第 10 章。

#### 2.9.5 数据目录与模型目录

程序的文件分两处存放。数据目录存放配置、译文缓存、统计、日志和界面状态；模型目录存放本地翻译模型、OCR 模型与 OCR 引擎的运行环境、词典包和显卡运行库。两处都可以在设置的“常规”页更改（`CFG-06`、`CFG-07`），路径的计算都在 `lst/app/paths.py`。

数据目录的默认位置，安装版是 `%LOCALAPPDATA%\LSTtranslate`，源码版是程序目录下的 `data/`。源码版不放进用户目录，原因是：程序以源码加虚拟环境的方式运行时，所在的目录树可能带有 Low 强制完整性标签（某些沙箱工具会给工作区打上这个标签），这时虚拟环境里的 Python 以 Low 完整性运行，写不了用户目录，只能写所在的目录树。旧版本 PP-StructureV3 加载失败就是这个原因。`data/` 不纳入版本库。

模型目录默认是数据目录下的 `models`，配置的 `[storage].models_dir` 可以把它指到别处。两个目录里的内容如下：

| 目录 | 条目 | 内容 |
|---|---|---|
| 数据目录 | `config/settings.toml` | 配置文件（2.9.1 节） |
| 数据目录 | `cache.sqlite3`、`stats.sqlite3` | 译文缓存与统计（2.9.3 节） |
| 数据目录 | `logs/` | 各进程的日志（2.9.4 节） |
| 数据目录 | `ui_state.ini`、`updates.json` | 窗口位置与引导状态、应用更新的检查记录 |
| 数据目录 | `tmp/`、`lsttranslate.lock` | 临时文件、单实例锁 |
| 模型目录 | `nmt/` | 本地翻译模型包，以及 NMT 进程的解压目录 `.extracted` 与 CUDA 编译缓存（8.3 节） |
| 模型目录 | `ocr/` | PaddleOCR 的模型缓存与 RapidOCR 另下载的模型（8.6 节） |
| 模型目录 | `engines/` | RapidOCR、PaddleOCR 的运行环境（8.6.2 节） |
| 模型目录 | `dict/` | 词典包（8.5.2 节） |
| 模型目录 | `runtimes/cublas12/` | 本地翻译的显卡运行库（8.3.1 节） |

OCR 引擎的运行环境放在模型目录而不是数据目录，原因是它的体积与模型相当：PaddleOCR 与 RapidOCR 的 CUDA 版运行环境各约 3 GB（8.6.2 节的估计值）。用户把模型目录挪到空间大的盘，通常是想把这些大文件一起挪走。之前的版本把运行环境放在 `<数据目录>/engines`、显卡运行库放在 `<数据目录>/runtimes`，组合根启动时把它们改名到模型目录下（`storage.migrate_legacy_layout`），新位置已有内容时不动。默认的模型目录与数据目录在同一个卷上，改名瞬间完成，不需要界面；改名失败（例如手工把模型目录指到了别的盘）时只记警告，这时引擎显示为未安装。

##### 数据目录的来源与位置文件

启动时按以下顺序决定数据目录（`paths.choose_data_dir`），排在前面的优先：

1. 命令行参数 `--data-dir`；
2. 环境变量 `LST_DATA_DIR`；
3. 位置文件里记的目录，即用户在设置里选的目录；
4. 默认位置。

配置文件在数据目录里，选好的数据目录不能记在配置里，否则要先知道数据目录才能读到数据目录。位置文件因此放在数据目录之外：安装版是 `%APPDATA%\LSTtranslate\location.json`，源码版是 `data/location.json`，内容是 `{"data_dir": "<绝对路径>"}`，写入时先写 `.part` 再原子改名。

安装版选 `%APPDATA%`（漫游配置目录），而不放进默认数据目录 `%LOCALAPPDATA%\LSTtranslate`，依据有两条。第一，用户把数据挪走以后，旧的默认数据目录看上去是没用的残留，手工删掉它很正常；位置文件如果在里面，会一起被删，程序下次启动就回到空的默认目录。第二，卸载时选“保留用户数据”，位置文件与数据一起保留，重新安装后继续使用选好的目录；选“删除用户数据”时卸载脚本删除它（15.5 节）。源码版仍放在 `data/` 下，因为以 Low 完整性运行的源码版写不了 `%APPDATA%`；源码版与安装版的位置文件因此互不影响（`INS-08`）。

命令行参数与环境变量指定了数据目录时，`StorageLocations.data_source` 是 `argument` 或 `environment`，设置里“更改…”不可用，并写明原因：位置文件的优先级更低，写了也不会生效。位置文件指向的目录在启动时建不起来（例如所在的移动硬盘没有接上），本次改用默认位置，启动完成后发一条通知，位置文件保留，下次接上磁盘后照常使用。

##### 写入检查

更改数据目录或模型目录之前，`storage.probe_writable` 在新目录里实际创建一个文件、写入并 `fsync`、改名、删除。只看 ACL 或 `os.access` 判断不出 Low 完整性这种情况：ACL 上用户是“完全控制”，以 Low 完整性运行的进程照样写不进中完整性的目录，只有真的写一次才知道。只读介质、受控文件夹访问的拦截也一样。检查失败时界面显示系统给出的原因，例如“Permission denied（13）”；检查中新建的目录随之删除。

##### 更改数据目录

`ChangeDataDir` 由存储位置模块（`lst/app/storage_module.py`）处理，步骤如下：

1. 来源是命令行参数或环境变量时拒绝（`fixed`）。新目录不是绝对路径、在当前数据目录里面、写入检查不通过时同样拒绝。选的就是当前目录时，撤销还没生效的更改，把位置文件改回当前目录。
2. 要复制现有数据时，列出数据目录的顶层条目，跳过 `tmp/`、锁文件与源码版的位置文件；新目录里已有同名条目时拒绝（`occupied`），空间不够时拒绝（`no_space`）。
3. 在线程里复制，进度每 200 毫秒最多发布一次。缓存与统计库正被程序使用，处在 WAL 模式，最近的提交可能还只在 `-wal` 文件里；分别复制主文件和 `-wal` 文件拿到的不是同一时刻的快照，所以 `.sqlite3` 文件用 SQLite 的在线备份接口（`sqlite3.Connection.backup`）复制，`-wal`、`-shm` 不复制。复制失败时删除已经复制到新目录的条目，数据目录不变。
4. 写位置文件。新目录就是默认位置时删除位置文件，不留一份与默认值相同的记录。
5. `StorageLocations.pending_data_dir` 记下新目录，设置里显示“重启后改用 …”和“立即重启”。

更改在重启后生效，因为配置服务、缓存库、统计库、日志从启动起就打开着数据目录里的文件，运行中逐个换掉的代价比重启大得多。复制之后、重启之前写入旧目录的内容（例如关闭设置窗口时保存的窗口位置）不会出现在新目录里，所以界面在复制完成后直接提供“立即重启”。

“立即重启”发出 `RestartApp`。应用内更新的重新启动由安装程序完成（`/RELAUNCH=yes`，15.4 节），那条路径必须先装一遍安装包，这里用不上。组合根收到 `RestartApp` 后记下“退出后重新启动”并请求退出；入口在全部模块停止、单实例锁与运行互斥体释放之后，以本进程的解释器和原来的参数（`sys.executable` 加 `sys.orig_argv[1:]`）启动一个分离进程（`lst/app/restart.py`）。新进程启动时旧进程已经注销全局热键、关闭了单实例服务，单实例检查不会连上正在退出的旧进程。

模型目录的更改不需要重启，过程见 8.5.3 节。

##### 临时文件

`tempfile.gettempdir()` 在 Low 完整性的环境下会退回当前目录。需要临时文件的模块一律在数据目录的 `tmp/` 下创建。例外是下载：下载中的模型、词典包、显卡运行库与 OCR 引擎运行环境都写在各自的目标目录里（`<名>.part`、`site.part`），因为下载完成后要用 `os.replace` 原子改名，原子改名要求临时文件与目标在同一个卷上，而模型目录可以在别的盘上。

#### 2.9.6 单实例

Windows 上两个 `QLocalServer` 可以同时监听同一个名称，所以 `listen()` 成功不能证明没有别的实例。启动流程分三步（`TR-06`）：

1. 先用 `QLocalSocket` 连接；连上了就发送“显示通知”，然后退出。
2. 连不上时，用 `QLockFile` 加锁。
3. 加锁成功后，再开始 `listen`。

## 3 进程模型

程序运行时共有三个由本程序管理的进程，外加用户自己运行的本地 LLM 服务。

| 进程 | Python 环境 | 职责 | 崩溃后果 |
|---|---|---|---|
| 主进程 | `.venv` | 界面、托盘、热键、截屏、竞速调度、在线引擎的 HTTP 请求、配置、缓存、统计 | 程序退出 |
| OCR 进程 | 随当前 OCR 引擎而定：Windows 自带 OCR 用主环境；RapidOCR、PaddleOCR 在源码运行时用 `.venv-ocr`，安装版用模型目录下各自的运行环境 | 加载当前引擎的 OCR 与版面分析模型，执行识别 | 主进程重启它，当前识别任务失败并提示重试（`OCR-08`） |
| NMT 进程 | `.venv`（与主进程共用） | 用 CTranslate2 加载本地 NMT 模型，执行翻译 | 主进程重启它，本地 NMT 实例在当前会话中记为失败，其他实例继续竞速（`ENG-06`） |
| Ollama / llama.cpp server | 用户自行安装 | 本地 LLM 推理 | 由用户管理；主进程把它当作一个 HTTP 服务 |

OCR 和 NMT 分成两个进程，理由有三条：

1. 环境不同：PaddleOCR 与 RapidOCR 的 OCR 进程需要独立的运行环境（2.4 节），NMT 进程直接用主进程的环境。
2. 故障隔离：任何一个进程崩溃，只影响它负责的那一类任务。
3. 并行：用户连续截屏时，NMT 进程翻译上一张截图的同时，OCR 进程可以识别下一张。

代价是两个进程各自占用一份 Python 解释器和推理库的内存，具体数值在 M3、M4 里程碑实测后记入本节。

主进程只有一个线程运行业务逻辑，也就是 Qt 主线程。asyncio 事件循环由 qasync 运行在 Qt 主线程上，所有业务模块的代码都在这个线程上以协程方式执行。这个决定带来两个直接结果：

- 业务模块之间共享的状态只在一个线程上读写，业务代码不需要锁。旧版本里用 `RLock` 保护会话表、持锁发布事件这一类问题随之消失。
- 业务代码里不能出现阻塞调用。网络请求用异步 HTTP 客户端；磁盘读写和语言检测这类可能超过几毫秒的同步调用，用 `asyncio.to_thread` 交给线程池；CPU 密集的识别和翻译在子进程中执行。

主线程上剩下的工作是事件分发、竞速合并和界面绘制。其中可能超过 16 毫秒的操作有三类，第 11 章分别给出处理方式：截屏冻结、窗口首次创建、覆盖窗口的字号测量。`REL-04` 的约束范围也据此限定在竞速进行中和窗口拖动、缩放期间。

## 4 代码分层与模块边界

### 4.1 目录结构

```
lst/
  contracts/        纯数据类型与接口协议：消息、段、版面块、引擎协议、端口协议。只依赖标准库。
                    界面与服务共用的少量纯函数也放在这里，例如语言码规范化（langs.py）、
                    模型选择规则 default_model_ids 与版本比较 version_key（models.py），因为 ui 不能依赖 services。
  kernel/           事件总线、模块生命周期。不依赖 Qt。
  services/         业务模块，不依赖 Qt
    race/           竞速合并、调度、会话管理、语言检测
    engines/        各类翻译引擎的实现（每类一个子包，含字典翻译与两个免费接口）
    ocr/            OCR 进程客户端、结构归一化、段落合并、版面结果转换
    ocr_engines/    OCR 引擎的安装、运行环境版本、显卡检测、流水线模型的下载与删除
    config/         配置读取、校验、写回、密钥
    cache/          译文缓存
    stats/          竞速统计与会话耗时记录
    models/         本地 NMT 模型与词典包的索引、下载、删除
    updates/        应用内更新
    diagnostics/    诊断信息导出
  workers/          子进程入口
    ocr_worker/
    nmt_worker/
  ui/               界面模块，依赖 Qt
    theme/          Fluent 主题、颜色、字体、图标（各界面模块共用）
    i18n/           界面文案与错误码的本地化（各界面模块共用）
    common/         各窗口共用的代码（11.1 节）
    hotkeys/        全局热键注册
    tray/
    capture/        截屏蒙版
    overlay/        截屏翻译覆盖窗口
    bilingual/      双语窗口
    settings/       设置窗口
    onboarding/     首次运行引导
  app/              组合根：启动流程、进程监管、端口实现、模块装配
    automation/     自动化接口的服务端：命名管道、方法表、会话记录（第 16 章）
  automation/       自动化接口的客户端与线路协议，只依赖标准库（第 16 章）
```

### 4.2 依赖规则

| 层 | 允许依赖 |
|---|---|
| `contracts` | 标准库 |
| `kernel` | `contracts` |
| `services.*` | `kernel`、`contracts`；同层模块之间互不依赖 |
| `ui.*` | `kernel`、`contracts`、`ui.theme`、`ui.i18n`、Qt；同层其他模块之间互不依赖 |
| `workers.*` | `contracts`、推理库 |
| `app` | 全部 |
| `automation` | 标准库 |

规则写在 `pyproject.toml` 的 import-linter 契约里，`tests/architecture` 中的测试在每次运行 pytest 时检查（`MNT-01`）。这张表落实了三条需求：

1. `services` 不依赖 Qt，竞速合并、会话管理、配置校验可以在没有 `QApplication` 的单元测试里运行（`MNT-03`）。
2. `ui` 不依赖 `services`，界面只通过事件拿数据、发命令。界面整体重写时，业务代码不需要改动。
3. 新增引擎时，只在 `services/engines/` 下增加一个子包实现 `contracts` 里的引擎协议，并在 `app` 的装配代码里注册。竞速模块只认识协议，不认识具体引擎（`MNT-02`）。

### 4.3 模块生命周期

每个模块对外只暴露一个实现以下协议的类：

```python
class Module(Protocol):
    name: str
    async def start(self, ctx: ModuleContext) -> None: ...
    async def stop(self) -> None: ...
```

`ModuleContext` 携带事件总线和该模块的日志器。模块需要的配置通过订阅 `AppConfig` 状态获得（第 9 章），所以配置只有总线这一个来源。模块在 `start` 里通过 `ctx.subscribe` 订阅消息，订阅句柄由 `ctx` 托管，`stop` 后由 `ctx.close()` 统一取消。模块代码里没有模块级单例，也没有 `get_xxx_manager()` 这类全局访问函数。

组合根自己的决策也要防并发。总线把每个协程处理函数包成独立任务，连续两次发布 `AppConfig` 会得到两个同时运行的处理函数，各自握着自己那一版配置，中间又有多次等待（重载 NMT 进程要等旧进程退出）。最初的实现因此出现过旧配置把 OCR 进程重载回去、模型管理模块被替换两次的问题。现在组合根的全部决策入口（配置变化、引擎目录变化、卸载与重装前停进程、`RestartWorker`、模型变化后的 NMT 重载）都在同一把锁里串行执行，并且每一步都读 `bus.latest(AppConfig)`，不用参数传进来的旧配置。`stop()` 一开始先置停止标志，之后到达的决策直接返回。

`app` 中的组合根（`lst/app/bootstrap.py`）按固定顺序启动模块：

1. `services.config`；
2. 业务模块：`stats`、`cache`、`models`、`engines`、`race`、`ocr_engines`、`ocr`、`updates`；
3. 界面模块：`capture`、`overlay`、`tray`、`hotkeys`、`bilingual`、`settings`；
4. 自动化接口 `app.automation`（第 16 章），它直接操作截屏、覆盖窗口与设置窗口，所以排在它们之后，退出时先于它们关掉管道；
5. 最后是 `onboarding`。

截屏与覆盖窗口排在全部业务模块之后：`CaptureCompleted` 的订阅顺序因此是 OCR、竞速在前，覆盖窗口在后，识别请求先提交，再创建窗口。托盘先于热键启动，热键注册失败的通知要有托盘接收。首次运行引导最后启动，它可能在 `start` 里立即弹出窗口，这时其他模块都已就绪。退出时按相反顺序停止。

### 4.4 端口与注入

事件总线适合广播和通知，处理不了“调用一个功能并等待返回值”的场景。模块之间有四处这样的调用，它们通过端口完成：端口是定义在 `contracts` 中的协议，由 `app` 在装配时把实现注入给需要它的模块。

| 端口 | 使用方 | 实现方 | 用途 |
|---|---|---|---|
| `TranslationCache` | services.race | services.cache | 批量查询与写入译文缓存（`RACE-08`） |
| `WorkerChannel` | services.ocr、services.engines 中的本地 NMT 引擎 | app 中的监管器 | 向推理进程发送请求并接收结果（第 8 章） |
| `SecretReader` | services.engines | services.config | 读取 API key；只读，写入走 `UpdateSecret` 命令 |
| `EngineSettingsSchemas` | services.config | services.engines | 各类引擎专有参数的 pydantic 模型，用于校验 `[[engines]]` 条目（`MNT-02`） |

引擎实例的集合会随配置变化，它通过总线传递：`services.engines` 每次根据配置重建实例后，发布 `EngineRoster` 状态，内容是当前全部引擎对象；`services.race` 订阅它并更新调度器（6.5 节）。

## 5 事件总线

### 5.1 消息分类

总线上传递三类消息，全部定义在 `contracts` 中，都是 `frozen=True, slots=True` 的 dataclass：

| 类别 | 命名 | 处理者数量 | 例子 |
|---|---|---|---|
| 命令（Command） | 祈使句 | 恰好一个，否则记录错误 | `StartCapture`、`TranslateText`、`RetryOcr` |
| 事件（Event） | 过去时 | 任意个，可以为零 | `CaptureCompleted`、`OcrCompleted`、`SegmentsUpdated` |
| 状态（State） | 名词 | 任意个；总线保存最新值，新订阅者立即收到一次 | `EngineStatuses`、`WorkerStatuses`、`AppConfig` |

状态类消息解决了旧版本中托盘绕过事件、直接查询线程池单例的问题。托盘在启动时订阅 `EngineStatuses`，立即收到当前值，之后每次变化都会再收到，它不需要知道状态由哪个模块维护。

### 5.2 接口与语义

```python
class EventBus:
    def subscribe[M: Message](self, kind: type[M], handler: Callable[[M], Awaitable[None] | None]) -> Subscription: ...
    def publish(self, message: Event | State) -> None: ...
    def send(self, command: Command) -> None: ...
    def latest[S: State](self, kind: type[S]) -> S | None: ...
```

约定如下：

- 消息类本身就是订阅的键，按类型精确匹配。basedpyright 根据 `kind` 推断 `handler` 的参数类型，类型写错时静态检查报错。
- 处理函数可以是普通函数，也可以是协程函数，协程函数由总线包装成任务放入事件循环。
- `publish` 和 `send` 可以在任何线程调用。调用方不在主线程时，总线用 `loop.call_soon_threadsafe` 把分发转到主线程。所有处理函数都在主线程执行，订阅者不需要关心发布方在哪个线程。
- 分发采用排队方式：`publish` 把消息放进队列后立即返回，处理函数在事件循环的下一轮执行。发布方的调用栈里不会执行订阅者的代码，旧版本“持锁发布”的问题在这一层也被排除。
- 同一线程发布的消息按发布顺序分发，同一消息按订阅顺序交给各处理函数。协程处理函数各自作为独立任务运行，它们之间的完成顺序不受这条保证约束；需要严格顺序的处理放在普通函数里完成。
- 总线内部有一把很短的锁，只保护订阅表和各状态的最新值，因为 `publish` 可能来自子进程通信的读线程。处理函数从不在锁内执行。
- 状态的最新值在 `publish` 调用时立即更新，`latest()` 不需要等待分发。组合根依次启动模块时，后启动的模块能立刻读到先启动的模块发布的状态。
- 新订阅者收到的状态序列，恰好是“订阅时刻的最新值”加上“之后发布的每一个值”，不重复、不遗漏、不乱序。实现方式是给每个状态值分配全局递增的版本号，每个订阅者只接收比自己已收到的版本更新的值。这条性质由 hypothesis 性质测试验证；测试找出过一个早期缺陷：回放读取的是执行回放那一刻的最新值，导致订阅之后发布的中间值被跳过。
- 处理函数抛出的异常用 `logger.exception` 记录完整堆栈，日志里只写消息的类名，不写消息内容（第 10 章）。异常不影响其他处理函数。开发模式下异常会重新抛出。
- `send` 发现命令没有处理者或处理者不止一个时，记录 ERROR 日志，日志里带上命令的类名。

总线只承载跨模块的通信。模块内部的组件之间直接调用方法，界面模块内部使用 Qt 信号。

### 5.3 消息目录

下表列出第一版的主要消息，发布方和订阅方都写模块名。

| 消息 | 类别 | 发布方 | 订阅方 |
|---|---|---|---|
| `StartCapture` | 命令 | ui.tray、ui.hotkeys | ui.capture |
| `ToggleBilingualWindow` | 命令 | ui.tray、ui.hotkeys | ui.bilingual |
| `OpenSettings` | 命令 | ui.tray | ui.settings |
| `CaptureCompleted(session, image, geometry)` | 事件 | ui.capture | services.ocr、services.race（附带截图）、ui.overlay |
| `OcrCompleted(session, blocks, segments)` | 事件 | services.ocr | services.race、ui.overlay |
| `OcrFailed(session, error)` | 事件 | services.ocr | ui.overlay |
| `RetryOcr(session)` | 命令 | ui.overlay | services.ocr |
| `TranslateText(session, text, source, target, engine_id)` | 命令 | ui.bilingual | services.race |
| `SegmentsUpdated(session, views)` | 事件 | services.race | ui.overlay、ui.bilingual |
| `SessionProgress(session, finished, total)` | 事件 | services.race | ui.overlay |
| `StatsSnapshot(engines)` | 状态 | services.stats（启动时一次，写入后 1 秒内合并发布一次，重置后立即发布） | ui.settings（竞速页，`RACE-07`） |
| `ResetStats` | 命令 | ui.settings | services.stats |
| `ClearCache` | 命令 | ui.settings | services.cache |
| `CacheCleared(removed)` | 事件 | services.cache | ui.settings |
| `CheckEngineConnection(engine_id)` | 命令 | ui.settings（“测试连接”） | services.engines |
| `EngineConnectionChecked(engine_id, ok, error_code, latency_ms)` | 事件 | services.engines | ui.settings |
| `AttemptRecorded(session, segment, attempt)` | 事件 | services.race | services.stats |
| `EngineRoster` | 状态 | services.engines | services.race |
| `EngineStatuses` | 状态 | services.engines | ui.tray、ui.settings、ui.bilingual（实例名与标识颜色） |
| `WorkerStatuses` | 状态 | app（汇总各监管器的状态） | ui.tray、ui.overlay、services.engines |
| `ModelCatalog` | 状态 | services.models | ui.settings、ui.onboarding |
| `OcrEngineCatalog(engines, active)` | 状态 | services.ocr_engines（组合根合并 OCR 进程状态） | ui.settings、app |
| `InstallOcrEngine(kind, variant)`、`CancelOcrEngineInstall(kind)`、`UninstallOcrEngine(kind)` | 命令 | ui.settings | services.ocr_engines |
| `DownloadOcrModel(kind, model_id)`、`CancelOcrModelDownload(kind, model_id)`、`DeleteOcrModel(kind, model_id)` | 命令 | ui.settings（截屏与识别页的流水线区域） | services.ocr_engines |
| `OcrEnginesChanged(kind, installed)` | 事件 | services.ocr_engines | （保留给需要区分安装与卸载的订阅方） |
| `RestartWorker(worker)` | 命令 | ui.tray、ui.settings（推理进程 FAILED 后的“重新启动”） | app |
| `UpdateStatus` | 状态 | services.updates | ui.tray、ui.settings |
| `CheckForUpdates(manual)`、`DownloadUpdate`、`InstallUpdate` | 命令 | ui.tray、ui.settings | services.updates |
| `ShowModelDownloads` | 命令 | ui.tray（菜单“下载翻译模型”） | ui.onboarding（重新显示引导窗口） |
| `RefreshModelCatalog`、`DownloadModels(ids)`、`CancelModelDownload(id)`、`DeleteModel(id)` | 命令 | ui.onboarding、ui.settings | services.models |
| `ModelsInstalled(ids, removed)` | 事件 | services.models | app（涉及 NMT 模型时重载 NMT 进程；只涉及词典包时忽略） |
| `AppConfig` | 状态 | services.config | 全部模块 |
| `UpdateConfig(patch)` | 命令 | ui.settings、ui.onboarding | services.config |
| `UpdateSecret(name, value)` | 命令 | ui.settings、ui.onboarding | services.config |
| `ConfigRejected(location, reason)` | 事件 | services.config | ui.tray（1 秒内合并成一条通知）、ui.settings |
| `NotificationRequested(title_key, message_key, level, params)` | 事件 | app、各模块 | ui.tray |
| `HotkeyRegistrationFailed(hotkey, reason)` | 事件 | ui.hotkeys | ui.tray |
| `ExportDiagnostics` | 命令（尚未定义，随 services.diagnostics 落地） | ui.tray、ui.settings | services.diagnostics |
| `QuitRequested` | 事件 | ui.tray | app |

`TranslateText` 的 `source` 和 `target` 为空时表示“自动”（`BI-03`）；`engine_id` 是双语窗口里选中的翻译服务，这次请求只交给它，为空表示没有可用的服务；`text` 去掉空白后为空表示取消双语窗口的当前会话。每个请求开始一个新会话，会话号由双语窗口生成（11.6 节）。`EngineStatuses` 除状态、名称与标识颜色外，还带各实例的优先级和计费标记，双语窗口据此排列服务列表并标出计费服务。`AttemptRecorded` 只带状态、耗时和是否命中缓存，不带译文（`PRV-02`）。

## 6 竞速翻译核心

竞速核心位于 `services/race`，由三部分组成：

- 合并器：同步的状态机，不依赖事件循环和时钟，决定一个结果到达后显示什么、竞速是否结束、某个排队中的尝试是否还可能改变显示。
- 调度器：把段打包成任务，派发给引擎实例，处理并发上限、剪枝和超时。
- 会话管理：按入口维护当前会话，处理新会话对旧会话的取消。

### 6.1 数据模型

一个引擎实例在一个段上的一次翻译称为一个尝试（Attempt）。尝试有五种状态：

```python
class AttemptState(Enum):
    QUEUED = 1      # 已派发，等待引擎的并发名额
    RUNNING = 2     # 引擎正在处理
    DONE = 3        # 成功返回译文
    FAILED = 4      # 失败（超时、网络、解析、实例被移除等）
    CANCELLED = 5   # 在排队时被剪枝，从未开始执行
```

前两种称为“未结束”，后三种称为“已结束”。已结束的尝试不会回到未结束状态。

```python
@dataclass(slots=True)
class Attempt:
    engine_id: EngineId
    priority: int
    state: AttemptState
    dispatched_at: float          # 段派发时刻，单调时钟
    finished_at: float | None
    text: str | None
    error: str | None
    arrival_seq: int | None       # 成功结果在本段内的到达次序，供界面和统计使用
    cached: bool                  # 结果来自缓存；竞速统计排除这类样本
    started: bool                 # 进入过 RUNNING 状态；竞速统计只计入执行过的尝试（RACE-07）
    error_code: ErrorCode | None  # 失败原因的分类，界面据此显示原因与建议（10.3 节）
```

每个段有一个 `SegmentRace`，保存该段全部尝试和当前胜出者；每个会话有一个 `SessionRace`，保存全部段并记录哪些段需要刷新界面。发给界面的快照 `SegmentView` 里，当前胜出者叫 `shown_engine`：它就是当前显示的译文来自的实例。没有参加竞速的段带一个原因：没有可翻译的文字、源语言与目标语言相同（都是 `RACE-09`），或者没有任何实例支持该语言对。

### 6.2 合并规则

记一个段的参赛实例集合为 $E$，实例 $e$ 的优先级为 $p(e)$，数值越小优先级越高。记 $w$ 为当前胜出实例，尚无成功结果时 $w = \varnothing$。

本章的推导建立在三条前提上：

- **前提 1（优先级互异）**：$E$ 中各实例的优先级两两不同。配置服务保证这一点：配置中出现重复的优先级时，按实例在列表中的顺序重新编号，设置窗口的拖拽排序也只产生 1、2、3……的编号（第 9 章）。
- **前提 2（每个尝试至多一个结果）**：一个尝试只在未结束状态下接受结果，结果到达后进入已结束状态，之后再到达的结果被忽略。
- **前提 3（尝试必然结束）**：每个尝试在有限时间内进入已结束状态。6.5 节说明调度器如何保证这一点。

实例 $e$ 返回成功译文时，合并器执行：

$$
w \leftarrow
\begin{cases}
e, & w = \varnothing \\
e, & w \neq \varnothing \text{ 且 } p(e) < p(w) \\
w, & \text{其他情况}
\end{cases}
$$

实例返回失败时，$w$ 不变（`RACE-04`）。

这条规则有两个性质：性质 1 保证界面不会来回闪烁，性质 2 保证最终显示的译文与网络快慢无关。

**性质 1（显示单调）**：每个段显示的译文优先级只升不降，一个段的显示变化次数不超过 $|E|$ 次。

证明：$w$ 只在两种情况下改变。

- 第一种是 $w$ 从 $\varnothing$ 变成某个实例，只发生一次。
- 第二种要求 $p(e) < p(w)$，每发生一次，$p(w)$ 严格减小。$p(w)$ 只能取 $E$ 中各实例的优先级值，最多 $|E|$ 个不同取值，所以严格减小最多发生 $|E| - 1$ 次。

两种情况合计，变化次数不超过 $|E|$。这个证明不需要前提 1。

**性质 2（终态与到达顺序无关）**：在前提 1 下，所有参赛实例都结束后，$w$ 是成功实例中优先级最高的那个。

证明：记 $S_k$ 为前 $k$ 个到达的成功结果对应的实例集合。对 $k$ 归纳，证明处理完第 $k$ 个成功结果后：

$$
w = \operatorname*{arg\,min}_{e \in S_k} p(e)
$$

- $k = 1$ 时，$w$ 从 $\varnothing$ 变为第一个成功实例，命题成立。
- 设命题对 $k$ 成立，第 $k+1$ 个成功实例为 $e$。若 $p(e) < p(w)$，则 $e$ 的优先级严格高于 $S_k$ 中所有实例，$w \leftarrow e$ 后命题成立。否则由前提 1 得 $p(e) > p(w)$，$w$ 不变，并且仍是 $S_{k+1}$ 中优先级最高的实例。

所有成功结果都到达后，$S_k$ 就是全部成功实例，命题即性质 2。

合并器的实现在优先级相同时保留先到达的结果，所以即使前提 1 被违反，性质 1 和后文的结束条件依然成立，只是并列最高的几个实例之间由到达顺序决定胜负。

需求文档 5.5 节的时间线是这两条性质的一个具体例子：显示依次经过优先级 3、2、1，变化 3 次，最终停在优先级最高的 DeepSeek。

### 6.3 结束条件与取消

定义仍可能改变显示的尝试集合：

$$
H = \{\, e \in E \;:\; e \text{ 未结束},\ \ w = \varnothing \ \text{或}\ p(e) < p(w) \,\}
$$

属于 $H$ 的尝试称为“竞争中”。$H = \varnothing$ 时称这个段“显示已定”（finished）：竞速结束，进度计数（`OV-07`）按它统计，调度器取消低优先级尝试也以 $H$ 为判据（6.4 节）。

显示已定时，$w \neq \varnothing$ 表示竞速成功结束；$w = \varnothing$ 表示所有实例都失败或被剪枝，界面显示原文和各实例的失败原因（`RACE-04`）。

**引理 1（$H$ 单调不增）**：参赛集合 $E$ 不变时，对任意时刻 $t < t'$，有 $H_{t'} \subseteq H_t$。

证明：只需证明任何一次状态变化都不会让某个尝试新进入 $H$。状态变化有三种：

- 某个尝试开始执行（QUEUED → RUNNING）：它仍未结束，$w$ 不变，所有尝试的成员资格不变。
- 某个尝试被剪枝或收到结果：它进入已结束状态，离开 $H$。若结果使 $w$ 改变，新胜出者 $w'$ 满足 $w = \varnothing$ 或 $p(w') < p(w)$。条件“$p(e) < p(w')$”比“$p(e) < p(w)$”更严格，所以没有尝试因此进入 $H$。
- 其他尝试的状态不受影响。

**定理 1（显示已定后不再变化）**：参赛集合不变时，若某段在时刻 $t$ 显示已定，则 $t$ 之后 $w$ 不再改变。

证明：用反证法。设 $t' > t$ 是 $t$ 之后 $w$ 第一次改变的时刻，这次改变由尝试 $e$ 的成功结果引起，所以在 $t'$ 之前 $e$ 未结束，并且 $w_{t'^-} = \varnothing$ 或 $p(e) < p(w_{t'^-})$。在 $t$ 与 $t'$ 之间 $w$ 没有变化，所以 $w_{t'^-} = w_t$。已结束的尝试不会回到未结束状态，所以 $e$ 在时刻 $t$ 也未结束。于是 $e \in H_t$，与 $H_t = \varnothing$ 矛盾。

**引理 2（取消不改变终态）**：调度器只取消不在 $H$ 中的尝试，排队中的和执行中的都一样。对任意一组结果，取消后的终态与这些尝试照常执行时的终态相同。

证明：设尝试 $e$ 在时刻 $t$ 被取消，此时 $e \notin H_t$ 且 $e$ 未结束，所以 $w_t \neq \varnothing$ 且 $p(e) > p(w_t)$。假如 $e$ 照常执行，并在之后某个时刻 $t''$ 返回成功结果。由性质 1，$p(w_{t''}) \le p(w_t) < p(e)$，所以这个结果不会改变 $w$。失败结果本来就不改变 $w$。因此 $e$ 是否执行，不影响任何时刻的 $w$。

引理 2 说明取消低优先级尝试只省下算力和费用，不改变任何时刻显示的译文。早期版本还有一个“完整模式”，让所有实例都跑完以便比较；按引理 2 它与取消的结果完全相同，只多花费用，现在已经删除，旧配置里的 `[race].mode` 读取时丢掉。

**活性**：在前提 3 下，每个段最终都会显示已定。$E$ 是有限集合，每个尝试在有限时间内结束，所以最终没有未结束的尝试，$H \subseteq \{\text{未结束的尝试}\} = \varnothing$。最高优先级实例失败时同样如此：例如 DeepSeek 返回 401 后，它离开 $H$，剩下的竞争中尝试结束后竞速就结束。

参赛集合 $E$ 在会话开始派发时确定，之后不再加入成员，所以引理 1 与定理 1 的前提“$E$ 不变”在整个会话里成立。早期版本的双语窗口按 `Ctrl+Enter` 时让计费实例晚加入当前会话，会扩大 $E$；双语窗口改为只用一个选中的服务之后，这条路径已经删除。

### 6.4 任务打包、取消与流式结果

调度器不直接为每个段发一次请求。每个引擎实例声明自己的打包参数，调度器把同一实例、同一语言对（源语言，目标语言）的段按顺序贪心打包成任务（Job）：一个任务的段数不超过 `max_batch_segments`，文本总长不超过 `max_batch_chars`，单段超长时单独成为一个任务。

| 引擎类别 | 打包参数的取值依据 |
|---|---|
| 本地 NMT | NMT 进程的批量推理上限 |
| 在线 LLM、本地 LLM | 上下文窗口与单次输出长度；任务同时附上会话原文作为上下文（`ENG-04`） |
| 传统在线接口 | 接口的单次请求条数和字符上限（2.7.2 节） |

语言对按段区分，是因为双语窗口的互译对规则（`RACE-10`）会让同一会话里不同段的目标语言不同。

每个引擎实例有一个两级任务队列，以及数量等于最大并发数的工作协程（`ENG-05`）。双语窗口的任务排在第一级，截屏的任务排在第二级，工作协程先取第一级，第一级为空时再取第二级，同一级内先进先出。双语窗口是边打字边看结果，目标是 500 毫秒出译文；一张截图可能有几十个任务，不能挡在它前面。

某个段的胜出者改变后，比新胜出者优先级低的同段尝试都不可能再改变显示（引理 1、引理 2）。调度器按尝试所处的阶段取消它们（`RACE-05`），判据都是“尝试未结束，并且不在 $H$ 中”：

- 排队中，即时取消：胜出者改变时，调度器立即把该段所有满足判据的 QUEUED 尝试标为 CANCELLED。界面和统计马上看到“已取消”，不必等这些尝试排到队首。
- 排队中，出队兜底：工作协程取出一个任务后、调用引擎前，对任务里的每个段检查该实例的尝试。尝试已不在 QUEUED 状态时，从任务中移除这个段；满足判据时标为 CANCELLED 并移除；其他情况标为 RUNNING，保留在任务中。剪枝后任务为空，就直接丢弃，不发请求。
- 执行中，按任务取消（`RaceScheduler._prune_running`）：每次有结果并入合并器之后，调度器检查包含这个段的执行中任务。一个任务里还在执行（RUNNING）的段全都不在 $H$ 中时，调度器取消这个任务；只要还有一个段在 $H$ 中，任务就继续，批量请求的其他段还需要它的译文。任务里已经出过结果的段不参与判断。被取消的任务结束后，它还没出结果的段标为 CANCELLED（`_finish_pruned`）。

执行中的取消沿引擎自己的取消通路传下去：任务所在的 asyncio 任务被取消，引擎的生成器收到 `CancelledError`。在线引擎由此关闭 HTTP 请求；本地 NMT 引擎调用请求句柄的 `cancel()`，监管器向 NMT 进程发 `cancel` 消息（6.5 节第 4 条）。

被取消的尝试记为 CANCELLED，不是 FAILED。悬停提示显示“已取消”；统计不把它算作执行过的尝试，也不计入失败率（`RACE-07`）。

三种取消可以并存。出队时先检查状态，已取消的尝试只会被移除，不会被重新启动；执行中的任务只取消一次（`_RunningJob.pruned` 置位后不再检查）。判据本身是稳定的：由引理 1，一个尝试一旦不在 $H$ 中，之后再也不会回到 $H$。取消的时机也早于“竞速结束”：例如 DeepSeek 还在运行、Argos 已经返回时，优先级低于 Argos 的另一个实例已经可以取消。任务在队列里时只有尝试的状态会变，任务本身的段列表只在出队这一刻修改，所以不存在“修改任务时它正好被取出执行”的竞争。

按任务而不是按段取消，是因为引擎协议的取消粒度就是任务：一个批量请求无法只撤回其中几段。按“剩下的段全都不在 $H$ 中”判断，保证被取消的任务不会带走任何还可能显示的译文：设任务 $J$ 在时刻 $t$ 被取消，$J$ 里每个 RUNNING 的段 $s$ 都满足 $e \notin H_t(s)$，由引理 2，$e$ 在 $s$ 上的结果无论成败都不改变 $s$ 的显示。

引擎协议如下，与 `lst/contracts/translation.py` 一致：

```python
class Engine(Protocol):
    @property
    def info(self) -> EngineInfo: ...
    def supports(self, source: Lang, target: Lang) -> bool: ...
    def translate(self, job: Job) -> AsyncIterator[SegmentOutcome]: ...
```

`translate` 是异步生成器，每完成一个段就产出一个结果。LLM 流式返回时，引擎每确认一项完整（2.7.1 节）就产出一次，调度器立即交给合并器。一个包含 20 段的批量请求，第一段的译文在流开始后就能显示，不需要等整批结束。

调度器对引擎的输出做三项防御：

- 引擎抛出异常：任务中还没有结果的段全部记为失败，错误信息带异常类型。
- 生成器结束后仍有段没有结果：这些段记为失败，原因为“引擎未返回该段”。
- 产出了不属于本任务的段号：忽略并记日志。

缓存（`RACE-08`）由调度器在打包之前查询。缓存键由原文、源语言、目标语言、引擎实例号和实例的配置指纹组成；配置指纹是影响译文的配置项（模型、提示词、温度等）的哈希，换模型或改提示词后旧译文不会被误用。命中的尝试直接记为成功，耗时为 0，并标记为缓存命中，不进入任务。

### 6.5 会话、取消与活性保证

会话按入口区分，截屏和双语窗口各自最多有一个当前会话。新会话开始时，旧会话按以下顺序取消（`RACE-01`、`CAP-06`）：

1. 旧会话被标记为已关闭并从会话表中移除。之后到达的结果属于已关闭的会话，直接丢弃，不产生任何界面事件。
2. 旧会话在各引擎队列里的任务在出队时被丢弃，不调用引擎。
3. 正在执行的任务所在的 asyncio 任务被取消，引擎的生成器收到 `CancelledError`。在线引擎借此中断 HTTP 连接。
4. 本地 NMT 引擎收到 `CancelledError` 时，调用请求句柄的 `cancel()`。请求还在监管器排队时，监管器直接结束它；已经发给 NMT 进程时，监管器发送 `cancel` 消息。尚未开始的批次一直留在调度器的队列里，没有发给 NMT 进程，所以 NMT 进程只需处理已经收到的请求；正在推理的批次跑完后，结果在主进程被丢弃。

前提 3（尝试必然结束）由以下四条机制共同保证：

1. 每个引擎实例的任务都有有限的总时限。配置服务要求每个实例的时限为有限值，默认值见 2.7.3 节。时限到达时，任务中还没有结果的段记为失败，原因为“超时”。
2. 排队等待的时间有限，分两级队列论证：
   - 双语任务排在第一级，同一级内先进先出。排在它前面的任务数量有限，每个任务的执行时间受第 1 条约束，所以它的等待时间有限。
   - 截屏任务排在第二级，只在第一级为空时执行。第一级的任务来自双语窗口，而双语窗口同一时刻只有一个当前会话：每次输入停顿产生的新会话会让旧会话的任务在出队时被立即丢弃，不调用引擎。所以只要用户在有限时间内停止输入，第一级就会在有限时间内清空，截屏任务随后开始执行。用户持续不停地输入时，截屏任务会一直等待。这是两级队列的有意取舍：双语窗口的交互延迟优先。
3. 推理进程进入 FAILED 状态（8.2 节）后，经由它执行的引擎对所有新任务和在途任务立即返回失败。
4. 配置变更导致某个引擎实例被移除或重建时，`services.race` 收到新的 `EngineRoster`，调用调度器的 `set_engines`。调度器以 `EngineInfo` 是否相等判断实例有没有变化。`EngineInfo` 里有两个字段专门服务于这个判断：`fingerprint` 是影响译文的配置的哈希，同时参与缓存键；`revision` 在实例的任何配置变化时改变，包括 API key 这类不影响译文的配置。所以只换 API key 时缓存仍然有效，实例照样会被替换。判断之后分三种情况处理：
   - 被移除或有变化的旧实例：它在所有会话中的未结束尝试记为失败，原因为“实例已移除”，执行中的任务被取消，队列中的任务被丢弃。
   - 有变化的实例和新增的实例：只参加之后开始的会话。
   - 没有变化的实例：原样保留。

单个任务的时限是固定值，不随任务中的段数变化。LLM 的批量任务耗时随段数增长，所以批的大小（`max_batch_chars`）和时限要一起设置。按段数缩放时限的做法留到 M2 实测之后再定。

### 6.6 界面更新合帧

会话创建时，全部段都记入待刷新集合，所以界面收到的第一个 `SegmentsUpdated` 包含会话里的每个段，暂时没有参赛者的段也在其中。之后合并器每改变一个段的状态，调度器就把这个段记入集合。刷新时把集合里所有段的快照放进一个 `SegmentsUpdated` 事件发布，然后清空集合。刷新时机按以下规则安排：

- 会话处于空闲状态（距上次刷新超过一帧，即 1/60 秒，并且没有待执行的刷新）时出现的第一次变化，立即安排刷新（`loop.call_soon`）。
- 刷新之后一帧之内的变化，合并到一次延后一帧执行的刷新里。

只用定时器延后刷新不够：Windows 定时器的默认粒度约为 15.6 毫秒，延后一帧的刷新实际可能晚 30 毫秒以上，挤占 50 毫秒的预算。按上面的规则，一个孤立到达的结果没有排队等待；结果密集到达时，每秒最多 60 次刷新。

需求 6.1 节的“引擎结果到界面完成替换”定义为：结果到达主进程，到界面绘制出过渡动画的第一帧。它的预算分配如下：

| 环节 | 预算 |
|---|---|
| 子进程读线程或 HTTP 流把结果交给事件循环 | ≤ 2 毫秒 |
| 合并与合帧等待（孤立结果为 0，密集到达时最多一帧） | ≤ 17 毫秒 |
| 事件分发与界面更新 | ≤ 5 毫秒 |
| 下一次屏幕刷新 | ≤ 17 毫秒 |
| 合计 | ≤ 41 毫秒 |

一张截图有 100 个段、4 个参赛实例时，由性质 1，显示最多变化 400 次。按段逐个发布事件就是 400 次界面刷新；合帧后，每秒最多 60 次刷新，与结果数量无关。

## 7 截屏翻译数据流

1. `ui.hotkeys` 或 `ui.tray` 发出 `StartCapture` 命令，`ui.capture` 冻结各显示器画面，显示蒙版。
2. 用户框选完成后，`ui.capture` 用 UUID4 生成新的会话号（统计库按会话号和段号记录胜出者，会话号必须跨重启唯一），把选区图像转成 `contracts` 中定义的 `RawImage`（宽、高、行跨度、BGRA 像素字节串），然后发布 `CaptureCompleted`。
   - 不用 `QImage`：`contracts` 不依赖 Qt。
   - 不编码成 PNG：PNG 要压缩约 8 MB 的像素数据，主线程承担不起；转成原始字节只是一次内存拷贝。
   - 事件是不可变对象，总线只传递引用，像素数据在进程内不会再被复制。
3. `ui.overlay` 收到 `CaptureCompleted`，在选区位置弹出覆盖窗口，显示模糊底图和“识别中”。OCR 进程还在加载时，覆盖窗口根据 `WorkerStatuses` 中 OCR 进程的状态显示“识别模型加载中”（`OCR-07`）。
4. `services.ocr` 收到 `CaptureCompleted`，通过 `WorkerChannel` 把识别请求和图像字节发给 OCR 进程。OCR 进程完成预处理和识别，返回版面块。
5. `services.ocr` 在主进程中对版面块做结构归一化（8.7 节，段落合并是其中的聚合步骤），再把版面块转换成段，发布 `OcrCompleted`。归一化与段落合并是纯 Python 的几何计算，耗时在毫秒级，并且需要回归测试（`OCR-05`、`OCR-11`）；放在 `services` 里，测试时不需要启动 OCR 进程。
6. `ui.overlay` 按版面块渲染原文；`services.race` 为每个段检测语言，然后开始竞速。开启了“附带截图”（`ENG-07`）的 LLM 实例，任务里同时带上截图（见 7.1 节）。
7. 竞速过程中，`services.race` 按 6.6 节的规则发布 `SegmentsUpdated` 和 `SessionProgress`，`ui.overlay` 增量重绘并播放过渡。
8. 每个尝试结束时，`services.race` 发布 `AttemptRecorded`，`services.stats` 写入统计库。

### 7.1 附带截图

`services.race` 订阅 `CaptureCompleted`，截屏一到就在线程里把截图缩放并编码为 PNG，编码与 OCR 同时进行，OCR 结束时截图通常已经编好。要编哪几种尺寸由名册决定：每个开启 `send_image` 的实例有自己的 `image_max_side`，名册里没有这样的实例时完全不编码。截屏会话开始时，调度器把对应尺寸的截图（`JobImage`：媒体类型、宽高、SHA-256 摘要与字节）放进这些实例的任务，其他实例的任务不带图。只保留最近一次截屏的截图。新截屏到达时只丢弃旧截图的引用，不取消正在进行的编码：线程里的编码本来无法中断，而上一次会话可能正等着它；只有模块停止时才取消。等待编码时内层任务被取消，按“没有截图”处理，不把取消传给调用方。编码失败只记日志，实例照常参赛，只是不带图。名册被调度器拒收时，截图尺寸仍按原名册计算。

`services` 不能依赖 PySide6，所以编码用 numpy 与 zlib：缩放用面积平均，输出 8 位 RGB 的 PNG。3840×2160 缩到 1568×882 在线程里实测约 0.27 秒。编码函数可以通过 `RaceModule(image_encoder=…)` 替换。

LLM 引擎把截图放在 user 消息的 content 数组里，排在文本部分之后，文本部分与不带图时逐字相同，前缀缓存照样命中。system 提示词末尾追加一句，说明截图用来理解上下文、纠正识别错误。服务返回“不支持图片”类错误时，这次尝试以 `engine.image_unsupported` 失败，不再逐段退回。

带图的尝试，译文缓存键多一个截图摘要：同样的文字配不同截图不会互相命中，不带图的缓存键与原来逐字节相同。请求体从不写日志，服务端在错误消息里回显的 data URL 与长 base64 片段替换为占位文字。

## 8 推理进程与监管

### 8.1 通信协议

主进程与两个推理进程之间的消息都是 JSON 对象，每条消息都有 `type` 字段。OCR 请求后面紧跟一个原始字节帧，内容是图像像素。

| 方向 | 消息 | 主要字段 |
|---|---|---|
| 主 → 子 | `hello` | 协议版本 |
| 主 → 子 | `configure` | 设备、模型等该进程需要的配置；只在启动和重载时发送 |
| 主 → 子 | `ocr` | 请求号、会话号、图像宽高与跨度、子能力开关与预处理参数；后跟像素帧 |
| 主 → 子 | `translate` | 请求号、会话号、语言对、段列表 |
| 主 → 子 | `cancel` | 请求号：尚未开始的请求直接丢弃并回复 `request_failed`；正在执行的照常跑完，结果由主进程丢弃 |
| 主 → 子 | `shutdown` | 无 |
| 子 → 主 | `ready` | 协议版本、能力清单、依赖版本、GPU 信息 |
| 子 → 主 | `progress` | 模型下载或加载进度 |
| 子 → 主 | `started` | 请求号：子进程开始处理该请求，处理时限从此刻计时 |
| 子 → 主 | `ocr_result` | 请求号、版面块列表 |
| 子 → 主 | `segment_result` | 请求号、段号、译文或错误码与错误；每完成一段发送一次 |
| 子 → 主 | `request_done` | 请求号：翻译请求的全部段都已发回 |
| 子 → 主 | `request_failed` | 请求号、错误码、错误信息；被取消时错误码为 `cancelled` |

消息的定义与编解码在 `contracts/worker_protocol.py`，主进程的监管器和子进程使用同一份代码。取消按请求号进行，因为发起取消的本地 NMT 引擎只知道自己发出的那个请求。翻译请求以 `request_done` 结束，OCR 请求以 `ocr_result` 结束，两者都可能以 `request_failed` 结束，调用方据此判断回复已经到齐。

子进程收到 `hello` 后比较协议版本，不一致时以退出码 64 退出。主进程和 OCR 进程的代码不同步时，问题在启动时就暴露出来。三个退出码表示确定性故障，监管器收到后直接进入 FAILED：

| 退出码 | 含义 |
|---|---|
| 64 | 协议版本不符 |
| 65 | 推理依赖导入失败，例如 DLL 加载失败 |
| 66 | 启动参数或配置无效 |

authkey 以十六进制从子进程的标准输入第一行传入，不放在命令行参数里，避免出现在进程列表中。

### 8.2 监管器

`app` 中的监管器为每个推理进程维护一个状态机：

```
STOPPED → STARTING → LOADING → READY ⇄ BUSY
             │           │        │
             └───────────┴────────┴──→ CRASHED ─（退避等待）→ STARTING
                                          │
                                          └─（10 分钟内第 5 次）→ FAILED
STARTING ─（确定性故障）→ FAILED
FAILED ─（用户重载或修改配置）→ STARTING
```

这些状态映射到托盘的五种显示状态（`TR-03`）：

| 监管器状态 | 托盘显示 |
|---|---|
| STOPPED | 未启用 |
| STARTING、LOADING | 加载中 |
| READY | 就绪 |
| BUSY | 忙碌 |
| CRASHED、FAILED | 出错 |

监管规则如下。

1. 崩溃检测：等待子进程句柄退出。原生库导致的段错误同样会让进程退出，所以这种情况也能检测到。
2. 确定性故障：协议版本不符、依赖导入失败（例如 DLL 加载失败）时，子进程以约定的退出码退出，监管器直接进入 FAILED，不做重启。这类故障重启多少次结果都一样。
3. 卡死检测：每个请求的处理时限从收到 `started` 消息时开始计时，排队时间不计入，超时后结束该进程并重启。监管器不做心跳检测：原生推理可能长时间持有 Python 的全局解释器锁（Global Interpreter Lock，GIL），心跳线程按时发不出消息，会造成误判。处理时限如下：

   | 请求 | 时限 |
   |---|---|
   | NMT 每批 | 5 秒 |
   | OCR 文本模式 | 30 秒 |
   | OCR 版面模式 | 60 秒，表格、公式、图表识别每开启一项再加 60 秒 |

   OCR 的时限只用来判断进程是否卡死，与需求 6.1 节的性能目标无关；时限值可以在配置中修改。
4. 重启：退避间隔依次为 1、2、4、8 秒。10 分钟内崩溃 5 次就进入 FAILED，不再自动重启，直到用户在设置里修改配置或点击“重载”。
5. 崩溃时的在途请求：OCR 请求以 `OcrFailed` 结束，覆盖窗口显示“OCR 进程崩溃，已重启”和重试按钮（`OCR-08`）。NMT 请求中尚未返回的段全部记为失败，其他引擎照常竞速（`ENG-06`）。
6. 就绪前的请求：子进程进入 READY 之前，请求在监管器中排队，不发给子进程（`OCR-07`）。监管器进入 FAILED 时，排队的请求全部以失败返回。
7. 重载（`CFG-03`）：先发送 `shutdown`，等待 3 秒，进程仍未退出就强制结束，然后用新配置启动。
8. 退出（`REL-03`）：向所有子进程发送 `shutdown`，总共等待 2 秒，然后强制结束剩下的进程。所有子进程在创建时都加入一个设置了 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Windows 作业对象，主进程即使崩溃，系统也会结束这些子进程。

监管器的实现（`lst/app/supervisor.py`）在上面八条之外，还确定了以下行为。

#### 8.2.1 启动、重载与提交

- `start()` 启动监管任务后立即返回，不等子进程就绪。OCR 模型加载可能要几分钟，组合根按顺序启动模块，不能卡在这里；加载期间提交的请求在监管器里排队。需要等待某个状态时调用 `wait_for_state()`。
- `reload()` 结束旧进程再用新配置启动，与第 7 条一致。已发给旧进程的请求以 `cancelled` 结束，还在监管器排队的请求留给新进程。OCR 子能力这类可以就地生效的配置随每个请求发送（8.4 节），不经过 `configure`，所以监管器只需要启动与重载两种入口。
- 监管器停止状态下提交的请求立即以失败结束，消费者不会一直等待。
- 退避间隔按 10 分钟窗口内的崩溃次数取 1、2、4、8 秒，之后保持 8 秒；窗口内第 5 次崩溃进入 FAILED。
- 启动命令可以延后给出：OCR 监管器构造时没有命令，由组合根第一次 `reload(settings, command=…)` 给出，命令随引擎而定。没有命令时 `start()`、`retry()` 抛异常。
- 创建进程在线程里进行，命令在事件循环线程上取快照后传入；线程返回后若命令已被 `reload(command=…)` 换掉，就按重载结束刚起来的进程，用新命令重启。不这样做的话，切换引擎恰好落在进程创建期间时，新配置会发给按旧命令启动的进程。
- FAILED 之后由 `RestartWorker` 命令恢复（托盘状态区与设置窗口关于页提供“重新启动”）：组合根按当前配置重新决定并启动进程，崩溃计数清零。
- `stop()` 等待监管任务结束有总时限 `stop_timeout_s`（默认 10 秒），超时记 ERROR、取消监管任务并强制结束子进程，程序退出不会被卡在驱动里的进程挂住。

#### 8.2.2 线程模型

监管器的状态只在事件循环线程上读写。每个子进程配一组线程：等待进程退出、accept、读消息、写消息（大的像素帧在这里发送）、读 stderr、读 stdout。它们通过 `call_soon_threadsafe` 把结果交回事件循环。stderr 每行记 WARNING，faulthandler 输出的崩溃堆栈因此在默认日志级别下保留；stdout 记 DEBUG；每行最长 4000 字符。

进程退出后，监管器最多再等 1 秒，让读线程交完退出前发出的消息，再结算在途请求。这样退出前刚发出的 `request_done` 不会被误判为崩溃。

#### 8.2.3 Windows 上的几个实测问题

- 关闭 `Listener` 并不能让阻塞中的 `accept` 返回：`PipeListener.accept` 先把要等待的管道实例从队列里取出来，`close()` 只关闭队列里剩下的实例。放弃等待时，监管器另起一个短命线程，以客户端身份反复连接再断开这个管道，`accept` 因认证失败返回。
- `.venv\Scripts\python.exe` 是一个启动器，它再启动真正的解释器，并把解释器放进自己的、带 `KILL_ON_JOB_CLOSE` 的作业对象。监管器在 `Popen` 返回后才把启动器加入自己的作业，这时解释器往往已经创建，不在监管器的作业里。`REL-03` 仍然成立，靠的是一条传递链：主进程退出 → 监管器的作业关闭 → 启动器被结束 → 启动器的作业关闭 → 解释器被结束。剩下的缺口是解释器自己再创建的子进程不在任何作业里；NMT 进程不创建子进程，OCR 进程在 M4 实测 PaddleOCR 是否创建子进程后再决定是否处理。
- 作业对象还设置了 `JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION`，崩溃的进程直接结束，不会被 Windows 错误报告对话框挂住，否则监管器等不到它退出。
- `ctypes.string_at(0)` 在 Windows 上不会让进程崩溃：ctypes 用结构化异常处理包住外部调用，把访问违例转成 `OSError`，但 faulthandler 照样打印堆栈。所以 stderr 里出现 faulthandler 堆栈，不一定说明进程崩溃了，监管器以进程是否退出为准。

#### 8.2.4 文档之外的异常情况

| 情况 | 处理 |
|---|---|
| 子进程在协议握手后以退出码 0 意外退出 | 按崩溃处理 |
| 管道断开后 1 秒内进程仍未退出 | 强制结束，按崩溃处理 |
| 子进程发来无法解析的消息 | 立即强制结束，按崩溃处理 |
| `ready` 里的协议版本不符 | 与退出码 64 相同，直接进入 FAILED |
| 管道上出现未通过 authkey 认证的连接 | 记一条警告，继续等待真正的子进程，不算崩溃 |

### 8.3 NMT 进程

- 模型在第一次用到某个语言对时加载，之后常驻内存。同时驻留的模型数有上限，默认 4 个，超出时卸载最近最少使用的模型。
- 推理只用一个线程，多个推理线程只会互相争抢 CPU。CTranslate2 内部的线程数默认取 0，交给它自己决定：本机是 8 个性能核加 16 个能效核的混合架构 CPU，实测 24 线程比默认值慢约 60%，把线程数设为物理核数在这类 CPU 上反而最慢。
- 模型包第一次用到时才解压到模型目录的 `nmt/.extracted`，目录名带模型包的内容摘要，解压过的不重复解压。能力清单里的模型指纹由 beam_size、实际使用的设备和计算类型、每个模型包的语言对、版本和内容摘要计算得出。
- 请求按到达顺序处理。收到 `cancel` 时，按请求号丢弃还没开始的请求。
- 非英语语言对按 2.5 节的方式经英语中转。第一跳的结果不发给主进程，直接送入第二跳。

#### 8.3.1 显卡运行库

CTranslate2 的 Windows 版按 CUDA 12 编译，CUDA 运行时静态链接在 `ctranslate2.dll` 里，显卡推理只额外需要 NVIDIA cuBLAS 12，也就是 `cublas64_12.dll` 和它依赖的 `cublasLt64_12.dll`。这两个文件合计约 770 MB，安装包不带，用户在设置的“模型”页按需下载（`MOD-06`）。

下载内容是 PyPI 上 NVIDIA 发布的 `nvidia-cublas-cu12` wheel，版本固定为 12.9.2.10，约 553 MB：

- **版本选择**：CUDA 12 的各个小版本对驱动的要求相同，12.8 起支持 RTX 50 系列，12.9 的 wheel 又比 12.8 小。
- **校验**：wheel 的大小与 SHA-256 写死在代码里，取自 PyPI 的 JSON 接口。下载时边写边算摘要，与预期不符就不解压。
- **解压**：wheel 是 zip 文件，`services.nmt_runtime` 只取出两个 DLL 和 NVIDIA 的许可证，并按 wheel 自带的 RECORD 再核对一次两个 DLL 的摘要。解压完成后删除 wheel。

文件放在模型目录的 `runtimes/cublas12` 下，目录布局是：

- `<版本>/bin/`：两个 DLL；
- `<版本>/License.txt`：NVIDIA 的许可证；
- `<版本>/installed.json`：说明文件，最后写入，记录版本和各文件的大小。

解压先写到 `<版本>.tmp`，完成后原子改名。没有说明文件或文件大小与说明不符的目录，按未装好处理。

NMT 进程按完整路径加载 cuBLAS，不按 PATH 搜索：

- **加载顺序**：组合根把已装版本的 `bin` 目录作为 `cuda_dll_dir` 写进 NMT 进程的设置。进程在选择设备时，依次在 `cuda_dll_dir` 和模块搜索路径里的 `nvidia/cublas/bin` 中，找第一个两个 DLL 都在的目录，先加载 `cublasLt64_12.dll`，再加载 `cublas64_12.dll`。后者源码运行时来自开发环境。CTranslate2 之后按同名取 DLL，取到的就是已加载的模块。
- **不按 PATH 搜索的原因**：本机实测，从某些终端启动的进程会继承“重分析点信任”（RedirectionTrust）强制策略。在这种策略下，按名称搜索 DLL 时只要遇到 PATH 上一个由普通用户创建的目录联接（例如 `...\OpenAI\Codex\bin`），加载就直接以 WinError 448 失败。PATH 上的其他 CUDA 版本也可能被误用。
- **设备选择**：`[nmt].device` 为 `auto` 时，有 CUDA 设备且 cuBLAS 能加载就用显卡，否则用 CPU 并记一条警告；为 `cuda` 时，cuBLAS 加载失败，进程以依赖错误（65）退出。

运行库装好或删除后，模块发布 `NmtRuntimeChanged`，组合根按最新配置重新决定 NMT 进程，进程的设置随之多出或去掉 `cuda_dll_dir`。删除前组合根先停掉 NMT 进程，否则 DLL 仍被加载，删除会失败。NMT 进程就绪时报告实际使用的设备（`Ready.versions["device"]`），监管器保存这份信息，组合根把它放进 `WorkerStatus.versions`。设置页据此显示“当前在显卡上运行”或“当前在 CPU 上运行”。

本机实测，RTX 5080 用显卡、float16，200 字中译英约 80 毫秒，200 词英译中约 110 毫秒；CPU、int8 分别约 160 毫秒和 250 毫秒。

### 8.4 OCR 进程

OCR 进程的后端由当前 OCR 引擎决定（`OCRE-01`，引擎的安装与选择见 8.6 节）：

| 引擎 | 模式 | 后端 | 运行环境 | 输出 |
|---|---|---|---|---|
| Windows 自带 OCR（默认） | 只有文本模式 | `winocr_backend.WinOcrBackend`，经 pywinrt 调用 Windows.Media.Ocr | 主环境 | 一个覆盖整张截图的正文块，lines 为每一行及其位置 |
| RapidOCR | 只有文本模式 | `rapid_backend.RapidBackend`，rapidocr 3.10 经 onnxruntime 运行 PP-OCR 模型 | RapidOCR 运行环境 | 同 Windows 引擎 |
| PaddleOCR | 版面模式 | PP-StructureV3 | PaddleOCR 运行环境 | 完整版面块 |
| PaddleOCR | 文本模式 | PaddleOCR 检测加识别产线 | PaddleOCR 运行环境 | 同 Windows 引擎 |

全部引擎遵守同一条位置约定（`OCR-10`）：能给出文字位置的引擎按行返回各自的 bbox；只返回文字的引擎由 `plain_text.plain_text_blocks` 把整张截图作为一个 bbox 输出单个正文块，覆盖窗口把译文铺满选区。

Windows 引擎的要点：

- 像素转成 Bgra8 的 SoftwareBitmap 交给系统识别；长边超过 `resize_max_side` 与系统上限（本机 10000）中较小者时，先用面积平均等比缩小，坐标再换算回原图。
- Windows 不给置信度，行的 score 记为 1.0。系统返回的中文行在汉字之间插了空格，所以行文字按词重新拼接：汉字、假名、全角标点之间直接相连，拉丁词之间加空格。
- 一个识别器只认一种语言。识别语言为“自动”时用用户配置文件语言创建识别器，失败再取第一个可用语言；指定语言时映射到系统语言（例如 zh-Hans 对应 zh-Hans-CN），系统没有该语言时以配置错误退出。本机实测“自动”选到简体中文时，英文单词会识别错，混排场景需要用户指定语言或改用 PaddleOCR。
- 等待异步结果用 `asyncio.run`：Qt 初始化过 COM 的线程是单线程套间，在那里调用 `IAsyncOperation.get()` 实测会抛异常。
- 实测：1920×1080、30 行中英混排的截图识别约 0.33–0.37 秒，30 行全部识别出来。

RapidOCR 引擎的要点（`lst/workers/ocr_worker/rapid_backend.py`）：

- 主进程在 `configure` 里给出各环节选用的模型文件的完整路径（检测、识别、文本行方向三个），路径由 `services.ocr_engines` 按 `[ocr_rapid].<环节>_model` 算出。RapidOCR 在模型路径为空时会按它自带的地址表下载模型，这里总是显式给出路径，onnxruntime 直接加载本地文件；文件缺失时以配置错误（66）退出，列出缺少的文件。识别模型的字表来自 onnx 元数据，不读字典文件。
- 运行环境的版本决定 onnxruntime 带哪个加速：`onnxruntime-gpu` 提供 CUDAExecutionProvider，`onnxruntime-directml` 提供 DmlExecutionProvider，`onnxruntime` 只有 CPU。推理设备为 `cpu` 时总用 CPU；为 `auto` 时运行环境带加速就用（CUDA 优先于 DirectML），本机不可用时退回 CPU 并记 WARNING；为 `gpu` 时要求加速，运行环境是 CPU 版或加速在本机不可用，进程以依赖错误（65）退出。
- 用 CUDA 时先调用 `onnxruntime.preload_dlls()`：CUDA 13 与 cuDNN 9 运行库装在运行环境的 `nvidia` 目录下，不在 DLL 搜索路径上。RapidOCR 在加速不可用时自己退回 CPU，只记一条警告，所以后端在预热之后读取检测会话实际使用的执行器核对。
- 识别语言不改变加载的模型，只用来核对选用的识别模型是否覆盖它：“自动”总可以，其他语言不在模型的覆盖范围里时以配置错误退出。
- RapidOCR 只接受 `text_score`、`box_thresh`、`unclip_ratio` 与 `use_cls` 几个逐次识别的参数，其余参数只能在创建识别器时给出，所以 `[ocr_rapid]` 里这些参数都标了重载（8.6.4 节）。
- 输出只取行级位置：每行的四点多边形取轴对齐包围盒，换算回原始截图，左上角向下取整、右下角向上取整。RapidOCR 能给出词级与字级的框，但版面块格式里的文本行只有 bbox、text、score 三个字段，所以不请求它们。

识别模式只对 PaddleOCR 有效。`OcrConfig.mode` 保留用户的选择，`effective_mode()` 在当前引擎是 Windows 自带 OCR 或 RapidOCR 时一律给出文本模式，请求、处理时限与结果转换都按它走；切回 PaddleOCR 时恢复原来的模式。

PaddleOCR 输出格式的细节只在 OCR 进程内部出现。进程在返回结果前，把输出转换成 `contracts` 中定义的版面块：

- 版面类别按下表映射到需求 `OCR-01` 的类别。映射表覆盖 PP-DocLayout_plus-L 的 20 类、PP-DocLayout-L/S 的 23 类和解析阶段改写出的标签；有一个测试直接读本机模型的 `inference.yml`，检查每个标签都有映射。

  | 需求类别 | PP-StructureV3 标签（节选） |
  |---|---|
  | 标题 | `doc_title`、`paragraph_title` |
  | 正文 | `text` 及其他含文字的标签 |
  | 表格 | `table` |
  | 公式 | `formula` |
  | 图表 | `chart` |
  | 图片 | `image` 及其他不含文字的标签 |
  | 页眉页脚 | `header`、`footer`、`number`（页码；最初按文档写作 `page_number`，两个都映射） |

  `header_image`、`footer_image` 归图片；`figure_title` 等图表标题归正文；未知标签有文字归正文、没有文字归图片，每个标签只记一次 WARNING。PaddleX 会改写部分标签：唯一的 `paragraph_title` 改成 `doc_title`，正文之上的 `footnote` 改成 `text`。
- 不落在任何版面块里的文本行也输出：PaddleX 默认丢掉这些行，而界面截图里的按钮、菜单文字常常是这种情况，丢掉就会漏翻。每行单独输出成一个正文块，排在全部版面块之后。
- 表格方向分类只在开了文档方向校正时启用。PaddleX 默认开着它，实测会把正放的两行三列表判成旋转 180 度，行列整个倒过来。

- 表格的 `pred_html` 用标准库 `html.parser` 解析成单元格网格（行列号、跨行跨列数），并与 `cell_box_list` 对齐。
- 坐标统一换算为相对原始截图左上角的物理像素坐标：缩放按两个轴各自的比例换算；文档方向校正的 90、180、270 度旋转用逆变换换算；去畸变只能按图像尺寸比例换算，结果是近似值。

截图预处理（`CAP-05`）也在 OCR 进程里做，主线程不做图像处理。

OCR 子能力的开关和预处理参数随每个 `ocr` 请求的 options 发送，OCR 进程以请求里的值为准，改动从下一次识别起生效，不需要重启进程。`configure` 里的同名字段只决定启动时预加载哪些模型；请求用到还没加载的子能力时，OCR 进程当场带上它重建产线，实测补加载公式模型加一次识别约 25 秒，在处理时限之内。组合根按 `[ocr]` 节与当前引擎的流水线配置节启动 OCR 进程，何时重载见 8.6.5 节；子能力开关、预处理参数与流水线配置里没有标重载的参数随请求发送，变化时不重载。

模型目录与联网：组合根把模型缓存目录（模型目录下的 `ocr`）作为 `model_cache_dir` 写进 `configure`（源码版在那里没有模型时回退到旧项目的 `assets/PaddleOCR_and_PP-Structure`）。OCR 进程给每个要用的模型显式写上 `model_dir = <模型缓存>/official_models/<模型名>`，PaddleX 拿到目录就直接加载，不经过下载逻辑；有模型缺失时以配置错误（退出码 66）退出，列出全部缺失的模型。进程入口还用审计钩子拦截一切对非回环地址的解析和连接（`network_guard.py`），作为 `PRV-01` 的兜底。

OCR 进程本身从不下载。RapidOCR 与 PaddleOCR 的运行环境与模型由主进程的 `services.ocr_engines` 在用户点“下载”后安装（8.6 节，`MOD-02`、`OCRE-02`），OCR 进程的网络拦截因此可以一直开着。当前引擎不可用（Windows 没有 OCR 语言、RapidOCR 或 PaddleOCR 未安装）时，组合根不启动 OCR 进程。

推理设备设为 GPU 时，安装的 PaddlePaddle 没有编译 CUDA，或编译了 CUDA 但没有可用的 GPU（没有显卡、驱动太旧），OCR 进程都以依赖错误（65）退出：两种情况缺的都是推理依赖的能力，界面显示为 `ocr_engine.runtime_mismatch`。导入 paddle 时会短暂运行一次 `where ccache`，识别期间没有常驻子进程（8.2.3 节要求确认的事项）。

OCR 进程严格串行处理请求。新截图到达时，services.ocr 先取消旧请求，再立即提交新请求（`OCR-06`）：旧请求还没开始就被丢弃，已经开始就照常跑完、结果由主进程丢弃。监管器重载或停止使当前请求以 `cancelled` 结束时，services.ocr 自动重新提交一次，新请求在监管器里排队等新进程；同一会话再次被取消时发布 `OcrFailed(engine.worker_crashed)`，覆盖窗口不会一直停在“识别中”。

识别结果的版面块格式写在 `lst/services/ocr/blocks.py` 的模块说明里。坐标是相对**原始**截图的物理像素，OCR 进程缩放过图像时要换算回原图。文本模式下 OCR 进程只返回一个覆盖整张截图的 text 块，结构归一化与段落合并在主进程里做（8.7 节），覆盖窗口按段的范围（`OcrSegment.bbox`）而不是块的范围渲染译文。

单次识别的失败沿用 `engine.*` 错误码（`engine.timeout`、`engine.worker_crashed`、`engine.internal`），随 `OcrFailed` 显示在覆盖窗口里。OCR 进程本身进入 FAILED 的原因另用 `ocr_engine.*` 错误码，显示在设置的引擎卡片上（8.6.5 节、10.3 节）。识别各阶段的耗时（排队、识别、转换）只写 INFO 日志，还没有进入统计（`OBS-02`）。

### 8.5 模型管理

`services.models` 负责本地模型（`MOD-01` 至 `MOD-05`）：

- NMT 模型：读取 Argos 的模型索引，在 `ModelCatalog` 状态里列出可下载的语言对和已安装的模型。下载、校验、安装的流程见 8.5.1 节。
- 词典包：读取词典索引，与 NMT 模型列在同一个 `ModelCatalog` 里（`ModelKind.DICT`），流程见 8.5.2 节。
- OCR 模型：不归这个模块管。它们随 OCR 引擎由 `services.ocr_engines` 下载（8.6 节，`MOD-02`），OCR 进程本身从不下载。
- 本地 LLM：通过 Ollama 和 llama.cpp server 的模型列表接口读取已有模型，`services.models` 不负责下载（`MOD-03`）。
- 模型目录来自配置的 `[storage].models_dir`，默认是数据目录下的 `models`（2.9.5 节，`MOD-04`）。NMT 模型在其中的 `nmt`，词典包在 `dict`；PaddleOCR 的 OCR 进程启动时，`PADDLE_PDX_CACHE_HOME` 指向其中的 `ocr`。运行中更换模型目录的过程见 8.5.3 节。

#### 8.5.1 NMT 模型的索引与下载

模型索引的地址是 `https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json`，与 argostranslate 自己的默认值相同（它的 `settings.py` 用 `ARGOS_PACKAGE_INDEX` 环境变量覆盖，默认指向这个仓库）。索引是 JSON 数组，每项描述一个模型包。本程序用到其中四个字段：

| 字段 | 含义 | 例子 |
|---|---|---|
| `from_code`、`to_code` | Argos 语言码，`zh` 是简体中文，`zt` 是繁体中文 | `en`、`zh` |
| `package_version` | 模型包版本 | `1.9` |
| `links` | 下载地址列表，取第一个 http(s) 地址 | `https://argos-net.com/v1/translate-en_zh-1_9.argosmodel` |

每项另有 `argos_version`、`from_name`、`to_name`、`code` 四个字段，本程序不用。索引里没有包的大小，界面在下载开始前显示“大小未知”，下载开始后用响应的 `Content-Length` 补上。核实时（2026 年 10 月）索引共 100 项，涉及中文的有 `zh→en`、`zt→en`、`en→zh`、`en→zt` 四个包。

模型 id 按语言对生成，形如 `argos:zh-Hans:en`。NMT 进程对每个语言对只用最高版本，所以一个语言对只对应一个模型条目。

联网时机遵守 `PRV-01`：

- 模块启动时只扫描本地模型，不联网；
- 只有收到 `RefreshModelCatalog` 命令才获取索引。首次运行引导显示时，如果本次运行还没取到过索引，自动发送一次；获取失败后不自动重试，由用户点“重试”；
- 只有用户点“下载”或“重试”时才下载模型包。

下载与安装的步骤：

1. 同一时间只下载一个模型。每个模型在自己的任务里下载，取消其中一个不影响队列里后面的。
2. 数据流式写入模型目录下的 `<名>.part`，每攒满 1 MB 交给线程写盘一次。进度每 200 毫秒最多发布一次，状态切换（排队、下载、校验、完成、失败、取消）总是立即发布。
3. 校验四项：zip 能读；CRC 无误；包内成员符合 NMT 进程的要求（唯一的 `metadata.json`，加上 `sentencepiece.model` 和 `model/model.bin`）；语言对与索引一致。
4. 校验通过后用 `os.replace` 原子改名为 `.argosmodel`，再删除同一语言对的旧版本包，然后发布 `ModelsInstalled`。已经进入改名阶段时，取消命令被忽略。
5. 模型包原样放进模型目录，不在这里解压。NMT 进程在第一次用到某个模型时解压到 `.extracted/`（8.3 节），解压只发生在一个地方。解压目录名带包内容的摘要，NMT 进程每次加载时删除不对应任何现有模型包的解压目录和崩溃留下的临时目录，升级或删除模型后旧的解压结果不会一直占用磁盘。

组合根收到 `ModelsInstalled` 后让监管器重载 NMT 进程，新进程重新扫描模型目录；协议里没有单独的“重新扫描”消息，重启进程只要约 1 秒。连续装好几个模型时合并重载：重载进行中又有模型变化，就在这次结束后再重载一次。删除模型同样发布 `ModelsInstalled`，用 `removed` 字段区分。

“已安装”的判定与 NMT 进程一致：NMT 进程会跳过的包，这里也不算已安装，目录里显示的状态因此与 NMT 进程实际能用的模型一致。同一语言对已有相同或更高版本时，下载命令不入队、不联网（`INS-07`）。模块启动时删除上次中断留下的 `.part` 文件。日志和错误详情里的地址都去掉查询参数、片段和用户信息。

模型管理模块的目录必须与 NMT 进程扫描的目录相同。源码版在 `<模型目录>/nmt` 没有模型时回退到旧项目的 `assets/argosmodel`，组合根把同一个解析结果交给两边；两边用了不同目录的话，第一次下载之后 NMT 进程会切到 `<模型目录>/nmt`，旧目录里的模型就从目录里消失了。`[nmt].model_dir`（本地翻译模型的单独目录，8.5.3 节）在运行中改变时，组合根停掉旧的模型管理模块，再启动一个指向新目录的实例（`MOD-04`）。

索引无法事先说明一个包用的是哪种分词器。不是 SentencePiece 分词的包要下载完才会以 `models.invalid_package` 失败。

#### 8.5.2 词典包的索引与下载

词典索引的地址是 `https://github.com/willlllliams/thunderbird-translate/releases/download/dicts-v1/dict-index.json`，包文件与索引在同一个发布标签下，下载地址把索引文件名换成包文件名。索引里每个包带语言对、文件名、压缩后与解压后的字节数、词条数、变形数、压缩文件的 SHA-256、数据源名称、许可证（SPDX 标识）与署名全文。

词典索引与 Argos 索引在收到同一条 `RefreshModelCatalog` 时同时获取，各自记录结果，一个失败不影响另一个；联网时机与 NMT 模型相同（`PRV-01`）。词典包与 NMT 模型共用一个下载队列，同一时间只下载一个。

下载与安装的步骤：

1. 数据流式写入词典目录下的 `dict-<src>-<tgt>.tsv.gz.part`，边写边计算 SHA-256；
2. 校验三项：摘要与索引一致；整个文件能解压（gzip 自带的 CRC 能发现损坏），内容是 UTF-8；包头的语言对与索引一致。摘要不符或不是词典包记为 `models.invalid_package`，语言对不符记为 `models.pair_mismatch`；
3. 写说明文件 `<包文件名>.json`，记下安装时索引给出的数据源、许可证、署名和 SHA-256；
4. 用 `os.replace` 原子改名为正式文件，替换旧版本，然后发布 `ModelsInstalled`。

词典目录里每个语言对一个文件，文件名固定，所以没有“同一语言对的多个版本”：安装新版本就是替换旧文件。已安装的包与索引的 SHA-256 不同时，设置的模型页显示“发布页上有更新的版本”。模块启动时删除上次中断留下的 `.part` 文件。

说明文件的作用是在没有联网获取索引时仍然显示署名。CC BY-SA 4.0 要求署名可见，而程序启动时不联网，只能从本地取得署名；说明文件缺失时（例如手工放进目录的包），署名用包头里的数据源名称和许可证代替。

词典数据的许可证与署名：

| 词典包 | 数据源 | 许可证 |
|---|---|---|
| 英语→简体中文 | ECDICT | MIT |
| 其余 21 个包 | Wiktionary（由 wiktextract 抽取、kaikki.org 发布） | CC BY-SA 4.0 |

两类数据都经过抽取、筛选和裁剪的改编。署名显示在三处：

- 设置的“模型”页，词典列表上方有一段总署名，许可证名链接到许可证全文，每个包的下一行是它的署名全文；
- 首次运行引导里每个词典包的行下；
- 仓库根目录的 `THIRD-PARTY-NOTICES`，写明数据来源、改编方式与许可证，`licenses/` 下是 ECDICT 的许可证全文。构建脚本把两者复制到安装目录的 `app\` 下（15.1 节），界面上的总署名指向这个文件。

组合根收到只涉及词典包的 `ModelsInstalled` 时不重载 NMT 进程，字典引擎按词典目录的修改时间自己发现变化（2.7.4 节）。

#### 8.5.3 运行中更换模型目录

模型目录（2.9.5 节）在设置里更改后立即生效，不需要重启程序（`CFG-07`）。用到它的有五处：NMT 进程、OCR 进程、模型管理模块（NMT 模型与词典包）、OCR 引擎模块（运行环境与 OCR 模型）、显卡运行库模块，另外字典引擎从词典目录读包。前三个模块在构造时拿到目录，运行中不能改目录；两个推理进程加载着目录里的模型文件和 DLL，Windows 上文件被占用时改名和删除都会失败。所以更换分三段：先全部停下，再移动文件，最后按新目录全部重建。

`ChangeModelsDir` 由存储位置模块处理，组合根实现它要的四个操作（`storage_module.StorageHost`）：

1. 检查：新目录是绝对路径，不在当前模型目录里面（否则会把目录移进它自己），写入检查通过（2.9.5 节）。要移动时，新目录里不能有同名的非空子目录；不在同一个卷上时，可用空间要放得下全部子目录。这一步不停任何东西，检查不通过时程序照常运行。
2. 停下（`suspend_models`，在组合根的决策锁里）：停 NMT 与 OCR 进程，停模型管理、OCR 引擎、显卡运行库三个模块，进行中的下载与安装随之取消，半成品按各模块的规则删除。组合根进入“暂停”状态：期间到达的配置变化只更新界面语言与主题，`ModelsInstalled`、`RestartWorker`、引擎目录的变化都不启动进程。
3. 移动（`storage.ModelsMove`，在线程里）：逐个处理 `nmt`、`ocr`、`dict`、`engines`、`runtimes` 五个子目录。新旧目录在同一个卷上时用 `os.replace` 改名，瞬间完成；不在同一个卷上时复制，进度按字节报告。任何一步失败都回滚：改过名的改回去，复制出来的删掉。
4. 写配置（`save_models_dir`）：组合根调用配置服务的 `update`，把新目录写进 `[storage].models_dir`（新目录是默认位置时写空串），等它落盘再返回。经总线发 `UpdateConfig` 不行，因为命令没有返回值，这里必须知道写没写进去：写不进去时把模型移回原处，否则下次启动时配置还指着旧目录，旧目录却已经空了。
5. 提交：跨卷复制的子目录这时才删除旧副本。删不掉的（例如被杀毒软件占用）只记警告，不影响结果。
6. 恢复（`resume_models`，在决策锁里）：按新目录（失败时按原目录）新建三个模块并启动，替换它们在启动记录里的位置，退出时仍按启动的逆序停止；引擎模块换词典目录（`EnginesModule.set_dict_dir`），只重建字典翻译实例；然后清除进程的去重键，按总线上最新的配置重新决定 NMT 与 OCR 进程。停下之前已经取到过模型索引的，再发一次 `RefreshModelCatalog`，新的模型管理模块启动时只扫描本地模型。

不移动现有模型时跳过第 3、5 步：新目录里已有模型就直接用，旧目录里的文件留在原处。手工改了配置文件里的 `[storage].models_dir` 时，组合根在处理 `AppConfig` 时发现它与当前目录不同，同样执行第 2、6 步，不移动文件。

`[nmt].model_dir` 是本地翻译模型的单独目录，留空时用模型目录下的 `nmt`。保留这个字段而不并入模型目录，依据有两条：已经指向别处 `.argosmodel` 文件夹的旧配置不需要迁移，继续有效；有人想直接用 argos-translate 自己下载的模型文件夹，单独目录正好满足。两个设置管的范围不同，不会出现两处设置同一件事的情况：模型目录管全部五类内容，单独目录只替换其中的 NMT 模型包。更改模型目录时不移动单独目录里的文件，那是用户自己管理的目录。设置里单独目录的输入框留空时，占位文字显示实际使用的 `<模型目录>\nmt`。NMT 进程的解压目录与 CUDA 编译缓存始终在 `<模型目录>/nmt` 下，与单独目录无关。

### 8.6 OCR 引擎的安装与选择

`services.ocr_engines` 发布 `OcrEngineCatalog`（契约见 `lst/contracts/ocr_engines.py`），处理六个命令：`InstallOcrEngine`、`CancelOcrEngineInstall`、`UninstallOcrEngine` 管引擎，`DownloadOcrModel`、`CancelOcrModelDownload`、`DeleteOcrModel` 管流水线里的单个模型（`OCRE-01` 至 `OCRE-09`）。

#### 8.6.1 三个引擎

目录里的三个引擎按 `OcrEngineKind` 的定义顺序排列：

| 引擎 | 安装版 | 源码版 |
|---|---|---|
| `windows` | 不需要下载。模块启动时用 pywinrt 读取系统的 OCR 识别语言，一个都没有时状态为“不可用”，错误码 `ocr_engine.no_language`，设置里提示在 Windows 设置的“语言”中添加 OCR 功能；对它发“安装”命令会重新读取一次语言 | 同安装版 |
| `rapid` | 按需下载运行环境，默认模型在 rapidocr 的 wheel 里，随运行环境就位；其余模型单独下载 | `.venv-ocr` 里装了 rapidocr、某个 onnxruntime 发行包，并且有自带模型，就算已安装；另下载的模型放在模型目录，与安装版相同 |
| `paddle` | 按需下载运行环境与默认模型，其余模型单独下载 | `.venv-ocr` 存在且某个模型缓存的 `official_models` 下有模型，就算已安装；模型属于开发环境，不提供下载与删除 |

源码版的安装与卸载命令一律忽略并记 INFO，模块从不改动开发环境。RapidOCR 与 PaddleOCR 共用 `installer.py` 的通用流程，子类只补充自己的部分：开发环境的检查、模型目录与下载方式、运行环境的完整性检查、卸载时多删的目录。两个引擎各有自己的引擎目录、安装任务与忙碌标志，可以同时安装。

#### 8.6.2 运行环境版本与安装

每个引擎声明可装的运行环境版本（`OcrRuntimeVariant`，`OCRE-07`）：

| 引擎 | 版本 | 推理库 | 另需 |
|---|---|---|---|
| PaddleOCR | `cpu` | paddlepaddle 3.3.1 | 无 |
| PaddleOCR | `cuda` | paddlepaddle-gpu 3.3.1，Paddle 官方 cu129 源 | CUDA 12.9 运行库与 cuDNN 随运行环境安装；驱动支持 CUDA 12.9（R575 及以上） |
| RapidOCR | `cpu` | onnxruntime 1.31.0 | 无 |
| RapidOCR | `directml` | onnxruntime-directml 1.24.4 | sympy；支持 DirectX 12 的显卡 |
| RapidOCR | `cuda` | onnxruntime-gpu 1.31.0 | CUDA 13 与 cuDNN 9 运行库随运行环境安装；驱动支持 CUDA 13（R580 及以上） |

PaddleOCR 的 CUDA 版选 cu129 而不选 cu126，是因为 CUDA 12.6 不支持 RTX 50 系列（Blackwell 架构要求 CUDA 12.8 及以上）。onnxruntime-directml 在 PyPI 上 cp313 的 Windows wheel 最高只到 1.24.4，所以 DirectML 版的推理库版本低于另外两个版本。

每个版本一份版本清单，文件名是 `app\ocr-<引擎>-<版本>-requirements.txt`，由构建脚本生成（15.6 节）：

- PaddleOCR CPU 版冻结 `.venv-ocr` 的全部包；RapidOCR CPU 版只含 rapidocr、onnxruntime 及其依赖的闭包；
- GPU 版在 CPU 版清单上只替换推理库：paddlepaddle 换成 cu129 源里同版本 paddlepaddle-gpu 的 wheel 直接地址，onnxruntime 换成 onnxruntime-directml 或 `onnxruntime-gpu[cuda,cudnn]`，再用 `uv pip compile` 以其余各包的精确版本为准补上新推理库的依赖；
- 全部清单再补上每个包的 SHA-256。

安装目录里缺某个版本的清单时（构建时无法生成），这个版本不出现在 `OcrEngineInfo.variants` 里。

安装分两步，都在 DOWNLOADING 状态下进行，可以取消：

1. 运行环境：用安装包自带的 `tools\uv.exe` 执行 `uv pip install --no-config --python <安装目录>\runtime\python.exe --target <site.part> --no-deps --no-build -r <版本清单>`，清单带哈希时加 `--require-hashes`；成功后写入运行环境标记，用 `os.replace` 原子改名为 `<模型目录>\engines\<引擎>\site`，删除 uv 缓存。`--no-build` 让 uv 只装预编译的 wheel，用户机器上不执行任何包的构建脚本；
2. 模型：默认组合里还没有就位、也不随运行环境自带的模型，逐个下载（8.6.4 节）。

全部完成后进入 INSTALLING，复查运行环境与默认组合，最后写安装标记 `installed.json`（版本清单的 SHA-256、已装的版本、默认组合的模型），状态变为 INSTALLED，发布 `OcrEnginesChanged`。`InstallOcrEngine` 的 `variant` 为空时装推荐版本，推荐版本不可装时装 CPU 版。

换版本：对已装好的引擎发另一个版本的安装命令，按上面的流程只重装运行环境。不同版本的清单不同，运行环境标记对不上，必然重装；模型在 `site` 之外，已经就位，不再下载。替换已有的 `site` 之前，模块调用组合根注入的 `before_runtime_update`，组合根先停掉正在使用旧运行环境的 OCR 进程，否则 DLL 被占用，删除会失败。换版本途中取消时旧运行环境原样保留，状态回到 INSTALLED（旧版本）；失败时状态为 ERROR，旧运行环境仍然可以运行，OCR 照常使用旧版本。

其余规则：

- 启动时安装标记与运行环境标记都要等于已装版本当前清单的 SHA-256 才算 INSTALLED。程序升级带来新清单时报“未安装”，并说明需要更新运行环境、已下载的模型保留，用户点“安装”只重装运行环境；
- 进度覆盖全部步骤，权重按字节数：运行环境取该版本装好后的大小，每个模型取它的大小。uv 不输出机器可读的进度，所以运行环境阶段每秒统计一次 uv 缓存目录的大小作为估计，封顶为这一段的 98%，uv 成功后补满。进度只增不减，最短发布间隔 0.2 秒；
- 取消或失败时删除半成品（`site.part`、uv 缓存、子类的临时文件），已经完整装好的部分保留，重试时复用；
- 卸载删除运行环境与全部模型。卸载开始时同步置忙标志，卸载期间的安装命令与重复的卸载命令被忽略；INSTALLING 阶段的取消命令被忽略。卸载当前引擎前，组合根先停 OCR 进程；卸载后 `[ocr].engine` 不变。选用的引擎不能运行时，组合根改用 Windows 自带 OCR 启动 OCR 进程，并记一条 WARNING；引擎目录再次变化（例如选用的引擎装好）时重新决定，换回选用的引擎。Windows 自带 OCR 也不能运行时，截屏识别不可用，直到用户另选一个引擎（`OCRE-04`）。

#### 8.6.3 显卡检测与推荐版本

模块启动时在线程里检测一次显卡（`gpu.py`），据此给出每个引擎的 `recommended_variant`。检测用 DXGI（DirectX Graphics Infrastructure）：

1. `CreateDXGIFactory1` 创建 IDXGIFactory1，`EnumAdapters1` 逐个取出适配器，`GetDesc1` 读出名称、厂商号（VendorId）、设备号、专用显存与标志位；
2. 带 `DXGI_ADAPTER_FLAG_SOFTWARE` 标志的适配器与 Microsoft Basic Render Driver（VendorId 0x1414）是软件渲染，不算显卡；同一块显卡以多个适配器出现时（混合显卡、远程桌面）按 LUID 去重；
3. 是否支持 DirectX 12 用 `D3D12CreateDevice(adapter, D3D_FEATURE_LEVEL_11_0, IID_ID3D12Device, NULL)` 判断。输出参数为 NULL 时这个函数只检查能否创建设备，不真正创建，返回 S_FALSE 表示可以。DirectML 要求的就是能创建特性级别不低于 11_0 的 D3D12 设备。

COM 接口用 ctypes 按虚函数表的位置调用，整个过程只调用本机的 `dxgi.dll` 与 `d3d12.dll`，不联网、不读写注册表。任何一步失败都按“没有显卡”处理，推荐 CPU 版。

推荐规则依次判断：

1. 有 NVIDIA 显卡（VendorId 0x10DE），并且引擎有 CUDA 版：推荐 CUDA；
2. 有支持 DirectX 12 的显卡，并且引擎有 DirectML 版：推荐 DirectML；
3. 否则推荐 CPU。

检测只说明硬件存在。驱动是否支持所选版本要求的 CUDA，要到 OCR 进程加载时才知道；不支持时进程以依赖错误退出，引擎卡片显示 `ocr_engine.runtime_mismatch`（8.6.5 节）。设置里的版本下拉框标出推荐版本，下面写出推荐理由，例如“推荐：CUDA（检测到 NVIDIA 显卡）”。

#### 8.6.4 流水线模型与配置

每个引擎的识别流水线由若干环节组成（`OcrStage`）：文字检测、文字识别、文本行方向、文档方向、去畸变、版面、区域、表格、公式、图表。Windows 自带 OCR 没有可选模型；RapidOCR 用检测、识别、文本行方向三个环节；PaddleOCR 用全部十个环节。每个环节可以在多个模型之间选择（`OCRE-08`），模型目录写在 `catalog.py`：

- PaddleOCR 的模型 id 是 PaddleX 的模型名。默认组合是版面模式加表格识别所需的模型，另带体积小的文档方向、去畸变、文本行方向模型，合计约 968 MB；公式（PP-FormulaNet_plus-L，约 731 MB）与图表（PP-Chart2Table，约 1.4 GB）默认不下载。表格识别同时用到 5 个模型，其中只有无线表格的结构模型可选（默认 SLANet_plus），另外 4 个（有线表格结构、表格分类、两个单元格检测模型）是附属模型，随引擎安装，不列在模型列表里，也不能单独删除；
- RapidOCR 的模型 id 写作 `<OCR 版本>/<规格>/<语言>/<任务>`，取值组合、下载地址与 SHA-256 抄录自 rapidocr 3.10.0 的 `default_models.yaml`。默认组合是 wheel 自带的 PP-OCRv6 small 检测与识别模型和 PP-OCR mobile v2.0 方向分类模型；PP-OCRv6 的检测与识别还有 tiny、medium 两档，PP-OCRv5 有 mobile、server 与按文字系统划分的专用识别模型。PP-OCRv5 的文本行方向模型输入尺寸是 80×160，rapidocr 3.10 按 48×192 预处理，换上它会得到错误的结果，所以文本行方向环节只提供自带的模型。

模型的下载方式：

| 引擎 | 下载 | 校验 | 落位 |
|---|---|---|---|
| PaddleOCR | 从 PaddleX 官方地址流式下载 `<模型名>_infer.tar` 到 `official_models\.<模型名>_infer.tar.part` | 长度与响应头的 Content-Length 一致；解压前逐个成员检查，拒绝绝对路径、`..`、盘符、链接与设备文件；解压后检查模型目录结构 | 原子改名为 `<模型缓存>\official_models\<模型名>` |
| RapidOCR | 单个 `.onnx` 文件，流式下载到 `<模型目录>\ocr\rapid\.<文件名>.part` | 长度一致；SHA-256 与 `default_models.yaml` 记录的一致 | 原子改名为 `<文件名>` |

下载中的文件以点开头，PaddleX 不会把它当成模型目录。校验不通过按无效包处理，删除半成品。不同模型可以同时下载；引擎在安装中时忽略模型下载命令。

`DeleteOcrModel` 拒绝删除以下四种模型，记一条 INFO：

1. 配置正在选用的模型。界面删除选用中的模型时先确认，把该环节改回默认模型（`UpdateConfig`），再发删除命令；
2. 随运行环境自带的模型（RapidOCR 的默认组合），它们在 `site` 里，删除会让运行环境不完整；
3. 附属模型；
4. 正在下载的模型。

模型状态按“下载任务 → 最近一次错误 → 是否就位”给出：DOWNLOADING、QUEUED 带进度，ERROR 带错误码，就位为 DOWNLOADED，否则 NOT_DOWNLOADED。

各环节选用哪个模型、流水线参数是什么，写在各引擎自己的配置节里（`lst/services/ocr/config.py`）：`[ocr_windows]`、`[ocr_rapid]`、`[ocr_paddle]`，结构归一化另有 `[ocr_normalize]`（8.7 节）。`[ocr_windows]` 目前没有字段，系统接口不提供可调参数，保留这一节是为了让三个引擎的配置结构一致。设置窗口按字段的元数据自动生成界面，各节遵守下面的约定：

1. 选择环节模型的字段名是 `<环节>_model`（例如 `detection_model`、`layout_model`），默认值是该环节默认模型的 id，`json_schema_extra` 里带 `{"ocr_stage": "<环节>", "reload": true}`。取值不做枚举校验，因为模型目录随引擎版本变化，界面从 `OcrEngineInfo.models` 里取可选项；OCR 进程加载时发现模型不存在，以配置错误退出；
2. 参数字段都有中文的 `title` 与 `description`，数值范围用 `ge`、`le` 写出，取值集合用 `Literal` 写出；界面据此标明范围，“恢复默认”取字段的默认值；
3. 不常用的参数带 `{"advanced": true}`，界面把它们放进“高级参数”折叠区；
4. 需要重载 OCR 进程的字段带 `{"reload": true}`：模型选择、线程数，以及只能在创建识别器时给出的参数。其余参数随每次识别请求发送，改动从下一次识别起生效；
5. 约定 0 表示“使用引擎的默认值”的数值字段，在 description 里写明。

字段名同时是发给 OCR 进程的键名：`worker_settings` 给出 `configure` 的内容（请求级字段加上带 `reload` 的字段、推理设备与引擎），`request_options` 给出每个请求的 options，后端按同样的名字读取。各环节的默认模型 id 同时出现在 `catalog.py` 与 `config.py` 里，两个模块互不导入，有一条测试核对两边相同。

PaddleOCR 的默认参数沿用重构前的调参。重构前的项目是 LingZero（独立的私有仓库 myocr，最后一次提交 2026-03-13），它的 `src/ocr/pp_structure_ocr.py:89-139` 用 `PPStructureV3` 创建产线，参数来自 `src/config/config_manager.py:28-60` 的默认值与已部署的 `config/config.ini:19-52`（修改于 2026-03-03）。该项目固定 paddleocr 3.4.0、paddlex 3.4.1，打包目录里保留了当时的 `paddlex/configs/pipelines/PP-StructureV3.yaml` 与 `OCR.yaml`。检测、识别与版面的数值阈值在旧版里全部留空，创建产线时传 `None`，即采用这份 yaml 的取值；旧版真正做出选择的是模型、子能力开关和 CPU 运行方式。当前版本 paddlex 的同名 yaml 与旧版只在检测和识别的默认模型上不同（旧版 PP-OCRv5 server，现在 PP-OCRv6 medium），阈值逐项相同。下表列出每一项的旧值、当前默认值和处理：

| 参数 | 旧版取值（出处） | 当前默认值（出处） | 处理 |
|---|---|---|---|
| 文字检测、识别模型 | PP-OCRv5_mobile_det、PP-OCRv5_mobile_rec（`config.ini:21-22`） | 同（`PADDLE_DEFAULT_MODELS`） | 不变 |
| 版面检测、区域检测模型 | PP-DocLayout_plus-L、PP-DocBlockLayout（`config.ini:20,27`） | 同 | 不变 |
| 无线表格结构、公式、图表模型 | SLANet_plus、PP-FormulaNet_plus-L、PP-Chart2Table（`config.ini:30,33,23`） | 同 | 不变 |
| 文档方向、去畸变、文本行方向模型 | PP-LCNet_x1_0_doc_ori、UVDoc、PP-LCNet_x1_0_textline_ori（`config.ini:24-26`） | 同 | 不变 |
| `text_det_limit_side_len`、`text_det_limit_type` | 空，产线默认 736、min（yaml 第 112-113 行） | 0、`default`，即产线默认 | 不变 |
| `text_det_thresh`、`text_det_box_thresh`、`text_det_unclip_ratio` | 空，产线默认 0.3、0.6、1.5（yaml 第 115-117 行） | 0.3、0.6、1.5 | 不变 |
| `text_rec_score_thresh` | 空，产线默认 0.0（yaml 第 128 行） | 0.0 | 不变 |
| `layout_threshold` | 空，产线按类别取 0.3 至 0.5（yaml 第 28-48 行） | 0，即按类别取默认 | 不变 |
| `layout_nms`、`layout_unclip_ratio` | 空，产线默认 True、[1.0, 1.0]（yaml 第 49-50 行） | True、1.0 | 不变 |
| `layout_merge_bboxes_mode` | 空，产线按类别取 large 或 union（yaml 第 51-71 行） | `default`，即按类别取默认 | 不变 |
| 版面区域检测 `use_region_detection` | 开（`config.ini:52`） | 开（`OcrConfig.region`） | 不变 |
| 表格识别 `use_table_recognition` | 关（`config.ini:49`，`config_manager.py:56`） | 原为开 | 改为关：表格识别要多载入 5 个模型，处理时限加 60 秒；需要时在设置里打开 |
| 公式、图表识别 | 关（`config.ini:50-51`） | 关 | 不变 |
| 文档方向校正、去畸变、文本行方向 | 关（`config.ini:46-48`） | 关 | 不变 |
| 印章识别 `use_seal_recognition` | 没有传，yaml 里是关（第 7 行） | 总是关（`paddle_config.structure_config`） | 不变，写死 |
| `enable_mkldnn` | 开（`config.ini:19`） | 原来在 CPU 上逐模型决定，没有开关 | 新增字段 `enable_mkldnn`，默认开，关闭后全部模型用普通方式运行 |
| `cpu_threads` | 8（`config.ini:45`） | 0，自动取 8 与逻辑处理器数的较小者 | 不变 |
| 推理设备 | gpu（`config.ini:44`，代码默认 cpu） | `auto`，有可用显卡就用 | 不变 |
| 截图缩放、灰度 | 关、关；缩放阈值 40000（`config.ini:14-16`） | 灰度关；缩放上限 4000，各引擎共用 | 不变，该字段属于所有引擎 |

阈值字段的取值范围覆盖全部旧值。旧版设置界面（`src/ui/settings.py:170-185`）允许的范围更宽：外扩系数只要求大于 0，识别分数只要求不小于 0。当前字段的下限（文本框外扩系数 1.0、版面框外扩系数 0.5）和识别分数上限 1.0 保持不变：识别分数本身落在 0 到 1 之间，小于 1 的外扩系数会把检测框缩进文字里，旧版没有任何默认值依赖它。

表格识别的默认下载组合不变：安装引擎时仍下载表格所需的模型，用户在设置里打开“表格识别”即可使用。表格识别关闭时，版面检测仍把表格区域标成 `table` 类别，块里只有文字，没有单元格。

#### 8.6.5 OCR 进程的启动、重载与失败

当前引擎写在 `[ocr].engine`，默认 `windows`，安装版装好即可截屏识别。组合根在三种情况下重新决定 OCR 进程：配置变化、`OcrEngineCatalog` 中安装状态的变化、`RestartWorker`。决定都在 4.3 节的决策锁里串行执行。

决定进程的依据合成一个去重键，只有去重键变化时才经 `WorkerSupervisor.reload(settings, command=…)` 重载，按引擎切换解释器与后端。去重键由七项组成：

1. 是否启用：`[ocr].enabled` 为真，并且当前引擎可以运行（Windows 有 OCR 语言、RapidOCR 或 PaddleOCR 已装好）；
2. 当前引擎；
3. `process_key()`：引擎与识别语言；PaddleOCR 再加识别模式；PaddleOCR 与 RapidOCR 再加推理设备；以及当前引擎流水线配置里带 `reload` 的字段；
4. PaddleOCR 的模型缓存目录；
5. Windows 已装的 OCR 语言；
6. 已装的运行环境版本；
7. 各环节选用的模型是否就位。

第 5 至 7 项让原来因确定性故障失败的进程在条件满足后重新启动：新装了 Windows OCR 语言、换装了运行环境的另一个版本、选用的模型下载完成，都会改变去重键。引擎目录因为下载进度频繁发布，组合根只比较目录里的安装状态（各引擎的可运行性、语言、已装版本、选用模型是否就位），合并进程状态引起的发布不触发判断。

OCR 进程的状态由组合根合并进目录，只作用于当前引擎、并且安装状态是 INSTALLED 的条目：

| 监管器状态 | 引擎卡片 |
|---|---|
| STARTING、LOADING、CRASHED | 加载中 |
| READY、BUSY | 就绪 |
| FAILED | 出错，附错误码 |

FAILED 的错误码按监管器给出的故障名区分：

| 故障 | 错误码 | 典型原因 |
|---|---|---|
| 退出码 65（依赖错误） | `ocr_engine.runtime_mismatch` | 推理设备选了显卡，而运行环境是 CPU 版，或驱动不支持所需的 CUDA 版本 |
| 退出码 66（配置错误） | `ocr_engine.bad_config` | 识别语言不受所选模型支持，或所选模型没有下载 |
| 其余（10 分钟内反复崩溃） | `ocr_engine.worker_failed` | 原生库崩溃等 |

OCR 进程进入 FAILED 后去重键保留，只有决定进程的依据变化时才重新启动。否则每次保存任何与 OCR 无关的设置（例如热键）都会把一个确定性故障的进程重新拉起，它又立即以同样的退出码失败，设置窗口里这个引擎一直显示“加载中”。`RestartWorker` 与卸载前的停止会清除去重键，配置没变也重新启动。

### 8.7 结构归一化中间层

OCR 进程返回的版面块经 `decode_blocks` 校验之后、`blocks_to_segments` 切分成段之前，先经过结构归一化中间层（`lst/services/ocr/normalize.py`，`OCR-11`），把不同引擎、不同模式的输出整理成同一种结构：

```
decode_blocks → 清洗 → 拆分 → 聚合 → blocks_to_segments
```

每一步实现同一个协议：

```python
class NormalizeStep(Protocol):
    name: str
    def apply(self, blocks: Sequence[LayoutBlock], context: NormalizeContext) -> tuple[LayoutBlock, ...]: ...
```

输入输出都是按阅读顺序排列的版面块，步骤之间只通过版面块传递信息，版面块本身不可变，步骤不修改输入。`NormalizeContext` 携带截图的物理像素宽高、是否文本模式与可选的原始像素帧。`NormalizePipeline` 依次执行各步骤，`build_pipeline` 按 `[ocr_normalize]` 节组装；全部步骤关闭时管线为空，版面块原样通过。这一节只在主进程里生效，没有需要重载 OCR 进程的字段。

#### 8.7.1 步骤的顺序

三步的顺序固定为清洗、拆分、聚合，因为每一步都要求前一步的输出是干净的：

1. 清洗在拆分之前。同一行被引擎重复输出两次时，不先去重，拆分会把这两份各拆一次，聚合时两份同样的文字又被并进同一段。低置信度的噪声框夹在两栏之间时，还会盖住栏间空白，让拆分判断“这里没有栏间空白”。
2. 拆分在聚合之前。一条横跨两栏的长行先拆成左右两行，聚合才能把它们分别并入各自那一栏的段落；先聚合的话，跨栏长行在垂直方向上同时挨着左右两栏的行，会把两栏粘成一段，事后再也拆不开。
3. 聚合最后执行，它的输入是一行一个框、互不重复的文本行。

#### 8.7.2 各步骤

清洗（`CleanStep`）按顺序做三件事：

1. 丢弃文字为空白的行；
2. 丢弃面积小于 `clean_min_area` 平方像素、或置信度低于 `clean_min_score` 的行与块；
3. 去掉重复框：两个框的交并比（Intersection over Union，IoU）不小于 `clean_duplicate_iou` 时视为重复，保留置信度高的一个，相同时保留先出现的。块只与同类别的块比较。

文本类块（正文、标题、页眉页脚）清洗后既没有文字也没有文本行时整块丢弃；表格、公式、图表、图片块的内容不在文字里，只按面积、置信度与重复判断。文本模式的容器块覆盖整张截图、置信度是各行的平均值，只清洗它的行，清洗后按剩下的行重算平均值。

拆分（`SplitStep`）只在文本模式下进行。判断一行 $L$（高 $h$）是不是跨栏长行分三步：

1. 邻居：中线与 $L$ 相差大于 $0.5h$（不在同一行）、又不超过 `split_window` 倍 $h$ 的其他行；
2. 空白：$L$ 的水平范围里没有被任何邻居盖住、宽度不小于 `split_min_gap` 倍 $h$、并且不贴着 $L$ 两端的区间；
3. 证据：空白左侧至少有一个邻居（右边缘不超过空白左端，并与 $L$ 的左段水平重叠），右侧同样至少有一个。只有一侧有邻居时，那是缩进或表格的一列，不是栏间空白。

找到空白后在文字里找切分点：按字符宽度（东亚宽字符为 1，其余为 0.5）估计每个字符在框里的水平位置，取估计位置离空白中心最近的一段连续空白，距离不超过空白宽度与 $2h$ 中较大的一个才切分。文字里没有空白（例如中文）时不拆，因为按比例估计的字符位置误差可能有一两个字，切错了比不切更糟。拆出的行沿用原行的上下边与置信度，左段的右边缘取空白左端，右段的左边缘取空白右端。

表格里粘连的单元格不拆分。单元格网格来自 PP-StructureV3 的 `pred_html` 与 `cell_box_list`，主进程手里只有单元格的框与文字，没有框线的像素信息；按文字推断网格线，在跨行跨列的合并单元格处会把本来正确的单元格拆坏，得不到可靠的结果。

聚合（`AggregateStep`）按模式分两种：

- 文本模式：全部正文块的文本行一起交给段落合并器（默认 `GeometricMerger`，阈值来自配置），每个段落成为一个新的正文块，id 为 `p<序号>`，lines 是成员行，text 是成员行拼成的段落文本。段落块排在原来第一个正文块的位置；
- 版面模式：先结合截图颜色、笔画粗细与间距拆开模型粘连的菜单和控件，连续正文保留为段落；再可选把阅读顺序上相邻、上下间距不超过 `merge_block_gap` 倍行高、水平重叠不少于较窄一块宽度一半的两个正文块合并成一块。

有截图像素时，`ImageAppearance` 提取前景色与字形高度，并估计完整像素采样中的笔画粗细。颜色按亮度对比度加权，抑制 ClearType 彩色边缘。短行里的不同颜色或明显不同间距的控件可以按文字投影拆开。编号、评分符号等附属片段与同一行的文字一起处理。没有像素证据时使用原有几何规则；截图样本与指标见 `docs/ocr-quality.md`。

词级检测框的字形高度随上伸和下伸部分变化，横向字号判断使用框高与笔画宽度的共同变化。`ImageAppearance.connects` 为较宽间隙提供同色、同背景的文字像素证据，合并器另检查中间没有其他已识别片段，防止跳过导航项。`ImageAppearance.gutter` 确认几何候选栏缝在原图中持续留白。这两个回调只参与主进程拼行，输入、输出协议与关闭整理功能时的行为保持原约定。

#### 8.7.3 默认值

| 步骤 | 默认开关 | 默认参数 |
|---|---|---|
| 清洗 | 开 | 最低置信度 0（不按置信度丢弃），最小面积 1 平方像素，重复框 IoU 0.9 |
| 拆分 | 关 | 栏间空白至少 1.5 倍行高，上下各看 3 倍行高 |
| 聚合：文字单元整理 | 开 | `MergeParams` 几何参数，加上截图颜色、笔画与间距判断 |
| 聚合：相邻正文块合并 | 关 | 间距上限 0.8 倍行高 |

清洗默认去掉空文字、零面积与 IoU 不小于 0.9 的重复框，跨栏拆分默认关闭。没有截图像素的既有段落回归样本继续使用原有几何规则，匹配率与直接调用 `GeometricMerger` 相同；实际截图另外使用带人工标注的保真度回归。

#### 8.7.4 与分段的衔接

文本模式下，聚合步骤已经把文本行合并成段落块，`blocks_to_segments` 以 premerged 方式一块一段，段号是 `p<序号>`。聚合关闭时文本行保持原样，`OcrModule` 用 `LineMerger` 一行一段。版面模式下，整理后的文本类块一块一段。

`OcrCompleted.blocks` 是归一化之后的版面块。文本模式下，这些块是段落块，不再是 OCR 进程返回的那个覆盖整张截图的容器块。覆盖窗口按段的范围渲染，不受影响。

日志只写数量，不写识别出的文本（`PRV-02`）。

## 9 配置

配置文件是 `config/settings.toml`。下面是一个节选，展示结构：

```toml
[hotkeys]
capture = "Alt+Q"
bilingual = "Alt+E"

[bindings]                 # 窗口内的键位，没写的操作用默认键位（11.8 节）
overlay_zoom = ["Ctrl+Wheel"]

[race]
show_source_marker = { overlay = false }   # 覆盖窗口的来源标记，见 RACE-06

[screenshot]
target_lang = "zh-Hans"

[bilingual]
pair = ["zh-Hans", "en"]   # 互译对，见 RACE-10
debounce_ms = 400

[ocr]
enabled = true            # 关闭后不启动 OCR 进程，截屏翻译不可用
engine = "windows"        # windows、rapid 或 paddle
mode = "structure"        # structure 或 text，只对 PaddleOCR 生效
device = "auto"           # auto、cpu 或 gpu，对 RapidOCR 与 PaddleOCR 生效
table = true
formula = false
chart = false
text_timeout_s = 30       # 处理时限：文本模式
structure_timeout_s = 60  # 版面模式的基础时限
feature_timeout_s = 60    # 表格、公式、图表每开一项，版面模式的时限再加这么多

[ocr_rapid]               # RapidOCR 的流水线（8.6.4 节）
detection_model = "PP-OCRv6/small/multi/det"
recognition_model = "PP-OCRv6/small/multi/rec"
det_thresh = 0.3

[ocr_paddle]              # PaddleOCR 的流水线
layout_model = "PP-DocLayout_plus-L"
table_model = "SLANet_plus"

[ocr_normalize]           # 结构归一化（8.7 节）
clean = true
split = false
aggregate = true

[[engines]]
id = "deepseek-flash"
kind = "openai_compat"     # 引擎实现：openai_compat、deepl、google、baidu、google_free、microsoft_free、local_nmt、dictionary
provider = "deepseek"
name = "DeepSeek Flash"
priority = 1
timeout_s = 60
base_url = "https://api.deepseek.com"
model = "deepseek-flash"
secret = "deepseek"        # keyring 中的条目名；配置文件里不出现 API key

[[engines]]
id = "argos"
kind = "local_nmt"
name = "Argos"
priority = 3
timeout_s = 10             # 任务总时限；单批处理时限 processing_timeout_s 默认 5

[[engines]]
id = "dictionary"
kind = "dictionary"
name = "词典"
priority = 4
pivot_via_english = false  # 两端都不是英语时经英语中转
```

OCR 配置一共五节，都由 `lst/services/ocr/config.py` 定义：

| 节 | 内容 |
|---|---|
| `[ocr]` | OCR 总开关、当前引擎、识别模式、推理设备、识别语言、子能力开关、截图预处理与处理时限，各引擎共用 |
| `[ocr_windows]`、`[ocr_rapid]`、`[ocr_paddle]` | 各引擎的流水线：每个环节选用的模型与流水线参数（8.6.4 节） |
| `[ocr_normalize]` | 结构归一化各步骤的开关与参数（8.7 节） |

没有后四节的旧配置文件照常可用，各字段取默认值，默认值与这几节出现之前写死在 OCR 进程里的模型名相同。

配置文件不存在时，引擎列表按 `EngineListSchema.default_items` 生成，需要 API key 的网络引擎不放进默认列表（`ENG-10`）：

| 优先级 | 实例号 | 类别 | 默认状态 |
|---|---|---|---|
| 1 | `argos` | `local_nmt` | 启用 |
| 2 | `google_free` | `google_free` | 不启用 |
| 3 | `microsoft_free` | `microsoft_free` | 不启用 |
| 4 | `dictionary` | `dictionary` | 启用 |

两个免费接口默认列出但不启用，因为启用后文本会发给谷歌或微软，只能由用户决定（`PRV-01`）。它们的译文好于逐词粗译，排在本地 NMT 之后、字典翻译之前；字典翻译最快、译文最粗，优先级排在最后，它先给出结果，其他引擎的译文到达后替换它。默认列表只在配置文件不存在时使用，已有配置文件的用户要在设置的翻译引擎页自己添加这些实例。

配置服务启动时读取并校验配置（2.9.1 节），然后发布 `AppConfig` 状态。校验除了字段类型和取值范围，还保证两条跨字段约束：

- 各引擎实例的优先级两两不同（6.2 节前提 1）。出现重复时，按（原优先级，列表位置）稳定排序后重新编号为 1、2、3……；只按列表位置编号会推翻用户的意图，例如 `[argos 3, deepseek 1, deepl 3]` 会把原本最高的 deepseek 降成 2。没有重复时原值不动；
- 每个引擎实例的时限是有限值（6.5 节）。

设置窗口通过 `UpdateConfig` 命令提交修改，配置服务校验通过后写回文件，并发布新的 `AppConfig`。各模块的处理方式如下：

| 模块 | 收到新配置后的处理 |
|---|---|
| ui.hotkeys | 重新注册热键 |
| services.engines | 每个实例按自己的输入判断是否重建：配置条目本身，网络引擎再加上 `secrets_revision`，本地 NMT 再加上模型指纹；输入变了才调用工厂，新实例的 `EngineInfo` 与旧实例相等时沿用旧对象。名册有变化时发布新的 `EngineRoster`，调度器按 6.5 节第 4 条处理旧实例 |
| services.ocr | 子能力开关、预处理、流水线的请求级参数与 `[ocr_normalize]` 随下一次识别生效；引擎、识别模式、设备、识别语言与流水线里带重载标记的字段变化时，由组合根重载 OCR 进程（8.6.5 节） |
| services.race | 目标语言、互译对等设置作用于之后开始的会话 |

缺少 API key 的网络引擎实例状态为“出错”（错误码 `engine.auth`），但照样进入名册：翻译时每段都以认证错误失败，失败原因显示在各个尝试上，用户在双语窗口的引擎列表里就能看到原因。不进名册的只有两种情况：没有 NMT 进程通道的本地 NMT 实例，以及工厂抛出异常的实例。

API key 不经过 `UpdateConfig`。设置窗口发送 `UpdateSecret` 命令，配置服务直接写入 keyring，事件总线和日志都只记录命令的类名（第 10 章）。

## 10 隐私与日志

需求 `PRV-01`、`PRV-02` 和 `CFG-04` 的落实方式如下。

### 10.1 网络请求

- 程序发起的网络请求只有四类：
  1. 用户启用的在线引擎实例，包括不需要 API key 的谷歌与微软免费接口，这两个实例默认不启用；
  2. 用户确认的下载：`services.models` 的 NMT 模型索引与模型包、词典索引与词典包（GitHub Release）；`services.ocr_engines` 的 RapidOCR 与 PaddleOCR 运行环境（由 uv 从 PyPI 获取，PaddleOCR 的 CUDA 版另从 Paddle 官方源获取 paddlepaddle-gpu）与模型（PaddleX 官方地址、RapidOCR 的 ModelScope 地址）；`services.updates` 的更新安装包；
  3. 检查新版本，只访问 GitHub 的版本列表，可以关闭；
  4. 用户配置的本地 LLM 服务。
- 显卡检测（8.6.3 节）只调用本机的 DXGI 与 D3D12，不联网。
- OCR 进程启动时设置 `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True`，关闭 PaddleX 启动时对模型源的连通性检查，并用审计钩子拦截对外连接（`network_guard.py`）。判定规则：字面 IP 用 `ipaddress` 判断，只放行回环、未指定与映射到回环的地址；域名只放行 `localhost`；`socket.connect`、`sendto` 的地址不是字面 IP 时一律拒绝（按域名直连不经过 `getaddrinfo` 审计事件，否则会漏过）；反向解析 `getnameinfo` 也拦截。最初的实现用 `startswith("127.")` 判断，`127.evil.example.com` 会被放行。审计钩子不拦截 subprocess 启动的外部程序和原生库直接调用的套接字；PaddleX 3.7 只在公式结果生成图片与训练接口里启动外部程序，OCR 后端不走这两条路径。
- 程序不包含任何统计上报或遥测。

### 10.2 日志内容

- httpx 和 httpcore 的日志级别设为 WARNING，它们的 INFO 日志会记录完整 URL。
- 日志配置中挂一个脱敏过滤器：配置服务把当前所有 API key 注册给它，任何日志记录里出现这些字符串都替换为 `***`。
- Google 的 API key 放在请求头里，百度用 POST 提交（2.7.2 节），API key 和原文都不出现在 URL 中，异常信息里也就不会带出它们。
- 事件总线记录异常时只写消息的类名。
- 原文、译文和截图默认不写日志。用户打开“调试记录内容”开关后，翻译相关的 DEBUG 日志才包含文本（`PRV-02`）。
- 统计库只保存会话号、段号、实例号、状态、耗时、是否命中缓存、是否执行过、错误码、胜出者、记录时间和段的字符数，不保存原文、译文和错误详情。
- 诊断导出（`OBS-03`）打包的配置文件里不含 API key，因为配置文件本来就只保存 keyring 条目名；导出前再对日志做一次与脱敏过滤器相同的替换。

### 10.3 面向用户的错误信息

错误码分三组，每组一个枚举：

- `ErrorCode`（`contracts/errors.py`）：翻译尝试与单次识别的失败原因，例如 `engine.auth`（API key 无效）、`engine.quota`（配额耗尽）、`engine.no_match`（字典翻译查不到这一段的任何词）、`race.engine_removed`（实例因配置变更被移除）；
- `ModelErrorCode`（`contracts/models.py`）：NMT 模型与词典包的下载、校验失败，前缀 `models.`；
- `OcrEngineErrorCode`（`contracts/ocr_engines.py`）：OCR 引擎安装失败或 OCR 进程出错，前缀 `ocr_engine.`。

`OcrEngineErrorCode` 的全部取值：

| 错误码 | 含义 | 来源 |
|---|---|---|
| `ocr_engine.no_language` | Windows 没有装任何 OCR 语言包 | Windows 引擎的语言检测 |
| `ocr_engine.network` | 下载时连接失败 | 运行环境或模型下载 |
| `ocr_engine.timeout` | 下载超时 | 运行环境或模型下载 |
| `ocr_engine.http` | 下载地址返回非 200 | 模型下载 |
| `ocr_engine.disk` | 写入失败或磁盘空间不足 | 安装、解压、改名 |
| `ocr_engine.invalid_package` | 下载内容校验失败：长度、SHA-256、压缩包成员或模型目录结构不符 | 模型下载、运行环境复查 |
| `ocr_engine.install_failed` | uv 安装运行环境返回非零 | 运行环境安装 |
| `ocr_engine.runtime_mismatch` | OCR 进程以退出码 65 退出：推理设备选了显卡，运行环境或驱动不支持 | OCR 进程 FAILED（8.6.5 节） |
| `ocr_engine.bad_config` | OCR 进程以退出码 66 退出：识别语言不受所选模型支持，或所选模型没有下载 | OCR 进程 FAILED |
| `ocr_engine.worker_failed` | OCR 进程 10 分钟内反复崩溃 | OCR 进程 FAILED |
| `ocr_engine.internal` | 其他未预期的异常 | 任意 |

引擎的每个失败结果都带两样东西：

- 错误码：界面据此显示原因和处理建议，`ui.i18n` 负责把它翻译成当前界面语言（`OBS-04`、`CFG-05`）；
- 技术细节：只写日志和诊断，例如“HTTP 401: invalid key”。

业务模块不包含任何界面文案。

网络引擎共用 `services/engines/http.py` 里的工具：

- 状态码到错误码的映射；
- 按 2.7.3 节执行的重试；
- SSE 解析；
- 令牌桶限速。

这些工具生成的技术细节只包含状态码和 JSON 响应里的错误消息字段，不包含 URL 和响应正文，因为有些接口会在错误响应里回显原文。

## 11 界面层

### 11.1 界面与总线的连接

qasync 让 asyncio 运行在 Qt 主线程上，总线的处理函数也在主线程执行，所以界面模块的处理函数可以直接操作控件，不需要再切换线程。

每个界面模块分为两部分：

- 展示器（Presenter）：订阅消息，把消息转换成对视图的调用，并把用户操作转换成命令。它不包含绘制代码，可以用 pytest-qt 测试。
- 视图：`QWidget` 子类，只负责绘制和收集输入。

`ui.theme` 集中定义主题：颜色、圆角、间距、字体（Segoe UI Variable）和 Mica 设置函数。所有界面模块都从这里取值，以保证视觉统一（`UI-01`）。`ui.i18n` 提供界面文案的简体中文和英文版本（`CFG-05`）。

`ui.common` 是第三个共用模块，放各窗口共用的代码：窗口状态存储与屏外检测、`SecondaryWindow` 基类（首次显示前设置 Mica，失败即回到不透明背景，深浅色变化时三个窗口一起更新）、语言名与大小格式化、模型行状态、进度条换算，以及界面层内部事件 `HotkeyRecordingChanged`。三个共用模块不得依赖任何具体的界面模块，import-linter 有一条契约约束。

界面语言由各界面模块在处理 `AppConfig` 时自己设置（`i18n.apply_config_language`），再刷新文案，不依赖组合根先处理配置。总线按订阅顺序调用处理函数，组合根的配置处理又是协程，要到下一轮循环才执行，最初双语窗口和托盘的悬停提示因此在切换语言后仍显示旧语言。

界面的测试为每个测试新建一个 qasync 事件循环。旧循环留下的 Qt 对象（qasync 内部的计时器与信号对象）如果在执行器线程上被垃圾回收析构，主线程随后会在 `exec` 里访问违例；测试曾因此偶发崩溃。`tests/ui/conftest.py` 与 `tests/integration/conftest.py` 在每个测试的夹具拆完、循环关闭之后，于主线程执行一次 `gc.collect()`。正式程序全程只有一个循环，不受影响。

所有窗口都按“Tab 顺序与视觉顺序一致”设置焦点链，并为每个按钮提供快捷键或助记键（`UI-07`）。焦点顺序由展示器测试检查。

托盘菜单的状态区（`TR-03`）用子菜单显示出错原因：处于“出错”状态的条目带一个子菜单，列出原因和处理建议。鼠标悬停时子菜单自动展开，满足“悬停可查看原因”；用键盘打开托盘菜单时，方向键也能进入子菜单。菜单项的工具提示只响应鼠标，键盘用户看不到，所以没有采用（`UI-07`）。

### 11.2 主线程上的耗时操作

第 3 章列出了三类可能超过 16 毫秒的主线程操作，处理方式如下：

| 操作 | 处理方式 | 约束它的指标 |
|---|---|---|
| 截屏冻结：每块屏幕 `grabWindow` 约 21 毫秒 | 只在按下热键时发生一次，此时没有竞速在进行 | 按下热键到蒙版出现 ≤ 200 毫秒 |
| 窗口首次创建 | 设置窗口和双语窗口在程序空闲时预先创建并隐藏，打开时只做显示 | 设置窗口打开 ≤ 300 毫秒 |
| 覆盖窗口的字号测量 | 分帧执行：每轮事件循环最多用 8 毫秒测量，未测量的段先用估算字号显示，测完后替换（11.5.3 节） | `REL-04` |

### 11.3 热键

`ui.hotkeys` 用 `RegisterHotKey` 注册热键，并加上 `MOD_NOREPEAT` 去掉按住时的连发。`WM_HOTKEY` 消息通过 `QAbstractNativeEventFilter` 在主线程收到，然后发送 `StartCapture` 或 `ToggleBilingualWindow` 命令。注册失败时发布 `HotkeyRegistrationFailed`，托盘弹出通知（`TR-05`）；失败原因是与本程序另一个热键相同时，照实说明，不说成被其他程序占用。

两个热键不能设成同一组合，`HotkeysConfig` 的校验器拒绝它，原因随界面语言显示。设置窗口录入热键时，系统会把已注册的组合截走、变成 `WM_HOTKEY`，录入框收不到，所以录入期间暂停全部全局热键：录入按钮进入或退出录入状态时，设置窗口发布 `HotkeyRecordingChanged`，`ui.hotkeys` 收到后注销或按当前配置重新注册。录入按 `nativeVirtualKey()` 生成键名，Shift 加数字录成 `Ctrl+Shift+1`，而不是 Qt 给出的 `Ctrl+Shift+!`。

### 11.4 截屏蒙版

截屏蒙版位于 `lst/ui/capture`，分成四部分：

- `geometry`：逻辑坐标与物理像素之间的换算，纯函数，不依赖 Qt，可以用虚构的多屏布局单独测试；
- `mask`：蒙版窗口 `MaskWindow`，每块屏幕一个，只负责绘制和收集鼠标、键盘输入；
- `controller`：`CaptureController`，负责冻结屏幕、框选、取消，最后发布 `CaptureCompleted`；
- `module`：`CaptureModule`，`StartCapture` 命令的唯一处理者。

#### 11.4.1 一次截屏的过程

1. 收到 `StartCapture` 时，对每块屏幕调用一次 `grabWindow(0)`，得到物理像素的冻结画面。冻结在显示蒙版之前完成，画面里不会出现蒙版本身。截屏已经在进行时再按热键，只把蒙版带到前面，不重新冻结。
2. 每块屏幕创建一个蒙版窗口，铺满这块屏幕。鼠标所在屏幕的蒙版获得焦点，用来接收键盘操作。
3. “框选区域”的按键（默认左键）按下后移动超过 4 个逻辑像素，控制器记下按下的点和所在的屏幕。之后的拖动只更新这块屏幕的选区，两个端点先限制在这块屏幕的范围内（`CAP-02`），右、下边界取闭区间，选区可以一直拉到屏幕边缘。
4. 松开时，选区的宽或高小于 5 个逻辑像素视为取消（`CAP-04`），按下后没有移动就松开也属于这种情况。否则从冻结画面裁出选区的物理像素，转成 BGRA 的 `RawImage`，用 UUID4 生成会话号，先关闭蒙版，再发布 `CaptureCompleted`。这个先后顺序保证覆盖窗口在下一轮事件循环里弹出时，蒙版已经不在屏幕上。
5. “取消截屏”（默认右键单击与 `Esc`）关闭蒙版，不发布任何事件。右键没有绑定拖动操作，所以单击在按下时就生效，框选途中按下右键也立即取消（11.8 节）。

`QPixmap.copy` 的矩形按像素计，不受设备像素比影响，裁切因此直接使用物理像素矩形。`grabWindow` 给出的是 RGB32 格式，转换成 `Format_ARGB32` 后，小端机器上的内存顺序就是 B、G、R、A，转换只是一次内存拷贝。

#### 11.4.2 蒙版窗口的绘制

蒙版窗口是自绘的 `QWidget`，无边框、置顶，不使用 `QGraphicsView`。它在 `paintEvent` 里依次画出：

1. 冻结画面。图像的设备像素比设成这块屏幕的设备像素比，Qt 绘制时不再缩放，画面按 1:1 落在物理像素上（`CAP-01`）；
2. 选区之外的四块区域盖上黑色遮罩，透明度约 43%（alpha 为 110）。冻结画面仍然看得清，选区与其余部分对比明显；
3. 选区外侧一圈 1 像素宽的强调色细线（cosmetic 画笔，线宽不随缩放变化）。细线画在选区外侧，不盖住会被截取的像素；
4. 选区左上角上方的尺寸标签，写出选区的物理像素宽高，也就是截图的尺寸。上方放不下时放到选区内的左上角；
5. 还没有选区时，屏幕顶部居中显示一条操作提示，按当前键位列出框选区域与取消截屏的键位，例如“框选区域：左键拖动　　取消截屏：右键单击 / Esc”。

选区的四条边按物理像素画，逻辑坐标等于物理像素除以设备像素比，所以屏幕上看到的选区与最终裁切的范围完全一致。拖动时只重绘新旧选区连同尺寸标签覆盖的区域，4K 屏上每次鼠标移动也只重绘一小块。

每块屏幕单独一个窗口，是因为缩放比例不同的屏幕拼在一起时，Qt 的逻辑坐标里会出现缝隙（推导见 11.4.3 节）。一个跨越全部屏幕的窗口无法同时覆盖这些缝隙两侧的区域并保持 1:1 绘制。

#### 11.4.3 坐标换算

程序按 Per-Monitor DPI Aware V2 运行，这是 Qt 6 在 Windows 上的默认值。系统给出物理像素坐标（下称原生坐标），Qt 再按每块屏幕各自的设备像素比（Device Pixel Ratio，DPR）换算成逻辑坐标交给程序。设一块屏幕的原生左上角为 $(o_x, o_y)$，设备像素比为 $r$，原生坐标 $(n_x, n_y)$ 对应的逻辑坐标是：

$$
l_x = o_x + \frac{n_x - o_x}{r},\qquad l_y = o_y + \frac{n_y - o_y}{r}
$$

由这个式子可以得到两个结论：

- 屏幕在逻辑坐标里的左上角与原生左上角相同，宽高是原生宽高除以 $r$ 后取整。
- 缩放比例不同的屏幕之间会出现缝隙。例如主屏在右、缩放 100%，左侧副屏宽 2560 像素、缩放 150%，副屏的原生范围是 $[-2560, 0)$。它的逻辑左边界是 $-2560$，逻辑宽度是 $\operatorname{round}(2560 / 1.5) = 1707$，逻辑范围因此是 $[-2560, -853)$，与主屏的起点 0 之间空出 853 个逻辑像素。

副屏在主屏左侧或上方时 $o_x$、$o_y$ 为负，公式不变。屏幕旋转只改变原生宽高（竖屏的宽小于高），冻结画面也按旋转后的方向给出，公式同样不变（`CAP-07`）。

蒙版窗口收到的鼠标位置是逻辑坐标的浮点数。把它换算回相对屏幕左上角的物理像素：

$$
p_x = \operatorname{round}\bigl((l_x - o_x)\, r\bigr),\qquad
p_y = \operatorname{round}\bigl((l_y - o_y)\, r\bigr)
$$

结果最后限制在冻结画面的范围 $[0, W] \times [0, H]$ 内，$W$、$H$ 是冻结画面的物理像素宽高。

误差分析（`CAP-03`）：设 Qt 给出的逻辑坐标与精确值相差 $\delta$，即 $l_x = o_x + (n_x - o_x)/r + \delta$。代入上式：

$$
(l_x - o_x)\, r = (n_x - o_x) + \delta r
$$

分两种情况：

- 鼠标位置由整数原生坐标精确换算而来时，$\delta$ 只来自浮点舍入，$|\delta r|$ 远小于 0.5，取整结果等于 $n_x - o_x$，与鼠标所在的物理像素完全相同。
- $|\delta| \le 0.5 / r$ 时，$|\delta r| \le 0.5$，$(n_x - o_x) + \delta r$ 落在 $[n_x - o_x - 0.5,\ n_x - o_x + 0.5]$ 内，四舍五入后只可能是 $n_x - o_x$ 或它相邻的整数，误差不超过 1 个物理像素。

取整用“加 0.5 后向下取整”，并先加上 $10^{-6}$。原因是 $(n / r) \times r$ 在浮点运算中可能得到 $n - 10^{-13}$ 这样的值，不吸收这点误差的话，恰好落在 0.5 处的值会被舍向错误的一侧。

换算用设备像素比 $r$，而不用“冻结画面宽度 ÷ 逻辑宽度”。逻辑宽度是取整后的值：175% 下 2560 宽的屏幕逻辑宽度是 1463，$2560 / 1463 \approx 1.74983$。用它换算原生坐标 2400 处的点，逻辑坐标是 $2400 / 1.75 \approx 1371.43$，换回物理像素得到 $1371.43 \times 1.74983 \approx 2399.77$，差出 0.23 个像素。鼠标坐标精确时，这个误差还不足以改变取整结果，但它占掉了取整的余量：鼠标坐标本身带有偏差时，两者叠加更容易落到相邻的像素上。用 $r$ 换算没有这部分误差。

覆盖窗口要按逻辑坐标摆放，所以选区还要从物理像素换回全局逻辑坐标：四条边各自按 $l = o + p / r$ 换算后四舍五入，宽高由取整后的边相减得到，至少为 1。

#### 11.4.4 截屏期间屏幕布局变化

截屏期间插拔显示器、改分辨率或缩放比例、旋转屏幕时，冻结画面与屏幕已经对不上。控制器取消这次截屏，并发布 `NotificationRequested` 请用户重来（`CAP-07`）。判断有两道：

1. 信号：`QGuiApplication` 的 `screenAdded`、`screenRemoved`、`primaryScreenChanged`，以及每块屏幕的 `geometryChanged`、`logicalDotsPerInchChanged`、`physicalDotsPerInchChanged`、`orientationChanged`。任何一个信号到达都取消截屏。
2. 松开鼠标时再核对一次：屏幕数量是否不变，每块屏幕的名称、逻辑矩形与设备像素比是否与冻结时相同。这一道防止信号漏发，例如两次变化互相抵消之前的中间状态。

测试通过注入冻结函数构造虚构的屏幕布局（负坐标、混合缩放、竖屏），这时没有真实的 `QScreen` 对象，控制器不跟踪屏幕变化，两道判断都不做。日志只记录屏幕数、尺寸和耗时，不记录画面内容（`PRV-02`）。

### 11.5 覆盖窗口

覆盖窗口位于 `lst/ui/overlay`，同样分成纯函数与视图两层：

- `geometry`：截图坐标、窗口逻辑坐标与设备像素之间的换算，缩放与单击判断，不依赖 Qt；
- `fitting`：字号适配的二分查找与估算，不依赖 Qt；
- `window`：覆盖窗口 `OverlayWindow`，自己绘制底图、版面块与译文；
- `presenter`：订阅总线消息，维护当前会话，驱动窗口；
- `module`：`OverlayModule`。

窗口是一个无边框、置顶的 `QWidget`，全部内容在 `paintEvent` 里自绘，不使用 `QGraphicsView`。每次截屏完成时新建一个窗口，因为窗口的尺寸、位置和底图都随截屏而定。

#### 11.5.1 三种坐标

覆盖窗口涉及三种坐标：

- 截图坐标：OCR 给出的包围盒，相对选区左上角的物理像素。选区宽 $W$、高 $H$；
- 窗口逻辑坐标：Qt 的窗口坐标；
- 设备像素：窗口当前所在屏幕的物理像素。

设截屏所在屏幕的设备像素比为 $r_c$，窗口当前所在屏幕的设备像素比为 $r_d$，用户滚轮缩放的倍数为 $z$。窗口的逻辑尺寸是：

$$
w = \operatorname{round}\Bigl(\frac{W z}{r_c}\Bigr),\qquad
h = \operatorname{round}\Bigl(\frac{H z}{r_c}\Bigr)
$$

$z = 1$ 时，窗口在截屏的屏幕上恰好盖住选区（`OV-01`）。一个截图像素对应的逻辑长度 $s$ 与设备像素数 $k$ 分别是：

$$
s = \frac{z}{r_c},\qquad k = s\, r_d = \frac{z\, r_d}{r_c}
$$

`paintEvent` 开始时先把画笔按 $1/r_d$ 缩放，之后的绘制都在设备像素坐标上进行。包围盒乘以 $k$ 就是它在屏幕上的物理像素位置，字号也按设备像素求。

窗口被拖到缩放比例不同的屏幕上时，Qt 保持窗口的逻辑尺寸不变，$r_d$ 改变，$k$ 随之改变。窗口在两个时机重新布局：原生窗口的 `screenChanged` 信号，以及 `DevicePixelRatioChange` 事件。重新布局时全部段按新的 $k$ 重新适配字号，文字始终按屏幕的实际分辨率光栅化，不会先在低分辨率上画好再放大（`CAP-07`、`UI-04`）。

窗口创建时按 `CaptureGeometry` 里的屏幕名找到截屏所在的屏幕，左上角放在选区的逻辑坐标处，尺寸取上面的 $w \times h$。

#### 11.5.2 底图与绘制顺序

底图（`OV-02`）在截图到达时算一次：截图先平滑缩小到约 1/10，绘制时再用双线性插值拉伸回原尺寸，效果是模糊。平均亮度在把截图平滑缩小到 16×16 之后，对 256 个像素按 ITU-R BT.601 的系数求：

$$
Y = 0.299R + 0.587G + 0.114B
$$

平均亮度不低于 128 算亮底：盖一层白色半透明层（alpha 为 150），文字用黑色；否则盖黑色半透明层，文字用白色。

每次绘制的顺序：

1. 底图与半透明层；
2. 公式、图片、图表块：画原图（`OV-03`）；
3. 表格块：画单元格的网格线；
4. 各段：先在段的包围盒内铺一层与底图同色调、不透明度较高的底（alpha 约 210），再画选中文字的底色，最后画译文或原文。图表数据表的段只在点开图表后显示，数据表的单元格按行列数均分图表块的范围。图片块、没有数据表的图表块和没有单元格网格的表格块里，每个文本行是一个段，画在第 2 步的原图之上（段的规则见 `lst/services/ocr/segments.py` 的模块说明）；
5. 来源标记（段左侧的色条）、右下角的进度（`OV-07`）、固定状态的边框，以及“识别中”“识别失败”等状态文字。

段的文字用 `QTextLayout` 排版后由 `QTextLayout.draw` 画出，排版规则与选中底色的画法见 11.5.6 节。

#### 11.5.3 字号适配

字号按设备像素求：给定一个区域的设备像素宽高，找出能让换行后的文字放进区域的最大整数字号，范围是 6 到 160 像素。“放得进”的判据是 `QFontMetricsF.boundingRect` 测出的换行后外接矩形，宽和高都不超过区域。

二分查找的正确性依赖单调性：字号越大，每行容纳的字越少、行高越大，外接矩形的宽和高都不减，所以判据在 $[6, 160]$ 上先真后假，二分找到的就是最后一个为真的字号。个别字体在相邻字号间的换行位置会跳动，单调性只是近似成立，这时得到的字号偶尔比真正的最大值小一两号。返回值本身一定通过了判据，所以不会出现放不下的情况。判据在最小字号上就不成立时返回 6，文字以最小字号显示，超出部分被裁掉。

适配一个段时做两次测量：

1. 上限：在整个包围盒里测原文的最大字号；
2. 结果：在文字区域里测显示文字的最大字号，查找上界取第 1 步的结果。

译文字号因此不超过原文在同一包围盒里能用的最大字号。原文在截图里本来就排满包围盒，这个上限接近截图里原来的字号。不加上限时，比原文短的译文会被放大到远超原文的字号，同一屏里各段大小悬殊。用下面的估算公式可以看出差别有多大：包围盒宽 400、高 120 设备像素，原文是约 110 个字符的英文，折合 60 个字宽单位，估算字号约为 24；译成 20 个汉字后，不加上限的估算字号约为 42，接近原来的两倍。

单行正文与标题的包围盒在适配字号前先扩到原文字号的行高。OCR 给出的行框紧贴字形，同一字号的两行，带下伸字母（p、g、y）的那行比不带的高约四分之一。完整截屏测试里两行 48 像素的英文，行框分别高 39 和 51 像素；按行框适配，译文字号是 17 与 22 设备像素，相差约 30%。扩展分三步：

1. 按字形的实际高度反推原文字号：取字形高度不超过行框高、整行宽度不超过行框宽的最大字号。两行反推出的字号相同。
2. 把行框以中线为中心，在竖直方向扩到这个字号的行高。
3. 扩展不越过截图边界，也不越过与它水平重叠的相邻段之间的中线，相邻段不会互相遮挡。

扩展之后，上面两行的译文字号都是 36 设备像素。多行段落、表格单元格和图表数据表不扩展：多行段落的行框本来就包含行距，后两者的范围由网格线决定。

上限在整个包围盒里测，而结果在文字区域里测。原因是原文在截图里排满的是整个包围盒，在缩进了内边距的区域里测原文，得到的字号会比原来小一两号。

文字区域是包围盒向内缩进一圈内边距。内边距的设备像素数是：

$$
\text{pad} = \max\Bigl(1,\ \min\bigl(3\, r_d,\ \tfrac{1}{10}\min(w_b, h_b)\bigr)\Bigr)
$$

其中 $w_b$、$h_b$ 是包围盒的设备像素宽高。也就是说内边距是 3 个逻辑像素，不超过包围盒较短边的十分之一，至少 1 个设备像素。内边距让文字不贴在表格线或段底的边缘上；十分之一的上限保证很小的包围盒（例如表格里的窄单元格）不会被内边距占满。

还没有测量的段先用估算字号显示。估算把每个字按宽度折算成字宽单位：汉字、假名、谚文与全角字符为 1，其余为 0.55。设字号为 $p$，行高为 $1.35p$，区域宽 $w$、高 $h$，总字宽为 $u$ 个单位，则：

$$
\text{行数} \approx \frac{u p}{w},\qquad
\text{总高度} \approx \frac{u p}{w} \cdot 1.35p = \frac{1.35\, u\, p^2}{w}
$$

令总高度等于 $h$，解出：

$$
p \approx \sqrt{\frac{w h}{1.35\, u}}
$$

结果再限制在 $[6,\ h / 1.35]$ 内，因为一行字也不能比区域高。估算字号同样取显示文字与原文两者的较小值。

测量分帧进行（11.2 节）：新出现的段排进队列，零间隔的单次计时器每轮最多测 8 毫秒，测完的段只重绘自己的范围。测量结果按（显示的文字、原文、文字区域与包围盒的设备像素宽高、是否标题）缓存，同样的段不重复测。屏幕或缩放倍数变化后，缓存键里的尺寸变了，段自然重新测量。

#### 11.5.4 译文替换的过渡

显示的文字变化时播放 150 毫秒的过渡（`RACE-06`、`OV-04`）。每段从自己的译文到达时刻起独立计时：

- 段的新文字到达时，`SegmentItem` 记下旧文字、旧字号和过渡开始的时刻 $t_0$（窗口时钟，默认 `time.monotonic`）；
- 绘制时这一段的进度是 $\min\bigl(1, (t - t_0)/0.15\,\text{s}\bigr)$，旧文字以 $1 - \text{进度}$ 的不透明度、按旧字号绘制，新文字以进度的不透明度绘制；
- 一个约 16 毫秒间隔的计时器推进全部过渡中的段，每一帧只重绘这些段的范围；进度到 1 的段清掉旧文字、离开过渡集合，集合空了计时器就停。

早期实现整个窗口只有一个 `QVariantAnimation`，每次刷新都把它停下、从 0 重新开始，仍在过渡中的段一起重新计时。一张截图的译文由批量请求逐段流式到达时，刷新间隔短于 150 毫秒，先到的段一次次回到“几乎全是原文”，直到最后一段到达后 150 毫秒才看清。实测 11 段、每段间隔 30 毫秒时，第一段的不透明度在 330 毫秒里一直低于 0.2。每段独立计时后，后到的译文不影响已经在过渡中的段，每段都在自己到达后 150 毫秒内完全显示。

所有段共用一个计时器，每一帧只有一次回调，由它统一请求这些段范围的局部重绘，开销与一次刷新里有多少段同时过渡无关。显示原文时不播放过渡，切换原文与译文时结束全部过渡、直接替换。

#### 11.5.5 操作

窗口内的操作（`OV-05`）由键位表驱动，键位文字、单击与拖动的区分和默认键位见 11.8 节。窗口的 `mousePressEvent`、`mouseMoveEvent`、`mouseReleaseEvent`、`mouseDoubleClickEvent`、`wheelEvent` 与 `keyPressEvent` 先查出操作，再按下表执行：

| 操作 | 实现 |
|---|---|
| 拖动窗口 | 按下时记录全局位置与窗口位置，开始拖动后窗口跟随全局位移移动。拖动判断用全局坐标，因为窗口移动时窗口内的坐标跟着变 |
| 切换原文与译文 | 切换显示的文字，不播放过渡 |
| 缩放整张图 | 滚轮每格把倍数乘以或除以 1.1，限制在 0.25 至 4 之间，以光标为中心缩放 |
| 放大、缩小 | 与滚轮转过一格相同的步长，锚点是窗口中心 |
| 还原大小 | 倍数回到 1，锚点是窗口中心 |
| 固定在最上层 | 固定或取消固定，固定时窗口四周画一圈强调色边框 |
| 选择文字 | 拖动时从按下处的字符选到当前位置的字符，可以跨段；同一按键双击选词、三击选段；单击时清除选中，点在带数据表的图表块上时展开或收起数据表。规则见 11.5.6 节 |
| 关闭 | 关闭窗口 |
| 复制（不在键位表里） | 系统标准的复制键（`QKeySequence.StandardKey.Copy`，Windows 上是 `Ctrl+C` 与 `Ctrl+Insert`）复制选中的文字，没有选中时不做任何事 |
| 悬停 | 没有按键按下时移动鼠标，提示框显示段的原文，以及各参赛实例的状态、耗时和译文，胜出者排在第一行（`OV-06`） |

四种缩放都调用同一个 `set_zoom(z', c)`，改的是整张图的倍数 $z$：底图、版面块与各段按新的 $s$ 重画，字号重新适配，窗口尺寸随之改变。滚轮的锚点 $c$ 是光标位置，键盘的锚点是窗口中心 $c = (w/2, h/2)$，$w$、$h$ 是缩放前窗口的逻辑宽高。

以锚点为中心缩放的推导：设锚点在窗口内的逻辑位置为 $c$，窗口左上角为 $p$，缩放前后的倍数为 $z$、$z'$。锚点下的截图点是 $c / s$，缩放后它在窗口内的位置变为 $c \cdot z' / z$。要让它停在屏幕上的同一处，新左上角 $p'$ 满足 $p' + c\, z'/z = p + c$，所以：

$$
p' = p + c - c\,\frac{z'}{z}
$$

键盘放大一步时 $z' = 1.1\,z$，代入窗口中心 $c = (w/2, h/2)$ 得 $p' = p - 0.05\,(w, h)$：窗口向左上各扩出新增宽高的一半，中心不动。例如 $w = 400$ 时左边缘左移 20 个逻辑像素，新宽度 440，右边缘右移 20 个逻辑像素。放大和缩小都受 0.25 至 4 的范围限制，到达上限后再按放大，倍数不变，窗口也不动。

窗口失去焦点时，未固定的窗口直接关闭，不是隐藏。判断依据是 `ActivationChange` 事件：窗口激活过一次之后再变为非激活，就调用关闭。之所以关闭，是因为没有任何命令能让隐藏的覆盖窗口重新出现：下一次截屏会新建窗口，展示器只认最近一次截屏的会话。隐藏的窗口只会继续占用截图的像素数据，并接收已经没人看的刷新。关闭后展示器丢弃这个会话之后到达的消息（`CAP-06`）。

展示器的会话规则：最近一次 `CaptureCompleted` 的会话号是当前会话，新截屏到达时关闭上一个窗口，属于其他会话的消息一律丢弃。`OcrCompleted` 之前到达的同一会话的 `SegmentsUpdated` 与 `SessionProgress` 先暂存，显示识别结果时一并应用，展示器因此不依赖这几条消息的到达顺序。

#### 11.5.6 文字选择

覆盖窗口里显示的原文或译文可以像普通文字一样逐字选中（`OV-05`）。覆盖窗口是自绘的 `QWidget`，没有现成的文本控件可以借用，选择由窗口自己实现，分成排版、命中测试、选择模型、绘制和复制五部分。

##### 排版

每段的显示文字用一个 `QTextLayout` 排版，绘制、选中底色和鼠标命中测试共用这一份排版结果。原来的绘制用 `QPainter.drawText`，它在内部排版，排版结果不对外公开；另做一份近似的排版来做命中测试，两份结果在换行位置或行的纵坐标上差一点，选中的范围就会和画出来的字错开。共用一份排版后，字形的位置与选区的位置来自同一组行和同一组横坐标，两者按构造一致。

`layout_text` 复现 `drawText` 配 `_TEXT_FLAGS`（左对齐、竖直居中、按词换行）时的排版规则，字号适配仍按 `drawText` 的规则测量（11.5.3 节），两边换行位置相同。设文字区域的设备像素宽高为 $w_t$、$h_t$，字体的 leading 为 $\ell$，第 $i$ 行的行高为 $h_i$。排版逐行进行：

1. 每行的行宽取 $w_t$，按词边界换行；
2. 第 $i$ 行顶边的纵坐标 $y_i$ 由前一行递推，起点 $u_0 = -\ell$：

$$
y_i = \lceil u_{i-1} + \ell \rceil,\qquad u_i = y_i + h_i
$$

3. 某一行排完后 $u_i \ge h_t$ 时停止，后面的行不排，`drawText` 同样不画这些行；
4. 设最后一行排完后的 $u$ 为文字总高 $H$，整体在文字区域里竖直居中，第一行顶边相对文字区域顶边的偏移是：

$$
t = \frac{h_t - H}{2}
$$

换行符换成 Unicode 行分隔符（U+2028）、回车换成空格，与 `drawText` 的预处理相同。两种替换都是一个字符换一个字符，排版里的字符位置与显示文字的字符位置一一对应，选区可以直接按显示文字的下标记录。

排版结果按段缓存在 `TextBox` 里，缓存键是（显示的文字、字号、是否标题、文字区域的设备像素宽高）。用到一段时先比较缓存键，键不同就重新排版。字号适配分帧进行（11.2 节）：一段先用估算字号显示，测完后字号变化，缓存键随之变化，下一次绘制或命中测试时按新字号重新排版。缩放与换屏改变 $k$，文字区域的设备像素宽高随之改变，缓存键同样变化。显示文字变化（换译文、切换原文与译文）也改变缓存键。所以缓存不需要单独的失效通知。过渡中的旧文字另用一份缓存，过渡结束时丢弃。

##### 命中测试

`hit_test` 把窗口逻辑坐标 $(x, y)$ 换成文字位置（段号、字符位置）：

1. 找段：先找包围盒包含该点的段；不在任何段上时，取包围盒到该点距离最近的段。拖过段与段之间的空白时，选择因此照常延伸到最近的段；
2. 换到排版坐标：设该段文字区域左上角的设备像素坐标为 $(x_t, y_t)$，窗口所在屏幕的设备像素比为 $r_d$，则

$$
x_\ell = x\, r_d - x_t,\qquad y_\ell = y\, r_d - y_t - t
$$

3. 找行：$y_\ell$ 在第一行上方时取文字开头，在最后一行下方时取文字末尾，否则取 $y_\ell$ 所在的行；
4. 找字符：拖选用 `QTextLine.xToCursor` 的 `CursorBetweenCharacters`，得到离 $x_\ell$ 最近的字符间隙；双击、三击用 `CursorOnCharacter`，得到 $x_\ell$ 落在其上的字符。

##### 选择模型

选择由两个文字位置表示：锚点（按下处）与插入点（当前位置）。两个位置按阅读顺序比较，阅读顺序是 `visible_items` 的顺序，即段的显示顺序；同一段内比较字符位置。设排好序后起点是第 $a$ 段的位置 $p$，终点是第 $b$ 段的位置 $q$，各段的选中范围是：

| 段 | 选中范围 |
|---|---|
| $a = b$ 时的这一段 | $[p, q)$ |
| 第 $a$ 段（$a < b$） | $[p, \text{段尾})$ |
| 第 $a$ 与第 $b$ 段之间的各段 | 整段 |
| 第 $b$ 段（$a < b$） | $[0, q)$ |

范围为空的段不算选中。图表数据表收起后，锚点或插入点所在的段不再显示，选择随之为空。

选择的单位有三种，双击与三击按单位延伸：

- 字符：拖动开始时，按下处的字符间隙成为固定端；
- 词：同一按键双击时选中按下处的词，词边界按 `QTextBoundaryFinder` 的 Word 类型（Unicode UAX #29）。西文按单词与空白、标点分开；这个规则不带词典，汉字与假名每个字各成一词，与双语窗口译文区（`QTextEdit`）的双击结果一致；
- 段：双击之后、在系统双击间隔内于同一位置（偏差不超过 4 个逻辑像素）再按一次算三击，选中整段。

双击或三击后不松开继续拖动时，固定端是最初选中的词或段，另一端取当前位置所在单位的边界：当前单位在固定单位之前时，锚点取固定单位的末尾、插入点取当前单位的开头；否则锚点取固定单位的开头、插入点取当前单位的末尾。按字符拖动是这条规则在单位为空范围时的特例。

以下情况清除选择：

- “选择文字”按键单击（按下后移动不超过 4 个逻辑像素）；
- 切换原文与译文：选择针对的是当前显示的文字，换了文字，字符位置失去意义；
- 识别出新结果；
- 选中范围内的段换了译文，原因同上。选中范围以外的段换译文不影响选择。

##### 绘制与复制

选中底色在画文字之前填充，颜色是主题强调色，alpha 为 70。底色按字符范围画：对排版里的每一行，取选中范围与这一行字符区间的交集，用 `QTextLine.cursorToX` 求出交集两端的横坐标，填充从左端到右端、高度为行高的矩形，再整体平移到 $(x_t, y_t + t)$。`selection_rects` 返回的就是这些矩形，测试用它核对底色是否落在对应的字形上。

复制用系统标准的复制键（`QKeySequence.StandardKey.Copy`）。`keyPressEvent` 先查键位表，没有匹配的操作时再判断是不是标准复制键；用户在键位表里把 `Ctrl+C` 绑定给别的操作时按键位表执行。复制的内容是各段的选中部分按阅读顺序用换行连接，经 `OverlayActions.copy` 交给展示器，展示器调用模块注入的剪贴板写入函数。测试注入记录函数，不改动系统剪贴板。

### 11.6 双语窗口

双语窗口位于 `lst/ui/bilingual`，分成展示器和视图两层。展示器不等待任何异步操作：输入停顿用单次 `QTimer`，发命令用只入队的 `bus.send`，订阅处理函数都是普通函数。qasync 让总线回调、Qt 信号槽和计时器在主线程上依次执行，所以展示器的状态不需要加锁。

#### 11.6.1 翻译服务与发送规则

双语窗口只用一个翻译服务（`BI-04`）。顶栏的“翻译服务”下拉框列出启用的引擎实例，按优先级排列，数据来自 `EngineStatuses`（状态、名称、优先级、计费标记）：

- 计费的实例在名称后面标“（计费）”。选中计费服务后，输入停顿照样自动翻译，每次停顿都会产生一次计费请求，下拉框的提示写明这一点；
- 出错的实例标“（出错）”并且不能选，未启用的实例不列出；
- 用户选过的服务记在 `ui_state.ini` 的 `bilingual_window/engine` 键下。记住的服务停用、删除或出错时，窗口改用列表里第一个能选的服务，并在译文区下面的状态行写明“‘某服务’现在不可用，暂时改用‘另一服务’”；记住的选择不变，它恢复可用后自动换回。没有选过时用列表第一项，即优先级最高的服务。没有任何能用的服务时状态行提示去设置里启用一个，请求的 `engine_id` 为空，各段按“没有支持这个语言对的引擎”显示原文。

发送规则：

- 输入停顿用单次 `QTimer` 判断，时长取 `[bilingual].debounce_ms`（默认 400 毫秒），到时发送 `TranslateText(engine_id=选中的服务)`。
- 去重比较四项：去掉首尾空白的文本、源语言、目标语言、服务。四项都没变时停顿不重发。
- 用户按立即翻译（默认 `Ctrl+Return`）时，先停止停顿计时器，再以新会话号发送当前文本。文本没变时也重新翻译，上次失败后可以借此重试；命中译文缓存的段不会再次请求服务。
- 输入清空时立即发送空文本（新会话号），竞速模块取消当前会话；上一次发出的已经是空文本时不再发送。
- 切换语言或服务时立即重发。交换语言时，两边都是“自动”则不动，只有一边是“自动”照样交换。

竞速模块收到 `TranslateText` 后只把段派发给 `engine_id` 指定的那个实例（`RaceScheduler.start_session` 的 `engines` 参数），所以双语窗口里每段只有一个尝试，不存在优先级替换；截屏翻译不限定实例，全部可用实例参加竞速，覆盖窗口按优先级替换显示（第 6 章）。早期版本的双语窗口让免费实例自动参赛、计费实例在按 `Ctrl+Enter` 时“晚加入”，并有 `[bilingual].auto_billing` 开关；这条规则连同晚加入一起删除，旧配置里的 `auto_billing` 读取时丢掉。

#### 11.6.2 译文区

- 只接受自己最新会话的 `SegmentsUpdated`，截屏会话和旧会话的刷新都丢弃。
- 译文区是只读的 `QTextEdit`，一段对应一个文本块，段内换行转成 U+2028，段号直接换算成块号。一次刷新的全部改动放在一个编辑块里，文档只重新排版一次。
- 新会话的第一帧整体替换文档内容，但不用 `setPlainText`：它会把滚动条拉回顶部，用户边输入边看长文末尾时，每次停顿都会跳回开头。
- 段的译文换了内容时淡入（`BI-06`），文字透明度在 150 毫秒内从 0 升到 1。同一次刷新里开始淡入的段共用一个动画，每帧只改一次文档；一段再次淡入时只离开它原来所在的组，其他组照常计时。
- 双语窗口只用一个服务，段旁不画来源标记；早期版本的色条和 `[race].show_source_marker.bilingual` 开关已经删除。
- 译文区下面是一行状态文字：服务回退或没有可用服务的说明优先；否则某段的尝试失败且没有译文时写出失败原因与处理建议（`error_text`，不含技术细节），某段因为选中的服务不支持语言对而显示原文时写明这一点；都没有时隐藏。
- 占位规则：语言与服务都没变时，新会话里原文没改动、还没有译文的段，先沿用上一个会话显示的译文；新结果与占位相同时不播放淡入。没有这条规则时，每次停顿所有没改动的段都会先闪回原文，再换成缓存译文并淡入一次。竞速结束仍没有胜出者的段按 `RACE-04` 回到原文。

#### 11.6.3 引擎结果列表（已删除）

早期版本在译文区下方有一个引擎结果列表，每个参赛实例一行，显示状态、耗时和译文。双语窗口改为只用一个服务之后，列表只剩一行，信息都已在译文区和状态行里，所以整块删除，连同它的控件、文案和测试。

#### 11.6.4 窗口状态与键盘

- 窗口的位置、大小、置顶状态、手动选择的两种语言和选中的翻译服务保存在 `data/ui_state.ini`（`QSettings` 的 INI 格式，`bilingual_window` 组），下次打开时恢复（`BI-01`）。这些属于界面状态，与用户配置分开存放；文件路径由组合根通过 `AppPaths.ui_state_file` 传入。
- 窗口隐藏、模块停止、切换置顶、切换语言时写入并立即落盘。本次运行从没显示过窗口时，模块停止不覆盖已保存的位置。
- 恢复位置时，外框顶边中点下方 12 像素处不在任何屏幕的可用区域内，就移到主屏中央；窗口比主屏大时先缩小。
- 标题栏关闭按钮和 `Alt+F4` 只隐藏窗口，并保存状态。置顶切换在原生窗口已创建时用 `QWindow.setFlags`，避免 `setWindowFlags` 先隐藏再重建造成的闪烁。
- 去掉置顶标志要按整数运算：`Qt.WindowType(int(flags.value) & ~int(hint.value))`。PySide6 6.12 的枚举取反 `~Qt.WindowType.WindowStaysOnTopHint` 得到 `0x1FBFFFF`，只覆盖 25 位以内的标志，`flags & ~hint` 会顺带清掉 `WindowCloseButtonHint`（`0x08000000`）。早期实现就是这样写的：用户打开再关闭置顶后，Windows 按缺少关闭按钮标志把系统菜单的“关闭”置灰，标题栏的关闭按钮随之变灰（实测 `GetMenuState(SC_CLOSE)` 返回 `MF_GRAYED`）。
- 窗口不在任务栏占按钮。Windows 有两种做法：给窗口加 `WS_EX_TOOLWINDOW`（`Qt.WindowType.Tool`），或者给它一个隐藏的所有者窗口。前者同时把窗口从 Alt+Tab 列表里去掉，标题栏变成工具窗口样式，只剩关闭按钮；后者只影响任务栏按钮，Alt+Tab、激活、置顶、Mica 背景和普通标题栏都不变。所以选后者：`SecondaryWindow(taskbar=False)` 在首次显示前创建一个只有原生句柄、从不显示的 `QWindow`，用 `QWindow.setTransientParent` 挂成双语窗口的所有者，Qt 在 Windows 上把它写成 `GWLP_HWNDPARENT`。有所有者的窗口最小化后没有任务栏按钮可以点回来，只会缩成桌面左下角的一小条标题栏，所以这类窗口去掉最小化按钮（`CustomizeWindowHint` 加标题、系统菜单、最大化与关闭按钮），隐藏窗口用关闭按钮、`Esc` 或热键，托盘与热键照常唤出。实测（`QT_QPA_PLATFORM=windows`）：所有者存在，扩展样式没有 `WS_EX_TOOLWINDOW`，`DWMWA_SYSTEMBACKDROP_TYPE` 仍为 Mica，置顶开关多次后关闭按钮保持可用。
- 键盘：立即翻译、交换语言、置顶、清空、关闭窗口五个操作由键位表驱动，默认键位见 11.8 节。关闭窗口与标题栏的关闭按钮走同一条路径，只隐藏窗口并保存状态；交换、置顶、清空调用按钮的 `click`，与鼠标点击按钮的路径相同。
- 按键要在子控件之前拦下：用户可以把操作改到输入框自己处理的组合上，例如 `Ctrl+A`。窗口显示期间在 `QApplication` 上装一个事件过滤器，发给本窗口及其子控件的按键、鼠标按下与双击先查键位表，匹配到操作才拦截并执行，其余照常交给控件；窗口隐藏时卸下过滤器，其他窗口的事件不经过它。焦点在可编辑的文字控件或下拉框里时，不带 `Ctrl`、`Alt` 的可打印字符一律交给控件，用户把操作绑到单个字母上时，打字也不会被吃掉。不用 `QShortcut`，因为它要求窗口处于激活状态。
- 复制不在键位表里。复制全部译文用“复制译文”按钮。译文区（只读的 `QTextEdit`）设了 `TextSelectableByMouse | TextSelectableByKeyboard`，选中的部分用系统标准的复制键（`Ctrl+C`、`Ctrl+Insert`）复制，由控件自己写剪贴板。前面的事件过滤器只拦截匹配到键位的按键，默认键位里没有 `Ctrl+C` 与 `Ctrl+Insert`，这两个键照常交给输入框和译文区；用户把 `Ctrl+C` 绑定给某个操作时，按键位表执行。
- 按钮提示和输入框的占位文字按当前键位显示，例如“交换源语言与目标语言（Ctrl+Shift+S）”；没有绑定的操作只显示说明文字，“复制译文”按钮的提示不写键位。按钮不用 `&X` 助记键，中文文案会一直显示括号字母。Tab 顺序与视觉顺序一致：源语言、交换、目标语言、翻译服务、置顶、输入框、清空、译文区、复制译文。

#### 11.6.5 实测

`REL-04` 要求界面刷新不阻塞输入。下表是总线处理函数本身的耗时，随后一轮事件循环里的布局与绘制都在 4 毫秒以内：

| 操作 | offscreen | 原生平台 |
|---|---|---|
| 新会话第一帧（100 段重建） | 3.0 毫秒 | 5.7 毫秒 |
| 100 段首个译文（全部淡入） | 2.6 毫秒 | 3.5 毫秒 |
| 100 段换成更高优先级的译文 | 4.1 毫秒 | 8.0 毫秒 |
| 淡入动画的一帧 | 0.7 毫秒 | 1.1 毫秒 |

### 11.7 设置窗口与首次运行引导

设置窗口由 `ui.settings` 用普通 Widgets 实现 Fluent 风格（2.2 节）：左侧是两级导航树（`nav.NavTree`），右侧是 `QStackedWidget` 承载的各页，页内的设置项是带标题、说明和控件的卡片，样式全部取自 `ui.theme`。导航结构见本节的“两级导航”，各页按 `UI-05` 划分，页面的要点是：

两级导航：

左侧导航分两级，共七个一级项、十七个页面。一级项有图标，“热键与键位”“截屏与识别”和“模型”有子页，其余四项没有子项、本身就是页面。页面标识对一级页面用 `general` 这样的名字，对二级页面用“一级.二级”的形式。

| 一级项 | 二级页面（页面标识） | 内容 |
|---|---|---|
| 常规 | 无（`general`） | 界面语言、主题、存储位置（数据目录与模型目录） |
| 热键与键位 | 全局热键（`hotkeys.global`） | 截屏翻译与双语窗口两个全局热键 |
| | 截屏蒙版（`hotkeys.capture`） | 截屏蒙版里两个操作的键位 |
| | 覆盖窗口（`hotkeys.overlay`） | 覆盖窗口里九个操作的键位 |
| | 双语窗口（`hotkeys.bilingual`） | 双语窗口里五个操作的键位 |
| 截屏与识别 | OCR 引擎（`capture.engines`） | 三个引擎的卡片：下载、卸载、运行环境版本、设为当前引擎、管理 |
| | Windows OCR（`capture.windows`） | 运行环境说明与可识别的语言、识别流水线的流程图，没有可选模型与参数 |
| | RapidOCR（`capture.rapid`） | 运行环境说明、识别流水线的流程图与各步骤的设置、识别参数 |
| | PaddleOCR（`capture.paddle`） | 运行环境说明、识别流水线的流程图与各步骤的设置、识别参数 |
| | 结构归一化（`capture.normalize`） | `[ocr_normalize]` 节的步骤开关与参数 |
| | 截图与识别选项（`capture.options`） | `[ocr]` 节的启用、模式、设备、语言、预处理，`[screenshot]` 节的目标语言 |
| 翻译引擎 | 无（`engines`） | 引擎实例列表与参数 |
| 竞速 | 无（`race`） | 覆盖窗口的来源标记、双语窗口设置、竞速统计 |
| 模型 | 本地翻译模型（`models.nmt`） | 本地翻译的流程图，各步骤的说明与设置（本地翻译模型的单独目录、本地 NMT 模型列表、集束宽度） |
| | 显卡加速（`models.gpu`） | 推理设备、NVIDIA cuBLAS 12 运行库 |
| | 词典（`models.dicts`） | 词典包列表与署名 |
| 关于与诊断 | 无（`about`） | 版本、更新、诊断 |

导航的行为如下：

- 点有子项的一级项，展开它并显示第一个子页；同一时间只展开当前页所在的分支。页面标题写成“一级 › 二级”，例如“截屏与识别 › PaddleOCR”。
- 键盘：`Ctrl+1` 至 `Ctrl+7` 切到第 n 个一级项，有子项时到它的第一个子页；`Ctrl+Tab` 与 `Ctrl+Shift+Tab` 按子页的显示顺序循环；在导航树里，上下键在可停留的行之间移动（已展开的一级项由它的子项代表），右键展开，左键收起并回到一级项，页面不变；`Esc` 关闭窗口（`UI-07`）。
- `show_page` 接受一级或二级页面标识。旧版本只有七个一级页面，旧的一级标识（`hotkeys`、`capture`、`models`）映射到该分支的第一个子页，例如 `hotkeys` 映射到 `hotkeys.global`，托盘、首次运行引导等调用方不用修改。
- 窗口把上次停留的子页标识记在 `ui_state.ini` 的 `settings_window/page_id` 键下。旧版本保存的页面序号（`settings_window/page`）读取时按旧的七页顺序映射到对应分支的第一个子页；两个键都认不出时显示“常规”。
- 配置被拒绝（`ConfigRejected`）时，错误显示在负责那个位置的卡片上。窗口可见、而当前页没有卡片负责它时，窗口切到负责它的子页（`SettingsWindow.show_location`）。`[bindings]` 节的字段属于哪个窗口，就切到哪个窗口的键位子页，例如 `bindings.overlay_zoom` 被拒绝时切到 `hotkeys.overlay`。几个页面共用的 `[ocr]` 节字段（例如文本行方向同时出现在 RapidOCR 与 PaddleOCR 子页）优先切到当前 OCR 引擎的子页。没有任何页面负责的位置显示在窗口底部的提示条里。

“热键与键位”分四个子页（`lst/ui/settings/pages.py` 的 `GlobalHotkeysPage`，`page_bindings.py` 的 `BindingsPage`）：

- “全局热键”页（`hotkeys.global`）是 `[hotkeys]` 节的截屏翻译与双语窗口两个热键卡片；
- 三个窗口各一个子页，页面标识是 `hotkeys.<窗口>`，窗口取 `lst.ui.common.bindings.Scope` 的值。每页只列出 `ACTIONS` 里属于这个窗口的操作，一个操作一张卡片，录制、手动输入、冲突提示与“替换”、单个操作的恢复默认都在卡片上（11.8 节）。冲突只在同一个窗口里判断，同一个窗口的操作都在同一页，所以冲突提示与替换不会跨页；
- 每个窗口子页右上角的“恢复本页默认”先确认，再在一条 `UpdateConfig` 里提交这个窗口里与默认值不同的字段，其他窗口的键位不动。同一个窗口的默认键位互不冲突，恢复后的整节一定能通过校验。

“常规”页的“存储位置”一组是数据目录与模型目录两张卡片（`page_storage.py`），内容来自组合根发布的 `StorageLocations`（2.9.5、8.5.3 节）：

- 卡片右侧是“更改…”与“打开”，模型目录不在默认位置时多一个“恢复默认位置”；下方依次是当前路径、状态行与进度条。状态行写明是否默认位置、重启后改用的目录、进行中的复制或移动进度，或数据目录不能更改的原因；
- “更改…”先经 `Dialogs.choose_directory` 选目录，再经 `Dialogs.confirm_option` 确认：数据目录的确认框带“把现有数据复制到新位置”，模型目录的带“把现有模型移动到新目录”，都默认勾选。确认后发 `ChangeDataDir` 或 `ChangeModelsDir`。选回当前数据目录时不再确认，直接撤销还没生效的更改；
- “打开”经 `Dialogs.open_path` 交给资源管理器；数据目录等待重启时，状态行旁边有“立即重启”，发 `RestartApp`；
- 复制或移动期间两张卡片的“更改…”都不可用；更改失败的原因（`StorageFailure`）显示在对应卡片的下方。对话框都经 `Dialogs` 协议调用，测试换成记录调用的假实现。

本地翻译模型页“取模型”节点面板里的目录卡片对应 `[nmt].model_dir`，标题是“本地翻译模型的单独目录”。留空时输入框的占位文字是实际使用的 `<模型目录>\nmt`，说明里写明模型目录在“常规 › 存储位置”里更改，这张卡片只用于直接使用别处已有的模型文件（8.5.3 节）。

- 翻译引擎页用支持拖拽的列表。放下后，按新顺序把优先级重新编号为 1、2、3……，再发送 `UpdateConfig`。
- 表单的字段和约束都来自 pydantic 模型，配置服务返回的 `ConfigRejected` 显示在对应字段旁边（`UI-06`）。
- 自动保存的时机：开关、下拉框、拖拽排序在操作完成时立即提交；文本框在停止输入 500 毫秒后提交，避免每按一个键就重建一次引擎实例。

`ui` 模块不能依赖 `services`，所以各配置节的 pydantic 模型和各类引擎的参数模型由组合根在构造 `SettingsModule` 时注入。设置窗口据此生成卡片：字段类型决定控件，取值范围和默认值来自模型，某节没有注入时依赖它的卡片不显示。

翻译引擎页的实现要点：

- 配置服务会把校验失败的 `[[engines]]` 条目整条丢弃，数组节又是整体替换的，所以界面在发送之前先用该类引擎的模型校验整条记录，不合法就不发送，原因显示在对应参数旁。
- 连续两次修改（例如排序后紧接着改名）时，第二次在第一次的发送内容上构造，直到新的 `AppConfig` 确认、收到引擎相关的 `ConfigRejected`，或超过 2 秒。
- 拖拽排序之外另有“上移”“下移”按钮，供键盘操作（`UI-07`）。实例的参数显示在列表下方的面板里，没有做成行内展开。
- API key 只经 `UpdateSecret` 提交，去掉首尾空白，只在回车或失去焦点时提交（不受文本框 500 毫秒停顿提交的影响，避免半截 key 写进凭据管理器），提交后清空；只有空白时不提交，删除 key 只走“清除”按钮，界面只显示“已设置/未设置”。判断依据是引擎模块发布的状态：启用且缺 key 的实例处于 `engine.auth` 错误；没启用的实例不读 key，显示“启用后显示状态”。新增实例的 keyring 条目名等于实例号；删除实例时，没有别的实例引用的 key 一并删除。
- “测试连接”用实例当前配置翻译固定词 “Hello”，先试英译中，不支持时试中译英，不经过调度器和缓存。缺 API key 时不发请求，直接返回 `engine.auth`。
- 谷歌与微软免费接口只有一个专有参数“接口地址”，它的说明写明接口没有公开文档、可能变动或限流、谷歌翻译在中国大陆不可用，以及启用后文本会发给对应的服务（`ENG-09`）。

“截屏与识别”分六个子页（`lst/ui/settings/page_capture.py`、`page_ocr_engine.py`、`page_ocr_flow.py`、`page_ocr_pipeline.py`）。

“OCR 引擎”页（`capture.engines`）是三个引擎卡片，内容由 `ocr_engine_rows.build_view` 按 `OcrEngineCatalog` 给出：

- 说明、状态文字、可识别语言、出错时的处理建议，以及随状态变化的按钮：下载、取消、重试、卸载、设为当前引擎。当前引擎的卡片画一圈强调色边框，显示“当前引擎”标识而不是“设为当前引擎”；
- 引擎有两个以上可装版本时，卡片上有运行环境版本的下拉框（`OCRE-07`）。推荐版本在选项里标出，下拉框下面写出推荐理由，例如“推荐：CUDA（检测到 NVIDIA 显卡）”。未安装或安装失败时，下拉框选的是要安装的版本，默认是推荐版本；排队、下载、安装中下拉框不可用；已安装时状态文字后面写出已装版本，下拉框默认选中已装版本，选了别的版本后“更换版本”可用，确认后发送带版本的 `InstallOcrEngine`，只重装运行环境；
- 已安装（或不需要安装）的引擎有“管理”按钮，点了直接切到该引擎的子页。

三个引擎子页（`capture.windows`、`capture.rapid`、`capture.paddle`，共用 `OcrEnginePage`）各自固定显示自己引擎的内容，不随当前引擎变化。从上到下是：

- 运行环境卡片：引擎名、状态与已装的运行环境版本、可识别的语言。引擎没有安装时，下面多一张说明卡片，写明要先在“OCR 引擎”页下载，并带“转到 OCR 引擎页”按钮。引擎不是当前引擎时，页面写明这里的修改在它设为当前引擎后生效；
- 识别流水线（`page_ocr_flow.EnginePipeline`）：流程图按代码的执行顺序画出这个引擎的每一步，节点定义见 11.7.2 节。点开环节节点，图下方显示这个环节的 `StageSection`，列出它的可选模型（`ocr_pipeline_rows`）。每行是一个单选按钮，旁边是状态、大小、覆盖的语言与“默认”标记，右边是下载、取消、删除按钮。选用一个模型就发送 `UpdateConfig {"ocr_<引擎>": {"<环节>_model": 模型 id}}`；未下载的模型不能选用。删除选用中的模型时先确认，把该环节改回默认模型，再发 `DeleteOcrModel`（`OCRE-08`）；
- 识别参数：该引擎配置节的参数按环节归属，挂在对应环节节点的面板里，不属于任何环节的参数在流程图下方。常用参数直接显示，带 `advanced` 标记的参数放在“高级参数”折叠区里。每项右边有“恢复默认值”，标题行右边的“恢复默认”把这一组参数恢复为默认值，选用的模型不变（`OCRE-09`）。

可选环节在 `StageSection` 的标题行右侧有开关，绑定 `[ocr]` 节里与环节同名的字段，改动只发送 `UpdateConfig {"ocr": {字段: 值}}`。PaddleOCR 子页有七个开关：文本行方向（`textline_orientation`）、文档方向校正（`doc_orientation`）、文档去畸变（`doc_unwarping`）、版面区域检测（`region`）、表格（`table`）、公式（`formula`）、图表（`chart`）；RapidOCR 子页只有文本行方向；Windows OCR 子页没有开关。配置节里没有对应字段时不显示开关。环节关闭时模型选择与下载变灰，说明里注明“已关闭，识别时跳过这一步”，开关本身仍可操作。`StageSection` 的开关、模型选择、下载与删除都封装在组件内，不依赖所在页面，页面可以把它放在任何位置。

Windows 自带 OCR 没有可选模型，也没有参数，它的子页是运行环境卡片、流程图和一句说明。

“结构归一化”页（`capture.normalize`）是 `[ocr_normalize]` 节的步骤开关与参数，同样分常用与高级（`OCR-11`）。步骤关闭时它的参数不可用。组合根没有注入这一节时，页面只留一句说明。

“截图与识别选项”页（`capture.options`）是 `[ocr]` 节的启用、识别模式、推理设备、识别语言、灰度化与缩放上限，以及 `[screenshot]` 节的目标语言。表格、公式、方向校正等开关不在这里，它们在引擎子页的环节里，同一个设置只出现在一处。当前引擎只有文本模式（Windows 自带 OCR、RapidOCR）时，只对 PaddleOCR 生效的设置项（识别模式、推理设备）整张卡片不可用，说明后面注明“仅 PaddleOCR 可用”，配置里的值保留。识别语言的下拉框只列出当前引擎报告的可识别语言，前面加“自动”；PaddleOCR 按识别语言自动换专用识别模型，下拉框列出全部语言。

每种引擎的卡片、每个引擎的环节与参数卡片都在构造时建好，只切换显示，焦点与 Tab 顺序不随状态变化；模型行随引擎目录里的模型列表重建，重建后接回焦点链里环节卡片的位置（`UI-07`）。设置窗口的展示器还没有发送模型命令与带版本安装命令的方法，这几条命令由页面的 `OcrActions` 直接经展示器的总线发送，与展示器其他命令一样先检查窗口是否已关闭。

“模型”分三个子页（`lst/ui/settings/page_models.py`）：

- “本地翻译模型”页（`models.nmt`）：本地翻译的流程图，节点定义见 11.7.3 节。模型列表（带筛选框与“只显示已安装的”）在“CTranslate2 推理”节点的面板里，默认打开；本地翻译模型的单独目录在“取模型”节点的面板里；
- “显卡加速”页（`models.gpu`）：推理设备下拉框与 NVIDIA cuBLAS 12 运行库卡片；
- “词典”页（`models.dicts`）：词典包列表（`MOD-05`），列表上方是总署名与 `THIRD-PARTY-NOTICES` 的提示，每个词典包一行，显示大小、许可证和状态，它的下一行是这个包的署名全文（8.5.2 节）。词典页有自己的筛选框。署名行跨全部列，`QTreeWidget` 按第一列的宽度估算行高，长署名会被截断，所以页面按视口宽度自己计算换行后的高度，写进署名行的 `sizeHint`。模型管理模块不管理词典包时只显示说明，不显示列表。

打开本地翻译模型页或词典页时获取一次模型索引（`PRV-01`）。

删除实例、清空缓存、重置统计、删除模型、恢复本页键位、更改数据目录与模型目录都先弹确认框。竞速页的统计表列出执行段数、胜出次数、胜出率、耗时中位数、耗时 P95 和失败率。

还没有实现的部分：

- 常规页的日志级别：没有对应的配置节，目前只能用命令行参数 `--log-level` 设置；
- 关于页的依赖与 GPU 信息：总线上没有这类数据；
- 导出诊断信息：`ExportDiagnostics` 命令尚未定义，按钮禁用。

首次运行引导（`UI-08`）由 `ui.onboarding` 实现，目标是逐项显示以下检测结果：

- OCR 进程 `ready` 消息中的 GPU 与 CUDA 信息；
- `ModelCatalog` 中的本地 NMT 模型与词典包，以及 OCR 模型；
- 本地 LLM 服务的连通性。

用户在引导里配置的第一个引擎实例，通过 `UpdateConfig` 和 `UpdateSecret` 提交。

目前实现的是本地 NMT 模型与词典包两项。引导按互译对列出需要的模型（每个方向优先用直连模型，没有直连时用经英语中转的两个模型，与 NMT 进程的中转规则相同），显示大小、下载进度和失败原因，用户一键下载。模型管理模块管理词典包时，模型下面再列出互译对两个方向的词典包，每行带许可证与署名；“同时下载词典”默认勾选，勾选时“下载”一并下载这些词典包。词典是可选的，“完成”只看本地翻译模型是否装齐。展示器不依赖 Qt，生成纯数据的画面描述，由视图绘制。

完成状态记在 `data/ui_state.ini` 的 `onboarding/completed` 键下，与双语窗口共用一个文件。点“完成”或“跳过”记录完成；点标题栏的关闭按钮只推迟，下次启动还会出现。已安装但有新版本的模型只显示“可更新”，引导不负责升级。

还没有实现的部分：

- GPU 与 CUDA 检测；
- 本地 LLM 服务的连通性检测；
- 在引导里配置第一个引擎实例；
- OCR 模型。OCR 模型不在 `ModelCatalog` 里，由 `services.ocr_engines` 随引擎在设置的“截屏与识别”页下载（8.6 节），引导里只有一行说明文字。

#### 11.7.1 流程图组件

OCR 引擎子页与本地翻译模型页用流程图展示处理流水线（`UI-05`）。流程图组件在 `lst/ui/settings/flow.py`，节点与顺序在 `lst/ui/settings/flow_specs.py`。组件只负责画图和收发用户操作，节点对应哪个设置、开关写哪个字段由页面决定。

流程的结构由两种元素组成：

- `FlowStep`：一个节点；
- `FlowBranch`：一处分支。前一个节点之后分成几条并列的路，每条路是一串节点，几条路在下一个节点之前汇合。

每个节点的显示内容是 `NodeState`：图标、名称、一行状态（所用模型、已开启或已关闭、CPU 或显卡）、可选的开关，以及是否被跳过。

排版由纯函数 `layout_flow` 完成，输入是流程结构、可用宽度和节点尺寸，输出是每个节点的矩形与全部连线，不依赖 `QApplication`，可以单独测试。规则如下：

1. 节点从左到右排列，一行放不下时换行。分支作为一个整体参与换行，不会被拆到两行。
2. 同一行相邻两个块之间画一条带箭头的直线，从前一个节点的右边缘中点画到后一个节点的左边缘中点。
3. 换行处的连线从上一行末尾节点的右侧伸出，向下走到两行之间，沿水平方向折回左侧留白，再向下接到下一行第一个节点的左侧，箭头指向它。折线只穿过空白，不压在任何节点上。
4. 分支的入口处分叉，每条路的第一个节点前有箭头；各条路的末端汇合到出口，出口之后的连线带箭头指向下一个节点。
5. 被跳过的节点变淡，边框改为虚线，进出它的连线也变淡。它上方画一条强调色的虚线：从它前面的连线上引出，越过它，落回它后面的连线上，表示数据直接流向下一个节点。同一行里相邻的几个被跳过的节点共用一条绕行虚线；一串被跳过的节点跨过换行时，每行各画一条。

全部节点用同一个宽度，按最长的名称与状态计算，限制在 136 至 176 像素之间，超出部分省略并在提示里显示全文。颜色在每次绘制时取自 `ui.theme` 的 `theme_colors`，深浅色切换后立即正确。

`FlowView` 把流程图与设置面板组合在一起。页面先创建 `FlowView`，再为每个节点创建面板并用 `add_panel` 交给它，所以焦点链里节点在面板之前。点开一个节点时，这个节点高亮，面板区只显示它的面板，其余面板隐藏。面板里放的是页面原有的设置组件（`StageSection`、`ParamsGroup`、设置卡片），流程图不复制设置逻辑。流程换了一组节点（例如 PaddleOCR 切换识别模式）而打开的节点不在新流程里时，改为打开页面指定的默认节点。

键盘与辅助功能按 `UI-07` 设计：

- 流程图在 Tab 顺序里只占一个位置，即当前节点。Tab 进入流程图时落在打开的节点上，再按 Tab 进入下方的面板。
- 方向键在节点之间移动，Home、End 到首尾。
- Enter 打开当前节点的设置；空格切换节点上的开关，节点没有开关时空格同样打开设置。
- 节点的可访问名称是节点名，可访问描述依次写出状态、开关状态、是否被跳过、“第几步，共几步”、设置是否已打开，以及上面的键盘操作。

节点上的开关只发出 `toggled(key, 新值)` 信号，不修改显示。页面写配置，配置变化后再刷新节点；用户刚切换、配置服务还没有确认的值先按用户的选择显示，收到 `ConfigRejected` 时恢复为配置里的值。

#### 11.7.2 OCR 引擎子页的节点

每个引擎子页的流程图由 `page_ocr_flow.EnginePipeline` 构建。PaddleOCR 按 `[ocr].mode` 在版面模式与文本模式两套流程之间切换，RapidOCR 与 Windows OCR 各只有一套。节点顺序取自代码的执行顺序，`tests/ui/settings/test_flow_pages.py` 读取下表各函数的源代码，核对调用的先后。

PaddleOCR 版面模式（PP-StructureV3）在 OCR 进程里的顺序取自 PaddleX 3.7 的 `LayoutParsingPipelineV2.predict`。表格、公式、图表三项在代码里依次执行：公式识别在整页文字识别之前，识别出的公式区域会被涂白，避免整页 OCR 把公式当成文字；表格与图表识别在整页文字识别之后。所以流程图把它们画成一条链，不画成并列的分支。

| 节点 | 进程 | 代码位置 | 开关 |
|---|---|---|---|
| 截图预处理 | OCR 进程 | `paddle_backend.PaddleBackend.recognize`：`plan_resize`、`cv2.resize`、`to_grayscale` | 无，参数在“截图与识别选项”页 |
| 文档方向校正 | OCR 进程 | PaddleX 文档预处理子产线的 `doc_ori_classify_model` | `[ocr].doc_orientation` |
| 文档去畸变 | OCR 进程 | 文档预处理子产线的 `doc_unwarping_model` | `[ocr].doc_unwarping` |
| 版面检测 | OCR 进程 | `layout_det_model` | 无 |
| 区域检测 | OCR 进程 | `region_detection_model` | `[ocr].region` |
| 公式识别 | OCR 进程 | `formula_recognition_pipeline` | `[ocr].formula` |
| 文字检测 | OCR 进程 | 整页 OCR 子产线的 `text_det_model` | 无 |
| 文本行方向 | OCR 进程 | `textline_orientation_model` | `[ocr].textline_orientation` |
| 文字识别 | OCR 进程 | `text_rec_model` | 无 |
| 表格识别 | OCR 进程 | `table_recognition_pipeline` | `[ocr].table` |
| 图表识别 | OCR 进程 | `chart_recognition_model` | `[ocr].chart` |
| 转成版面块 | OCR 进程 | `extract.extract_structure`、`convert.convert_structure` | 无 |
| 解码校验 | 主程序 | `lst/services/ocr/blocks.py` 的 `decode_blocks` | 无 |
| 清洗 | 主程序 | `normalize.CleanStep` | `[ocr_normalize].clean` |
| 合并相邻正文块 | 主程序 | `normalize.AggregateStep` 的相邻块合并 | `[ocr_normalize].merge_blocks` |
| 切分成段 | 主程序 | `segments.blocks_to_segments` | 无 |

图表识别只在显卡上运行（`paddle_backend.GPU_ONLY_FEATURES`）。OCR 进程报告的设备是 CPU 时，图表节点即使开着也画成被跳过，状态写“CPU 上跳过”。

版面模式的流程里没有“拆分跨栏长行”和“合并段落”。原因是 `SplitStep.apply` 在版面模式下原样返回输入，`AggregateStep` 在版面模式下只做相邻正文块合并。

PaddleOCR 文本模式由 `paddle_config.text_config` 配置，只包含文档预处理与整页 OCR 子产线。它的流程是：截图预处理 → 文档方向校正 → 文档去畸变 → 文字检测 → 文本行方向 → 文字识别 → 转成版面块（`extract_text`、`convert_text`），之后接文本模式的主程序部分。

文本模式的主程序部分是：解码校验 → 清洗 → 拆分跨栏长行（`SplitStep`，开关 `[ocr_normalize].split`）→ 合并段落（`AggregateStep` 的段落合并，开关 `[ocr_normalize].aggregate`）→ 切分成段。顺序来自 `OcrModule._complete` 与 `normalize.build_pipeline`。

RapidOCR 的流程是：截图预处理 → 文字检测 → 文本行方向（开关 `[ocr].textline_orientation`，即 RapidOCR 的 `use_cls`）→ 文字识别 → 转成版面块（`rapid_backend.build_blocks_json`），之后接文本模式的主程序部分。检测、方向分类、识别三步由 rapidocr 3.10 的 `RapidOCR.__call__` 依次执行。

Windows OCR 的流程是：截图预处理（缩放上限还要不超过 `OcrEngine.max_image_dimension`）→ 系统 OCR（`OcrEngine.recognize_async`，一次完成检测与识别）→ 拼成文本行（`winocr_backend.build_blocks`）。之后接文本模式的主程序部分。

点开节点后，面板的内容按节点类型决定：

- 环节节点：节点 key 是 `OcrStage` 的值，面板里是这个环节的 `StageSection`（选模型、下载与删除），以及归属这个环节的参数（`ParamsGroup`）。
- 截图预处理：面板是说明与“转到截图与识别选项”按钮，缩放上限与灰度在那一页设置。
- 清洗、拆分、两种合并：面板是说明与“转到结构归一化”按钮，阈值在那一页设置。
- 其余节点：面板只有说明。

环节节点上的开关与 `StageSection` 标题行里的开关绑定同一个 `[ocr]` 字段。切换节点开关时，页面点一下环节卡片里的开关，修改经同一个 `Binding` 提交，所以两处显示一致。结构归一化节点的开关经展示器写 `[ocr_normalize]` 节。配置里没有对应字段时，节点上不显示开关。

引擎配置节的参数按 `page_ocr_pipeline.param_stage` 归到环节。字段的 `json_schema_extra` 带 `ocr_stage` 时以它为准；没有时按字段名前缀判断，前缀表 `flow_specs.PARAM_STAGE_PREFIXES` 如下：

| 前缀 | 环节 | 依据 |
|---|---|---|
| `text_det_`、`det_` | 文字检测 | PP-StructureV3 的检测参数；rapidocr 配置的 Det 节 |
| `text_rec_`、`rec_`、`text_score` | 文字识别 | 识别分数阈值；rapidocr 配置的 Rec 节 |
| `cls_` | 文本行方向 | rapidocr 配置的 Cls 节 |
| `layout_` | 版面检测 | PP-StructureV3 的版面检测参数 |

不属于任何环节的参数（CPU 线程数、oneDNN、RapidOCR 的整体尺寸限制）在流程图下方单独成组，标题是“识别参数”。各环节的参数组用另一组助记键（“恢复默认”是 `S`，“展开”“收起”是 `X`），与下方的识别参数组同时可见时不冲突。

#### 11.7.3 本地翻译模型页的节点

本地翻译模型页（`models.nmt`）上面是本地翻译的流程图，下面是被点开的节点的面板，默认打开“CTranslate2 推理”。

| 节点 | 进程 | 代码位置 | 面板 |
|---|---|---|---|
| 语言检测 | 主程序 | `lst/services/race/module.py` 的 `_detect`，调用 `langid.LanguageDetector.detect_many` | 说明 |
| 查译文缓存 | 主程序 | `lst/services/race/scheduler.py` 的 `_dispatch` 先调用 `_apply_cache` | 说明 |
| 分批 | 主程序 | `_enqueue_pairs` 里的 `pack_segments`，上限是实例的 `max_batch_segments`、`max_batch_chars` | 说明、“转到翻译引擎”按钮 |
| 提交到翻译进程 | 主程序 | `engines/local_nmt/engine.py` 的 `LocalNmtEngine.translate` 构造 `TranslateRequest` 并提交 | 说明 |
| 路线规划 | NMT 进程 | `nmt_worker/routing.py` 的 `plan_route` | 说明 |
| 分句 | NMT 进程 | `nmt_worker/sentences.py` 的 `split_sentences` | 说明 |
| 直连 / 经英语中转（分支） | NMT 进程 | `translate_segments` 里 `for hop in route`，直连一跳，中转两跳 | 说明 |
| 取模型 | NMT 进程 | `ct2_backend.Ct2Backend._model`，LRU 缓存最多常驻 4 个模型 | 说明、`[nmt].model_dir` |
| 分词 | NMT 进程 | `translate_sentences` 里的 `tokenizer.encode`（SentencePiece） | 说明 |
| CTranslate2 推理 | NMT 进程 | `translator.translate_batch` | 说明、模型列表、推理设备卡片、`[nmt].beam_size` |
| 还原与整理 | NMT 进程 | `ct2_backend.detokenize`，中文译文再经 `tidy_chinese` | 说明 |
| 拼回段落 | NMT 进程 | `sentences.join_sentences`，处理器逐段发回 `SegmentResult` | 说明 |

路线规划在分句之前执行：`translate_segments` 先对整个请求调用一次 `plan_route`，再逐段分句。直连与经英语中转的区别体现在每段逐跳翻译的次数上，所以分支画在分句之后，两条路汇合到“取模型”。经英语中转时，“取模型”到“还原与整理”四步执行两遍，第一跳的英语译文直接送进第二跳，不发回主程序。

节点的状态来自运行中的数据：

- “CTranslate2 推理”显示 NMT 进程在 `WorkerStatuses` 里报告的 `versions` 的设备与计算类型，例如“显卡 · float16”；进程还没有报告时显示设置里选的推理设备。
- “提交到翻译进程”显示 NMT 进程的状态。
- “分批”显示本地翻译实例的批大小上限。

推理设备只在“显卡加速”页设置。推理节点的面板用一张卡片写出当前设备，并带“转到显卡加速”按钮，同一个设置不在两处出现。

### 11.8 窗口内的键位

截屏蒙版、覆盖窗口和双语窗口里的操作都由键位表驱动（`UI-09`）。键位写在配置的 `[bindings]` 节，由 `lst/ui/common/bindings.py` 定义；这个模块属于界面共用模块，三个窗口模块和设置窗口都依赖它，它不依赖任何一个具体的界面模块。组合根把 `BindingsConfig` 登记为 `[bindings]` 节的模型，配置服务按 2.9.1 节的规则校验和回退。

每个可改键位的操作是一个 `ActionSpec`，带四项信息：

- 操作 id，形如 `overlay.toggle_source`，点前面是窗口；
- 生效的窗口（`Scope`）：`capture`、`overlay` 或 `bilingual`；
- 操作的类别（`ActionKind`），决定它接受哪些键位，见 11.8.2 节；
- 默认键位。

配置里的字段名是操作 id 把点换成下划线，例如 `overlay_toggle_source`。取值是键位文字的列表，每个操作最多三个键位，空列表表示不绑定。

#### 11.8.1 键位文字

键位文字分三类。

键盘键位是一个组合键，按 `QKeySequence` 的可移植写法（`PortableText`）书写，例如 `Ctrl+C`、`Esc`、`Ctrl+=`、`Ctrl+Return`、`F5`。只有一个组合，`Ctrl+K, Ctrl+C` 这样的多段序列无效；只按修饰键（`Ctrl`、`Ctrl+Shift`）也无效。

鼠标键位由可选的修饰键、按键名和手势三部分组成：

$$
\text{鼠标键位} = [\,\texttt{Ctrl+}\,][\,\texttt{Alt+}\,][\,\texttt{Shift+}\,]\ \text{按键}\ \text{手势}
$$

按键是 `Left`、`Right`、`Middle` 之一，手势是 `Click`、`DoubleClick`、`Drag` 之一，例如 `RightClick`、`Ctrl+LeftDrag`、`MiddleDoubleClick`。

滚轮键位是可选的修饰键加 `Wheel`，例如 `Wheel`、`Ctrl+Wheel`。

鼠标与滚轮键位的修饰键只认 `Ctrl`、`Alt`、`Shift`。`parse_binding` 解析时不区分大小写、忽略首尾空白，返回规范写法，配置里保存的是规范写法：

| 用户写的 | 规范写法 |
|---|---|
| `ctrl+c` | `Ctrl+C` |
| `Escape` | `Esc` |
| `shift+ctrl+s` | `Ctrl+Shift+S` |
| `Shift+Ctrl+LeftDrag` | `Ctrl+Shift+LeftDrag` |
| `shift+ctrl+wheel` | `Ctrl+Shift+Wheel` |

界面上显示键位时，键盘键位用系统的写法，鼠标与滚轮键位按界面语言写，例如 `Ctrl+右键拖动`、`滚轮`。双语窗口的按钮提示与截屏蒙版的操作提示都按当前键位生成。

#### 11.8.2 三类操作

操作按类别接受不同的键位，类别不符的键位在配置校验时报错：

| 类别 | 接受的键位 | 操作举例 |
|---|---|---|
| 触发（`TRIGGER`） | 键盘、单击、双击 | 关闭、固定在最上层、切换原文与译文 |
| 拖动（`DRAG`） | 鼠标拖动 | 框选区域、拖动窗口、选择文字 |
| 滚轮（`WHEEL`） | 滚轮 | 缩放整张图 |

拖动操作在按下、移动、松开三个阶段都要执行，单击只在一个时刻执行一次，两者的处理代码不同，所以分成两类。放大、缩小、还原归入触发类，因为它们由一次按键完成，与滚轮的“转过多少格缩放多少”不同。

#### 11.8.3 单击与拖动的区分

同一个按键的单击与拖动是两个键位，可以分给两个操作，例如覆盖窗口默认用右键单击切换原文与译文、右键拖动移动窗口。`MouseGestures` 按下面的规则把按下、移动、松开归成单击或拖动：

1. 按下时记下按键、修饰键和位置，查这个按键加修饰键有没有绑定拖动操作。
2. 没有绑定拖动、绑定了单击时，单击在按下时就执行。这时不需要等松开来判断用户想单击还是拖动，响应更快。截屏蒙版的右键取消因此在按下时生效，与 0.2 版相同。
3. 绑定了拖动时，按下后移动超过 4 个逻辑像素进入拖动，之后每次移动都交给拖动操作，松开时结束拖动。
4. 绑定了拖动、但松开前移动没有超过 4 个逻辑像素，就按单击处理：查这个按键加修饰键绑定的单击操作并执行。没有单击操作时交回拖动操作，由它执行自己的单击行为。覆盖窗口的“选择文字”在单击时清除选中、点开图表，截屏蒙版的“框选区域”在单击时得到空选区，按 `CAP-04` 取消。
5. 双击由窗口的 `mouseDoubleClickEvent` 查双击键位。没有绑定双击时，第二次按下当作普通的按下，与 `QWidget` 的缺省行为一致。双击之前的第一次单击照常执行。

同一时间只跟踪一个按键。跟踪期间按下另一个键，只有第 2 条“按下就执行”的单击会执行，正在跟踪的按键不变，后按下的键松开时不产生任何操作。框选途中按右键因此立即取消截屏；覆盖窗口里左键选择文字途中按右键，右键绑定了拖动，这次按下被忽略，选择照常完成。

判断拖动用的坐标因窗口而异。覆盖窗口用全局坐标，因为拖动窗口时窗口本身在移动，窗口内的坐标跟着变；截屏蒙版铺满屏幕、位置不变，用窗口内的坐标。

#### 11.8.4 冲突规则

`BindingsConfig` 的模型校验器保证同一个窗口里的两个操作不用同一个键位。比较的是规范写法，所以 `ctrl+c` 与 `Ctrl+C` 算同一个键位。冲突时报错的说明写出两个操作的名称和键位，例如“‘固定在最上层（失去焦点也不关闭）’和‘关闭’都用了 Esc”。

以下几种情况不算冲突：

- 不同窗口用同一个键位，例如三个窗口的关闭默认都是 `Esc`。三个窗口不会同时接收同一次按键：截屏时蒙版在最上层并拿着焦点，覆盖窗口与双语窗口各自只处理发给自己的事件；
- 同一个按键的单击、双击和拖动，例如 `RightClick` 与 `RightDrag`；
- 修饰键不同的同一个按键，例如 `LeftDrag` 与 `Ctrl+LeftDrag`。

同一个操作里重复写的键位只保留一个，不报错。

配置文件里的冲突在启动时按 2.9.1 节的规则回退：从默认值出发按文件里的顺序逐个字段加入，造成冲突的那个字段回到默认值，并发布 `ConfigRejected`。设置窗口修改键位时，配置服务用同一个校验器检查，造成冲突的字段被拒绝，原有键位不变；同一个 `UpdateConfig` 里的几个字段先合在一起校验，所以一次修改里交换两个操作的键位可以通过。窗口取键位用 `bindings_from(config.section("bindings"))`，节缺失或无法解析时用默认键位，作为最后一道保护。

#### 11.8.5 默认键位

默认键位与 0.2 版写死的操作相同；覆盖窗口的放大、缩小、还原是新增的操作。

| 窗口 | 操作 | 操作 id | 类别 | 默认键位 |
|---|---|---|---|---|
| 截屏蒙版 | 框选区域 | `capture.select` | 拖动 | `LeftDrag` |
| 截屏蒙版 | 取消截屏 | `capture.cancel` | 触发 | `RightClick`、`Esc` |
| 覆盖窗口 | 切换原文与译文 | `overlay.toggle_source` | 触发 | `RightClick` |
| 覆盖窗口 | 拖动窗口 | `overlay.move` | 拖动 | `RightDrag` |
| 覆盖窗口 | 缩放整张图 | `overlay.zoom` | 滚轮 | `Wheel` |
| 覆盖窗口 | 放大 | `overlay.zoom_in` | 触发 | `Ctrl+=` |
| 覆盖窗口 | 缩小 | `overlay.zoom_out` | 触发 | `Ctrl+-` |
| 覆盖窗口 | 还原大小 | `overlay.zoom_reset` | 触发 | `Ctrl+0` |
| 覆盖窗口 | 固定在最上层 | `overlay.pin` | 触发 | `MiddleClick` |
| 覆盖窗口 | 选择文字 | `overlay.select` | 拖动 | `LeftDrag` |
| 覆盖窗口 | 关闭 | `overlay.close` | 触发 | `Esc` |
| 双语窗口 | 立即翻译 | `bilingual.translate` | 触发 | `Ctrl+Return` |
| 双语窗口 | 交换语言 | `bilingual.swap` | 触发 | `Ctrl+Shift+S` |
| 双语窗口 | 置顶 | `bilingual.pin` | 触发 | `Ctrl+Shift+T` |
| 双语窗口 | 清空 | `bilingual.clear` | 触发 | `Ctrl+Shift+L` |
| 双语窗口 | 关闭窗口 | `bilingual.close` | 触发 | `Esc` |

小键盘的回车与主键盘的回车等同，`Ctrl+Return` 也匹配 `Ctrl` 加小键盘回车。

复制不在这张表里。覆盖窗口与双语窗口里显示的文字都可以选中，复制选中的文字用系统标准的复制键（`Ctrl+C`、`Ctrl+Insert`），双语窗口复制全部译文用“复制译文”按钮。标准复制键是 Windows 上所有文字控件共同的约定，放进键位表反而允许用户把它改掉或与别的操作冲突，所以由窗口直接处理（11.5.6、11.6.4 节），设置页也不列出。

此前的版本在键位表里有覆盖窗口的 `overlay_copy` 和双语窗口的 `bilingual_copy` 两个字段，那时写下的配置文件里可能还留着它们。`BindingsConfig` 设了 `extra="forbid"`，遇到不认识的字段时整个 `[bindings]` 节校验失败，用户改过的其他键位全部退回默认值。为此模块里记了一个常量 `REMOVED_FIELDS`，列出已经去掉的字段；`BindingsConfig` 的 `before` 校验器在校验各操作之前先丢掉这些字段，不论它们的取值是否有效、是否与别的操作冲突。设置窗口之后写回 `[bindings]` 节时只写改动的字段，旧字段留在文件里也不影响读取。

#### 11.8.6 配置示例

下面的 `[bindings]` 节把切换原文与译文改到空格，拖动窗口改到 `Ctrl` 加左键拖动，缩放改到 `Ctrl` 加滚轮，取消固定在最上层的绑定；没有写的操作保持默认键位：

```toml
[bindings]
overlay_toggle_source = ["Space"]
overlay_move = ["Ctrl+LeftDrag"]
overlay_zoom = ["Ctrl+Wheel"]
overlay_pin = []
capture_cancel = ["Esc", "RightClick", "MiddleClick"]
```

改键后，右键单击在覆盖窗口里不再有任何操作：右键单击的键位没有绑定，右键拖动也没有绑定。左键拖动仍然选择文字，`Ctrl` 加左键拖动移动窗口，两者修饰键不同，不冲突。

恢复默认就是删掉对应的字段，或者把它写回默认值。

#### 11.8.7 生效方式

三个窗口模块都订阅 `AppConfig`，收到后用 `bindings_from` 取出键位：

- 覆盖窗口的展示器生成 `Keymap`，交给已经打开的窗口（`set_keymap`），之后新建的窗口也用它；
- 截屏控制器保存 `BindingsConfig`，交给正在显示的蒙版（`set_bindings`），之后的截屏也用它。蒙版的操作提示同时更新；
- 双语窗口的展示器把 `BindingsConfig` 交给窗口（`set_bindings`），窗口重建键位表，并更新按钮提示与占位文字。

键位变化时正在进行的拖动按按下时的键位做完：`MouseGestures` 在按下时就记下了拖动操作，换键位表只影响之后的按下。

## 12 测试策略

| 层次 | 对象 | 工具 | 运行时机 |
|---|---|---|---|
| 单元测试 | contracts、kernel、services 各模块 | pytest、pytest-asyncio | 每次运行 pytest |
| 性质测试 | 竞速合并器、事件总线、任务打包、配置回退 | hypothesis | 每次运行 pytest |
| 穷举测试 | 参赛实例数不超过 4 时，合并器在所有到达顺序和成败组合下的行为 | pytest 参数化 | 每次运行 pytest |
| 引擎契约测试 | 每类引擎的请求构造、流式解析、错误映射、日志脱敏 | `httpx.MockTransport` 回放录制的响应，包括 SSE 流和格式错误的 JSON | 每次运行 pytest |
| 集成测试 | 真实引擎、调度器、缓存、事件总线、统计串联运行：显示逐级升级、缓存命中短路、最高优先级实例失败时竞速照常结束 | `tests/integration`，引擎连接 `httpx.MockTransport` 扮演的服务端 | 每次运行 pytest |
| 推理进程测试 | 协议握手、取消、崩溃重启、确定性故障、超时结束进程 | 启动真实子进程，后端替换为假实现；用 `os._exit` 和故意的段错误模拟崩溃 | 每次运行 pytest |
| 回归测试 | 段落合并、版面结果转换、版面标签映射 | 录制的真实 OCR 输出加人工标注的段落，指标是段落完全匹配率，低于基线即失败 | 每次运行 pytest |
| 界面测试 | 展示器、截屏坐标换算、字号适配、焦点顺序 | pytest-qt，使用 offscreen 平台 | 每次运行 pytest |
| 架构测试 | 模块依赖规则 | import-linter，由 `tests/architecture` 中的测试调用 | 每次运行 pytest |
| 自动化接口测试 | 管道的权限与协议、各个方法、开关与构建类型 | `tests/automation` 建真实的命名管道；`tests/integration/screenshot` 经接口驱动真实的组合根，其中一个用真实的 Windows OCR 与 NMT 进程 | 每次运行 pytest |
| 性能测试 | 需求 6.1 节的全部指标 | `scripts/bench_*.py`，在真实硬件上手动运行，结果记入文档 | 每个里程碑 |
| 静态检查 | 全部代码 | basedpyright strict、ruff | 每次运行 `scripts/check.bat` |

单元测试和性质测试不依赖 Qt、推理库和网络，在只装了开发依赖的环境里就能运行。这一点由第 4.2 节的依赖规则保证。

每个测试有 60 秒的上限（pytest-timeout）。推理进程测试会启动真实子进程并通过管道收发数据，一旦出现死锁，测试会在上限处失败，而不是挂住整个测试运行。Windows 上 `multiprocessing` 管道的缓冲区只有 8 KB，同一个线程先写大块数据再读会互相等待，测试和实现都要把收发放在不同线程里。

## 13 需求追溯

| 需求 | 落点 |
|---|---|
| `TR-01` 至 `TR-03`、`TR-06` | 11.1 节、5.3 节（`EngineStatuses`、`WorkerStatuses`）、8.2 节状态映射、2.9.6 节 |
| `TR-04`、`TR-05` | 11.3 节、第 9 章（热键模块的配置处理） |
| `CAP-01` 至 `CAP-07` | 11.4 节、11.5.1 节（覆盖窗口跨屏重新布局）、8.4 节（预处理）、6.5 节（作废旧会话） |
| `OCR-01` 至 `OCR-10` | 2.4 节、8.4 节、第 7 章、8.2 节第 5、6 条 |
| `OCR-11` | 8.7 节 |
| `OCRE-01` 至 `OCRE-09` | 8.4 节、8.6 节、11.7 节（截屏与识别页） |
| `ENG-01` 至 `ENG-07` | 2.5 节、2.7 节、6.4 节、7.1 节 |
| `ENG-08` 至 `ENG-10` | 2.7.2 节、2.7.4 节、8.5.2 节、第 9 章 |
| `RACE-01` 至 `RACE-10` | 第 6 章、2.6 节 |
| `OV-01` 至 `OV-07` | 11.5 节、第 7 章 |
| `BI-01` 至 `BI-07` | 11.6 节、6.4 节（`start_session` 的 `engines` 参数） |
| `CFG-01` 至 `CFG-05` | 第 9 章、2.9.1 节、2.9.2 节、10.3 节 |
| `CFG-06`、`CFG-07` | 2.9.5 节、8.5.3 节、11.7 节（常规页的存储位置）、15.5 节 |
| `MOD-01` 至 `MOD-05` | 8.5 节（`MOD-04` 见 8.5.3 节）、8.6.4 节（OCR 模型） |
| `MOD-06` | 8.3.1 节 |
| `UI-01` 至 `UI-08` | 2.2 节、11.1 节、11.7 节 |
| `UI-09` | 11.8 节、11.7 节（热键与键位的四个子页），以及 11.4.1、11.5.5、11.6.4 节里各窗口的操作 |
| `REL-01` 至 `REL-04` | 第 3 章、8.2 节、11.2 节 |
| `OBS-01` 至 `OBS-04` | 2.9.4 节、第 7 章第 8 步、10.2 节、10.3 节 |
| `PRV-01`、`PRV-02` | 第 10 章 |
| `MNT-01` 至 `MNT-05` | 第 4 章、第 12 章 |
| `INS-01` 至 `INS-09` | 第 15 章 |
| `UPD-01` 至 `UPD-06` | 15.9 节 |
| `DEV-01` 至 `DEV-08` | 第 16 章、15.6 节（构建类型）、15.9 节（按构建类型选安装包） |

## 14 实施顺序

每个里程碑结束时，第 12 章中已有的检查全部通过。

M3.5 是开发过程中插入的里程碑。双语窗口不依赖 OCR，托盘、双语窗口、竞速和引擎组合在一起就是一个能用的翻译工具，所以先于截屏翻译交付，尽早在真实使用中验证竞速核心和引擎；M4 的 OCR 和 M5 的截屏界面随后接入同一套模块。

| 里程碑 | 内容 | 完成标准 |
|---|---|---|
| M0 | 项目骨架、工具链、依赖规则 | pytest、basedpyright、ruff、import-linter 在空项目上通过 |
| M1 | 事件总线、竞速合并器、调度器 | 6.2、6.3 节的性质由性质测试和穷举测试验证；需求文档 5.5 节的时间线用假引擎端到端跑通 |
| M2 | OpenAI 兼容引擎、DeepL、Google、百度、缓存、统计 | 各引擎的契约测试通过；用真实 key 手动验证一次 |
| M3 | NMT 进程、监管器、通信协议 | 崩溃、超时、取消、确定性故障的测试通过；直连语言对的 200 字译文在 500 毫秒内返回 |
| M3.5 | 双语竖切片：配置服务、引擎模块、竞速模块、语言检测、托盘、热键、双语窗口、组合根 | 程序可以启动；双语窗口用本地 NMT 和已配置的在线引擎完成竞速翻译；单实例、日志、退出流程可用 |
| M4 | OCR 进程、版面结果转换、段落合并回归集、模型管理 | 回归集基线建立；版面标签映射表固定；版面模式和文本模式都能跑通 |
| M5 | 主题、托盘、热键、截屏蒙版、覆盖窗口 | 截屏翻译端到端可用 |
| M6 | 双语窗口、设置窗口、首次运行引导 | 全部功能需求可用 |
| M7 | 性能验收、诊断导出、文档 | 需求 6.1 节全部指标实测达标 |

## 15 打包、安装与卸载

本章落实需求 5.11 节（`INS-01` 至 `INS-08`）。安装包是精简包，按当前用户安装，模型在首次使用时下载。

### 15.1 安装目录布局

安装程序把程序装到 `%LOCALAPPDATA%\Programs\LSTtranslate`，目录结构如下：

```
runtime\        独立版 CPython 3.13（来自 uv 管理的解释器，去掉 pip、tcl/tkinter、IDLE、头文件）
site\main\      主进程、NMT 进程与 Windows OCR 引擎的依赖（按 uv.lock 导出）
app\lst\        程序代码，构建时预编译为 .pyc
app\assets\     图标
app\installed.json   安装版标记：版本号与构建时间
app\ocr-<引擎>-<版本>-requirements.txt   RapidOCR、PaddleOCR 各运行环境版本的精确版本清单（带哈希），按需安装时使用
app\THIRD-PARTY-NOTICES   第三方数据（词典包）的来源、改编方式与许可证
app\licenses\   第三方许可证全文（ECDICT）
tools\uv.exe    按需安装 OCR 引擎运行环境用的 uv，许可证在 tools\licenses\uv\
launcher\       启动器：lst_main.pyw（主程序）、worker.py（推理进程与维护命令）
unins000.exe    卸载程序（Inno Setup 生成）
```

安装包不捆绑 RapidOCR 与 PaddleOCR（`OCRE-02`）。用户下载后，它们的运行环境分别在模型目录（2.9.5 节）的 `engines\rapid\site\` 与 `engines\paddle\site\`，与主进程依赖分开，推理库和其他依赖不会在同一个进程里加载（2.4 节）。两套依赖共用一个解释器，因为它们的 Python 版本相同；虚拟环境的 `pyvenv.cfg` 写死了解释器的绝对路径，不能随安装目录移动，所以安装版不用虚拟环境，而由启动器设定模块搜索路径。

主进程依赖用 `PySide6-Essentials` 代替 `PySide6`。后者会连带安装 WebEngine 等本程序用不到的模块；换用后 PySide6 部分从数百 MB 降到 214 MB。

### 15.2 启动方式

- 开始菜单快捷方式运行 `runtime\pythonw.exe -I -X utf8 launcher\lst_main.pyw`。`-I` 隔离模式忽略用户机器上的 `PYTHONPATH` 和用户级 site-packages，启动器把 `app\` 与 `site\main\` 放到模块搜索路径的最前面，再调用 `lst.__main__.main()`。
- 推理进程由监管器以 `runtime\python.exe -I -X utf8 launcher\worker.py nmt|ocr …` 启动。`ocr` 子命令带 `--ocr-site <目录>|main`：Windows 引擎用 `main`（即 `site\main`）并透传 `--backend`；RapidOCR 用 `<模型目录>\engines\rapid\site` 并传 `--backend lst.workers.ocr_worker.rapid_backend:RapidBackend`；PaddleOCR 用 `<模型目录>\engines\paddle\site`；没有可用目录时以依赖错误（65）退出。组合根的 `worker_command()`（NMT）与 `ocr_worker_command()`（OCR，按引擎）给出命令，源码版用 `.venv` 与 `.venv-ocr` 的解释器。
- 程序判断自己是否是安装版的依据是 `app\installed.json` 是否存在（`lst/app/paths.py`）。

### 15.3 数据目录

安装版的默认数据目录是 `%LOCALAPPDATA%\LSTtranslate`，源码版仍是程序目录下的 `data/`（2.9.5 节），两者互不影响（`INS-08`）。用户在设置里另选的数据目录记在 `%APPDATA%\LSTtranslate\location.json`，模型目录记在配置的 `[storage].models_dir`。安装版位于 `%LOCALAPPDATA%\Programs` 下，以中完整性级别运行，可以写用户目录；源码版遇到的 Low 完整性标签问题不出现在安装版上。

### 15.4 安装、升级与运行检测

安装脚本是 `installer\lsttranslate.iss`，用 Inno Setup 6 编译，要点如下：

- `PrivilegesRequired=lowest`：当前用户安装，不弹管理员确认（`INS-01`）。
- 程序运行期间持有命名互斥体 `LSTtranslate.Running`，安装脚本的 `AppMutex` 指向它；安装、升级、卸载时检测到程序正在运行，提示用户关闭（`INS-05`）。
- 可选任务：桌面快捷方式、登录时自动启动（写 `HKCU\...\Run`，卸载时删除）（`INS-02`）。
- 升级时先删除旧版本的 `app\`、`site\`、`runtime\`、`launcher\`、`tools\`，避免残留已经移除的模块；用户数据（包括已下载的 OCR 引擎与词典包）不在安装目录里，不受影响（`INS-07`）。
- 应用内更新（15.9 节）以 `/SILENT /SUPPRESSMSGBOXES /NORESTART /RELAUNCH=yes` 运行新安装包。带 `/RELAUNCH=yes` 时，`InitializeSetup` 每 250 毫秒检查一次互斥体，最多等 120 秒，等程序退出后再继续；装完由 `[Run]` 里只在这种情况下执行的条目重新启动程序。用最小脚本实测确认 `InitializeSetup` 在 AppMutex 检查之前执行：不带该参数时静默安装检测到程序运行即退出（退出码 1）；带该参数、互斥体被持有 6 秒时，等待 5 秒后通过检查并完成安装（退出码 0）。
  程序这一侧，入口收到退出请求后先释放运行互斥体，再停止各模块与推理进程，安装程序不必等关停完成。超过 120 秒仍未释放时安装程序放弃这次更新；带 `/RELAUNCH=yes` 而没有装完时，`DeinitializeSetup` 从卸载登记项的 `Inno Setup: App Path` 找到安装目录，重新启动旧程序，用户不会看到程序消失且没有升级。这段 `[Code]` 只编译检查过，没有运行验证。
- `AppId` 固定为 `{435CC09B-5F36-4944-9918-62F7215D2057}`，升级据此识别同一个软件，发布后不能更改。

安装向导目前是英文。Inno Setup 的简体中文语言文件属于非官方翻译，没有随 Inno Setup 一起安装；需要下载后加入 `[Languages]`。卸载时询问是否删除数据的提示已经同时写了中英文。

### 15.5 卸载

卸载程序删除程序文件、快捷方式和登录自启项，然后询问是否删除用户数据（`INS-06`）：

1. 选“是”时，先用安装目录里的解释器运行 `launcher\worker.py purge-secrets`，删除凭据管理器中本程序的全部通用凭据。keyring 保存凭据时，目标名是服务名 `LSTtranslate`，同一服务有第二个条目时改用 `条目名@LSTtranslate`，清理按这两种目标名枚举并删除（`lst/app/uninstall.py`）。这一步必须在删除程序文件之前做。
   枚举凭据时不能用 `*@LSTtranslate` 这样带前导星号的过滤器（Win32 的 `CredEnumerateW` 只支持“前缀加星号”），所以先枚举全部通用凭据，再在程序里按“等于服务名，或以 `@服务名` 结尾”筛选，不区分大小写。keyring 的行为是：每写入一个新条目，原来占用服务名的条目改存到 `条目名@服务名`。清理失败时 `purge-secrets` 以非 0 退出，卸载脚本记日志，不阻止卸载。
2. 接着运行 `launcher\worker.py purge-data`（`uninstall.purge_user_data`），处理用户在设置里另选的目录：位置文件指向的数据目录（目录里有 `config\settings.toml` 才算），以及默认数据目录、`LST_DATA_DIR` 目录与位置文件指向的目录在各自配置里指定的模型目录。这些目录可能是“D:\资料”这样还放着别的文件的目录，所以只删除程序创建的条目（数据目录的 `config`、`logs`、`tmp`、两个 SQLite 库及其附属文件、`ui_state.ini`、`updates.json`、锁文件，模型目录的五个子目录），删完目录为空时再删除目录本身，最后删除位置文件。有条目删不掉时以非 0 退出，卸载脚本记日志，不阻止卸载。
3. 程序文件删除之后，再删除默认数据目录 `%LOCALAPPDATA%\LSTtranslate` 与位置文件所在的 `%APPDATA%\LSTtranslate`。默认数据目录只放本程序的文件，整个删除。

静默卸载默认保留用户数据，命令行加 `/DELETEUSERDATA=yes` 时删除，供自动化测试使用。Inno Setup 不记录 `[Code]` 段里 `Exec` 的执行情况，安装脚本因此把 `purge-secrets` 与 `purge-data` 的退出码写进卸载日志（`/LOG=文件` 时可见）。选“保留用户数据”时位置文件也保留，重新安装后程序继续使用选好的目录。

用 `LST_DATA_DIR` 改过数据目录的用户，卸载脚本读取这个环境变量，同时满足三个条件才删除该目录：是绝对路径，不是驱动器根目录，目录里有 `config\settings.toml`。条件是为了避免变量指错时删掉别的东西。

源码版和安装版用同一个 keyring 服务名，凭据管理器里的条目两者共用。在同一台机器上同时使用两种运行方式时，删除数据的卸载也会删掉源码版保存的 API key。

### 15.6 构建

在仓库根目录运行 `uv run python scripts/build_installer.py`。`--flavor normal|dev` 选构建类型，缺省 `normal`；`--all` 用同一份暂存目录依次编译两种（16.6 节）。步骤：

1. 复制 uv 管理的独立版 CPython 3.13 到暂存目录，去掉用不到的部分；
2. `uv export` 按 `uv.lock` 导出主进程依赖，`uv pip install --target` 安装到 `site\main`；
3. 按 `.venv-ocr` 当前的精确版本生成各 OCR 引擎各运行环境版本的清单 `app\ocr-<引擎>-<版本>-requirements.txt`（8.6.2 节）：
   - PaddleOCR CPU 版：`uv pip freeze` 冻结 `.venv-ocr` 的全部包，校验为纯“名==版”并排序（冻结结果里有不止一个 OpenCV 发行包时中止，见下文）；
   - RapidOCR CPU 版：只含 rapidocr、onnxruntime 及其依赖的闭包。闭包按 `.venv-ocr` 里各包元数据的 Requires-Dist 求出，环境标记按构建机求值，可选依赖不计入；rapidocr 声明的 opencv-python-headless 换成 `.venv-ocr` 实际装的 OpenCV 发行包；
   - GPU 版：在 CPU 版清单上只替换推理库，再用 `uv pip compile` 以其余各包的精确版本为准解析一次，补上新推理库的依赖（CUDA 运行库、DirectML 版的 sympy），加 `--override ocr-overrides.txt`；
   - 全部清单再用 `uv pip compile --no-deps --generate-hashes` 补上每个包的 SHA-256。不能联网时加 `--allow-unhashed` 写入不带哈希的清单；GPU 版的依赖这时也解析不了，清单里写上 `# lst: resolve-dependencies`，运行时安装不加 `--no-deps`，由 uv 在用户机器上补齐依赖；
4. 复制本机 `uv.exe` 与许可证到 `tools\`；
5. 复制程序代码、图标、启动器、`THIRD-PARTY-NOTICES` 与 `licenses\`，预编译 `.pyc`，写入 `installed.json`；
6. 用安装布局实际导入主进程的关键模块，并运行 `tools\uv.exe --version`，失败则中止构建；
7. 每种构建类型写一份 `build\flavors\<类型>\installed.json`，调用 Inno Setup 编译，输出 `dist\LSTtranslate-<版本>-setup.exe`（普通版）或 `dist\LSTtranslate-<版本>-dev-setup.exe`（开发版），并在每个安装包旁边生成 `.sha256` 校验文件（sha256sum 格式），供应用内更新校验。

构建开头用更新服务的 `parse_version` 校验版本号，不是规范的语义化版本就中止；版本号以 `-dev` 结尾时也中止，它的普通版安装包会与上一个版本号的开发版安装包同名。PEP 440 的写法（`0.2.0b1`）更新服务解析不了，用了会让更新功能整体失效，所以预发布版本要写成 `0.2.0-beta.1`；运行时读到包元数据里归一化后的 PEP 440 写法，由 `semver_text` 换算回来。生成哈希清单的 `uv pip compile` 带 `--no-config --only-binary :all:`，只发布源码包的依赖在构建阶段就会失败。

第 6 步的自检还检查：主环境能导入 Windows OCR 后端与 pywinrt；keyring 的后端是 Windows 凭据管理器；通过真实的 `launcher\worker.py` 分别启动 NMT 与 OCR 进程入口（不带地址参数，期望以配置错误退出，证明按安装布局导入成功，全程不联网）；每份版本清单的格式，以及是否带哈希与构建参数一致。

暂存目录 `build\` 与输出目录 `dist\` 不纳入版本库。

构建脚本在 Git Bash 里调用 ISCC 没有问题；手动在 Git Bash 里运行 ISCC 或安装程序时，要设置 `MSYS_NO_PATHCONV=1`。否则 MSYS 会把 `/DAppVersion=…`、`/DIR=…` 这类以斜杠开头的参数当成路径改写，ISCC 报“只能指定一个脚本文件”。

OCR 环境曾同时装有两个 OpenCV 发行包：`paddlex[ocr]` 锁定 `opencv-contrib-python==4.10.0.84`，`rapidocr` 依赖 `opencv-python-headless`，解析出 5.0 版。两个包都把文件装进同一个 `cv2` 目录，后装的覆盖先装的一部分文件。开发环境里碰巧是 5.0 的二进制覆盖在上面，导入正常；安装包按另一种顺序装，`cv2` 的 Python 部分与二进制部分版本不一致，导入时报循环导入，第 5 步的自检因此失败。解决办法是 `ocr-overrides.txt`：用一个永远不成立的环境标记把 `opencv-python-headless` 从依赖里去掉，环境里只留 contrib 版。contrib 版包含 headless 版的全部模块，满足 rapidocr 的需要。

ISCC 是 32 位程序。`lzma2/ultra64` 的大字典在 ISCC 进程内压缩时内存不足，所以安装脚本设置 `LZMAUseSeparateProcess=yes`，在单独的 64 位进程里压缩，压缩线程数取 4。

### 15.7 GPU 加速

安装包不带 cuBLAS，安装版的 NMT 进程在 CPU 上运行。2.5 节说明了原因：CTranslate2 的 Windows wheel 只自带 cuDNN，CUDA 路径还需要 `cublas64_12.dll` 和 `cublasLt64_12.dll`。本机有 RTX 5080，但系统里没有这两个 DLL，安装版启动时 NMT 进程记录“有 CUDA 设备，但 cuBLAS 无法加载，改用 CPU”，随后正常就绪。CPU int8 的实测延迟（2.5 节，约 200 字 150–270 毫秒）满足截屏翻译的要求，所以精简包默认走 CPU。

GPU 加速列入后续版本，做法与模型相同：把 `nvidia-cublas-cu12` 作为按需下载的组件放进数据目录，NMT 进程启动时把该目录加入 DLL 搜索路径。现有的 `_add_nvidia_dll_dirs()` 只扫描模块搜索路径下的 `nvidia/*/bin`，届时改为同时扫描数据目录里的组件目录。

### 15.8 实测

下表是本机（Windows 11，build 26300）上的实测结果。“精简”指 `--skip-ocr` 构建，“完整”含 OCR 环境：

| 项目 | 精简 | 完整 |
|---|---|---|
| 运行时 | 36 MB | 36 MB |
| 主进程依赖 `site\main` | 618 MB | 618 MB |
| OCR 依赖 `site\ocr` | 无 | 939 MB |
| 程序代码 `app` | 2 MB | 2 MB |
| 暂存目录合计 | 656 MB | 1596 MB |
| 安装包 | 203 MB | 396 MB |
| 构建用时 | 约 1 分 30 秒（ISCC） | 233 秒（全部步骤） |

完整包的数字是修复 OpenCV 冲突之后的构建：运行时 38 MB，OCR 依赖 975 MB，暂存目录合计 1633 MB，安装包 402 MB。

安装与运行实测：

| 项目 | 结果 |
|---|---|
| 静默安装（精简包） | 39 秒，装到 `%LOCALAPPDATA%\Programs\LSTtranslate`，占用 674 MB |
| 静默安装（完整包） | 102 秒，占用 1.7 GB |
| 卸载条目 | `HKCU\...\Uninstall\{435CC09B-…}_is1`，显示名、版本、图标、发布者齐全 |

安装与卸载的验证步骤如下，每一步都在本机实际执行过：

1. 静默安装：`LSTtranslate-0.1.0-setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`，退出码 0。开始菜单出现快捷方式，“应用”列表出现卸载条目。
2. 用开始菜单快捷方式的命令行启动安装版。数据目录 `%LOCALAPPDATA%\LSTtranslate` 生成默认配置、缓存库、统计库和日志；日志依次记录“全部模块已启动”、热键注册成功、NMT 进程就绪，从进程启动到 NMT 就绪约 1 秒。
3. 程序运行时静默卸载。卸载程序检测到互斥体 `LSTtranslate.Running`，在静默模式下默认选择“取消”，退出码 1，程序文件全部保留（`INS-05`）。
4. 强制结束主进程。NMT 进程随作业对象一起退出，没有残留（8.2.3 节）。
5. 静默卸载并删除数据：`unins000.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DELETEUSERDATA=yes`，退出码 0。程序目录、开始菜单快捷方式、卸载条目、数据目录全部删除，卸载日志记录 `purge-secrets exit code: 0`（`INS-06`）。

完整包在本机重复了上面的步骤，结果相同。安装版首次启动时弹出首次运行引导，取到模型索引（100 个可下载的翻译模型）；数据目录里没有 OCR 模型，组合根不启动 OCR 进程，只记一条 WARNING（8.4 节）。

### 15.9 应用内更新

`services.updates` 发布 `UpdateStatus`（契约见 `lst/contracts/updates.py`），处理 `CheckForUpdates`、`DownloadUpdate`、`InstallUpdate`（`UPD-01` 至 `UPD-06`）：

- 版本来源：`GET https://api.github.com/repos/willlllliams/LSTtranslate/releases`（`/releases/latest` 不返回预发布版本）。只考虑同时带本构建类型的安装包与它的 `.sha256` 附件的版本：普通版找 `LSTtranslate-<版本>-setup.exe`，开发版找 `LSTtranslate-<版本>-dev-setup.exe`，按文件名完全相等匹配（16.6 节），按语义化版本比较（含预发布后缀），`[updates].include_prerelease` 为假时跳过预发布。不需要任何认证，日志里的地址去掉查询参数。
- 自动检查：启动 30 秒后检查一次，之后按 `[updates].check_interval_hours`（默认 24）检查；最近一次检查与已通知的版本写在数据目录的 `updates.json`，重启后不重复检查，同一版本只通知一次。自动检查失败只记日志；手动检查失败进入 ERROR，托盘给出原因。
- 下载：先取 `.sha256` 文件，要求文件名与安装包一致；安装包流式写入 `tmp\updates\` 的 `.part`，在线程里边写边算 SHA-256，一致才改名并进入 READY。启动时清理 `.part` 与不高于当前版本的旧安装包。
- 安装：以分离进程运行安装包（`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`，不继承句柄），参数见 15.4 节，成功后发布 `QuitRequested`。源码运行时只检查，下载与安装命令进入 `update.not_installed`。
- 托盘菜单有“检查更新”；发现新版本后变为“安装更新 x.y.z”，先下载、再安装，下载中显示进度。
- 命令只生效一次：下载命令在第一次等待之前就进入 DOWNLOADING，安装命令同样先置标志，双击按钮或托盘与设置窗口同时发命令时不会并发下载、不会启动两个安装程序。DOWNLOADING、READY、INSTALLING 状态下的检查命令被忽略，但会重新发布一次当前状态，界面不会停在“正在检查”。
- 下载目录只保留当前可用版本的安装包：启动时删除不高于当前版本的，每次检查成功后删除已被更新版本取代的。

## 16 自动化接口

本章落实需求 5.13 节（`DEV-01` 至 `DEV-08`）。自动化接口让同一台电脑上以当前用户身份运行的程序，例如测试脚本和 AI agent，通过程序自己的命名管道驱动并检查真实的交互：截屏翻译、覆盖窗口上的操作、键位、设置、存储位置迁移和立即重启。驱动走程序内部的同一条链路，不模拟操作系统的键盘鼠标，所以不需要窗口在前台，也不会和用户自己的操作抢输入。给 agent 的使用说明在 `docs/automation.md`。

代码分两处。客户端与线路协议在 `lst/automation/`，只依赖标准库，安装版的启动器可以直接运行它；服务端在组合根旁边的 `lst/app/automation/`，因为它要同时调用截屏、覆盖窗口和设置窗口三个界面模块，而界面模块之间互不依赖（4.2 节），只有 `app` 层能同时看到它们。

### 16.1 构建类型与开关

构建类型（`BuildFlavor`，`lst/contracts/automation.py`）有普通版（`normal`）和开发版（`dev`）两种。两种构建的程序文件完全相同，区别只在开关的默认值：开发版开启，普通版关闭。构建类型按下面的顺序确定（`lst/app/flavor.py`）：

- 安装版读 `app\installed.json` 的 `flavor` 字段，由构建脚本写入。字段缺失（旧版本的安装包）或取值不认识时按普通版处理。安装版不接受命令行参数和环境变量的覆盖，因为构建类型还决定应用内更新下载哪一种安装包（16.6 节），一个环境变量不应把普通版升级成开发版；
- 源码版先看命令行参数 `--flavor normal|dev`，再看环境变量 `LST_FLAVOR`，都没有时是普通版。

开关是配置的 `[automation].enabled`，类型为 `bool | None`，默认 `None`。生效的开关按下式计算：

$$
\text{enabled} =
\begin{cases}
\text{配置里的 enabled}, & \text{配置文件写了 enabled},\\
\text{flavor} = \text{dev}, & \text{配置文件没有写}.
\end{cases}
$$

默认值取 `None` 而不取某个布尔值，原因在配置服务生成默认配置文件的方式：`render_default` 把每一节的默认值写进文件，值为 `None` 的字段跳过（TOML 没有空值）。如果默认值是 `False`，普通版第一次运行就会在文件里写下 `enabled = false`，用户之后换装开发版，这个并非用户写下的值会让开关继续关闭。用 `None` 时，没有动过开关的用户换装另一种构建，开关跟着新构建的默认值走；用户在设置里打开或关闭过一次之后，文件里有了明确的 `true` 或 `false`，以后无论装哪种构建都以它为准。下面三种情形对应上式的两个分支：

| 配置文件 | 普通版 | 开发版 |
|---|---|---|
| 没有 `enabled` | 关闭 | 开启 |
| `enabled = true` | 开启 | 开启 |
| `enabled = false` | 关闭 | 关闭 |

自动化接口模块（`AutomationModule`）订阅 `AppConfig`，每次配置变化都按上式重新计算。开关打开时建立管道，关闭时撤掉管道，不需要重启。模块发布 `AutomationStatus`（构建类型、开关、开关是否写在配置里、管道是否已经建好、管道名、建不起来时的原因），三处界面读它：

- 托盘在管道建好期间，悬停提示多一行“自动化接口已开启”；
- 设置的“关于与诊断”页有一张开关卡片，说明里写清风险和这种构建的默认值，下方一行显示正在监听的管道名或建不起来的原因。点开关发 `UpdateConfig {"automation": {"enabled": ...}}`；
- 关于页的版本号后面，开发版注明“开发版”。

本次运行里管道第一次建好时，托盘弹一条通知，提醒用户本机的程序现在可以控制 LSTtranslate。同一次运行里再次开关不重复通知。

开关卡片放在“关于与诊断”页而不放在“常规”页：自动化接口是给测试脚本与 agent 用的诊断功能，与版本号、构建类型放在同一页，用户一眼能看到“开发版默认开启”这条说明和它的由来。

### 16.2 管道与安全描述符

管道名由当前用户的安全标识符（Security Identifier，SID）算出：固定前缀 `\\.\pipe\lsttranslate-automation-`，后接 SID 字符串的 SHA-256 摘要的前 12 位十六进制，例如 `\\.\pipe\lsttranslate-automation-3bafc7af28dd`。同一个用户在不同登录会话里得到同一个名字，不同用户的名字不同。环境变量 `LST_AUTOMATION_PIPE` 可以覆盖它，写完整的 `\\.\pipe\…` 或只写最后一段都可以；测试用它让每个测试有自己的管道，不会碰到用户正在运行的程序。

管道用显式的安全描述符创建，写成安全描述符定义语言（Security Descriptor Definition Language，SDDL）是：

```text
O:<SID>D:P(A;;GA;;;<SID>)
```

各部分的含义：

- `O:<SID>`：所有者是当前用户；
- `D:P`：自主访问控制列表（Discretionary Access Control List，DACL）带保护标志，不从上级继承任何条目；
- `(A;;GA;;;<SID>)`：唯一的一条允许条目，把全部权限给当前用户。

SYSTEM、Administrators 组与其他用户都不在列表里。Windows 创建管道时把通用权限 GA 映射成文件对象的 FA（FILE_ALL_ACCESS），读回来的描述符是 `O:<SID>D:P(A;;FA;;;<SID>)`，测试按这个字符串逐项断言。管理员仍然可以凭“取得所有权”特权改写描述符，这是 Windows 给管理员的能力，任何对象都一样。描述符里没有写强制完整性标签，内核对象缺省按 Medium 完整性的“不可向上写”规则处理，Low 完整性的进程（浏览器沙箱、某些代码执行沙箱）打不开管道写入。

创建管道还带三个标志：

- `FILE_FLAG_FIRST_PIPE_INSTANCE`：第一个实例带上它。同名管道已经存在时创建失败，程序不会接到别人抢先建好的管道上；
- `PIPE_REJECT_REMOTE_CLIENTS`：拒绝经网络连进来的客户端，只接受本机；
- 字节流模式（`PIPE_TYPE_BYTE`）：消息边界由换行符决定，与读写的块大小无关。

客户端也做两件事。打开管道时带 `SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION`，服务端最多只能识别客户端的身份，不能冒用它的权限；连上之后读管道的所有者，不是当前用户就断开，不把请求发给别人建的同名管道。

### 16.3 服务端

服务端运行在主进程的事件循环里。qasync 在 Windows 上的循环继承自 asyncio 的 `ProactorEventLoop`，用 I/O 完成端口收发，命名管道的连接、读、写都能直接交给它，实测在 qasync 与 asyncio 的缺省循环下都能工作。asyncio 自带的 `start_serving_pipe` 创建管道时不带安全描述符，用的是系统缺省的 DACL，里面有 SYSTEM、Administrators 和“所有人读”，所以服务端自己创建管道实例，再用循环的 proactor 完成 `ConnectNamedPipe`、`ReadFile`、`WriteFile`。proactor 是循环的内部属性 `_proactor`，CPython 3.8 至 3.13 的 `ProactorEventLoop` 与 qasync 都有它；循环没有它时开启失败，`AutomationStatus.error` 给出原因，程序其他部分照常运行。

接受连接的方式与 `start_serving_pipe` 相同：始终保留一个空闲实例等下一个客户端，客户端连上后立即再建一个新实例，然后在这个连接上循环“读一行、处理、写回复”。多个客户端可以同时连接，各自的请求按顺序处理。请求在主线程上执行，方法可以直接读写模块和窗口。

关闭分两步。先关掉空闲实例、停止接受，空闲的连接立即断开；正在处理请求的连接写完这次回复再断开。经接口关掉开关时，“关掉开关”的这一次 `settings.set` 因此照样收到回复。程序退出时组合根等这些连接结束，超过 2 秒仍在处理的请求被取消。

线路协议是一行一个 JSON，UTF-8 编码：

```text
→ {"id": 1, "method": "capture", "params": {"rect": [0, 0, 800, 300]}}
← {"id": 1, "result": {"session_id": "9f…", "mode": "direct", "rect": [0, 0, 800, 300]}}
← {"id": 2, "error": {"code": "timeout", "message": "…", "data": {…}}}
```

`params` 可以省略。一行最长 1 MiB，超过时回复 `invalid_request` 并断开。错误码如下：

| 错误码 | 含义 |
|---|---|
| `parse_error` | 请求不是合法的 UTF-8 JSON |
| `invalid_request` | 不是 `{"id", "method", "params"}` 的形式，或一行太长 |
| `unknown_method` | 没有这个方法，`data.methods` 列出全部方法 |
| `invalid_params` | 参数缺失或类型、范围不对 |
| `unavailable` | 要操作的对象现在不存在，例如没有打开的覆盖窗口 |
| `timeout` | 等待超时，`data` 是超时那一刻的状态 |
| `rejected` | 配置服务或存储位置模块拒绝了修改，`data` 是原因 |
| `not_confirmed` | 会移动文件或重启程序的操作，参数里没有 `confirm: true` |
| `internal` | 方法里出现未预期的异常，详情在日志里 |

日志只记方法名、结果与耗时，例如 `自动化接口：capture → ok，12 毫秒`，不记参数和结果里的文字（`PRV-02`）。

### 16.4 方法

| 方法 | 作用 |
|---|---|
| `status` | 版本、构建类型、进程号、各推理进程的状态与 versions、OCR 引擎目录摘要、启用的翻译服务、数据目录与模型目录、真实屏幕的原生矩形、方法列表 |
| `capture` | 按原生坐标的矩形截屏；`mode` 为 `direct` 时不显示蒙版，为 `mask` 时打开真实蒙版并按当前键位模拟拖动；返回会话号 |
| `capture.release`、`capture.cancel`、`capture.state`、`capture.key`、`capture.mouse` | `mask` 模式带 `hold: true` 时松开或取消按着的框选、查询蒙版，以及在蒙版上执行按键或鼠标键位 |
| `wait_result` | 等识别完成、翻译全部定下来，返回块、段、各尝试与覆盖窗口的状态 |
| `overlay.state`、`overlay.select`、`overlay.select_at`、`overlay.copy` | 覆盖窗口的状态，按字符位置或按窗口坐标拖动选字，取出选中的文字 |
| `overlay.toggle_source`、`overlay.zoom_in`、`overlay.zoom_out`、`overlay.zoom_reset`、`overlay.pin`、`overlay.close` | 覆盖窗口上的操作，调用窗口自己的方法 |
| `overlay.key`、`overlay.mouse` | 在覆盖窗口上执行键位：直接给键位文字，或给操作 id 取它当前的键位 |
| `grab` | 把覆盖窗口、蒙版或设置窗口当前的画面存成 PNG，文件在数据目录的 `tmp/automation` 下 |
| `settings.open`、`settings.get`、`settings.set`、`settings.pages` | 打开设置窗口的某一页、读一节配置、改一节配置并等结果、导航树 |
| `bindings.get`、`bindings.set`、`bindings.reset` | 键位表、改一个操作的键位、把一个窗口（或全部）的键位恢复默认 |
| `storage.status`、`storage.change_data_dir`、`storage.change_models_dir`、`storage.restart` | 存储位置、更改数据目录或模型目录并等到完成、立即重启 |
| `logs.tail` | 主日志的最后 n 行 |
| `quit` | 退出程序 |

各方法的参数与返回示例见 `docs/automation.md`。下面几节说明它们与程序内部的衔接。

#### 16.4.1 截屏

`capture` 的矩形 `[x, y, 宽, 高]` 用原生坐标，也就是全局物理像素，与 11.4.3 节的约定一致：屏幕在原生坐标里的左上角与它的逻辑左上角相同，宽高是冻结画面的像素数 $(W, H)$，所以屏幕 $s$ 占的原生矩形是

$$
N_s = [o_x,\ o_x + W) \times [o_y,\ o_y + H).
$$

`status.screens` 按这个规则列出每块真实屏幕的原生矩形，agent 据此选截屏范围。

`direct` 模式调用截屏控制器的 `capture_rect`：冻结全部屏幕，把矩形 $R$ 与每块屏幕的 $N_s$ 求交，每个非空交集 $R \cap N_s$ 从屏幕 $s$ 的冻结画面里裁出，放到截图里的位置 $(R \cap N_s) - (R_x, R_y)$。缩放比例不同的屏幕在原生坐标里首尾相接、互不重叠，各部分拼起来就是矩形里有屏幕的部分，屏幕之间的空隙留黑。矩形只落在一块屏幕上时，裁切与框选完全相同（`to_raw_image`），测试逐字节比较两者。

覆盖窗口按矩形面积最大的那块屏幕 $s^\ast$ 摆放，面积相同时取屏幕列表里靠前的一块。选区的逻辑矩形由 11.4.3 节的 `logical_rect` 换算：先把 $R$ 写成相对 $s^\ast$ 左上角的物理像素 $P = R - (o_x, o_y)$，再按

$$
l = o + \frac{p}{r}
$$

逐条边换算后取整，$r$ 是 $s^\ast$ 的设备像素比。例如左侧 150% 副屏原点为 $(-2560, -200)$，矩形 $R = (-2470, 10, 1260, 360)$ 时 $P = (90, 210, 1260, 360)$，四条边换算为

$$
x = -2560 + \tfrac{90}{1.5} = -2500,\quad y = -200 + \tfrac{210}{1.5} = -60,\quad
w = \tfrac{1350}{1.5} - \tfrac{90}{1.5} = 840,\quad h = \tfrac{570}{1.5} - \tfrac{210}{1.5} = 240,
$$

与在蒙版上框出同一块得到的 `(-2500, -60, 840, 240)` 相同。

`mask` 模式走真实的蒙版：发 `StartCapture` 同样的 `controller.start()`，找到矩形左上角所在屏幕的蒙版，把起点与终点从原生坐标换成蒙版窗口内的逻辑坐标（11.4.3 节第一个公式减去屏幕的逻辑左上角），再按“框选区域”当前的第一个键位合成按下、移动、松开。事件经过蒙版的 `MouseGestures` 与控制器，选区按 CAP-02 限制在按下时所在的屏幕内，结果与用户拖动相同。带 `hold: true` 时只按下并移动、不松开，蒙版带着选区停在屏幕上，`grab` 可以截下它，之后用 `capture.release` 松开或 `capture.cancel` 取消。

#### 16.4.2 合成事件

`lst/ui/common/inject.py` 按键位文字（11.8.1 节的写法）合成键盘、鼠标与滚轮事件，用 `QApplication.sendEvent` 交给目标窗口。事件经过窗口的 `event`、`mousePressEvent` 等处理函数，查的是窗口当前的 `Keymap`，所以改键位之后用旧键位注入不再触发操作，用新键位注入才触发，这正是“改键后是否生效”要验证的事情。各种键位合成的事件序列：

- 键盘：按下、松开；
- 单击：按下、松开；
- 双击：按下、松开、双击、松开，与 Qt 收到真实双击时的顺序相同；
- 拖动：在起点按下，均分 8 步移到终点，在终点松开。第一步的位移一般就超过 4 个逻辑像素，第一次移动即开始拖动；
- 滚轮：转 `steps` 格，每格 120。

拖动期间窗口本身可能移动（覆盖窗口的“拖动窗口”）。真实鼠标的全局坐标不随窗口移动，所以合成时在按下那一刻按窗口的位置一次算好全部全局坐标，之后每个事件的窗口内坐标由全局坐标减去窗口当前的位置得到。

`overlay.key`、`overlay.mouse`、`capture.key`、`capture.mouse` 的 `action` 参数取操作当前的键位：按键方法取第一个键盘键位，鼠标方法取第一个鼠标或滚轮键位。

#### 16.4.3 等结果

自动化接口模块订阅 `CaptureCompleted`、`OcrCompleted`、`OcrFailed`、`SegmentsUpdated`、`SessionProgress`，按会话号记下识别与竞速的结果。识别结果来自总线消息，不读 OCR 模块与竞速模块的内部状态。记录只在管道建好期间进行，只保留最近 8 个会话，关掉开关时清空，不写盘。一个会话“已定”的条件是下面三条之一：

1. 识别失败；
2. 识别完成且没有段；
3. 识别完成，并且每个段都收到过 `finished` 为真的快照，或者 `SessionProgress` 报告全部段已定。

`wait_result` 等到已定为止，超时回复 `timeout`，`data` 里是那一刻的结果。返回内容包括截图尺寸与选区、各版面块、各段的原文、当前显示的文字、`shown_engine`、跳过原因、每个尝试的状态与耗时，以及覆盖窗口的状态（阶段、进度、显示原文还是译文、选中的范围与文字、各段在窗口里的矩形）。覆盖窗口与记录器处理的是同一批消息，返回前让出一轮事件循环，窗口先应用最后一次刷新再被读取。

#### 16.4.4 覆盖窗口

选字、切换、缩放、固定、关闭都调用覆盖窗口自己的方法，与展示器和测试调用的相同。`overlay.copy` 缺省只返回选中的文字，不碰系统剪贴板，避免覆盖用户剪贴板里的内容；带 `clipboard: true` 时另外走标准复制键的路径（`copy_selection`），由覆盖窗口模块写剪贴板。

覆盖窗口在失去焦点时关闭（`OV-05`）。agent 在另一个程序里发命令时，覆盖窗口有可能被激活后又失去焦点，操作之前先 `overlay.pin` 可以避免它中途关闭。

#### 16.4.5 设置与键位

`settings.set` 给 `UpdateConfig` 附上唯一请求号，等待同号的 `ConfigUpdateCompleted`。配置服务在修改锁内完成校验和落盘后发送回执，回执包含该次操作完成时的配置快照、是否变化与拒绝原因。无变化和全部拒绝均有回执；超时回复 `timeout`。并发修改按各自请求号对应，其他配置的修订号变化不会提前完成这次调用。

`bindings.set` 与 `bindings.reset` 都换算成 `[bindings]` 节的 `settings.set`，与设置页的键位卡片走同一条路径，冲突与格式错误由配置校验拒绝（11.8.4 节）。

#### 16.4.6 存储位置与重启

`storage.change_data_dir` 与 `storage.change_models_dir` 调用常规页存储位置卡片的同一套判断（`StorageSection.plan_data_dir`、`submit_data_dir` 等）：选回当前目录时撤销还没生效的更改，选的就是当前目录时什么也不做，另一次复制或移动进行中、数据目录由命令行或环境变量指定时不能更改。设置页在这里弹确认对话框，接口调用本身就是确认，但参数里必须写 `confirm: true`，否则回复 `not_confirmed`。路径必须是绝对路径；能不能写入由存储位置模块实际写一次文件来判断（2.9.5 节的写入检查），写不进去时回复 `rejected`，`data` 里是失败原因。提交之后方法跟着 `StorageLocations` 等更改结束，途中的进度记在返回的 `progress` 里。

`storage.restart` 与 `quit` 的动作必须在回复写出之后执行，否则程序先退出，回复来不及写出。服务端为此支持“回复之后的动作”（`Deferred`）：方法返回结果与一个回调，服务端把回复写完再调用回调。`storage.restart` 的回调就是常规页“立即重启”按钮调用的 `restart_app`。旧进程退出、新进程以同样的命令行启动后，新进程建立同名管道；客户端的 `restart_and_reconnect` 反复连接，直到连上的服务端进程号与旧的不同。

### 16.5 客户端

`lst/automation/` 只依赖标准库，import-linter 的契约“自动化接口的客户端只依赖标准库”保证这一点。客户端有两种用法：

- 命令行：源码版 `python -m lst.automation <方法> [JSON 参数]`，安装版 `<安装目录>\runtime\python.exe -I -X utf8 <安装目录>\launcher\worker.py automation <方法> [JSON 参数]`。启动器的 `automation` 子命令设好模块搜索路径后运行同一个模块。结果以缩进的 JSON 打印到标准输出；退出码 0 表示成功，1 表示接口回复了错误，2 表示连不上或连接中断，64 表示参数写错。`storage.restart` 默认等新进程的接口可用后再退出，打印新进程的 `status`；
- Python：`AutomationClient.connect()` 返回一条连接，`call(method, params)` 返回 result，接口回复错误时抛 `AutomationError`（带 `code`、`message`、`data`），连接断开时抛 `ConnectionLost`。测试与示例脚本都用它。

客户端的读写是阻塞的，没有单独的读超时。等待类方法的时限由服务端按参数执行，服务端进程退出时管道断开，读操作随之返回。

### 16.6 构建与发布

构建脚本加 `--flavor normal|dev`（缺省 `normal`）与 `--all`。两种构建共用一份暂存目录，暂存目录里的 `app\installed.json` 只给构建脚本自检用；每编译一种，脚本先写 `build\flavors\<类型>\installed.json`，再调用 ISCC 并传入 `/DFlavor` 与 `/DMarkerDir`。安装脚本的 `[Files]` 用 `Excludes: "\app\installed.json"` 排除暂存目录里的那一份，改装 `MarkerDir` 下的这一份；用一个只有一个文件的假暂存目录实际编译两种安装包，ISCC 的输出列出被压缩的文件，确认了排除与替换都生效。

| | 普通版 | 开发版 |
|---|---|---|
| 安装包名 | `LSTtranslate-<版本>-setup.exe` | `LSTtranslate-<版本>-dev-setup.exe` |
| 校验文件 | 安装包名加 `.sha256` | 安装包名加 `.sha256` |
| `installed.json` 的 `flavor` | `normal` | `dev` |
| AppId 与启动方式 | 相同 | 相同 |

AppId 相同，两种安装包互相覆盖安装，卸载信息只有一份，用户数据不受影响。

同一个 GitHub Release 同时放两个安装包与各自的校验文件。应用内更新只取与当前构建类型相同的那一个：安装包名由 `installer_name(版本, 构建类型)` 拼出，在附件里按文件名完全相等查找，普通版不会选中 `-dev-setup.exe`，开发版也不会选中不带 `-dev` 的安装包。只放了一种安装包的版本，另一种构建类型视为没有可用的更新。下载目录的清理用 `parse_installer_name` 把文件名拆成版本号与构建类型，两种文件名都认得。

`-dev` 夹在版本号与 `-setup.exe` 之间，版本号本身以 `-dev` 结尾时会产生歧义：`0.2.0-dev` 的普通版安装包与 `0.2.0` 的开发版安装包同名。构建脚本因此拒绝以 `-dev` 结尾的版本号。

### 16.7 安全与适用场景

自动化接口的威胁模型是“同一台电脑上、以同一个用户身份运行的程序”。这类程序本来就能读写用户的文件、截取屏幕；开启接口后，它们还能控制 LSTtranslate：截屏、读取识别与翻译结果、修改设置、移动数据目录与模型目录、重启和退出程序。措施如下：

- 普通版默认关闭，开启时托盘通知、悬停提示与设置页都写明状态；
- 管道只允许当前用户连接，拒绝远程客户端，Low 完整性的进程打不开；
- 没有执行任意代码、读写任意文件的方法。`grab` 只写数据目录下的 `tmp/automation`，文件名由程序拼出，`name` 参数只接受字母、数字、下划线与连字符；`logs.tail` 只读主日志；
- 会移动文件或重启的操作要求 `confirm: true`，路径必须是绝对路径并经过写入检查；
- 日志不记参数与结果里的文字。

适用场景是开发与测试：在源码版或开发版上让测试脚本、AI agent 驱动真实界面并检查结果。平时使用的普通版不需要它，开发版装在日常使用的电脑上时，不需要时请在“关于与诊断”页关闭。
