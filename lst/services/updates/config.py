"""更新服务拥有的配置节 [updates]。

组合根把 CONFIG_SECTIONS 交给配置服务，配置服务用 UpdatesConfig 校验配置文件并发布 AppConfig；
更新模块再用同一个模型解析 [updates] 节。
"""

from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class UpdatesConfig(BaseModel):
    """[updates] 节。

    include_prerelease 默认开启：目前发布的版本都是预发布版本，关闭后检查不到任何版本。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    auto_check: bool = True
    """启动后与之后每隔 check_interval_hours 小时自动检查一次新版本。"""
    check_interval_hours: int = Field(default=24, ge=1, le=24 * 30)
    """两次自动检查的最短间隔，单位小时。"""
    include_prerelease: bool = True
    """是否把预发布版本（GitHub 标为 prerelease 或版本号带预发布后缀）算作可用版本。"""


CONFIG_SECTIONS: Final[dict[str, type[BaseModel]]] = {"updates": UpdatesConfig}
"""节名 → 该节的配置模型，由组合根注册给配置服务。"""
