"""单位校验与 Decimal 合价计算，产出可直接落库的完整明细行（只读数据库）。

红线：面积/体积/长度/数量不得混用；同维度不同倍率必须有 is_confirmed=1
的换算规则；相似匹配只作参考价、不计入正式合价；无匹配不自行生成价格。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from . import models
from .db import fetch_all
from .matching import match_price

_CENT = Decimal("0.01")
_INVALID_QUANTITY_REASON = "工程量数值非法，禁止计价，请人工确认"


def _to_decimal(value: Any) -> Decimal | None:
    """把数值安全转为 Decimal（先用 str 中转，避免二进制浮点误差）。

    非法输入（非数值字符串、None、NaN/Inf 等）返回 None，不抛裸
    decimal.InvalidOperation；由调用点给出中文错误或挂起，避免畸形
    识图数据导致接口崩溃。
    """
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def calculate_amount(pricing_quantity: Decimal, unit_price: Decimal) -> Decimal:
    """合价 = 计价工程量 × 单价，保留 2 位小数（ROUND_HALF_UP）。

    Example:
        calculate_amount(Decimal('53.7812'), Decimal('100.00'))
        -> Decimal('5378.12')

    Raises:
        ValueError: 计价工程量或单价不是合法数值时给出明确中文错误。
    """
    quantity = _to_decimal(pricing_quantity)
    price = _to_decimal(unit_price)
    if quantity is None or price is None:
        raise ValueError(
            "合价计算失败：计价工程量或单价不是合法数值，"
            f"pricing_quantity={pricing_quantity!r}, unit_price={unit_price!r}，"
            "请人工确认工程量与单价"
        )
    return (quantity * price).quantize(_CENT, rounding=ROUND_HALF_UP)


def check_unit(
    conn: Any,
    quantity_type: str,
    quantity_unit: str,
    pricing_unit: str,
    quantity_value: Any = None,
) -> dict:
    """校验计价单位是否合法，并在规则已确认时换算计价工程量。

    Args:
        conn: 由 db.connect() 返回的连接（只读）。
        quantity_type: 归一后的工程量维度 area/volume/length/count。
        quantity_unit: 归一后的识图单位（m2/cm2/m3/m/cm/个/套 ...）。
        pricing_unit: 历史价格的计价单位。
        quantity_value: 可选工程量，传入时一并算出 pricing_quantity。
            该参数为可选，前四个参数与约定签名严格一致。

    Returns:
        {'ok', 'pricing_quantity'(Decimal|None), 'factor'(Decimal|None),
         'rule_id', 'reason'}。
    """
    result: dict[str, Any] = {
        "ok": False,
        "pricing_quantity": None,
        "factor": None,
        "rule_id": None,
        "reason": "",
    }

    qtype = models.normalize_quantity_type(quantity_type) or str(quantity_type or "")
    q_norm = models.normalize_unit(quantity_unit)
    q_unit = q_norm[1] if q_norm else str(quantity_unit or "")
    p_norm = models.normalize_unit(pricing_unit)
    p_unit = p_norm[1] if p_norm else str(pricing_unit or "")
    p_dimension = p_norm[0] if p_norm else models.unit_dimension(pricing_unit)

    if not p_dimension:
        result["reason"] = (
            f"计价单位 {pricing_unit!r} 无法识别维度，禁止直接相乘，请人工确认"
        )
        return result

    if qtype and qtype != p_dimension:
        result["reason"] = (
            f"识图工程量单位 {q_unit}（{qtype}）与计价单位 {p_unit}（{p_dimension}）"
            "不属于同一维度，禁止直接相乘，请人工确认"
        )
        return result

    # 同维度同单位：直通
    if q_unit == p_unit:
        factor = Decimal("1")
        converted = _to_decimal(quantity_value) if quantity_value is not None else None
        if quantity_value is not None and converted is None:
            result["reason"] = _INVALID_QUANTITY_REASON
            return result
        result.update({"ok": True, "factor": factor, "rule_id": None, "reason": ""})
        if converted is not None:
            result["pricing_quantity"] = converted * factor
        return result

    # 同维度不同倍率：仅 is_confirmed=1 的规则可用
    rules = fetch_all(
        conn,
        "SELECT * FROM pricing_rules "
        "WHERE from_quantity_type=? AND from_unit=? AND to_pricing_unit=? "
        "AND is_confirmed=1 "
        "ORDER BY (item_id IS NULL) ASC, id DESC",
        (qtype, q_unit, p_unit),
    )
    if rules:
        rule = rules[0]
        factor = Decimal(str(rule["conversion_factor"]))
        converted = _to_decimal(quantity_value) if quantity_value is not None else None
        if quantity_value is not None and converted is None:
            result["reason"] = _INVALID_QUANTITY_REASON
            return result
        result.update(
            {
                "ok": True,
                "factor": factor,
                "rule_id": int(rule["id"]),
                "reason": "",
            }
        )
        if converted is not None:
            result["pricing_quantity"] = converted * factor
        return result

    result["reason"] = (
        f"换算规则未经确认，禁止换算，请人工确认（{q_unit}→{p_unit}；"
        "仅 is_confirmed=1 的已确认规则可用于计算）"
    )
    return result


def _source_fields(source: dict[str, Any] | None) -> tuple[int | None, str, str]:
    """从价格来源中提取 price_history_id / 施工说明 / 备注。"""
    if not source:
        return None, "", ""
    return (
        source.get("price_history_id"),
        str(source.get("construction_note") or ""),
        str(source.get("remark") or ""),
    )


def price_item(conn: Any, item: dict, price_type: str) -> dict:
    """对单条识图项目完成「匹配 → 单位校验 → 合价」，产出完整明细行。

    键固定为第 3 步 api.py 落库所需字段；金额字段用 float 输出，
    内部一律用 Decimal 精确计算后再转换。

    Args:
        conn: 由 db.connect() 返回的连接（只读）。
        item: normalize_recognition_result() 产出的单条 item。
        price_type: 四类价格类型之一。

    Returns:
        完整明细行 dict（含 quantity/pricing_quantity/amount/provisional_amount/
        needs_confirmation/messages/validation_errors 等）。
    """
    quantity = dict(item.get("quantity") or {})
    value = quantity.get("value")
    # 畸形工程量（list/dict 等）不能作为 SQLite 绑定参数：转成文本后仍由
    # validation_errors 挂起，原始值完整保留在 row['raw'] 中，不参与数值计算。
    if not (value is None or isinstance(value, (int, float, str, Decimal))):
        value = str(value)
    quantity_type = str(quantity.get("type") or "")
    quantity_unit = str(quantity.get("unit") or "")
    review_status = str(quantity.get("review_status") or "unconfirmed")
    validation_errors = list(item.get("validation_errors") or [])
    parse_notes = list(item.get("parse_notes") or [])

    match = match_price(conn, item, price_type)
    messages: list[str] = list(parse_notes) + list(match["messages"])
    candidates: list[dict[str, Any]] = list(match["candidates"])

    target_unit = match["pricing_unit"] or quantity_unit
    if match["unit_mismatch"] and candidates:
        target_unit = str(candidates[0].get("pricing_unit") or target_unit)

    # 校验失败的行不进入任何数值计算，避免畸形工程量触发 Decimal 异常；
    # 下方「校验错误 → 挂起」分支会补齐中文提示并置 needs_confirmation=True。
    if validation_errors:
        check = {
            "ok": False,
            "pricing_quantity": None,
            "factor": None,
            "rule_id": None,
            "reason": "",
        }
    else:
        check = check_unit(
            conn, quantity_type, quantity_unit, target_unit, value
        )
    pricing_quantity = check["pricing_quantity"]

    source = match["price_source"]
    history_id, construction_note, remark = _source_fields(source)

    row: dict[str, Any] = {
        "item_id": str(item.get("item_id") or ""),
        "item_name": str(item.get("item_name") or ""),
        "material": str(item.get("material") or ""),
        "process": str(item.get("process") or ""),
        "spec": str(item.get("spec") or ""),
        "quantity": {
            "value": value,
            "type": quantity_type,
            "unit": quantity_unit,
            "review_status": review_status,
        },
        "pricing_quantity": float(pricing_quantity) if pricing_quantity is not None else None,
        "pricing_unit": target_unit,
        "recommended_unit_price": match["recommended_unit_price"],
        "final_unit_price": None,
        "price_history_id": history_id,
        "price_source": source,
        "match_status": match["match_status"],
        "match_score": match["match_score"],
        "candidates": candidates,
        "amount": None,
        "provisional_amount": None,
        "needs_confirmation": True,
        "messages": messages,
        "construction_note": construction_note,
        "remark": remark,
        "raw": dict(item.get("raw") or {}),
        "validation_errors": validation_errors,
    }

    # 1) 校验错误 → 挂起
    if validation_errors:
        messages.append("识图结果校验未通过：" + "；".join(validation_errors))
        row["messages"] = messages
        return row

    # 2) 工程量未经人工确认 → 不参与计价
    if not models.is_review_usable_for_pricing(review_status):
        messages.append("工程量未经人工确认，暂不参与计价")
        if review_status == "modified":
            messages.append(
                "modified 状态的人工修改打标口径待蒋确认，本版暂不参与计价"
            )
        row["messages"] = messages
        return row

    # 3) 同名历史价但计价单位不一致：用候选单位做单位校验
    if match["unit_mismatch"]:
        if not candidates:
            messages.append("存在同名项目但无可用历史价格，暂无可靠价格，等待人工填写")
            row["messages"] = messages
            return row
        if not check["ok"] or pricing_quantity is None:
            messages.append(check["reason"])
            row["pricing_quantity"] = None
            row["messages"] = messages
            return row
        best = candidates[0]
        best_source = best.get("price_source") or {}
        unit_price = best.get("unit_price")
        if unit_price is None:
            messages.append("同名历史价缺少可用单价，暂无可靠价格，等待人工填写")
            row["messages"] = messages
            return row
        amount = calculate_amount(pricing_quantity, unit_price)
        best_id, best_note, best_remark = _source_fields(best_source or None)
        row.update(
            {
                "match_status": "exact",
                "match_score": 1.0,
                "recommended_unit_price": float(unit_price),
                "final_unit_price": float(unit_price),
                "amount": float(amount),
                "price_history_id": best_id,
                "price_source": best_source or None,
                "construction_note": best_note,
                "remark": best_remark,
                "needs_confirmation": False,
            }
        )
        messages.append(
            "项目名称/材料/工艺/规格完全匹配，计价单位经已确认规则换算，视同完全匹配"
        )
        messages.append(
            f"已按确认的换算规则（rule_id={check['rule_id']}，"
            f"系数 {check['factor']}）将 {quantity_unit} 换算为 {target_unit} 后计价"
        )
        row["messages"] = messages
        return row

    # 4) 相似匹配：仅参考价，给暂估金额但不计入正式合价
    if match["match_status"] == "fuzzy":
        if (
            check["ok"]
            and pricing_quantity is not None
            and match["recommended_unit_price"] is not None
        ):
            row["provisional_amount"] = float(
                calculate_amount(
                    pricing_quantity, match["recommended_unit_price"]
                )
            )
        else:
            messages.append(check["reason"])
        row["messages"] = messages
        return row

    # 5) 无匹配：不自行生成价格
    if match["match_status"] == "none":
        row["final_unit_price"] = None
        row["messages"] = messages
        return row

    # 6) 完全匹配：单位校验通过后正式计价
    if not check["ok"] or pricing_quantity is None:
        messages.append(check["reason"])
        row["pricing_quantity"] = None
        row["messages"] = messages
        return row
    unit_price = match["recommended_unit_price"]
    amount = calculate_amount(pricing_quantity, unit_price)
    row.update(
        {
            "final_unit_price": float(unit_price),
            "amount": float(amount),
            "needs_confirmation": False,
        }
    )
    if source and int(source.get("is_confirmed", 1)) == 0:
        row["needs_confirmation"] = True
        messages.append("该历史价格未经确认，需人工确认后方可作为正式价")
    row["messages"] = messages
    return row
