"""UpdateSecret 与密钥存储（需求 CFG-04，架构文档 2.9.2 节、第 9 章）。

测试从不接触真实的 Windows 凭据管理器：配置服务用内存存储；KeyringSecretStore 用测试专用的
内存 keyring 后端，要么直接注入，要么临时设为 keyring 的全局后端并在测试后恢复。
"""

import logging
from pathlib import Path

import keyring
import keyring.core
import pytest
from keyring.backend import KeyringBackend
from keyring.compat import properties
from keyring.errors import PasswordDeleteError

from lst.contracts.config import UpdateConfig, UpdateSecret
from lst.contracts.ports import SecretReader
from lst.services.config import KeyringSecretStore, MemorySecretStore, SecretStore, SecretStoreError
from tests.services.config.helpers import MODULE_LOGGER, Harness, Launcher, engine, write

KEY = "sk-deepseek-5c1e9a7b3d2f4e60"
OTHER_KEY = "sk-openai-0a1b2c3d4e5f6a7b"


class InMemoryKeyring(KeyringBackend):
    """测试专用的 keyring 后端。priority 抛出 RuntimeError，keyring 自动选择后端时不会选中它。"""

    @properties.classproperty
    def priority(cls) -> float:
        raise RuntimeError("测试专用后端")

    def __init__(self, *, fail_with: str | None = None) -> None:
        super().__init__()
        self.entries: dict[tuple[str, str], str] = {}
        self.fail_with = fail_with

    def get_password(self, service: str, username: str) -> str | None:
        if self.fail_with is not None:
            raise RuntimeError(self.fail_with)
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.fail_with is not None:
            # 模拟把值带进异常消息的后端。
            raise RuntimeError(f"{self.fail_with}: {password}")
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.entries:
            raise PasswordDeleteError("not found")
        del self.entries[(service, username)]


class FailingStore(MemorySecretStore):
    """写入和删除都失败的存储，异常消息里带着要写入的值。"""

    def set_secret(self, name: str, value: str) -> None:
        raise RuntimeError(f"凭据管理器拒绝写入 {value}")

    def delete_secret(self, name: str) -> bool:
        raise OSError("凭据管理器不可用")


def messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


async def secret_harness(start: Launcher, path: Path, store: SecretStore | None = None) -> Harness:
    return await start(path, store=store if store is not None else MemorySecretStore())


# ---------------------------------------------------------------------- UpdateSecret


async def test_write_secret(start: Launcher, config_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    store = MemorySecretStore()
    harness = await secret_harness(start, config_path, store)

    await harness.send(UpdateSecret("deepseek", KEY))

    assert store.get_secret("deepseek") == KEY
    assert (harness.config.revision, harness.config.secrets_revision) == (2, 1)
    assert harness.rejections == []
    # 日志里只有条目名。
    assert any("deepseek" in message for message in messages(caplog))
    assert all(KEY not in message for message in messages(caplog))
    # 写入的值登记到了脱敏过滤器。
    assert harness.service.redacting_filter.redact(f"key={KEY}") == "key=***"


async def test_delete_secret(start: Launcher, config_path: Path) -> None:
    store = MemorySecretStore({"deepseek": KEY})
    harness = await secret_harness(start, config_path, store)

    await harness.send(UpdateSecret("deepseek", None))

    assert store.get_secret("deepseek") is None
    assert (harness.config.revision, harness.config.secrets_revision) == (2, 1)


async def test_empty_value_deletes(start: Launcher, config_path: Path) -> None:
    store = MemorySecretStore({"deepseek": KEY})
    harness = await secret_harness(start, config_path, store)

    await harness.send(UpdateSecret("deepseek", "  "))

    assert store.get_secret("deepseek") is None
    assert harness.config.secrets_revision == 1


async def test_deleting_missing_entry_publishes_nothing(start: Launcher, config_path: Path) -> None:
    harness = await secret_harness(start, config_path)

    await harness.send(UpdateSecret("nothing-here", None))

    assert len(harness.configs) == 1
    assert harness.rejections == []


async def test_secrets_revision_increments(start: Launcher, config_path: Path) -> None:
    harness = await secret_harness(start, config_path)

    await harness.send(UpdateSecret("a", KEY))
    await harness.send(UpdateSecret("b", OTHER_KEY))
    await harness.send(UpdateConfig({"race": {"mode": "full"}}))
    await harness.send(UpdateSecret("a", None))

    assert [(c.revision, c.secrets_revision) for c in harness.configs] == [
        (1, 0),
        (2, 1),
        (3, 2),
        (4, 2),
        (5, 3),
    ]


async def test_store_failure_is_reported_without_value(
    start: Launcher, config_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    harness = await secret_harness(start, config_path, FailingStore())

    await harness.send(UpdateSecret("deepseek", KEY))
    await harness.send(UpdateSecret("deepseek", None))

    assert harness.locations() == [("secrets", "deepseek"), ("secrets", "deepseek")]
    assert harness.rejections[0].reason == "写入密钥失败：RuntimeError: 凭据管理器拒绝写入 ***"
    assert harness.rejections[1].reason == "删除密钥失败：OSError: 凭据管理器不可用"
    assert len(harness.configs) == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and r.name == MODULE_LOGGER]
    assert len(errors) == 2
    assert all(KEY not in message for message in messages(caplog))
    assert all(KEY not in rejection.reason for rejection in harness.rejections)


async def test_empty_name_is_rejected(start: Launcher, config_path: Path) -> None:
    store = MemorySecretStore()
    harness = await secret_harness(start, config_path, store)

    await harness.send(UpdateSecret(" ", KEY))

    assert harness.locations() == [("secrets", " ")]
    assert store.get_secret(" ") is None
    assert len(harness.configs) == 1


def test_update_secret_repr_hides_value() -> None:
    assert KEY not in repr(UpdateSecret("deepseek", KEY))


# ---------------------------------------------------------------------- 脱敏登记


async def test_referenced_secrets_are_registered_at_start(start: Launcher, config_path: Path) -> None:
    write(
        config_path,
        """\
[[engines]]
id = "deepseek"
kind = "online"
priority = 1
base_url = "https://api.deepseek.com"
secret = "deepseek"
""",
    )
    store = MemorySecretStore({"deepseek": KEY, "unused": OTHER_KEY})

    harness = await start(config_path, store=store)

    redact = harness.service.redacting_filter.redact
    assert redact(f"Bearer {KEY}") == "Bearer ***"
    # 配置里没有引用的条目不读取。
    assert redact(OTHER_KEY) == OTHER_KEY


async def test_secret_referenced_by_update_is_registered(start: Launcher, config_path: Path) -> None:
    store = MemorySecretStore({"openai": OTHER_KEY})
    harness = await start(config_path, store=store)
    assert harness.service.redacting_filter.redact(OTHER_KEY) == OTHER_KEY

    await harness.send(UpdateConfig({"engines": [engine("gpt", 1, kind="online", secret="openai")]}))

    assert harness.service.redacting_filter.redact(OTHER_KEY) == "***"


# ---------------------------------------------------------------------- 存储实现


def test_memory_store() -> None:
    store = MemorySecretStore({"a": "1"})
    reader: SecretReader = store
    assert reader.get_secret("a") == "1"
    store.set_secret("b", "2")
    assert store.delete_secret("b") is True
    assert store.delete_secret("b") is False
    assert store.get_secret("b") is None


def test_keyring_store_with_injected_backend() -> None:
    backend = InMemoryKeyring()
    store = KeyringSecretStore(backend=backend)
    reader: SecretReader = store

    assert reader.get_secret("deepseek") is None
    store.set_secret("deepseek", KEY)
    assert backend.entries == {("LSTtranslate", "deepseek"): KEY}
    assert reader.get_secret("deepseek") == KEY
    assert store.delete_secret("deepseek") is True
    assert store.delete_secret("deepseek") is False
    assert backend.entries == {}


def test_keyring_store_errors_hide_value(caplog: pytest.LogCaptureFixture) -> None:
    store = KeyringSecretStore("Custom", backend=InMemoryKeyring(fail_with="backend down"))

    with pytest.raises(SecretStoreError) as raised:
        store.set_secret("deepseek", KEY)
    assert str(raised.value) == "RuntimeError: backend down: ***"
    # 异常链被切断，原始消息不会随 traceback 进入日志。
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__

    with pytest.raises(SecretStoreError):
        store.delete_secret("deepseek")

    # 读取失败时记 ERROR 并返回 None：SecretReader 的契约里没有错误通道。
    with caplog.at_level(logging.ERROR):
        assert store.get_secret("deepseek") is None
    assert any("deepseek" in message for message in messages(caplog))


def test_keyring_store_uses_global_backend_and_restores_it() -> None:
    previous = keyring.core._keyring_backend
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    try:
        assert keyring.get_keyring() is backend
        store = KeyringSecretStore()
        store.set_secret("deepseek", KEY)
        assert backend.entries == {("LSTtranslate", "deepseek"): KEY}
        assert store.get_secret("deepseek") == KEY
        assert store.delete_secret("deepseek") is True
    finally:
        # 直接恢复模块里保存的后端：原来还没有初始化时恢复成 None，不触发对真实后端的探测。
        keyring.core._keyring_backend = previous
    assert keyring.core._keyring_backend is previous


async def test_service_with_keyring_store(start: Launcher, config_path: Path) -> None:
    backend = InMemoryKeyring()
    harness = await start(config_path, store=KeyringSecretStore(backend=backend))

    await harness.send(UpdateSecret("deepseek", KEY))
    assert backend.entries == {("LSTtranslate", "deepseek"): KEY}

    await harness.send(UpdateSecret("deepseek", None))
    assert backend.entries == {}
    assert harness.config.secrets_revision == 2
