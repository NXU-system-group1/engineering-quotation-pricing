"""数据库连接、建库、事务与轻量 CRUD 封装（零第三方依赖，仅标准库）。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SchemaParams = tuple[Any, ...] | list[Any] | dict[str, Any]


def connect(database_path: str) -> sqlite3.Connection:
    """打开（或创建）SQLite 数据库。

    父目录自动创建；row_factory=sqlite3.Row；每个连接强制开启外键约束。

    Args:
        database_path: 数据库文件路径（相对或绝对）。

    Returns:
        已配置好的 sqlite3.Connection。
    """
    path = Path(database_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """读取同目录下的 schema.sql 并执行建表、索引与只追加触发器。

    Args:
        conn: 由 connect() 返回的连接。
    """
    schema_path = Path(__file__).with_name("schema.sql")
    script = schema_path.read_text(encoding="utf-8")
    conn.executescript(script)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """事务上下文：异常自动 rollback 并原样抛出，正常结束自动 commit。

    Args:
        conn: 由 connect() 返回的连接。

    Yields:
        同一个连接，供 with 体内执行写操作。
    """
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def execute(conn: sqlite3.Connection, sql: str, params: SchemaParams = ()) -> sqlite3.Cursor:
    """执行单条写语句并返回 cursor（不自动提交，交由 transaction 管理）。"""
    return conn.execute(sql, params)


def fetch_all(conn: sqlite3.Connection, sql: str, params: SchemaParams = ()) -> list[sqlite3.Row]:
    """执行查询并返回全部行。"""
    return conn.execute(sql, params).fetchall()


def fetch_one(conn: sqlite3.Connection, sql: str, params: SchemaParams = ()) -> sqlite3.Row | None:
    """执行查询并返回首行，无结果时返回 None。"""
    return conn.execute(sql, params).fetchone()
