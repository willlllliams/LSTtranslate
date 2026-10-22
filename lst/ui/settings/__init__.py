"""设置窗口（需求 UI-05、UI-06、UI-07、CFG-01 至 CFG-05，架构文档 11.7 节）。

- strings：中英文文案，导入时登记到 ui.i18n；
- schema：从注入的 pydantic 模型读出设置项的类型、取值范围和默认值，不依赖 Qt；
- entries：引擎实例的读取、排序、新建与校验，不依赖 Qt；
- presenter：展示器，订阅状态、发送 UpdateConfig 等命令，不依赖 Qt；
- controls、cards：各种取值的编辑器、设置卡片，以及把它们接到展示器上的生成器；
- pages、page_capture、page_engines、page_models、page_about：七个页面；
  ocr_engine_rows、model_rows 把 OCR 引擎目录、模型目录整理成显示内容，不依赖 Qt；
- window：左侧导航加右侧页面的窗口，Mica 背景；
- module：SettingsModule，实现 kernel 的 Module 协议。

界面模块不能 import services 与 app：各节配置的 pydantic 模型和引擎类别的模型由组合根注入
``SettingsModule``。
"""

from lst.ui.settings.module import SettingsModule
from lst.ui.settings.presenter import KeyState, SettingsPresenter, Topic
from lst.ui.settings.window import SettingsWindow

__all__ = ["KeyState", "SettingsModule", "SettingsPresenter", "SettingsWindow", "Topic"]
