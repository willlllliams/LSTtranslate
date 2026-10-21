"""推理进程测试用的假 NMT 后端，通过 `--backend tests.workers.fake_backend:FakeBackend` 装入子进程。

行为全部由 Configure 的设置控制：

| 键 | 含义 |
|---|---|
| pairs | 直连语言对，默认 [["en", "zh-Hans"], ["zh-Hans", "en"]] |
| pivot | 中转语言，默认 None |
| segment_delay | 每段的处理耗时（秒） |
| load_delay | 加载耗时（秒），期间每 0.05 秒发一条 Progress |
| load_error | 加载时出错："import"、"oserror"（模拟 DLL 加载失败）、"config"、"crash" |
| fail_on | 段文字包含它时，该段的推理抛出异常 |
| exit_on | 段文字包含它时，进程用 os._exit(3) 直接退出 |
| segfault_on | 段文字包含它时，制造真正的段错误（访问违例） |
| hang_on | 段文字包含它时，卡住不返回 |
| tag | 任意字符串，计入 fingerprint，用来区分两次配置 |

译文格式为 `[源>目标]原句`，逐句加前缀，可以直接看出切句、中转和拼回的结果。
"""

import ctypes
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import cast

from lst.contracts.worker_protocol import JsonValue, TranslateSegment
from lst.workers._runtime import ConfigError, LoadResult, ProgressReporter
from lst.workers.nmt_worker.backend import Pair, SegmentOutcome, make_capabilities, stable_fingerprint
from lst.workers.nmt_worker.routing import translate_segments

DEFAULT_PAIRS: list[JsonValue] = [["en", "zh-Hans"], ["zh-Hans", "en"]]


def _text(settings: Mapping[str, JsonValue], key: str) -> str | None:
    value = settings.get(key)
    return value if isinstance(value, str) and value else None


def _number(settings: Mapping[str, JsonValue], key: str) -> float:
    value = settings.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def segfault() -> None:
    """制造一次真正的访问违例。

    在 Windows 上，ctypes 调用外部函数时包着结构化异常处理，`ctypes.string_at(0)` 这类越界访问只会变成
    Python 的 OSError，进程不会崩溃。所以这里新建一个从地址 0 开始执行的原生线程：它没有任何异常处理，
    访问违例直接终止整个进程，与推理库里的段错误一样。当前线程等它，faulthandler 打印的堆栈里就有这一帧。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateThread.restype = ctypes.c_void_p
    kernel32.CreateThread.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    handle = kernel32.CreateThread(None, 0, None, None, 0, None)
    kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)


class FakeBackend:
    """假后端，实现 lst.workers.nmt_worker.backend.NmtBackend。"""

    def __init__(self) -> None:
        self._settings: Mapping[str, JsonValue] = {}
        self._pairs: set[Pair] = set()
        self._pivot: str | None = None
        self._loads = 0

    def load(self, settings: Mapping[str, JsonValue], progress: ProgressReporter) -> LoadResult:
        match settings.get("load_error"):
            case "import":
                raise ImportError("DLL load failed while importing _fake_ext: 找不到指定的模块。")
            case "oserror":
                raise OSError("[WinError 126] 找不到指定的模块。")
            case "config":
                raise ConfigError("假后端：配置无效")
            case "crash":
                raise RuntimeError("假后端：加载时出现未预期的异常")
            case _:
                pass
        steps = round(_number(settings, "load_delay") / 0.05)
        for step in range(steps):
            time.sleep(0.05)
            progress("model_load", "fake", step + 1, steps)
        raw_pairs = cast(list[list[str]], settings.get("pairs", DEFAULT_PAIRS))
        self._pairs = {(source, target) for source, target in raw_pairs}
        self._pivot = _text(settings, "pivot")
        self._settings = settings
        self._loads += 1
        pairs: list[JsonValue] = [[s, t] for s, t in sorted(self._pairs)]
        fingerprint = stable_fingerprint({"tag": settings.get("tag", ""), "pairs": pairs})
        capabilities = make_capabilities(self._pairs, self._pivot, fingerprint)
        capabilities["loads"] = self._loads
        return LoadResult(capabilities, {"fake": "1.0"})

    def _translate_sentences(self, pair: Pair, sentences: list[str]) -> list[str]:
        settings = self._settings
        time.sleep(_number(settings, "segment_delay"))
        joined = "\n".join(sentences)
        if (marker := _text(settings, "exit_on")) and marker in joined:
            os._exit(3)
        if (marker := _text(settings, "segfault_on")) and marker in joined:
            segfault()
        if (marker := _text(settings, "hang_on")) and marker in joined:
            while True:
                time.sleep(1)
        if (marker := _text(settings, "fail_on")) and marker in joined:
            raise RuntimeError("假后端：这一段推理失败")
        return [f"[{pair[0]}>{pair[1]}]{sentence}" for sentence in sentences]

    def translate(
        self, source: str, target: str, segments: Sequence[TranslateSegment]
    ) -> Iterable[SegmentOutcome]:
        return translate_segments(
            source,
            target,
            segments,
            pairs=self._pairs,
            pivot=self._pivot,
            translate=self._translate_sentences,
        )


class SkippingBackend(FakeBackend):
    """不守规矩的后端：漏掉最后一段，并多产出一个未知段号。用来检查处理器补发缺失的段。"""

    def translate(
        self, source: str, target: str, segments: Sequence[TranslateSegment]
    ) -> Iterable[SegmentOutcome]:
        outcomes = list(super().translate(source, target, segments))
        return [*outcomes[:-1], SegmentOutcome.ok("no-such-segment", "x")]


class RaisingBackend(FakeBackend):
    """产出第一段后抛出异常的后端。处理器应把剩下的段记为 engine.internal。"""

    def translate(
        self, source: str, target: str, segments: Sequence[TranslateSegment]
    ) -> Iterable[SegmentOutcome]:
        outcomes = iter(super().translate(source, target, segments))
        yield next(outcomes)
        raise RuntimeError("假后端：迭代中途出错")
