r"""存储位置：数据目录与模型目录的查询、更改与重启（需求 CFG-06、CFG-07、MOD-04，架构文档 2.9.5、8.5 节）。

数据目录存放配置、译文缓存、统计、日志与界面状态；模型目录存放本地翻译模型（nmt）、OCR 模型（ocr）、
OCR 引擎的运行环境（engines）、词典包（dict）与显卡运行库（runtimes），默认是数据目录下的 models。

- ``StorageLocations``（State）：组合根发布当前的两个目录、数据目录的来源、重启后生效的新数据目录、
  进行中的复制或移动的进度，以及最近一次更改失败的原因；
- ``ChangeDataDir``（Command）：把数据目录改到 target。配置文件就在数据目录里，选择结果写在数据目录之外的
  位置文件里，重启程序后生效。copy 为真时先在后台把现有数据复制过去，复制失败时不切换；
- ``ChangeModelsDir``（Command）：把模型目录改到 target，target 为空串表示恢复默认位置。move 为真时先停下
  本地翻译与文字识别进程，把现有模型移过去，再按新目录重新扫描并启动进程，不需要重启程序；移动失败时
  移回原处；
- ``RestartApp``（Command）：退出后以同样的命令行重新启动程序。

路径一律是绝对路径的字符串。
"""

from dataclasses import dataclass
from enum import StrEnum

from lst.contracts.messages import Command, State


class DataDirSource(StrEnum):
    """本次运行的数据目录从哪里来，优先级从高到低。"""

    ARGUMENT = "argument"
    """命令行参数 ``--data-dir``。"""
    ENVIRONMENT = "environment"
    """环境变量 ``LST_DATA_DIR``。"""
    CHOSEN = "chosen"
    """用户在设置里选择、记在位置文件里的目录。"""
    DEFAULT = "default"
    """默认位置：安装版是 %LOCALAPPDATA%\\LSTtranslate，源码版是程序目录下的 data。"""

    @property
    def fixed(self) -> bool:
        """由命令行参数或环境变量指定：设置里不能更改，改了也不会生效。"""
        return self in (DataDirSource.ARGUMENT, DataDirSource.ENVIRONMENT)


class StorageKind(StrEnum):
    DATA = "data"
    MODELS = "models"


class StorageErrorCode(StrEnum):
    """更改失败的原因。界面按 ``storage.error.<值>`` 取说明文字。"""

    FIXED = "fixed"
    """数据目录由命令行参数或环境变量指定。"""
    RELATIVE = "relative"
    """不是绝对路径。"""
    NESTED = "nested"
    """新目录在当前目录里面。"""
    NOT_WRITABLE = "not_writable"
    """新目录无法创建或写不进文件（权限、只读介质、Low 完整性等）。"""
    NO_SPACE = "no_space"
    """新目录所在的磁盘空间不够放下要复制或移动的内容。"""
    OCCUPIED = "occupied"
    """新目录里已经有同名的数据或模型。"""
    COPY_FAILED = "copy_failed"
    MOVE_FAILED = "move_failed"
    SAVE_FAILED = "save_failed"
    """写位置文件或配置文件失败。"""
    BUSY = "busy"
    """另一次复制或移动还没有结束。"""


@dataclass(frozen=True, slots=True)
class StorageProgress:
    """进行中的复制（数据目录）或移动（模型目录）。total_bytes 为 0 表示还在统计大小。"""

    kind: StorageKind
    target: str
    done_bytes: int = 0
    total_bytes: int = 0


@dataclass(frozen=True, slots=True)
class StorageFailure:
    """最近一次更改失败。detail 是给日志与界面的技术细节（例如系统错误信息），可以为空。"""

    kind: StorageKind
    code: StorageErrorCode
    target: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class StorageLocations(State):
    """当前的数据目录与模型目录。

    - data_source：数据目录的来源，``fixed`` 时设置里不能更改；
    - pending_data_dir：已经选好、重启程序后才生效的数据目录，没有时为 None；
    - models_default：模型目录是否是默认位置（数据目录下的 models）；
    - task：进行中的复制或移动；failure：最近一次失败，下一次更改开始时清除。
    """

    data_dir: str
    data_source: DataDirSource
    models_dir: str
    models_default: bool = True
    pending_data_dir: str | None = None
    task: StorageProgress | None = None
    failure: StorageFailure | None = None


@dataclass(frozen=True, slots=True)
class ChangeDataDir(Command):
    target: str
    copy: bool = True


@dataclass(frozen=True, slots=True)
class ChangeModelsDir(Command):
    target: str
    move: bool = True


@dataclass(frozen=True, slots=True)
class RestartApp(Command):
    """退出并以同样的命令行重新启动程序。"""


__all__ = [
    "ChangeDataDir",
    "ChangeModelsDir",
    "DataDirSource",
    "RestartApp",
    "StorageErrorCode",
    "StorageFailure",
    "StorageKind",
    "StorageLocations",
    "StorageProgress",
]
