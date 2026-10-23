"""竞速模块拥有的三节配置：race、screenshot、bilingual（架构文档第 9 章）。

组合根把 CONFIG_SECTIONS 交给配置服务，配置服务用这些模型校验配置文件并发布 AppConfig；
竞速模块再用同样的模型解析 screenshot、bilingual 两节（lst/contracts/config.py 的“配置如何传递”）。
语言代码在校验时规范化（架构文档 2.6 节），所以 AppConfig 里的语言代码已经是规范写法。

旧配置的 [race] 可能含 mode（结束方式）与 show_source_marker（来源标记），
[bilingual] 可能含 auto_billing（计费引擎自动参赛）。这些字段均已删除。
读取时丢掉它们（见 REMOVED_FIELDS）；否则 extra="forbid" 会把它们报成“未知字段”。
"""

from collections.abc import Mapping
from typing import Annotated, Final, Literal, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from lst.contracts.ids import Lang
from lst.services.race.langid import normalize_lang

LangCode = Annotated[Lang, AfterValidator(normalize_lang)]
"""校验时规范化的语言代码，非法代码校验失败。"""


REMOVED_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    "race": frozenset({"mode", "show_source_marker"}),
    "bilingual": frozenset({"auto_billing"}),
}
"""各模型里已经删除、读取时丢掉的字段，键是模型的配置名。"""


def _drop(data: object, removed: frozenset[str]) -> object:
    if not isinstance(data, dict):
        return data
    raw = cast(dict[str, object], data)
    if removed.isdisjoint(raw):
        return raw
    return {key: value for key, value in raw.items() if key not in removed}


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RaceConfig(_Section):
    """兼容旧 [race] 节；已删除的字段读取后丢弃，新配置不含功能字段。"""

    @model_validator(mode="before")
    @classmethod
    def _drop_removed(cls, data: object) -> object:
        return _drop(data, REMOVED_FIELDS["race"])


class ScreenshotConfig(_Section):
    """[screenshot] 节：截屏翻译的目标语言（需求 RACE-10）。"""

    target_lang: LangCode = Lang("zh-Hans")


class BilingualConfig(_Section):
    """[bilingual] 节：双语窗口的互译对、输入停顿时长与布局。"""

    pair: tuple[LangCode, LangCode] = (Lang("zh-Hans"), Lang("en"))
    """互译对（需求 RACE-10）：检测到第一个语言就译成第二个，反之亦然。两个语言规范化后必须不同。"""

    debounce_ms: int = Field(default=400, ge=100, le=5000)
    """输入停顿多久后自动翻译（需求 BI-04），单位毫秒。"""

    layout: Literal["vertical", "horizontal"] = "vertical"
    """双语窗口原文区与译文区的排列方向。竞速模块不使用它，只负责校验；ui.bilingual 按键读取。"""

    @model_validator(mode="before")
    @classmethod
    def _drop_removed(cls, data: object) -> object:
        return _drop(data, REMOVED_FIELDS["bilingual"])

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
