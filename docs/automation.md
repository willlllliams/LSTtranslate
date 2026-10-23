# 自动化接口使用说明

本文写给用程序驱动 LSTtranslate 的测试脚本和 AI agent（例如 Claude Code）。自动化接口是程序自己的本机命名管道，调用方发一行 JSON 请求，收一行 JSON 回复。截屏、框选、覆盖窗口上的按键与鼠标操作都在程序内部合成，经过窗口自己的事件处理，不需要模拟系统的键盘鼠标，也不需要窗口在前台。设计与安全措施见 `docs/architecture.md` 第 16 章，需求是 `docs/requirements.md` 的 `DEV-01` 至 `DEV-08`。

## 1 开启

接口的开关是配置的 `[automation].enabled`。配置里没有写这个字段时，开关跟随构建类型：开发版开启，普通版关闭。写了 `true` 或 `false` 之后以配置为准。

- 安装版：开发版安装包（`LSTtranslate-<版本>-dev-setup.exe`）装好就是开启的；普通版在设置的“关于与诊断”页打开“自动化接口”开关，立即生效；
- 源码版：启动时加 `--flavor dev`，或设环境变量 `LST_FLAVOR=dev`，例如 `uv run python -m lst --flavor dev --data-dir data/tmp/agent-run`；也可以在设置里打开开关。

接口开启后，托盘图标的悬停提示多一行“自动化接口已开启”，关于页的开关下方显示管道名。管道名按当前用户生成，形如 `\\.\pipe\lsttranslate-automation-3bafc7af28dd`，客户端会自动算出同一个名字。需要同时运行两个实例时，给它们设不同的环境变量 `LST_AUTOMATION_PIPE`（服务端与客户端读同一个变量），或者给客户端传 `--pipe`。

只有以同一个用户身份运行的程序能连上。在 Low 完整性的沙箱里运行的进程连不上，这时命令行客户端报 `connection` 错误。

## 2 命令行

源码版在仓库根目录运行：

```bash
uv run python -m lst.automation <方法> '<JSON 参数>'
```

安装版用安装目录里的解释器经启动器运行：

```bat
"%LOCALAPPDATA%\Programs\LSTtranslate\runtime\python.exe" -I -X utf8 "%LOCALAPPDATA%\Programs\LSTtranslate\launcher\worker.py" automation status
```

成功时结果以缩进的 JSON 打印到标准输出。退出码：

| 退出码 | 含义 | 标准输出 |
|---|---|---|
| 0 | 成功 | 方法的 result |
| 1 | 接口回复了错误 | `{"error": {"code", "message", "data"}}` |
| 2 | 连不上，或连接中途断开 | `{"error": {"code": "connection", ...}}` |
| 64 | 命令行参数写错，例如参数不是 JSON 对象 | `{"error": {"code": "usage", ...}}` |

其他选项：`--pipe <管道名>`、`--connect-timeout <秒>`（缺省 5）、`--no-wait`（`storage.restart` 发出后不等新进程）、`--restart-timeout <秒>`（缺省 120）。

JSON 参数的引号按所用的 shell 写：

- Git Bash：整个参数用单引号包起来，`'{"rect": [0, 0, 800, 300]}'`。Windows 路径写成正斜杠 `C:/Users/me/LST data`，或者在 JSON 里把反斜杠写成两个；
- PowerShell 7.3 及以后：同样用单引号；
- Windows PowerShell 5.1：传给外部程序时会去掉参数里的双引号，要写成 `'{\"rect\": [0, 0, 800, 300]}'`；
- cmd：`"{\"rect\": [0, 0, 800, 300]}"`。

## 3 Python 客户端

```python
from lst.automation import AutomationClient, AutomationError

with AutomationClient.connect(timeout=10) as client:
    status = client.call("status")
    try:
        client.call("overlay.copy")
    except AutomationError as error:
        print(error.code, error.message, error.data)  # unavailable 没有打开的覆盖窗口 None
```

- `AutomationClient.connect(pipe=None, timeout=5.0)`：连接，timeout 秒内管道一直不存在时抛 `FileNotFoundError`。连接时检查管道的所有者是当前用户；
- `call(method, params)`：返回 result；接口回复错误时抛 `AutomationError`，连接断开时抛 `ConnectionLost`；
- `wait_for_server(pipe, timeout, accept)`：反复连接，直到连上并且 `accept(client)` 为真；
- `restart_and_reconnect(client, timeout=120)`：调用 `storage.restart`，等新进程的接口可用后返回新连接，判断依据是服务端进程号变了。

客户端只依赖标准库。安装版上运行脚本时，把 `<安装目录>\app` 放进模块搜索路径，或者用安装目录里的解释器。

## 4 坐标

`capture` 的矩形是原生坐标：全局物理像素，`[x, y, 宽, 高]`，整数。每块屏幕在原生坐标里的左上角与 Windows 显示设置里的位置相同，宽高是屏幕的物理分辨率。`status` 的 `screens` 列出每块屏幕的原生矩形与缩放比例：

```json
"screens": [{"name": "XG27ACS", "native": [0, 0, 2560, 1440], "dpr": 1.5}]
```

覆盖窗口里的坐标（`overlay.select_at`、`overlay.mouse` 的 `at`、`to`）是窗口内的逻辑坐标。`overlay.state` 的 `items[*].rect` 给出每段在窗口里的矩形 `[x, y, 宽, 高]`，按它算点击和拖动的位置。蒙版上的坐标（`capture.mouse` 的 `at`、`to`）用原生坐标。

## 5 方法

参数都写在一个 JSON 对象里，下面的“参数”一栏省略外层的大括号。返回示例省略了部分字段。

### 5.1 status

参数：无。

```json
{
  "app": "LSTtranslate", "protocol": 1, "version": "0.2.0-beta.2", "flavor": "dev",
  "installed": false, "pid": 81852, "qt_platform": "windows",
  "automation": {"enabled": true, "explicit": false, "pipe": "\\\\.\\pipe\\lsttranslate-automation-3bafc7af28dd"},
  "workers": {
    "nmt": {"state": "ready", "detail": "", "versions": {"ctranslate2": "4.8.3", "device": "cpu", "compute_type": "int8"}},
    "ocr": {"state": "ready", "detail": "", "versions": {"engine": "windows", "winrt": "3.2.1"}}
  },
  "ocr": {"active": "windows", "running": "windows",
          "engines": [{"kind": "windows", "state": "ready", "language_count": 2, "languages": ["en", "zh-Hans"]}]},
  "engines": [{"id": "argos", "name": "Argos", "health": "ready", "priority": 1}],
  "storage": {"data_dir": "…", "data_source": "argument", "models_dir": "…", "pending_data_dir": null},
  "data_dir": "…", "models_dir": "…",
  "screens": [{"name": "XG27ACS", "native": [0, 0, 2560, 1440], "dpr": 1.5}],
  "methods": ["bindings.get", "…"]
}
```

`engines` 只列启用的翻译服务。截屏之前确认 `workers.ocr.state` 是 `ready`，否则识别要等 OCR 进程加载完。

### 5.2 capture 与蒙版

`capture`：参数 `rect`（必填）、`mode`（`direct` 或 `mask`，缺省 `direct`）、`hold`（只用于 `mask`，缺省 false）。

- `direct`：不显示蒙版，冻结全部屏幕后按矩形截取，矩形可以跨屏，屏幕之间的空隙留黑。覆盖窗口按矩形面积最大的那块屏幕摆放；
- `mask`：打开真实的截屏蒙版，按“框选区域”当前的键位从矩形左上角拖到右下角。选区按真实框选的规则限制在左上角所在的屏幕内；
- `hold: true`：只按下并拖到终点、不松开，蒙版带着选区留在屏幕上，之后用 `capture.release` 松开或 `capture.cancel` 取消。

```json
{"session_id": "51865cd81a55420ab91704b5fcc2e10f", "mode": "direct", "rect": [0, 0, 900, 320]}
{"session_id": null, "mode": "mask", "held": true, "binding": "LeftDrag", "selection": [100, 100, 700, 260], "masks": […]}
```

`mask` 模式下选区太小（宽或高不到 5 个逻辑像素）会按真实框选的规则取消，这时返回 `"session_id": null, "cancelled": true`。

- `capture.release`：松开按着的框选，返回 `{"session_id": "…", "mode": "mask", "binding": "LeftDrag"}`；
- `capture.cancel`：关闭蒙版，返回 `{"cancelled": true}`；没有打开的蒙版时为 false；
- `capture.state`：`{"active": true, "held": true, "last_session": "…", "masks": [{"screen", "native", "dpr", "selection", "visible"}]}`；
- `capture.key`：参数 `binding`（键位文字，例如 `"Esc"`）或 `action`（操作 id，例如 `"capture.cancel"`，取它当前的第一个键盘键位），二选一。返回 `{"sent": "Esc", "active": false}`；
- `capture.mouse`：参数 `binding` 或 `action`（取第一个鼠标键位）、`at`（原生坐标）、`to`（拖动的终点）、`steps`（滚轮格数）。返回 `{"sent": "RightClick", "active": false, "session_id": null}`。

### 5.3 wait_result

参数：`session_id`（缺省为最近一次截屏）、`timeout`（秒，缺省 60）。等到识别完成、每段的显示都定下来，或者识别失败。超时回复 `timeout` 错误，`data` 里是那一刻的结果。

```json
{
  "session_id": "51865cd8…", "phase": "done", "settled": true, "ocr_ms": 130.8, "settled_ms": 1052.9,
  "capture": {"width": 900, "height": 320, "screen": "XG27ACS", "selection": [0, 0, 600, 213], "dpr": 1.5},
  "progress": [4, 4], "error": null,
  "blocks": [{"id": "2", "category": "text", "bbox": [265, 169, 343, 203], "order": 2, "score": 1.0, "text": "Codex", "lines": 1}],
  "segments": [
    {"index": 2, "id": "p3", "block_id": "2", "bbox": [265, 169, 343, 203], "cell": null,
     "source": "Codex", "display": "代码", "shown_engine": "argos", "finished": true, "skip_reason": null,
     "attempts": [{"engine": "argos", "state": "done", "text": "代码", "error": null, "error_code": null,
                   "latency_ms": 849.4, "cached": false, "started": true}]}
  ],
  "overlay": {"open": true, "phase": "result", "progress": [4, 4], "showing": "translation", "pinned": false,
              "zoom": 1.0, "geometry": [0, 0, 600, 213], "items": [{"index": 2, "id": "p3", "shown": "代码",
              "font_px": 20, "rect": [176.67, 112.67, 52.0, 22.67]}],
              "selection": {"ranges": {}, "text": "", "unit": "char"}}
}
```

- `phase`：`recognizing`、`translating`、`done`、`failed`。识别失败时 `error` 是 `{"code", "message"}`；
- `bbox` 是截图里的物理像素 `[x0, y0, x1, y1]`；
- `attempts[*].state`：`queued`、`running`、`done`、`failed`、`cancelled`；`skip_reason`：`no_text`、`same_language`、`no_engine`；
- `overlay` 是覆盖窗口在返回时的状态，覆盖窗口已经关闭或换成别的会话时 `open` 为 false。

结果只记在内存里，最近 8 次截屏，关掉接口时清空。

### 5.4 覆盖窗口

覆盖窗口在失去焦点时会关闭。从另一个程序操作它之前，先调用 `overlay.pin` 把它固定住。

| 方法 | 参数 | 返回 |
|---|---|---|
| `overlay.state` | 无 | 覆盖窗口的状态，没有窗口时 `{"open": false}` |
| `overlay.select` | `start`、`end`：`[段, 字符位置]`，段是 `items` 里的序号或段号 | 选择之后的状态 |
| `overlay.select_at` | `from`、`to`：窗口内坐标 | 按“选择文字”当前的键位拖动之后的状态，多一个 `binding` |
| `overlay.copy` | `clipboard`：缺省 false | `{"text": "…", "clipboard": false}` |
| `overlay.toggle_source`、`overlay.zoom_in`、`overlay.zoom_out`、`overlay.zoom_reset`、`overlay.pin`、`overlay.close` | 无 | 操作之后的状态 |
| `overlay.key` | `binding` 或 `action`（取第一个键盘键位） | 操作之后的状态，多一个 `sent` |
| `overlay.mouse` | `binding` 或 `action`（取第一个鼠标键位）、`at`（缺省为窗口中心）、`to`、`steps` | 同上 |

`overlay.copy` 缺省只返回选中的文字，不写系统剪贴板；`clipboard: true` 时另外按复制键的路径写剪贴板，会覆盖用户剪贴板里的内容。选中范围按阅读顺序跨段，段与段之间用换行连接。

键位文字的写法：键盘写 `Ctrl+C`、`Esc`；鼠标写 `RightClick`、`Ctrl+LeftDrag`、`MiddleDoubleClick`；滚轮写 `Wheel`、`Ctrl+Wheel`。拖动键位要给 `to`。

### 5.5 grab

参数：`target`（`overlay`、`mask` 或 `settings`，缺省 `overlay`）、`name`（可选，只能用字母、数字、下划线与连字符，最长 64 个字符）。文件写在数据目录的 `tmp/automation` 下，文件名由程序拼出。

```json
{"files": [{"target": "overlay", "path": "…\\tmp\\automation\\overlay-20261022-222850-598-smoke.png", "size": [900, 320]}],
 "directory": "…\\tmp\\automation"}
```

`mask` 每块屏幕一个文件。设置窗口的 Mica 背景由系统绘制，截下的图垫的是主题的窗口底色。

### 5.6 设置

- `settings.pages`：`{"tree": [{"id": "general", "name": "常规", "children": []}, …], "pages": ["general", "hotkeys.global", …], "current": "general", "visible": false}`；
- `settings.open`：参数 `page`（可选，例如 `about`、`hotkeys.overlay`）。返回 `{"visible": true, "page": "about"}`；
- `settings.get`：参数 `section`（可选）。不给时返回 `{"revision": 3, "sections": [...]}`，给了返回 `{"revision": 3, "section": "ui", "values": {"language": "auto", "theme": "dark"}}`；
- `settings.set`：参数 `section`、`values`（这一节要改的字段，数组节写整个条目数组）、`timeout`（缺省 10）。

```json
{"changed": true, "revision": 4, "rejected": [], "section": "ui", "values": {"language": "auto", "theme": "dark"}}
```

全部字段都被拒绝时回复 `rejected` 错误，`data.rejected` 是 `[{"location": ["ocr", "mode"], "reason": "Input should be 'structure' or 'text'"}]`；部分字段被拒绝时修改照常生效，被拒绝的列在结果的 `rejected` 里。值与当前相同时返回 `"changed": false`。

每次修改按唯一请求号等待配置服务回执，返回的是该次修改完成时的快照。并发客户端的其他修改不会提前完成这次调用；等待回执超时回复 `timeout`。

API key 不在配置里，接口读不到也写不了。

### 5.7 键位

- `bindings.get`：参数 `scope`（可选：`capture`、`overlay`、`bilingual`）。返回每个操作的 `id`、`scope`、`kind`（`trigger`、`drag`、`wheel`）、`field`、`name`、`defaults`、`bindings`；
- `bindings.set`：参数 `action`（例如 `overlay.toggle_source`）、`bindings`（键位文字的数组，空数组表示不绑定）。返回与 `settings.set` 相同，冲突与写法错误回复 `rejected`；
- `bindings.reset`：参数 `scope`（缺省 `all`），把这个窗口的全部操作写回默认键位。

### 5.8 存储位置与重启

- `storage.status`：`{"data_dir", "data_source", "models_dir", "models_default", "pending_data_dir", "task", "failure"}`；
- `storage.change_data_dir`：参数 `path`（绝对路径）、`copy`（缺省 true，把现有数据复制过去）、`confirm`（必须为 true）、`timeout`（缺省 600）。新目录在重启之后生效；
- `storage.change_models_dir`：参数 `path`（绝对路径，空串表示恢复默认位置）、`move`（缺省 true）、`confirm`、`timeout`（缺省 1800）。不需要重启；
- `storage.restart`：参数 `confirm`（必须为 true）。先回复，再退出并以同样的命令行启动新进程。

```json
{"plan": "change", "progress": [[0, 0]],
 "storage": {"data_dir": "…", "models_dir": "C:\\…\\auto-run-models", "models_default": false, "pending_data_dir": null,
             "task": null, "failure": null}}
```

`plan` 是设置页同一套判断的结果：`change` 更改；`revert` 选回当前目录，撤销还没生效的更改；`same` 什么也不用做。现在不能更改时回复 `rejected`，原因是 `fixed`（数据目录由 `--data-dir` 或环境变量 `LST_DATA_DIR` 指定）或 `busy`（另一次复制或移动还没结束）。新目录写不进去、空间不够、已有同名数据时同样回复 `rejected`，`data.storage.failure` 给出原因。

命令行调用 `storage.restart` 时客户端会等新进程的接口可用，打印新进程的 `status`。

### 5.9 logs.tail 与 quit

- `logs.tail`：参数 `n`（缺省 100，最多 5000）。返回 `{"path": "…\\logs\\main.log", "lines": [...]}`。接口本身的日志形如 `自动化接口：capture → ok，12 毫秒`；
- `quit`：退出程序，返回 `{"quitting": true, "pid": 81852}`。

## 6 错误码

| 错误码 | 含义 |
|---|---|
| `parse_error`、`invalid_request` | 请求不是合法的 JSON 请求行 |
| `unknown_method` | 没有这个方法，`data.methods` 列出全部方法 |
| `invalid_params` | 参数缺失或类型、范围不对 |
| `unavailable` | 要操作的对象现在不存在，例如没有打开的覆盖窗口、没有按着的框选 |
| `timeout` | 等待超时，`data` 是那一刻的状态 |
| `rejected` | 配置或存储位置的修改被拒绝，`data` 是原因 |
| `not_confirmed` | 会移动文件或重启的操作没有写 `confirm: true` |
| `internal` | 程序内部出错，详情在日志里 |

## 7 完整示例

下面的脚本依次做：截屏、等结果、选字复制、改键位并验证新键位生效、迁移数据目录、重启并重新连上。数据目录只能在它不是由 `--data-dir` 或 `LST_DATA_DIR` 指定时更改，源码版这时用仓库下的 `data/`，脚本改动的是这份数据的位置，跑完后在设置里改回去即可。

```python
"""截屏 → 等结果 → 选字复制 → 改键位 → 验证新键位 → 迁移数据目录 → 重启。"""

import sys

from lst.automation import AutomationClient, restart_and_reconnect

NEW_DATA_DIR = sys.argv[1]  # 绝对路径，例如 D:/LST data

client = AutomationClient.connect(timeout=10)
try:
    status = client.call("status")
    assert status["workers"]["ocr"]["state"] in ("ready", "busy"), status["workers"]
    x, y, width, height = status["screens"][0]["native"]

    # 1. 截取主屏左上角的一块，走真实蒙版的框选
    started = client.call("capture", {"rect": [x + 40, y + 40, 900, 320], "mode": "mask"})
    result = client.call("wait_result", {"session_id": started["session_id"], "timeout": 120})
    assert result["phase"] == "done", result["error"]
    for segment in result["segments"]:
        print(segment["source"], "=>", segment["display"], segment["shown_engine"])

    # 2. 选中第一段的前 4 个字并取出
    client.call("overlay.pin")
    client.call("overlay.select", {"start": [0, 0], "end": [0, 4]})
    print("复制：", client.call("overlay.copy")["text"])

    # 3. “切换原文与译文”改成 Ctrl+T，验证旧键位失效、新键位生效
    client.call("bindings.set", {"action": "overlay.toggle_source", "bindings": ["Ctrl+T"]})
    assert client.call("overlay.mouse", {"binding": "RightClick"})["showing"] == "translation"
    assert client.call("overlay.key", {"action": "overlay.toggle_source"})["showing"] == "source"
    client.call("bindings.reset", {"scope": "overlay"})
    client.call("overlay.close")

    # 4. 把数据目录迁到新位置（复制现有数据），重启后生效
    moved = client.call("storage.change_data_dir", {"path": NEW_DATA_DIR, "copy": True, "confirm": True})
    print("复制进度：", moved["progress"], "重启后使用：", moved["storage"]["pending_data_dir"])

    # 5. 立即重启，等新进程的接口可用
    client = restart_and_reconnect(client, timeout=120)
    print("新进程：", client.call("status")["pid"], client.call("status")["data_dir"])
finally:
    client.close()
```

同样的流程用命令行写：

```bash
A="uv run python -m lst.automation"
$A status
$A capture '{"rect": [40, 40, 900, 320], "mode": "mask"}'
$A wait_result '{"timeout": 120}'
$A overlay.pin
$A overlay.select '{"start": [0, 0], "end": [0, 4]}'
$A overlay.copy
$A bindings.set '{"action": "overlay.toggle_source", "bindings": ["Ctrl+T"]}'
$A overlay.key '{"action": "overlay.toggle_source"}'
$A storage.change_data_dir '{"path": "D:/LST data", "confirm": true}'
$A storage.restart
```

## 8 风险

开启接口后，同一台电脑上以你的账户运行的任何程序都可以截屏、读取识别与翻译结果、修改设置、移动数据目录和重启程序。接口只在本机可用，不接受其他用户与远程连接，不提供执行任意代码或读写任意文件的方法，日志不记截屏里的文字。它适合在开发、测试时让脚本或 agent 驱动程序；日常使用时用普通版，或者在“关于与诊断”页把开关关掉。
