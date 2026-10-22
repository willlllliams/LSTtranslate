"""Windows 自带 OCR（Windows.Media.Ocr）的后端（需求 OCRE-01，lst/contracts/ocr_engines.py）。

后端通过 pywinrt 调用系统的 OcrEngine，运行在主环境里，不导入 paddle、paddlex、cv2；
启动命令是
``python -m lst.workers.ocr_worker --backend lst.workers.ocr_worker.winocr_backend:WinOcrBackend``。
winrt 只在 load 时导入，导入失败（不是 Windows，或缺 pywinrt 的包）时进程以 DEPENDENCY_ERROR 退出。

Configure 的设置
----------------

与 Paddle 后端共用 lst/services/ocr/config.py 的 worker_settings，这里用到的键：

| 键 | 默认值 | 含义 |
|---|---|---|
| mode | "text" | 只能是 text；Windows OCR 没有版面分析，给出 structure 按配置错误处理 |
| engine | "windows" | 给出时只能是 windows |
| language | "auto" | auto 或规范语言码，见下文“识别语言” |
| grayscale | false | 请求没有给出时的默认预处理：灰度化 |
| resize_max_side | 0 | 请求没有给出时的默认预处理：长边上限，0 表示不缩放 |

Paddle 专用的键（device、子能力开关、模型目录等，见 settings.KNOWN_KEYS）直接忽略；其余未知的键记 WARNING
后忽略。
请求 options 里的 grayscale、resize_max_side 覆盖上表的默认值，取值类型不对时回复 engine.bad_request；
mode、language 与当前设置不同时只记 WARNING（切换由主进程重载进程）。

识别语言
--------

Windows 的 OCR 引擎一次只识别一种语言，可用的语言取决于系统装了哪些语言的 OCR 功能
（OcrEngine.available_recognizer_languages，例如 en-US、zh-Hans-CN）。

- auto：用 OcrEngine.try_create_from_user_profile_languages（用户语言列表里第一个装了 OCR 的语言）；
  返回 None 时取第一个可用语言。
- 指定语言：在可用语言里找规范写法（lst.contracts.langs.normalize_lang）相同的一项，例如 zh-Hans 对应
  zh-Hans-CN、en 对应 en-US。找不到时按配置错误处理，消息列出系统可用的语言。
- 系统一个 OCR 语言都没有时按配置错误处理（退出码 66），消息说明要在 Windows 设置里添加语言的 OCR 功能。

识别与坐标
----------

1. 处理器给出 BGR 图像；先按 resize_max_side 与 OcrEngine.max_image_dimension 中较小的一个等比缩小
   （面积平均，与 OpenCV 的 INTER_AREA 同一原理），再按 grayscale 灰度化，补上不透明的 alpha 通道，
   复制成 Bgra8 格式的 SoftwareBitmap，交给 OcrEngine.recognize_async。
2. 结果按行输出：行的包围盒是该行各词矩形的并集，经 geometry.CoordinateMap 换算回原始截图，
   左上角向下取整、右下角向上取整并裁剪到截图内；行的文字由词拼成（见 assemble_line）。
3. 输出遵守文本模式的约定：一个覆盖整张截图的 text 块，order 为 0，text 为空串，lines 为全部非空行；
   没有识别到文字时返回空数组。

Windows OCR 不给置信度，行和块的 score 都写 1.0；主进程只用 score 做展示和排序之外的判断时要注意这一点。

recognize_async 返回 WinRT 的异步操作。推理线程在测试里可能是单线程套间（Qt 已初始化 COM），
单线程套间不允许阻塞等待（IAsyncOperation.get 会抛 RuntimeError），所以用 asyncio.run 等待它完成。
"""

import asyncio
import importlib.metadata
import logging
import platform
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

import numpy as np
from numpy.typing import NDArray

from lst.contracts.langs import normalize_lang
from lst.contracts.ocr import BlockCategory
from lst.contracts.text import is_cjk, needs_space
from lst.contracts.worker_protocol import JsonValue
from lst.workers._runtime import ConfigError, LoadResult, ProgressReporter
from lst.workers.ocr_worker.blocks import OutBlock, OutLine, blocks_to_json
from lst.workers.ocr_worker.geometry import Box, CoordinateMap, plan_resize, union_box
from lst.workers.ocr_worker.image import Pixels, to_grayscale
from lst.workers.ocr_worker.settings import KNOWN_KEYS, BadOptions

if TYPE_CHECKING:
    from winrt.windows.media.ocr import OcrEngine

log = logging.getLogger(__name__)

ENGINE: Final = "windows"
AUTO: Final = "auto"
SCORE: Final = 1.0
"""Windows OCR 不给置信度，行与块一律写 1.0。"""

MIXED_GAP_RATIO: Final = 0.3
"""汉字与拉丁文字相邻时，词间距不小于行高的这个比例才加空格，见 assemble_line。"""

NO_LANGUAGE_MESSAGE: Final = (
    "Windows 没有安装任何 OCR 语言。请在“设置 → 时间和语言 → 语言和区域”中为一种语言安装“光学字符识别”"
    "语言功能（例如在“中文(简体)”或“English”的语言选项里添加），然后重新启动 OCR"
)

# 面积平均缩放时每次处理的条带宽度（像素），限制中间数组的内存。
_RESIZE_STRIP: Final = 1024


# ---------------------------------------------------------------- 设置与选项


def _bool(raw: Mapping[str, JsonValue], key: str, default: bool, error: type[Exception]) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise error(f"{key} 应为布尔值，实际为 {type(value).__name__}")
    return value


def _non_negative_int(raw: Mapping[str, JsonValue], key: str, default: int, error: type[Exception]) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise error(f"{key} 应为整数，实际为 {type(value).__name__}")
    if value < 0:
        raise error(f"{key} 不能为负，实际为 {value}")
    return value


@dataclass(frozen=True, slots=True)
class WinOcrSettings:
    """Configure 的设置，见模块说明的表格。"""

    language: str
    grayscale: bool
    resize_max_side: int

    @classmethod
    def parse(cls, raw: Mapping[str, JsonValue]) -> "WinOcrSettings":
        """校验设置；无效时抛 ConfigError。"""
        unknown = sorted(set(raw) - KNOWN_KEYS)
        if unknown:
            log.warning("忽略未知的设置项：%s", ", ".join(unknown))
        mode = raw.get("mode", "text")
        if mode != "text":
            raise ConfigError(f"Windows OCR 只有文本模式，mode 应为 text，实际为 {mode!r}")
        engine = raw.get("engine", ENGINE)
        if engine != ENGINE:
            raise ConfigError(f"本后端是 Windows OCR，engine 应为 {ENGINE}，实际为 {engine!r}")
        language = raw.get("language", AUTO)
        if not isinstance(language, str) or not language.strip():
            raise ConfigError("language 应为非空字符串")
        return cls(
            language=language.strip(),
            grayscale=_bool(raw, "grayscale", False, ConfigError),
            resize_max_side=_non_negative_int(raw, "resize_max_side", 0, ConfigError),
        )


@dataclass(frozen=True, slots=True)
class WinOcrOptions:
    """一次请求实际生效的预处理参数。"""

    grayscale: bool
    resize_max_side: int

    @classmethod
    def parse(cls, raw: Mapping[str, JsonValue], settings: WinOcrSettings) -> "WinOcrOptions":
        """按请求的 options 覆盖设置里的默认值；取值类型不对时抛 BadOptions。子能力开关全部忽略。"""
        for key, current in (("mode", "text"), ("language", settings.language)):
            requested = raw.get(key)
            if requested is not None and requested != current:
                log.warning("请求的 %s 为 %r，与进程当前的 %r 不同；切换需要重载进程，本次按 %r 处理",
                            key, requested, current, current)  # fmt: skip
        return cls(
            grayscale=_bool(raw, "grayscale", settings.grayscale, BadOptions),
            resize_max_side=_non_negative_int(raw, "resize_max_side", settings.resize_max_side, BadOptions),
        )


# ---------------------------------------------------------------- 识别语言


def canonical_language(tag: str) -> str:
    """Windows 语言标签的规范写法：zh-Hans-CN → zh-Hans，en-US → en。无法规范化时原样返回。"""
    try:
        return normalize_lang(tag)
    except ValueError:
        return tag


def match_language(requested: str, available: Sequence[str]) -> str | None:
    """在系统可用的识别语言标签里找与 requested 对应的一项，见模块说明；找不到时返回 None。

    先找标签完全相同（不区分大小写）的一项，再找规范写法相同的第一项。
    """
    wanted = requested.strip()
    for tag in available:
        if tag.casefold() == wanted.casefold():
            return tag
    target = canonical_language(wanted)
    for tag in available:
        if canonical_language(tag) == target:
            return tag
    return None


# ---------------------------------------------------------------- 缩放


def target_size(width: int, height: int, resize_max_side: int, max_dimension: int) -> tuple[int, int]:
    """送入 Windows OCR 的图像尺寸：长边不超过 resize_max_side（0 表示不限）与 max_dimension 中较小的一个。"""
    limits = [value for value in (resize_max_side, max_dimension) if value > 0]
    return plan_resize(width, height, min(limits)) if limits else (width, height)


def _area_rows(image: NDArray[np.float32], rows: int) -> NDArray[np.float32]:
    """沿第 0 轴把 n 行面积平均成 rows 行（rows ≤ n）。

    把每一列看成分段常数函数 f，积分 F(x) = Σ_{i<⌊x⌋} f_i + (x − ⌊x⌋)·f_{⌊x⌋}；
    第 k 个输出行是 f 在 [k·s, (k+1)·s) 上的平均值 (F((k+1)·s) − F(k·s)) / s，其中 s = n / rows。
    """
    n = image.shape[0]
    scale = n / rows
    edges = np.arange(rows + 1, dtype=np.float64) * scale
    edges[-1] = n
    index = np.minimum(np.floor(edges).astype(np.int64), n - 1)
    frac = (edges - index).astype(np.float32)
    prefix = np.concatenate([np.zeros((1, *image.shape[1:]), np.float32), np.cumsum(image, axis=0)], axis=0)
    shape = (rows + 1,) + (1,) * (image.ndim - 1)
    integral = prefix[index] + frac.reshape(shape) * image[index]
    return (integral[1:] - integral[:-1]) / np.float32(scale)


def resize_area(image: Pixels, size: tuple[int, int]) -> Pixels:
    """把 height × width × c 的 uint8 图像面积平均缩小到 size = (宽, 高)。尺寸不变时原样返回。

    先缩行再缩列，按条带处理，中间数组最多是一个条带的 float32 副本。
    """
    height, width = image.shape[:2]
    new_width, new_height = size
    if (new_width, new_height) == (width, height):
        return image
    if new_width > width or new_height > height or new_width <= 0 or new_height <= 0:
        raise ValueError(f"只能缩小：{width}×{height} → {new_width}×{new_height}")
    rows = np.empty((new_height, width, *image.shape[2:]), np.float32)
    for start in range(0, width, _RESIZE_STRIP):
        strip = image[:, start : start + _RESIZE_STRIP].astype(np.float32)
        rows[:, start : start + _RESIZE_STRIP] = _area_rows(strip, new_height)
    out = np.empty((new_height, new_width, *image.shape[2:]), np.uint8)
    for start in range(0, new_height, _RESIZE_STRIP):
        strip = np.swapaxes(rows[start : start + _RESIZE_STRIP], 0, 1)
        cols = np.swapaxes(_area_rows(np.ascontiguousarray(strip), new_width), 0, 1)
        out[start : start + _RESIZE_STRIP] = np.clip(np.rint(cols), 0, 255).astype(np.uint8)
    return out


def to_bgra(image: Pixels) -> bytes:
    """BGR（或已经是 BGRA）的 uint8 图像转成紧凑的 BGRA 字节（stride = width × 4，alpha 为 255）。"""
    if image.shape[2] == 4:
        return np.ascontiguousarray(image).tobytes()
    height, width = image.shape[:2]
    out = np.empty((height, width, 4), np.uint8)
    out[:, :, :3] = image
    out[:, :, 3] = 255
    return out.tobytes()


# ---------------------------------------------------------------- 行的拼装


@dataclass(frozen=True, slots=True)
class Word:
    """Windows OCR 的一个词：文字与矩形（x, y, 宽, 高），坐标在送入 OCR 的图像里。"""

    text: str
    x: float
    y: float
    width: float
    height: float

    @property
    def box(self) -> Box:
        return (self.x, self.y, self.x + self.width, self.y + self.height)


def assemble_line(words: Sequence[Word]) -> tuple[str, Box] | None:
    """把一行的词拼成（文字，包围盒）；没有非空词时返回 None。

    包围盒是各词矩形的并集。文字按词的顺序拼接：

    - 两侧都是汉字、假名或全角标点（中文、日文）：直接相连。Windows OCR 把每个汉字当作一个词，
      OcrLine.text 在汉字之间插了空格，所以不用它；
    - 两侧都不是（拉丁文字等用空格分词的文字）：加一个空格；
    - 一侧是汉字、另一侧是拉丁文字：两词的水平间距不小于行高（各词高度的最大值）的 MIXED_GAP_RATIO 倍时
      加空格，否则直接相连。“第三行 mixed”与“第三行mixed”在截图里的区别只在这段间距上。
    """
    kept = [word for word in words if word.text.strip()]
    if not kept:
        return None
    box = union_box(word.box for word in kept)
    assert box is not None
    line_height = max(word.height for word in kept)
    text = kept[0].text.strip()
    previous = kept[0]
    for word in kept[1:]:
        piece = word.text.strip()
        if needs_space(text, piece):
            text += " "
        elif not (is_cjk(text[-1]) and is_cjk(piece[0])):
            gap = word.x - (previous.x + previous.width)
            if gap >= MIXED_GAP_RATIO * line_height:
                text += " "
        text += piece
        previous = word
    return text, box


def build_blocks(lines: Sequence[Sequence[Word]], cmap: CoordinateMap) -> list[OutBlock]:
    """把识别出的行转换成文本模式的输出块（一个覆盖整张截图的 text 块），见模块说明第 2、3 步。"""
    out: list[OutLine] = []
    for words in lines:
        assembled = assemble_line(words)
        if assembled is None:
            continue
        text, box = assembled
        pixels = cmap.to_pixels(box)
        if pixels is not None:
            out.append(OutLine(pixels, text, SCORE))
    if not out:
        return []
    width, height = cmap.original_size
    return [OutBlock(BlockCategory.TEXT, (0, 0, width, height), 0, SCORE, lines=out)]


# ---------------------------------------------------------------- 系统接口


class OcrApi(Protocol):
    """后端用到的 Windows OCR 接口。默认实现是 WinRtOcrApi，测试可以换成假的实现。"""

    @property
    def max_image_dimension(self) -> int: ...

    def available_languages(self) -> list[str]:
        """系统可用的识别语言标签，例如 ["en-US", "zh-Hans-CN"]。"""
        ...

    def create(self, tag: str | None) -> "RecognizerHandle | None":
        """为语言 tag 创建识别器；tag 为 None 时按用户语言列表创建。创建失败返回 None。"""
        ...


class RecognizerHandle(Protocol):
    """一种语言的识别器。"""

    @property
    def language_tag(self) -> str: ...

    def recognize(self, bgra: bytes, width: int, height: int) -> list[list[Word]]:
        """识别一张紧凑的 BGRA 图像，返回每行的词。"""
        ...


class _WinRtRecognizer:
    def __init__(self, engine: "OcrEngine") -> None:
        self._engine = engine

    @property
    def language_tag(self) -> str:
        return self._engine.recognizer_language.language_tag

    def recognize(self, bgra: bytes, width: int, height: int) -> list[list[Word]]:
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap

        bitmap = SoftwareBitmap.create_copy_from_buffer(bgra, BitmapPixelFormat.BGRA8, width, height)
        try:
            engine = self._engine

            async def run() -> list[list[Word]]:
                result = await engine.recognize_async(bitmap)
                lines: list[list[Word]] = []
                for line in result.lines:
                    words: list[Word] = []
                    for word in line.words:
                        rect = word.bounding_rect
                        words.append(Word(word.text, rect.x, rect.y, rect.width, rect.height))
                    lines.append(words)
                return lines

            return asyncio.run(run())
        finally:
            bitmap.close()


class WinRtOcrApi:
    """通过 pywinrt 调用 Windows.Media.Ocr。构造时导入 winrt，失败抛 ImportError。"""

    def __init__(self) -> None:
        from winrt.windows.media.ocr import OcrEngine

        self._engine_type = OcrEngine

    @property
    def max_image_dimension(self) -> int:
        return int(self._engine_type.max_image_dimension)

    def available_languages(self) -> list[str]:
        return [language.language_tag for language in self._engine_type.available_recognizer_languages]

    def create(self, tag: str | None) -> RecognizerHandle | None:
        from winrt.windows.globalization import Language

        if tag is None:
            engine = self._engine_type.try_create_from_user_profile_languages()
        else:
            engine = self._engine_type.try_create_from_language(Language(tag))
        return None if engine is None else _WinRtRecognizer(engine)


def _winrt_version() -> str:
    try:
        return importlib.metadata.version("winrt-runtime")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


# ---------------------------------------------------------------- 后端


class WinOcrBackend:
    """Windows OCR 后端，实现 backend.OcrBackend。api 缺省时在 load 中创建 WinRtOcrApi。"""

    def __init__(self, api: OcrApi | None = None) -> None:
        self._api = api
        self._settings: WinOcrSettings | None = None
        self._recognizer: RecognizerHandle | None = None
        self._available: list[str] = []

    def load(self, settings: Mapping[str, JsonValue], progress: ProgressReporter) -> LoadResult:
        parsed = WinOcrSettings.parse(settings)
        progress("model_load", ENGINE)
        api = self._api
        if api is None:
            api = self._api = WinRtOcrApi()
        available = api.available_languages()
        if not available:
            raise ConfigError(NO_LANGUAGE_MESSAGE)
        recognizer = self._create(api, parsed.language, available)
        self._settings, self._recognizer, self._available = parsed, recognizer, available
        log.info(
            "Windows OCR 已就绪：识别语言 %s（设置为 %s），系统可用 %s，图像边长上限 %d",
            recognizer.language_tag,
            parsed.language,
            ", ".join(available),
            api.max_image_dimension,
        )
        return LoadResult(self._capabilities(recognizer, available), self._versions())

    @staticmethod
    def _create(api: OcrApi, language: str, available: list[str]) -> RecognizerHandle:
        """按设置的语言创建识别器，见模块说明的“识别语言”。"""
        if language.casefold() == AUTO:
            recognizer = api.create(None)
            if recognizer is None:
                log.info("用户语言列表里没有装了 OCR 的语言，改用 %s", available[0])
                recognizer = api.create(available[0])
            if recognizer is None:
                raise ConfigError(f"无法创建 Windows OCR 识别器（系统可用的语言：{', '.join(available)}）")
            return recognizer
        tag = match_language(language, available)
        if tag is None:
            raise ConfigError(
                f"Windows 没有安装识别语言 {language} 的 OCR 功能；"
                f"系统可用的识别语言：{', '.join(available)}。"
                "可以在 Windows 设置里为该语言添加“光学字符识别”功能，或把识别语言改为 auto"
            )
        recognizer = api.create(tag)
        if recognizer is None:
            raise ConfigError(f"无法创建识别语言 {tag} 的 Windows OCR 识别器")
        return recognizer

    @staticmethod
    def _capabilities(recognizer: RecognizerHandle, available: list[str]) -> dict[str, JsonValue]:
        languages: list[JsonValue] = []
        for tag in available:
            code = canonical_language(tag)
            if code not in languages:
                languages.append(code)
        return {
            "mode": "text",
            "engine": ENGINE,
            "language": canonical_language(recognizer.language_tag),
            "languages": languages,
            "features": ["text"],
        }

    @staticmethod
    def _versions() -> dict[str, str]:
        return {"engine": ENGINE, "winrt": _winrt_version(), "windows": platform.version()}

    def recognize(self, image: Pixels, options: Mapping[str, JsonValue]) -> list[JsonValue]:
        settings, recognizer, api = self._settings, self._recognizer, self._api
        if settings is None or recognizer is None or api is None:
            raise RuntimeError("后端尚未加载")
        parsed = WinOcrOptions.parse(options, settings)
        height, width = image.shape[:2]
        target = target_size(width, height, parsed.resize_max_side, api.max_image_dimension)
        # 先缩放再灰度化：灰度化只处理缩小后的像素，与 Paddle 后端的顺序相同。
        prepared = resize_area(image, target)
        if parsed.grayscale:
            prepared = to_grayscale(prepared)
        lines = recognizer.recognize(to_bgra(prepared), *target)
        return blocks_to_json(build_blocks(lines, CoordinateMap((width, height), target)))
