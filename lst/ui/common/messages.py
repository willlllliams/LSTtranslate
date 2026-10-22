"""界面模块之间的消息（只在界面层内部使用，不属于业务契约）。

界面模块互不依赖（架构文档 4.2 节），设置窗口与热键模块之间的协作只能经过事件总线。这类消息只有界面模块
发出和接收，所以定义在界面共用模块里，不放进 lst.contracts。
"""

from dataclasses import dataclass

from lst.contracts.messages import Event


@dataclass(frozen=True, slots=True)
class HotkeyRecordingChanged(Event):
    """设置窗口的热键录入开始（active=True）或结束（active=False）。

    RegisterHotKey 注册的全局热键先于任何窗口收到按键，录入一个已经注册的组合时，按键会被热键模块截走，
    录入框收不到。热键模块收到 active=True 时注销全部热键，收到 active=False 时按当前配置重新注册。
    是事件而不是命令：没有热键模块（例如测试只启动设置窗口）时没有人处理也不算错。
    """

    active: bool
