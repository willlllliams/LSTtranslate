"""Windows OCR 后端的单元测试：不调用系统 OCR，用假的 OcrApi 代替。

覆盖：设置与请求选项的校验、识别语言的映射与 auto 回退、没有 OCR 语言时的配置错误、缩放尺寸与面积平均、
坐标换算回原图（取整与裁剪）、行文字的拼接、空结果，以及输出能通过主进程的 decode_blocks。
"""

from dataclasses import dataclass, field

import numpy as np
import pytest

from lst.contracts.worker_protocol import JsonValue
from lst.services.ocr.blocks import decode_blocks
from lst.workers._runtime import ConfigError
from lst.workers.ocr_worker import winocr_backend
from lst.workers.ocr_worker.blocks import blocks_to_json
from lst.workers.ocr_worker.geometry import CoordinateMap
from lst.workers.ocr_worker.image import Pixels
from lst.workers.ocr_worker.settings import BadOptions
from lst.workers.ocr_worker.winocr_backend import (
    NO_LANGUAGE_MESSAGE,
    WinOcrBackend,
    WinOcrOptions,
    WinOcrSettings,
    Word,
    assemble_line,
    build_blocks,
    canonical_language,
    match_language,
    resize_area,
    restore_word,
    target_size,
    to_bgra,
)
from tests.workers.ocr.validation import validate_blocks


def no_progress(kind: str, detail: str = "", done: int | None = None, total: int | None = None) -> None:
    del kind, detail, done, total


# ---------------------------------------------------------------- 假的系统接口


@dataclass
class FakeRecognizer:
    language_tag: str
    lines: list[list[Word]] = field(default_factory=lambda: list[list[Word]]())
    calls: list[tuple[int, int, int]] = field(default_factory=lambda: list[tuple[int, int, int]]())

    def recognize(self, bgra: bytes, width: int, height: int) -> list[list[Word]]:
        self.calls.append((len(bgra), width, height))
        return self.lines


@dataclass
class FakeApi:
    languages: list[str]
    profile: str | None = None
    max_image_dimension: int = 10000
    lines: list[list[Word]] = field(default_factory=lambda: list[list[Word]]())
    created: list[str | None] = field(default_factory=lambda: list[str | None]())
    recognizer: FakeRecognizer | None = None

    def available_languages(self) -> list[str]:
        return list(self.languages)

    def create(self, tag: str | None) -> FakeRecognizer | None:
        self.created.append(tag)
        chosen = self.profile if tag is None else tag
        if chosen is None or chosen not in self.languages:
            return None
        self.recognizer = FakeRecognizer(chosen, self.lines)
        return self.recognizer


# ---------------------------------------------------------------- 设置与选项


def test_settings_defaults_and_ignored_paddle_keys() -> None:
    settings = WinOcrSettings.parse(
        {"mode": "text", "engine": "windows", "device": "gpu", "table": True, "model_cache_dir": "x"}
    )
    assert settings == WinOcrSettings(language="auto", grayscale=False, resize_max_side=0)


@pytest.mark.parametrize(
    "raw",
    [
        {"mode": "structure"},
        {"engine": "paddle"},
        {"language": ""},
        {"language": 3},
        {"grayscale": "yes"},
        {"resize_max_side": -1},
        {"resize_max_side": True},
    ],
)
def test_invalid_settings_are_config_errors(raw: dict[str, JsonValue]) -> None:
    with pytest.raises(ConfigError):
        WinOcrSettings.parse(raw)


def test_request_options_override_defaults() -> None:
    settings = WinOcrSettings("auto", grayscale=True, resize_max_side=2000)
    assert WinOcrOptions.parse({}, settings) == WinOcrOptions(True, 2000)
    assert WinOcrOptions.parse({"grayscale": False, "resize_max_side": 0, "table": True}, settings) == (
        WinOcrOptions(False, 0)
    )
    with pytest.raises(BadOptions):
        WinOcrOptions.parse({"grayscale": 1}, settings)
    with pytest.raises(BadOptions):
        WinOcrOptions.parse({"resize_max_side": -5}, settings)


# ---------------------------------------------------------------- 识别语言


@pytest.mark.parametrize(
    ("tag", "code"),
    [("zh-Hans-CN", "zh-Hans"), ("zh-Hant-TW", "zh-Hant"), ("zh-TW", "zh-Hant"), ("en-US", "en"),
     ("ja", "ja"), ("pt-BR", "pt"), ("sr-Latn-RS", "sr"), ("x-bogus!", "x-bogus!")],
)  # fmt: skip
def test_canonical_language(tag: str, code: str) -> None:
    assert canonical_language(tag) == code


AVAILABLE = ["en-US", "zh-Hans-CN", "zh-Hant-TW", "ja"]


@pytest.mark.parametrize(
    ("requested", "tag"),
    [
        ("zh-Hans", "zh-Hans-CN"),
        ("zh", "zh-Hans-CN"),
        ("zh-CN", "zh-Hans-CN"),
        ("zh-Hant", "zh-Hant-TW"),
        ("zh-HK", "zh-Hant-TW"),
        ("en", "en-US"),
        ("EN-us", "en-US"),
        ("en-GB", "en-US"),
        ("ja-JP", "ja"),
        ("ko", None),
        ("de", None),
    ],
)
def test_match_language(requested: str, tag: str | None) -> None:
    assert match_language(requested, AVAILABLE) == tag


def test_auto_pairs_the_profile_language_with_english() -> None:
    api = FakeApi(["en-US", "zh-Hans-CN"], profile="zh-Hans-CN")
    result = WinOcrBackend(api).load({"language": "auto"}, no_progress)
    assert api.created == [None, "en-US"]
    assert result.capabilities == {
        "mode": "text",
        "engine": "windows",
        "language": "auto",
        "languages": ["en", "zh-Hans"],
        "features": ["text"],
    }
    assert result.versions["engine"] == "windows"


def test_auto_falls_back_to_the_first_available_language() -> None:
    api = FakeApi(["en-US", "zh-Hans-CN"], profile=None)
    result = WinOcrBackend(api).load({}, no_progress)
    assert api.created == [None, "en-US", "zh-Hans-CN"]
    assert result.capabilities["language"] == "auto"


def test_auto_keeps_other_profile_languages() -> None:
    api = FakeApi(["en-US", "de-DE", "zh-Hans-CN"], profile="de-DE")
    result = WinOcrBackend(api).load({}, no_progress)
    assert api.created == [None]
    assert result.capabilities["language"] == "de"


def test_auto_with_one_language_uses_one_recognizer() -> None:
    api = FakeApi(["en-US"], profile="en-US")
    result = WinOcrBackend(api).load({}, no_progress)
    assert api.created == [None]
    assert result.capabilities["language"] == "en"


def test_auto_partner_creation_failure_preserves_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi(["en-US", "zh-Hans-CN"], profile="zh-Hans-CN")
    create = api.create

    def with_failure(tag: str | None) -> FakeRecognizer | None:
        return None if tag == "en-US" else create(tag)

    monkeypatch.setattr(api, "create", with_failure)
    result = WinOcrBackend(api).load({}, no_progress)
    assert result.capabilities["language"] == "zh-Hans"


@pytest.mark.parametrize("profile", ["en-US", "zh-Hans-CN"])
def test_auto_runs_both_recognizers_on_the_same_prepared_image(
    profile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    english = FakeRecognizer("en-US", [[Word("Screen", 100, 10, 80, 20)]])
    native = FakeRecognizer("zh-Hans-CN", [[Word("屏", 10, 10, 20, 20), Word("幕", 32, 10, 20, 20),
                                         Word("Screen", 100, 10, 80, 20)]])  # fmt: skip
    handles = {"en-US": english, "zh-Hans-CN": native}
    api = FakeApi(list(handles), profile=profile)

    def create(tag: str | None) -> FakeRecognizer | None:
        return handles.get(profile if tag is None else tag)

    monkeypatch.setattr(api, "create", create)
    backend = WinOcrBackend(api)
    backend.load({}, no_progress)
    blocks = backend.recognize(np.zeros((80, 400, 3), np.uint8), {})
    (block,) = validate_blocks(blocks, 400, 80)
    assert block["lines"] == [{"bbox": [10, 10, 180, 30], "text": "屏幕 Screen", "score": 1.0}]
    assert english.calls == native.calls == [(400 * 80 * 4, 400, 80)]


def test_explicit_language_is_mapped_to_the_system_tag() -> None:
    api = FakeApi(["en-US", "zh-Hans-CN"], profile="en-US")
    result = WinOcrBackend(api).load({"language": "zh-Hans"}, no_progress)
    assert api.created == ["zh-Hans-CN"]
    assert result.capabilities["language"] == "zh-Hans"


def test_missing_language_is_a_config_error_listing_available_ones() -> None:
    api = FakeApi(["en-US", "zh-Hans-CN"])
    with pytest.raises(ConfigError, match="ko") as info:
        WinOcrBackend(api).load({"language": "ko"}, no_progress)
    assert "en-US, zh-Hans-CN" in str(info.value)


def test_no_ocr_language_is_a_config_error() -> None:
    with pytest.raises(ConfigError) as info:
        WinOcrBackend(FakeApi([])).load({}, no_progress)
    assert str(info.value) == NO_LANGUAGE_MESSAGE
    assert "光学字符识别" in NO_LANGUAGE_MESSAGE


def test_recognize_before_load_fails() -> None:
    with pytest.raises(RuntimeError):
        WinOcrBackend(FakeApi(["en-US"])).recognize(np.zeros((4, 4, 3), np.uint8), {})


# ---------------------------------------------------------------- 缩放


@pytest.mark.parametrize(
    ("size", "resize", "limit", "target"),
    [
        ((1920, 1080), 0, 10000, (1920, 1080)),
        ((1920, 1080), 4000, 10000, (1920, 1080)),
        ((12000, 3000), 0, 10000, (10000, 2500)),
        ((12000, 3000), 4000, 10000, (4000, 1000)),
        ((3000, 12001), 0, 10000, (2500, 10000)),
        ((5000, 100), 0, 0, (5000, 100)),
        ((20001, 1), 0, 10000, (10000, 1)),
    ],
)
def test_target_size(size: tuple[int, int], resize: int, limit: int, target: tuple[int, int]) -> None:
    assert target_size(*size, resize, limit) == target


def test_resize_area_averages_blocks() -> None:
    image = np.zeros((4, 6, 3), np.uint8)
    image[:2, :3] = 200  # 左上 2×3 的块
    image[2:, 3:] = (10, 20, 30)
    out = resize_area(image, (2, 2))
    assert out.shape == (2, 2, 3)
    assert out[0, 0].tolist() == [200, 200, 200]
    assert out[0, 1].tolist() == [0, 0, 0]
    assert out[1, 1].tolist() == [10, 20, 30]


def test_resize_area_handles_fractional_scale_and_strips() -> None:
    rng = np.random.default_rng(0)
    image = rng.integers(0, 256, (37, 2100, 3), dtype=np.uint8)
    out = resize_area(image, (1000, 15))
    assert out.shape == (15, 1000, 3)
    # 面积平均保持整体均值（每个输出像素覆盖相同面积）。
    assert abs(float(out.mean()) - float(image.mean())) < 0.6
    assert resize_area(image, (2100, 37)) is image
    with pytest.raises(ValueError):
        resize_area(image, (2200, 37))


def test_to_bgra_adds_opaque_alpha() -> None:
    image = np.array([[[1, 2, 3], [4, 5, 6]]], np.uint8)
    assert to_bgra(image) == bytes([1, 2, 3, 255, 4, 5, 6, 255])


# ---------------------------------------------------------------- 行与块


def words(*items: tuple[str, float, float, float, float]) -> list[Word]:
    return [Word(*item) for item in items]


def test_chinese_words_are_joined_without_spaces() -> None:
    line = words(("屏", 100, 171, 37, 34), ("幕", 138, 169, 35, 36), ("翻", 175, 171, 34, 34))
    assert assemble_line(line) == ("屏幕翻", (100, 169, 209, 205))


def test_latin_words_are_joined_with_spaces() -> None:
    line = words(("Windows", 204, 270, 162, 31), ("OCR", 380, 271, 78, 30), ("engine", 470, 271, 118, 39))
    text, box = assemble_line(line) or ("", (0, 0, 0, 0))
    assert text == "Windows OCR engine"
    assert box == (204, 270, 588, 310)


def test_mixed_scripts_use_the_gap() -> None:
    # 汉字与拉丁文字之间：间距 14 像素（行高 36 的 0.39 倍）加空格，间距 3 像素不加。
    spaced = words(("行", 175, 369, 36, 36), ("mixed", 225, 370, 102, 31), ("文", 478, 369, 36, 36))
    tight = words(("行", 175, 369, 36, 36), ("mixed", 214, 370, 102, 31), ("文", 319, 369, 36, 36))
    assert (assemble_line(spaced) or ("",))[0] == "行 mixed 文"
    assert (assemble_line(tight) or ("",))[0] == "行mixed文"


def test_full_width_punctuation_attaches_directly() -> None:
    line = words(("你好", 0, 0, 40, 20), ("，", 40, 0, 10, 20), ("OK", 52, 0, 30, 20))
    assert (assemble_line(line) or ("",))[0] == "你好，OK"


def test_blank_words_and_lines_are_skipped() -> None:
    assert assemble_line([]) is None
    assert assemble_line(words((" ", 0, 0, 5, 5))) is None
    cmap = CoordinateMap.identity(100, 100)
    assert build_blocks([[], words(("", 1, 1, 2, 2))], cmap) == []


def test_lines_are_mapped_back_to_the_original_and_clamped() -> None:
    # 原图 2000×1000 缩小成 1000×500 识别：坐标乘 2，左上向下取整、右下向上取整，再裁剪到原图内。
    cmap = CoordinateMap((2000, 1000), (1000, 500))
    lines = [
        words(("Hello", 10.2, 20.6, 50, 10), ("world", 70, 20, 40.3, 12)),
        words(("边缘", 980, 490, 30, 20)),  # 超出缩小后的图像，裁剪到原图右下角
        words(("外面", 1200, 600, 10, 10)),  # 完全在图像之外，丢弃
    ]
    blocks = blocks_to_json(build_blocks(lines, cmap))
    (block,) = validate_blocks(blocks, 2000, 1000)
    assert block["bbox"] == [0, 0, 2000, 1000]
    assert block["text"] == ""
    assert block["lines"] == [
        {"bbox": [20, 40, 221, 64], "text": "Hello world", "score": 1.0},
        {"bbox": [1960, 980, 2000, 1000], "text": "边缘", "score": 1.0},
    ]
    (decoded,) = decode_blocks(blocks, width=2000, height=1000)
    assert [line.text for line in decoded.lines] == ["Hello world", "边缘"]


def test_recognize_resizes_and_maps_coordinates_with_grayscale_on() -> None:
    api = FakeApi(["en-US"], profile="en-US", max_image_dimension=500)
    api.lines = [words(("Big", 100, 50, 100, 25))]
    backend = WinOcrBackend(api)
    backend.load({"grayscale": True}, no_progress)
    image = np.zeros((400, 1000, 3), np.uint8)
    image[:, :, 2] = 255  # 纯红
    blocks = backend.recognize(image, {})
    recognizer = api.recognizer
    assert recognizer is not None
    assert recognizer.calls == [(500 * 200 * 4, 500, 200)]
    (block,) = validate_blocks(blocks, 1000, 400)
    assert block["lines"] == [{"bbox": [200, 100, 400, 150], "text": "Big", "score": 1.0}]
    # 请求里的 resize_max_side 比系统上限更小时取请求的值。
    backend.recognize(image, {"resize_max_side": 256, "grayscale": False})
    assert recognizer.calls[-1] == (256 * 102 * 4, 256, 102)


def test_image_is_resized_before_grayscale(monkeypatch: pytest.MonkeyPatch) -> None:
    """预处理先缩放再灰度化，灰度化只处理缩小后的像素（与 Paddle 后端的顺序相同）。"""
    seen: list[tuple[int, ...]] = []
    real = winocr_backend.to_grayscale

    def recording(image: Pixels) -> Pixels:
        seen.append(image.shape)
        return real(image)

    monkeypatch.setattr(winocr_backend, "to_grayscale", recording)
    api = FakeApi(["en-US"], profile="en-US", max_image_dimension=500)
    backend = WinOcrBackend(api)
    backend.load({"grayscale": True}, no_progress)
    backend.recognize(np.zeros((400, 1000, 3), np.uint8), {})
    assert seen == [(200, 500, 3)]


def test_empty_result_is_an_empty_array() -> None:
    backend = WinOcrBackend(FakeApi(["en-US"], profile="en-US"))
    backend.load({}, no_progress)
    assert backend.recognize(np.full((20, 30, 3), 255, np.uint8), {}) == []


@pytest.mark.parametrize("angle", [None, 0.0])
def test_unrotated_word_is_unchanged(angle: float | None) -> None:
    word = Word("text", 40, 35, 28, 27)
    assert restore_word(word, angle, 1600, 300) is word


@pytest.mark.parametrize(
    "angle,expected", [(90.0, (100.0, 0.0, 10.0, 20.0)), (-90.0, (90.0, 80.0, 10.0, 20.0))]
)
def test_word_rotation_uses_image_center_and_all_four_corners(
    angle: float, expected: tuple[float, float, float, float]
) -> None:
    word = restore_word(Word("text", 50, 40, 20, 10), angle, 200, 100)
    assert (word.x, word.y, word.width, word.height) == pytest.approx(expected)
    assert word.text == "text"
