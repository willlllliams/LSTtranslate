"""PaddleOCR 后端读取 [ocr_paddle] 的产线参数（OCRE-09）：假的 PaddleX 产线，不导入 paddle。

真实产线的测试在 test_paddle_process.py。这里只检查参数从 Configure 与请求 options 走到 predict 的关键字参数，
等于默认值的参数不传，版面参数只在版面模式下传。
"""

from dataclasses import dataclass, field

import numpy as np
import pytest

from lst.workers.ocr_worker.image import Pixels
from lst.workers.ocr_worker.paddle_backend import PaddleBackend, resolve_device
from lst.workers.ocr_worker.settings import OcrSettings, RequestOptions


@dataclass
class FakePipeline:
    calls: list[dict[str, object]] = field(default_factory=lambda: list[dict[str, object]]())

    def predict(self, input: Pixels, **kwargs: object) -> list[object]:
        del input
        self.calls.append(kwargs)
        return [object()]

    def close(self) -> None:
        pass


def backend_with(settings: dict[str, object]) -> tuple[PaddleBackend, FakePipeline]:
    backend = PaddleBackend()
    pipeline = FakePipeline()
    backend._settings = OcrSettings.parse(settings)  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    backend._pipeline = pipeline  # pyright: ignore[reportPrivateUsage]
    return backend, pipeline


def test_default_parameters_are_not_passed() -> None:
    backend, pipeline = backend_with({"mode": "structure"})
    settings = backend._settings  # pyright: ignore[reportPrivateUsage]
    assert settings is not None
    backend._predict(np.zeros((4, 4, 3), np.uint8), frozenset({"table"}))  # pyright: ignore[reportPrivateUsage]
    [call] = pipeline.calls
    assert not any(key.startswith(("text_det", "text_rec", "layout_")) for key in call)
    assert call["use_table_recognition"] is True


def test_configured_and_requested_parameters_reach_predict() -> None:
    backend, pipeline = backend_with({"mode": "structure", "text_det_thresh": 0.2, "layout_threshold": 0.45})
    settings = backend._settings  # pyright: ignore[reportPrivateUsage]
    assert settings is not None
    options = RequestOptions.parse(
        {"text_rec_score_thresh": 0.5, "layout_merge_bboxes_mode": "union"}, settings
    )
    backend._predict(np.zeros((4, 4, 3), np.uint8), options.features, options.predict)  # pyright: ignore[reportPrivateUsage]
    [call] = pipeline.calls
    assert call["text_det_thresh"] == 0.2
    assert call["text_rec_score_thresh"] == 0.5
    assert call["layout_threshold"] == 0.45
    assert call["layout_merge_bboxes_mode"] == "union"


def test_text_mode_does_not_pass_layout_parameters() -> None:
    backend, pipeline = backend_with(
        {"mode": "text", "layout_threshold": 0.45, "text_det_limit_side_len": 960}
    )
    backend._predict(np.zeros((4, 4, 3), np.uint8), frozenset())  # pyright: ignore[reportPrivateUsage]
    [call] = pipeline.calls
    assert call["text_det_limit_side_len"] == 960
    assert "layout_threshold" not in call and "use_table_recognition" not in call


def test_gpu_on_cpu_paddle_names_the_fix() -> None:
    with pytest.raises(ImportError) as info:
        resolve_device(False, 0, "gpu")
    assert "CUDA 版" in str(info.value)


def test_cuda_build_without_usable_gpu_is_a_dependency_error() -> None:
    with pytest.raises(ImportError) as info:
        resolve_device(True, 0, "gpu")
    assert "CUDA 不可用" in str(info.value)
    assert resolve_device(True, 0, "auto") == "cpu"
    assert resolve_device(True, 1, "auto") == "gpu"
