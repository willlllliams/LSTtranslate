"""用本机的两个真实 Argos 模型包（assets/argosmodel）在子进程里运行 CTranslate2 后端。

模型包只读使用，解压到 data/models/nmt/.extracted（已在 .gitignore 中）。模型包不存在时跳过。
中转路线需要第三种语言的模型，这里测不了，由 test_nmt_process.py 用假后端覆盖。

性能测量（test_hot_path_latency）在模型已加载后计时，从发出请求到收到 RequestDone，含进程间通信。
CUDA 一档在子进程无法使用 CUDA（没有 CUDA 设备或缺少 cuBLAS）时跳过；环境变量 LST_TEST_CUDA_DLL_DIR
可以指向一个含 cublas64_12.dll 与 cublasLt64_12.dll 的目录，测试把它作为 cuda_dll_dir 传给子进程。
"""

import os
import re
import statistics
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lst.contracts.errors import ErrorCode
from lst.contracts.worker_protocol import (
    ExitCode,
    JsonValue,
    RequestDone,
    SegmentResult,
    TranslateRequest,
    TranslateSegment,
    WorkerMessage,
)
from tests.workers.harness import REAL_BACKEND, ROOT, WorkerExited, WorkerProcess
from tests.workers.packages import ASSETS, REAL_EN_ZH, REAL_ZH_EN

pytestmark = pytest.mark.skipif(
    not REAL_EN_ZH.is_file() or not REAL_ZH_EN.is_file(),
    reason="本机没有 assets/argosmodel 下的 Argos 模型包",
)

NMT_DATA = ROOT / "data" / "models" / "nmt"
_HAN = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")
_LETTER = re.compile(r"[^\W\d_]")
_BAD = re.compile(r">>|<<|<unk>|</?s>|\u2581|[\ue000-\uf8ff]")  # 特殊标记、▁、私用区字符


def settings(device: str, **extra: JsonValue) -> dict[str, JsonValue]:
    return {
        "model_dir": str(ASSETS),
        "extract_dir": str(NMT_DATA / ".extracted"),
        "cuda_cache_dir": str(NMT_DATA / ".cuda-cache"),
        "device": device,
        **extra,
    }


def request(request_id: str, source: str, target: str, *texts: str) -> TranslateRequest:
    segments = tuple(TranslateSegment(str(i), text) for i, text in enumerate(texts))
    return TranslateRequest(request_id, "s", source, target, segments)


def run(worker: WorkerProcess, req: TranslateRequest, timeout: float = 30.0) -> list[SegmentResult]:
    worker.send(req)
    messages: list[WorkerMessage] = worker.recv_until(
        lambda m: isinstance(m, RequestDone) and m.request_id == req.request_id, timeout
    )
    return [m for m in messages if isinstance(m, SegmentResult)]


@pytest.fixture(scope="module")
def cpu_worker() -> Iterator[WorkerProcess]:
    worker = WorkerProcess(REAL_BACKEND)
    try:
        worker.start()
        worker.handshake(settings("cpu"), timeout=60)
        yield worker
    finally:
        worker.close()


EN_SENTENCES = [
    "Hello, world.",
    "Please restart the application to apply the changes.",
    "Dr. Smith arrived at 3 p.m. on Monday. The meeting was postponed.",
    "Machine translation has improved dramatically over the past decade.\n"
    "Click here to download the latest version.",
    "OK",
    "File not found.",
]
ZH_SENTENCES = [
    "你好，世界。",
    "今天天气很好，我们去公园散步吧。机器翻译是自然语言处理的一个重要分支！",
    "请重新启动应用程序以应用更改。",
    "点击这里下载最新版本。\n他昨天下午三点到了。",
    "确定",
]


def test_ready_lists_both_installed_pairs(cpu_worker: WorkerProcess) -> None:
    # 握手在 fixture 里完成；再配置一次检查能力清单，顺带验证运行中重新加载。
    ready = cpu_worker.configure(settings("cpu"), timeout=60)
    assert ready.capabilities["pairs"] == [["en", "zh-Hans"], ["zh-Hans", "en"]]
    assert ready.capabilities["pivot"] == "en"
    assert ready.versions["ctranslate2"].startswith("4.")
    assert ready.versions["device"] == "cpu" and ready.versions["compute_type"] == "int8"


def test_english_to_chinese(cpu_worker: WorkerProcess) -> None:
    results = run(cpu_worker, request("en-zh", "en", "zh-Hans", *EN_SENTENCES))
    assert [r.segment_id for r in results] == [str(i) for i in range(len(EN_SENTENCES))]
    for source, result in zip(EN_SENTENCES, results, strict=True):
        assert result.error is None and result.text, result
        assert _HAN.search(result.text), f"译文没有汉字：{result.text!r}"
        assert not _BAD.search(result.text), f"译文含特殊标记或乱码：{result.text!r}"
        assert result.text.count("\n") == source.count("\n")


def test_real_model_loads_from_unicode_and_space_paths(tmp_path: Path) -> None:
    worker = WorkerProcess(REAL_BACKEND)
    try:
        worker.start()
        worker.handshake(
            settings("cpu", extract_dir=str(tmp_path / "中文模型 目录"), intra_threads=2), timeout=60
        )
        (result,) = run(worker, request("unicode-model", "en", "zh-Hans", "Hello, world."))
        assert result.error is None and result.text, result
        assert _HAN.search(result.text), result
    finally:
        worker.close()


def test_chinese_to_english(cpu_worker: WorkerProcess) -> None:
    results = run(cpu_worker, request("zh-en", "zh-Hans", "en", *ZH_SENTENCES))
    for source, result in zip(ZH_SENTENCES, results, strict=True):
        assert result.error is None and result.text, result
        letters = _LETTER.findall(result.text)
        latin = _LATIN.findall(result.text)
        assert letters and len(latin) / len(letters) > 0.9, f"译文不是以拉丁字母为主：{result.text!r}"
        assert not _BAD.search(result.text), f"译文含特殊标记或乱码：{result.text!r}"
        assert result.text.count("\n") == source.count("\n")


def test_known_translations(cpu_worker: WorkerProcess) -> None:
    results = run(
        cpu_worker, request("known", "zh-Hans", "en", "机器翻译在过去十年中取得了巨大进步。", "设置")
    )
    assert results[0].text == "Machine translation has made tremendous progress over the past decade."
    assert results[1].text == "Settings"


def test_unsupported_pair_reports_model_missing(cpu_worker: WorkerProcess) -> None:
    results = run(cpu_worker, request("ja", "ja", "en", "こんにちは"))
    assert results[0].code == ErrorCode.MODEL_MISSING


# ---------------------------------------------------------------- 性能

# 约 200 个汉字的中文段落。
ZH_PARAGRAPH = (
    "随着深度学习的发展，机器翻译的质量在过去十年里有了明显的提高。神经网络模型能够同时考虑整句话的上下文，"
    "所以译文比早期的统计方法更加通顺。不过，模型的大小和推理速度仍然是实际应用中的主要限制。"
    "在个人电脑上运行翻译模型时，用户希望在截图之后很快看到结果，因此程序需要在质量和速度之间做出取舍。"
    "本项目把句子切分、模型加载和推理都放在独立的进程中完成，界面线程只负责显示结果。"
    "模型文件只在第一次使用时解压，之后直接从磁盘加载。"
)
# 约 200 个英文单词的段落。
EN_PARAGRAPH = (
    "Screen translation tools help people read software, games and documents written in "
    "languages they do not know well. The user selects a region of the screen, the program "
    "recognises the text in that region, and a translation appears on top of the original "
    "content a moment later. Speed matters a great deal in this workflow, because the user is "
    "waiting with the selection still in mind. A local translation model avoids the network "
    "round trip entirely, which also keeps the text on the user's own computer. Small "
    "transformer models trained on public parallel corpora are good enough for many everyday "
    "sentences, although they still make mistakes with rare names, idioms and very long "
    "sentences. To keep the quality acceptable, the text is split into sentences before "
    "translation, and all sentences of one paragraph are translated together as a single "
    "batch. The results are then joined again in the original order. When the model has "
    "already been loaded, the remaining cost is mostly the decoding itself, which depends on "
    "the number of output tokens and on the beam size. This test measures that warm path for a "
    "paragraph of roughly two hundred words, so that the numbers can be compared with the "
    "latency targets in the requirements."
)


def measure(worker: WorkerProcess, source: str, target: str, text: str, runs: int = 3) -> list[float]:
    run(
        worker, request("warmup", source, target, text), timeout=50
    )  # 首次使用时解压、加载模型（CUDA 还有即时编译）
    timings: list[float] = []
    for i in range(runs):
        started = time.perf_counter()
        results = run(worker, request(f"bench-{i}", source, target, text))
        timings.append(time.perf_counter() - started)
        assert results[0].text and results[0].error is None
    return timings


def cuda_settings() -> dict[str, JsonValue]:
    dll_dir = os.environ.get("LST_TEST_CUDA_DLL_DIR")
    return {"cuda_dll_dir": dll_dir} if dll_dir else {}


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_hot_path_latency(device: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert 200 <= len(ZH_PARAGRAPH) <= 220 and 190 <= len(EN_PARAGRAPH.split()) <= 220
    worker = WorkerProcess(REAL_BACKEND)
    try:
        worker.start()
        try:
            extra = cuda_settings() if device == "cuda" else {}
            ready = worker.handshake(settings(device, **extra), timeout=60)
        except WorkerExited:
            code = worker.wait_exit()
            if device == "cuda" and code in (ExitCode.DEPENDENCY_ERROR, ExitCode.CONFIG_ERROR):
                pytest.skip(f"子进程无法使用 CUDA（退出码 {code}）")
            raise
        zh_en = measure(worker, "zh-Hans", "en", ZH_PARAGRAPH)
        en_zh = measure(worker, "en", "zh-Hans", EN_PARAGRAPH)
    finally:
        worker.close()
    compute_type = ready.versions["compute_type"]
    # 直接写到终端，不被 pytest 捕获。控制台可能不是 UTF-8 编码，所以只输出 ASCII。
    with capsys.disabled():
        for name, timings in (("zh->en 200 chars", zh_en), ("en->zh 200 words", en_zh)):
            median, best = statistics.median(timings) * 1000, min(timings) * 1000
            print(
                f"\n[nmt warm path] {device}/{compute_type} {name}: "
                f"median {median:.0f} ms, min {best:.0f} ms ({len(timings)} runs)"
            )
    assert max(zh_en) < 3.0 and max(en_zh) < 3.0
