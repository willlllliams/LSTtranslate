# LSTtranslate

LSTtranslate 是常驻 Windows 托盘的翻译工具。同一段文字同时交给多个翻译引擎，谁先返回就先显示谁的译文，优先级更高的引擎返回后再替换，这就是“竞速翻译”。引擎分四类：本地离线 NMT（CTranslate2 加载 Argos 模型）、在线大模型 API（DeepSeek、OpenAI 等兼容接口）、本地大模型（Ollama、llama.cpp）和传统在线翻译 API（DeepL、Google、百度）。

当前代码版本为 0.2.0-beta.3，各功能的状态如下：

| 功能 | 状态 |
|---|---|
| 双语互译窗口（Alt+E） | 可用：使用选中的翻译服务，输入停顿后自动翻译，Ctrl+Enter 立即发送 |
| 多引擎竞速与译文缓存 | 可用 |
| 设置窗口 | 可用：分组管理常规、热键、截屏、OCR、引擎、竞速、模型、运行环境与存储，修改自动保存 |
| 本地翻译模型下载 | 可用：首次运行引导或托盘菜单“下载翻译模型” |
| 安装与卸载 | 可用：当前用户安装，不需要管理员权限 |
| 截屏翻译（Alt+Q） | 可用：Windows OCR、RapidOCR、PaddleOCR，支持版面整理与译文覆盖 |

## 系统要求

Windows 11 22H2（build 22621）或更高版本，64 位。设置窗口和双语窗口的 Mica 背景依赖这个版本。

## 安装

运行 `LSTtranslate-<版本>-setup.exe`。程序装到 `%LOCALAPPDATA%\Programs\LSTtranslate`，用户数据（配置、缓存、日志、模型）放在 `%LOCALAPPDATA%\LSTtranslate`。安装时可以选择创建桌面快捷方式、登录时自动启动。

开发版安装包名为 `LSTtranslate-<版本>-dev-setup.exe`，自动化接口默认开启。两种安装包覆盖安装同一个应用，显式保存过的自动化开关优先于构建类型的默认值。安装包旁的 `.sha256` 用于校验下载内容。

安装包不含任何模型。首次启动时引导窗口按互译对列出需要的本地翻译模型，点“下载”即可；也可以跳过，只用在线引擎。在线引擎的 API key 在设置窗口的“翻译引擎”页填写，保存在 Windows 凭据管理器里，不写进配置文件。

卸载在“设置 → 应用”里进行。卸载时会询问是否删除用户数据；选“是”会删除配置、缓存、统计、日志、已下载的模型和保存的 API key。程序运行时无法卸载，安装程序会提示先退出。

## 从源码运行

开发环境用 [uv](https://docs.astral.sh/uv/) 管理，Python 版本是 3.13。

```bash
uv sync
uv run python -m lst
```

源码版的数据目录是仓库下的 `data/`，也可以用 `--data-dir` 指定。`--log-level DEBUG` 打开详细日志。`--flavor dev`（或环境变量 `LST_FLAVOR=dev`）按开发版运行，自动化接口默认开启。

## 自动化接口

程序带一个本机命名管道上的自动化接口，供测试脚本与 AI agent 驱动截屏翻译、覆盖窗口、键位、设置、存储位置迁移和重启，不需要模拟系统的键盘鼠标。普通版默认关闭，在设置的“关于与诊断”页打开；开发版默认开启。只有当前用户的程序能连接。源码版的命令行客户端：

```bash
uv run python -m lst.automation status
uv run python -m lst.automation capture '{"rect": [0, 0, 800, 300]}'
uv run python -m lst.automation wait_result
```

方法、参数与完整示例见 `docs/automation.md`。

OCR 进程使用独立的环境 `.venv-ocr`（PaddlePaddle 与 PyTorch 不能在同一个进程里加载），按 `ocr-requirements.txt` 文件头的说明创建：

```bash
uv venv .venv-ocr --python 3.13
uv pip install --python .venv-ocr\Scripts\python.exe -r ocr-requirements.txt --override ocr-overrides.txt
```

## 测试与检查

```bash
uv run pytest
uv run basedpyright
uv run ruff check lst tests
uv run lint-imports
```

`scripts/check.bat` 依次运行以上全部检查，另外检查代码格式。OCR 的真实识别测试需要 `.venv-ocr` 和本地模型，缺少时自动跳过。

## 构建安装包

需要 Inno Setup 6（`winget install JRSoftware.InnoSetup`）。

```bash
uv run python scripts/build_installer.py
```

输出在 `dist\LSTtranslate-<版本>-setup.exe`。`--flavor dev` 构建开发版，输出 `dist\LSTtranslate-<版本>-dev-setup.exe`；`--all` 一次构建两种，发布时两个安装包与各自的 `.sha256` 放进同一个 Release。`--skip-iscc` 只准备暂存目录、不编译安装包，`--allow-unhashed` 在不能联网时写入不带哈希的 OCR 运行环境清单。构建过程见 `docs/architecture.md` 第 15 章与 16.6 节。

## 文档

- `docs/requirements.md`：需求规格，按编号列出每项需求。
- `docs/architecture.md`：架构设计，包括事件总线、竞速核心的正确性论证、推理进程与监管器、配置、测试策略、打包与安装、自动化接口。
- `docs/automation.md`：自动化接口的使用说明，写给驱动本程序的测试脚本与 AI agent。
- `docs/ocr-quality.md`：原始截图的标注、质量指标与复现方法。
- `docs/server-ocr-analysis.md`：服务器 OCR 模型的检测尺度与分段误差对照。
- `docs/technical-debt.md`：代码审核后保留的技术债与后续验收范围。

## 许可证

本项目以 GNU 通用公共许可证第 3 版（GPLv3）发布，全文见 `LICENSE`。分发本程序或它的修改版时，必须以同样的许可证提供完整源码。
