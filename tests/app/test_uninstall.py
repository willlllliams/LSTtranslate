"""lst.app.uninstall：卸载时清理凭据管理器里本程序的 API key，以及用户另选的数据目录与模型目录。

凭据的测试只用假的 advapi32：它按 Win32 的调用约定把凭据数组写进调用方传入的指针，测试从不读写真实的
凭据管理器。目录的测试在临时目录里搭出数据目录、模型目录与位置文件。
"""

import ctypes
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lst.app import uninstall
from lst.app.paths import write_location
from lst.app.uninstall import (
    SERVICE,
    CredentialError,
    configured_models_dir,
    credential_targets,
    purge_secrets,
    purge_user_data,
)

_ERROR_ACCESS_DENIED = 5
_ERROR_NOT_FOUND = 1168
_CRED_TYPE_GENERIC = 1
_CRED_TYPE_DOMAIN_PASSWORD = 2


@dataclass
class FakeAdvapi32:
    """凭据管理器的替身：credentials 是（目标名，类型）列表。

    CredEnumerateW 只支持 filter 为 None（列出全部）；收到别的过滤器时按 Win32 的行为报 ERROR_NOT_FOUND，
    这样旧实现依赖的 ``*@LSTtranslate`` 过滤器在测试里同样拿不到任何条目。
    """

    credentials: list[tuple[str, int]]
    enumerate_error: int | None = None
    delete_errors: dict[str, int] = field(default_factory=dict[str, int])
    filters: list[str | None] = field(default_factory=list[str | None])
    deleted: list[str] = field(default_factory=list[str])
    freed: int = 0
    _keep: list[Any] = field(default_factory=list[Any])

    def CredEnumerateW(self, filter_: str | None, flags: int, count: Any, credentials: Any) -> int:
        self.filters.append(filter_)
        if self.enumerate_error is not None:
            ctypes.set_last_error(self.enumerate_error)
            return 0
        if filter_ is not None or not self.credentials:
            ctypes.set_last_error(_ERROR_NOT_FOUND)
            return 0
        items = (uninstall.Credential * len(self.credentials))()
        names = [ctypes.create_unicode_buffer(name) for name, _ in self.credentials]
        for item, name, (_, kind) in zip(items, names, self.credentials, strict=True):
            item.Type = kind
            item.TargetName = ctypes.cast(name, ctypes.c_wchar_p)
        pointers = (ctypes.POINTER(uninstall.Credential) * len(self.credentials))(
            *(ctypes.pointer(item) for item in items)
        )
        self._keep += [items, names, pointers]
        count._obj.value = len(self.credentials)
        ctypes.c_void_p.from_buffer(credentials._obj).value = ctypes.addressof(pointers)
        return 1

    def CredFree(self, buffer: Any) -> None:
        self.freed += 1

    def CredDeleteW(self, target: str, kind: int, flags: int) -> int:
        error = self.delete_errors.get(target)
        if error is not None:
            ctypes.set_last_error(error)
            return 0
        self.deleted.append(target)
        return 1


def fake(credentials: Iterable[tuple[str, int]], **kwargs: Any) -> FakeAdvapi32:
    return FakeAdvapi32(list(credentials), **kwargs)


STORED = [
    ("LSTtranslate", _CRED_TYPE_GENERIC),  # keyring：最新写入的条目占用服务名本身
    ("deepl@LSTtranslate", _CRED_TYPE_GENERIC),  # 旧条目改用“条目名@服务名”
    ("openai@lsttranslate", _CRED_TYPE_GENERIC),  # 凭据管理器的目标名不区分大小写
    ("LSTtranslate-other", _CRED_TYPE_GENERIC),
    ("other@LSTtranslate.example", _CRED_TYPE_GENERIC),
    ("git:https://github.com", _CRED_TYPE_GENERIC),
    ("x@LSTtranslate", _CRED_TYPE_DOMAIN_PASSWORD),
]


def test_targets_enumerate_everything_and_filter_in_python() -> None:
    api = fake(STORED)
    assert credential_targets(advapi32=api) == ["LSTtranslate", "deepl@LSTtranslate", "openai@lsttranslate"]
    # 前导星号的过滤器不被支持，一律列出全部再自己筛选。
    assert api.filters == [None]
    assert api.freed == 1


def test_no_credentials_is_not_an_error() -> None:
    assert credential_targets(advapi32=fake([])) == []


def test_enumeration_failure_is_raised_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    with pytest.raises(CredentialError):
        credential_targets(advapi32=fake(STORED, enumerate_error=_ERROR_ACCESS_DENIED))
    assert "5" in caplog.text


def test_purge_deletes_matching_generic_credentials() -> None:
    api = fake(STORED)
    result = purge_secrets(SERVICE, advapi32=api)
    assert api.deleted == ["LSTtranslate", "deepl@LSTtranslate", "openai@lsttranslate"]
    assert (result.deleted, result.failed, result.ok) == (3, 0, True)


def test_purge_reports_failures(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    api = fake(STORED, delete_errors={"deepl@LSTtranslate": _ERROR_ACCESS_DENIED})
    result = purge_secrets(SERVICE, advapi32=api)
    assert (result.deleted, result.failed, result.ok) == (2, 1, False)
    # 日志只写错误码，不写目标名（目标名里是用户起的条目名）。
    assert "deepl" not in caplog.text


def test_purge_ignores_credentials_deleted_meanwhile() -> None:
    api = fake(STORED, delete_errors={"LSTtranslate": _ERROR_NOT_FOUND})
    result = purge_secrets(SERVICE, advapi32=api)
    assert result.ok and result.deleted == 2


def test_purge_fails_when_enumeration_fails() -> None:
    result = purge_secrets(SERVICE, advapi32=fake(STORED, enumerate_error=_ERROR_ACCESS_DENIED))
    assert not result.ok


def test_purge_without_credential_manager_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # 非 Windows（没有 advapi32）时什么也不做。
    monkeypatch.setattr(uninstall, "_load_advapi32", lambda: None)
    result = purge_secrets()
    assert result.ok and result.deleted == 0


# ---------------------------------------------------------------- 数据目录与模型目录


def _data_dir(root: Path, models_dir: Path | None = None) -> None:
    (root / "config").mkdir(parents=True)
    storage = f'[storage]\nmodels_dir = "{models_dir.as_posix()}"\n' if models_dir is not None else ""
    (root / "config" / "settings.toml").write_text(storage, encoding="utf-8")
    (root / "logs").mkdir()
    (root / "cache.sqlite3").write_bytes(b"x")
    (root / "models" / "nmt").mkdir(parents=True)


def _models_dir(root: Path) -> None:
    for name in ("nmt", "ocr", "dict", "engines", "runtimes"):
        (root / name).mkdir(parents=True)
        (root / name / "f.bin").write_bytes(b"x")


def test_purge_data_removes_only_what_the_program_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """选的数据目录与模型目录里还放着用户自己的文件：只删程序创建的条目，目录本身不为空就保留。"""
    monkeypatch.setattr(uninstall, "default_data_dir", lambda: tmp_path / "default")
    chosen, models, env_models = tmp_path / "D" / "资料", tmp_path / "E" / "models", tmp_path / "F" / "m"
    _data_dir(chosen, models)
    (chosen / "我的笔记.txt").write_text("别删", encoding="utf-8")
    _models_dir(models)
    (models / "photos").mkdir()
    env_dir = tmp_path / "env-data"
    _data_dir(env_dir, env_models)
    _models_dir(env_models)
    location = tmp_path / "roaming" / "LSTtranslate" / "location.json"
    write_location(location, chosen)

    result = purge_user_data(location=location, environ={"LST_DATA_DIR": str(env_dir)})

    assert result.ok and result.deleted > 0
    assert sorted(p.name for p in chosen.iterdir()) == ["我的笔记.txt"]
    assert sorted(p.name for p in models.iterdir()) == ["photos"]
    assert not env_models.exists()  # 模型子目录删完后目录为空，一并删除
    assert env_dir.is_dir()  # LST_DATA_DIR 指定的数据目录由卸载脚本整个删除，这里不动
    assert not location.exists() and not location.parent.exists()


def test_purge_data_ignores_a_location_that_is_not_a_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """位置文件指向的目录里没有 config/settings.toml（例如被用户换成了别的文件夹）：一个文件也不删。"""
    monkeypatch.setattr(uninstall, "default_data_dir", lambda: tmp_path / "default")
    other = tmp_path / "other"
    (other / "logs").mkdir(parents=True)
    location = tmp_path / "location.json"
    write_location(location, other)
    result = purge_user_data(location=location, environ={})
    assert result.ok and result.deleted == 0
    assert (other / "logs").is_dir()
    assert not location.exists()


def test_purge_data_reads_the_model_dir_of_the_default_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认数据目录由卸载脚本删除，但它的配置可能把模型目录指到了别的盘，那里的模型子目录要在这里删。"""
    default, models = tmp_path / "default", tmp_path / "G" / "models"
    _data_dir(default, models)
    _models_dir(models)
    monkeypatch.setattr(uninstall, "default_data_dir", lambda: default)
    result = purge_user_data(location=tmp_path / "none.json", environ={})
    assert result.ok and not models.exists()
    assert (default / "config" / "settings.toml").is_file()
    assert configured_models_dir(default) == models
