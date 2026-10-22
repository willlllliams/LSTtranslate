"""配置文件的 [automation] 节（需求 DEV-01、DEV-03）。

enabled 的类型是 ``bool | None``，默认 None：生成默认配置文件时 None 不写进文件（配置服务跳过空值），
所以没有动过这个开关的用户，配置文件里没有 enabled，开关跟随构建类型（开发版开启、普通版关闭）。
用户在设置里打开或关闭过之后，文件里写下 true 或 false，以后换装另一种构建类型也以它为准。
"""

from pydantic import BaseModel, ConfigDict, Field

from lst.contracts.automation import BuildFlavor
from lst.contracts.config import AppConfig


class AutomationConfig(BaseModel):
    """自动化接口：供同一台电脑上以当前用户身份运行的程序（例如测试脚本、AI agent）控制本程序。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool | None = Field(
        default=None,
        description="是否开启自动化接口；不写时开发版开启、普通版关闭",
    )


CONFIG_SECTIONS: dict[str, type[BaseModel]] = {"automation": AutomationConfig}


def automation_config(config: AppConfig | None) -> AutomationConfig:
    """取 [automation] 节；节缺失或无法解析时用默认值（配置服务已经校验过，正常运行时不会无法解析）。"""
    raw = config.section("automation") if config is not None else None
    try:
        return AutomationConfig.model_validate(raw if isinstance(raw, dict) else {})
    except ValueError:
        return AutomationConfig()


def effective_enabled(config: AutomationConfig, flavor: BuildFlavor) -> bool:
    """配置里写了 enabled 时以它为准，否则按构建类型的默认值。"""
    return config.enabled if config.enabled is not None else flavor.automation_default


__all__ = ["CONFIG_SECTIONS", "AutomationConfig", "automation_config", "effective_enabled"]
