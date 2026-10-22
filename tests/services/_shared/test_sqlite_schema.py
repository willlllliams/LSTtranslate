"""ensure_schema 的建表、版本核对与逐级迁移（架构文档 2.9.3 节）。"""

import sqlite3
from collections.abc import Iterator

import pytest

from lst.services._shared.sqlite import ensure_schema

SCRIPT = "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0);"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", autocommit=True)
    yield connection
    connection.close()


def version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def columns(conn: sqlite3.Connection) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info(items)")]


def test_new_database_is_created_at_target_version(conn: sqlite3.Connection) -> None:
    ensure_schema(conn, 3, SCRIPT, {1: "SELECT 1;", 2: "SELECT 1;"})
    assert version(conn) == 3
    assert columns(conn) == ["id", "name", "size"]


def test_old_database_is_migrated_step_by_step(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT NOT NULL); PRAGMA user_version = 1;"
    )
    conn.execute("INSERT INTO items (title) VALUES ('a')")
    migrations = {
        1: "ALTER TABLE items RENAME COLUMN title TO name;",
        2: "ALTER TABLE items ADD COLUMN size INTEGER NOT NULL DEFAULT 0;",
    }
    ensure_schema(conn, 3, SCRIPT, migrations)
    assert version(conn) == 3
    assert columns(conn) == ["id", "name", "size"]
    assert conn.execute("SELECT name, size FROM items").fetchall() == [("a", 0)]


def test_failed_step_is_rolled_back(conn: sqlite3.Connection) -> None:
    """一级迁移出错时这一级整体回滚，版本号停在出错之前，已经完成的级保持完成。"""
    conn.executescript("CREATE TABLE items (id INTEGER PRIMARY KEY); PRAGMA user_version = 1;")
    migrations = {
        1: "ALTER TABLE items ADD COLUMN name TEXT NOT NULL DEFAULT '';",
        2: "ALTER TABLE items ADD COLUMN size INTEGER; SELECT * FROM missing_table;",
    }
    with pytest.raises(sqlite3.OperationalError):
        ensure_schema(conn, 3, SCRIPT, migrations)
    assert not conn.in_transaction
    assert version(conn) == 2
    assert columns(conn) == ["id", "name"]


@pytest.mark.parametrize(
    ("found", "migrations"),
    [(5, {1: "SELECT 1;"}), (1, {}), (1, {2: "SELECT 1;"})],
    ids=["版本更新", "没有迁移", "缺少一级"],
)
def test_unknown_or_unreachable_version_is_rejected(
    conn: sqlite3.Connection, found: int, migrations: dict[int, str]
) -> None:
    conn.execute(f"PRAGMA user_version = {found}")
    with pytest.raises(sqlite3.DatabaseError, match="本程序只认识版本 3"):
        ensure_schema(conn, 3, SCRIPT, migrations)
    assert version(conn) == found
