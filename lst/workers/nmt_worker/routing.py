"""翻译路线规划与逐段翻译流程（架构文档 2.5、8.3 节）。

路线是一串直连模型。有直连模型时走一跳；没有直连模型、但有“源语言→中转语言”和“中转语言→目标语言”
两个模型时走两跳，第一跳的译文直接送进第二跳，不发回主进程。

这里的流程与推理库无关：真实后端和测试用的假后端都把“用某个直连模型翻译一批句子”的函数交给
translate_segments，切分、中转、拼回和段级错误隔离都在这里完成。
"""

import logging
from collections.abc import Callable, Collection, Iterator, Sequence

from lst.contracts.errors import ErrorCode
from lst.contracts.worker_protocol import TranslateSegment
from lst.workers.nmt_worker.backend import Pair, SegmentOutcome
from lst.workers.nmt_worker.languages import canonical_code
from lst.workers.nmt_worker.sentences import join_sentences, split_sentences

log = logging.getLogger(__name__)

type Route = tuple[Pair, ...]
type SentenceTranslator = Callable[[Pair, list[str]], list[str]]


class ModelUnavailable(Exception):
    """路线上的某个模型无法使用（模型包损坏、解压失败、加载失败）。对应错误码 engine.model_missing。"""


def plan_route(source: str, target: str, pairs: Collection[Pair], pivot: str | None) -> Route | None:
    """规划从 source 到 target 的路线；无法翻译时返回 None。

    - source 与 target 相同：空路线，原文即译文；
    - 有直连模型：一跳；
    - 否则 pivot 不为 None、且两端都不是 pivot 时，查 source→pivot 与 pivot→target，都有则两跳。
    """
    if source == target:
        return ()
    if (source, target) in pairs:
        return ((source, target),)
    if pivot is None or pivot in (source, target):
        return None
    first, second = (source, pivot), (pivot, target)
    if first in pairs and second in pairs:
        return (first, second)
    return None


def translate_segments(
    source: str,
    target: str,
    segments: Sequence[TranslateSegment],
    *,
    pairs: Collection[Pair],
    pivot: str | None,
    translate: SentenceTranslator,
) -> Iterator[SegmentOutcome]:
    """逐段翻译：切句、按路线逐跳翻译、拼回。每段产出一个结果，单段失败不影响其他段。

    translate(pair, sentences) 用 pair 对应的直连模型翻译一批句子，返回同样条数的译文。
    它抛 ModelUnavailable 时该段记为 engine.model_missing，抛其他异常时记为 engine.internal。
    """
    src, tgt = canonical_code(source), canonical_code(target)
    route = plan_route(src, tgt, pairs, pivot)
    if route is None:
        reason = f"没有 {src}→{tgt} 的模型" + (f"，也无法经 {pivot} 中转" if pivot is not None else "")
        for segment in segments:
            yield SegmentOutcome.failed(segment.id, ErrorCode.MODEL_MISSING, reason)
        return
    for segment in segments:
        layout = split_sentences(segment.text)
        if not layout.sentences or not route:
            # 空白段或同语言：没有需要翻译的内容。
            yield SegmentOutcome.ok(segment.id, segment.text.strip())
            continue
        try:
            sentences = list(layout.sentences)
            for hop in route:
                sentences = translate(hop, sentences)
                if len(sentences) != len(layout.sentences):
                    raise RuntimeError(
                        f"{hop[0]}→{hop[1]} 返回 {len(sentences)} 句，应为 {len(layout.sentences)} 句"
                    )
            text = join_sentences(layout, sentences, tgt)
        except ModelUnavailable as exc:
            log.warning("段 %s 无可用模型：%s", segment.id, exc)
            yield SegmentOutcome.failed(segment.id, ErrorCode.MODEL_MISSING, str(exc))
        except Exception as exc:
            # 日志只记异常，不记原文和译文（架构文档 10.2 节）。
            log.exception("段 %s 推理失败", segment.id)
            yield SegmentOutcome.failed(segment.id, ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}")
        else:
            yield SegmentOutcome.ok(segment.id, text)
