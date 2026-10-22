"""ModelsModule 的词典包：词典索引的解析与获取、启动扫描、下载校验与原子安装、跳过与更新、取消、删除。

词典包是内存里拼出的 gzip 文本，词典索引与包都由 MockTransport 扮演的假服务器返回，测试不访问网络。
Argos 索引与词典索引分开获取：一个失败不影响另一个，NMT 模型的行为不变。
"""

import asyncio
import gzip
import hashlib
import json
import logging
from pathlib import Path

import pytest

from lst.contracts.ids import Lang
from lst.contracts.models import (
    CancelModelDownload,
    DeleteModel,
    DownloadModels,
    DownloadState,
    ModelCatalog,
    ModelErrorCode,
    ModelInfo,
    ModelKind,
    ModelsInstalled,
    RefreshModelCatalog,
    default_dict_ids,
    dict_model_id,
    dict_pack_file_name,
    model_kind_of,
    parse_dict_pack_file_name,
)
from lst.kernel import EventBus
from lst.services.models import dictionary as dicts
from tests.services.models.conftest import Published, StartModule
from tests.services.models.helpers import (
    DICT_INDEX_URL,
    EN_ZH,
    FakeServer,
    Route,
    index_item,
    write_package,
)

D = DownloadState
DICT_BASE = DICT_INDEX_URL.rsplit("/", 1)[0]
DICT_EN_ZH = dict_model_id("en", "zh-Hans")
DICT_ZH_EN = dict_model_id("zh-Hans", "en")
DICT_DE_EN = dict_model_id("de", "en")
WIKTIONARY = (
    "Adapted (extracted, filtered and trimmed) from Wiktionary by Wiktionary contributors "
    "(https://www.wiktionary.org/), extracted by wiktextract and published by kaikki.org "
    "(https://kaikki.org/); CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/)"
)
ECDICT = (
    "Adapted (extracted, filtered and trimmed) from ECDICT (https://github.com/skywind3000/ECDICT), "
    "Copyright (c) 2025 Linwei; MIT License"
)
_logger = logging.getLogger("tests.models.dict")


def dict_pack(source: str, target: str, *, version: str = "20261003", words: int = 3) -> bytes:
    """一个合成词典包的文件内容。words 控制词条数，用来得到不同的文件。"""
    name, license_id = (
        ("ECDICT", "MIT") if (source, target) == ("en", "zh-Hans") else ("Wiktionary", "CC-BY-SA-4.0")
    )
    lines = [f"#dict\t{source}\t{target}\t{version}\t{name}\t{license_id}"]
    lines += [f"w\tword{index}\t译{index}" for index in range(words)]
    return gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dict_item(source: str, target: str, body: bytes, **extra: object) -> dict[str, object]:
    """词典索引里的一项，字段与 dict-index.json 相同。"""
    is_ecdict = (source, target) == ("en", "zh-Hans")
    item: dict[str, object] = {
        "src": source,
        "tgt": target,
        "file": dict_pack_file_name(source, target),
        "bytes": len(body),
        "rawBytes": extra.pop("rawBytes") if "rawBytes" in extra else len(gzip.decompress(body)),
        "entries": 3,
        "forms": 0,
        "sha256": sha(body),
        "source": "ECDICT" if is_ecdict else "Wiktionary via kaikki.org 2026-10-03",
        "license": "MIT" if is_ecdict else "CC-BY-SA-4.0",
        "attribution": ECDICT if is_ecdict else WIKTIONARY,
    }
    item.update(extra)
    return item


def serve_dicts(server: FakeServer, packs: dict[tuple[str, str], bytes], **routes: Route) -> None:
    """在假服务器上放词典索引和包。routes 按文件名替换某个包的路由（例如加闸门或改内容）。"""
    items = [dict_item(source, target, body) for (source, target), body in packs.items()]
    server.add(DICT_INDEX_URL, Route(body=json.dumps({"version": 1, "packs": items}).encode()))
    for (source, target), body in packs.items():
        name = dict_pack_file_name(source, target)
        server.add(f"{DICT_BASE}/{name}", routes.get(name, Route(body=body)))


def files(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []


async def refresh(bus: EventBus) -> None:
    bus.send(RefreshModelCatalog())
    await bus.drain()


def dict_models(catalog: ModelCatalog) -> tuple[ModelInfo, ...]:
    return catalog.of_kind(ModelKind.DICT)


# ---------------------------------------------------------------- 共用规则


def test_dict_ids_and_file_names() -> None:
    assert DICT_EN_ZH == "dict:en:zh-Hans"
    assert model_kind_of(DICT_EN_ZH) is ModelKind.DICT
    assert model_kind_of(EN_ZH) is ModelKind.NMT
    assert dict_pack_file_name("zh-Hans", "en") == "dict-zh-Hans-en.tsv.gz"
    assert parse_dict_pack_file_name("dict-en-zh-Hans.tsv.gz") == ("en", "zh-Hans")
    assert parse_dict_pack_file_name("dict-zh-Hant-en.tsv.gz") == ("zh-Hant", "en")
    for name in ("dict-en-en.tsv.gz", "dict-en.tsv.gz", "dict-en-zh-Hans.tsv.gz.part", "en-de.tsv.gz"):
        assert parse_dict_pack_file_name(name) is None, name


def test_models_installed_reports_its_kinds() -> None:
    assert ModelsInstalled((DICT_EN_ZH,)).kinds() == {ModelKind.DICT}
    assert ModelsInstalled((EN_ZH,), removed=(DICT_EN_ZH,)).kinds() == {ModelKind.NMT, ModelKind.DICT}


def test_default_dict_ids_lists_both_directions_that_exist() -> None:
    def info(source: str, target: str) -> ModelInfo:
        return ModelInfo(
            dict_model_id(source, target), ModelKind.DICT, Lang(source), Lang(target), "", 1, False
        )

    catalog = ModelCatalog(models=(info("en", "zh-Hans"), info("zh-Hans", "en"), info("de", "en")))
    assert default_dict_ids(catalog, ["zh-Hans", "en"]) == (DICT_ZH_EN, DICT_EN_ZH)
    assert default_dict_ids(catalog, ["en", "de"]) == (DICT_DE_EN,)
    assert default_dict_ids(catalog, ["en", "en"]) == ()
    assert default_dict_ids(catalog, ["xx-invalid-?", "en"]) == ()


# ---------------------------------------------------------------- 词典索引的解析


def test_parse_dict_index_keeps_valid_items_and_skips_broken_ones(caplog: pytest.LogCaptureFixture) -> None:
    body = dict_pack("de", "en")
    good = dict_item("de", "en", body)
    data = {
        "version": 1,
        "packs": [
            dict_item("en", "zh-Hans", dict_pack("en", "zh-Hans")),
            good,
            {**good, "sha256": "xyz"},  # 摘要不合法
            {**good, "src": "en", "tgt": "fr"},  # 文件名与语言对不符
            {**good, "file": "../evil.tsv.gz"},  # 文件名会写到词典目录之外
            {**good, "src": "EN", "file": "dict-EN-en.tsv.gz"},  # 语言码不是规范写法
            {**good, "bytes": True},  # 布尔值不是字节数
            {key: value for key, value in good.items() if key != "attribution"},  # 缺字段
            "not an object",
        ],
    }
    caplog.set_level(logging.WARNING, logger=_logger.name)
    entries = dicts.parse_dict_index(data, DICT_INDEX_URL, _logger)
    assert [entry.pair for entry in entries] == [("de", "en"), ("en", "zh-Hans")]
    de_en = entries[0]
    assert de_en.url == f"{DICT_BASE}/dict-de-en.tsv.gz"
    assert (de_en.license, de_en.attribution, de_en.sha256) == ("CC-BY-SA-4.0", WIKTIONARY, sha(body))
    assert len([record for record in caplog.records if "已跳过" in record.getMessage()]) == 7


@pytest.mark.parametrize(
    "data", [[], {"version": 2, "packs": []}, {"version": 1}, {"version": 1, "packs": {}}]
)
def test_parse_dict_index_rejects_other_shapes(data: object) -> None:
    with pytest.raises(dicts.DictIndexFormatError):
        dicts.parse_dict_index(data, DICT_INDEX_URL, _logger)


def test_pack_url_sits_next_to_the_index() -> None:
    assert (
        dicts.pack_url(f"{DICT_INDEX_URL}?x=1", "dict-en-zh-Hans.tsv.gz")
        == f"{DICT_BASE}/dict-en-zh-Hans.tsv.gz"
    )


# ---------------------------------------------------------------- 启动扫描


async def test_start_lists_installed_packs_without_network(
    start_module: StartModule, published: Published, server: FakeServer, dict_dir: Path
) -> None:
    dict_dir.mkdir(parents=True)
    (dict_dir / "dict-de-en.tsv.gz").write_bytes(dict_pack("de", "en"))
    (dict_dir / "dict-en-fr.tsv.gz").write_bytes(dict_pack("en", "de"))  # 包头与文件名不符
    (dict_dir / "dict-en-ja.tsv.gz").write_bytes(b"not gzip")
    (dict_dir / "dict-en-ko.tsv.gz.part").write_bytes(b"left over from a crash")

    await start_module(dict_dir=dict_dir)

    assert server.requests == []
    catalog = published.catalog
    assert catalog.dicts_enabled and not catalog.dict_index_loaded
    (model,) = dict_models(catalog)
    assert (model.id, model.installed, model.installed_version) == (DICT_DE_EN, True, "20261003")
    # 没有说明文件时，署名由包头的数据源与许可证拼出。
    assert (model.license, model.attribution) == ("CC-BY-SA-4.0", "Wiktionary (CC-BY-SA-4.0)")
    assert model.size_bytes == (dict_dir / "dict-de-en.tsv.gz").stat().st_size
    assert "dict-en-ko.tsv.gz.part" not in files(dict_dir)


async def test_without_dict_dir_no_dictionaries_are_managed(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer
) -> None:
    server.add_index([index_item("en", "zh")])
    await start_module()
    await refresh(bus)
    assert not published.catalog.dicts_enabled
    assert dict_models(published.catalog) == ()
    assert server.requested("dict-index.json") == 0

    bus.send(DownloadModels((DICT_EN_ZH,)))
    await bus.drain()
    progress = published.progress(DICT_EN_ZH)
    assert progress is not None and progress.error_code is ModelErrorCode.UNAVAILABLE


# ---------------------------------------------------------------- 获取两个索引


async def test_refresh_fetches_both_indexes_and_merges_dictionaries(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, dict_dir: Path
) -> None:
    server.add_index([index_item("en", "zh")])
    serve_dicts(server, {("en", "zh-Hans"): dict_pack("en", "zh-Hans"), ("de", "en"): dict_pack("de", "en")})
    await start_module(dict_dir=dict_dir)
    await refresh(bus)

    catalog = published.catalog
    assert catalog.index_loaded and catalog.dict_index_loaded
    assert [model.kind for model in catalog.models] == [ModelKind.NMT, ModelKind.DICT, ModelKind.DICT]
    de_en, en_zh = dict_models(catalog)
    assert (en_zh.id, en_zh.installed, en_zh.license, en_zh.attribution) == (DICT_EN_ZH, False, "MIT", ECDICT)
    assert en_zh.url == f"{DICT_BASE}/dict-en-zh-Hans.tsv.gz"
    assert de_en.size_bytes == len(dict_pack("de", "en"))


async def test_dictionary_index_failure_does_not_affect_the_model_index(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, dict_dir: Path
) -> None:
    server.add_index([index_item("en", "zh")])
    server.add(DICT_INDEX_URL, Route(status=503))
    await start_module(dict_dir=dict_dir)
    await refresh(bus)

    catalog = published.catalog
    assert catalog.index_loaded and catalog.index_error is None
    assert not catalog.dict_index_loaded
    assert catalog.dict_index_error_code is ModelErrorCode.HTTP
    assert catalog.model(EN_ZH) is not None


async def test_model_index_failure_does_not_affect_the_dictionary_index(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, dict_dir: Path
) -> None:
    serve_dicts(server, {("de", "en"): dict_pack("de", "en")})
    await start_module(dict_dir=dict_dir)  # Argos 索引没有路由：404
    await refresh(bus)

    catalog = published.catalog
    assert catalog.index_error_code is ModelErrorCode.HTTP and not catalog.index_loaded
    assert catalog.dict_index_loaded and catalog.dict_index_error is None
    assert [model.id for model in dict_models(catalog)] == [DICT_DE_EN]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"{not json", ModelErrorCode.BAD_INDEX),
        (json.dumps({"version": 7}).encode(), ModelErrorCode.BAD_INDEX),
    ],
)
async def test_bad_dictionary_index_is_reported(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    dict_dir: Path,
    body: bytes,
    code: ModelErrorCode,
) -> None:
    server.add_index([])
    server.add(DICT_INDEX_URL, Route(body=body))
    await start_module(dict_dir=dict_dir)
    await refresh(bus)
    assert published.catalog.dict_index_error_code is code


# ---------------------------------------------------------------- 下载、校验与安装


async def start_with_dicts(
    bus: EventBus, start_module: StartModule, server: FakeServer, dict_dir: Path, **routes: Route
) -> dict[tuple[str, str], bytes]:
    packs = {("en", "zh-Hans"): dict_pack("en", "zh-Hans"), ("de", "en"): dict_pack("de", "en")}
    server.add_index([])
    serve_dicts(server, packs, **routes)
    await start_module(dict_dir=dict_dir)
    await refresh(bus)
    return packs


async def test_download_verifies_and_installs_atomically(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, dict_dir: Path
) -> None:
    gate = asyncio.Event()
    body = dict_pack("en", "zh-Hans")
    route = Route(body=body, gate=gate, chunk_size=64)
    await start_with_dicts(bus, start_module, server, dict_dir, **{"dict-en-zh-Hans.tsv.gz": route})

    bus.send(DownloadModels((DICT_EN_ZH,)))
    await asyncio.wait_for(route.reached.wait(), 5)
    # 下载期间只有临时文件，字典引擎看不到半个包。
    assert files(dict_dir) == ["dict-en-zh-Hans.tsv.gz.part"]
    gate.set()
    await bus.drain()

    assert files(dict_dir) == ["dict-en-zh-Hans.tsv.gz", "dict-en-zh-Hans.tsv.gz.json"]
    assert (dict_dir / "dict-en-zh-Hans.tsv.gz").read_bytes() == body
    meta = json.loads((dict_dir / "dict-en-zh-Hans.tsv.gz.json").read_text(encoding="utf-8"))
    assert (meta["license"], meta["attribution"], meta["sha256"]) == ("MIT", ECDICT, sha(body))
    assert published.states(DICT_EN_ZH) == [D.QUEUED, D.DOWNLOADING, D.VERIFYING, D.DONE]
    model = published.catalog.model(DICT_EN_ZH)
    assert model is not None and model.installed and not model.update_available
    assert model.attribution == ECDICT
    assert published.installed == [ModelsInstalled((DICT_EN_ZH,))]
    assert published.installed[0].kinds() == {ModelKind.DICT}


async def test_sha256_mismatch_fails_and_leaves_nothing(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, dict_dir: Path
) -> None:
    # 发布页上的文件被换过：SHA-256 与索引不符。
    served, code = b"tampered", ModelErrorCode.INVALID_PACKAGE
    await start_with_dicts(bus, start_module, server, dict_dir, **{"dict-de-en.tsv.gz": Route(body=served)})
    bus.send(DownloadModels((DICT_DE_EN,)))
    await bus.drain()
    progress = published.progress(DICT_DE_EN)
    assert progress is not None and progress.state is D.FAILED and progress.error_code is code
    assert files(dict_dir) == []
    assert published.installed == []


async def test_pack_whose_header_has_another_pair_is_refused(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, dict_dir: Path
) -> None:
    # 索引里 de→en 的摘要正好是一个 en→de 包的摘要：摘要校验通过，包头核对不通过。
    wrong = dict_pack("en", "de")
    server.add_index([])
    item = dict_item("de", "en", wrong)
    server.add(DICT_INDEX_URL, Route(body=json.dumps({"version": 1, "packs": [item]}).encode()))
    server.add(f"{DICT_BASE}/dict-de-en.tsv.gz", Route(body=wrong))
    await start_module(dict_dir=dict_dir)
    await refresh(bus)

    bus.send(DownloadModels((DICT_DE_EN,)))
    await bus.drain()
    progress = published.progress(DICT_DE_EN)
    assert progress is not None and progress.error_code is ModelErrorCode.PAIR_MISMATCH
    assert files(dict_dir) == []


@pytest.mark.parametrize(
    "body",
    [
        b"plain text, not gzip",
        gzip.compress(b"#dict\tde\ten\t1\tx\tMIT\nw\t\xff\tbad\n"),  # 不是 UTF-8
        gzip.compress(b"w\thund\tdog\n"),  # 没有包头
    ],
)
async def test_file_that_is_not_a_pack_is_refused_even_with_a_matching_sha256(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    dict_dir: Path,
    body: bytes,
) -> None:
    server.add_index([])
    item = dict_item("de", "en", body, rawBytes=1)
    server.add(DICT_INDEX_URL, Route(body=json.dumps({"version": 1, "packs": [item]}).encode()))
    server.add(f"{DICT_BASE}/dict-de-en.tsv.gz", Route(body=body))
    await start_module(dict_dir=dict_dir)
    await refresh(bus)

    bus.send(DownloadModels((DICT_DE_EN,)))
    await bus.drain()
    progress = published.progress(DICT_DE_EN)
    assert progress is not None and progress.error_code is ModelErrorCode.INVALID_PACKAGE
    assert files(dict_dir) == []


async def test_same_pack_is_not_downloaded_again(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, dict_dir: Path
) -> None:
    dict_dir.mkdir(parents=True)
    (dict_dir / "dict-de-en.tsv.gz").write_bytes(dict_pack("de", "en"))
    await start_with_dicts(bus, start_module, server, dict_dir)

    bus.send(DownloadModels((DICT_DE_EN,)))
    await bus.drain()
    assert server.requested("dict-de-en.tsv.gz") == 0
    model = published.catalog.model(DICT_DE_EN)
    assert model is not None and model.installed and not model.update_available


async def test_rebuilt_pack_is_offered_and_replaces_the_old_one(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, dict_dir: Path
) -> None:
    dict_dir.mkdir(parents=True)
    (dict_dir / "dict-de-en.tsv.gz").write_bytes(dict_pack("de", "en", version="20260901", words=2))
    packs = await start_with_dicts(bus, start_module, server, dict_dir)

    model = published.catalog.model(DICT_DE_EN)
    assert model is not None and model.update_available and model.installed_version == "20260901"

    bus.send(DownloadModels((DICT_DE_EN,)))
    await bus.drain()
    assert (dict_dir / "dict-de-en.tsv.gz").read_bytes() == packs[("de", "en")]
    model = published.catalog.model(DICT_DE_EN)
    assert model is not None and not model.update_available and model.installed_version == "20261003"


async def test_cancel_removes_the_partial_file(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, dict_dir: Path
) -> None:
    gate = asyncio.Event()
    route = Route(body=dict_pack("de", "en", words=2000), gate=gate, chunk_size=256)
    await start_with_dicts(bus, start_module, server, dict_dir, **{"dict-de-en.tsv.gz": route})

    bus.send(DownloadModels((DICT_DE_EN,)))
    await asyncio.wait_for(route.reached.wait(), 5)
    bus.send(CancelModelDownload(DICT_DE_EN))
    await bus.drain()
    gate.set()
    await bus.drain()

    progress = published.progress(DICT_DE_EN)
    assert progress is not None and progress.state is D.CANCELLED
    assert files(dict_dir) == []


async def test_models_and_dictionaries_share_one_queue(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    dict_dir: Path,
    models_dir: Path,
) -> None:
    server.add_index([index_item("en", "zh")])
    server.add_package("en", "zh")
    serve_dicts(server, {("en", "zh-Hans"): dict_pack("en", "zh-Hans")})
    await start_module(dict_dir=dict_dir)
    await refresh(bus)

    bus.send(DownloadModels((EN_ZH, DICT_EN_ZH)))
    await bus.drain()
    assert files(models_dir) == ["translate-en_zh-1_9.argosmodel"]
    assert files(dict_dir) == ["dict-en-zh-Hans.tsv.gz", "dict-en-zh-Hans.tsv.gz.json"]
    assert published.installed == [ModelsInstalled((EN_ZH,)), ModelsInstalled((DICT_EN_ZH,))]


# ---------------------------------------------------------------- 删除


async def test_delete_removes_the_pack_and_its_sidecar(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, dict_dir: Path
) -> None:
    await start_with_dicts(bus, start_module, server, dict_dir)
    bus.send(DownloadModels((DICT_DE_EN,)))
    await bus.drain()
    assert files(dict_dir) == ["dict-de-en.tsv.gz", "dict-de-en.tsv.gz.json"]

    bus.send(DeleteModel(DICT_DE_EN))
    await bus.drain()
    assert files(dict_dir) == []
    model = published.catalog.model(DICT_DE_EN)
    assert model is not None and not model.installed  # 索引里还有，可以再下载
    assert published.installed[-1] == ModelsInstalled((), removed=(DICT_DE_EN,))
    assert published.installed[-1].kinds() == {ModelKind.DICT}


async def test_delete_of_a_pack_that_is_not_installed_does_nothing(
    bus: EventBus, start_module: StartModule, published: Published, server: FakeServer, dict_dir: Path
) -> None:
    await start_with_dicts(bus, start_module, server, dict_dir)
    bus.send(DeleteModel(DICT_DE_EN))
    await bus.drain()
    assert published.installed == []


async def test_deleting_a_dictionary_leaves_nmt_models_alone(
    bus: EventBus,
    start_module: StartModule,
    published: Published,
    server: FakeServer,
    dict_dir: Path,
    models_dir: Path,
) -> None:
    write_package(models_dir, "en", "zh")
    dict_dir.mkdir(parents=True)
    (dict_dir / "dict-en-zh-Hans.tsv.gz").write_bytes(dict_pack("en", "zh-Hans"))
    await start_module(dict_dir=dict_dir)

    bus.send(DeleteModel(DICT_EN_ZH))
    await bus.drain()
    assert files(models_dir) == ["translate-en_zh-1_9.argosmodel"]
    model = published.catalog.model(EN_ZH)
    assert model is not None and model.installed
