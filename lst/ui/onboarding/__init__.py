"""首次运行引导（需求 UI-08、INS-03，架构文档 11.7 节）。

- strings：中英文文案，导入时登记到 ui.i18n；
- presenter：展示器与画面模型，纯逻辑，不依赖 Qt；
- window：引导窗口（视图）；
- store：用 QSettings 记录引导是否完成；
- module：OnboardingModule，实现 kernel 的 Module 协议。

第一版只覆盖本地翻译模型的下载。UI-08 中的 GPU 与 CUDA、Ollama 检测和“配置第一个引擎实例”尚未实现。
"""

from lst.ui.onboarding import strings as strings  # 导入时登记文案
from lst.ui.onboarding.module import OnboardingModule
from lst.ui.onboarding.presenter import (
    DEFAULT_PAIR,
    ModelRow,
    OnboardingPresenter,
    OnboardingScreen,
    RowState,
    build_screen,
    read_pair,
)

__all__ = [
    "DEFAULT_PAIR",
    "ModelRow",
    "OnboardingModule",
    "OnboardingPresenter",
    "OnboardingScreen",
    "RowState",
    "build_screen",
    "read_pair",
]
