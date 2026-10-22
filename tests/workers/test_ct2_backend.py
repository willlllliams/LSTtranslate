"""CTranslate2 后端中不需要加载模型的部分：设置校验、设备与计算类型选择、译文清理、能力清单与 fingerprint。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from lst.contracts.worker_protocol import PROTOCOL_VERSION, Configure, ExitCode, Hello, JsonValue
from lst.workers._runtime import ConfigError
from lst.workers.nmt_worker import ct2_backend
from lst.workers.nmt_worker.ct2_backend import (
    Ct2Backend,
    Ct2Settings,
    decoding_limit,
    detokenize,
    is_suppressed_token,
    tidy_chinese,
)
from tests.workers.harness import REAL_BACKEND, ROOT, WorkerProcess
from tests.workers.packages import make_package

# ---------------------------------------------------------------- 设置


def test_settings_defaults(tmp_path: Path) -> None:
    settings = Ct2Settings.parse({"model_dir": str(tmp_path)})
    assert settings.model_dir == tmp_path
    assert settings.extract_dir == tmp_path / ".extracted"
    assert settings.cuda_cache_dir is None
    assert (settings.device, settings.compute_type) == ("auto", "auto")
    assert (settings.beam_size, settings.intra_threads, settings.max_loaded_models) == (2, 0, 4)
    assert settings.warmup is True


def test_relative_paths_are_resolved_against_working_directory() -> None:
    settings = Ct2Settings.parse({"model_dir": "data/models/nmt", "extract_dir": "data/x"})
    assert settings.model_dir == Path.cwd() / "data" / "models" / "nmt"
    assert settings.extract_dir == Path.cwd() / "data" / "x"


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"model_dir": ""},
        {"model_dir": 3},
        {"model_dir": "m", "device": "gpu"},
        {"model_dir": "m", "compute_type": "int4"},
        {"model_dir": "m", "beam_size": 0},
        {"model_dir": "m", "beam_size": True},
        {"model_dir": "m", "beam_size": 2.5},
        {"model_dir": "m", "intra_threads": -1},
        {"model_dir": "m", "max_loaded_models": 1},
        {"model_dir": "m", "warmup": "yes"},
        {"model_dir": "m", "warmup": 1},
    ],
)
def test_invalid_settings_raise_config_error(raw: dict[str, JsonValue]) -> None:
    with pytest.raises(ConfigError):
        Ct2Settings.parse(raw)


def test_unknown_settings_are_ignored_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    Ct2Settings.parse({"model_dir": "m", "colour": "blue"})
    assert "colour" in caplog.text


# ---------------------------------------------------------------- 设备与计算类型


class StubCt2:
    """只实现设备查询的 ctranslate2 替身。"""

    __version__ = "0"

    def __init__(self, cuda_devices: int, cpu: set[str], cuda: set[str]) -> None:
        self.cuda_devices = cuda_devices
        self.supported = {"cpu": cpu, "cuda": cuda}

    def get_cuda_device_count(self) -> int:
        return self.cuda_devices

    def get_supported_compute_types(self, device: str) -> set[str]:
        return self.supported[device]


def cublas_loaded(dirs: object) -> None:
    """替换 _load_cublas：假装 cuBLAS 已经加载。"""
    del dirs


CPU_TYPES = {"int8", "int8_float32", "int16", "float32"}
CUDA_TYPES = {"int8", "int8_float32", "int8_float16", "float16", "bfloat16", "float32"}


def resolve(stub: StubCt2, device: str, compute_type: str = "auto") -> tuple[str, str]:
    backend: Any = Ct2Backend
    chosen = backend._resolve_device(stub, device)
    return chosen, backend._resolve_compute_type(stub, chosen, compute_type)


def test_auto_uses_cpu_int8_without_cuda() -> None:
    assert resolve(StubCt2(0, CPU_TYPES, set()), "auto") == ("cpu", "int8")


def test_auto_uses_cuda_float16_when_cublas_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ct2_backend, "_load_cublas", cublas_loaded)
    assert resolve(StubCt2(1, CPU_TYPES, CUDA_TYPES), "auto") == ("cuda", "float16")


def test_auto_falls_back_to_cpu_when_cublas_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(dirs: object) -> None:
        raise OSError("cublas64_12.dll not found")

    monkeypatch.setattr(ct2_backend, "_load_cublas", missing)
    assert resolve(StubCt2(1, CPU_TYPES, CUDA_TYPES), "auto") == ("cpu", "int8")


def test_explicit_cuda_requires_device_and_cublas(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError):
        resolve(StubCt2(0, CPU_TYPES, set()), "cuda")

    def missing(dirs: object) -> None:
        raise OSError("cublas64_12.dll not found")

    monkeypatch.setattr(ct2_backend, "_load_cublas", missing)
    with pytest.raises(OSError):  # 进程以 DEPENDENCY_ERROR 退出
        resolve(StubCt2(1, CPU_TYPES, CUDA_TYPES), "cuda")


def test_explicit_compute_type_must_be_supported() -> None:
    stub = StubCt2(0, CPU_TYPES, set())
    assert resolve(stub, "cpu", "int16") == ("cpu", "int16")
    assert resolve(stub, "cpu", "default") == ("cpu", "default")
    with pytest.raises(ConfigError):
        resolve(stub, "cpu", "float16")


def test_auto_compute_type_falls_back_to_float32() -> None:
    assert resolve(StubCt2(0, {"float32"}, set()), "cpu") == ("cpu", "float32")


# ---------------------------------------------------------------- 译文清理


@pytest.mark.parametrize(
    ("tokens", "target", "expected"),
    [
        (["▁Hello", ",", "▁world", "."], "en", "Hello, world."),
        (["▁It", "'", "s", "▁a", "▁nice", "▁day", "."], "en", "It's a nice day."),
        ([">>cmn_Hans<<", "▁你好", ",", "世界", "。"], "zh-Hans", "你好，世界。"),
        (["▁", "\ue6d4", "罢"], "zh-Hans", "罢"),
        (["▁文件", "未", "找到", "▁。"], "zh-Hans", "文件未找到。"),
        (["▁", "快速", "棕", "狐", "跳", "过", "懒", "狗", "."], "zh-Hans", "快速棕狐跳过懒狗。"),
        (["<s>", "▁OK", "</s>"], "en", "OK"),
        (["<unk>"], "en", ""),
        (["▁", "CO", "VI", "D", "-19", "疫苗"], "zh-Hans", "COVID-19疫苗"),
    ],
)
def test_detokenize(tokens: list[str], target: str, expected: str) -> None:
    assert detokenize(tokens, target) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("你好,世界.", "你好，世界。"),
        ("真的吗?是的!", "真的吗？是的！"),
        ("时间:下午", "时间：下午"),
        ("时间:3:00", "时间:3:00"),  # 后面是数字，按时间格式保留
        ("版本 3.0 发布", "版本 3.0 发布"),  # 汉字与数字之间的空格不动
        ("使用 Python 编程", "使用 Python 编程"),
        ("你 好 。", "你好。"),
        ("他说 “你好” 。", "他说“你好”。"),
        ("Hello, world.", "Hello, world."),  # 没有汉字时不改
    ],
)
def test_tidy_chinese(text: str, expected: str) -> None:
    assert tidy_chinese(text) == expected


def test_suppressed_tokens() -> None:
    assert is_suppressed_token(">>cmn_Hans<<")
    assert is_suppressed_token("\ue019")
    assert is_suppressed_token("▁\ue4c7x")
    assert not is_suppressed_token("▁你好")
    assert not is_suppressed_token("</s>")
    assert not is_suppressed_token(">>")


def test_decoding_limit_grows_with_source_length() -> None:
    assert decoding_limit(1) == 23
    assert decoding_limit(100) == 320


# ---------------------------------------------------------------- 能力清单与 fingerprint


def no_progress(kind: str, detail: str = "", done: int | None = None, total: int | None = None) -> None:
    pass


def capabilities(model_dir: Path, **settings: JsonValue) -> Mapping[str, JsonValue]:
    raw: dict[str, JsonValue] = {"model_dir": str(model_dir), "device": "cpu", **settings}
    return Ct2Backend().load(raw, no_progress).capabilities


@pytest.fixture
def models(tmp_path: Path) -> Path:
    directory = tmp_path / "models"
    make_package(directory, "en", "zh")
    make_package(directory, "zh", "en")
    make_package(directory, "zt", "en")
    return directory


def test_capabilities_format(models: Path) -> None:
    caps = capabilities(models)
    assert set(caps) == {"pairs", "pivot", "fingerprint"}
    assert caps["pairs"] == [["en", "zh-Hans"], ["zh-Hans", "en"], ["zh-Hant", "en"]]
    assert caps["pivot"] == "en"
    fingerprint = caps["fingerprint"]
    assert isinstance(fingerprint, str) and len(fingerprint) == 64
    json.dumps(caps)  # 能原样放进 Ready 消息


def test_fingerprint_is_stable_and_location_independent(models: Path, tmp_path: Path) -> None:
    first = capabilities(models)["fingerprint"]
    assert capabilities(models)["fingerprint"] == first
    moved = tmp_path / "moved"
    shutil.copytree(models, moved)
    assert capabilities(moved)["fingerprint"] == first
    # 不影响译文的设置不改变 fingerprint
    assert capabilities(models, intra_threads=2, max_loaded_models=8)["fingerprint"] == first


def test_fingerprint_changes_with_settings_and_models(models: Path) -> None:
    base = capabilities(models)["fingerprint"]
    assert capabilities(models, beam_size=4)["fingerprint"] != base
    assert capabilities(models, compute_type="float32")["fingerprint"] != base
    make_package(models, "en", "de")
    assert capabilities(models)["fingerprint"] != base


def test_fingerprint_is_same_in_another_process(models: Path) -> None:
    """哈希不依赖 PYTHONHASHSEED 和进程：另起一个随机种子不同的解释器，结果相同。"""
    expected = capabilities(models)["fingerprint"]
    code = (
        "import sys, json\n"
        "from lst.workers.nmt_worker.ct2_backend import Ct2Backend\n"
        "settings = {'model_dir': sys.argv[1], 'device': 'cpu'}\n"
        "caps = Ct2Backend().load(settings, lambda *a, **k: None).capabilities\n"
        "print(caps['fingerprint'])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(models)],
        cwd=ROOT,
        env={**os.environ, "PYTHONHASHSEED": "12345"},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert result.stdout.strip() == expected


def test_reload_rescans_model_directory(models: Path) -> None:
    backend = Ct2Backend()
    settings: dict[str, JsonValue] = {"model_dir": str(models), "device": "cpu"}
    first = backend.load(settings, no_progress).capabilities
    make_package(models, "en", "de")
    second = backend.load(settings, no_progress).capabilities
    pairs = second["pairs"]
    assert isinstance(pairs, list) and ["en", "de"] in pairs
    assert second["fingerprint"] != first["fingerprint"]


# ---------------------------------------------------------------- CUDA 预热


class FakeResult:
    def __init__(self, tokens: list[str]) -> None:
        self.hypotheses = [tokens]


class FakeTranslator:
    """记录 translate_batch 调用的翻译器替身；fail 为真时抛出 CTranslate2 风格的 RuntimeError。"""

    def __init__(self, runtime: FakeRuntime, model_path: str) -> None:
        self.runtime = runtime
        self.model_path = model_path

    def translate_batch(self, source: list[list[str]], **options: Any) -> list[FakeResult]:
        self.runtime.batches.append(source)
        if self.runtime.fail:
            raise RuntimeError("CUDA failed with error no kernel image is available")
        return [FakeResult(["▁ok"]) for _ in source]


class FakeTokenizer:
    def encode(self, input: list[str], *, out_type: type[str]) -> list[list[str]]:
        return [[f"▁{text}"] for text in input]


class FakeRuntime:
    """同时扮演 ctranslate2 和 sentencepiece 两个模块，用来模拟有 CUDA 设备的机器。"""

    __version__ = "0-fake"

    def __init__(self, cuda_devices: int = 1, fail: bool = False) -> None:
        self.cuda_devices = cuda_devices
        self.fail = fail
        self.translators: list[FakeTranslator] = []
        self.batches: list[list[list[str]]] = []

    def get_cuda_device_count(self) -> int:
        return self.cuda_devices

    def get_supported_compute_types(self, device: str) -> set[str]:
        return CUDA_TYPES if device == "cuda" else CPU_TYPES

    def Translator(self, model_path: str, **options: Any) -> FakeTranslator:
        translator = FakeTranslator(self, model_path)
        self.translators.append(translator)
        return translator

    def SentencePieceProcessor(self, *, model_file: str) -> FakeTokenizer:
        return FakeTokenizer()


class ProgressLog:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    def __call__(
        self, kind: str, detail: str = "", done: int | None = None, total: int | None = None
    ) -> None:
        self.kinds.append(kind)


def fake_load(
    monkeypatch: pytest.MonkeyPatch, models: Path, runtime: FakeRuntime, **settings: JsonValue
) -> tuple[Ct2Backend, ProgressLog, Mapping[str, JsonValue]]:
    """用替身运行时执行 Ct2Backend.load，cuBLAS 视为可以加载。"""

    def import_runtime(self: Ct2Backend) -> tuple[FakeRuntime, FakeRuntime]:
        return runtime, runtime

    monkeypatch.setattr(Ct2Backend, "_import_runtime", import_runtime)
    monkeypatch.setattr(ct2_backend, "_load_cublas", cublas_loaded)
    backend = Ct2Backend()
    progress = ProgressLog()
    raw: dict[str, JsonValue] = {"model_dir": str(models), **settings}
    return backend, progress, backend.load(raw, progress).capabilities


def test_cuda_load_warms_up_first_model_once(monkeypatch: pytest.MonkeyPatch, models: Path) -> None:
    runtime = FakeRuntime()
    backend, progress, caps = fake_load(monkeypatch, models, runtime)
    assert runtime.batches == [[[f"▁{ct2_backend.WARMUP_SENTENCE}"]]]  # 只翻译了一次固定短句
    assert progress.kinds.count("warmup") == 1
    assert len(runtime.translators) == 1 and "translate-en_zh" in runtime.translators[0].model_path
    assert backend.loaded_pairs() == [("en", "zh-Hans")]  # 语言对排序后的第一个
    assert caps["pairs"] == [["en", "zh-Hans"], ["zh-Hans", "en"], ["zh-Hant", "en"]]


def test_warmup_model_counts_against_lru_capacity(monkeypatch: pytest.MonkeyPatch, models: Path) -> None:
    runtime = FakeRuntime()
    backend, _, _ = fake_load(monkeypatch, models, runtime, max_loaded_models=2)
    backend.translate_sentences(("zh-Hans", "en"), ["一"])
    backend.translate_sentences(("zh-Hant", "en"), ["一"])
    # 预热的模型是最近最少使用的，装第三个模型时被淘汰，没有多占名额。
    assert backend.loaded_pairs() == [("zh-Hans", "en"), ("zh-Hant", "en")]


def test_warmup_failure_is_logged_and_load_still_succeeds(
    monkeypatch: pytest.MonkeyPatch, models: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = FakeRuntime(fail=True)
    _, progress, caps = fake_load(monkeypatch, models, runtime)
    assert len(runtime.batches) == 1
    assert "warmup" in progress.kinds
    assert caps["pairs"] == [["en", "zh-Hans"], ["zh-Hans", "en"], ["zh-Hant", "en"]]
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "预热" in r.getMessage()]
    assert len(warnings) == 1 and "RuntimeError" in warnings[0].getMessage()


def test_warmup_model_load_failure_is_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    models = tmp_path / "broken"
    make_package(models, "en", "de", extra_members={"model/../../evil": b"x"})  # 解压时被拒绝
    runtime = FakeRuntime()
    _, _, caps = fake_load(monkeypatch, models, runtime)
    assert caps["pairs"] == [["en", "de"]]
    assert runtime.translators == []
    assert any(r.levelname == "WARNING" and "预热" in r.getMessage() for r in caplog.records)


def test_cpu_does_not_warm_up(monkeypatch: pytest.MonkeyPatch, models: Path) -> None:
    """CPU 不预热：第一次翻译慢一些用户可以接受，启动时不多占内存（之后保持热态另有保证）。"""
    runtime = FakeRuntime()
    backend, progress, _ = fake_load(monkeypatch, models, runtime, device="cpu")
    assert runtime.batches == [] and runtime.translators == []
    assert "warmup" not in progress.kinds
    assert backend.loaded_pairs() == []


def test_auto_without_cuda_does_not_warm_up(monkeypatch: pytest.MonkeyPatch, models: Path) -> None:
    runtime = FakeRuntime(cuda_devices=0)
    _, progress, _ = fake_load(monkeypatch, models, runtime)
    assert runtime.batches == [] and "warmup" not in progress.kinds


def test_warmup_can_be_disabled(monkeypatch: pytest.MonkeyPatch, models: Path) -> None:
    runtime = FakeRuntime()
    backend, progress, _ = fake_load(monkeypatch, models, runtime, device="cuda", warmup=False)
    assert runtime.batches == [] and runtime.translators == []
    assert "warmup" not in progress.kinds
    assert backend.loaded_pairs() == []


def test_no_packages_means_nothing_to_warm_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = FakeRuntime()
    _, progress, caps = fake_load(monkeypatch, tmp_path / "empty", runtime)
    assert runtime.batches == [] and "warmup" not in progress.kinds
    assert caps["pairs"] == []


# ---------------------------------------------------------------- 真实后端的进程级配置错误


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({"device": "cpu"}, id="missing-model-dir"),
        pytest.param({"model_dir": "data", "compute_type": "bogus"}, id="unknown-compute-type"),
        pytest.param(
            {"model_dir": "data", "device": "cpu", "compute_type": "int8_float16"}, id="gpu-type-on-cpu"
        ),
    ],
)
def test_real_backend_config_errors_exit_66(settings: dict[str, JsonValue]) -> None:
    with WorkerProcess(REAL_BACKEND) as worker:
        worker.send(Hello(PROTOCOL_VERSION))
        worker.send(Configure(settings))
        assert worker.wait_exit(timeout=30) == ExitCode.CONFIG_ERROR


def test_cublas_is_looked_up_only_in_given_directories(tmp_path: Path) -> None:
    """只在设置给出的目录与 nvidia/cublas/bin 里找 cuBLAS，按完整路径加载，不按 PATH 搜索（MOD-06）。"""
    configured = tmp_path / "runtime" / "bin"
    dirs = ct2_backend.cublas_dirs(configured)
    assert dirs[0] == configured
    assert all(d.parts[-3:] == ("nvidia", "cublas", "bin") for d in dirs[1:])
    only_one = tmp_path / "partial"
    only_one.mkdir()
    (only_one / "cublas64_12.dll").write_bytes(b"x")  # 缺 cublasLt64_12.dll，不算
    if sys.platform == "win32":
        with pytest.raises(FileNotFoundError):
            ct2_backend._load_cublas([only_one, tmp_path / "missing"])  # pyright: ignore[reportPrivateUsage]
