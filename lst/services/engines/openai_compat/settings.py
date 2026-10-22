"""OpenAI 兼容 LLM 引擎的配置模型（需求 ENG-02，架构文档 2.7.1、2.7.3 节）。"""

from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from lst.contracts.langs import normalize_lang
from lst.services.engines.common import EngineSettings

Provider = Literal["deepseek", "openai", "ollama", "llamacpp", "generic"]
StructuredOutput = Literal["auto", "json_schema", "json_object"]
OutputMode = Literal["json_schema", "json_object"]

# 本地 LLM 服务：引擎类别为 LOCAL_LLM，不计费，允许没有 API key。
LOCAL_PROVIDERS: frozenset[str] = frozenset({"ollama", "llamacpp"})


class OpenAICompatSettings(EngineSettings):
    """一条 OpenAI 兼容 LLM 引擎的 `[[engines]]` 配置。

    - base_url 原样使用，请求地址是去掉末尾斜杠后加上 `/chat/completions`；含 `/v1` 的地址写全即可。
    - first_token_timeout_s 为 None 时按 provider 取默认值：在线 8 秒；本地首次 60 秒（含模型加载），
      拿到过首 token 之后 15 秒。给出数值时所有请求都用这个值。
    - structured_output 为 auto 时，DeepSeek 用 json_object（它不支持 json_schema），其余用 json_schema。
    - languages 是语言白名单，None 表示支持任意语言对。校验时按 contracts.langs.normalize_lang 整理成
      规范写法并去重（zh、zh-CN → zh-Hans，en-US → en），调度器按规范写法比较语言代码；非法代码校验失败。
    - 本地 provider 没有显式配置 max_concurrency 时默认为 1，对应 Ollama 默认的服务端并行数（2.7.3 节）。
    - send_image 为真时，截屏翻译把截图连同识别出的文字一起发给模型（需求 ENG-07）；截图发送前等比缩小，
      长边不超过 image_max_side。DeepSeek 等不接受图片的服务请保持关闭。
    """

    provider: Provider
    base_url: str
    model: str = Field(min_length=1)
    temperature: float = Field(default=0.2, ge=0, le=2)
    system_prompt: str | None = None
    max_batch_segments: int = Field(default=20, ge=1, le=200)
    max_batch_chars: int = Field(default=2000, ge=1, le=100_000)
    first_token_timeout_s: float | None = Field(default=None, gt=0, le=600)
    structured_output: StructuredOutput = "auto"
    languages: tuple[str, ...] | None = None
    send_image: bool = Field(
        default=False,
        description=(
            "截屏翻译时把截图连同识别出的文字一起发给模型，帮助理解上下文、纠正识别错误。"
            "截图会发送给该服务。模型不支持图片输入时，该实例的翻译会失败。"
        ),
    )
    image_max_side: int = Field(
        default=1568,
        ge=256,
        le=8192,
        description="附带截图时，截图发送前等比缩小，长边不超过这个像素数，以节省 token。",
    )

    @model_validator(mode="before")
    @classmethod
    def _default_local_concurrency(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        fields = cast(dict[str, object], data)
        if fields.get("provider") in LOCAL_PROVIDERS and "max_concurrency" not in fields:
            return {**fields, "max_concurrency": 1}
        return fields

    @field_validator("languages")
    @classmethod
    def _normalize_languages(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        return tuple(dict.fromkeys(normalize_lang(code) for code in value))

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        # 地址会出现在 httpx 的日志里，所以不允许带查询参数和用户名密码（架构文档第 10 章）。
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("base_url 必须是 http 或 https 地址")
        if parts.query or parts.fragment:
            raise ValueError("base_url 不能带查询参数或片段")
        if parts.username is not None or parts.password is not None:
            raise ValueError("base_url 不能包含用户名或密码")
        return value

    @property
    def is_local(self) -> bool:
        return self.provider in LOCAL_PROVIDERS

    @property
    def needs_api_key(self) -> bool:
        return not self.is_local

    @property
    def output_mode(self) -> OutputMode:
        """structured_output 的最终取值。"""
        if self.structured_output != "auto":
            return self.structured_output
        return "json_object" if self.provider == "deepseek" else "json_schema"

    @property
    def endpoint(self) -> str:
        """Chat Completions 接口的完整地址。"""
        return self.base_url.rstrip("/") + "/chat/completions"
