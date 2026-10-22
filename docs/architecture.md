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
| OCR 与版面分析 | PaddleOCR PP-StructureV3，独立 venv | PaddleOCR 3.7.0，PaddlePaddle 3.3.x |
| OCR 文本模式 | PaddleOCR 检测加识别产线（PP-OCRv5 mobile） | PaddleOCR 3.7.0 |
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
2. 截屏蒙版和覆盖窗口要做到物理像素级控制，`QWidget` 和 `QGraphicsView` 的坐标换算直接可控。
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

OCR 进程使用独立的 venv `.venv-ocr`，环境里只装 PaddlePaddle、PaddleOCR、RapidOCR 及其依赖，不装 PyTorch。旧版本遇到的 Paddle 与 PyTorch DLL 冲突（`shm.dll` WinError 127）只在两者同进程加载时出现，这个环境里没有 PyTorch，冲突的前提就不成立。

识别模型的选择：

- 默认用 `PP-OCRv5_mobile_rec`，文本检测用 `PP-OCRv5_mobile_det`，版面检测用 `PP-DocLayout_plus-L`。识别语言为“自动”、简体中文、繁体中文、英文、日文时都用这个通用识别模型。
- 韩文、拉丁、西里尔、阿拉伯、天城等文字系统映射到 `<文字系统>_PP-OCRv5_mobile_rec`。这些多语言模型目前不在模型缓存里，选中后 OCR 进程以配置错误退出。

最初的方案是默认用 PP-OCRv6，文本模式走 RapidOCR。实现时改了两处：RapidOCR 首次使用要联网下载模型，本机模型缓存里也没有 PP-OCRv6。文本模式因此改用 PaddleOCR 自带的检测加识别产线，与版面模式共用一套模型。RapidOCR 仍装在 `.venv-ocr` 里，后端以后再接。

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
- 第一次在 CUDA 上翻译要即时编译内核，本机约 6.8 秒，超过监管器“每批 5 秒”的处理时限。所以设备是 cuda 时，NMT 进程在加载阶段（`ready` 之前）先翻译一句短句完成编译；监管器等待 `ready` 的上限是 300 秒，足够容纳。编译结果缓存在数据目录下，命中缓存后首次调用约 0.13 秒。

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

检测不出的段（页码、日期、金额这类没有语言信息的内容）按“源语言与目标语言相同”跳过，不送去翻译，计费引擎不会白白花钱。单个单词的检测不可靠（实测 Hello 判成意大利语），双语窗口里用户可以手动指定源语言。

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

#### 2.7.2 传统在线翻译接口

| 接口 | 实现要点 |
|---|---|
| DeepL | 认证只用请求头 `Authorization: DeepL-Auth-Key`。官方已取消单请求 50 条 text 的限制，只剩请求体 128 KiB 的上限；默认仍按 50 条打包，控制单次请求的延迟。会话原文放进 `context` 参数，这部分不计费，超出请求体上限时截取最长的可容纳前缀。正式程度用 `prefer_more`、`prefer_less`，对不支持正式程度的目标语言传 `more`、`less` 会让整个请求失败。返回 456 表示配额耗尽，该实例进入“出错”状态，直到用户处理 |
| Google Cloud Translation | 用 v2 接口，API key 放在请求头 `X-Goog-Api-Key` 里，不放在 URL 中，避免出现在日志和异常信息里（`CFG-04`）。单请求最多 128 条；官方建议 v2 单次不超过 5000 码位，所以默认按 5000 字符打包。Google 用 403 同时表示无权限和限流，按错误响应里的 reason 区分：`rateLimitExceeded` 等记为限流并重试，`dailyLimitExceeded` 记为配额耗尽，其余记为认证错误 |
| 百度翻译开放平台 | 用 POST 提交，原文不进入 URL（`PRV-02`）。签名为 `MD5(appid+q+salt+密钥)`。多段用 `\n` 拼接，段内换行先替换为空格，否则返回的段数对不上。单次请求不超过 6000 字节（UTF-8），超出时按字节拆成多次请求顺序发送。百度在 HTTP 200 里用 `error_code` 报错：超时 52001、系统错误 52002 等 1 秒重试，访问频率受限 54003 等 1 秒、长请求过于频繁 54005 等 3 秒重试，重试次数与 HTTP 层共用。用令牌桶按所在版本的每秒请求数（Queries Per Second，QPS）限速，重试也要先领令牌 |

#### 2.7.3 超时与重试

| 引擎类别 | 超时 | 重试 | 默认并发 |
|---|---|---|---|
| 本地 NMT | 单批处理 5 秒，从 NMT 进程开始处理时计时；任务总时限 10 秒 | 不重试 | 1 |
| 在线 LLM | 连接 3 秒；首 token 8 秒；两次数据之间空闲 10 秒；总计 60 秒 | 429、5xx、断流最多重试 1 次 | 2 |
| 本地 LLM | 首次请求首 token 60 秒（含模型加载），之后 15 秒；总计 60 秒 | 503 最多重试 1 次 | 取服务端并行数，Ollama 默认 1 |
| DeepL、Google、百度 | 总计 8 秒 | 限流、5xx、网络错误最多重试 1 次 | 2；百度按 QPS 限速 |

重试次数是每个实例的配置项 `max_retries`（`ENG-05`），取值 0 至 3，默认 1。网络类引擎默认最多重试 1 次。竞速中其他引擎本身就是冗余，一个引擎反复重试只会拖长它处于“仍可能改变显示”状态的时间，推迟竞速结束（6.3 节）。重试前的等待按指数退避加随机抖动计算，响应带 `Retry-After` 时以它为准（`ENG-05`），等待时间计入任务的总时限，不计入首 token 时限：首 token 时限从每次发送开始算，只包住单次发送。最初的实现让退避等待处在上一次请求设下的首 token 时限里，较慢的 5xx 或 429 之后重试根本没发出，还被误报成超时。

LLM 引擎的整批阶段，HTTP 层重试与断流重发共用一份额度，合计最多 `max_retries + 1` 个请求；逐段退回时每段各有同样的额度。一个任务的请求数上限是 $(\text{max\_retries} + 1)(1 + m)$，$m$ 是整批阶段之后仍缺的段数。默认配置下整批最多 2 个请求，每个退回段最多再 2 个。

本地 NMT 不重试，它的失败只有两种来源：模型缺失这类确定性错误，重试结果相同；进程崩溃，由监管器处理（8.2 节）。本地 NMT 的配置里 `max_retries` 固定为 0。

本地 NMT 有两个时限，计时范围不同：

- 单批处理时限（默认 5 秒）交给监管器，从 NMT 进程发出 `started` 时计时，超时说明进程卡死，监管器结束并重启它。
- 任务总时限（默认 10 秒）交给调度器，从任务开始执行算起，包括在 NMT 进程里排队的时间。NMT 进程只有一个推理线程，被取消的批次仍会跑完，新请求最坏要先等它跑完，再算自己的处理时间，所以总时限取处理时限的两倍。

本地 NMT 引擎的 `supports` 依据 NMT 进程最近一次 `ready` 消息里的能力清单判断。NMT 进程首次就绪之前（程序启动后的一两秒内），本地 NMT 不参加竞速；首次就绪之后，即使进程崩溃重启，能力清单仍然保留，请求在监管器中排队等待（8.2 节第 6 条）。能力清单里的模型指纹参与缓存键，`services.engines` 在每次收到指纹不同的 `ready` 时重建本地 NMT 实例，并发布新的 `EngineRoster`，模型更新后旧译文因此不会被误用。

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

统计库会随使用无限增长，实测每个尝试约 188 字节。保留策略（例如把 90 天前的明细折算成按实例累计的计数）放在 M7 里程碑实施。

#### 2.9.4 日志

每个进程写自己的按天滚动的日志文件。Windows 上 `TimedRotatingFileHandler` 滚动时要重命名文件，多个进程共用一个文件会报 WinError 32。

- 主进程用 `QueueHandler` 入队，由 `QueueListener` 线程落盘，主线程不做文件读写（`REL-04`）。
- 子进程启用 `faulthandler`，原生库崩溃时的堆栈写到标准错误，由主进程读取后记入日志，用于定位 `REL-01` 中的崩溃原因。

日志的脱敏规则见第 10 章。

#### 2.9.5 数据目录

配置、缓存、统计、日志和模型默认都放在程序目录下的 `data/` 中，位置可以在配置里修改。不放进 `%LOCALAPPDATA%` 等用户目录，原因是：程序以源码加虚拟环境的方式运行，所在的目录树可能带有 Low 强制完整性标签（某些沙箱工具会给工作区打上这个标签），这时虚拟环境里的 Python 以 Low 完整性运行，写不了用户目录，只能写所在的目录树。旧版本 PP-StructureV3 加载失败就是这个原因。`data/` 不纳入版本库。

同样的原因，`tempfile.gettempdir()` 在这种环境下会退回当前目录。需要临时文件的模块一律在 `data/tmp/` 下创建。唯一的例外是模型下载：下载中的文件写在模型目录下的 `<名>.part`，因为下载完成后要用 `os.replace` 原子改名，原子改名要求临时文件与目标文件在同一个卷上，而模型目录可以配置到别的盘（8.5 节）。

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
| OCR 进程 | `.venv-ocr`（PaddlePaddle、PaddleOCR、RapidOCR） | 加载 OCR 与版面分析模型，执行识别 | 主进程重启它，当前识别任务失败并提示重试（`OCR-08`） |
| NMT 进程 | `.venv`（与主进程共用） | 用 CTranslate2 加载本地 NMT 模型，执行翻译 | 主进程重启它，本地 NMT 实例在当前会话中记为失败，其他实例继续竞速（`ENG-06`） |
| Ollama / llama.cpp server | 用户自行安装 | 本地 LLM 推理 | 由用户管理；主进程把它当作一个 HTTP 服务 |

OCR 和 NMT 分成两个进程，理由有三条：

1. 环境不同：OCR 进程需要 PaddlePaddle 的独立环境（2.4 节），NMT 进程直接用主进程的环境。
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
    engines/        各类翻译引擎的实现（每类一个子包）
    ocr/            OCR 进程客户端、段落合并、版面结果转换
    config/         配置读取、校验、写回、密钥
    cache/          译文缓存
    stats/          竞速统计与会话耗时记录
    models/         本地模型的索引、下载、删除
    diagnostics/    诊断信息导出
  workers/          子进程入口
    ocr_worker/
    nmt_worker/
  ui/               界面模块，依赖 Qt
    theme/          Fluent 主题、颜色、字体、图标（各界面模块共用）
    i18n/           界面文案与错误码的本地化（各界面模块共用）
    hotkeys/        全局热键注册
    tray/
    capture/        截屏蒙版
    overlay/        截屏翻译覆盖窗口
    bilingual/      双语窗口
    settings/       设置窗口
    onboarding/     首次运行引导
  app/              组合根：启动流程、进程监管、端口实现、模块装配
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

`app` 中的组合根按固定顺序启动模块：先 `config`，再 `cache`、`stats`、`models`，然后 `ocr`、`engines`、`race`，最后启动全部界面模块。退出时按相反顺序停止。

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
| `TranslateText(session, text, source, target, include_billing)` | 命令 | ui.bilingual | services.race |
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
| `InstallOcrEngine(kind)`、`CancelOcrEngineInstall(kind)`、`UninstallOcrEngine(kind)` | 命令 | ui.settings | services.ocr_engines |
| `OcrEnginesChanged(kind, installed)` | 事件 | services.ocr_engines | （保留给需要区分安装与卸载的订阅方） |
| `UpdateStatus` | 状态 | services.updates | ui.tray、ui.settings |
| `CheckForUpdates(manual)`、`DownloadUpdate`、`InstallUpdate` | 命令 | ui.tray、ui.settings | services.updates |
| `ShowModelDownloads` | 命令 | ui.tray（菜单“下载翻译模型”） | ui.onboarding（重新显示引导窗口） |
| `RefreshModelCatalog`、`DownloadModels(ids)`、`CancelModelDownload(id)`、`DeleteModel(id)` | 命令 | ui.onboarding、ui.settings | services.models |
| `ModelsInstalled(ids, removed)` | 事件 | services.models | app（重载 NMT 进程） |
| `AppConfig` | 状态 | services.config | 全部模块 |
| `UpdateConfig(patch)` | 命令 | ui.settings、ui.onboarding | services.config |
| `UpdateSecret(name, value)` | 命令 | ui.settings、ui.onboarding | services.config |
| `ConfigRejected(location, reason)` | 事件 | services.config | ui.tray（1 秒内合并成一条通知）、ui.settings |
| `NotificationRequested(title_key, message_key, level, params)` | 事件 | app、各模块 | ui.tray |
| `HotkeyRegistrationFailed(hotkey, reason)` | 事件 | ui.hotkeys | ui.tray |
| `ExportDiagnostics` | 命令（尚未定义，随 services.diagnostics 落地） | ui.tray、ui.settings | services.diagnostics |
| `QuitRequested` | 事件 | ui.tray | app |

`TranslateText` 的 `source` 和 `target` 为空时表示“自动”（`BI-03`）；`text` 去掉空白后为空表示取消双语窗口的当前会话。会话号由双语窗口决定，沿用当前会话号并带 `include_billing` 表示让计费实例晚加入（11.6 节）。`AttemptRecorded` 只带状态、耗时和是否命中缓存，不带译文（`PRV-02`）。

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

每个段有一个 `SegmentRace`，保存该段全部尝试和当前胜出者；每个会话有一个 `SessionRace`，保存全部段并记录哪些段需要刷新界面。没有参加竞速的段带一个原因：源语言与目标语言相同（`RACE-09`），或者没有任何实例支持该语言对。

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

属于 $H$ 的尝试称为“竞争中”。一个段有两个结束概念：

| 概念 | 条件 | 用途 |
|---|---|---|
| 显示已定（finished） | $H = \varnothing$ | 竞速结束，进度计数（`OV-07`），节省模式的剪枝依据 |
| 全部完成（complete） | $E$ 中所有尝试都已结束 | 完整模式下判断比较数据是否齐全（`RACE-05`） |

全部完成蕴含显示已定，因为没有未结束的尝试时 $H$ 必然为空。显示已定时，$w \neq \varnothing$ 表示竞速成功结束；$w = \varnothing$ 表示所有实例都失败或被剪枝，界面显示原文和各实例的失败原因（`RACE-04`）。

**引理 1（$H$ 单调不增）**：参赛集合 $E$ 不变时，对任意时刻 $t < t'$，有 $H_{t'} \subseteq H_t$。

证明：只需证明任何一次状态变化都不会让某个尝试新进入 $H$。状态变化有三种：

- 某个尝试开始执行（QUEUED → RUNNING）：它仍未结束，$w$ 不变，所有尝试的成员资格不变。
- 某个尝试被剪枝或收到结果：它进入已结束状态，离开 $H$。若结果使 $w$ 改变，新胜出者 $w'$ 满足 $w = \varnothing$ 或 $p(w') < p(w)$。条件“$p(e) < p(w')$”比“$p(e) < p(w)$”更严格，所以没有尝试因此进入 $H$。
- 其他尝试的状态不受影响。

**定理 1（显示已定后不再变化）**：参赛集合不变时，若某段在时刻 $t$ 显示已定，则 $t$ 之后 $w$ 不再改变。

证明：用反证法。设 $t' > t$ 是 $t$ 之后 $w$ 第一次改变的时刻，这次改变由尝试 $e$ 的成功结果引起，所以在 $t'$ 之前 $e$ 未结束，并且 $w_{t'^-} = \varnothing$ 或 $p(e) < p(w_{t'^-})$。在 $t$ 与 $t'$ 之间 $w$ 没有变化，所以 $w_{t'^-} = w_t$。已结束的尝试不会回到未结束状态，所以 $e$ 在时刻 $t$ 也未结束。于是 $e \in H_t$，与 $H_t = \varnothing$ 矛盾。

**引理 2（剪枝不改变终态）**：节省模式只剪枝不在 $H$ 中的排队尝试。对任意一组结果，剪枝后的终态与这些尝试照常执行时的终态相同。

证明：设尝试 $e$ 在时刻 $t$ 被剪枝，此时 $e \notin H_t$ 且 $e$ 未结束，所以 $w_t \neq \varnothing$ 且 $p(e) > p(w_t)$。假如 $e$ 照常执行，并在之后某个时刻 $t''$ 返回成功结果。由性质 1，$p(w_{t''}) \le p(w_t) < p(e)$，所以这个结果不会改变 $w$。失败结果本来就不改变 $w$。因此 $e$ 是否执行，不影响任何时刻的 $w$。

引理 2 说明节省模式与完整模式的显示完全相同，两者的区别只在花费和统计数据是否齐全。

**活性**：在前提 3 下，每个段最终都会显示已定。$E$ 是有限集合，每个尝试在有限时间内结束，所以最终没有未结束的尝试，$H \subseteq \{\text{未结束的尝试}\} = \varnothing$。最高优先级实例失败时同样如此：例如 DeepSeek 返回 401 后，它离开 $H$，剩下的竞争中尝试结束后竞速就结束。

**晚加入**：双语窗口按 `Ctrl+Enter` 时，计费引擎加入一个已经开始的会话（`BI-04`）。新实例进入 $E$，处于 QUEUED 状态。如果它的优先级高于当前胜出者，它属于 $H$，该段从显示已定变回未定。这一步扩大了 $E$，引理 1 和定理 1 的前提不再成立，所以“显示已定”是由当前状态算出来的属性，合并器每次查询时重新计算。已经被剪枝的尝试保持 CANCELLED，不会重新参赛；已经参赛的实例不会重复加入。计费实例在会话进行中被替换时（用户补填了 API key，revision 改变），晚加入改用当前同号的新实例；旧实例在某段上的尝试若是 FAILED，新实例以新尝试替换它重新参赛。被替换的尝试从来不是胜出者，胜出者不变，引理 1 中“胜出者优先级单调不增”仍成立。会话开始后才新增的实例号不加入。

### 6.4 任务打包、出队剪枝与流式结果

调度器不直接为每个段发一次请求。每个引擎实例声明自己的打包参数，调度器把同一实例、同一语言对（源语言，目标语言）的段按顺序贪心打包成任务（Job）：一个任务的段数不超过 `max_batch_segments`，文本总长不超过 `max_batch_chars`，单段超长时单独成为一个任务。

| 引擎类别 | 打包参数的取值依据 |
|---|---|
| 本地 NMT | NMT 进程的批量推理上限 |
| 在线 LLM、本地 LLM | 上下文窗口与单次输出长度；任务同时附上会话原文作为上下文（`ENG-04`） |
| 传统在线接口 | 接口的单次请求条数和字符上限（2.7.2 节） |

语言对按段区分，是因为双语窗口的互译对规则（`RACE-10`）会让同一会话里不同段的目标语言不同。

每个引擎实例有一个两级任务队列，以及数量等于最大并发数的工作协程（`ENG-05`）。双语窗口的任务排在第一级，截屏的任务排在第二级，工作协程先取第一级，第一级为空时再取第二级，同一级内先进先出。双语窗口是边打字边看结果，目标是 500 毫秒出译文；一张截图可能有几十个任务，不能挡在它前面。

节省模式（`RACE-05`）的剪枝分两处进行，判据相同：尝试仍处于 QUEUED，并且不在 $H$ 中。

- 即时剪枝：某个段的胜出者改变后，调度器立即把该段所有满足判据的尝试标为 CANCELLED。界面和统计马上看到“已取消”，不必等这些尝试排到队首。
- 出队兜底：工作协程取出一个任务后、调用引擎前，对任务里的每个段检查该实例的尝试。尝试已不在 QUEUED 状态（例如已被即时剪枝）时，从任务中移除这个段；节省模式下尝试满足判据时，标为 CANCELLED 并移除；其他情况标为 RUNNING，保留在任务中。剪枝后任务为空，就直接丢弃，不发请求。

两处剪枝可以并存，因为出队时先检查状态：已取消的尝试只会被移除，不会被重新启动。判据本身是稳定的：由引理 1，一个尝试一旦不在 $H$ 中，之后再也不会回到 $H$；晚加入只会让新实例进入 $H$。剪枝的时机也早于“竞速结束”：例如 DeepSeek 还在运行、Argos 已经返回时，优先级低于 Argos 的另一个排队实例已经可以剪掉。任务在队列里时只有尝试的状态会变，任务本身的段列表只在出队这一刻修改，所以不存在“修改任务时它正好被取出执行”的竞争。

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
5. `services.ocr` 在主进程中做段落合并，把版面块转换成段，发布 `OcrCompleted`。段落合并是纯 Python 的几何计算，耗时在毫秒级，并且需要回归测试（`OCR-05`）；放在 `services` 里，测试时不需要启动 OCR 进程。
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
- 模型包第一次用到时才解压到数据目录，目录名带模型包的内容摘要，解压过的不重复解压。能力清单里的模型指纹由 beam_size、实际使用的设备和计算类型、每个模型包的语言对、版本和内容摘要计算得出。
- 请求按到达顺序处理。收到 `cancel` 时，按请求号丢弃还没开始的请求。
- 非英语语言对按 2.5 节的方式经英语中转。第一跳的结果不发给主进程，直接送入第二跳。

### 8.4 OCR 进程

OCR 进程的后端由当前 OCR 引擎决定（`OCRE-01`，引擎的安装与选择见 8.6 节）：

| 引擎 | 模式 | 后端 | 运行环境 | 输出 |
|---|---|---|---|---|
| Windows 自带 OCR（默认） | 只有文本模式 | `winocr_backend.WinOcrBackend`，经 pywinrt 调用 Windows.Media.Ocr | 主环境 | 一个覆盖整张截图的正文块，lines 为每一行及其位置 |
| PaddleOCR | 版面模式 | PP-StructureV3 | PaddleOCR 运行环境 | 完整版面块 |
| PaddleOCR | 文本模式 | PaddleOCR 检测加识别产线 | PaddleOCR 运行环境 | 同 Windows 引擎 |

全部引擎遵守同一条位置约定（`OCR-10`）：能给出文字位置的引擎按行返回各自的 bbox；只返回文字的引擎由 `plain_text.plain_text_blocks` 把整张截图作为一个 bbox 输出单个正文块，覆盖窗口把译文铺满选区。

Windows 引擎的要点：

- 像素转成 Bgra8 的 SoftwareBitmap 交给系统识别；长边超过 `resize_max_side` 与系统上限（本机 10000）中较小者时，先用面积平均等比缩小，坐标再换算回原图。
- Windows 不给置信度，行的 score 记为 1.0。系统返回的中文行在汉字之间插了空格，所以行文字按词重新拼接：汉字、假名、全角标点之间直接相连，拉丁词之间加空格。
- 一个识别器只认一种语言。识别语言为“自动”时用用户配置文件语言创建识别器，失败再取第一个可用语言；指定语言时映射到系统语言（例如 zh-Hans 对应 zh-Hans-CN），系统没有该语言时以配置错误退出。本机实测“自动”选到简体中文时，英文单词会识别错，混排场景需要用户指定语言或改用 PaddleOCR。
- 等待异步结果用 `asyncio.run`：Qt 初始化过 COM 的线程是单线程套间，在那里调用 `IAsyncOperation.get()` 实测会抛异常。
- 实测：1920×1080、30 行中英混排的截图识别约 0.33–0.37 秒，30 行全部识别出来。

识别模式只对 PaddleOCR 有效。`OcrConfig.mode` 保留用户的选择，`effective_mode()` 在当前引擎是 Windows 时一律给出文本模式，请求、处理时限与结果转换都按它走；切回 PaddleOCR 时恢复原来的模式。

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

OCR 子能力的开关和预处理参数随每个 `ocr` 请求的 options 发送，OCR 进程以请求里的值为准，改动从下一次识别起生效，不需要重启进程。`configure` 里的同名字段只决定启动时预加载哪些模型；请求用到还没加载的子能力时，OCR 进程当场带上它重建产线，实测补加载公式模型加一次识别约 25 秒，在处理时限之内。组合根按 `[ocr]` 节启动 OCR 进程：`process_key()` 给出决定进程的字段（Windows 引擎只看引擎与识别语言，PaddleOCR 再看识别模式与推理设备），这些字段、`enabled`、引擎可用性或模型目录变化时重载，子能力开关变化不重载。

模型目录与联网：组合根把模型缓存目录作为 `model_cache_dir` 写进 `configure`（源码版在 `data/models/ocr` 没有模型时回退到旧项目的 `assets/PaddleOCR_and_PP-Structure`）。OCR 进程给每个要用的模型显式写上 `model_dir = <模型缓存>/official_models/<模型名>`，PaddleX 拿到目录就直接加载，不经过下载逻辑；有模型缺失时以配置错误（退出码 66）退出，列出全部缺失的模型。进程入口还用审计钩子拦截一切对非回环地址的解析和连接（`network_guard.py`），作为 `PRV-01` 的兜底。

OCR 进程本身从不下载。PaddleOCR 的运行环境与模型由主进程的 `services.ocr_engines` 在用户点“下载”后安装（8.6 节，`MOD-02`、`OCRE-02`），OCR 进程的网络拦截因此可以一直开着。当前引擎不可用（Windows 没有 OCR 语言、PaddleOCR 未安装）时，组合根不启动 OCR 进程。

推理设备设为 GPU 而安装的 PaddlePaddle 没有编译 CUDA 时，OCR 进程以依赖错误（65）退出；编译了 CUDA 但没有可用 GPU 时以配置错误（66）退出，与 NMT 进程一致。导入 paddle 时会短暂运行一次 `where ccache`，识别期间没有常驻子进程（8.2.3 节要求确认的事项）。

OCR 进程严格串行处理请求。新截图到达时，services.ocr 先取消旧请求，再立即提交新请求（`OCR-06`）：旧请求还没开始就被丢弃，已经开始就照常跑完、结果由主进程丢弃。监管器重载或停止使当前请求以 `cancelled` 结束时，services.ocr 自动重新提交一次，新请求在监管器里排队等新进程；同一会话再次被取消时发布 `OcrFailed(engine.worker_crashed)`，覆盖窗口不会一直停在“识别中”。

识别结果的版面块格式写在 `lst/services/ocr/blocks.py` 的模块说明里。坐标是相对**原始**截图的物理像素，OCR 进程缩放过图像时要换算回原图。文本模式下 OCR 进程只返回一个覆盖整张截图的 text 块，段落合并在主进程里做，覆盖窗口按段的范围（`OcrSegment.bbox`）而不是块的范围渲染译文。

OCR 失败沿用 `engine.*` 错误码（`engine.timeout`、`engine.worker_crashed`、`engine.internal`），目前没有 OCR 专用的错误码。识别各阶段的耗时（排队、识别、转换）只写 INFO 日志，还没有进入统计（`OBS-02`）。

### 8.5 模型管理

`services.models` 负责本地模型（`MOD-01` 至 `MOD-04`）：

- NMT 模型：读取 Argos 的模型索引，在 `ModelCatalog` 状态里列出可下载的语言对和已安装的模型。下载、校验、安装的流程见 8.5.1 节。
- OCR 模型：由 PaddleX 在 OCR 进程预加载时自动下载到模型目录。进程通过 `progress` 消息报告进度，托盘通知显示进度（`MOD-02`）。
- 本地 LLM：通过 Ollama 和 llama.cpp server 的模型列表接口读取已有模型，`services.models` 不负责下载（`MOD-03`）。
- 模型目录来自配置（`MOD-04`）。OCR 进程启动时，监管器把 `PADDLE_PDX_CACHE_HOME` 设为该目录。

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

模型管理模块的目录必须与 NMT 进程扫描的目录相同。源码版在 `data/models/nmt` 没有模型时回退到旧项目的 `assets/argosmodel`，组合根把同一个解析结果交给两边；两边用了不同目录的话，第一次下载之后 NMT 进程会切到 `data/models/nmt`，旧目录里的模型就从目录里消失了。`[nmt].model_dir` 在运行中改变时，组合根停掉旧的模型管理模块，再启动一个指向新目录的实例（`MOD-04`）。

索引无法事先说明一个包用的是哪种分词器。不是 SentencePiece 分词的包要下载完才会以 `models.invalid_package` 失败。

### 8.6 OCR 引擎的安装与选择

`services.ocr_engines` 发布 `OcrEngineCatalog`（契约见 `lst/contracts/ocr_engines.py`），处理 `InstallOcrEngine`、`CancelOcrEngineInstall`、`UninstallOcrEngine` 三个命令（`OCRE-01` 至 `OCRE-06`）：

- Windows 引擎：模块启动时用 pywinrt 读取系统的 OCR 识别语言。一个都没有时状态为“不可用”，错误码 `ocr_engine.no_language`，设置里提示在 Windows 设置的“语言”中添加 OCR 功能；对它发“安装”命令会重新读取一次语言。
- PaddleOCR（安装版）：分两段安装。
  1. 运行环境：用安装包自带的 `tools\uv.exe`，按 `app\ocr-paddle-requirements.txt`（构建时由 `.venv-ocr` 冻结并生成哈希）以 `uv pip install --target` 装进 `<数据目录>\engines\paddle\site.part`，成功后写入清单哈希并原子改名为 `site`。
  2. 模型：逐个从 PaddleX 官方地址下载 `<模型名>_infer.tar`，解压前逐个成员检查（拒绝绝对路径、`..`、盘符、链接与设备文件），校验后原子改名为 `<数据目录>\models\ocr\official_models\<模型名>`。
  全部完成后写 `installed.json`，发布 `OcrEnginesChanged`。进度按字节加权：运行环境按装好后的约 975 MB 计，模型按清单里的大小计；uv 阶段每秒统计一次 uv 缓存目录的大小估计进度。取消或失败时删除半成品，已经装好的部分保留，重试时复用。
  运行环境标记与 `installed.json` 都记录版本清单的 SHA-256，两者都与当前清单一致才算已安装。程序升级带来新清单时，引擎显示“未安装”并说明“版本清单已随程序更新，需要重新安装运行环境；已下载的模型保留”，用户点安装只重装运行环境。新环境装进 `site.part` 后、删除旧 `site` 之前，模块调用组合根的 `before_runtime_update`，组合根先停掉正在使用 PaddleOCR 的 OCR 进程。卸载开始时同步置忙标志，卸载期间的安装命令被忽略；运行时的 uv 命令带 `--no-build`，不在用户机器上从源码包构建。
- PaddleOCR（源码版）：`.venv-ocr` 存在且模型缓存里有模型就算已安装，安装与卸载命令被忽略，模块从不改动开发环境。

默认模型清单对应版面模式加表格识别，另带方向分类与去畸变三个小模型，合计约 968 MB；公式识别（PP-FormulaNet_plus-L，约 731 MB）不在默认清单里。PaddleOCR 装好后数据目录多出约 1.9 GB。

当前引擎写在 `[ocr].engine`，默认 `windows`，安装版装好即可截屏识别。组合根订阅 `OcrEngineCatalog`：每次发布都重新判断 OCR 进程的启动条件（判断有去重，下载进度频繁发布不会重启进程），按引擎给出启动命令，经 `WorkerSupervisor.reload(settings, command=…)` 切换解释器与后端。OCR 进程的状态由组合根合并进目录：启动、加载、崩溃重启中显示“加载中”，就绪与忙碌显示“就绪”，FAILED 显示“出错”（`ocr_engine.worker_failed`）；合并只作用于当前引擎、并且已经装好的条目。卸载当前引擎前，组合根先停 OCR 进程，否则运行环境里的 DLL 被占用，删除会失败。

## 9 配置

配置文件是 `config/settings.toml`。下面是一个节选，展示结构：

```toml
[hotkeys]
capture = "Alt+Q"
bilingual = "Alt+E"

[race]
mode = "saver"             # saver 或 full，见 RACE-05
show_source_marker = { overlay = false, bilingual = true }

[screenshot]
target_lang = "zh-Hans"

[bilingual]
pair = ["zh-Hans", "en"]   # 互译对，见 RACE-10
debounce_ms = 400
auto_billing = false       # BI-04

[ocr]
enabled = true            # 关闭后不启动 OCR 进程，截屏翻译不可用
mode = "structure"         # structure 或 text
device = "auto"           # auto、cpu 或 gpu；PyPI 上的 PaddlePaddle 是 CPU 版
table = true
formula = false
chart = false
text_timeout_s = 30       # 处理时限：文本模式
structure_timeout_s = 60  # 版面模式的基础时限
feature_timeout_s = 60    # 表格、公式、图表每开一项，版面模式的时限再加这么多

[[engines]]
id = "deepseek-flash"
kind = "openai_compat"            # 引擎实现：openai_compat、deepl、google、baidu、local_nmt
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
timeout_s = 10                    # 任务总时限；单批处理时限 processing_timeout_s 默认 5
```

配置服务启动时读取并校验配置（2.9.1 节），然后发布 `AppConfig` 状态。校验除了字段类型和取值范围，还保证两条跨字段约束：

- 各引擎实例的优先级两两不同（6.2 节前提 1）。出现重复时，按（原优先级，列表位置）稳定排序后重新编号为 1、2、3……；只按列表位置编号会推翻用户的意图，例如 `[argos 3, deepseek 1, deepl 3]` 会把原本最高的 deepseek 降成 2。没有重复时原值不动；
- 每个引擎实例的时限是有限值（6.5 节）。

设置窗口通过 `UpdateConfig` 命令提交修改，配置服务校验通过后写回文件，并发布新的 `AppConfig`。各模块的处理方式如下：

| 模块 | 收到新配置后的处理 |
|---|---|
| ui.hotkeys | 重新注册热键 |
| services.engines | 每个实例按自己的输入判断是否重建：配置条目本身，网络引擎再加上 `secrets_revision`，本地 NMT 再加上模型指纹；输入变了才调用工厂，新实例的 `EngineInfo` 与旧实例相等时沿用旧对象。名册有变化时发布新的 `EngineRoster`，调度器按 6.5 节第 4 条处理旧实例 |
| services.ocr | 子能力开关随下一次识别请求生效；识别模式、设备、识别语言变化时请组合根重载 OCR 进程 |
| services.race | 竞速模式切换后立即影响进行中会话里之后的剪枝判断；由引理 2，显示的译文不受影响，区别只在花费和统计是否齐全。目标语言、互译对等设置作用于之后开始的会话 |

缺少 API key 的网络引擎实例状态为“出错”（错误码 `engine.auth`），但照样进入名册：翻译时每段都以认证错误失败，失败原因显示在各个尝试上，用户在双语窗口的引擎列表里就能看到原因。不进名册的只有两种情况：没有 NMT 进程通道的本地 NMT 实例，以及工厂抛出异常的实例。

API key 不经过 `UpdateConfig`。设置窗口发送 `UpdateSecret` 命令，配置服务直接写入 keyring，事件总线和日志都只记录命令的类名（第 10 章）。

## 10 隐私与日志

需求 `PRV-01`、`PRV-02` 和 `CFG-04` 的落实方式如下。

### 10.1 网络请求

- 程序发起的网络请求只有四类：用户启用的在线引擎实例；用户确认的下载（`services.models` 的 NMT 模型、`services.ocr_engines` 的 PaddleOCR 运行环境与模型、`services.updates` 的更新安装包）；检查新版本（只访问 GitHub 的版本列表，可以关闭）；用户配置的本地 LLM 服务。
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

错误码由 `contracts/errors.py` 定义，例如 `engine.auth`（API key 无效）、`engine.quota`（配额耗尽）、`race.engine_removed`（实例因配置变更被移除）。引擎的每个失败结果都带两样东西：

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
| 覆盖窗口的字号测量 | 分帧执行：每帧最多用 8 毫秒测量，未测量的段先用估算字号显示，测完后替换 | `REL-04` |

### 11.3 热键

`ui.hotkeys` 用 `RegisterHotKey` 注册热键，并加上 `MOD_NOREPEAT` 去掉按住时的连发。`WM_HOTKEY` 消息通过 `QAbstractNativeEventFilter` 在主线程收到，然后发送 `StartCapture` 或 `ToggleBilingualWindow` 命令。注册失败时发布 `HotkeyRegistrationFailed`，托盘弹出通知（`TR-05`）；失败原因是与本程序另一个热键相同时，照实说明，不说成被其他程序占用。

两个热键不能设成同一组合，`HotkeysConfig` 的校验器拒绝它，原因随界面语言显示。设置窗口录入热键时，系统会把已注册的组合截走、变成 `WM_HOTKEY`，录入框收不到，所以录入期间暂停全部全局热键：录入按钮进入或退出录入状态时，设置窗口发布 `HotkeyRecordingChanged`，`ui.hotkeys` 收到后注销或按当前配置重新注册。录入按 `nativeVirtualKey()` 生成键名，Shift 加数字录成 `Ctrl+Shift+1`，而不是 Qt 给出的 `Ctrl+Shift+!`。

### 11.4 截屏蒙版

1. 按下热键时，对每块屏幕调用一次 `grabWindow(0)`，得到冻结画面。
2. 每块屏幕创建一个无边框置顶窗口，把截图按 1:1 画到物理像素上。Qt 在混合缩放的多屏之间，逻辑坐标会出现缝隙，所以每块屏幕需要单独一个窗口。
3. 选区边界换算为物理像素：$x_{\text{phys}} = \operatorname{round}(x_{\text{logical}} \times \text{DPR})$，其中 DPR（Device Pixel Ratio）是该屏幕的设备像素比。Qt 6 的鼠标坐标是浮点数，取整误差不超过 0.5 物理像素，满足 `CAP-03`。
4. 鼠标按下时记录所在的屏幕，拖动时把选区限制在这块屏幕的范围内（`CAP-02`）。
5. 按 `Esc` 或单击右键时取消；松开鼠标时选区的宽或高小于 5 个逻辑像素也视为取消（`CAP-04`）。取消后关闭蒙版，不发布 `CaptureCompleted`。

### 11.5 覆盖窗口

覆盖窗口用 `QGraphicsView` 实现。场景坐标就是截图的物理像素坐标，视图变换取 1/DPR。版面块的包围盒因此可以直接用作场景坐标，不需要换算。

- 底图：模糊和调亮（或调暗）在截图到达时计算一次，之后作为静态图片显示。
- 字号适配（`OV-03`）：用二分法求出能让文字放进包围盒的最大字号，判断依据是 `QFontMetricsF` 测出的换行后高度。它是一个纯函数，单独测试；分帧执行方式见 11.2 节。
- 译文替换的过渡（`RACE-06`）：在同一位置叠放新旧两个文字项，用 `QPropertyAnimation` 在 150 毫秒内同时把旧项透明度降到 0、新项升到 1。
- 鼠标操作（`OV-05`）：右键按下时记录位置。松开时，若移动不超过 4 像素，视为单击，切换原文/译文；否则视为拖动窗口。

### 11.6 双语窗口

双语窗口位于 `lst/ui/bilingual`，分成展示器和视图两层。展示器不等待任何异步操作：输入停顿用单次 `QTimer`，发命令用只入队的 `bus.send`，订阅处理函数都是普通函数。qasync 让总线回调、Qt 信号槽和计时器在主线程上依次执行，所以展示器的状态不需要加锁。

#### 11.6.1 发送规则

- 输入停顿用单次 `QTimer` 判断，时长取 `[bilingual].debounce_ms`（默认 400 毫秒），到时发送 `TranslateText(include_billing=False)`。这时计费实例照样查缓存，命中的组合直接以缓存结果参赛，因为缓存命中不产生费用；没命中的组合等用户按 `Ctrl+Enter` 再加入。
- 去重比较三项：去掉首尾空白的文本、源语言、目标语言。三项都没变时不重发。
- 用户按 `Ctrl+Enter` 时，先停止停顿计时器，再发送 `TranslateText(include_billing=True)`。三项都没变时沿用当前会话号，竞速模块让当前会话中还没有参赛的计费实例加入（6.3 节“晚加入”）；有变化时用新会话号，竞速模块开始新会话。
- “是否没变”只由双语窗口判断，竞速模块只看会话号是否相同。两边各自判断会出现分歧：竞速模块按切段结果比较时，`"a
b"` 改成 `"a

b"` 被看作没变，而窗口看作变了；竞速模块于是扩展旧会话，结果以旧会话号发布，被窗口当作过期结果丢弃，计费实例的译文就丢了。
- 输入清空时立即发送空文本（新会话号），竞速模块取消当前会话；上一次发出的已经是空文本时不再发送。
- 切换语言时立即重发，`include_billing=False`。交换语言时，两边都是“自动”则不动，只有一边是“自动”照样交换。

#### 11.6.2 译文区

- 只接受自己最新会话的 `SegmentsUpdated`，截屏会话和旧会话的刷新都丢弃。
- 译文区是只读的 `QTextEdit`，一段对应一个文本块，段内换行转成 U+2028，段号直接换算成块号。一次刷新的全部改动放在一个编辑块里，文档只重新排版一次。
- 新会话的第一帧整体替换文档内容，但不用 `setPlainText`：它会把滚动条拉回顶部，用户边输入边看长文末尾时，每次停顿都会跳回开头。
- 胜出者变化时译文淡入（`BI-06`），文字透明度在 150 毫秒内从 0 升到 1。同一次刷新里开始淡入的段共用一个动画，每帧只改一次文档。
- 来源标记（`OV-06` 在双语窗口的对应项）是段左侧 3 像素宽的色条，在 `paintEvent` 里画，颜色取 `EngineStatuses.colors`，为空时用主题的默认引擎色。
- 占位规则：语言没变时，新会话里原文没改动、还没有胜出者的段，先沿用上一个会话显示的译文；新结果与占位相同时不播放淡入。没有这条规则时，每次停顿所有没改动的段都会先闪回原文，再换成缓存译文并淡入一次。竞速结束仍没有胜出者的段按 `RACE-04` 回到原文。

#### 11.6.3 引擎结果列表

引擎结果列表（`BI-05`）每个实例一行，把它在各段上的尝试合成一行：

| 列 | 规则 |
|---|---|
| 状态 | 有段在翻译中显示“翻译中”，其次是“排队”；都结束后只要有一段成功就显示“完成”，否则依次是“失败”“已取消” |
| 耗时 | 非缓存、已结束尝试的最大值；成功的尝试全部来自缓存时显示“缓存” |
| 译文 | 各段的译文按段落顺序拼接，每行最多显示 300 字，复制按钮复制全文 |
| 失败原因 | `error_text` 给出的原因和处理建议，不显示技术细节 |

排序：胜出段数多的在前，单段输入时胜出者因此排第一；段数相同时近似按优先级排。300 字截断的原因是 `QLabel.setVisible` 会同步重排父布局，不截断时 100 段长文一次刷新要 22 毫秒。

#### 11.6.4 窗口状态与键盘

- 窗口的位置、大小、置顶状态和手动选择的两种语言保存在 `data/ui_state.ini`（`QSettings` 的 INI 格式，`bilingual_window` 组），下次打开时恢复（`BI-01`）。这些属于界面状态，与用户配置分开存放；文件路径由组合根通过 `AppPaths.ui_state_file` 传入。
- 窗口隐藏、模块停止、切换置顶、切换语言时写入并立即落盘。本次运行从没显示过窗口时，模块停止不覆盖已保存的位置。
- 恢复位置时，外框顶边中点下方 12 像素处不在任何屏幕的可用区域内，就移到主屏中央；窗口比主屏大时先缩小。
- 标题栏关闭按钮和 `Alt+F4` 只隐藏窗口。置顶切换在原生窗口已创建时用 `QWindow.setFlags`，避免 `setWindowFlags` 先隐藏再重建造成的闪烁。
- 键盘：`Ctrl+Enter` 立即翻译，`Ctrl+Shift+C` 复制译文，`Esc` 隐藏窗口，`Ctrl+Shift+S` 交换语言，`Ctrl+Shift+T` 切换置顶，`Ctrl+Shift+L` 清空。这些按键在窗口的 `keyPressEvent` 里处理，因为 `QShortcut` 要求窗口处于激活状态。按钮不用 `&X` 助记键，中文文案会一直显示括号字母。各行的复制按钮用 Tab 加空格操作，Tab 顺序与视觉顺序一致，引擎行重新排序后重新设置。

#### 11.6.5 实测

`REL-04` 要求界面刷新不阻塞输入。下表是总线处理函数本身的耗时，随后一轮事件循环里的布局与绘制都在 4 毫秒以内：

| 操作 | offscreen | 原生平台 |
|---|---|---|
| 新会话第一帧（100 段重建） | 3.0 毫秒 | 5.7 毫秒 |
| 100 段首个译文（全部淡入） | 2.6 毫秒 | 3.5 毫秒 |
| 100 段换成更高优先级的译文 | 4.1 毫秒 | 8.0 毫秒 |
| 淡入动画的一帧 | 0.7 毫秒 | 1.1 毫秒 |

### 11.7 设置窗口与首次运行引导

设置窗口由 `ui.settings` 用普通 Widgets 实现 Fluent 风格（2.2 节）：左侧是导航列表，右侧是 `QStackedWidget` 承载的各页，页内的设置项是带标题、说明和控件的卡片，样式全部取自 `ui.theme`。按 `UI-05` 分页：

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

删除实例、清空缓存、重置统计、删除模型都先弹确认框。竞速页的统计表列出执行段数、胜出次数、胜出率、耗时中位数、耗时 P95 和失败率。

还没有实现的部分：

- 常规页的日志级别：没有对应的配置节，目前只能用命令行参数 `--log-level` 设置；
- 关于页的依赖与 GPU 信息：总线上没有这类数据；
- 导出诊断信息：`ExportDiagnostics` 命令尚未定义，按钮禁用。

首次运行引导（`UI-08`）由 `ui.onboarding` 实现，目标是逐项显示以下检测结果：

- OCR 进程 `ready` 消息中的 GPU 与 CUDA 信息；
- `ModelCatalog` 中的本地 NMT 模型，以及 OCR 模型；
- 本地 LLM 服务的连通性。

用户在引导里配置的第一个引擎实例，通过 `UpdateConfig` 和 `UpdateSecret` 提交。

目前实现的是本地 NMT 模型这一项：引导按互译对列出需要的模型（每个方向优先用直连模型，没有直连时用经英语中转的两个模型，与 NMT 进程的中转规则相同），显示大小、下载进度和失败原因，用户一键下载。展示器不依赖 Qt，生成纯数据的画面描述，由视图绘制。

完成状态记在 `data/ui_state.ini` 的 `onboarding/completed` 键下，与双语窗口共用一个文件。点“完成”或“跳过”记录完成；点标题栏的关闭按钮只推迟，下次启动还会出现。已安装但有新版本的模型只显示“可更新”，引导不负责升级。

还没有实现的部分：

- GPU 与 CUDA 检测；
- 本地 LLM 服务的连通性检测；
- 在引导里配置第一个引擎实例；
- OCR 模型。`ModelCatalog` 目前只有 NMT 模型（`ModelKind` 只有一个取值），OCR 模型由 OCR 进程预加载时下载，引导里只有一行说明。

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
| 性能测试 | 需求 6.1 节的全部指标 | `scripts/bench_*.py`，在真实硬件上手动运行，结果记入文档 | 每个里程碑 |
| 静态检查 | 全部代码 | basedpyright strict、ruff | 每次运行 `scripts/check.bat` |

单元测试和性质测试不依赖 Qt、推理库和网络，在只装了开发依赖的环境里就能运行。这一点由第 4.2 节的依赖规则保证。

每个测试有 60 秒的上限（pytest-timeout）。推理进程测试会启动真实子进程并通过管道收发数据，一旦出现死锁，测试会在上限处失败，而不是挂住整个测试运行。Windows 上 `multiprocessing` 管道的缓冲区只有 8 KB，同一个线程先写大块数据再读会互相等待，测试和实现都要把收发放在不同线程里。

## 13 需求追溯

| 需求 | 落点 |
|---|---|
| `TR-01` 至 `TR-03`、`TR-06` | 11.1 节、5.3 节（`EngineStatuses`、`WorkerStatuses`）、8.2 节状态映射、2.9.6 节 |
| `TR-04`、`TR-05` | 11.3 节、第 9 章（热键模块的配置处理） |
| `CAP-01` 至 `CAP-06` | 11.4 节、8.4 节（预处理）、6.5 节（作废旧会话） |
| `OCR-01` 至 `OCR-09` | 2.4 节、8.4 节、第 7 章、8.2 节第 5、6 条 |
| `ENG-01` 至 `ENG-06` | 2.5 节、2.7 节、6.4 节 |
| `RACE-01` 至 `RACE-10` | 第 6 章、2.6 节 |
| `OV-01` 至 `OV-07` | 11.5 节、第 7 章 |
| `BI-01` 至 `BI-07` | 11.6 节、6.3 节“晚加入” |
| `CFG-01` 至 `CFG-05` | 第 9 章、2.9.1 节、2.9.2 节、10.3 节 |
| `MOD-01` 至 `MOD-04` | 8.5 节 |
| `UI-01` 至 `UI-08` | 2.2 节、11.1 节、11.7 节 |
| `REL-01` 至 `REL-04` | 第 3 章、8.2 节、11.2 节 |
| `OBS-01` 至 `OBS-04` | 2.9.4 节、第 7 章第 8 步、10.2 节、10.3 节 |
| `PRV-01`、`PRV-02` | 第 10 章 |
| `MNT-01` 至 `MNT-05` | 第 4 章、第 12 章 |
| `INS-01` 至 `INS-08` | 第 15 章 |

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
app\ocr-paddle-requirements.txt   PaddleOCR 运行环境的精确版本清单（带哈希），按需安装时使用
tools\uv.exe    按需安装 PaddleOCR 运行环境用的 uv，许可证在 tools\licenses\uv\
launcher\       启动器：lst_main.pyw（主程序）、worker.py（推理进程与维护命令）
unins000.exe    卸载程序（Inno Setup 生成）
```

安装包不捆绑 PaddleOCR（`OCRE-02`）。用户下载后，它的运行环境在数据目录的 `engines\paddle\site\`，与主进程依赖分开，PaddlePaddle 和其他依赖不会在同一个进程里加载（2.4 节）。两套依赖共用一个解释器，因为它们的 Python 版本相同；虚拟环境的 `pyvenv.cfg` 写死了解释器的绝对路径，不能随安装目录移动，所以安装版不用虚拟环境，而由启动器设定模块搜索路径。

主进程依赖用 `PySide6-Essentials` 代替 `PySide6`。后者会连带安装 WebEngine 等本程序用不到的模块；换用后 PySide6 部分从数百 MB 降到 214 MB。

### 15.2 启动方式

- 开始菜单快捷方式运行 `runtime\pythonw.exe -I -X utf8 launcher\lst_main.pyw`。`-I` 隔离模式忽略用户机器上的 `PYTHONPATH` 和用户级 site-packages，启动器把 `app\` 与 `site\main\` 放到模块搜索路径的最前面，再调用 `lst.__main__.main()`。
- 推理进程由监管器以 `runtime\python.exe -I -X utf8 launcher\worker.py nmt|ocr …` 启动。`ocr` 子命令带 `--ocr-site <目录>|main`：Windows 引擎用 `main`（即 `site\main`）并透传 `--backend`，PaddleOCR 用数据目录里的运行环境；没有可用目录时以依赖错误（65）退出。组合根的 `worker_command()`（NMT）与 `ocr_worker_command()`（OCR，按引擎）给出命令，源码版用 `.venv` 与 `.venv-ocr` 的解释器。
- 程序判断自己是否是安装版的依据是 `app\installed.json` 是否存在（`lst/app/paths.py`）。

### 15.3 数据目录

安装版的数据目录是 `%LOCALAPPDATA%\LSTtranslate`，源码版仍是程序目录下的 `data/`（2.9.5 节），两者互不影响（`INS-08`）。安装版位于 `%LOCALAPPDATA%\Programs` 下，以中完整性级别运行，可以写用户目录；源码版遇到的 Low 完整性标签问题不出现在安装版上。

### 15.4 安装、升级与运行检测

安装脚本是 `installer\lsttranslate.iss`，用 Inno Setup 6 编译，要点如下：

- `PrivilegesRequired=lowest`：当前用户安装，不弹管理员确认（`INS-01`）。
- 程序运行期间持有命名互斥体 `LSTtranslate.Running`，安装脚本的 `AppMutex` 指向它；安装、升级、卸载时检测到程序正在运行，提示用户关闭（`INS-05`）。
- 可选任务：桌面快捷方式、登录时自动启动（写 `HKCU\...\Run`，卸载时删除）（`INS-02`）。
- 升级时先删除旧版本的 `app\`、`site\`、`runtime\`、`launcher\`、`tools\`，避免残留已经移除的模块；用户数据（包括已下载的 PaddleOCR）不在安装目录里，不受影响（`INS-07`）。
- 应用内更新（15.9 节）以 `/SILENT /SUPPRESSMSGBOXES /NORESTART /RELAUNCH=yes` 运行新安装包。带 `/RELAUNCH=yes` 时，`InitializeSetup` 每 250 毫秒检查一次互斥体，最多等 120 秒，等程序退出后再继续；装完由 `[Run]` 里只在这种情况下执行的条目重新启动程序。用最小脚本实测确认 `InitializeSetup` 在 AppMutex 检查之前执行：不带该参数时静默安装检测到程序运行即退出（退出码 1）；带该参数、互斥体被持有 6 秒时，等待 5 秒后通过检查并完成安装（退出码 0）。
  程序这一侧，入口收到退出请求后先释放运行互斥体，再停止各模块与推理进程，安装程序不必等关停完成。超过 120 秒仍未释放时安装程序放弃这次更新；带 `/RELAUNCH=yes` 而没有装完时，`DeinitializeSetup` 从卸载登记项的 `Inno Setup: App Path` 找到安装目录，重新启动旧程序，用户不会看到程序消失且没有升级。这段 `[Code]` 只编译检查过，没有运行验证。
- `AppId` 固定为 `{435CC09B-5F36-4944-9918-62F7215D2057}`，升级据此识别同一个软件，发布后不能更改。

安装向导目前是英文。Inno Setup 的简体中文语言文件属于非官方翻译，没有随 Inno Setup 一起安装；需要下载后加入 `[Languages]`。卸载时询问是否删除数据的提示已经同时写了中英文。

### 15.5 卸载

卸载程序删除程序文件、快捷方式和登录自启项，然后询问是否删除用户数据（`INS-06`）：

1. 选“是”时，先用安装目录里的解释器运行 `launcher\worker.py purge-secrets`，删除凭据管理器中本程序的全部通用凭据。keyring 保存凭据时，目标名是服务名 `LSTtranslate`，同一服务有第二个条目时改用 `条目名@LSTtranslate`，清理按这两种目标名枚举并删除（`lst/app/uninstall.py`）。这一步必须在删除程序文件之前做。
   枚举凭据时不能用 `*@LSTtranslate` 这样带前导星号的过滤器（Win32 的 `CredEnumerateW` 只支持“前缀加星号”），所以先枚举全部通用凭据，再在程序里按“等于服务名，或以 `@服务名` 结尾”筛选，不区分大小写。keyring 的行为是：每写入一个新条目，原来占用服务名的条目改存到 `条目名@服务名`。清理失败时 `purge-secrets` 以非 0 退出，卸载脚本记日志，不阻止卸载。
2. 程序文件删除之后，再删除数据目录 `%LOCALAPPDATA%\LSTtranslate`。

静默卸载默认保留用户数据，命令行加 `/DELETEUSERDATA=yes` 时删除，供自动化测试使用。Inno Setup 不记录 `[Code]` 段里 `Exec` 的执行情况，安装脚本因此把 `purge-secrets` 的退出码写进卸载日志（`/LOG=文件` 时可见）。

用 `LST_DATA_DIR` 改过数据目录的用户，卸载脚本读取这个环境变量，同时满足三个条件才删除该目录：是绝对路径，不是驱动器根目录，目录里有 `config\settings.toml`。条件是为了避免变量指错时删掉别的东西。

源码版和安装版用同一个 keyring 服务名，凭据管理器里的条目两者共用。在同一台机器上同时使用两种运行方式时，删除数据的卸载也会删掉源码版保存的 API key。

### 15.6 构建

在仓库根目录运行 `uv run python scripts/build_installer.py`，步骤：

1. 复制 uv 管理的独立版 CPython 3.13 到暂存目录，去掉用不到的部分；
2. `uv export` 按 `uv.lock` 导出主进程依赖，`uv pip install --target` 安装到 `site\main`；
3. `uv pip freeze` 冻结 `.venv-ocr` 的精确版本，校验为纯“名==版”并排序（冻结结果里有不止一个 OpenCV 发行包时中止，见下文），再用 `uv pip compile --generate-hashes` 补哈希，写入 `app\ocr-paddle-requirements.txt`；不能联网时加 `--allow-unhashed` 写入不带哈希的清单；
4. 复制本机 `uv.exe` 与许可证到 `tools\`；
5. 复制程序代码、图标和启动器，预编译 `.pyc`，写入 `installed.json`；
6. 用安装布局实际导入主进程的关键模块，并运行 `tools\uv.exe --version`，失败则中止构建；
7. 调用 Inno Setup 编译，输出 `dist\LSTtranslate-<版本>-setup.exe`，并在旁边生成 `.sha256` 校验文件（sha256sum 格式），供应用内更新校验。

构建开头用更新服务的 `parse_version` 校验版本号，不是规范的语义化版本就中止。PEP 440 的写法（`0.2.0b1`）更新服务解析不了，用了会让更新功能整体失效，所以预发布版本要写成 `0.2.0-beta.1`；运行时读到包元数据里归一化后的 PEP 440 写法，由 `semver_text` 换算回来。生成哈希清单的 `uv pip compile` 带 `--no-config --only-binary :all:`，只发布源码包的依赖在构建阶段就会失败。

第 6 步的自检还检查：主环境能导入 Windows OCR 后端与 pywinrt；keyring 的后端是 Windows 凭据管理器；通过真实的 `launcher\worker.py` 分别启动 NMT 与 OCR 进程入口（不带地址参数，期望以配置错误退出，证明按安装布局导入成功，全程不联网）；清单是否带哈希与构建参数一致。

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

- 版本来源：`GET https://api.github.com/repos/willlllliams/LSTtranslate/releases`（`/releases/latest` 不返回预发布版本）。只考虑同时带安装包与 `.sha256` 附件的版本，按语义化版本比较（含预发布后缀），`[updates].include_prerelease` 为假时跳过预发布。不需要任何认证，日志里的地址去掉查询参数。
- 自动检查：启动 30 秒后检查一次，之后按 `[updates].check_interval_hours`（默认 24）检查；最近一次检查与已通知的版本写在数据目录的 `updates.json`，重启后不重复检查，同一版本只通知一次。自动检查失败只记日志；手动检查失败进入 ERROR，托盘给出原因。
- 下载：先取 `.sha256` 文件，要求文件名与安装包一致；安装包流式写入 `tmp\updates\` 的 `.part`，在线程里边写边算 SHA-256，一致才改名并进入 READY。启动时清理 `.part` 与不高于当前版本的旧安装包。
- 安装：以分离进程运行安装包（`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`，不继承句柄），参数见 15.4 节，成功后发布 `QuitRequested`。源码运行时只检查，下载与安装命令进入 `update.not_installed`。
- 托盘菜单有“检查更新”；发现新版本后变为“安装更新 x.y.z”，先下载、再安装，下载中显示进度。
- 命令只生效一次：下载命令在第一次等待之前就进入 DOWNLOADING，安装命令同样先置标志，双击按钮或托盘与设置窗口同时发命令时不会并发下载、不会启动两个安装程序。DOWNLOADING、READY、INSTALLING 状态下的检查命令被忽略，但会重新发布一次当前状态，界面不会停在“正在检查”。
- 下载目录只保留当前可用版本的安装包：启动时删除不高于当前版本的，每次检查成功后删除已被更新版本取代的。
