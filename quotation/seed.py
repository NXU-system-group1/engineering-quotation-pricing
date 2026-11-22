"""种子数据装载：建库 + 建表 + 灌入模拟/脱敏样例数据（幂等、只追加）。

所有种子价格强制 is_demo=1、source_type='demo_seed'，来源项目名带
「模拟/脱敏样例」字样，均为自造数值，不涉及任何真实商业报价。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db

_SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "seed_prices.json"

_CATALOG_TABLES = (
    "item_catalog",
    "price_history",
    "pricing_rules",
    "quote_records",
    "quote_items",
)


def load_seed(path: str | None = None) -> dict[str, Any]:
    """读取种子 JSON 文件。

    Args:
        path: 自定义种子文件路径；为 None 时读取模块默认的 data/seed_prices.json。

    Returns:
        含 disclaimer / items / prices / rules 四个键的 dict。
    """
    seed_path = Path(path).expanduser().resolve() if path else _SEED_PATH
    with seed_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _now() -> str:
    """返回当前本地时间的 ISO 字符串（秒精度）。"""
    return datetime.now().isoformat(timespec="seconds")


def _insert_catalog(conn: Any, seed: dict[str, Any], now: str) -> dict[str, int]:
    """插入标准项目并返回 catalog_key -> item_catalog.id 的映射（已存在则复用）。"""
    key_to_id: dict[str, int] = {}
    for item in seed["items"]:
        key = (item["canonical_name"], item["material"], item["process"], item["spec"])
        row = conn.execute(
            "SELECT id FROM item_catalog "
            "WHERE canonical_name=? AND material=? AND process=? AND spec=?",
            key,
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                "INSERT INTO item_catalog "
                "(canonical_name, category, material, process, spec, aliases, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item["canonical_name"],
                    item.get("category", ""),
                    item["material"],
                    item["process"],
                    item["spec"],
                    json.dumps(item.get("aliases", []), ensure_ascii=False),
                    now,
                ),
            )
            key_to_id[item["catalog_key"]] = int(cursor.lastrowid)
        else:
            key_to_id[item["catalog_key"]] = int(row["id"])
    return key_to_id


def _insert_prices(conn: Any, seed: dict[str, Any], key_to_id: dict[str, int], now: str) -> None:
    """插入历史价格；强制 is_demo=1、source_type='demo_seed'。"""
    for price in seed["prices"]:
        conn.execute(
            "INSERT INTO price_history "
            "(item_id, price_type, unit_price, pricing_unit, currency, "
            "source_project, quote_date, source_type, source_ref, "
            "construction_note, remark, is_confirmed, is_demo, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key_to_id[price["catalog_key"]],
                price["price_type"],
                price["unit_price"],
                price["pricing_unit"],
                price.get("currency", "CNY"),
                price["source_project"],
                price["quote_date"],
                "demo_seed",
                "",
                price.get("construction_note", ""),
                price.get("remark", ""),
                int(price.get("is_confirmed", 0)),
                1,
                now,
            ),
        )


def _insert_rules(conn: Any, seed: dict[str, Any], key_to_id: dict[str, int], now: str) -> None:
    """插入计价换算规则；catalog_key 为 null 时 item_id 写 NULL 表示通用规则。"""
    for rule in seed["rules"]:
        catalog_key = rule.get("catalog_key")
        item_id = key_to_id.get(catalog_key) if catalog_key else None
        is_confirmed = int(rule.get("is_confirmed", 0))
        conn.execute(
            "INSERT INTO pricing_rules "
            "(item_id, from_quantity_type, from_unit, to_pricing_unit, "
            "conversion_factor, rule_note, is_confirmed, confirmed_by, confirmed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                rule["from_quantity_type"],
                rule["from_unit"],
                rule["to_pricing_unit"],
                rule["conversion_factor"],
                rule.get("rule_note", ""),
                is_confirmed,
                rule.get("confirmed_by", ""),
                now if is_confirmed else "",
                now,
            ),
        )


def _seed_into_conn(conn: Any, seed: dict[str, Any]) -> None:
    """在给定连接上完成全部种子插入（调用方负责事务边界）。"""
    now = _now()
    key_to_id = _insert_catalog(conn, seed, now)
    _insert_prices(conn, seed, key_to_id, now)
    _insert_rules(conn, seed, key_to_id, now)


def init_database(database_path: str, force: bool = False) -> str:
    """建库 + 建表 + 灌种子，返回数据库绝对路径字符串。

    force=True 时才允许删除旧库重建；非 force 且库已存在且有数据时保持幂等，
    不会重复灌入种子（历史价格只追加、不覆盖）。

    Args:
        database_path: 数据库文件路径。
        force: 是否强制删除已有库文件后重建。

    Returns:
        数据库文件的绝对路径字符串。
    """
    path = Path(database_path).expanduser().resolve()
    if force and path.exists():
        path.unlink()

    conn = db.connect(str(path))
    try:
        db.init_schema(conn)
        count = conn.execute("SELECT COUNT(*) AS n FROM item_catalog").fetchone()["n"]
        if count == 0:
            seed = load_seed()
            with db.transaction(conn):
                _seed_into_conn(conn, seed)
    finally:
        conn.close()
    return str(path)


def table_counts(database_path: str) -> dict[str, int]:
    """返回 5 张表的当前行数，便于联调自检。"""
    conn = db.connect(database_path)
    try:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in _CATALOG_TABLES
        }
    finally:
        conn.close()
