"""各类引擎共用的配置基类、EngineInfo 构造，以及机器翻译引擎共用的结果处理。

每类引擎在自己的子包里定义一个继承 EngineSettings 的 pydantic 模型，描述配置文件中一条
`[[engines]]` 的字段；配置服务用这些模型校验配置（架构文档 4.4 节的 EngineSettingsSchemas 端口）。

DeepL、Google、百度三个机器翻译引擎的处理方式相同：空白段不发给接口、原样返回（split_blank），
响应按 pydantic 模型校验（parse_json、check_count），请求失败时整批记为同一个错误（failures）。
"""

import hashlib
import json
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import EngineId
from lst.contracts.translation import EngineInfo, EngineKind, Segment, SegmentOutcome
from lst.services.engines.http import EngineError


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
    image_max_side: int | None = None,
) -> EngineInfo:
    """由配置构造 EngineInfo。

    - fingerprint 只覆盖影响译文的配置（fingerprint_parts，例如模型名、提示词、温度），参与缓存键；
    - revision 覆盖全部配置加上 API key 的哈希，任何一项变化都会让调度器替换实例（架构文档 6.5 节）。
      API key 只以哈希形式参与计算，不会出现在 EngineInfo 里；
    - image_max_side 原样写入 EngineInfo：不为 None 表示实例在截屏翻译时接收截图（需求 ENG-07）。
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
        image_max_side=image_max_side,
    )


# ---------------------------------------------------------------- 机器翻译引擎的结果处理


def split_blank(
    segments: Sequence[Segment], texts: Sequence[str] | None = None
) -> tuple[list[SegmentOutcome], list[Segment]]:
    """把空白段分出来：它们不发给接口，原文原样作为译文返回。

    texts 是各段实际要发送的文本（例如百度把段内换行换成空格之后的文本），默认是段的原文；
    去掉空白后为空的段算空白段。返回（空白段的结果，其余的段），两者都保持原来的顺序。
    """
    blank: list[SegmentOutcome] = []
    rest: list[Segment] = []
    for segment, text in zip(
        segments, texts if texts is not None else [s.text for s in segments], strict=True
    ):
        if text.strip():
            rest.append(segment)
        else:
            blank.append(SegmentOutcome(segment.id, text=segment.text))
    return blank, rest


def failures(segments: Sequence[Segment], code: ErrorCode, detail: str) -> list[SegmentOutcome]:
    """一批段全部记为同一个错误。"""
    return [SegmentOutcome(segment.id, error=detail, code=code) for segment in segments]


def parse_json[M: BaseModel](content: bytes, model: type[M]) -> M:
    """按 model 校验 JSON 响应体；不符合时抛出 BAD_RESPONSE。错误信息只写字段路径，不含响应正文。"""
    try:
        return model.model_validate_json(content)
    except ValidationError as exc:
        where = ", ".join(".".join(str(part) for part in error["loc"]) or "<root>" for error in exc.errors())
        raise EngineError(ErrorCode.BAD_RESPONSE, f"响应结构不符合预期（{where}）") from None


def check_count(count: int, expected: int) -> None:
    """返回的译文条数必须与请求的段数相同，否则抛出 BAD_RESPONSE。"""
    if count != expected:
        raise EngineError(ErrorCode.BAD_RESPONSE, f"返回 {count} 条译文，请求了 {expected} 条")
