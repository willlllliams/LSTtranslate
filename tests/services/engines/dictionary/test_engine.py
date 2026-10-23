"""字典翻译引擎：supports 按已装的包判断、逐段翻译与错误码、在线程里读包并缓存、包变化后重新读取、耗时。"""

import asyncio
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import EngineId, Lang, SegmentId, SessionId
from lst.contracts.models import dict_pack_file_name
from lst.contracts.translation import EngineKind, Job, Segment, SegmentOutcome
from lst.services.engines.dictionary import DictionarySettings, create_dictionary_engine
from lst.services.engines.dictionary import library as library_module
from lst.services.engines.dictionary.engine import DictionaryEngine
from lst.services.engines.dictionary.library import PackLibrary, PackUnavailableError, shared_library
from lst.services.engines.dictionary.pack import DictPack, normalize_key
from tests.services.engines.dictionary.helpers import (
    EN_ZH_ENTRIES,
    EN_ZH_FORMS,
    ZH_EN_ENTRIES,
    pack_bytes,
    write_pack,
)

EN, ZH, ZH_HANT, DE, FR = Lang("en"), Lang("zh-Hans"), Lang("zh-Hant"), Lang("de"), Lang("fr")


def settings(**fields: object) -> DictionarySettings:
    return DictionarySettings.model_validate({"id": "dictionary", "name": "词典", "priority": 9, **fields})


def engine_for(directory: Path, **fields: object) -> DictionaryEngine:
    """每个测试用自己的 PackLibrary，不与进程里共用的实例互相影响。"""
    return create_dictionary_engine(settings(**fields), directory, library=PackLibrary(directory))


def job(source: Lang, target: Lang, *texts: str) -> Job:
    segments = tuple(Segment(SegmentId(f"s{index}"), text) for index, text in enumerate(texts))
    return Job(SessionId("session"), EngineId("dictionary"), segments, source, target)


async def run(engine: DictionaryEngine, request: Job) -> list[SegmentOutcome]:
    return [outcome async for outcome in engine.translate(request)]


# ---------------------------------------------------------------- 配置


def test_defaults_need_no_key_and_never_retry() -> None:
    config = settings()
    assert config.max_retries == 0
    assert config.pivot_via_english is False
    assert config.timeout_s == 5.0
    with pytest.raises(ValueError):
        settings(max_retries=1)


def test_info_describes_an_offline_free_engine(tmp_path: Path) -> None:
    info = engine_for(tmp_path).info
    assert (info.id, info.kind, info.billing, info.priority) == ("dictionary", EngineKind.LOCAL_NMT, False, 9)
    # 中转开关影响译文，参与缓存指纹。
    assert engine_for(tmp_path, pivot_via_english=True).info.fingerprint != info.fingerprint


# ---------------------------------------------------------------- supports


def test_supports_follows_the_installed_packs(tmp_path: Path) -> None:
    directory = tmp_path / "dict"
    engine = engine_for(directory)
    # 目录还不存在。
    assert not engine.supports(EN, ZH)

    write_pack(directory, "en", "zh-Hans", EN_ZH_ENTRIES)
    assert engine.supports(EN, ZH)
    assert not engine.supports(ZH, EN)  # 方向不对
    assert not engine.supports(EN, EN)

    (directory / dict_pack_file_name("en", "zh-Hans")).unlink()
    assert not engine.supports(EN, ZH)


def test_supports_sees_additions_and_removals_with_the_same_directory_time(tmp_path: Path) -> None:
    write_pack(tmp_path, "en", "zh-Hans", EN_ZH_ENTRIES)
    engine = engine_for(tmp_path)
    stamp = tmp_path.stat().st_mtime_ns
    assert engine.supports(EN, ZH)
    write_pack(tmp_path, "zh-Hans", "en", ZH_EN_ENTRIES)
    os.utime(tmp_path, ns=(stamp, stamp))
    assert engine.supports(ZH, EN)
    (tmp_path / dict_pack_file_name("en", "zh-Hans")).unlink()
    os.utime(tmp_path, ns=(stamp, stamp))
    assert not engine.supports(EN, ZH)


def test_supports_ignores_files_that_are_not_packs(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    for name in (
        "dict-en-zh-Hans.tsv.gz.part",
        "dict-en-zh-Hans.tsv.gz.json",
        "dict-xx.tsv.gz",
        "readme.txt",
    ):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "dict-en-de.tsv.gz").mkdir()  # 同名目录不算包
    assert PackLibrary(tmp_path).installed_pairs() == frozenset()


def test_pivot_through_english_is_optional_and_skips_chinese_variants(tmp_path: Path) -> None:
    write_pack(tmp_path, "de", "en", {"hund": "dog"})
    write_pack(tmp_path, "en", "fr", {"dog": "chien"})
    write_pack(tmp_path, "zh-Hans", "en", ZH_EN_ENTRIES)
    write_pack(tmp_path, "en", "zh-Hant", {"dog": "狗"})
    assert not engine_for(tmp_path).supports(DE, FR)
    pivot = engine_for(tmp_path, pivot_via_english=True)
    assert pivot.route(DE, FR) == ((DE, EN), (EN, FR))
    assert not pivot.supports(ZH, ZH_HANT)


# ---------------------------------------------------------------- translate


async def test_translate_segments(tmp_path: Path) -> None:
    write_pack(tmp_path, "en", "zh-Hans", EN_ZH_ENTRIES, EN_ZH_FORMS)
    outcomes = await run(
        engine_for(tmp_path),
        job(EN, ZH, "The cat is sleeping.", "  \n", "3.14 / 42", "Lorem ipsum", "The zork cat"),
    )
    assert [outcome.segment_id for outcome in outcomes] == ["s0", "s1", "s2", "s3", "s4"]
    assert outcomes[0].text == "那猫是睡眠."
    assert outcomes[1].text == "  \n"  # 空白段原样返回
    assert outcomes[2].text == "3.14 / 42"  # 只有数字和标点：没有可查的词，原样就是译文
    # 有词却一个都查不到：以 NO_MATCH 失败，让其他引擎胜出，不把原文当作译文。
    assert outcomes[3].text is None and outcomes[3].code is ErrorCode.NO_MATCH
    assert outcomes[4].text == "那 zork 猫"  # 部分命中：未命中的词原样保留


async def test_translate_without_a_pack_fails_with_model_missing(tmp_path: Path) -> None:
    outcomes = await run(engine_for(tmp_path), job(EN, ZH, "cat", "the"))
    assert [outcome.code for outcome in outcomes] == [ErrorCode.MODEL_MISSING] * 2


async def test_broken_pack_fails_with_model_missing(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / dict_pack_file_name("en", "zh-Hans")).write_bytes(b"not gzip")
    outcomes = await run(engine_for(tmp_path), job(EN, ZH, "cat"))
    assert outcomes[0].code is ErrorCode.MODEL_MISSING


async def test_pack_whose_header_disagrees_with_its_name_is_refused(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / dict_pack_file_name("en", "zh-Hans")).write_bytes(pack_bytes("en", "de", {"cat": "Katze"}))
    library = PackLibrary(tmp_path)
    with pytest.raises(PackUnavailableError, match="与文件名不符"):
        library.load("en", "zh-Hans")


async def test_translate_through_english(tmp_path: Path) -> None:
    write_pack(tmp_path, "de", "en", {"der": "the", "hund": "dog"})
    write_pack(tmp_path, "en", "fr", {"the": "le", "dog": "chien"})
    outcomes = await run(engine_for(tmp_path, pivot_via_english=True), job(DE, FR, "Der Hund", "Xyz"))
    assert outcomes[0].text == "le chien"
    assert outcomes[1].code is ErrorCode.NO_MATCH


async def test_empty_job_yields_nothing(tmp_path: Path) -> None:
    assert await run(engine_for(tmp_path), job(EN, ZH)) == []


# ---------------------------------------------------------------- 读包：线程、缓存与重新读取


async def test_pack_is_loaded_in_a_worker_thread_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_pack(tmp_path, "en", "zh-Hans", EN_ZH_ENTRIES)
    original = library_module.read_pack_file
    threads: list[threading.Thread] = []

    def recording(path: Path) -> DictPack:
        threads.append(threading.current_thread())
        return original(path)

    monkeypatch.setattr(library_module, "read_pack_file", recording)
    engine = engine_for(tmp_path)
    await run(engine, job(EN, ZH, "cat"))
    await run(engine, job(EN, ZH, "the cat"))
    assert len(threads) == 1  # 第二次用缓存
    assert threads[0] is not threading.main_thread()


async def test_updated_pack_is_read_again(tmp_path: Path) -> None:
    path = write_pack(tmp_path, "en", "zh-Hans", {"cat": "猫"})
    engine = engine_for(tmp_path)
    assert (await run(engine, job(EN, ZH, "cat")))[0].text == "猫"

    path.write_bytes(pack_bytes("en", "zh-Hans", {"cat": "猫咪", "dog": "狗"}))
    stamp = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))
    assert (await run(engine, job(EN, ZH, "cat")))[0].text == "猫咪"


async def test_deleted_pack_is_dropped_from_memory(tmp_path: Path) -> None:
    path = write_pack(tmp_path, "en", "zh-Hans", {"cat": "猫"})
    library = PackLibrary(tmp_path)
    library.load("en", "zh-Hans")
    assert library.loaded_pairs() == ((EN, ZH),)
    path.unlink()
    with pytest.raises(PackUnavailableError):
        library.load("en", "zh-Hans")
    assert library.loaded_pairs() == ()


def test_cache_keeps_the_most_recently_used_packs(tmp_path: Path) -> None:
    for target in ("de", "fr", "ja"):
        write_pack(tmp_path, "en", target, {"cat": target})
    library = PackLibrary(tmp_path, max_loaded=2)
    library.load("en", "de")
    library.load("en", "fr")
    library.load("en", "de")
    library.load("en", "ja")
    assert library.loaded_pairs() == ((EN, DE), (EN, Lang("ja")))


def test_shared_library_is_one_per_directory(tmp_path: Path) -> None:
    assert shared_library(tmp_path) is shared_library(tmp_path / "." / "")
    assert shared_library(tmp_path) is not shared_library(tmp_path / "other")


async def test_concurrent_jobs_share_one_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_pack(tmp_path, "en", "zh-Hans", EN_ZH_ENTRIES)
    original = library_module.read_pack_file
    calls: list[Path] = []

    def slow(path: Path) -> DictPack:
        calls.append(path)
        time.sleep(0.05)
        return original(path)

    monkeypatch.setattr(library_module, "read_pack_file", slow)
    engine = engine_for(tmp_path)
    results = await asyncio.gather(*(run(engine, job(EN, ZH, "cat")) for _ in range(4)))
    assert [outcomes[0].text for outcomes in results] == ["猫"] * 4
    assert len(calls) == 1


async def test_logs_do_not_contain_the_text(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    write_pack(tmp_path, "en", "zh-Hans", EN_ZH_ENTRIES)
    caplog.set_level(logging.DEBUG, logger="lst.services.engines.dictionary")
    await run(engine_for(tmp_path), job(EN, ZH, "The secret cat"))
    assert caplog.records
    assert all(
        "secret" not in record.getMessage() and "猫" not in record.getMessage() for record in caplog.records
    )


# ---------------------------------------------------------------- 耗时


def _large_packs(directory: Path) -> None:
    """接近真实规模的包：英译中 6 万个词条，中译英 4 万个词条（1 到 4 字）。"""
    words = [f"w{index}" for index in range(60_000)]
    entries = {word: "词" for word in words} | dict(EN_ZH_ENTRIES)
    write_pack(directory, "en", "zh-Hans", entries, EN_ZH_FORMS)
    chars = [chr(code) for code in range(0x4E00, 0x4E00 + 400)]
    zh_entries: dict[str, str] = dict(ZH_EN_ENTRIES)
    for index in range(40_000):
        size = 1 + index % 4
        key = "".join(chars[(index * 7 + step * 13) % len(chars)] for step in range(size))
        zh_entries[normalize_key(key, "zh-Hans")] = f"e{index}"
    write_pack(directory, "zh-Hans", "en", zh_entries)


async def test_thousand_characters_translate_within_milliseconds(tmp_path: Path) -> None:
    _large_packs(tmp_path)
    engine = engine_for(tmp_path)
    english = ("The cat is sleeping in front of the warm house, w1 w2 w3. " * 20)[:1000]
    chinese = "中华人民共和国万岁，我爱你。" + "".join(chr(0x4E00 + i % 400) for i in range(40))
    chinese = (chinese * 20)[:1000]
    # 第一次包含读包；之后测纯翻译耗时。
    await run(engine, job(EN, ZH, english))
    await run(engine, job(ZH, EN, chinese))
    for source, target, text in ((EN, ZH, english), (ZH, EN, chinese)):
        started = time.perf_counter()
        outcomes = await run(engine, job(source, target, text))
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert outcomes[0].text
        # 实测几毫秒；上限放宽到 200 毫秒，避免慢速 CI 误报。
        assert elapsed_ms < 200, f"{source}→{target} 1000 字用时 {elapsed_ms:.1f} 毫秒"
