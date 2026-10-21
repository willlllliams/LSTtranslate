"""竞速模块拥有的三节配置：race、screenshot、bilingual（架构文档第 9 章）。

组合根把 CONFIG_SECTIONS 交给配置服务，配置服务用这些模型校验配置文件并发布 AppConfig；
竞速模块再用同样的模型解析自己的三节（lst/contracts/config.py 的“配置如何传递”）。
语言代码在校验时规范化（架构文档 2.6 节），所以 AppConfig 里的语言代码已经是规范写法。
"""

from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from lst.contracts.ids import Lang
from lst.services.race.langid import normalize_lang
from lst.services.race.scheduler import RaceMode

LangCode = Annotated[Lang, AfterValidator(normalize_lang)]
"""校验时规范化的语言代码，非法代码校验失败。"""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceMarkerConfig(_Section):
    """是否在段旁显示当前胜出实例的标识颜色（需求 RACE-06）。"""

    overlay: bool = False
    bilingual: bool = True


class RaceConfig(_Section):
    """[race] 节：竞速的结束方式与来源标记。"""

    mode: RaceMode = RaceMode.SAVER
    """saver（节省模式）或 full（完整模式），见需求 RACE-05。"""

    show_source_marker: SourceMarkerConfig = SourceMarkerConfig()


class ScreenshotConfig(_Section):
    """[screenshot] 节：截屏翻译的目标语言（需求 RACE-10）。"""

    target_lang: LangCode = Lang("zh-Hans")


class BilingualConfig(_Section):
    """[bilingual] 节：双语窗口的互译对、输入停顿时长与计费实例的触发方式。"""

    pair: tuple[LangCode, LangCode] = (Lang("zh-Hans"), Lang("en"))
    """互译对（需求 RACE-10）：检测到第一个语言就译成第二个，反之亦然。两个语言规范化后必须不同。"""

    debounce_ms: int = Field(default=400, ge=100, le=5000)
    """输入停顿多久后自动翻译（需求 BI-04），单位毫秒。"""

    auto_billing: bool = False
    """计费实例是否也在输入停顿时自动参赛；默认只在按 Ctrl+Enter 时加入（需求 BI-04）。"""

    layout: Literal["vertical", "horizontal"] = "vertical"
    """双语窗口原文区与译文区的排列方向。竞速模块不使用它，只负责校验；ui.bilingual 按键读取。"""

    # 用字段校验器而不是模型校验器：错误位置落在 pair 上，配置服务只回退这一个字段（CFG-02）。
    @field_validator("pair")
    @classmethod
    def _check_pair(cls, pair: tuple[Lang, Lang]) -> tuple[Lang, Lang]:
        if pair[0] == pair[1]:
            raise ValueError(f"互译对的两个语言必须不同，现在都是 {pair[0]}")
        return pair


CONFIG_SECTIONS: Final[dict[str, type[BaseModel]]] = {
    "race": RaceConfig,
    "screenshot": ScreenshotConfig,
    "bilingual": BilingualConfig,
}
"""节名 → 该节的配置模型，由组合根注册给配置服务。"""
