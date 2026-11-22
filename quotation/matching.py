"""三态历史价格匹配：完全匹配 / 相似匹配（仅参考） / 无匹配（只读数据库）。

严禁任何均价、趋势、插值或预测造价；价格只能来自 price_history 中
is_confirmed=1 的真实历史记录，并保留完整来源用于追溯。
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from . import models
from .db import fetch_all, fetch_one


def _names(row: Any) -> list[str]:
    """返回 catalog 行的 canonical_name + 全部别名。"""
    try:
        aliases = json.loads(row["aliases"] or "[]")
    except (ValueError, TypeError):
        aliases = []
    if not isinstance(aliases, list):
        aliases = []
    return [str(row["canonical_name"])] + [str(a) for a in aliases]


def _catalog_rows(conn: Any) -> list[Any]:
    """读取全部标准项目（只读）。"""
    return fetch_all(conn, "SELECT * FROM item_catalog ORDER BY id")


def _latest_price(conn: Any, item_id: int, price_type: str, pricing_unit: str | None = None) -> Any:
    """取指定项目/价格类型下最新一条已确认历史价（同名多历史价取价策略）。

    取价策略：is_confirmed=1 优先，quote_date DESC, id DESC 取最新。
    该策略待蒋确认，本版仅按此默认实现，不视为业务裁定。
    """
    sql = (
        "SELECT * FROM price_history "
        "WHERE item_id=? AND price_type=? AND is_confirmed=1"
    )
    params: list[Any] = [item_id, price_type]
    if pricing_unit is not None:
        sql += " AND pricing_unit=?"
        params.append(pricing_unit)
    sql += " ORDER BY quote_date DESC, id DESC LIMIT 1"
    return fetch_one(conn, sql, tuple(params))


def _other_unit_prices(conn: Any, item_id: int, price_type: str) -> list[Any]:
    """取该项目该价格类型下所有已确认历史价，按日期倒序（只读）。"""
    return fetch_all(
        conn,
        "SELECT * FROM price_history "
        "WHERE item_id=? AND price_type=? AND is_confirmed=1 "
        "ORDER BY quote_date DESC, id DESC",
        (item_id, price_type),
    )


def _source_from_row(row: Any) -> dict[str, Any]:
    """构造完整可追溯的价格来源字典。"""
    price_type = str(row["price_type"])
    return {
        "price_history_id": int(row["id"]),
        "source_project": str(row["source_project"]),
        "quote_date": str(row["quote_date"]),
        "price_type": price_type,
        "price_type_label": models.PRICE_TYPES.get(price_type, ""),
        "pricing_unit": str(row["pricing_unit"]),
        "unit_price": float(row["unit_price"]),
        "is_confirmed": int(row["is_confirmed"]),
        "is_demo": int(row["is_demo"]),
        "source_type": str(row["source_type"]),
        "source_ref": str(row["source_ref"]),
        "construction_note": str(row["construction_note"]),
        "remark": str(row["remark"]),
        "currency": str(row["currency"]),
    }


def _empty_result(pricing_unit: str) -> dict[str, Any]:
    """匹配结果的默认值。"""
    return {
        "match_status": "none",
        "match_score": 0.0,
        "recommended_unit_price": None,
        "pricing_unit": pricing_unit,
        "price_source": None,
        "candidates": [],
        "messages": [],
        "unit_mismatch": False,
    }


def _find_exact_catalog(
    item_name: str,
    material: str,
    process: str,
    spec: str,
    catalog_rows: list[Any],
) -> Any:
    """别名归一 + canonical_name/material/process/spec 全等的完全匹配项目。"""
    target = models.normalize_text(item_name)
    if not target:
        return None
    for row in catalog_rows:
        if not any(target == models.normalize_text(name) for name in _names(row)):
            continue
        if (
            models.normalize_text(material) == models.normalize_text(row["material"])
            and models.normalize_text(process) == models.normalize_text(row["process"])
            and models.normalize_text(spec) == models.normalize_text(row["spec"])
        ):
            return row
    return None


def _catalog_candidate(row: Any, score: float, source: dict[str, Any]) -> dict[str, Any]:
    """构造一条相似匹配候选（含完整来源）。"""
    return {
        "catalog_id": int(row["id"]),
        "canonical_name": str(row["canonical_name"]),
        "material": str(row["material"]),
        "process": str(row["process"]),
        "spec": str(row["spec"]),
        "match_score": round(float(score), 4),
        "pricing_unit": source["pricing_unit"],
        "unit_price": source["unit_price"],
        "price_source": source,
    }


def match_price(conn: Any, item: dict, price_type: str) -> dict:
    """在历史价格库中为单条识图项目做三态匹配（只读）。

    Args:
        conn: 由 db.connect() 返回的连接。
        item: normalize_recognition_result() 产出的单条 item。
        price_type: 四类价格类型之一。

    Returns:
        {'match_status', 'match_score', 'recommended_unit_price', 'pricing_unit',
         'price_source', 'candidates', 'messages', 'unit_mismatch'}。

    Raises:
        ValueError: price_type 非法时列出四个合法值。
    """
    if price_type not in models.PRICE_TYPES:
        raise ValueError(
            f"price_type 非法：{price_type!r}；合法值为："
            + ", ".join(models.PRICE_TYPES.keys())
        )

    quantity = item.get("quantity") or {}
    raw_unit = quantity.get("unit") or ""
    normalized = models.normalize_unit(raw_unit)
    quantity_unit = normalized[1] if normalized else str(raw_unit)
    result = _empty_result(quantity_unit)

    catalog_rows = _catalog_rows(conn)
    exact_catalog = _find_exact_catalog(
        str(item.get("item_name") or ""),
        str(item.get("material") or ""),
        str(item.get("process") or ""),
        str(item.get("spec") or ""),
        catalog_rows,
    )

    # ---------- 完全匹配 ----------
    if exact_catalog is not None:
        price_row = _latest_price(
            conn, int(exact_catalog["id"]), price_type, quantity_unit
        )
        if price_row is not None:
            source = _source_from_row(price_row)
            result.update(
                {
                    "match_status": "exact",
                    "match_score": 1.0,
                    "recommended_unit_price": source["unit_price"],
                    "pricing_unit": source["pricing_unit"],
                    "price_source": source,
                    "candidates": [],
                    "messages": [],
                    "unit_mismatch": False,
                }
            )
            return result

        # ---------- 同名但计价单位不一致（不得自动推荐该价格）----------
        grouped: dict[str, Any] = {}
        for row in _other_unit_prices(conn, int(exact_catalog["id"]), price_type):
            grouped.setdefault(str(row["pricing_unit"]), row)
        candidates: list[dict[str, Any]] = []
        for pricing_unit, row in grouped.items():
            source = _source_from_row(row)
            candidates.append(
                {
                    "catalog_id": int(exact_catalog["id"]),
                    "canonical_name": str(exact_catalog["canonical_name"]),
                    "material": str(exact_catalog["material"]),
                    "process": str(exact_catalog["process"]),
                    "spec": str(exact_catalog["spec"]),
                    "match_score": None,
                    "pricing_unit": pricing_unit,
                    "unit_price": source["unit_price"],
                    "price_source": source,
                }
            )
        result.update(
            {
                "match_status": "none",
                "match_score": 0.0,
                "recommended_unit_price": None,
                "pricing_unit": quantity_unit,
                "price_source": None,
                "candidates": candidates,
                "unit_mismatch": len(candidates) > 0,
                "messages": (
                    [
                        "存在同名历史价，但计价单位与识图工程量单位不一致，"
                        "需人工确认或补充已确认换算规则"
                    ]
                    if candidates
                    else ["暂无可靠价格，等待人工填写"]
                ),
            }
        )
        return result

    # ---------- 相似匹配（仅参考价，阈值待蒋确认）----------
    target_name = models.normalize_text(item.get("item_name") or "")
    target_material = models.normalize_text(item.get("material") or "")
    fuzzy_candidates: list[dict[str, Any]] = []
    for row in catalog_rows:
        name_score = max(
            (
                difflib.SequenceMatcher(
                    None, target_name, models.normalize_text(name)
                ).ratio()
                for name in _names(row)
            ),
            default=0.0,
        )
        material_score = difflib.SequenceMatcher(
            None, target_material, models.normalize_text(row["material"])
        ).ratio()
        score = 0.7 * name_score + 0.3 * material_score
        if score < models.FUZZY_THRESHOLD:
            continue
        price_row = _latest_price(conn, int(row["id"]), price_type)
        if price_row is None:
            continue
        source = _source_from_row(price_row)
        fuzzy_candidates.append(_catalog_candidate(row, score, source))

    fuzzy_candidates.sort(key=lambda c: c["match_score"], reverse=True)
    top3 = fuzzy_candidates[:3]
    if top3:
        best = top3[0]
        result.update(
            {
                "match_status": "fuzzy",
                "match_score": best["match_score"],
                "recommended_unit_price": best["unit_price"],
                "pricing_unit": best["pricing_unit"],
                "price_source": best["price_source"],
                "candidates": top3,
                "messages": ["名称/材料相似，价格仅供参考，必须人工确认"],
                "unit_mismatch": False,
            }
        )
        return result

    # ---------- 无匹配 ----------
    result["messages"] = ["暂无可靠价格，等待人工填写"]
    return result
