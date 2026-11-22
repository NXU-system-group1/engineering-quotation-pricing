"""对外接口层：报价生成、草案改价、人工确认回写（零第三方依赖）。

红线落地：
- price_history 只 INSERT，绝不 UPDATE/DELETE（DB 另有触发器兜底）。
- 未确认价格不写入正式历史价；确认回写在单个事务内完成，失败 rollback
  并抛 QuoteSaveError("数据库保存失败：...")。
- 无匹配不自行生成价格；相似匹配仅参考价，不计入 total_amount。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import db, matching, pricing
from .models import (
    FUZZY_THRESHOLD,
    PRICE_TYPES,
    QUANTITY_TYPES,
    load_recognition_result,
    normalize_recognition_result,
    validate_recognition_result,
)
from .seed import init_database, table_counts
from .xlsx import export_quote_excel


class QuoteSaveError(Exception):
    """确认回写失败：事务已回滚，历史价格未写入。"""


_UNIT_BLOCK_KEYWORDS: tuple[str, ...] = (
    "不属于同一维度",
    "换算规则未经确认",
    "禁止换算",
    "计价单位与识图工程量单位不一致",
    "无法识别维度",
)

_INSERT_RECORD_SQL = (
    "INSERT INTO quote_records "
    "(quote_id, project_id, project_name, price_type, status, generated_at, "
    "confirmed_by, confirmed_at, total_amount, currency, remark) "
    "VALUES (" + ", ".join(["?"] * 11) + ")"
)

_INSERT_ITEM_COLUMNS = (
    "quote_id, item_id, item_name, material, process, spec, "
    "quantity_value, quantity_type, quantity_unit, review_status, "
    "pricing_quantity, pricing_unit, recommended_unit_price, final_unit_price, "
    "price_history_id, match_status, match_score, amount, provisional_amount, "
    "needs_confirmation, messages, candidates, construction_note, remark, "
    "raw_json, created_at"
)
_INSERT_ITEM_SQL = (
    f"INSERT INTO quote_items ({_INSERT_ITEM_COLUMNS}) "
    "VALUES (" + ", ".join(["?"] * 26) + ")"
)

_INSERT_HISTORY_COLUMNS = (
    "item_id, price_type, unit_price, pricing_unit, currency, "
    "source_project, quote_date, source_type, source_ref, "
    "construction_note, remark, is_confirmed, is_demo, created_at"
)
_INSERT_HISTORY_SQL = (
    f"INSERT INTO price_history ({_INSERT_HISTORY_COLUMNS}) "
    "VALUES (" + ", ".join(["?"] * 14) + ")"
)

_UPDATE_ITEM_SQL = (
    "UPDATE quote_items SET final_unit_price=?, amount=?, pricing_quantity=?, "
    "needs_confirmation=?, match_status=?, messages=?, remark=? WHERE id=?"
)
_UPDATE_RECORD_SQL = (
    "UPDATE quote_records SET status='confirmed', confirmed_by=?, "
    "confirmed_at=?, total_amount=?, remark=? WHERE quote_id=?"
)
_UPDATE_TOTAL_SQL = "UPDATE quote_records SET total_amount=? WHERE quote_id=?"


def _now() -> str:
    """当前本地时间 ISO 字符串（秒精度）。"""
    return datetime.now().isoformat(timespec="seconds")


def _abs_path(database_path: str) -> str:
    """返回数据库路径的绝对路径字符串。"""
    return str(Path(database_path).expanduser().resolve())


def _json_dumps(value: Any) -> str:
    """JSON 文本化（保留中文，非标准类型退化为字符串）。"""
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(text: Any, default: Any) -> Any:
    """安全地从 DB 文本还原 JSON；失败时返回默认值。"""
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def _sum_amounts(rows: list[dict[str, Any]], key: str) -> float:
    """用 Decimal 累加指定金额字段后转 float，避免二进制浮点误差。"""
    total = Decimal("0")
    for row in rows:
        value = row.get(key)
        if value is not None:
            total += Decimal(str(value))
    return float(total)


def _default_warnings() -> list[str]:
    """本版固定的业务规则/数据声明提示。"""
    return [
        "本次使用模拟/脱敏价格数据，非真实商业报价",
        "工程量取整/损耗系数/跨视图合并规则待蒋确认，本版未启用",
        "相似匹配阈值与同名多历史价取价策略待蒋确认",
    ]


def _is_unit_blocked(row: dict[str, Any]) -> bool:
    """判断该行是否因计价单位问题被拦截。"""
    messages = [str(m) for m in (row.get("messages") or [])]
    return any(
        keyword in message
        for message in messages
        for keyword in _UNIT_BLOCK_KEYWORDS
    )


def _pending_reason(row: dict[str, Any]) -> str:
    """按优先级给出挂起原因（业务规则不猜测，只做明确分类）。"""
    if row.get("validation_errors"):
        return "识图结果校验未通过"
    quantity = row.get("quantity") or {}
    if quantity.get("review_status") != "confirmed":
        return "工程量未经人工确认"
    status = str(row.get("match_status") or "")
    if status == "fuzzy":
        return "相似匹配，价格需人工确认"
    if _is_unit_blocked(row):
        return "计价单位不匹配或无已确认换算规则"
    if status == "none":
        return "暂无可靠价格，等待人工填写"
    return "需人工确认"


def _build_pending_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从明细行生成待确认项目列表。"""
    pending: list[dict[str, Any]] = []
    for row in items:
        if not row.get("needs_confirmation"):
            continue
        pending.append(
            {
                "item_id": row.get("item_id", ""),
                "item_name": row.get("item_name", ""),
                "reason": _pending_reason(row),
                "message": "；".join(
                    str(m) for m in (row.get("messages") or [])
                ),
                "match_status": row.get("match_status", ""),
            }
        )
    return pending


def _catalog_ready(conn: sqlite3.Connection) -> bool:
    """检查 item_catalog 是否存在且有数据。"""
    try:
        row = db.fetch_one(conn, "SELECT COUNT(*) AS n FROM item_catalog")
    except sqlite3.Error:
        return False
    return row is not None and int(row["n"]) > 0


def _item_insert_values(row: dict[str, Any], quote_id: str, created_at: str) -> tuple[Any, ...]:
    """把完整明细行映射成 quote_items 的 26 个插入值。"""
    quantity = row.get("quantity") or {}
    return (
        quote_id,
        row.get("item_id", ""),
        row.get("item_name", ""),
        row.get("material", ""),
        row.get("process", ""),
        row.get("spec", ""),
        quantity.get("value"),
        quantity.get("type", ""),
        quantity.get("unit", ""),
        quantity.get("review_status", ""),
        row.get("pricing_quantity"),
        row.get("pricing_unit", ""),
        row.get("recommended_unit_price"),
        row.get("final_unit_price"),
        row.get("price_history_id"),
        row.get("match_status", ""),
        row.get("match_score"),
        row.get("amount"),
        row.get("provisional_amount"),
        1 if row.get("needs_confirmation") else 0,
        _json_dumps(row.get("messages") or []),
        _json_dumps(row.get("candidates") or []),
        row.get("construction_note", ""),
        row.get("remark", ""),
        _json_dumps(row.get("raw") or {}),
        created_at,
    )


def _load_price_source(conn: sqlite3.Connection, price_history_id: Any) -> dict | None:
    """按 id 从 price_history 还原完整可追溯来源字典。"""
    if price_history_id is None:
        return None
    row = db.fetch_one(
        conn, "SELECT * FROM price_history WHERE id=?", (price_history_id,)
    )
    if row is None:
        return None
    return matching._source_from_row(row)


def _row_to_item(conn: sqlite3.Connection, r: sqlite3.Row) -> dict[str, Any]:
    """把 quote_items 的一行还原成与 generate_quote 一致的明细行 dict。

    manual（人工填写）行的 price_source 置 None，但 price_history_id 仍保留
    在 DB 中，仅作为原推荐价追溯线索。
    """
    source = (
        None
        if r["match_status"] == "manual"
        else _load_price_source(conn, r["price_history_id"])
    )
    return {
        "item_id": r["item_id"],
        "item_name": r["item_name"],
        "material": r["material"],
        "process": r["process"],
        "spec": r["spec"],
        "quantity": {
            "value": r["quantity_value"],
            "type": r["quantity_type"],
            "unit": r["quantity_unit"],
            "review_status": r["review_status"],
        },
        "pricing_quantity": r["pricing_quantity"],
        "pricing_unit": r["pricing_unit"],
        "recommended_unit_price": r["recommended_unit_price"],
        "final_unit_price": r["final_unit_price"],
        "price_history_id": r["price_history_id"],
        "price_source": source,
        "match_status": r["match_status"],
        "match_score": r["match_score"],
        "candidates": _json_loads(r["candidates"], []),
        "amount": r["amount"],
        "provisional_amount": r["provisional_amount"],
        "needs_confirmation": bool(r["needs_confirmation"]),
        "messages": _json_loads(r["messages"], []),
        "construction_note": r["construction_note"],
        "remark": r["remark"],
        "raw": _json_loads(r["raw_json"], {}),
        "validation_errors": [],
    }


def _recalc_total(conn: sqlite3.Connection, quote_id: str) -> float:
    """按当前 quote_items.amount 重算并写回 quote_records.total_amount。"""
    rows = db.fetch_all(
        conn, "SELECT amount FROM quote_items WHERE quote_id=?", (quote_id,)
    )
    total = Decimal("0")
    for row in rows:
        if row["amount"] is not None:
            total += Decimal(str(row["amount"]))
    amount = float(total)
    db.execute(conn, _UPDATE_TOTAL_SQL, (amount, quote_id))
    return amount


def _valid_unit_price(value: Any) -> bool:
    """人工单价必须是 >0 的数值（bool 不算）。"""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float, Decimal)):
        return False
    if isinstance(value, float) and value != value:  # NaN
        return False
    return value > 0


def generate_quote(recognition_result: dict, price_type: str, database_path: str) -> dict:
    """主接口：识图结果 → 报价草案（落库为 draft），返回完整报价 dict。

    Args:
        recognition_result: 识图结果 dict（也兼容 JSON 字符串 / .json 路径）。
        price_type: 四类价格类型之一。
        database_path: SQLite 数据库路径。

    Returns:
        含 quote_id / items / total_amount / provisional_amount /
        pending_items / warnings / database_path 的结果 dict。

    Raises:
        ValueError: price_type 非法时列出四个合法值。
    """
    if price_type not in PRICE_TYPES:
        raise ValueError(
            f"price_type 非法：{price_type!r}；合法值为："
            + ", ".join(PRICE_TYPES.keys())
        )

    raw = load_recognition_result(recognition_result)
    norm = normalize_recognition_result(raw)

    conn = db.connect(database_path)
    try:
        if not _catalog_ready(conn):
            conn.close()
            init_database(database_path, force=False)
            conn = db.connect(database_path)

        rows = [pricing.price_item(conn, item, price_type) for item in norm["items"]]
        total_amount = _sum_amounts(rows, "amount")
        provisional_amount = _sum_amounts(rows, "provisional_amount")
        pending_items = _build_pending_items(rows)

        warnings = _default_warnings()
        if norm["validation_errors"]:
            warnings.append(
                "识图结果校验未通过：" + "；".join(norm["validation_errors"])
            )

        quote_id = (
            "Q-"
            + datetime.now().strftime("%Y%m%d%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        generated_at = _now()

        with db.transaction(conn):
            db.execute(
                conn,
                _INSERT_RECORD_SQL,
                (
                    quote_id,
                    norm["project_id"],
                    norm["project_name"],
                    price_type,
                    "draft",
                    generated_at,
                    "",
                    "",
                    total_amount,
                    "CNY",
                    "",
                ),
            )
            for row in rows:
                db.execute(
                    conn,
                    _INSERT_ITEM_SQL,
                    _item_insert_values(row, quote_id, generated_at),
                )

        return {
            "quote_id": quote_id,
            "project_id": norm["project_id"],
            "project_name": norm["project_name"],
            "price_type": price_type,
            "price_type_label": PRICE_TYPES[price_type],
            "generated_at": generated_at,
            "status": "draft",
            "currency": "CNY",
            "items": rows,
            "total_amount": total_amount,
            "provisional_amount": provisional_amount,
            "pending_items": pending_items,
            "warnings": warnings,
            "database_path": _abs_path(database_path),
        }
    finally:
        conn.close()


def get_quote(quote_id: str, database_path: str) -> dict:
    """从 DB 重建与 generate_quote 同结构的报价 dict。

    Raises:
        ValueError: quote_id 不存在。
    """
    conn = db.connect(database_path)
    try:
        record = db.fetch_one(
            conn, "SELECT * FROM quote_records WHERE quote_id=?", (quote_id,)
        )
        if record is None:
            raise ValueError(f"报价单不存在：{quote_id}")
        rows = db.fetch_all(
            conn,
            "SELECT * FROM quote_items WHERE quote_id=? ORDER BY id",
            (quote_id,),
        )
        items = [_row_to_item(conn, row) for row in rows]
        return {
            "quote_id": record["quote_id"],
            "project_id": record["project_id"],
            "project_name": record["project_name"],
            "price_type": record["price_type"],
            "price_type_label": PRICE_TYPES.get(record["price_type"], ""),
            "generated_at": record["generated_at"],
            "status": record["status"],
            "currency": record["currency"],
            "items": items,
            "total_amount": (
                float(record["total_amount"])
                if record["total_amount"] is not None
                else 0.0
            ),
            "provisional_amount": _sum_amounts(items, "provisional_amount"),
            "pending_items": _build_pending_items(items),
            "warnings": _default_warnings(),
            "database_path": _abs_path(database_path),
        }
    finally:
        conn.close()


def update_quote_item_price(
    quote_id: str,
    item_id: str,
    unit_price: Any,
    database_path: str,
    remark: str = "",
) -> dict:
    """草案态人工改价：只更新 quote_items，绝不写 price_history。

    Raises:
        ValueError: 报价已确认 / 单价非法 / item_id 不存在。
    """
    conn = db.connect(database_path)
    try:
        record = db.fetch_one(
            conn, "SELECT * FROM quote_records WHERE quote_id=?", (quote_id,)
        )
        if record is None:
            raise ValueError(f"报价单不存在：{quote_id}")
        if record["status"] != "draft":
            raise ValueError("报价已确认，不能修改明细价格")
        if not _valid_unit_price(unit_price):
            raise ValueError(
                f"unit_price 必须是 >0 的数值，实际为 {unit_price!r}"
            )
        row = db.fetch_one(
            conn,
            "SELECT * FROM quote_items WHERE quote_id=? AND item_id=?",
            (quote_id, item_id),
        )
        if row is None:
            raise ValueError(f"报价明细中找不到 item_id={item_id}")

        check = pricing.check_unit(
            conn,
            row["quantity_type"],
            row["quantity_unit"],
            row["pricing_unit"],
            row["quantity_value"],
        )
        messages = list(_json_loads(row["messages"], []))
        if check["ok"] and check["pricing_quantity"] is not None:
            pricing_quantity = float(check["pricing_quantity"])
            amount = float(
                pricing.calculate_amount(check["pricing_quantity"], unit_price)
            )
            needs_confirmation = 0
        else:
            pricing_quantity = None
            amount = None
            needs_confirmation = 1
            messages.append(check["reason"])

        if row["price_history_id"] is not None:
            messages.append(
                f"人工覆盖原推荐价（原 price_history_id=#{row['price_history_id']}）"
            )
        else:
            messages.append("人工覆盖原推荐价（原 price_history_id=#无，原为无匹配）")
        messages.append(
            f"人工填写单价 {unit_price} 元/单位（草案态，未写入历史价格库）"
        )

        new_remark = str(row["remark"] or "")
        if remark:
            new_remark = (new_remark + "；" if new_remark else "") + str(remark)

        with db.transaction(conn):
            db.execute(
                conn,
                _UPDATE_ITEM_SQL,
                (
                    float(unit_price),
                    amount,
                    pricing_quantity,
                    needs_confirmation,
                    "manual",
                    _json_dumps(messages),
                    new_remark,
                    row["id"],
                ),
            )
            _recalc_total(conn, quote_id)
        return get_quote(quote_id, database_path)
    finally:
        conn.close()


def _ensure_catalog(
    conn: sqlite3.Connection,
    canonical_name: str,
    material: str,
    process: str,
    spec: str,
    created_at: str,
) -> int:
    """按 (canonical_name, material, process, spec) 找标准项目，不存在则新建。"""
    row = db.fetch_one(
        conn,
        "SELECT id FROM item_catalog "
        "WHERE canonical_name=? AND material=? AND process=? AND spec=?",
        (canonical_name, material, process, spec),
    )
    if row is not None:
        return int(row["id"])
    cursor = db.execute(
        conn,
        "INSERT INTO item_catalog "
        "(canonical_name, category, material, process, spec, aliases, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (canonical_name, "", material, process, spec, "[]", created_at),
    )
    return int(cursor.lastrowid)


def _apply_price_override(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    price: Any,
) -> tuple[Any, Any, int, list[str]]:
    """应用一条人工单价覆盖，返回 (pricing_quantity, amount, needs, 追加消息)。"""
    messages: list[str] = []
    check = pricing.check_unit(
        conn,
        row["quantity_type"],
        row["quantity_unit"],
        row["pricing_unit"],
        row["quantity_value"],
    )
    if check["ok"] and check["pricing_quantity"] is not None:
        pricing_quantity = float(check["pricing_quantity"])
        amount = float(pricing.calculate_amount(check["pricing_quantity"], price))
        needs = 0
    else:
        pricing_quantity = None
        amount = None
        needs = 1
        messages.append(check["reason"])
    messages.append(
        f"人工填写单价 {price} 元/单位（报价确认，已写入历史价格库）"
    )
    return pricing_quantity, amount, needs, messages


def confirm_quote(
    quote_id: str,
    items: list[dict] | None,
    confirmed_by: str,
    database_path: str,
    allow_unpriced: bool = False,
    note: str = "",
) -> dict:
    """人工确认报价：单事务内更新明细 + 主记录 + 只 INSERT 新历史价格。

    业务规则校验失败（重复确认、仍有未填价、参数非法）抛 ValueError 原样透出，
    便于前端区分；其余任何异常一律 rollback 并抛 QuoteSaveError。

    Args:
        quote_id: 报价单号。
        items: [{'item_id', 'final_unit_price'(None 表示沿用), 'remark'}]，
            None/空表示全部沿用当前值。
        confirmed_by: 确认人。
        database_path: 数据库路径。
        allow_unpriced: 是否允许存在无最终单价的行（默认 False）。
        note: 确认备注。

    Returns:
        get_quote(...) 的结果，并附 history_records_added: 新增历史价条数。
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = db.connect(database_path)
        record = db.fetch_one(
            conn, "SELECT * FROM quote_records WHERE quote_id=?", (quote_id,)
        )
        if record is None:
            raise ValueError(f"报价单不存在：{quote_id}")
        if record["status"] == "confirmed":
            raise ValueError("报价已确认，禁止重复确认（避免重复写入历史价格）")

        db_rows = db.fetch_all(
            conn,
            "SELECT * FROM quote_items WHERE quote_id=? ORDER BY id",
            (quote_id,),
        )
        valid_ids = {r["item_id"] for r in db_rows}
        overrides: dict[str, dict] = {}
        for entry in items or []:
            if not isinstance(entry, dict):
                raise ValueError("items 的每一项必须是 dict")
            key = str(entry.get("item_id") or "")
            overrides[key] = entry
        unknown = [key for key in overrides if key not in valid_ids]
        if unknown:
            raise ValueError(f"items 中存在未知 item_id：{', '.join(unknown)}")

        plan: list[dict[str, Any]] = []
        for row in db_rows:
            messages = list(_json_loads(row["messages"], []))
            final_price = row["final_unit_price"]
            amount = row["amount"]
            pricing_quantity = row["pricing_quantity"]
            needs = int(row["needs_confirmation"])
            match_status = row["match_status"]
            remark = str(row["remark"] or "")

            entry = overrides.get(row["item_id"])
            if entry is not None:
                price = entry.get("final_unit_price")
                if price is not None:
                    if not _valid_unit_price(price):
                        raise ValueError(
                            f"item_id={row['item_id']} 的 final_unit_price "
                            f"必须是 >0 的数值，实际为 {price!r}"
                        )
                    pricing_quantity, amount, needs, added = _apply_price_override(
                        conn, row, price
                    )
                    messages.extend(added)
                    final_price = float(price)
                    match_status = "manual"
                entry_remark = entry.get("remark", "")
                if entry_remark:
                    remark = (remark + "；" if remark else "") + str(entry_remark)

            plan.append(
                {
                    "db_id": row["id"],
                    "item_id": row["item_id"],
                    "item_name": row["item_name"],
                    "material": row["material"],
                    "process": row["process"],
                    "spec": row["spec"],
                    "pricing_unit": row["pricing_unit"],
                    "construction_note": row["construction_note"],
                    "final_price": final_price,
                    "amount": amount,
                    "pricing_quantity": pricing_quantity,
                    "needs": needs,
                    "match_status": match_status,
                    "messages": messages,
                    "remark": remark,
                }
            )

        unpriced = [p["item_id"] for p in plan if p["final_price"] is None]
        if not allow_unpriced and unpriced:
            raise ValueError(
                "以下项目仍无最终单价，需人工填写或传 allow_unpriced=True："
                + ", ".join(unpriced)
            )

        total = Decimal("0")
        for p in plan:
            if p["amount"] is not None:
                total += Decimal(str(p["amount"]))
        total_amount = float(total)

        now = _now()
        today = date.today().isoformat()
        source_project = record["project_name"] or record["project_id"]
        added = 0

        with db.transaction(conn):
            for p in plan:
                db.execute(
                    conn,
                    _UPDATE_ITEM_SQL,
                    (
                        p["final_price"],
                        p["amount"],
                        p["pricing_quantity"],
                        p["needs"],
                        p["match_status"],
                        _json_dumps(p["messages"]),
                        p["remark"],
                        p["db_id"],
                    ),
                )
            db.execute(
                conn,
                _UPDATE_RECORD_SQL,
                (confirmed_by, now, total_amount, note, quote_id),
            )
            for p in plan:
                if p["final_price"] is None:
                    continue
                catalog_id = _ensure_catalog(
                    conn,
                    p["item_name"],
                    p["material"],
                    p["process"],
                    p["spec"],
                    now,
                )
                history_remark = (
                    p["remark"] + "；" if p["remark"] else ""
                ) + f"人工确认：{confirmed_by}"
                db.execute(
                    conn,
                    _INSERT_HISTORY_SQL,
                    (
                        catalog_id,
                        record["price_type"],
                        p["final_price"],
                        p["pricing_unit"],
                        "CNY",
                        source_project,
                        today,
                        "quote_confirmation",
                        quote_id,
                        p["construction_note"],
                        history_remark,
                        1,
                        0,
                        now,
                    ),
                )
                added += 1

        result = get_quote(quote_id, database_path)
        result["history_records_added"] = added
        return result
    except ValueError:
        # 业务规则校验失败：原样透出 ValueError，便于胡的前端区分处理。
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        raise
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        raise QuoteSaveError(
            f"数据库保存失败：{exc}；本次确认已回滚，历史价格未写入"
        ) from exc
    finally:
        if conn is not None:
            conn.close()
