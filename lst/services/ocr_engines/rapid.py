r"""RapidOCR 引擎：运行环境带默认模型，其余模型单独下载（模型目录见 catalog.py 的 RAPID_MODELS）。

安装版：通用安装流程（installer.py）用 ``tools\uv.exe`` 按 ``app\ocr-rapid-<版本>-requirements.txt`` 把
rapidocr、推理库及其依赖装进 ``<模型目录>\engines\rapid\site``。
rapidocr 3.10.0 的 wheel 自带 PP-OCRv6 small 的
检测与识别模型和 PP-OCR mobile v2.0 的方向分类模型（rapidocr/models 下，核实于 wheel 的 RECORD），这三个是
默认组合，随运行环境就位；安装完成后按 catalog.RAPID_BUNDLED_FILES 复查，缺任何一个按无效包处理。

运行环境版本（OCRE-07）与推理库：

| 版本 | 推理库 | 另需 |
|---|---|---|
| cpu | onnxruntime 1.31.0 | 无 |
| directml | onnxruntime-directml 1.24.4 | sympy；Windows 10 1903 以上、支持 DirectX 12 的显卡 |
| cuda | onnxruntime-gpu 1.31.0 | CUDA 13 与 cuDNN 9 运行库（见下）；驱动支持 CUDA 13（R580 以上） |

DirectML 版的 sympy 是 onnxruntime-directml 声明的依赖（CPU 版里它是可选依赖）。CUDA 版的运行库是
nvidia-cuda-runtime、nvidia-cuda-nvrtc、nvidia-cublas、nvidia-cufft、nvidia-curand、nvidia-nvjitlink 与
nvidia-cudnn-cu13。onnxruntime-gpu 1.31.0 是按 CUDA 13 编译的（它在 PyPI 上的 Requires-Dist 用 extra
"cuda"、"cudnn" 声明了上面这些 nvidia 包，版本约束为 ~=13.0、cudnn ~=9.0），Windows 上由 OCR 进程调用
``onnxruntime.preload_dlls()`` 从 site 里的 nvidia 目录加载这些 DLL（路径见 onnxruntime/__init__.py 的
_get_nvidia_dll_paths）。

另下载的模型：单个 .onnx 文件，流式下载到 ``<模型目录>\ocr\rapid\.<文件名>.part``，SHA-256 与
default_models.yaml 记录的一致才原子改名为 ``<文件名>``；不一致按无效包处理，删除半成品。
换版本与卸载：换版本只重装运行环境，另下载的模型保留；卸载删除运行环境与另下载的模型。

源码运行（DevRapidEnvironment）：.venv-ocr 里装了 rapidocr 与某个 onnxruntime 发行包、并且有自带模型就算
已安装，已装版本按 .venv-ocr 里的推理库判断。DevRapidEnvironment 给出 model_dir 时允许下载与删除模型
（下载到模型目录，不改 .venv-ocr）。
"""

import hashlib
from pathlib import Path
from typing import ClassVar, Final

import httpx

from lst.contracts.ocr_engines import OcrEngineErrorCode, OcrEngineKind, OcrRuntimeVariant, OcrStage
from lst.services._shared.aio import blocking
from lst.services._shared.download import content_length, open_for_write, stream_to_file
from lst.services.ocr_engines.archive import remove_tree
from lst.services.ocr_engines.catalog import (
    RAPID_BUNDLED_FILES,
    RAPID_MODELS,
    RAPID_STAGE_DEFAULTS,
    ModelSpec,
)
from lst.services.ocr_engines.installer import (
    EngineHost,
    InstallFailure,
    InstallOptions,
    ProgressCallback,
    RuntimeEngine,
)
from lst.services.ocr_engines.layout import (
    DevRapidEnvironment,
    RapidInstallPaths,
    RuntimeInstallPaths,
    has_distribution,
)
from lst.services.ocr_engines.net import DOWNLOAD_TIMEOUT, classify_transport_error, describe_error, safe_url

_HTTP_OK: Final = 200

RAPID_FEATURES: Final = ("text",)
RAPID_RUNTIMES: Final = ("onnxruntime", "onnxruntime-directml", "onnxruntime-gpu")
"""开发环境里至少要有其中一个推理库。"""

RAPID_VARIANT_DETAIL: Final[dict[OcrRuntimeVariant, str]] = {
    OcrRuntimeVariant.DIRECTML: "DirectML 版：onnxruntime-directml 1.24.4，需要支持 DirectX 12 的显卡",
    OcrRuntimeVariant.CUDA: (
        "CUDA 版：onnxruntime-gpu 1.31.0（CUDA 13、cuDNN 9），需要 NVIDIA 驱动支持 CUDA 13；"
        "CUDA 运行库随运行环境安装"
    ),
}


def dev_rapid_status(dev: DevRapidEnvironment) -> tuple[bool, str]:
    """开发环境是否可用，以及说明。"""
    if not dev.python.is_file():
        return False, f"找不到 {dev.python}，按 ocr-requirements.txt 的说明创建 .venv-ocr"
    site = dev.site_packages
    if not has_distribution(site, "rapidocr"):
        return False, f"源码运行：{dev.venv} 里没有安装 rapidocr，按 ocr-requirements.txt 安装"
    if not any(has_distribution(site, name) for name in RAPID_RUNTIMES):
        return False, f"源码运行：{dev.venv} 里没有安装 onnxruntime，按 ocr-requirements.txt 安装"
    missing = [name for name in RAPID_BUNDLED_FILES if not (dev.bundled_dir / name).is_file()]
    if missing:
        return False, f"源码运行：{dev.bundled_dir} 里缺少 RapidOCR 的模型 {', '.join(missing)}"
    return True, f"源码运行：使用 {dev.venv}"


def rapid_model_path(spec: ModelSpec, bundled_dir: Path, model_dir: Path | None) -> Path:
    """模型文件的位置：自带的在运行环境的 rapidocr/models，另下载的在 model_dir。"""
    if spec.bundled or model_dir is None:
        return bundled_dir / spec.filename
    return model_dir / spec.filename


class RapidEngine(RuntimeEngine):
    """RapidOCR 引擎，见模块说明。"""

    kind = OcrEngineKind.RAPID
    title = "RapidOCR"
    outdated_detail = "版本清单已随程序更新，需要重新安装 RapidOCR 运行环境；已下载的模型保留"
    stage_defaults: ClassVar = RAPID_STAGE_DEFAULTS

    def __init__(
        self,
        layout: RapidInstallPaths | DevRapidEnvironment,
        host: EngineHost,
        options: InstallOptions,
        *,
        client: httpx.AsyncClient,
        models: tuple[ModelSpec, ...] = RAPID_MODELS,
        runtime_bytes: dict[OcrRuntimeVariant, int],
        download_bytes: dict[OcrRuntimeVariant, int],
        languages: tuple[str, ...],
    ) -> None:
        paths = layout if isinstance(layout, RapidInstallPaths) else None
        super().__init__(
            paths,
            host,
            options,
            runtime_bytes=runtime_bytes,
            download_bytes=download_bytes,
            languages=languages,
        )
        self._dev = layout if isinstance(layout, DevRapidEnvironment) else None
        self._client = client
        self._models = models

    def dev_downloads(self) -> bool:
        return self._dev is not None and self._dev.model_dir is not None

    @property
    def features(self) -> tuple[str, ...]:
        return RAPID_FEATURES

    def variant_detail(self, variant: OcrRuntimeVariant | None) -> str:
        return RAPID_VARIANT_DETAIL.get(variant, "") if variant is not None else ""

    def languages(self) -> tuple[str, ...]:
        """当前选用的识别模型覆盖的语言。"""
        rec_id = self.selection.get(OcrStage.RECOGNITION)
        rec = next((spec for spec in self._models if spec.id == rec_id), None)
        return rec.languages if rec is not None and rec.languages else self._languages

    def dev_venv(self) -> Path:
        assert self._dev is not None
        return self._dev.venv

    def dev_site(self) -> Path:
        assert self._dev is not None
        return self._dev.site_packages

    def check_dev(self) -> tuple[bool, str]:
        assert self._dev is not None
        return dev_rapid_status(self._dev)

    @property
    def bundled_dir(self) -> Path:
        if self._paths is not None:
            assert isinstance(self._paths, RapidInstallPaths)
            return self._paths.bundled_dir
        assert self._dev is not None
        return self._dev.bundled_dir

    @property
    def model_dir(self) -> Path | None:
        if self._paths is not None:
            assert isinstance(self._paths, RapidInstallPaths)
            return self._paths.model_dir
        assert self._dev is not None
        return self._dev.model_dir

    def model_specs(self) -> tuple[ModelSpec, ...]:
        return self._models

    def model_path(self, spec: ModelSpec) -> Path:
        return rapid_model_path(spec, self.bundled_dir, self.model_dir)

    def model_is_ready(self, spec: ModelSpec) -> bool:
        return self.model_path(spec).is_file()

    def _part(self, spec: ModelSpec) -> Path:
        directory = self.model_dir
        assert directory is not None
        return directory / f".{spec.filename}.part"

    def model_leftovers(self, spec: ModelSpec) -> list[Path]:
        return [self._part(spec)] if self.model_dir is not None and not spec.bundled else []

    def delete_model_files(self, spec: ModelSpec) -> None:
        if spec.bundled or self.model_dir is None:
            return
        remove_tree(self.model_path(spec))

    def missing_parts(self, paths: RuntimeInstallPaths) -> list[str]:
        bundled = paths.site_dir / "rapidocr" / "models"
        return [f"模型 {name}" for name in RAPID_BUNDLED_FILES if not (bundled / name).is_file()]

    def runtime_complete(self, paths: RuntimeInstallPaths) -> bool:
        # 自带模型在运行环境里：模型文件缺失说明运行环境不完整，要重装。
        return not self.missing_parts(paths)

    def uninstall_targets(self, paths: RuntimeInstallPaths) -> list[Path]:
        assert isinstance(paths, RapidInstallPaths)
        return [paths.model_dir]

    def leftover_dir(self, paths: RuntimeInstallPaths) -> Path | None:
        assert isinstance(paths, RapidInstallPaths)
        return paths.model_dir

    async def fetch_model(self, spec: ModelSpec, report: ProgressCallback) -> None:
        directory = self.model_dir
        if directory is None or not spec.url:
            raise InstallFailure(OcrEngineErrorCode.INTERNAL, f"{spec.id}：没有下载地址或下载目录")
        part = self._part(spec)
        self.logger.info("开始下载 RapidOCR 模型 %s：%s", spec.id, safe_url(spec.url))
        sink = await blocking(open_for_write, part)
        digest = hashlib.sha256()
        length: int | None = None
        received = 0
        try:
            async with self._client.stream(
                "GET", spec.url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
            ) as response:
                if response.status_code != _HTTP_OK:
                    raise InstallFailure(OcrEngineErrorCode.HTTP, f"{spec.id}：HTTP {response.status_code}")
                length = content_length(response)
                expected = length or spec.size_bytes

                def progress(done: int) -> None:
                    if expected:
                        report(0.99 * done / expected)

                received = await stream_to_file(
                    response,
                    sink,
                    chunk_bytes=self._options.write_chunk_bytes,
                    on_progress=progress,
                    digest=digest,
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise InstallFailure(classify_transport_error(exc), f"{spec.id}：{describe_error(exc)}") from exc
        finally:
            await blocking(sink.close)
        if length is not None and received != length:
            raise InstallFailure(
                OcrEngineErrorCode.INVALID_PACKAGE, f"{spec.id}：收到 {received} 字节，应为 {length} 字节"
            )
        if spec.sha256 and digest.hexdigest() != spec.sha256:
            raise InstallFailure(OcrEngineErrorCode.INVALID_PACKAGE, f"{spec.id}：SHA-256 与记录的不一致")
        await blocking(part.replace, self.model_path(spec))
        self.logger.info("RapidOCR 模型 %s 已就位（%d 字节）", spec.id, received)
        report(1.0)
