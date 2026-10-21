r"""安装版的主程序启动器（架构文档第 15 章）。

快捷方式运行 `runtime\pythonw.exe -I -X utf8 launcher\lst_main.pyw`。-I 隔离模式忽略用户机器上的
PYTHONPATH 和用户级 site-packages，模块搜索路径由这里显式设定：安装目录的 app\ 与 site\main\。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "site" / "main")]

from lst.__main__ import main  # noqa: E402

sys.exit(main())
