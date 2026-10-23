"""原始四张 PNG 上的语言互补回归；缓存绑定源图，区分识别与内容保留。

英文结果必须保留英文识别器的全部词；繁体中文图保留中文识别器已有的文字。
后者验证合并没有丢字，不把系统识别器已有的错字计为正确识别。
"""

import hashlib
import json
from collections import Counter
from typing import TypedDict, cast

import pytest

from lst.contracts.text import is_cjk
from lst.workers.ocr_worker.winocr_backend import Word, fuse_languages
from tests.fidelity.pipeline import FIXTURES, load_image


class WordData(TypedDict):
    text: str
    x: float
    y: float
    width: float
    height: float


class Candidates(TypedDict):
    source_sha256: str
    width: int
    height: int
    lines: list[list[WordData]]


def candidates(sample: str, language: str) -> list[list[Word]]:
    path = FIXTURES / "windows-languages" / f"{sample}-{language}.json"
    cached = cast(Candidates, json.loads(path.read_text("utf-8")))
    source = FIXTURES / f"{sample}.png"
    assert cached["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    image = load_image(source)
    assert (cached["width"], cached["height"]) == (image.width(), image.height())
    return [[Word(**word) for word in row] for row in cached["lines"]]


@pytest.mark.parametrize("sample", ["addons-en", "blog-en"])
def test_english_pages_keep_every_english_word_without_false_chinese(sample: str) -> None:
    english, native = candidates(sample, "en-US"), candidates(sample, "zh-Hans-CN")
    actual = fuse_languages(english, native)
    assert Counter(word for row in actual for word in row) == Counter(word for row in english for word in row)


# 人工查看原图标出的星形区域；数值计数在右侧，不在这些矩形里。
STAR_BOXES = (
    *(
        (568, y0, 680, y1)
        for y0, y1 in ((390, 410), (500, 522), (609, 631), (867, 890), (977, 1000), (1088, 1110))
    ),
    *((995, y0, 1109, y1) for y0, y1 in ((390, 410), (500, 522), (609, 631), (867, 890), (977, 1000))),
)


def star(word: Word, sample: str) -> bool:
    x, y = word.x + word.width / 2, word.y + word.height / 2
    return sample == "addons-zh-hant" and any(x0 <= x < x1 and y0 <= y < y1 for x0, y0, x1, y1 in STAR_BOXES)


def cjk(word: Word) -> bool:
    return any(is_cjk(char) and char.isalpha() for char in word.text)


@pytest.mark.parametrize("sample", ["addons-zh-hant", "blog-zh-hant"])
def test_chinese_pages_keep_recognized_text_and_remove_false_chinese_in_stars(sample: str) -> None:
    english, native = candidates(sample, "en-US"), candidates(sample, "zh-Hans-CN")
    actual = [word for row in fuse_languages(english, native) for word in row]
    expected = Counter(word for row in native for word in row if cjk(word) and not star(word, sample))
    assert Counter(word for word in actual if cjk(word)) == expected
    assert not any(cjk(word) and star(word, sample) for word in actual)
