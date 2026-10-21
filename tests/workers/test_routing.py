"""语言代码规范化、中转路线规划与逐段翻译流程。"""

import pytest

from lst.contracts.errors import ErrorCode
from lst.contracts.worker_protocol import TranslateSegment
from lst.workers.nmt_worker.backend import Pair, SegmentOutcome
from lst.workers.nmt_worker.languages import canonical_code, from_argos_code, sentence_joiner
from lst.workers.nmt_worker.routing import ModelUnavailable, plan_route, translate_segments

# ---------------------------------------------------------------- 语言代码


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("zh", "zh-Hans"),
        ("zh-CN", "zh-Hans"),
        ("zh_SG", "zh-Hans"),
        ("zh-Hans", "zh-Hans"),
        ("zh-hans-cn", "zh-Hans"),
        ("zh-TW", "zh-Hant"),
        ("zh-HK", "zh-Hant"),
        ("zh-Hant", "zh-Hant"),
        ("zh-Hant-TW", "zh-Hant"),
        ("en", "en"),
        ("en-US", "en"),
        ("PT_br", "pt"),
        (" ja ", "ja"),
    ],
)
def test_canonical_code(code: str, expected: str) -> None:
    assert canonical_code(code) == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [("zh", "zh-Hans"), ("zt", "zh-Hant"), ("ZT", "zh-Hant"), ("en", "en"), ("pt_BR", "pt"), ("nb", "nb")],
)
def test_argos_codes_map_to_canonical(code: str, expected: str) -> None:
    assert from_argos_code(code) == expected


def test_sentence_joiner() -> None:
    assert sentence_joiner("zh-Hans") == ""
    assert sentence_joiner("zh-Hant") == ""
    assert sentence_joiner("ja") == ""
    assert sentence_joiner("en") == " "
    assert sentence_joiner("ko") == " "


# ---------------------------------------------------------------- 路线规划

PAIRS: set[Pair] = {("ja", "en"), ("en", "de"), ("en", "ja"), ("ja", "de"), ("fr", "en")}


def test_direct_model_is_preferred_over_pivot() -> None:
    assert plan_route("ja", "de", PAIRS, "en") == (("ja", "de"),)


def test_pivot_route_uses_two_hops() -> None:
    assert plan_route("fr", "de", PAIRS, "en") == (("fr", "en"), ("en", "de"))
    assert plan_route("fr", "ja", PAIRS, "en") == (("fr", "en"), ("en", "ja"))


def test_no_route_when_a_hop_is_missing() -> None:
    assert plan_route("de", "fr", PAIRS, "en") is None  # 没有 de→en
    assert plan_route("fr", "ko", PAIRS, "en") is None  # 没有 en→ko


def test_no_pivot_when_disabled_or_endpoint_is_pivot() -> None:
    assert plan_route("fr", "de", PAIRS, None) is None
    assert plan_route("en", "fr", PAIRS, "en") is None  # 源语言就是中转语言，只能直连
    assert plan_route("de", "en", PAIRS, "en") is None


def test_same_language_is_an_empty_route() -> None:
    assert plan_route("en", "en", set(), None) == ()


# ---------------------------------------------------------------- 逐段翻译


def fake_translate(pair: Pair, sentences: list[str]) -> list[str]:
    if any("MISSING" in s for s in sentences):
        raise ModelUnavailable("模型包损坏")
    if any("BROKEN" in s for s in sentences):
        raise ValueError("推理失败")
    if any("SHORT" in s for s in sentences):
        return sentences[:-1]
    return [f"<{pair[0]}>{pair[1]}>{s}" for s in sentences]


def run(source: str, target: str, *texts: str, pivot: str | None = "en") -> list[SegmentOutcome]:
    segments = [TranslateSegment(str(i), text) for i, text in enumerate(texts)]
    return list(
        translate_segments(source, target, segments, pairs=PAIRS, pivot=pivot, translate=fake_translate)
    )


def test_segments_are_translated_sentence_by_sentence() -> None:
    assert run("ja", "de", "あ。い。", "う") == [
        SegmentOutcome.ok("0", "<ja>de>あ。 <ja>de>い。"),
        SegmentOutcome.ok("1", "<ja>de>う"),
    ]


def test_pivot_output_feeds_second_hop() -> None:
    assert run("fr", "ja", "Un. Deux.") == [SegmentOutcome.ok("0", "<en>ja><fr>en>Un.<en>ja><fr>en>Deux.")]


def test_request_codes_are_canonicalised() -> None:
    segments = [TranslateSegment("0", "x")]
    outcomes = list(
        translate_segments(
            "en-US", "zh-CN", segments, pairs={("en", "zh-Hans")}, pivot=None, translate=fake_translate
        )
    )
    assert outcomes == [SegmentOutcome.ok("0", "<en>zh-Hans>x")]


def test_missing_route_fails_every_segment_with_model_missing() -> None:
    outcomes = run("de", "fr", "a", "b")
    assert [o.code for o in outcomes] == [ErrorCode.MODEL_MISSING, ErrorCode.MODEL_MISSING]
    assert all(o.text is None and o.error for o in outcomes)


def test_segment_level_errors_are_isolated() -> None:
    outcomes = run("ja", "de", "fine", "MISSING model", "BROKEN input", "SHORT. result", "fine again")
    assert [o.code for o in outcomes] == [
        None,
        ErrorCode.MODEL_MISSING,
        ErrorCode.INTERNAL,
        ErrorCode.INTERNAL,
        None,
    ]
    assert outcomes[0].text == "<ja>de>fine" and outcomes[4].text == "<ja>de>fine again"
    assert outcomes[2].error is not None and "ValueError" in outcomes[2].error


def test_blank_segment_and_same_language_return_text_unchanged() -> None:
    assert run("ja", "de", "   ") == [SegmentOutcome.ok("0", "")]
    assert run("ja", "ja", " 原文。 ") == [SegmentOutcome.ok("0", "原文。")]
