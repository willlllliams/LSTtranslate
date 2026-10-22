r"""程序目录、数据目录与模型目录（架构文档 2.9.5 节、第 15 章，需求 CFG-06、CFG-07、MOD-04）。

程序有两种运行方式：
- 安装版：由安装程序装到 %LOCALAPPDATA%\Programs\LSTtranslate，lst 包在安装目录的 app\ 下，
  旁边有构建时写入的 installed.json。默认数据目录是 %LOCALAPPDATA%\LSTtranslate。
- 源码版（开发用）：源码加虚拟环境运行，默认数据目录是程序目录下的 data/。源码所在的目录树可能带有
  Low 强制完整性标签，这时虚拟环境里的 Python 写不了用户目录，只能写所在的目录树。

数据目录按优先级取：命令行参数 ``--data-dir`` → 环境变量 ``LST_DATA_DIR`` → 位置文件 → 默认位置。
位置文件记着用户在设置里选的数据目录。配置文件本身在数据目录里，所以位置文件必须放在数据目录之外：

- 安装版：``%APPDATA%\LSTtranslate\location.json``。它与默认数据目录（%LOCALAPPDATA% 下）不在同一个目录树里，
  用户清理旧数据目录时不会连带删掉它；卸载时选“保留用户数据”同样保留它，重新安装后继续用选好的目录；
- 源码版：``data/location.json``。源码版可能以 Low 完整性运行，写不了 %APPDATA%，只能写程序目录树。

模型目录默认是数据目录下的 models，可以在配置的 ``[storage].models_dir`` 里改到别处。本地翻译模型（nmt）、
OCR 模型（ocr）、词典包（dict）、OCR 引擎的运行环境（engines）与显卡运行库（runtimes/cublas12）都在它下面。

两种方式下 tempfile.gettempdir() 都可能不可写或退回当前目录，临时文件一律放在数据目录的 tmp/ 下。
"""

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from lst.contracts.storage import DataDirSource

logger = logging.getLogger(__name__)

PROGRAM_DIR = Path(__file__).resolve().parents[2]
r"""lst 包所在的目录：源码版是仓库根目录，安装版是安装目录下的 app\。"""

INSTALL_MARKER = PROGRAM_DIR / "installed.json"
"""安装版的标记文件，由打包脚本写入。"""

DATA_DIR_ENV = "LST_DATA_DIR"
APP_DIR_NAME = "LSTtranslate"
LOCATION_FILE_NAME = "location.json"
MODELS_DIR_NAME = "models"
MODEL_SUBDIRS = ("nmt", "ocr", "dict", "engines", "runtimes")
"""模型目录下由程序管理的子目录。移动模型目录、卸载时删除用户数据，都只处理这几个子目录。"""


def is_installed() -> bool:
    return INSTALL_MARKER.is_file()


def install_root() -> Path:
    r"""安装版的安装目录（app\、runtime\、launcher\ 的上一级）；源码版返回程序目录本身。"""
    return PROGRAM_DIR.parent if is_installed() else PROGRAM_DIR


def default_data_dir() -> Path:
    if is_installed():
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_DIR_NAME
    return PROGRAM_DIR / "data"


def location_file() -> Path:
    r"""记录用户所选数据目录的位置文件：安装版在 %APPDATA%\LSTtranslate，源码版在 data/ 下。"""
    if is_installed():
        roaming = os.environ.get("APPDATA")
        base = Path(roaming) if roaming else Path.home() / "AppData" / "Roaming"
        return base / APP_DIR_NAME / LOCATION_FILE_NAME
    return PROGRAM_DIR / "data" / LOCATION_FILE_NAME


def read_location(path: Path) -> Path | None:
    """读位置文件里的数据目录。文件不存在、内容不是 ``{"data_dir": "<绝对路径>"}`` 时返回 None。"""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("位置文件 %s 无法读取，使用默认数据目录：%s", path, exc)
        return None
    value = cast(dict[str, object], raw).get("data_dir") if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        logger.warning("位置文件 %s 里没有有效的数据目录，使用默认数据目录", path)
        return None
    return Path(value)


def write_location(path: Path, data_dir: Path | None) -> None:
    """写位置文件：先写临时文件再原子改名。data_dir 为 None 表示回到默认位置，删除位置文件。

    失败时抛出 OSError。
    """
    if data_dir is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps({"data_dir": str(data_dir)}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(part, path)


@dataclass(frozen=True, slots=True)
class DataDirChoice:
    """本次运行的数据目录、它的来源，以及位置文件的路径（设置里更改数据目录时写它）。

    fallback 是位置文件指向、但这次无法使用的目录（例如移动硬盘没有接上）；这时 path 是默认位置。
    """

    path: Path
    source: DataDirSource
    location: Path
    fallback: Path | None = None


def choose_data_dir(
    argument: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    location: Path | None = None,
) -> DataDirChoice:
    """按优先级选数据目录：参数 → 环境变量 LST_DATA_DIR → 位置文件 → 默认位置。

    位置文件指向的目录无法创建时退回默认位置，并在 fallback 里记下原来的目录。
    """
    env = os.environ if environ is None else environ
    pointer = location_file() if location is None else location
    if argument:
        return DataDirChoice(Path(argument).resolve(), DataDirSource.ARGUMENT, pointer)
    from_env = env.get(DATA_DIR_ENV)
    if from_env:
        return DataDirChoice(Path(from_env).resolve(), DataDirSource.ENVIRONMENT, pointer)
    chosen = read_location(pointer)
    if chosen is not None:
        try:
            chosen.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("位置文件指向的数据目录 %s 无法使用，本次改用默认数据目录：%s", chosen, exc)
            return DataDirChoice(default_data_dir(), DataDirSource.DEFAULT, pointer, fallback=chosen)
        return DataDirChoice(chosen, DataDirSource.CHOSEN, pointer)
    return DataDirChoice(default_data_dir(), DataDirSource.DEFAULT, pointer)


@dataclass(frozen=True, slots=True)
class AppPaths:
    """程序用到的全部路径。只计算路径，不创建目录；ensure()、ensure_models() 负责创建。

    models_dir 是配置里指定的模型目录，None 表示默认位置（数据目录下的 models）。
    """

    data_dir: Path
    models_dir: Path | None = None

    @classmethod
    def resolve(
        cls, data_dir: str | os.PathLike[str] | None = None, *, location: Path | None = None
    ) -> "AppPaths":
        r"""按 ``choose_data_dir`` 的优先级取数据目录。

        默认目录：安装版是 %LOCALAPPDATA%\LSTtranslate，源码版是程序目录下的 data/。
        """
        return cls(choose_data_dir(data_dir, location=location).path)

    def with_models_dir(self, models_dir: str | os.PathLike[str] | None) -> "AppPaths":
        """换一个模型目录。空串与 None 都表示默认位置。"""
        return replace(self, models_dir=Path(models_dir) if models_dir else None)

    @property
    def default_models_dir(self) -> Path:
        return self.data_dir / MODELS_DIR_NAME

    @property
    def models_root(self) -> Path:
        """当前的模型目录。"""
        return self.models_dir if self.models_dir is not None else self.default_models_dir

    @property
    def config_file(self) -> Path:
        return self.data_dir / "config" / "settings.toml"

    @property
    def cache_db(self) -> Path:
        return self.data_dir / "cache.sqlite3"

    @property
    def stats_db(self) -> Path:
        return self.data_dir / "stats.sqlite3"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def nmt_models_dir(self) -> Path:
        return self.models_root / "nmt"

    @property
    def ocr_models_dir(self) -> Path:
        return self.models_root / "ocr"

    @property
    def dict_models_dir(self) -> Path:
        """词典包目录：模型管理模块把词典包装在这里，字典翻译引擎从这里读取。"""
        return self.models_root / "dict"

    @property
    def engines_dir(self) -> Path:
        """需要下载的 OCR 引擎（RapidOCR、PaddleOCR）的运行环境，每个引擎一个子目录。"""
        return self.models_root / "engines"

    @property
    def nmt_runtime_dir(self) -> Path:
        """本地翻译的显卡运行库（cuBLAS 12）目录，设置里按需下载（需求 MOD-06）。"""
        return self.models_root / "runtimes" / "cublas12"

    @property
    def ui_state_file(self) -> Path:
        return self.data_dir / "ui_state.ini"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "lsttranslate.lock"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure(self) -> None:
        """创建数据目录里程序运行需要的目录。模型目录在读到配置之后由 ensure_models 创建。"""
        for directory in (self.config_file.parent, self.logs_dir, self.tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def ensure_models(self) -> None:
        """创建模型目录下的模型子目录。"""
        for directory in (self.nmt_models_dir, self.ocr_models_dir, self.dict_models_dir):
            directory.mkdir(parents=True, exist_ok=True)
