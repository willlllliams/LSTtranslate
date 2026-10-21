"""CTranslate2 后端：直接加载 Argos 模型包，不安装 argostranslate（架构文档 2.5、8.3 节）。

Configure 的设置项：

| 键 | 默认值 | 含义 |
|---|---|---|
| model_dir | 必填 | 存放 .argosmodel 的目录 |
| extract_dir | `<model_dir>/.extracted` | 模型包的解压目录 |
| cuda_cache_dir | 见下 | CUDA 即时编译缓存目录 |
| device | "auto" | "auto"、"cpu" 或 "cuda"；auto 在有 CUDA 设备且 cuBLAS 可加载时用 cuda |
| compute_type | "auto" | CTranslate2 的计算类型；auto 在 CPU 上取 int8，在 GPU 上取 float16 |
| beam_size | 2 | 集束搜索宽度 |
| intra_threads | 0 | 每个模型的计算线程数，0 表示由 CTranslate2 决定 |
| max_loaded_models | 4 | 同时常驻的模型数，至少为 2（中转路线同时用到两个模型） |
| warmup | true | 设备是 cuda 时，在加载阶段用第一个模型预热一次 |

cuda_cache_dir 不设时沿用环境变量 CUDA_CACHE_PATH，环境变量也没有时为 `<model_dir>/.cuda-cache`。

预热的原因：在 RTX 5080 上，第一次在 CUDA 上翻译要先由驱动即时编译内核，本机实测约 7 秒，超过监管器
每批 5 秒的处理时限（架构文档 8.2 节第 3 条），第一个真实请求会被误判为卡死。所以实际设备是 cuda 时，
load 在返回能力清单之前加载语言对排序后的第一个模型，翻译一句固定的短句，把编译放在加载阶段完成
（监管器等待 Ready 不计处理时限）。预热加载的模型留在 LRU 缓存里，占用的是正常的常驻名额。
预热失败只记一条警告，照常就绪。CPU 冷启动只是解压和加载，约 0.5 秒，不预热。

调用 CTranslate2 的细节。第 1、2、4 条与 argostranslate 的做法相同，第 3、5、6 条是本项目加的；
每一条都用本机的 en→zh、zh→en 两个模型验证过：

1. 用包里的 sentencepiece.model 把句子切成子词（out_type=str），一段的全部句子作为一个批次
   调用 translate_batch。
2. 多目标语言的 OPUS-MT 模型（例如 en→zh）要在源序列开头放目标语言标记 `>>cmn_Hans<<`。模型的
   config.json 里 add_source_bos=true、bos_token=">>cmn_Hans<<"，CTranslate2 加载时读取这两项，
   自动把标记加在源序列开头。这里不再手动添加：手动加会变成两个标记，译文随之改变。
3. 解码时用 suppress_sequences 禁止生成语言标记和私用区字符。两个模型的目标词表里各有 512 个
   私用区（U+E000–U+F8FF）词元，beam_size 为 1、2 时，短输入（例如 "OK"）会译出这类乱码。
4. 目标词表是源、目标共用的词表，包里的 sentencepiece.model 只覆盖源语言，用它解码会留下 ▁。
   所以把词元直接拼接，再把 SentencePiece 的词首标记 ▁ 换成空格。
5. 中文译文再整理一次：去掉汉字之间和中文标点两侧多余的空格，紧跟汉字的英文标点换成中文标点。
   en→zh 模型输出的中文常用英文逗号和句点，词首标记还会在句号前留下空格（“文件未找到 。”）。
6. 最大解码长度按本批最长源句设上限（decoding_limit），模型陷入重复时不会一直生成到默认的 256 个词元。
"""

import contextlib
import ctypes
import importlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Self, cast

from lst.contracts.worker_protocol import JsonValue, TranslateSegment
from lst.workers._runtime import ConfigError, LoadResult, ProgressReporter
from lst.workers.nmt_worker.argos import (
    ArgosPackage,
    describe,
    extract_package,
    prune_extracted,
    scan_packages,
)
from lst.workers.nmt_worker.backend import Pair, SegmentOutcome, make_capabilities, stable_fingerprint
from lst.workers.nmt_worker.languages import canonical_code
from lst.workers.nmt_worker.lru import LruCache
from lst.workers.nmt_worker.routing import ModelUnavailable, translate_segments

log = logging.getLogger(__name__)

# 解码参数或译文后处理变化时加一，fingerprint 随之变化，旧的缓存译文不再命中。
ALGORITHM_VERSION = 1
PIVOT = "en"

type Device = Literal["cpu", "cuda"]

_COMPUTE_TYPES = frozenset(
    {
        "auto",
        "default",
        "int8",
        "int8_float32",
        "int8_float16",
        "int8_bfloat16",
        "int16",
        "float16",
        "bfloat16",
        "float32",
    }
)
_KNOWN_KEYS = frozenset(
    {"model_dir", "extract_dir", "cuda_cache_dir", "device", "compute_type", "beam_size", "intra_threads",
     "max_loaded_models", "warmup"}
)  # fmt: skip
_CUBLAS_DLLS = ("cublasLt64_12.dll", "cublas64_12.dll")
# 预热用的固定短句。预热只为触发内核编译，译文不用。
WARMUP_SENTENCE = "Hello."


# ---------------------------------------------------------------- 设置


def _get_str(raw: Mapping[str, JsonValue], key: str, default: str | None) -> str | None:
    value = raw.get(key, default)
    if value is None or isinstance(value, str):
        return value
    raise ConfigError(f"{key} 应为字符串，实际为 {type(value).__name__}")


def _get_int(raw: Mapping[str, JsonValue], key: str, default: int, minimum: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 应为整数，实际为 {type(value).__name__}")
    if value < minimum:
        raise ConfigError(f"{key} 至少为 {minimum}，实际为 {value}")
    return value


def _get_bool(raw: Mapping[str, JsonValue], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} 应为布尔值，实际为 {type(value).__name__}")
    return value


@dataclass(frozen=True, slots=True)
class Ct2Settings:
    """CTranslate2 后端的设置，见模块说明的表格。"""

    model_dir: Path
    extract_dir: Path
    cuda_cache_dir: Path | None
    device: Literal["auto", "cpu", "cuda"]
    compute_type: str
    beam_size: int
    intra_threads: int
    max_loaded_models: int
    warmup: bool

    @classmethod
    def parse(cls, raw: Mapping[str, JsonValue]) -> Self:
        """校验 Configure 的设置；无效时抛 ConfigError。未知的键记警告后忽略。"""
        unknown = sorted(set(raw) - _KNOWN_KEYS)
        if unknown:
            log.warning("忽略未知的设置项：%s", ", ".join(unknown))
        model_dir = _get_str(raw, "model_dir", None)
        if not model_dir:
            raise ConfigError("缺少 model_dir")
        root = Path(model_dir).absolute()
        extract_dir = _get_str(raw, "extract_dir", None)
        cuda_cache_dir = _get_str(raw, "cuda_cache_dir", None)
        device = _get_str(raw, "device", "auto")
        if device not in ("auto", "cpu", "cuda"):
            raise ConfigError(f"device 应为 auto、cpu 或 cuda，实际为 {device!r}")
        compute_type = _get_str(raw, "compute_type", "auto") or "auto"
        if compute_type not in _COMPUTE_TYPES:
            raise ConfigError(f"未知的 compute_type：{compute_type!r}")
        return cls(
            model_dir=root,
            extract_dir=Path(extract_dir).absolute() if extract_dir else root / ".extracted",
            cuda_cache_dir=Path(cuda_cache_dir).absolute() if cuda_cache_dir else None,
            device=device,
            compute_type=compute_type,
            beam_size=_get_int(raw, "beam_size", 2, 1),
            intra_threads=_get_int(raw, "intra_threads", 0, 0),
            max_loaded_models=_get_int(raw, "max_loaded_models", 4, 2),
            warmup=_get_bool(raw, "warmup", True),
        )


# ---------------------------------------------------------------- 推理库的类型（只列出用到的部分）


class _TranslationResult(Protocol):
    @property
    def hypotheses(self) -> list[list[str]]: ...


class _Translator(Protocol):
    def translate_batch(
        self,
        source: list[list[str]],
        *,
        beam_size: int,
        max_decoding_length: int,
        replace_unknowns: bool,
        suppress_sequences: list[list[str]] | None,
    ) -> list[_TranslationResult]: ...


class _Ct2Module(Protocol):
    __version__: str

    def Translator(
        self, model_path: str, *, device: str, compute_type: str, inter_threads: int, intra_threads: int
    ) -> _Translator: ...

    def get_cuda_device_count(self) -> int: ...

    def get_supported_compute_types(self, device: str) -> set[str]: ...


class _SpProcessor(Protocol):
    def encode(self, input: list[str], *, out_type: type[str]) -> list[list[str]]: ...


class _SpModule(Protocol):
    __version__: str

    def SentencePieceProcessor(self, *, model_file: str) -> _SpProcessor: ...


def _add_nvidia_dll_dirs() -> None:
    """把 pip 安装的 nvidia-cublas-cu12 等包的 DLL 目录加入搜索路径（PATH 和 add_dll_directory 都加）。"""
    for entry in sys.path:
        nvidia = Path(entry, "nvidia")
        if not nvidia.is_dir():
            continue
        for bin_dir in sorted(nvidia.glob("*/bin")):
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
            with contextlib.suppress(OSError):
                os.add_dll_directory(str(bin_dir))


def _load_cublas() -> None:
    """预先加载 cuBLAS，失败时抛 OSError。

    CTranslate2 的 CUDA 路径在第一次翻译时才加载 cuBLAS。缺少 cuBLAS 时，float16 翻译报错，
    int8 翻译则会一直卡住（本机实测超过 400 秒没有返回），所以在选择设备时就要确认它能加载。
    这里按标准搜索顺序（含 PATH）加载，之后 CTranslate2 按同名取到的就是已加载的模块。
    """
    if sys.platform != "win32":
        return
    for name in _CUBLAS_DLLS:
        ctypes.WinDLL(name, winmode=0)


# ---------------------------------------------------------------- 译文清理

_SPECIAL_TOKEN = re.compile(r"^(?:<s>|</s>|<pad>|<unk>|>>[^<>\s]+<<)$")
_PRIVATE_USE = re.compile(r"[\ue000-\uf8ff]")
_HAN = r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"  # 假名、汉字
_CJK_PUNCT = r"，。！？；：、“”‘’（）《》【】「」『』…—"
_SPACE_BETWEEN_HAN = re.compile(rf"(?<=[{_HAN}])\s+(?=[{_HAN}])")
_SPACE_AROUND_CJK_PUNCT = re.compile(rf"\s+(?=[{_CJK_PUNCT}])|(?<=[{_CJK_PUNCT}])\s+")
_ASCII_TO_FULLWIDTH = {",": "，", "?": "？", "!": "！", ";": "；", ":": "："}
_ASCII_AFTER_HAN = re.compile(rf"(?<=[{_HAN}])([,?!;:])(?!\d)")
_PERIOD_AFTER_HAN = re.compile(rf"(?<=[{_HAN}])\.(?=$|\s|[{_HAN}])")


def is_suppressed_token(token: str) -> bool:
    """解码时禁止生成的词元：语言标记（>>xx<<）和含私用区字符的词元。"""
    return (token.startswith(">>") and token.endswith("<<")) or _PRIVATE_USE.search(token) is not None


def tidy_chinese(text: str) -> str:
    """整理中文译文：去掉汉字之间、中文标点两侧的空格；紧跟汉字的英文标点换成中文标点。"""
    text = _SPACE_BETWEEN_HAN.sub("", text)
    text = _ASCII_AFTER_HAN.sub(lambda m: _ASCII_TO_FULLWIDTH[m.group(1)], text)
    text = _PERIOD_AFTER_HAN.sub("。", text)
    return _SPACE_AROUND_CJK_PUNCT.sub("", text)


def detokenize(tokens: Iterable[str], target: str) -> str:
    """把模型输出的词元还原成文字：去掉特殊标记，▁ 换成空格，去掉私用区字符，中文再整理一次。"""
    text = "".join(token for token in tokens if not _SPECIAL_TOKEN.match(token)).replace("▁", " ")
    text = " ".join(_PRIVATE_USE.sub("", text).split())
    if canonical_code(target) in ("zh-Hans", "zh-Hant"):
        text = tidy_chinese(text)
    return text


def _read_target_vocabulary(model_path: Path) -> list[str]:
    """读取 CTranslate2 模型目录里的目标词表（共享词表或目标词表，JSON 或文本格式）。"""
    for name in ("target_vocabulary", "shared_vocabulary", "vocabulary"):
        json_path = model_path / f"{name}.json"
        if json_path.is_file():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(token) for token in cast(list[object], data)]
        text_path = model_path / f"{name}.txt"
        if text_path.is_file():
            return text_path.read_text(encoding="utf-8").splitlines()
    return []


def decoding_limit(longest_source: int) -> int:
    """最大解码长度。默认的 256 太宽松：模型陷入重复时会一直生成到上限，按源句长度给上限可以限住耗时。"""
    return 3 * longest_source + 20


# ---------------------------------------------------------------- 后端


@dataclass(slots=True)
class _LoadedModel:
    translator: _Translator
    tokenizer: _SpProcessor
    suppress: list[list[str]]


class Ct2Backend:
    """CTranslate2 后端，实现 backend.NmtBackend。"""

    def __init__(self) -> None:
        self._ct2: _Ct2Module | None = None
        self._spm: _SpModule | None = None
        self._settings: Ct2Settings | None = None
        self._device: Device = "cpu"
        self._compute_type = "float32"
        self._packages: dict[Pair, ArgosPackage] = {}
        self._models: LruCache[Pair, _LoadedModel] | None = None
        self._broken: dict[Pair, str] = {}

    # ---- 加载

    def load(self, settings: Mapping[str, JsonValue], progress: ProgressReporter) -> LoadResult:
        parsed = Ct2Settings.parse(settings)
        self._prepare_cuda_cache(parsed)
        ct2, spm = self._ct2, self._spm = self._import_runtime()
        device = self._resolve_device(ct2, parsed.device)
        compute_type = self._resolve_compute_type(ct2, device, parsed.compute_type)

        if self._models is not None:
            self._models.clear()
        self._broken.clear()
        self._settings = parsed
        self._device = device
        self._compute_type = compute_type
        self._packages = scan_packages(parsed.model_dir)
        prune_extracted(self._packages.values(), parsed.extract_dir)
        self._models = LruCache(parsed.max_loaded_models, self._load_model, self._unload_model)
        progress("scan", str(parsed.model_dir), len(self._packages), len(self._packages))
        log.info(
            "CTranslate2 后端就绪：设备 %s，计算类型 %s，%d 个模型包",
            device,
            compute_type,
            len(self._packages),
        )
        if device == "cuda" and parsed.warmup:
            self._warm_up(progress)

        fingerprint = stable_fingerprint(
            {
                "algorithm": ALGORITHM_VERSION,
                "beam_size": parsed.beam_size,
                "device": device,
                "compute_type": compute_type,
                "models": cast(JsonValue, describe(self._packages.values())),
            }
        )
        versions = {
            "ctranslate2": ct2.__version__,
            "sentencepiece": spm.__version__,
            "device": device,
            "compute_type": compute_type,
        }
        return LoadResult(make_capabilities(self._packages, PIVOT, fingerprint), versions)

    def _warm_up(self, progress: ProgressReporter) -> None:
        """用排序后的第一个模型翻译一句固定短句，把 CUDA 内核的即时编译放在加载阶段完成。

        模型经 LRU 缓存加载，预热之后留在缓存里。任何异常只记警告：预热失败不影响就绪，
        问题留到真实请求时按段报告。
        """
        if not self._packages:
            return
        pair = min(self._packages)
        progress("warmup", f"{pair[0]}→{pair[1]}")
        started = time.perf_counter()
        try:
            self.translate_sentences(pair, [WARMUP_SENTENCE])
        except Exception as exc:
            log.warning("预热 %s→%s 失败，照常就绪：%s: %s", pair[0], pair[1], type(exc).__name__, exc)
            return
        log.info("预热 %s→%s 完成，耗时 %.2f 秒", pair[0], pair[1], time.perf_counter() - started)

    def _prepare_cuda_cache(self, settings: Ct2Settings) -> None:
        """设置 CUDA 即时编译缓存的位置，必须在 CUDA 初始化之前。

        在 RTX 5080 上，第一次在 CUDA 上翻译要先由驱动即时编译内核，本机实测约 7 秒，之后命中缓存只要
        0.1 秒左右。驱动默认把编译结果缓存在 %APPDATA%\\NVIDIA\\ComputeCache；进程以 Low 完整性运行时
        写不进这个目录（架构文档 2.9.5 节），每次启动都要重新编译，所以默认改到模型目录下。
        """
        if settings.cuda_cache_dir is not None:
            os.environ["CUDA_CACHE_PATH"] = str(settings.cuda_cache_dir)
        else:
            os.environ.setdefault("CUDA_CACHE_PATH", str(settings.model_dir / ".cuda-cache"))

    def _import_runtime(self) -> tuple[_Ct2Module, _SpModule]:
        """导入推理库。ImportError、OSError（例如 DLL 加载失败）原样抛出，进程以 DEPENDENCY_ERROR 退出。"""
        if self._ct2 is None or self._spm is None:
            _add_nvidia_dll_dirs()
            self._ct2 = cast(_Ct2Module, importlib.import_module("ctranslate2"))
            self._spm = cast(_SpModule, importlib.import_module("sentencepiece"))
        return self._ct2, self._spm

    @staticmethod
    def _resolve_device(ct2: _Ct2Module, requested: Literal["auto", "cpu", "cuda"]) -> Device:
        if requested == "cpu":
            return "cpu"
        count = ct2.get_cuda_device_count()
        if requested == "cuda":
            if count == 0:
                raise ConfigError("device 设为 cuda，但没有可用的 CUDA 设备")
            _load_cublas()  # 失败抛 OSError，进程以 DEPENDENCY_ERROR 退出
            return "cuda"
        if count == 0:
            return "cpu"
        try:
            _load_cublas()
        except OSError as exc:
            log.warning("有 CUDA 设备，但 cuBLAS 无法加载（%s），改用 CPU", exc)
            return "cpu"
        return "cuda"

    @staticmethod
    def _resolve_compute_type(ct2: _Ct2Module, device: Device, requested: str) -> str:
        supported = ct2.get_supported_compute_types(device)
        if requested == "auto":
            order = (
                ("float16", "bfloat16", "float32")
                if device == "cuda"
                else ("int8", "int8_float32", "float32")
            )
            return next((t for t in order if t in supported), "default")
        if requested == "default" or requested in supported:
            return requested
        raise ConfigError(f"{device} 不支持计算类型 {requested}，可用的有 {', '.join(sorted(supported))}")

    # ---- 模型

    def _load_model(self, pair: Pair) -> _LoadedModel:
        settings, ct2, spm = self._settings, self._ct2, self._spm
        assert settings is not None and ct2 is not None and spm is not None
        package = self._packages[pair]
        try:
            root = extract_package(package, settings.extract_dir)
            translator = ct2.Translator(
                str(root / "model"),
                device=self._device,
                compute_type=self._compute_type,
                inter_threads=1,
                intra_threads=settings.intra_threads,
            )
            tokenizer = spm.SentencePieceProcessor(model_file=str(root / "sentencepiece.model"))
            vocabulary = _read_target_vocabulary(root / "model")
        except Exception as exc:
            raise ModelUnavailable(f"{package.path.name} 无法加载：{type(exc).__name__}: {exc}") from exc
        suppress = [[token] for token in vocabulary if is_suppressed_token(token)]
        log.info("已加载模型 %s→%s（%s）", pair[0], pair[1], package.path.name)
        return _LoadedModel(translator, tokenizer, suppress)

    @staticmethod
    def _unload_model(pair: Pair, model: _LoadedModel) -> None:
        log.info("卸载模型 %s→%s", pair[0], pair[1])

    def loaded_pairs(self) -> list[Pair]:
        """当前常驻的模型，从最近最少使用到最近使用排列。"""
        return self._models.keys() if self._models is not None else []

    def _model(self, pair: Pair) -> _LoadedModel:
        assert self._models is not None
        if pair in self._broken:
            raise ModelUnavailable(self._broken[pair])
        try:
            return self._models.get(pair)
        except ModelUnavailable as exc:
            # 同一个包在本次配置内不再重试，避免每段都解压、加载一遍。
            self._broken[pair] = str(exc)
            raise

    # ---- 翻译

    def translate_sentences(self, pair: Pair, sentences: list[str]) -> list[str]:
        """用 pair 的直连模型翻译一批句子。"""
        settings = self._settings
        assert settings is not None
        model = self._model(pair)
        tokens = model.tokenizer.encode(sentences, out_type=str)
        results = model.translator.translate_batch(
            tokens,
            beam_size=settings.beam_size,
            max_decoding_length=decoding_limit(max(len(t) for t in tokens)),
            replace_unknowns=True,
            suppress_sequences=model.suppress or None,
        )
        return [detokenize(result.hypotheses[0], pair[1]) for result in results]

    def translate(
        self, source: str, target: str, segments: Sequence[TranslateSegment]
    ) -> Iterable[SegmentOutcome]:
        return translate_segments(
            source, target, segments, pairs=self._packages, pivot=PIVOT, translate=self.translate_sentences
        )
