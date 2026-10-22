"""构建类型与自动化接口的状态（需求 DEV-01 至 DEV-08，架构文档第 16 章）。

- ``BuildFlavor``：普通版与开发版。两者的代码与功能完全相同，唯一的区别是 ``[automation].enabled`` 没有写进
  配置文件时的默认值：开发版开启，普通版关闭。安装版的构建类型写在 ``app\\installed.json`` 的 flavor 字段；
- ``AutomationStatus``（State）：组合根里的自动化接口模块发布；托盘据此在悬停提示里写“自动化接口已开启”，
  设置窗口据此显示开关与管道名。
"""

from dataclasses import dataclass
from enum import StrEnum

from lst.contracts.messages import State


class BuildFlavor(StrEnum):
    NORMAL = "normal"
    DEV = "dev"

    @property
    def automation_default(self) -> bool:
        """配置里没有写 [automation].enabled 时自动化接口是否开启。"""
        return self is BuildFlavor.DEV


@dataclass(frozen=True, slots=True)
class AutomationStatus(State):
    """自动化接口的当前状态。

    - enabled：按配置与构建类型算出的开关；explicit 为真表示配置文件里写了 enabled，
      为假表示用的是构建类型的默认值；
    - listening：管道已经建好、正在接受连接。enabled 为真而 listening 为假时，error 是原因
      （例如同名管道已被占用）；
    - pipe：管道名，没有开启时也给出，方便界面说明。
    """

    flavor: BuildFlavor
    enabled: bool
    explicit: bool
    listening: bool
    pipe: str = ""
    error: str = ""


__all__ = ["AutomationStatus", "BuildFlavor"]
