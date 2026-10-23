r"""需要下载的 OCR 引擎（PaddleOCR、RapidOCR）的目录约定、运行环境版本的清单与安装标记。

两个引擎的运行环境都由 ``tools\uv.exe pip install --target`` 按各自的版本清单装进模型目录，目录约定相同
（``RuntimeInstallPaths``，组合根构造时传入，本模块不导入 lst.app）：

- ``<模型目录>\engines\<引擎>\site``：运行环境；
- ``<模型目录>\engines\<引擎>\site.previous``：版本切换中的旧运行环境，安装标记提交后清理；
- ``<模型目录>\engines\<引擎>\site.part``：安装中的运行环境，uv 成功后用 ``os.replace`` 原子改名为 site；
- ``<模型目录>\engines\<引擎>\uv-cache``：安装期间 uv 的缓存目录，装完删除；
- ``<模型目录>\engines\<引擎>\installed.json``：安装完成的标记，运行环境与默认模型都就位后最后写入，
  记录版本清单的 SHA-256 与已装的运行环境版本（variant）。

运行环境的版本（OCRE-07）
=========================

每个引擎声明可装的版本（ENGINE_VARIANTS）：PaddleOCR 有 CPU 与 CUDA；RapidOCR 有 CPU、DirectML 与 CUDA。
每个版本一份版本清单 ``<安装目录>\app\ocr-<引擎>-<版本>-requirements.txt``（requirements_name），由
scripts/build_installer.py 生成：CPU 版按 .venv-ocr 冻结，GPU 版只把推理库换成对应的包（paddlepaddle →
paddlepaddle-gpu，onnxruntime → onnxruntime-directml 或 onnxruntime-gpu）并加上它们需要的 CUDA 运行库，
其余依赖与 CPU 版相同。安装目录里缺某个版本的清单（构建时无法生成）时，这个版本不出现在
OcrEngineInfo.variants 里。

同一时刻每个引擎只装一个版本；换版本时只重装运行环境，模型在 site 之外，保留。

模型
====

- PaddleOCR：``<模型缓存>\official_models\<模型名>\``（PADDLE_PDX_CACHE_HOME 指向模型缓存）；
- RapidOCR：wheel 自带的模型在 ``site\rapidocr\models``，另下载的模型在 ``<模型目录>\ocr\rapid``。

运行环境、模型缓存与临时文件放在同一个模型目录里（默认是数据目录下的 models，可以在设置里更改），
临时目录与目标目录在同一个卷上，原子改名才能成立。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final, cast

from lst.contracts.ocr_engines import OcrEngineKind, OcrRuntimeVariant

DEFAULT_MODEL_BASE_URL: Final = (
    "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0"
)
"""PaddleX 官方推理模型的下载地址前缀。
与 paddlex/inference/utils/official_models.py 的 _BosModelHoster 相同。"""

OFFICIAL_MODELS: Final = "official_models"
MODEL_CONFIG_FILE: Final = "inference.yml"
MODEL_PARAMS_FILES: Final = ("inference.pdiparams", "model_state.pdparams")
"""PaddleX 模型目录里的参数文件：推理模型是 inference.pdiparams，PP-Chart2Table 这类生成式模型是
model_state.pdparams（开发机上的 PP-Chart2Table 目录核实）。"""

MARKER_NAME: Final = "installed.json"
RUNTIME_STAMP: Final = ".lst-runtime.json"
"""运行环境目录里的标记：记录安装所用的版本清单的 SHA-256，清单不变时重试安装可以复用运行环境。"""

V = OcrRuntimeVariant
ENGINE_VARIANTS: Final[dict[OcrEngineKind, tuple[OcrRuntimeVariant, ...]]] = {
    OcrEngineKind.PADDLE: (V.CPU, V.CUDA),
    OcrEngineKind.RAPID: (V.CPU, V.DIRECTML, V.CUDA),
}
"""各引擎声明可装的运行环境版本。"""

RUNTIME_BYTES: Final[dict[tuple[OcrEngineKind, OcrRuntimeVariant], int]] = {
    (OcrEngineKind.PADDLE, V.CPU): 975 * 1024 * 1024,
    (OcrEngineKind.PADDLE, V.CUDA): 3 * 1024 * 1024 * 1024,
    (OcrEngineKind.RAPID, V.CPU): 272_547_604,
    (OcrEngineKind.RAPID, V.DIRECTML): 272_547_604 - 44_000_000 + 70_000_000,
    (OcrEngineKind.RAPID, V.CUDA): 272_547_604 - 44_000_000 + 3_200_000_000,
}
"""运行环境装好后的大小（字节），作为运行环境阶段的进度权重。

- PaddleOCR CPU：构建时实测的 site 约 975 MB；
- RapidOCR CPU：.venv-ocr 中 rapidocr 与 onnxruntime 依赖闭包（21 个包）RECORD 里文件大小的合计，其中
  onnxruntime 44 MB；
- GPU 版没有在本机装过，按 CPU 版减去推理库、加上 GPU 推理库与 CUDA 运行库的估计值：onnxruntime-directml
  解压后约 70 MB（wheel 25 MB）；onnxruntime-gpu 与 nvidia-cudnn-cu13、nvidia-cublas 等 CUDA 13 运行库合计
  约 3.2 GB；paddlepaddle-gpu 与它依赖的 CUDA 12 运行库约 3 GB。数字只影响进度条走得快慢。
"""

DOWNLOAD_BYTES: Final[dict[tuple[OcrEngineKind, OcrRuntimeVariant], int]] = {
    (OcrEngineKind.PADDLE, V.CPU): 975 * 1024 * 1024,
    (OcrEngineKind.PADDLE, V.CUDA): 3 * 1024 * 1024 * 1024,
    (OcrEngineKind.RAPID, V.CPU): 111_164_637,
    (OcrEngineKind.RAPID, V.DIRECTML): 111_164_637 - 14_900_000 + 25_112_253,
    (OcrEngineKind.RAPID, V.CUDA): 111_164_637 - 14_900_000 + 1_340_000_000,
}
"""运行环境的下载大小（估计，界面显示“需要下载约 … MB”）。

RapidOCR CPU 版是 21 个包装好后的每个文件按 zlib 级别 6 压缩后的大小之和（opencv-contrib-python 45.5 MB、
rapidocr 27.3 MB、onnxruntime 14.9 MB 等）。DirectML 版把 onnxruntime 换成 onnxruntime-directml 1.24.4 的
wheel（PyPI 上 25,112,253 字节）。CUDA 版换成 onnxruntime-gpu 1.31.0（168,093,295 字节），另加
nvidia-cudnn-cu13 9.27（436 MB）、nvidia-cublas 13.8（423 MB）、nvidia-cufft 12.4（161 MB）、nvidia-curand
10.4（57 MB）、nvidia-cuda-nvrtc 13.4（47 MB）、nvidia-nvjitlink 13.4（39 MB）、
nvidia-cuda-runtime 13.4（3 MB）
的 Windows wheel，合计约 1.34 GB（均为 PyPI 上 win_amd64 wheel 的大小）。PaddleOCR 的数字是估计值。
"""


def requirements_name(kind: OcrEngineKind, variant: OcrRuntimeVariant) -> str:
    """一个引擎一个版本的版本清单在安装目录 app\\ 下的文件名。"""
    return f"ocr-{kind.value}-{variant.value}-requirements.txt"


def model_url(base_url: str, name: str) -> str:
    """PaddleX 模型包的下载地址：``<前缀>/<模型名>_infer.tar``。"""
    return f"{base_url.rstrip('/')}/{name}_infer.tar"


@dataclass(frozen=True, slots=True)
class RuntimeInstallPaths:
    r"""安装版里一个需要下载的引擎的运行环境路径，由组合根传入。

    - engine_dir：``<模型目录>\engines\<引擎>``；
    - uv：``<安装目录>\tools\uv.exe``；
    - python：``<安装目录>\runtime\python.exe``，uv 按它的版本与平台选择 wheel；
    - requirements_dir：版本清单所在的 ``<安装目录>\app``，各版本的文件名见 requirements_name。
    """

    KIND: ClassVar[OcrEngineKind]

    engine_dir: Path
    uv: Path
    python: Path
    requirements_dir: Path

    def requirements(self, variant: OcrRuntimeVariant) -> Path:
        return self.requirements_dir / requirements_name(self.KIND, variant)

    @property
    def site_dir(self) -> Path:
        return self.engine_dir / "site"

    @property
    def site_part(self) -> Path:
        return self.engine_dir / "site.part"

    @property
    def site_backup(self) -> Path:
        """换版本时保留的旧运行环境，安装标记提交后才清理。"""
        return self.engine_dir / "site.previous"

    @property
    def uv_cache(self) -> Path:
        return self.engine_dir / "uv-cache"

    @property
    def marker(self) -> Path:
        return self.engine_dir / MARKER_NAME

    @property
    def marker_part(self) -> Path:
        """写安装标记时的临时文件，写完原子改名为 marker。"""
        return self.engine_dir / (MARKER_NAME + ".part")


@dataclass(frozen=True, slots=True)
class PaddleInstallPaths(RuntimeInstallPaths):
    """安装版的 PaddleOCR 引擎路径。model_cache_dir 是模型缓存（PADDLE_PDX_CACHE_HOME），模型在其中的
    official_models 下。"""

    KIND: ClassVar[OcrEngineKind] = OcrEngineKind.PADDLE

    model_cache_dir: Path

    @property
    def models_dir(self) -> Path:
        return self.model_cache_dir / OFFICIAL_MODELS

    def model_dir(self, name: str) -> Path:
        return self.models_dir / name

    def download_part(self, name: str) -> Path:
        """下载中的模型包。以点开头，PaddleX 不会把它当成模型目录。"""
        return self.models_dir / f".{name}_infer.tar.part"

    def extract_dir(self, name: str) -> Path:
        return self.models_dir / f".{name}.extract"


@dataclass(frozen=True, slots=True)
class RapidInstallPaths(RuntimeInstallPaths):
    r"""安装版的 RapidOCR 引擎路径：engine_dir 是 ``<模型目录>\engines\rapid``；model_dir 是另下载的模型
    所在的 ``<模型目录>\ocr\rapid``（自带的模型在 site 里）。"""

    KIND: ClassVar[OcrEngineKind] = OcrEngineKind.RAPID

    model_dir: Path

    @property
    def bundled_dir(self) -> Path:
        return self.site_dir / "rapidocr" / "models"


@dataclass(frozen=True, slots=True)
class DevPaddleEnvironment:
    """源码运行时的 PaddleOCR 开发环境，由组合根传入。模块只检查它，从不修改。

    - venv：仓库下的 .venv-ocr；
    - model_caches：依次检查的模型缓存目录（data/models/ocr、assets/PaddleOCR_and_PP-Structure），
      其中任意一个的 official_models 下有模型就算有模型。
    """

    venv: Path
    model_caches: tuple[Path, ...]

    @property
    def python(self) -> Path:
        return self.venv / "Scripts" / "python.exe"

    @property
    def site_packages(self) -> Path:
        return self.venv / "Lib" / "site-packages"


@dataclass(frozen=True, slots=True)
class DevRapidEnvironment:
    """源码运行时的 RapidOCR 开发环境（仓库下的 .venv-ocr），由组合根传入。模块不修改 .venv-ocr；
    另下载的模型放在 model_dir（模型目录下，与安装版相同），None 表示源码运行时不提供模型下载。"""

    venv: Path
    model_dir: Path | None = None

    @property
    def python(self) -> Path:
        return self.venv / "Scripts" / "python.exe"

    @property
    def site_packages(self) -> Path:
        return self.venv / "Lib" / "site-packages"

    @property
    def bundled_dir(self) -> Path:
        return self.site_packages / "rapidocr" / "models"


RAPID_REQUIRED_FILES: Final = ("rapidocr/__init__.py",)


def has_distribution(site: Path, name: str) -> bool:
    """site 下有名为 name 的已安装发行包（``<名>-<版本>.dist-info`` 目录，名字里的 - 写作 _）。"""
    pattern = name.replace("-", "_")
    return any(entry.is_dir() for entry in site.glob(f"{pattern}-*.dist-info"))


def detect_variant(kind: OcrEngineKind, site: Path) -> OcrRuntimeVariant:
    """按 site 里装的推理库判断运行环境的版本（源码运行时看 .venv-ocr）。"""
    if kind is OcrEngineKind.PADDLE:
        return V.CUDA if has_distribution(site, "paddlepaddle-gpu") else V.CPU
    if has_distribution(site, "onnxruntime-gpu"):
        return V.CUDA
    if has_distribution(site, "onnxruntime-directml"):
        return V.DIRECTML
    return V.CPU


def model_ready(directory: Path) -> bool:
    """PaddleX 模型目录可用：有 inference.yml 与 MODEL_PARAMS_FILES 中的一个。"""
    if not (directory / MODEL_CONFIG_FILE).is_file():
        return False
    return any((directory / name).is_file() for name in MODEL_PARAMS_FILES)


def read_marker(path: Path) -> dict[str, object] | None:
    """读安装标记；不存在或不是 JSON 对象时返回 None。"""
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    items = cast(dict[object, object], data).items()
    return {str(key): value for key, value in items}


def marker_variant(marker: dict[str, object] | None) -> OcrRuntimeVariant:
    """安装标记里记录的运行环境版本；旧版本程序写的标记没有这一项，按 CPU 版处理。"""
    raw = marker.get("variant") if marker is not None else None
    try:
        return OcrRuntimeVariant(raw) if isinstance(raw, str) else V.CPU
    except ValueError:
        return V.CPU
