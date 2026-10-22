r"""PaddleOCR 引擎：运行环境之外下载 PaddleX 模型（模型目录见 catalog.py 的 PADDLE_MODELS）。

模型下载：流式下载 ``<模型名>_infer.tar`` 到 ``official_models\.<模型名>_infer.tar.part``，长度与响应头的
Content-Length 一致才算完整；解压到 ``.<模型名>.extract``（archive.py 逐个成员检查），校验模型目录结构后原子
改名为 ``official_models\<模型名>``。下载占进度的 95%，解压与改名占 5%。卸载时删除整个 official_models 目录。

运行环境版本（OCRE-07）：CPU 版用 paddlepaddle，CUDA 版用 paddlepaddle-gpu（Paddle 官方源 cu129 的
3.3.1，支持到 RTX 50 系列；它依赖的 CUDA 12.9 运行库与 cuDNN 由构建脚本解析 wheel 的依赖写进清单）。
CUDA 版要求 NVIDIA 驱动支持 CUDA 12.9（R575 及以上）；驱动太旧或显卡不支持时 OCR 进程以依赖错误退出，
设置窗口显示原因。

已装好的判断：安装标记、运行环境与附属模型（PADDLE_AUXILIARY）都在。可选模型可以单独删除，删除后引擎仍是
已安装，界面显示该模型未下载。

源码运行（DevPaddleEnvironment）：.venv-ocr 存在且某个模型缓存的 official_models 下有模型就算已安装；
模型属于开发环境，不提供下载与删除，状态按候选模型缓存里是否有该模型给出。
"""

from pathlib import Path
from typing import ClassVar, Final

import httpx

from lst.contracts.ocr_engines import OcrEngineErrorCode, OcrEngineKind, OcrRuntimeVariant
from lst.services._shared.aio import blocking
from lst.services._shared.download import content_length, open_for_write, stream_to_file
from lst.services.ocr_engines.archive import InvalidArchive, extract_model, remove_tree
from lst.services.ocr_engines.catalog import PADDLE_AUXILIARY, PADDLE_MODELS, PADDLE_STAGE_DEFAULTS, ModelSpec
from lst.services.ocr_engines.installer import (
    EngineHost,
    InstallFailure,
    InstallOptions,
    ProgressCallback,
    RuntimeEngine,
)
from lst.services.ocr_engines.layout import (
    OFFICIAL_MODELS,
    DevPaddleEnvironment,
    PaddleInstallPaths,
    RuntimeInstallPaths,
    model_ready,
    model_url,
)
from lst.services.ocr_engines.net import DOWNLOAD_TIMEOUT, classify_transport_error, describe_error, safe_url

_HTTP_OK: Final = 200

PADDLE_OUTDATED_DETAIL: Final = "版本清单已随程序更新，需要重新安装 PaddleOCR 运行环境；已下载的模型保留"
PADDLE_FEATURES: Final = ("text", "layout", "table", "formula")
PADDLE_CUDA_DETAIL: Final = (
    "CUDA 版：paddlepaddle-gpu 3.3.1（cu129），需要 NVIDIA 驱动支持 CUDA 12.9；CUDA 运行库随运行环境安装"
)


def dev_paddle_status(dev: DevPaddleEnvironment) -> tuple[bool, str]:
    """开发环境是否可用，以及说明。"""
    if not dev.python.is_file():
        return False, f"找不到 {dev.python}，按 ocr-requirements.txt 的说明创建 .venv-ocr"
    for cache in dev.model_caches:
        models = cache / OFFICIAL_MODELS
        if models.is_dir() and any(entry.is_dir() for entry in models.iterdir()):
            return True, f"源码运行：使用 {dev.venv} 与 {cache}"
    caches = "、".join(str(cache / OFFICIAL_MODELS) for cache in dev.model_caches)
    return False, f"源码运行：{caches} 下都没有模型"


class PaddleEngine(RuntimeEngine):
    """PaddleOCR 引擎，见模块说明。"""

    kind = OcrEngineKind.PADDLE
    title = "PaddleOCR"
    outdated_detail = PADDLE_OUTDATED_DETAIL
    stage_defaults: ClassVar = PADDLE_STAGE_DEFAULTS

    def __init__(
        self,
        layout: PaddleInstallPaths | DevPaddleEnvironment,
        host: EngineHost,
        options: InstallOptions,
        *,
        client: httpx.AsyncClient,
        models: tuple[ModelSpec, ...] = PADDLE_MODELS + PADDLE_AUXILIARY,
        model_base_url: str,
        runtime_bytes: dict[OcrRuntimeVariant, int],
        download_bytes: dict[OcrRuntimeVariant, int],
        languages: tuple[str, ...],
    ) -> None:
        paths = layout if isinstance(layout, PaddleInstallPaths) else None
        super().__init__(
            paths,
            host,
            options,
            runtime_bytes=runtime_bytes,
            download_bytes=download_bytes,
            languages=languages,
        )
        self._dev = layout if isinstance(layout, DevPaddleEnvironment) else None
        self._client = client
        self._models = models
        self._base_url = model_base_url

    # ------------------------------------------------------------------ 信息

    @property
    def features(self) -> tuple[str, ...]:
        return PADDLE_FEATURES

    def variant_detail(self, variant: OcrRuntimeVariant | None) -> str:
        return PADDLE_CUDA_DETAIL if variant is OcrRuntimeVariant.CUDA else ""

    def dev_venv(self) -> Path:
        assert self._dev is not None
        return self._dev.venv

    def dev_site(self) -> Path:
        assert self._dev is not None
        return self._dev.site_packages

    def check_dev(self) -> tuple[bool, str]:
        assert self._dev is not None
        return dev_paddle_status(self._dev)

    def _paths_of(self, paths: RuntimeInstallPaths) -> PaddleInstallPaths:
        assert isinstance(paths, PaddleInstallPaths)
        return paths

    def model_specs(self) -> tuple[ModelSpec, ...]:
        return self._models

    def model_directories(self, spec: ModelSpec) -> list[Path]:
        """模型可能所在的目录：安装版只有一个，源码运行依次是各候选模型缓存。"""
        if self._paths is not None:
            return [self._paths_of(self._paths).model_dir(spec.id)]
        assert self._dev is not None
        return [cache / OFFICIAL_MODELS / spec.id for cache in self._dev.model_caches]

    def model_is_ready(self, spec: ModelSpec) -> bool:
        return any(model_ready(directory) for directory in self.model_directories(spec))

    def model_leftovers(self, spec: ModelSpec) -> list[Path]:
        if self._paths is None:
            return []
        layout = self._paths_of(self._paths)
        return [layout.download_part(spec.id), layout.extract_dir(spec.id)]

    def delete_model_files(self, spec: ModelSpec) -> None:
        if self._paths is None:
            return
        remove_tree(self._paths_of(self._paths).model_dir(spec.id))

    # ------------------------------------------------------------------ 安装流程的 PaddleOCR 部分

    def missing_parts(self, paths: RuntimeInstallPaths) -> list[str]:
        layout = self._paths_of(paths)
        return [
            f"模型 {spec.id}"
            for spec in self._models
            if spec.stage is None and not model_ready(layout.model_dir(spec.id))
        ]

    def uninstall_targets(self, paths: RuntimeInstallPaths) -> list[Path]:
        return [self._paths_of(paths).models_dir]

    def leftover_dir(self, paths: RuntimeInstallPaths) -> Path | None:
        return self._paths_of(paths).models_dir

    async def fetch_model(self, spec: ModelSpec, report: ProgressCallback) -> None:
        paths = self._paths_of(self.require_paths())
        url = model_url(self._base_url, spec.id)
        part = paths.download_part(spec.id)
        await blocking(paths.models_dir.mkdir, parents=True, exist_ok=True)
        self.logger.info("开始下载 OCR 模型 %s：%s", spec.id, safe_url(url))
        sink = await blocking(open_for_write, part)
        length: int | None = None
        received = 0
        try:
            async with self._client.stream(
                "GET", url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
            ) as response:
                if response.status_code != _HTTP_OK:
                    raise InstallFailure(OcrEngineErrorCode.HTTP, f"{spec.id}：HTTP {response.status_code}")
                length = content_length(response)
                expected = length or spec.size_bytes

                def progress(done: int) -> None:
                    if expected:
                        report(0.95 * done / expected)

                received = await stream_to_file(
                    response, sink, chunk_bytes=self._options.write_chunk_bytes, on_progress=progress
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise InstallFailure(classify_transport_error(exc), f"{spec.id}：{describe_error(exc)}") from exc
        finally:
            await blocking(sink.close)
        if length is not None and received != length:
            raise InstallFailure(
                OcrEngineErrorCode.INVALID_PACKAGE, f"{spec.id}：收到 {received} 字节，应为 {length} 字节"
            )
        try:
            await blocking(extract_model, part, paths.extract_dir(spec.id), paths.model_dir(spec.id), spec.id)
        except InvalidArchive as exc:
            raise InstallFailure(OcrEngineErrorCode.INVALID_PACKAGE, f"{spec.id}：{exc}") from exc
        self.logger.info("OCR 模型 %s 已就位（%d 字节）", spec.id, received)
        report(1.0)
