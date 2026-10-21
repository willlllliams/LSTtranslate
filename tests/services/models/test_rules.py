"""服务与界面共用的规则：default_model_ids、版本号比较、ModelInfo 与 DownloadProgress 的派生属性。"""

from collections.abc import Sequence

import pytest

from lst.contracts.ids import Lang
from lst.contracts.models import (
    DownloadProgress,
    DownloadState,
    ModelCatalog,
    ModelInfo,
    ModelKind,
    nmt_model_id,
    version_key,
)
from lst.services.models import default_model_ids


def model(
    source: str,
    target: str,
    *,
    installed: bool = False,
    version: str = "1.9",
    installed_version: str | None = None,
    url: str = "https://packages.invalid/x.argosmodel",
) -> ModelInfo:
    return ModelInfo(
        id=nmt_model_id(source, target),
        kind=ModelKind.NMT,
        source=Lang(source),
        target=Lang(target),
        version=version,
        size_bytes=None,
        installed=installed,
        url=url,
        installed_version=installed_version
        if installed_version is not None
        else (version if installed else None),
    )


def catalog(*pairs: tuple[str, str], installed: Sequence[tuple[str, str]] = ()) -> ModelCatalog:
    models = [model(s, t) for s, t in pairs] + [model(s, t, installed=True, url="") for s, t in installed]
    return ModelCatalog(models=tuple(models))


def test_direct_models_in_both_directions() -> None:
    cat = catalog(("en", "zh-Hans"), ("zh-Hans", "en"), ("en", "de"))
    assert default_model_ids(cat, ["zh-Hans", "en"]) == (
        nmt_model_id("zh-Hans", "en"),
        nmt_model_id("en", "zh-Hans"),
    )


def test_pair_codes_are_normalized() -> None:
    cat = catalog(("en", "zh-Hans"), ("zh-Hans", "en"))
    assert default_model_ids(cat, ["zh-CN", "en-US"]) == (
        nmt_model_id("zh-Hans", "en"),
        nmt_model_id("en", "zh-Hans"),
    )


def test_installed_models_count_as_available() -> None:
    cat = catalog(("en", "ja"), installed=[("ja", "en")])
    assert default_model_ids(cat, ["ja", "en"]) == (nmt_model_id("ja", "en"), nmt_model_id("en", "ja"))


def test_pivot_through_english_when_no_direct_model() -> None:
    cat = catalog(("ja", "en"), ("en", "ja"), ("zh-Hans", "en"), ("en", "zh-Hans"))
    assert default_model_ids(cat, ["ja", "zh-Hans"]) == (
        nmt_model_id("ja", "en"),
        nmt_model_id("en", "zh-Hans"),
        nmt_model_id("zh-Hans", "en"),
        nmt_model_id("en", "ja"),
    )


def test_direct_model_preferred_over_pivot() -> None:
    cat = catalog(("ja", "zh-Hans"), ("ja", "en"), ("en", "zh-Hans"), ("zh-Hans", "en"), ("en", "ja"))
    assert default_model_ids(cat, ["ja", "zh-Hans"]) == (
        nmt_model_id("ja", "zh-Hans"),
        nmt_model_id("zh-Hans", "en"),
        nmt_model_id("en", "ja"),
    )


def test_direction_without_any_route_is_skipped() -> None:
    cat = catalog(("en", "ko"), ("ja", "en"))
    # ko→en 没有直连模型，经英语中转也不适用（目标就是英语）。
    assert default_model_ids(cat, ["en", "ko"]) == (nmt_model_id("en", "ko"),)
    # ja→ko 只有第一跳 ja→en，缺第二跳 en→ko，整条路线不可用；ko→ja 两跳都缺。
    assert default_model_ids(catalog(("ja", "en")), ["ja", "ko"]) == ()


@pytest.mark.parametrize(
    "pair", [[], ["en"], ["en", "en"], ["en", "???"], ["en", "de", "fr"], ["en-US", "en"]]
)
def test_invalid_pairs_give_nothing(pair: list[str]) -> None:
    cat = catalog(("en", "de"), ("de", "en"))
    assert default_model_ids(cat, pair) == ()


@pytest.mark.parametrize(
    ("older", "newer"),
    [("1.9", "1.10"), ("1.0", "1.0.1"), ("", "0.1"), ("1.9", "2"), ("v1.2", "v1.3")],
)
def test_version_key_orders_numerically(older: str, newer: str) -> None:
    assert version_key(older) < version_key(newer)


def test_update_available() -> None:
    assert model("en", "de", installed=True, version="1.10", installed_version="1.9").update_available
    assert not model("en", "de", installed=True, version="1.9").update_available
    assert not model("en", "de", installed=False).update_available
    # 索引里没有这个模型（没有下载地址）时不提示更新。
    assert not model(
        "en", "de", installed=True, version="2.0", installed_version="1.0", url=""
    ).update_available


def test_progress_fraction() -> None:
    assert DownloadProgress(DownloadState.DOWNLOADING, 25, 100).fraction == 0.25
    assert DownloadProgress(DownloadState.DOWNLOADING, 25, None).fraction is None
    assert DownloadProgress(DownloadState.DOWNLOADING, 150, 100).fraction == 1.0


def test_catalog_lookup() -> None:
    cat = catalog(("en", "de"))
    assert cat.model(nmt_model_id("en", "de")) == cat.models[0]
    assert cat.model("argos:xx:yy") is None
