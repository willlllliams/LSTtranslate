"""各类引擎共用的配置基类与 EngineInfo 构造。

每类引擎在自己的子包里定义一个继承 EngineSettings 的 pydantic 模型，描述配置文件中一条
`[[engines]]` 的字段；配置服务用这些模型校验配置（架构文档 4.4 节的 EngineSettingsSchemas 端口）。
"""

import hashlib
import json
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from lst.contracts.ids import EngineId
from lst.contracts.translation import EngineInfo, EngineKind


class EngineSettings(BaseModel):
    """一条 `[[engines]]` 配置的公共字段。

    timeout_s 必须是有限正数：竞速的活性依赖每个任务都在有限时间内结束（架构文档 6.5 节）。
    max_retries 是网络错误、限流与服务端错误的最大重试次数（需求 ENG-05，默认 1，架构文档 2.7.3 节）。
    secret 是 keyring 中的条目名，配置文件里不出现 API key 本身。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    enabled: bool = True
    priority: int = Field(ge=1)
    color: str = ""
    timeout_s: float = Field(gt=0, le=600)
    max_concurrency: int = Field(default=2, ge=1, le=32)
    max_retries: int = Field(default=1, ge=0, le=3)
    secret: str | None = None


def stable_hash(*parts: object) -> str:
    """对任意可 JSON 序列化的值求稳定的短哈希（SHA-256 的前 16 个十六进制字符）。"""
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_info(
    settings: EngineSettings,
    *,
    kind: EngineKind,
    billing: bool,
    max_batch_segments: int,
    max_batch_chars: int,
    fingerprint_parts: Sequence[object],
    secret_value: str | None,
) -> EngineInfo:
    """由配置构造 EngineInfo。

    - fingerprint 只覆盖影响译文的配置（fingerprint_parts，例如模型名、提示词、温度），参与缓存键；
    - revision 覆盖全部配置加上 API key 的哈希，任何一项变化都会让调度器替换实例（架构文档 6.5 节）。
      API key 只以哈希形式参与计算，不会出现在 EngineInfo 里。
    """
    fingerprint = stable_hash(kind.value, *fingerprint_parts)
    secret_digest = "" if secret_value is None else hashlib.sha256(secret_value.encode("utf-8")).hexdigest()
    revision = int(stable_hash(settings.model_dump(mode="json"), secret_digest)[:8], 16)
    return EngineInfo(
        id=EngineId(settings.id),
        name=settings.name,
        kind=kind,
        priority=settings.priority,
        billing=billing,
        max_concurrency=settings.max_concurrency,
        max_batch_segments=max_batch_segments,
        max_batch_chars=max_batch_chars,
        timeout_s=settings.timeout_s,
        color=settings.color,
        fingerprint=fingerprint,
        revision=revision,
    )
