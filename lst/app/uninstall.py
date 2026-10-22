"""卸载时的用户数据清理（需求 INS-06，架构文档第 15 章）。

卸载程序在用户选择“同时删除用户数据”时，先运行 `launcher\\worker.py purge-secrets` 删除凭据管理器中
本程序保存的 API key，再运行 `launcher\\worker.py purge-data` 删除用户在设置里选的数据目录与模型目录
（``purge_user_data``），最后由安装程序删除默认数据目录。

用户选的目录可能是“D:\\资料”这样还放着别的文件的目录，所以 ``purge_user_data`` 不整个删除它们，
只删程序创建的条目（``lst.app.storage.data_entries``、``model_entries``），删完目录为空时再删目录本身。

keyring 的 Windows 后端（WinVaultKeyring）把通用凭据的目标名设为服务名本身（LSTtranslate）。
同一服务下再写入另一个条目时，它先把原来占着服务名的旧条目改存到“旧条目名@服务名”，新条目占用服务名。
所以本程序的凭据有两种目标名：服务名本身，以及以“@服务名”结尾的组合名，清理时两种都删。

CredEnumerateW 的过滤器只支持末尾的星号，``*@LSTtranslate`` 这样的前导星号匹配不到任何条目。
这里不带过滤器列出当前用户的全部凭据，再在 Python 里按目标名筛选（不区分大小写，与凭据管理器一致）。
"""

import contextlib
import ctypes
import ctypes.wintypes
import logging
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from lst.app.paths import DATA_DIR_ENV, default_data_dir, location_file, read_location
from lst.app.storage import (
    data_entries,
    default_models_root,
    is_inside,
    model_entries,
    remove_paths,
    same_path,
)

logger = logging.getLogger(__name__)

SERVICE = "LSTtranslate"
_CRED_TYPE_GENERIC = 1
_ERROR_NOT_FOUND = 1168


class Credential(ctypes.Structure):
    """Win32 的 CREDENTIALW 结构。"""

    _fields_ = (
        ("Flags", ctypes.wintypes.DWORD),
        ("Type", ctypes.wintypes.DWORD),
        ("TargetName", ctypes.wintypes.LPWSTR),
        ("Comment", ctypes.wintypes.LPWSTR),
        ("LastWritten", ctypes.wintypes.FILETIME),
        ("CredentialBlobSize", ctypes.wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
        ("Persist", ctypes.wintypes.DWORD),
        ("AttributeCount", ctypes.wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.wintypes.LPWSTR),
        ("UserName", ctypes.wintypes.LPWSTR),
    )


class Advapi32(Protocol):
    """本模块用到的 advapi32 函数。测试注入假实现，从不触碰真实的凭据管理器。"""

    def CredEnumerateW(self, filter_: str | None, flags: int, count: Any, credentials: Any) -> int: ...

    def CredFree(self, buffer: Any) -> None: ...

    def CredDeleteW(self, target: str, kind: int, flags: int) -> int: ...


class CredentialError(Exception):
    """枚举凭据失败（ERROR_NOT_FOUND 表示没有凭据，不算失败）。"""


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """清理结果：删除的条目数与删除失败的条目数；enumerated 为假表示连枚举都失败了。"""

    deleted: int = 0
    failed: int = 0
    enumerated: bool = True

    @property
    def ok(self) -> bool:
        return self.enumerated and self.failed == 0


def _load_advapi32() -> Advapi32 | None:
    if sys.platform != "win32":
        return None
    return ctypes.WinDLL("advapi32", use_last_error=True)  # pyright: ignore[reportReturnType]


def belongs_to(target: str, service: str = SERVICE) -> bool:
    """目标名是否属于本程序：等于服务名，或以“@服务名”结尾。"""
    name, suffix = target.casefold(), service.casefold()
    return name == suffix or name.endswith("@" + suffix)


def credential_targets(service: str = SERVICE, *, advapi32: Advapi32 | None = None) -> list[str]:
    """列出本程序在凭据管理器里的全部通用凭据目标名，排序后返回。

    枚举失败（ERROR_NOT_FOUND 之外的错误）时记 WARNING 并抛出 CredentialError。
    """
    api = advapi32 if advapi32 is not None else _load_advapi32()
    if api is None:
        return []
    count = ctypes.wintypes.DWORD()
    creds = ctypes.POINTER(ctypes.POINTER(Credential))()
    if not api.CredEnumerateW(None, 0, ctypes.byref(count), ctypes.byref(creds)):
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            return []
        logger.warning("枚举凭据失败（错误码 %s）", error)
        raise CredentialError(error)
    targets: set[str] = set()
    try:
        for index in range(count.value):
            credential = creds[index].contents
            name = credential.TargetName
            if credential.Type == _CRED_TYPE_GENERIC and name and belongs_to(name, service):
                targets.add(name)
    finally:
        api.CredFree(creds)
    return sorted(targets)


def purge_secrets(service: str = SERVICE, *, advapi32: Advapi32 | None = None) -> PurgeResult:
    """删除本程序在凭据管理器里保存的全部 API key。日志只写数量和错误码，不写目标名。

    删除时报 ERROR_NOT_FOUND（条目在枚举之后已被删掉）不算失败。
    """
    api = advapi32 if advapi32 is not None else _load_advapi32()
    if api is None:
        return PurgeResult()
    try:
        targets = credential_targets(service, advapi32=api)
    except CredentialError:
        return PurgeResult(enumerated=False)
    deleted = failed = 0
    for target in targets:
        if api.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
            deleted += 1
            continue
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            continue
        failed += 1
        logger.warning("删除一条凭据失败（错误码 %s）", error)
    logger.info("已删除 %s 条凭据，%s 条删除失败", deleted, failed)
    return PurgeResult(deleted, failed)


# ---------------------------------------------------------------- 数据目录与模型目录


@dataclass(frozen=True, slots=True)
class DataPurgeResult:
    """删除的条目数与没能删掉的条目数。"""

    deleted: int = 0
    failed: int = 0

    @property
    def ok(self) -> bool:
        return self.failed == 0


def looks_like_data_dir(directory: Path) -> bool:
    """目录里有 config/settings.toml：是本程序的数据目录。"""
    return (directory / "config" / "settings.toml").is_file()


def configured_models_dir(data_dir: Path) -> Path | None:
    """数据目录的配置里 [storage].models_dir 指定的模型目录；没有指定、读不出来时为 None。"""
    try:
        with (data_dir / "config" / "settings.toml").open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    section = document.get("storage")
    value = cast(dict[str, object], section).get("models_dir") if isinstance(section, dict) else None
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        return None
    return Path(value)


def _rmdir_if_empty(directory: Path) -> None:
    with contextlib.suppress(OSError):
        directory.rmdir()


def purge_user_data(
    *, location: Path | None = None, environ: Mapping[str, str] | None = None
) -> DataPurgeResult:
    r"""删除用户选的数据目录、各数据目录配置里指定的模型目录中由程序创建的内容，以及位置文件。

    默认数据目录（%LOCALAPPDATA%\LSTtranslate）与 LST_DATA_DIR 指定的目录由卸载脚本整个删除，这里只读它们的
    配置，找出放在别处的模型目录。位置文件指向的目录里没有 config\settings.toml 时不当作数据目录，不删。
    """
    env = os.environ if environ is None else environ
    pointer = location if location is not None else location_file()
    data_dirs = [default_data_dir()]
    from_env = env.get(DATA_DIR_ENV, "").strip()
    if from_env and Path(from_env).is_absolute() and looks_like_data_dir(Path(from_env)):
        data_dirs.append(Path(from_env))
    targets: list[Path] = []
    emptied: list[Path] = []
    chosen = read_location(pointer)
    if chosen is not None and looks_like_data_dir(chosen):
        data_dirs.append(chosen)
        targets.extend(data_entries(chosen))
        targets.extend(model_entries(default_models_root(chosen)))
        emptied.extend((default_models_root(chosen), chosen))
    for data_dir in data_dirs:
        root = configured_models_dir(data_dir)
        if root is None or same_path(root, data_dir) or is_inside(root, data_dir):
            continue  # 放在数据目录里面的模型目录随数据目录一起删
        targets.extend(model_entries(root))
        emptied.append(root)
    left = remove_paths(targets)
    for directory in emptied:
        _rmdir_if_empty(directory)
    try:
        pointer.unlink(missing_ok=True)
    except OSError:
        left.append(pointer)
    _rmdir_if_empty(pointer.parent)
    for path in left:
        logger.warning("没能删除 %s", path)
    logger.info("已删除 %d 项用户数据，%d 项删除失败", len(targets) - len(left), len(left))
    return DataPurgeResult(len(targets) - len(left), len(left))
