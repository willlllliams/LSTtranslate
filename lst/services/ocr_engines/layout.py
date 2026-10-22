r"""PaddleOCR 引擎的目录约定与模型清单。

安装版的目录（组合根构造 ``PaddleInstallPaths`` 时传入，本模块不导入 lst.app）：

- ``<数据目录>\engines\paddle\site``：运行环境，由 ``tools\uv.exe pip install --target`` 装入；
- ``<数据目录>\engines\paddle\site.part``：安装中的运行环境，uv 成功后用 ``os.replace`` 原子改名为 site；
- ``<数据目录>\engines\paddle\uv-cache``：安装期间 uv 的缓存目录，装完删除；
- ``<数据目录>\engines\paddle\installed.json``：安装完成的标记，运行环境与全部模型就位后最后写入；
- ``<模型缓存>\official_models\<模型名>\``：PaddleX 的模型目录（PADDLE_PDX_CACHE_HOME 指向模型缓存）。

运行环境与模型缓存放在同一个数据目录里，临时目录与目标目录在同一个卷上，原子改名才能成立。

模型清单按 OCR 进程的默认设置确定（lst/workers/ocr_worker/settings.py 与 paddle_config.py）：
版面模式、开启表格，另加三个体积小的子能力模型（文档方向分类、去畸变、文本行方向分类），
让这些开关不需要再次下载就能打开。公式识别（PP-FormulaNet_plus-L，约 730 MB）与图表识别
（PP-Chart2Table，约 1.4 GB）默认关闭，不在默认清单里。大小取自开发机上同名模型目录里
``inference.*`` 文件的合计，其中 PP-OCRv5_mobile_det、PP-OCRv5_mobile_rec、PP-DocLayout_plus-L
与官方下载地址 HEAD 请求返回的 Content-Length 一致（4.9 MB、16.8 MB、130 MB）。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

DEFAULT_MODEL_BASE_URL: Final = (
    "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0"
)
"""PaddleX 官方推理模型的下载地址前缀。

与 paddlex/inference/utils/official_models.py 的 _BosModelHoster 相同。
"""

OFFICIAL_MODELS: Final = "official_models"
MODEL_REQUIRED_FILES: Final = ("inference.yml", "inference.pdiparams")
"""一个模型目录可用的最低要求。PaddleX 3 的模型另有 inference.json，旧格式是 inference.pdmodel。"""

MARKER_NAME: Final = "installed.json"
RUNTIME_STAMP: Final = ".lst-runtime.json"
"""运行环境目录里的标记：记录安装所用的版本清单的 SHA-256，清单不变时重试安装可以复用运行环境。"""


@dataclass(frozen=True, slots=True)
class PaddleModel:
    """一个需要下载的 PaddleX 模型。size_bytes 是估计的下载大小，用于进度的权重与界面显示。"""

    name: str
    size_bytes: int
    feature: str = "base"
    """用途：base（版面与文本）、table、doc_orientation、doc_unwarping、textline_orientation、formula、chart。"""


DEFAULT_PADDLE_MODELS: Final[tuple[PaddleModel, ...]] = (
    PaddleModel("PP-DocLayout_plus-L", 130_391_437),
    PaddleModel("PP-DocBlockLayout", 129_924_055),
    PaddleModel("PP-OCRv5_mobile_det", 4_923_617),
    PaddleModel("PP-OCRv5_mobile_rec", 16_824_734),
    PaddleModel("PP-LCNet_x1_0_table_cls", 6_849_121, "table"),
    PaddleModel("SLANeXt_wired", 365_732_881, "table"),
    PaddleModel("SLANet_plus", 7_984_210, "table"),
    PaddleModel("RT-DETR-L_wired_table_cell_det", 129_924_066, "table"),
    PaddleModel("RT-DETR-L_wireless_table_cell_det", 129_924_069, "table"),
    PaddleModel("PP-LCNet_x1_0_doc_ori", 6_859_373, "doc_orientation"),
    PaddleModel("UVDoc", 32_245_627, "doc_unwarping"),
    PaddleModel("PP-LCNet_x1_0_textline_ori", 6_849_052, "textline_orientation"),
)
"""默认下载的模型，合计约 968 MB。表格方向分类也用 PP-LCNet_x1_0_doc_ori。"""

FORMULA_MODEL: Final = PaddleModel("PP-FormulaNet_plus-L", 731_493_173, "formula")
"""公式识别模型。组合根把它加进清单时，引擎的 features 会带上 formula。"""

RUNTIME_BYTES: Final = 975 * 1024 * 1024
"""运行环境装好后的大小（构建时实测的 site\\ocr 约 975 MB），用作运行环境阶段的进度权重。"""

PADDLE_LANGUAGES: Final = ("zh-Hans", "zh-Hant", "en", "ja")
"""默认识别模型 PP-OCRv5_mobile_rec 覆盖的语言（lst/workers/ocr_worker/languages.py）。"""


def model_url(base_url: str, name: str) -> str:
    """一个模型的下载地址：``<前缀>/<模型名>_infer.tar``。"""
    return f"{base_url.rstrip('/')}/{name}_infer.tar"


def features_of(models: tuple[PaddleModel, ...]) -> tuple[str, ...]:
    """按模型清单给出引擎的能力，取值与 OcrEngineInfo.features 相同。"""
    features = ["text", "layout"]
    kinds = {model.feature for model in models}
    if "table" in kinds:
        features.append("table")
    if "formula" in kinds:
        features.append("formula")
    return tuple(features)


@dataclass(frozen=True, slots=True)
class PaddleInstallPaths:
    r"""安装版的 PaddleOCR 引擎路径，由组合根传入。

    - engine_dir：``<数据目录>\engines\paddle``；
    - model_cache_dir：模型缓存（PADDLE_PDX_CACHE_HOME），模型在其中的 official_models 下；
    - uv：``<安装目录>\tools\uv.exe``；
    - python：``<安装目录>\runtime\python.exe``，uv 按它的版本与平台选择 wheel；
    - requirements：``<安装目录>\app\ocr-paddle-requirements.txt``，构建时按 .venv-ocr 冻结的版本清单。
    """

    engine_dir: Path
    model_cache_dir: Path
    uv: Path
    python: Path
    requirements: Path

    @property
    def site_dir(self) -> Path:
        return self.engine_dir / "site"

    @property
    def site_part(self) -> Path:
        return self.engine_dir / "site.part"

    @property
    def uv_cache(self) -> Path:
        return self.engine_dir / "uv-cache"

    @property
    def marker(self) -> Path:
        return self.engine_dir / MARKER_NAME

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


def model_ready(directory: Path) -> bool:
    """模型目录里有 MODEL_REQUIRED_FILES 列出的全部文件。"""
    return all((directory / name).is_file() for name in MODEL_REQUIRED_FILES)


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
