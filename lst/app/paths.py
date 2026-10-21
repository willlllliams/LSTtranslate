r"""程序目录与数据目录（架构文档 2.9.5 节、第 15 章）。

程序有两种运行方式：
- 安装版：由安装程序装到 %LOCALAPPDATA%\Programs\LSTtranslate，lst 包在安装目录的 app\ 下，
  旁边有构建时写入的 installed.json。数据目录是 %LOCALAPPDATA%\LSTtranslate。
- 源码版（开发用）：源码加虚拟环境运行，数据目录是程序目录下的 data/。源码所在的目录树可能带有
  Low 强制完整性标签，这时虚拟环境里的 Python 写不了用户目录，只能写所在的目录树。

两种方式下 tempfile.gettempdir() 都可能不可写或退回当前目录，临时文件一律放在数据目录的 tmp/ 下。
"""

import os
from dataclasses import dataclass
from pathlib import Path

PROGRAM_DIR = Path(__file__).resolve().parents[2]
r"""lst 包所在的目录：源码版是仓库根目录，安装版是安装目录下的 app\。"""

INSTALL_MARKER = PROGRAM_DIR / "installed.json"
"""安装版的标记文件，由打包脚本写入。"""

DATA_DIR_ENV = "LST_DATA_DIR"
APP_DIR_NAME = "LSTtranslate"


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


@dataclass(frozen=True, slots=True)
class AppPaths:
    """程序用到的全部路径。只计算路径，不创建目录；ensure() 负责创建。"""

    data_dir: Path

    @classmethod
    def resolve(cls, data_dir: str | os.PathLike[str] | None = None) -> "AppPaths":
        r"""按优先级取数据目录：参数 → 环境变量 LST_DATA_DIR → 默认目录。

        默认目录：安装版是 %LOCALAPPDATA%\LSTtranslate，源码版是程序目录下的 data/。
        """
        chosen = data_dir if data_dir is not None else os.environ.get(DATA_DIR_ENV)
        return cls(Path(chosen).resolve() if chosen else default_data_dir())

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
        return self.data_dir / "models" / "nmt"

    @property
    def ocr_models_dir(self) -> Path:
        return self.data_dir / "models" / "ocr"

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
        """创建程序运行需要的目录。"""
        for directory in (
            self.config_file.parent,
            self.logs_dir,
            self.nmt_models_dir,
            self.ocr_models_dir,
            self.tmp_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
