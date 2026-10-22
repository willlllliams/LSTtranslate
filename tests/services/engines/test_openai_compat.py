"""OpenAI 兼容 LLM 引擎的测试。所有请求都由 httpx.MockTransport 应答，不访问网络。

时机相关的用例用 ContentStream 的闸门控制每个片段何时到达；只有超时用例用到很短的真实时间。
"""

import asyncio
import base64
import json
import logging
import struct
from collections.abc import AsyncGenerator, AsyncIterator
from itertools import pairwise
from typing import Any, cast

import httpx
import jiter
import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from pydantic import ValidationError

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang
from lst.contracts.ocr import RawImage
from lst.contracts.translation import EngineInfo, EngineKind, JobImage
from lst.services.engines.http import EngineError
from lst.services.engines.openai_compat import OpenAICompatSettings, create_openai_compat_engine
from lst.services.engines.openai_compat import engine as engine_module
from lst.services.engines.openai_compat import parser as parser_module
from lst.services.engines.openai_compat.engine import OpenAICompatEngine
from lst.services.engines.openai_compat.parser import ReplyParser, parse_chunk, stream_error
from lst.services.engines.openai_compat.prompt import (
    CONTEXT_SEPARATOR,
    DEFAULT_SYSTEM_PROMPT,
    IMAGE_NOTE,
    build_messages,
    language_name,
    with_image,
)
from lst.services.race.screenshot import encode_screenshot
from tests.services.engines.openai_compat_fixtures import (
    EN,
    JA,
    KEY,
    ZH,
    ContentStream,
    FakeServer,
    Secrets,
    Sleeps,
    codes_of,
    collect,
    content_event,
    make_engine,
    make_job,
    make_settings,
    never_respond,
    next_task,
    reply_json,
    reply_response,
    sse_response,
    stream_response,
    texts_of,
    wait_until_blocked,
)


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
async def client(server: FakeServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        yield http


# ---------------------------------------------------------------- 配置


class TestSettings:
    def test_defaults(self) -> None:
        settings = make_settings("openai")
        assert settings.temperature == 0.2
        assert (settings.max_batch_segments, settings.max_batch_chars) == (20, 2000)
        assert settings.structured_output == "auto"
        assert settings.first_token_timeout_s is None
        assert settings.languages is None
        assert settings.system_prompt is None

    @pytest.mark.parametrize(
        ("provider", "structured", "mode"),
        [
            ("deepseek", "auto", "json_object"),
            ("openai", "auto", "json_schema"),
            ("generic", "auto", "json_schema"),
            ("ollama", "auto", "json_schema"),
            ("llamacpp", "auto", "json_schema"),
            ("openai", "json_object", "json_object"),
            ("deepseek", "json_schema", "json_schema"),
        ],
    )
    def test_output_mode(self, provider: str, structured: str, mode: str) -> None:
        assert make_settings(provider, structured_output=structured).output_mode == mode

    @pytest.mark.parametrize(
        ("base_url", "endpoint"),
        [
            ("https://api.deepseek.test", "https://api.deepseek.test/chat/completions"),
            ("https://api.openai.test/v1", "https://api.openai.test/v1/chat/completions"),
            ("http://localhost:11434/v1/", "http://localhost:11434/v1/chat/completions"),
        ],
    )
    def test_endpoint(self, base_url: str, endpoint: str) -> None:
        assert make_settings("openai", base_url=base_url).endpoint == endpoint

    @pytest.mark.parametrize(
        "base_url",
        [
            "api.openai.test/v1",
            "ftp://api.openai.test/v1",
            "https://api.openai.test/v1?key=abc",
            "https://api.openai.test/v1#x",
            "https://user:pass@api.openai.test/v1",
        ],
    )
    def test_rejects_bad_base_url(self, base_url: str) -> None:
        with pytest.raises(ValidationError):
            make_settings("openai", base_url=base_url)

    def test_requires_provider_and_base_url(self) -> None:
        with pytest.raises(ValidationError):
            OpenAICompatSettings.model_validate(
                {"id": "x", "name": "x", "priority": 1, "timeout_s": 60, "model": "m"}
            )

    def test_local_concurrency_defaults_to_one(self) -> None:
        assert make_settings("ollama").max_concurrency == 1
        assert make_settings("llamacpp").max_concurrency == 1
        assert make_settings("ollama", max_concurrency=4).max_concurrency == 4
        assert make_settings("deepseek").max_concurrency == 2


# ---------------------------------------------------------------- EngineInfo 与语言对


def info_of(provider: str = "deepseek", key: str | None = KEY, **overrides: Any) -> EngineInfo:
    settings = make_settings(provider, **overrides)
    secrets = Secrets({} if key is None or settings.secret is None else {settings.secret: key})
    return OpenAICompatEngine(settings, secrets, httpx.AsyncClient()).info


class TestInfo:
    @pytest.mark.parametrize(
        ("provider", "kind", "billing"),
        [
            ("deepseek", EngineKind.ONLINE_LLM, True),
            ("openai", EngineKind.ONLINE_LLM, True),
            ("generic", EngineKind.ONLINE_LLM, True),
            ("ollama", EngineKind.LOCAL_LLM, False),
            ("llamacpp", EngineKind.LOCAL_LLM, False),
        ],
    )
    def test_kind_and_billing(self, provider: str, kind: EngineKind, billing: bool) -> None:
        info = info_of(provider)
        assert (info.kind, info.billing) == (kind, billing)

    def test_static_fields(self) -> None:
        info = info_of(
            max_batch_segments=7, max_batch_chars=900, timeout_s=45, max_concurrency=3, color="#f00"
        )
        assert (info.max_batch_segments, info.max_batch_chars) == (7, 900)
        assert (info.timeout_s, info.max_concurrency, info.color, info.priority) == (45, 3, "#f00", 1)
        assert info.id == "deepseek-1"

    @pytest.mark.parametrize(
        "change",
        [
            {"model": "deepseek-v4-pro"},
            {"temperature": 0.7},
            {"system_prompt": "Translate like a poet. Reply in json."},
            {"base_url": "https://other.deepseek.test"},
            {"structured_output": "json_schema"},
        ],
    )
    def test_fingerprint_follows_translation_settings(self, change: dict[str, Any]) -> None:
        assert info_of(**change).fingerprint != info_of().fingerprint

    def test_fingerprint_follows_provider(self) -> None:
        # 同样的地址和模型，provider 不同时结构化输出方式和请求体不同，译文可能不同。
        same = {"base_url": "https://llm.example.test/v1", "model": "m"}
        assert info_of("generic", **same).fingerprint != info_of("openai", **same).fingerprint

    def test_key_change_keeps_fingerprint_but_changes_revision(self) -> None:
        old, new = info_of(key="sk-old"), info_of(key="sk-new")
        assert old.fingerprint == new.fingerprint
        assert old.revision != new.revision
        assert KEY not in repr(info_of())

    def test_non_translation_settings_only_change_revision(self) -> None:
        old = info_of()
        new = info_of(languages=("en", "zh-Hans"), name="renamed", priority=5, max_retries=2)
        assert old.fingerprint == new.fingerprint
        assert old.revision != new.revision

    def test_blank_system_prompt_means_builtin(self) -> None:
        assert info_of(system_prompt="  ").fingerprint == info_of().fingerprint


class TestSupports:
    def test_any_pair_except_same_language(self) -> None:
        engine = make_engine(httpx.AsyncClient())
        assert engine.supports(EN, ZH)
        assert engine.supports(JA, EN)
        assert not engine.supports(EN, EN)

    def test_whitelist(self) -> None:
        engine = make_engine(httpx.AsyncClient(), languages=("en", "zh-Hans"))
        assert engine.supports(EN, ZH)
        assert engine.supports(ZH, EN)
        assert not engine.supports(EN, JA)
        assert not engine.supports(JA, ZH)
        assert not engine.supports(ZH, ZH)

    def test_whitelist_codes_are_normalized(self) -> None:
        """白名单按规范写法比较：zh、zh-CN 都是 zh-Hans，en-US 是 en。"""
        settings = make_settings("openai", languages=("zh", "en-US", "zh-CN"))
        assert settings.languages == ("zh-Hans", "en")
        engine = make_engine(httpx.AsyncClient(), "openai", languages=("zh-CN", "en-US"))
        assert engine.supports(EN, ZH)
        assert engine.supports(ZH, EN)

    def test_whitelist_rejects_invalid_code(self) -> None:
        with pytest.raises(ValidationError):
            make_settings("openai", languages=("english!",))


@pytest.mark.parametrize(
    ("code", "name"),
    [
        ("en", "English"),
        ("en-US", "English"),
        ("zh-Hans", "Simplified Chinese"),
        ("zh-Hant-TW", "Traditional Chinese"),
        ("zh-TW", "Traditional Chinese"),
        ("zh_CN", "Simplified Chinese"),
        ("pt-BR", "Brazilian Portuguese"),
        ("JA", "Japanese"),
        ("xx-YY", "xx-YY"),
    ],
)
def test_language_name(code: str, name: str) -> None:
    assert language_name(code) == name


# ---------------------------------------------------------------- 增量解析


def feed_all(parser: ReplyParser, *pieces: str) -> list[list[object]]:
    return [parser.feed(piece) for piece in pieces]


class TestReplyParser:
    def test_item_confirmed_when_next_item_starts(self) -> None:
        parser = ReplyParser()
        steps = feed_all(parser, '{"r":[{"i":1,"t":"甲"}', ",", "{", '"i":2,"t":"乙"}', "]", "}")
        assert steps == [[], [], [{"i": 1, "t": "甲"}], [], [], [{"i": 2, "t": "乙"}]]
        assert parser.closed

    def test_t_before_i_waits_for_next_item(self) -> None:
        parser = ReplyParser()
        steps = feed_all(parser, '{"r":[{"t":"x","i":1', "2", "}", ',{"t":"y","i":13}', "]}")
        assert steps == [[], [], [], [{"t": "x", "i": 12}], [{"t": "y", "i": 13}]]

    def test_last_item_only_after_whole_output_closes(self) -> None:
        parser = ReplyParser()
        assert parser.feed('{"r":[{"i":1,"t":"a"}]') == []
        assert parser.feed("}") == [{"i": 1, "t": "a"}]
        assert parser.feed(" trailing") == []
        assert parser.finish() == []

    def test_code_fence_is_tolerated(self) -> None:
        # 开头标记被跳过；根对象闭合时全部项立即确认，之后的结尾标记被忽略。
        parser = ReplyParser()
        got = parser.feed("```json\n" + reply_json("a", "b") + "\n```")
        assert got == [{"i": 1, "t": "a"}, {"i": 2, "t": "b"}]
        assert parser.closed
        assert parser.finish() == []

    def test_unclosed_output_is_not_completed_by_finish(self) -> None:
        parser = ReplyParser()
        assert parser.feed('```json\n{"r":[{"i":1,"t":"a"},{"i":2,"t":"b"}]') == [{"i": 1, "t": "a"}]
        assert parser.finish() == []  # 根对象没有闭合，截到最后一个 } 也解析不了
        assert not parser.closed

    def test_each_item_is_parsed_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """逐字符喂入 200 项：每项只严格解析一次，根对象闭合时再解析一次全文，不重复解析已扫描的部分。"""
        calls: list[int] = []
        real = jiter.from_json

        def counting(data: bytes, **kwargs: Any) -> object:
            calls.append(len(data))
            return real(data, **kwargs)

        monkeypatch.setattr(parser_module.jiter, "from_json", counting)
        texts = [f"译文{n}" for n in range(200)]
        output = reply_json(*texts)
        parser = ReplyParser()
        got: list[object] = []
        for char in output:
            got += parser.feed(char)
        assert [cast(dict[str, object], item)["t"] for item in got] == texts
        assert len(calls) == len(texts)  # 前 199 项各一次，最后一项随全文解析确认
        assert sum(calls) < 3 * len(output.encode("utf-8"))

    def test_truncated_output_keeps_last_item_unconfirmed(self) -> None:
        parser = ReplyParser()
        assert parser.feed('{"r":[{"i":1,"t":"a"},{"i":2,"t":"b') == [{"i": 1, "t": "a"}]
        assert parser.finish() == []
        assert not parser.closed
        assert parser.taken == 1

    def test_garbage(self) -> None:
        parser = ReplyParser()
        assert parser.feed("I cannot translate this.") == []
        assert parser.finish() == []
        assert not parser.closed

    def test_syntax_error_stops_parsing(self) -> None:
        parser = ReplyParser()
        assert parser.feed('{"r":[{"i":1,"t":"a"},oops') == []
        assert parser.malformed
        assert parser.feed(',{"i":2,"t":"b"}]}') == []
        assert parser.finish() == []

    def test_garbage_between_items_blocks_later_items(self) -> None:
        # partial 模式把这里的乱码当成输入结束，后面的项永远不会出现在解析结果里。
        parser = ReplyParser()
        assert parser.feed('{"r":[{"i":1,"t":"a"},{"i":2,"t":"b"}x,') == [{"i": 1, "t": "a"}]
        assert parser.feed('{"i":3,"t":"c"},{"i":4,"t":"d"}]}') == []
        assert parser.finish() == []
        assert parser.taken == 1

    def test_non_list_r(self) -> None:
        parser = ReplyParser()
        assert parser.feed('{"r":{"i":1,"t":"a"}}') == []
        assert parser.closed

    @hypothesis_settings(max_examples=300, deadline=None)
    @given(
        items=st.lists(
            st.tuples(st.integers(min_value=1, max_value=10_000), st.text(max_size=10), st.booleans()),
            max_size=6,
        ),
        indent=st.sampled_from([None, 1]),
        ascii_only=st.booleans(),
        fence=st.booleans(),
        cuts=st.lists(st.integers(min_value=0, max_value=100_000), max_size=40),
    )
    def test_any_split_gives_same_items(
        self,
        items: list[tuple[int, str, bool]],
        indent: int | None,
        ascii_only: bool,
        fence: bool,
        cuts: list[int],
    ) -> None:
        # t_first 为真的项先写 t 再写 i，对应 json_object 模式下键序不定的情况。
        expected: list[object] = [
            {"t": text, "i": index} if t_first else {"i": index, "t": text} for index, text, t_first in items
        ]
        output = json.dumps({"r": expected}, ensure_ascii=ascii_only, indent=indent)
        if fence:
            output = f"```json\n{output}\n```"
        points = sorted({cut % (len(output) + 1) for cut in cuts} | {0, len(output)})
        pieces = [output[a:b] for a, b in pairwise(points) if b > a]

        parser = ReplyParser()
        got: list[object] = []
        for piece in pieces:
            got += parser.feed(piece)
            # 已确认的项与最终结果逐项相同：不会产出截断的译文或没收全的段号。
            assert got == expected[: len(got)]
        got += parser.finish()
        assert got == expected
        assert parser.closed


class TestParseChunk:
    def test_content_and_finish_reason(self) -> None:
        assert parse_chunk('{"choices":[{"index":0,"delta":{"content":"ab"}}]}') == ("ab", None)
        assert parse_chunk('{"choices":[{"delta":{},"finish_reason":"length"}]}') == ("", "length")

    def test_chunks_without_content(self) -> None:
        assert parse_chunk('{"choices":[{"delta":{"role":"assistant","content":null}}]}') == ("", None)
        assert parse_chunk('{"choices":[],"usage":{"total_tokens":3}}') == ("", None)
        assert parse_chunk('{"choices":[{"delta":{"reasoning_content":"hmm"}}]}') == ("", None)

    @pytest.mark.parametrize("data", ["not json", "[1,2]", '"text"'])
    def test_malformed_chunk(self, data: str) -> None:
        with pytest.raises(EngineError) as info:
            parse_chunk(data)
        assert info.value.code is ErrorCode.BAD_RESPONSE

    @pytest.mark.parametrize(
        ("error", "code"),
        [
            ({"message": "overloaded", "type": "server_error"}, ErrorCode.SERVER),
            ({"message": "bad key", "code": "invalid_api_key"}, ErrorCode.AUTH),
            ({"message": "no money", "type": "insufficient_quota"}, ErrorCode.QUOTA),
            ({"message": "slow down", "code": 429}, ErrorCode.RATE_LIMITED),
            ({"message": "bad", "code": 400}, ErrorCode.BAD_REQUEST),
            ({"message": "weird", "type": "unknown"}, ErrorCode.BAD_RESPONSE),
            ("plain message", ErrorCode.BAD_RESPONSE),
        ],
    )
    def test_error_object(self, error: object, code: ErrorCode) -> None:
        with pytest.raises(EngineError) as info:
            parse_chunk(json.dumps({"error": error}))
        assert info.value.code is code

    def test_error_detail_is_truncated(self) -> None:
        detail = stream_error({"message": "x" * 1000}).detail
        assert detail.startswith("流中返回错误: x")
        assert len(detail) < 250


# ---------------------------------------------------------------- 流式翻译


class TestStreaming:
    async def test_segments_in_order_with_whitespace_stripped(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(sse_response('{"r":[{"i":1,"t":" 甲 "},', '{"i":2,"t":"乙\\n"},', '{"i":3,"t":"丙"}]}'))
        outcomes = await collect(make_engine(client).translate(make_job("a", "b", "c")))
        assert [(o.segment_id, o.text) for o in outcomes] == [("s1", "甲"), ("s2", "乙"), ("s3", "丙")]
        assert len(server.requests) == 1

    async def test_first_segment_arrives_before_rest_of_stream(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        stream = ContentStream(['{"r":[{"i":1,"t":"甲"},{"i":2,', '"t":"乙"}]}'], gate_before=[1])
        server.queue(stream_response(stream))
        outcomes = make_engine(client).translate(make_job("a", "b"))

        first = await asyncio.wait_for(anext(outcomes), timeout=2.0)
        assert (first.segment_id, first.text) == ("s1", "甲")
        assert stream.sent == 1  # 第二个片段还被闸门挡着

        stream.release(1)
        second = await asyncio.wait_for(anext(outcomes), timeout=2.0)
        assert (second.segment_id, second.text) == ("s2", "乙")
        with pytest.raises(StopAsyncIteration):
            await anext(outcomes)

    async def test_t_before_i_waits_for_next_item_or_close(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        pieces = ['{"r":[{"t":"甲","i":1', "}", ',{"t":"乙","i":2}', "]}"]
        stream = ContentStream(pieces, gate_before=[1, 2, 3])
        server.queue(stream_response(stream))
        outcomes = make_engine(client).translate(make_job("a", "b"))
        task = next_task(outcomes)

        await wait_until_blocked(stream, 1)  # t 和 i 都已出现，但这一项可能还没收全
        assert not task.done()
        stream.release(1)
        await wait_until_blocked(stream, 2)  # 这一项已闭合，但后面还没出现下一项
        assert not task.done()
        stream.release(2)
        first = await asyncio.wait_for(task, timeout=2.0)
        assert (first.segment_id, first.text) == ("s1", "甲")

        task = next_task(outcomes)
        await wait_until_blocked(stream, 3)  # 最后一项要等整个输出闭合
        assert not task.done()
        stream.release(3)
        second = await asyncio.wait_for(task, timeout=2.0)
        assert (second.segment_id, second.text) == ("s2", "乙")

    async def test_single_character_pieces(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        output = json.dumps(
            {"r": [{"t": '引号"与\\反斜杠', "i": 1}, {"i": 2, "t": "😀 emoji"}, {"i": 3, "t": "{}[],:"}]},
            ensure_ascii=False,
        )
        server.queue(sse_response(*output))
        outcomes = await collect(make_engine(client).translate(make_job("a", "b", "c")))
        assert texts_of(outcomes) == {"s1": '引号"与\\反斜杠', "s2": "😀 emoji", "s3": "{}[],:"}
        assert len(server.requests) == 1

    async def test_code_fence_needs_no_fallback(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        server.queue(sse_response("```json\n", reply_json("甲", "乙"), "\n```"))
        outcomes = await collect(make_engine(client).translate(make_job("a", "b")))
        assert texts_of(outcomes) == {"s1": "甲", "s2": "乙"}
        assert len(server.requests) == 1

    async def test_empty_job_sends_nothing(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        assert await collect(make_engine(client).translate(make_job())) == []
        assert server.requests == []


# ---------------------------------------------------------------- 逐段退回


class TestFallback:
    async def test_only_missing_segment_is_requested_again(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        batch_output = json.dumps({"r": [{"i": 1, "t": "甲"}, {"i": 3, "t": "丙"}]}, ensure_ascii=False)
        server.queue(sse_response(batch_output), reply_response("乙"))
        outcomes = await collect(make_engine(client).translate(make_job("a", "b", "c")))
        assert texts_of(outcomes) == {"s1": "甲", "s2": "乙", "s3": "丙"}
        assert len(server.requests) == 2
        assert server.segments(1) == [{"i": 1, "t": "b"}]

    async def test_unparseable_output_falls_back_for_all(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(
            sse_response("Sorry, here is the translation: 甲乙丙"),
            reply_response("甲"),
            reply_response("乙"),
            reply_response("丙"),
        )
        outcomes = await collect(make_engine(client).translate(make_job("a", "b", "c")))
        assert texts_of(outcomes) == {"s1": "甲", "s2": "乙", "s3": "丙"}
        assert [server.segments(n) for n in (1, 2, 3)] == [
            [{"i": 1, "t": "a"}],
            [{"i": 1, "t": "b"}],
            [{"i": 1, "t": "c"}],
        ]

    async def test_truncated_output_falls_back_for_unconfirmed(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(
            sse_response('{"r":[{"i":1,"t":"甲"},{"i":2,"t":"乙', finish_reason="length"),
            reply_response("乙"),
        )
        outcomes = await collect(make_engine(client).translate(make_job("a", "b")))
        assert texts_of(outcomes) == {"s1": "甲", "s2": "乙"}
        assert server.segments(1) == [{"i": 1, "t": "b"}]

    async def test_failed_fallback_fails_only_that_segment(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(
            sse_response(json.dumps({"r": [{"i": 2, "t": "乙"}]}, ensure_ascii=False)),
            sse_response("still not json"),
            httpx.Response(400, json={"error": {"message": "bad input"}}),
        )
        outcomes = await collect(make_engine(client).translate(make_job("a", "b", "c")))
        assert texts_of(outcomes) == {"s1": None, "s2": "乙", "s3": None}
        assert codes_of(outcomes) == {"s1": ErrorCode.BAD_RESPONSE, "s2": None, "s3": ErrorCode.BAD_REQUEST}
        assert len(server.requests) == 3

    async def test_service_error_in_fallback_stops_remaining_requests(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(
            sse_response(json.dumps({"r": [{"i": 3, "t": "丙"}]}, ensure_ascii=False)),
            httpx.Response(401, json={"error": {"message": "key revoked"}}),
        )
        outcomes = await collect(make_engine(client).translate(make_job("a", "b", "c")))
        assert codes_of(outcomes) == {"s1": ErrorCode.AUTH, "s2": ErrorCode.AUTH, "s3": None}
        assert len(server.requests) == 2

    async def test_invalid_items_are_ignored(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        batch_output = json.dumps(
            {
                "r": [
                    {"i": 1, "t": "甲"},
                    {"i": 1, "t": "重复"},
                    {"i": 0, "t": "越界"},
                    {"i": 9, "t": "越界"},
                    {"i": True, "t": "布尔"},
                    {"i": "2", "t": "字符串段号"},
                    {"i": 2, "t": 123},
                    "不是对象",
                ]
            },
            ensure_ascii=False,
        )
        server.queue(sse_response(batch_output), reply_response("乙"))
        outcomes = await collect(make_engine(client).translate(make_job("a", "b")))
        assert texts_of(outcomes) == {"s1": "甲", "s2": "乙"}
        assert len(server.requests) == 2

    async def test_single_segment_job_is_not_requested_twice(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(sse_response('{"r":[]}'))
        outcomes = await collect(make_engine(client).translate(make_job("a")))
        assert codes_of(outcomes) == {"s1": ErrorCode.BAD_RESPONSE}
        assert len(server.requests) == 1

    async def test_blank_translation_counts_as_missing(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        """原文非空而译文只有空白：按缺段处理，逐段重新请求。"""
        batch_output = json.dumps({"r": [{"i": 1, "t": "  "}, {"i": 2, "t": "乙"}]}, ensure_ascii=False)
        server.queue(sse_response(batch_output), reply_response("甲"))
        outcomes = await collect(make_engine(client).translate(make_job("a", "b")))
        assert texts_of(outcomes) == {"s1": "甲", "s2": "乙"}
        assert server.segments(1) == [{"i": 1, "t": "a"}]

    async def test_blank_translation_of_single_segment_fails(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(reply_response(" "))
        outcomes = await collect(make_engine(client).translate(make_job("a")))
        assert codes_of(outcomes) == {"s1": ErrorCode.BAD_RESPONSE}
        assert len(server.requests) == 1

    async def test_blank_source_accepts_blank_translation(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        batch_output = json.dumps({"r": [{"i": 1, "t": ""}, {"i": 2, "t": "乙"}]}, ensure_ascii=False)
        server.queue(sse_response(batch_output))
        outcomes = await collect(make_engine(client).translate(make_job(" ", "b")))
        assert texts_of(outcomes) == {"s1": "", "s2": "乙"}
        assert len(server.requests) == 1


# ---------------------------------------------------------------- 请求失败与断流


class TestErrors:
    async def test_auth_error_fails_all_without_fallback(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(httpx.Response(401, json={"error": {"message": "invalid key"}}))
        outcomes = await collect(make_engine(client).translate(make_job("a", "b", "c")))
        assert codes_of(outcomes) == {"s1": ErrorCode.AUTH, "s2": ErrorCode.AUTH, "s3": ErrorCode.AUTH}
        assert {o.error for o in outcomes} == {"HTTP 401: invalid key"}
        assert len(server.requests) == 1

    async def test_quota_error_is_not_retried(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        server.queue(httpx.Response(402, json={"error": {"message": "Insufficient Balance"}}))
        outcomes = await collect(make_engine(client).translate(make_job("a", "b")))
        assert set(codes_of(outcomes).values()) == {ErrorCode.QUOTA}
        assert len(server.requests) == 1

    async def test_rate_limit_with_short_retry_after(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        sleeps = Sleeps()
        server.queue(
            httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"message": "slow down"}}),
            reply_response("甲", "乙"),
        )
        outcomes = await collect(make_engine(client, sleeps=sleeps).translate(make_job("a", "b")))
        assert texts_of(outcomes) == {"s1": "甲", "s2": "乙"}
        assert sleeps.delays == [1.0]
        assert len(server.requests) == 2

    async def test_local_retries_503_only(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        sleeps = Sleeps()
        server.queue(httpx.Response(503), reply_response("甲"), httpx.Response(500))
        engine = make_engine(client, "ollama", sleeps=sleeps)
        assert texts_of(await collect(engine.translate(make_job("a")))) == {"s1": "甲"}
        assert codes_of(await collect(engine.translate(make_job("a")))) == {"s1": ErrorCode.SERVER}
        assert len(server.requests) == 3
        assert len(sleeps.delays) == 1

    async def test_stream_break_resends_unfinished_once(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        broken = ContentStream(['{"r":[{"i":1,"t":"甲"},{"i":2,"t":"乙'], error=httpx.ReadError("reset"))
        server.queue(stream_response(broken), reply_response("乙", "丙"))
        outcomes = await collect(make_engine(client).translate(make_job("a", "b", "c")))
        assert texts_of(outcomes) == {"s1": "甲", "s2": "乙", "s3": "丙"}
        assert server.segments(1) == [{"i": 1, "t": "b"}, {"i": 2, "t": "c"}]
        assert broken.closed

    async def test_second_stream_break_gives_network_error(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        first = ContentStream(['{"r":[{"i":1,"t":"甲"},{'], error=httpx.RemoteProtocolError("peer closed"))
        second = ContentStream(['{"r":[{"i":1,'], error=httpx.ReadError("reset"))
        server.queue(stream_response(first), stream_response(second))
        outcomes = await collect(make_engine(client).translate(make_job("a", "b", "c")))
        assert texts_of(outcomes)["s1"] == "甲"
        assert codes_of(outcomes) == {"s1": None, "s2": ErrorCode.NETWORK, "s3": ErrorCode.NETWORK}
        assert len(server.requests) == 2
        assert first.closed and second.closed

    async def test_no_resend_when_max_retries_is_zero(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        broken = ContentStream(['{"r":[{"i":1,"t":"甲"},{'], error=httpx.ReadError("reset"))
        server.queue(stream_response(broken))
        outcomes = await collect(make_engine(client, max_retries=0).translate(make_job("a", "b", "c")))
        assert codes_of(outcomes) == {"s1": None, "s2": ErrorCode.NETWORK, "s3": ErrorCode.NETWORK}
        assert len(server.requests) == 1
        assert broken.closed

    async def test_two_resends_when_max_retries_is_two(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        # 每次重发只含仍未完成的段，并重新编号；第二次重发后成功。
        server.queue(
            stream_response(ContentStream(['{"r":[{"i":1,"t":"甲"},{'], error=httpx.ReadError("r1"))),
            stream_response(ContentStream(['{"r":[{"i":1,"t":"乙"},{'], error=httpx.ReadError("r2"))),
            reply_response("丙"),
        )
        outcomes = await collect(make_engine(client, max_retries=2).translate(make_job("a", "b", "c")))
        assert texts_of(outcomes) == {"s1": "甲", "s2": "乙", "s3": "丙"}
        assert server.segments(1) == [{"i": 1, "t": "b"}, {"i": 2, "t": "c"}]
        assert server.segments(2) == [{"i": 1, "t": "c"}]

    async def test_resends_exhausted_when_max_retries_is_two(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        streams = [ContentStream([], error=httpx.ReadError(f"r{n}")) for n in range(3)]
        server.queue(*(stream_response(stream) for stream in streams))
        outcomes = await collect(make_engine(client, max_retries=2).translate(make_job("a", "b")))
        assert codes_of(outcomes) == {"s1": ErrorCode.NETWORK, "s2": ErrorCode.NETWORK}
        assert len(server.requests) == 3
        assert all(stream.closed for stream in streams)

    async def test_http_retry_and_resend_share_one_budget(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        """整批阶段的 HTTP 重试与断流重发共用 max_retries：503 重试一次之后再断流，不再重发。"""
        broken = ContentStream(['{"r":[{"i":1,"t":"甲"},{'], error=httpx.ReadError("reset"))
        server.queue(httpx.Response(503), stream_response(broken))
        outcomes = await collect(make_engine(client).translate(make_job("a", "b")))
        assert codes_of(outcomes) == {"s1": None, "s2": ErrorCode.NETWORK}
        assert len(server.requests) == 2

    async def test_resend_gets_remaining_http_retries(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        """max_retries 为 2：断流重发一次后遇到 503，还剩一次重试可用。"""
        broken = ContentStream(['{"r":[{"i":1,"t":"甲"},{'], error=httpx.ReadError("reset"))
        server.queue(stream_response(broken), httpx.Response(503), reply_response("乙"))
        outcomes = await collect(make_engine(client, max_retries=2).translate(make_job("a", "b")))
        assert texts_of(outcomes) == {"s1": "甲", "s2": "乙"}
        assert len(server.requests) == 3

    async def test_http_retry_disabled_when_max_retries_is_zero(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        sleeps = Sleeps()
        server.queue(httpx.Response(429, headers={"Retry-After": "1"}))
        engine = make_engine(client, sleeps=sleeps, max_retries=0)
        outcomes = await collect(engine.translate(make_job("a", "b")))
        assert set(codes_of(outcomes).values()) == {ErrorCode.RATE_LIMITED}
        assert (len(server.requests), sleeps.delays) == (1, [])

    @pytest.mark.parametrize("provider", ["openai", "ollama"])
    async def test_http_retries_twice_when_max_retries_is_two(
        self, client: httpx.AsyncClient, server: FakeServer, provider: str
    ) -> None:
        sleeps = Sleeps()
        server.queue(httpx.Response(503), httpx.Response(503), reply_response("甲"))
        engine = make_engine(client, provider, sleeps=sleeps, max_retries=2)
        outcomes = await collect(engine.translate(make_job("a")))
        assert texts_of(outcomes) == {"s1": "甲"}
        assert (len(server.requests), len(sleeps.delays)) == (3, 2)

    async def test_local_does_not_retry_500_even_with_retries(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(httpx.Response(500))
        engine = make_engine(client, "llamacpp", max_retries=3)
        assert codes_of(await collect(engine.translate(make_job("a")))) == {"s1": ErrorCode.SERVER}
        assert len(server.requests) == 1

    async def test_break_after_all_segments_is_harmless(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        stream = ContentStream([reply_json("甲", "乙")], error=httpx.ReadError("reset"))
        server.queue(stream_response(stream))
        outcomes = await collect(make_engine(client).translate(make_job("a", "b")))
        assert texts_of(outcomes) == {"s1": "甲", "s2": "乙"}
        assert len(server.requests) == 1

    async def test_error_object_in_stream(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        body = content_event('{"r":[{"i":1,"t":"甲"},{') + (
            b'data: {"error":{"message":"overloaded","type":"server_error"}}\n\n'
        )
        server.queue(httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body))
        outcomes = await collect(make_engine(client).translate(make_job("a", "b", "c")))
        assert texts_of(outcomes)["s1"] == "甲"
        assert codes_of(outcomes) == {"s1": None, "s2": ErrorCode.SERVER, "s3": ErrorCode.SERVER}
        assert outcomes[1].error == "流中返回错误: overloaded"
        assert len(server.requests) == 1

    async def test_missing_key_fails_without_request(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        engine = make_engine(client, "deepseek", secrets={})
        outcomes = await collect(engine.translate(make_job("a", "b")))
        assert codes_of(outcomes) == {"s1": ErrorCode.AUTH, "s2": ErrorCode.AUTH}
        assert server.requests == []

    async def test_missing_secret_name_fails_without_request(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        engine = make_engine(client, "openai", secret=None)
        outcomes = await collect(engine.translate(make_job("a")))
        assert codes_of(outcomes) == {"s1": ErrorCode.AUTH}
        assert server.requests == []


# ---------------------------------------------------------------- 时限


class TestTimeouts:
    async def test_first_token_timeout_on_silent_stream(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        stream = ContentStream([], stall=True)
        server.queue(stream_response(stream))
        engine = make_engine(client, first_token_timeout_s=0.05)
        outcomes = await collect(engine.translate(make_job("a", "b")))
        assert codes_of(outcomes) == {"s1": ErrorCode.TIMEOUT, "s2": ErrorCode.TIMEOUT}
        assert outcomes[0].error == "首 token 超时（0.05 秒）"
        assert len(server.requests) == 1
        assert stream.closed

    async def test_first_token_timeout_while_waiting_for_headers(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(never_respond)
        engine = make_engine(client, first_token_timeout_s=0.05)
        outcomes = await collect(engine.translate(make_job("a")))
        assert codes_of(outcomes) == {"s1": ErrorCode.TIMEOUT}

    async def test_first_token_means_non_empty_content(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        # 只有 role 和空 content 的 chunk 不算首 token。
        stream = ContentStream(["", ""], stall=True)
        server.queue(stream_response(stream))
        engine = make_engine(client, first_token_timeout_s=0.05)
        outcomes = await collect(engine.translate(make_job("a")))
        assert codes_of(outcomes) == {"s1": ErrorCode.TIMEOUT}

    async def test_idle_after_first_token_resends(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        stalled = ContentStream(['{"r":[{"i":1,"t":"甲"},{'], stall=True)
        server.queue(stream_response(stalled), reply_response("乙"))
        engine = make_engine(client, idle_timeout_s=0.05)
        outcomes = await collect(engine.translate(make_job("a", "b")))
        assert texts_of(outcomes) == {"s1": "甲", "s2": "乙"}
        assert server.segments(1) == [{"i": 1, "t": "b"}]
        assert stalled.closed

    async def test_online_request_timeouts(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        engine = make_engine(client)
        assert engine.first_token_timeout_s == 8.0
        server.queue(reply_response("甲"))
        await collect(engine.translate(make_job("a")))
        timeout = server.requests[0].extensions["timeout"]
        assert timeout == {"connect": 3.0, "read": 10.0, "write": 10.0, "pool": 3.0}
        assert engine.first_token_timeout_s == 8.0

    async def test_local_first_request_gets_model_loading_time(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        engine = make_engine(client, "ollama")
        assert engine.first_token_timeout_s == 60.0
        server.queue(reply_response("甲"), reply_response("乙"))
        await collect(engine.translate(make_job("a")))
        assert engine.first_token_timeout_s == 15.0
        await collect(engine.translate(make_job("b")))
        # 等待响应头也受 read 时限约束，所以 read 时限不小于首 token 时限。
        reads = [request.extensions["timeout"]["read"] for request in server.requests]
        assert reads == [60.0, 15.0]

    async def test_local_failure_before_first_token_keeps_long_limit(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        engine = make_engine(client, "llamacpp")
        server.queue(httpx.Response(500))
        await collect(engine.translate(make_job("a")))
        assert engine.first_token_timeout_s == 60.0

    async def test_local_timeout_returns_to_long_limit(
        self, client: httpx.AsyncClient, server: FakeServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(engine_module, "LOCAL_FIRST_TOKEN_WARM_S", 0.05)
        engine = make_engine(client, "ollama")
        server.queue(reply_response("甲"), stream_response(ContentStream([], stall=True)))
        await collect(engine.translate(make_job("a")))
        assert engine.first_token_timeout_s == 0.05
        outcomes = await collect(engine.translate(make_job("b")))
        assert codes_of(outcomes) == {"s1": ErrorCode.TIMEOUT}
        # 模型可能已被服务端卸载，下一个请求重新给出含模型加载的时限。
        assert engine.first_token_timeout_s == 60.0

    async def test_configured_first_token_timeout_overrides(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        engine = make_engine(client, "ollama", first_token_timeout_s=5.0)
        assert engine.first_token_timeout_s == 5.0
        server.queue(reply_response("甲"))
        await collect(engine.translate(make_job("a")))
        assert engine.first_token_timeout_s == 5.0
        assert server.requests[0].extensions["timeout"]["read"] == 10.0

    async def test_backoff_wait_is_not_counted_in_first_token_limit(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        """重试前的退避等待比首 token 时限长：重试照常发出，时限从重试发出时重新计算。"""

        async def slow_sleep(delay: float) -> None:
            await asyncio.sleep(0.1)

        server.queue(httpx.Response(503), reply_response("甲"))
        settings = make_settings("deepseek", first_token_timeout_s=0.05)
        engine = OpenAICompatEngine(settings, Secrets({"deepseek": KEY}), client, sleep=slow_sleep)
        outcomes = await collect(engine.translate(make_job("a")))
        assert texts_of(outcomes) == {"s1": "甲"}
        assert len(server.requests) == 2


# ---------------------------------------------------------------- 请求内容


async def send_one(client: httpx.AsyncClient, server: FakeServer, provider: str, **overrides: Any) -> None:
    server.queue(reply_response("甲", "乙"))
    engine = make_engine(client, provider, **overrides)
    await collect(engine.translate(make_job("Hello", "World", context="Hello\nWorld\nBye")))


class TestRequestBody:
    async def test_deepseek(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        await send_one(client, server, "deepseek")
        request = server.requests[0]
        body = server.body(0)
        assert str(request.url) == "https://api.deepseek.test/chat/completions"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert (body["model"], body["temperature"], body["stream"]) == ("deepseek-flash", 0.2, True)
        assert body["response_format"] == {"type": "json_object"}
        assert body["thinking"] == {"type": "disabled"}
        # json_object 模式要求提示词里出现 "json" 并给出输出示例。
        batch_part = server.batch_part(0)
        assert "json" in batch_part
        assert '{"r":[{"i":1,"t":' in batch_part

    async def test_openai_uses_strict_json_schema(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        await send_one(client, server, "openai", temperature=0.0)
        body = server.body(0)
        assert str(server.requests[0].url) == "https://api.openai.test/v1/chat/completions"
        assert "thinking" not in body
        assert body["temperature"] == 0.0
        response_format = body["response_format"]
        assert response_format["type"] == "json_schema"
        spec = response_format["json_schema"]
        assert (spec["name"], spec["strict"]) == ("translation", True)
        schema = spec["schema"]
        item = schema["properties"]["r"]["items"]
        for obj in (schema, item):
            assert obj["type"] == "object"
            assert obj["additionalProperties"] is False
            assert sorted(obj["required"]) == sorted(obj["properties"])
        assert item["properties"] == {"i": {"type": "integer"}, "t": {"type": "string"}}

    async def test_local_without_key_has_no_authorization(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        await send_one(client, server, "ollama")
        assert "authorization" not in server.requests[0].headers
        assert server.body(0)["model"] == "qwen3:8b"
        assert server.body(0)["response_format"]["type"] == "json_schema"

    async def test_local_with_key_sends_it(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        await send_one(client, server, "llamacpp", secret="local", secrets={"local": "sk-local"})
        assert server.requests[0].headers["authorization"] == "Bearer sk-local"

    async def test_messages(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        await send_one(client, server, "openai")
        messages = server.body(0)["messages"]
        # 只有一条 user 消息，兼容要求 user/assistant 严格交替的对话模板。
        assert [m["role"] for m in messages] == ["system", "user"]
        assert messages[0]["content"] == DEFAULT_SYSTEM_PROMPT
        user = server.user_message(0)
        assert user.startswith(server.context_part(0) + CONTEXT_SEPARATOR)
        assert server.context_part(0).endswith("Hello\nWorld\nBye")
        assert "from English to Simplified Chinese" in server.batch_part(0)
        assert server.segments(0) == [{"i": 1, "t": "Hello"}, {"i": 2, "t": "World"}]

    async def test_custom_system_prompt(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        await send_one(client, server, "deepseek", system_prompt="You translate game dialogue.")
        assert server.body(0)["messages"][0]["content"] == "You translate game dialogue."
        assert "json" in server.batch_part(0)  # 自定义提示词里没写 json 也满足 json_object 的要求

    async def test_empty_context_uses_batch_text(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        server.queue(reply_response("甲", "乙"))
        await collect(make_engine(client).translate(make_job("Hello", "World")))
        assert server.context_part(0).endswith("Hello\nWorld")

    async def test_prefix_is_stable_within_session(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        context = "Line one\nLine two\nLine three"
        engine = make_engine(client)
        server.queue(reply_response("一", "二"), reply_response("三"))
        await collect(engine.translate(make_job("Line one", "Line two", context=context)))
        await collect(engine.translate(make_job("Line three", context=context, target=JA, prefix="t")))
        first, second = server.body(0)["messages"], server.body(1)["messages"]
        # system 消息相同，user 消息中分隔标记之前的会话全文前缀也相同。
        assert first[0] == second[0]
        assert server.context_part(0) == server.context_part(1)
        assert server.context_part(0).endswith(context)
        # 两个请求的字面开头一直相同到分隔标记为止，之后才出现随批次变化的内容。
        shared = server.context_part(0) + CONTEXT_SEPARATOR
        assert server.user_message(0).startswith(shared)
        assert server.user_message(1).startswith(shared)
        assert server.batch_part(0) != server.batch_part(1)
        assert "Japanese" in server.batch_part(1)
        assert "Japanese" not in first[0]["content"] + shared

    async def test_fallback_shares_prefix_with_batch(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(sse_response('{"r":[{"i":1,"t":"甲"}]}'), reply_response("乙"))
        await collect(make_engine(client).translate(make_job("a", "b")))
        first, second = server.body(0)["messages"], server.body(1)["messages"]
        assert first[0] == second[0]
        assert server.context_part(0) == server.context_part(1)
        assert server.segments(1) == [{"i": 1, "t": "b"}]


def test_separator_split_is_unambiguous() -> None:
    # 会话原文和段落里都含有分隔标记的文字时，最后一次出现的标记仍是真正的分界：
    # 段落以 JSON 编码，换行被转义，以换行开头的标记不可能出现在 JSON 里。
    context = f"before{CONTEXT_SEPARATOR}after"
    texts = [f"x{CONTEXT_SEPARATOR}y", "plain"]
    system, user = build_messages("sys", context, texts, "en", "ja")
    assert (system["role"], user["role"]) == ("system", "user")
    head, _, tail = user["content"].rpartition(CONTEXT_SEPARATOR)
    assert head.endswith(context)
    segments = json.loads(tail[tail.index('{"segments"') :])["segments"]
    assert segments == [{"i": 1, "t": texts[0]}, {"i": 2, "t": "plain"}]


# ---------------------------------------------------------------- 隐私与取消


SOURCES = ("SRC-alpha 原文一", "SRC-beta 原文二", "SRC-gamma 原文三")
TRANSLATIONS = ("DST-alpha 译文一", "DST-beta 译文二", "DST-gamma 译文三")


class TestPrivacy:
    async def test_logs_contain_no_text_or_key(
        self, client: httpx.AsyncClient, server: FakeServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        a, b, c = TRANSLATIONS
        messy = json.dumps(
            {
                "r": [
                    {"i": 1, "t": a},
                    {"i": SOURCES[1], "t": b},
                    {"i": 2, "t": {"nested": c}},
                    {"i": 7, "t": c},
                    SOURCES[2],
                ]
            },
            ensure_ascii=False,
        )
        server.queue(
            sse_response(messy),  # 无效项被忽略，第 2、3 段逐段退回
            reply_response(b),
            sse_response(f"not json {c}"),
            stream_response(ContentStream([f'{{"r":[{{"i":1,"t":"{a}"}},{{"i"'], error=httpx.ReadError("x"))),
            httpx.Response(401, json={"error": {"message": "invalid key"}}),
        )
        engine = make_engine(client)
        outcomes = await collect(engine.translate(make_job(*SOURCES, context="\n".join(SOURCES))))
        outcomes += await collect(engine.translate(make_job(*SOURCES, prefix="t")))
        outcomes += await collect(make_engine(client, secrets={}).translate(make_job(*SOURCES, prefix="u")))

        assert any(o.ok for o in outcomes) and any(not o.ok for o in outcomes)
        logged = caplog.text
        # 确实走过了各条记日志的路径：无效项、逐段退回、断流重发、请求失败、缺少 key。
        for message in ("序号无效", "退回逐段请求", "重发未完成", "请求失败", "没有配置 API key"):
            assert message in logged
        for secret in (*SOURCES, *TRANSLATIONS, KEY, "SRC-", "DST-"):
            assert secret not in logged
            assert all(secret not in (o.error or "") for o in outcomes)


class TestCancellation:
    async def test_cancel_propagates_and_closes_response(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        stream = ContentStream(['{"r":[{"i":1,"t":"甲"},{', '"i":2,"t":"乙"}]}'], gate_before=[1])
        server.queue(stream_response(stream))
        outcomes = make_engine(client).translate(make_job("a", "b"))
        first = await anext(outcomes)
        assert first.text == "甲"

        task = next_task(outcomes)
        await wait_until_blocked(stream, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
        with pytest.raises(StopAsyncIteration):  # 生成器随 CancelledError 结束，没有吞掉取消
            await anext(outcomes)
        assert len(server.requests) == 1

    async def test_aclose_closes_response(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        stream = ContentStream(['{"r":[{"i":1,"t":"甲"},{', '"i":2,"t":"乙"}]}'], gate_before=[1])
        server.queue(stream_response(stream))
        outcomes = make_engine(client).translate(make_job("a", "b"))
        await anext(outcomes)
        assert isinstance(outcomes, AsyncGenerator)
        await outcomes.aclose()
        assert stream.closed

    async def test_cancel_while_waiting_for_headers(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(never_respond)
        task = next_task(make_engine(client).translate(make_job("a")))
        for _ in range(100):
            if server.requests:
                break
            await asyncio.sleep(0)
        assert len(server.requests) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_factory_returns_engine(client: httpx.AsyncClient, server: FakeServer) -> None:
    sleeps = Sleeps()
    engine = create_openai_compat_engine(
        make_settings("openai"), Secrets({"openai": KEY}), client, sleep=sleeps
    )
    assert isinstance(engine, OpenAICompatEngine)
    assert engine.info.kind is EngineKind.ONLINE_LLM
    assert engine.supports(Lang("en"), Lang("ja"))
    server.queue(httpx.Response(503), reply_response("甲"))
    outcomes = await collect(engine.translate(make_job("a")))
    assert texts_of(outcomes) == {"s1": "甲"}
    assert len(sleeps.delays) == 1  # 注入的 sleep 被重试使用


# ---------------------------------------------------------------- 附带截图（ENG-07）


def screenshot(width: int = 30, height: int = 12) -> JobImage:
    """真实编码的 PNG 截图：在 2 倍尺寸上生成像素，由竞速模块的编码函数缩小到 width×height。"""
    size = width * height * 16
    pixels = (bytes(range(256)) * (size // 256 + 1))[:size]
    raw = RawImage(width * 2, height * 2, width * 8, pixels)
    return encode_screenshot(raw, max(width, height))


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def image_part(server: FakeServer, index: int) -> dict[str, Any]:
    content = server.body(index)["messages"][1]["content"]
    assert isinstance(content, list)
    parts = cast(list[dict[str, Any]], content)
    assert [part["type"] for part in parts] == ["text", "image_url"]
    return parts[1]


class TestImageSettings:
    def test_defaults_off(self) -> None:
        settings = make_settings("openai")
        assert (settings.send_image, settings.image_max_side) == (False, 1568)
        assert info_of("openai").image_max_side is None

    def test_enabled_sets_image_max_side(self) -> None:
        assert info_of("openai", send_image=True).image_max_side == 1568
        assert info_of("ollama", send_image=True, image_max_side=1024).image_max_side == 1024
        assert info_of("openai", send_image=False, image_max_side=1024).image_max_side is None

    @pytest.mark.parametrize("side", [0, 255, 8193])
    def test_rejects_bad_image_max_side(self, side: int) -> None:
        with pytest.raises(ValidationError):
            make_settings("openai", send_image=True, image_max_side=side)

    def test_description_warns_about_sending(self) -> None:
        description = OpenAICompatSettings.model_fields["send_image"].description or ""
        assert "截图会发送给该服务" in description

    def test_fingerprint_ignores_image_options_but_revision_follows(self) -> None:
        # 带图的尝试在缓存键里另有截图摘要；不带图的请求与关闭选项时完全相同，译文缓存可以共用。
        off, on = info_of("openai"), info_of("openai", send_image=True)
        assert off.fingerprint == on.fingerprint
        assert off.revision != on.revision
        assert off != on  # 调度器据此替换实例


class TestImageRequest:
    async def test_body_has_text_then_image(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        image = screenshot()
        server.queue(reply_response("甲", "乙"), reply_response("甲", "乙"))
        engine = make_engine(client, "openai", send_image=True)
        context = "Hello\nWorld\nBye"
        await collect(engine.translate(make_job("Hello", "World", context=context)))
        await collect(engine.translate(make_job("Hello", "World", context=context, image=image)))

        plain, with_img = server.body(0)["messages"], server.body(1)["messages"]
        assert isinstance(plain[1]["content"], str)
        content = with_img[1]["content"]
        assert content[0] == {"type": "text", "text": plain[1]["content"]}  # 文本逐字相同
        url = image_part(server, 1)["image_url"]["url"]
        prefix = "data:image/png;base64,"
        assert url.startswith(prefix)
        data = base64.b64decode(url[len(prefix) :], validate=True)
        assert data == image.data
        assert png_size(data) == (30, 12)  # 60×24 的截图按最大边长 30 缩小
        # system 消息保留原提示词，末尾加一句截图的用途说明。
        assert plain[0]["content"] == DEFAULT_SYSTEM_PROMPT
        assert with_img[0]["content"] == f"{DEFAULT_SYSTEM_PROMPT}\n{IMAGE_NOTE}"
        # 其余字段与不带图时相同。
        rest = [{k: v for k, v in server.body(i).items() if k != "messages"} for i in (0, 1)]
        assert rest[0] == rest[1]

    async def test_image_note_follows_custom_prompt(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(reply_response("甲"))
        engine = make_engine(client, "openai", send_image=True, system_prompt="You translate menus.")
        await collect(engine.translate(make_job("Hello", image=screenshot())))
        assert server.body(0)["messages"][0]["content"] == f"You translate menus.\n{IMAGE_NOTE}"

    async def test_disabled_instance_ignores_image(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        server.queue(reply_response("甲"))
        await collect(make_engine(client, "openai").translate(make_job("Hello", image=screenshot())))
        messages = server.body(0)["messages"]
        assert isinstance(messages[1]["content"], str)
        assert messages[0]["content"] == DEFAULT_SYSTEM_PROMPT
        assert "base64" not in server.requests[0].content.decode()

    async def test_resend_and_fallback_keep_image(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        broken = ContentStream(['{"r":[{"i":1,"t":"甲"},{"i":2'], error=httpx.ReadError("reset"))
        server.queue(stream_response(broken), sse_response('{"r":[{"i":1,"t":"乙"}]}'), reply_response("丙"))
        engine = make_engine(client, "openai", send_image=True)
        outcomes = await collect(engine.translate(make_job("a", "b", "c", image=screenshot())))
        assert texts_of(outcomes) == {"s1": "甲", "s2": "乙", "s3": "丙"}
        assert len({image_part(server, i)["image_url"]["url"] for i in range(3)}) == 1
        # 前缀缓存：三次请求的 system 消息与会话全文前缀都相同。
        assert len({json.dumps(server.body(i)["messages"][0]) for i in range(3)}) == 1
        assert len({server.context_part(i) for i in range(3)}) == 1

    def test_with_image_keeps_text_verbatim(self) -> None:
        messages = build_messages("sys", "ctx", ["a", "b"], "en", "ja")
        result = with_image(messages, "data:image/png;base64,AAAA")
        assert result[0] == {"role": "system", "content": f"sys\n{IMAGE_NOTE}"}
        assert result[1] == {
            "role": "user",
            "content": [
                {"type": "text", "text": messages[1]["content"]},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }


_TICK = chr(96)  # 反引号


class TestImageErrors:
    @pytest.mark.parametrize(
        ("provider", "status", "message"),
        [
            (
                "deepseek",
                400,
                f"Failed to deserialize the JSON body: unknown variant {_TICK}image_url{_TICK}, "
                f"expected {_TICK}text{_TICK}",
            ),
            ("openai", 400, "Invalid content type. image_url is only supported by certain models."),
            ("generic", 422, "This model does not support vision input"),
            ("generic", 400, "Multimodal input is not enabled for this deployment"),
            ("ollama", 500, "this model is missing data required for image input"),
            ("llamacpp", 500, "image input is not supported - hint: you may need to provide the mmproj"),
        ],
    )
    async def test_maps_to_image_unsupported(
        self, client: httpx.AsyncClient, server: FakeServer, provider: str, status: int, message: str
    ) -> None:
        server.queue(httpx.Response(status, json={"error": {"message": message}}))
        engine = make_engine(client, provider, send_image=True)
        outcomes = await collect(engine.translate(make_job("a", "b", image=screenshot())))
        assert set(codes_of(outcomes).values()) == {ErrorCode.IMAGE_UNSUPPORTED}
        assert len(server.requests) == 1  # 不逐段退回

    async def test_stream_error_object_maps_too(self, client: httpx.AsyncClient, server: FakeServer) -> None:
        body = b'data: {"error":{"message":"image input is not supported","code":400}}\n\n'
        server.queue(httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body))
        engine = make_engine(client, "llamacpp", send_image=True)
        outcomes = await collect(engine.translate(make_job("a", image=screenshot())))
        assert codes_of(outcomes) == {"s1": ErrorCode.IMAGE_UNSUPPORTED}

    @pytest.mark.parametrize(
        ("provider", "status", "message", "code"),
        [
            ("openai", 401, "image access denied", ErrorCode.AUTH),
            ("openai", 429, "too many image requests", ErrorCode.RATE_LIMITED),
            ("openai", 400, "temperature must be <= 2", ErrorCode.BAD_REQUEST),
            ("generic", 500, "vision backend crashed", ErrorCode.SERVER),  # 在线服务的 5xx 不改判
        ],
    )
    async def test_other_errors_keep_their_code(
        self,
        client: httpx.AsyncClient,
        server: FakeServer,
        provider: str,
        status: int,
        message: str,
        code: ErrorCode,
    ) -> None:
        server.queue(httpx.Response(status, json={"error": {"message": message}}))
        engine = make_engine(client, provider, send_image=True, max_retries=0)
        outcomes = await collect(engine.translate(make_job("a", image=screenshot())))
        assert codes_of(outcomes) == {"s1": code}

    async def test_without_image_message_is_not_reinterpreted(
        self, client: httpx.AsyncClient, server: FakeServer
    ) -> None:
        message = f"unknown variant {_TICK}image_url{_TICK}"
        server.queue(httpx.Response(400, json={"error": {"message": message}}))
        engine = make_engine(client, "deepseek", send_image=True)
        outcomes = await collect(engine.translate(make_job("a")))  # 双语会话，没有截图
        assert codes_of(outcomes) == {"s1": ErrorCode.BAD_REQUEST}


class TestImagePrivacy:
    async def test_logs_and_errors_contain_no_image_data(
        self, client: httpx.AsyncClient, server: FakeServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        image = screenshot(64, 32)
        payload = base64.b64encode(image.data).decode()
        echoed = f"invalid image: data:image/png;base64,{payload[:150]}"
        server.queue(
            reply_response("甲"),
            httpx.Response(400, json={"error": {"message": echoed}}),
            httpx.Response(400, json={"error": {"message": f"bad request near {payload[:120]}"}}),
        )
        engine = make_engine(client, "openai", send_image=True)
        outcomes = await collect(engine.translate(make_job("a", image=image)))
        outcomes += await collect(engine.translate(make_job("a", image=image, prefix="t")))
        outcomes += await collect(engine.translate(make_job("a", image=image, prefix="u")))

        assert "附带截图 64×32" in caplog.text
        assert [o.code for o in outcomes] == [None, ErrorCode.IMAGE_UNSUPPORTED, ErrorCode.BAD_REQUEST]
        errors = [o.error or "" for o in outcomes]
        assert all("图片数据已省略" in e for e in errors[1:])
        for text in (caplog.text, *errors):
            assert payload[:40] not in text
            assert payload[60:100] not in text
            assert "base64," not in text
