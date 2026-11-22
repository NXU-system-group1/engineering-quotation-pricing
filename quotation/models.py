"""识图结果契约：常量、文本/单位归一化、加载与校验（零第三方依赖）。

本模块只做解析与校验，不访问数据库、不猜测任何业务规则。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Any

# 四类价格类型（与 price_history.price_type 的 CHECK 约束一致）
PRICE_TYPES: dict[str, str] = {
    "material_cost": "材料成本",
    "self_construction_cost": "自施工成本",
    "outsource_cost": "外包成本",
    "customer_quote": "对客报价",
}

# 工程量维度（英文枚举 + 中文标签）
QUANTITY_TYPES: dict[str, str] = {
    "area": "面积",
    "volume": "体积",
    "length": "长度",
    "count": "数量",
}

# 相似匹配阈值：阈值待蒋确认，本版仅为联调默认值，不得视为业务裁定。
FUZZY_THRESHOLD: float = 0.60

# 识图/人工修改后的合法状态
VALID_REVIEW_STATUS: frozenset[str] = frozenset(
    {"confirmed", "modified", "unconfirmed", "pending"}
)

# 本版只有 confirmed 参与计价；modified 的打标与可用口径待蒋确认，不擅自启用。
PRICING_REVIEW_STATUS: frozenset[str] = frozenset({"confirmed"})

_QUANTITY_TYPE_MAP: dict[str, str] = {
    "area": "area", "volume": "volume", "length": "length", "count": "count",
    "面积": "area", "体积": "volume", "长度": "length", "数量": "count",
}

_AREA_M2: frozenset[str] = frozenset(
    {"m2", "㎡", "平方米", "平米", "m^2", "m²", "㎡", "平方", "方米"}
)
_AREA_CM2: frozenset[str] = frozenset({"cm2", "cm²", "㎠", "平方厘米"})
_VOLUME_M3: frozenset[str] = frozenset({"m3", "m³", "㎥", "立方米", "方", "立方"})
_LENGTH_M: frozenset[str] = frozenset({"m", "米", "延米", "公尺"})
_LENGTH_CM: frozenset[str] = frozenset({"cm", "厘米", "公分"})
_COUNT_UNITS: frozenset[str] = frozenset(
    {"个", "套", "件", "项", "台", "樘", "处", "次", "块"}
)


def normalize_text(s: Any) -> str:
    """NFKC 归一化 + 去空白/标点 + 转小写，用于名称与材料的安全比较。

    Args:
        s: 任意输入，None 视为空串。

    Returns:
        归一化后的字符串。
    """
    if s is None:
        return ""
    text = unicodedata.normalize("NFKC", str(s))
    chars: list[str] = []
    for ch in text:
        if ch.isspace():
            continue
        if unicodedata.category(ch).startswith("P"):
            continue
        chars.append(ch)
    return "".join(chars).lower()


def _unit_key(unit: Any) -> str:
    """把单位写法转成 NFKC + 去空白 + 小写的查找键。"""
    if unit is None:
        return ""
    key = unicodedata.normalize("NFKC", str(unit)).strip()
    return re.sub(r"\s+", "", key).lower()


def normalize_unit(unit: Any) -> tuple[str, str] | None:
    """把识图/计价单位归一为 (quantity_type, canonical_unit)。

    覆盖：m2/㎡/平方米/m^2/M2/平米、cm2/cm²/平方厘米、m3/m³/立方米/方、
    m/米/延米/M、cm/厘米、个/套/件/项/台/樘/处/次/块。
    数量类单位之间默认不互通，返回 ('count', 原单位)。

    Returns:
        (维度, 规范单位) 或 None（未知单位）。
    """
    key = _unit_key(unit)
    if not key:
        return None
    if key in _AREA_M2:
        return ("area", "m2")
    if key in _AREA_CM2:
        return ("area", "cm2")
    if key in _VOLUME_M3:
        return ("volume", "m3")
    if key in _LENGTH_M:
        return ("length", "m")
    if key in _LENGTH_CM:
        return ("length", "cm")
    if key in _COUNT_UNITS:
        return ("count", key)
    return None


def unit_dimension(unit: Any) -> str:
    """给计价单位反查维度（m2→area, m3→volume, m→length, 套/个/项→count）。"""
    normalized = normalize_unit(unit)
    if normalized is not None:
        return normalized[0]
    key = _unit_key(unit)
    for token in ("cm2", "m2", "m3", "cm", "m", "套", "个", "项", "件", "台", "块"):
        if token in key:
            found = normalize_unit(token)
            if found is not None:
                return found[0]
    return ""


def normalize_quantity_type(t: Any) -> str:
    """接受 area/volume/length/count 及中文 面积/体积/长度/数量，未知返回空串。"""
    if t is None:
        return ""
    key = str(t).strip().lower()
    return _QUANTITY_TYPE_MAP.get(key, "")


def is_review_usable_for_pricing(review_status: Any) -> bool:
    """本版工程量是否可参与计价：仅 confirmed。

    modified（即便带 modified_by）的可用口径待蒋确认，本版保守挂起，不擅自启用。
    """
    return str(review_status or "").strip().lower() in PRICING_REVIEW_STATUS


def load_recognition_result(src: Any) -> dict:
    """加载识图结果，支持 dict / JSON 字符串 / .json 文件路径三种输入。

    Args:
        src: dict、JSON 文本、或本地 .json 文件路径（str 或 os.PathLike）。

    Returns:
        识图结果 dict。

    Raises:
        TypeError: 输入类型不支持。
        ValueError: 既不是已存在的文件也不是合法 JSON，或顶层不是 dict。
        FileNotFoundError: 明确给出类文件路径但文件不存在。
    """
    if isinstance(src, dict):
        return dict(src)
    if not isinstance(src, (str, os.PathLike)):
        raise TypeError(f"识图结果输入类型不支持：{type(src).__name__}")

    text = os.fspath(src)
    try:
        is_file = Path(text).is_file()
    except OSError:
        is_file = False

    if is_file:
        payload = json.loads(Path(text).read_text(encoding="utf-8"))
    else:
        stripped = text.strip()
        if stripped.endswith(".json") and not (
            stripped.startswith("{") or stripped.startswith("[")
        ):
            raise FileNotFoundError(f"识图结果文件不存在：{text}")
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"识图结果既不是已存在的 .json 文件，也不是合法 JSON 字符串：{exc}"
            ) from exc

    if not isinstance(payload, dict):
        raise ValueError(
            f"识图结果顶层必须是 dict，实际为 {type(payload).__name__}"
        )
    return payload


def _extract_quantity(raw_item: dict[str, Any], index: int) -> tuple[dict[str, Any], list[str], list[str]]:
    """提取 quantity（支持折平写在 item 顶层），返回 (q, errors, notes)。"""
    prefix = f"items[{index}]"
    q_raw = raw_item.get("quantity")
    if isinstance(q_raw, dict) and q_raw:
        q: dict[str, Any] = dict(q_raw)
    elif isinstance(q_raw, dict):
        q = {}
    elif q_raw is not None:
        return (
            {"value": None, "type": None, "unit": None},
            [f"{prefix}.quantity 必须是 dict（实际 {type(q_raw).__name__}）"],
            [],
        )
    else:
        q = {}

    if not q:
        folded: dict[str, Any] = {}
        for key in ("value", "type", "unit"):
            if key in raw_item:
                folded[key] = raw_item[key]
        if folded:
            q = folded
            notes = [f"{prefix} 缺少 quantity 字段，已按折平的 value/type/unit 兜底解析"]
        else:
            return (
                {"value": None, "type": None, "unit": None},
                [f"{prefix} 缺少 quantity 字段（或折平的 value/type/unit）"],
                [],
            )
    else:
        notes = []

    if "review_status" not in q and "review_status" in raw_item:
        q["review_status"] = raw_item["review_status"]
    return q, [], notes


def _validate_detailed(d: Any) -> tuple[list[str], list[list[str]], list[list[str]]]:
    """返回 (全局错误, 逐项错误, 逐项解析提示)，不抛异常、不静默跳过。"""
    if not isinstance(d, dict):
        return [f"识图结果必须是 dict，实际为 {type(d).__name__}"], [], []

    global_errors: list[str] = []
    if not d.get("project_id"):
        global_errors.append("缺少必填字段 project_id（或值为空）")

    raw_items = d.get("items")
    if raw_items is None:
        global_errors.append("缺少必填字段 items")
        return global_errors, [], []
    if not isinstance(raw_items, list):
        global_errors.append(
            f"items 必须是 list，实际为 {type(raw_items).__name__}"
        )
        return global_errors, [], []

    per_errors: list[list[str]] = []
    per_notes: list[list[str]] = []
    for index, raw in enumerate(raw_items):
        errors: list[str] = []
        notes: list[str] = []
        if not isinstance(raw, dict):
            errors.append(
                f"items[{index}] 不是 dict，实际为 {type(raw).__name__}"
            )
            per_errors.append(errors)
            per_notes.append(notes)
            continue

        for key in ("item_id", "item_name"):
            if not raw.get(key):
                errors.append(f"items[{index}] 缺少必填字段 {key}")

        q, q_errors, q_notes = _extract_quantity(raw, index)
        errors.extend(q_errors)
        notes.extend(q_notes)

        value = q.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            errors.append(
                f"items[{index}].quantity.value 非数值（实际 {value!r}）"
            )
        elif value < 0:
            errors.append(
                f"items[{index}].quantity.value 为负数（{value}）"
            )

        qtype = normalize_quantity_type(q.get("type"))
        if not qtype:
            errors.append(
                f"items[{index}].quantity.type 未知（{q.get('type')!r}）"
            )

        unit = normalize_unit(q.get("unit"))
        if unit is None:
            errors.append(
                f"items[{index}].quantity.unit 未知（{q.get('unit')!r}）"
            )
        elif qtype and qtype != unit[0]:
            errors.append(
                f"items[{index}].quantity.type 与 unit 维度不一致"
                f"（type={qtype}，unit={unit[1]} 属于 {unit[0]}）"
            )

        review_status = q.get("review_status") or "unconfirmed"
        if review_status not in VALID_REVIEW_STATUS:
            errors.append(
                f"items[{index}].review_status 非法（{review_status!r}），"
                f"合法值：{', '.join(sorted(VALID_REVIEW_STATUS))}"
            )

        per_errors.append(errors)
        per_notes.append(notes)
    return global_errors, per_errors, per_notes


def validate_recognition_result(d: dict) -> list[str]:
    """校验识图结果，返回结构化错误文本列表（不抛异常、不静默跳过）。

    覆盖：缺 project_id、items 非列表、缺 item_id/item_name、quantity 缺失、
    value 非数值或为负、type 未知、unit 未知、type 与 unit 维度不一致、
    review_status 非法等。
    """
    global_errors, per_errors, _ = _validate_detailed(d)
    all_errors = list(global_errors)
    for group in per_errors:
        all_errors.extend(group)
    return all_errors


def normalize_recognition_result(d: dict) -> dict:
    """把宽容解析后的识图结果归一为计算层可直接消费的结构。

    Returns:
        {'project_id', 'project_name', 'items': [...], 'validation_errors': [...]}。
        每个 item 含 item_id/item_name/material/process/spec、
        quantity{value,type,unit,review_status}、dimensions、evidence、raw，
        另附 validation_errors（逐项错误）与 parse_notes（折平兜底提示），
        raw 保留原始 item 全部字段（含所有未知字段），不丢弃任何信息。
    """
    global_errors, per_errors, per_notes = _validate_detailed(d)
    raw_items = d.get("items") if isinstance(d, dict) else None
    if not isinstance(raw_items, list):
        raw_items = []

    items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items):
        errors = list(per_errors[index]) if index < len(per_errors) else []
        notes = list(per_notes[index]) if index < len(per_notes) else []
        if not isinstance(raw, dict):
            items.append(
                {
                    "item_id": "",
                    "item_name": "",
                    "material": "",
                    "process": "",
                    "spec": "",
                    "quantity": {
                        "value": None, "type": "", "unit": "",
                        "review_status": "unconfirmed",
                    },
                    "dimensions": {},
                    "evidence": [],
                    "raw": {},
                    "validation_errors": errors,
                    "parse_notes": notes,
                }
            )
            continue

        q, _, _ = _extract_quantity(raw, index)
        unit = normalize_unit(q.get("unit"))
        qtype = normalize_quantity_type(q.get("type"))
        value = q.get("value")
        if isinstance(value, Decimal):
            value = float(value)
        review_status = str(q.get("review_status") or "unconfirmed")

        items.append(
            {
                "item_id": str(raw.get("item_id") or ""),
                "item_name": str(raw.get("item_name") or ""),
                "material": str(raw.get("material") or ""),
                "process": str(raw.get("process") or ""),
                "spec": str(raw.get("spec") or ""),
                "quantity": {
                    "value": value,
                    "type": qtype or str(q.get("type") or ""),
                    "unit": unit[1] if unit else str(q.get("unit") or ""),
                    "review_status": review_status,
                },
                "dimensions": raw.get("dimensions", {}),
                "evidence": raw.get("evidence", []),
                "raw": dict(raw),
                "validation_errors": errors,
                "parse_notes": notes,
            }
        )

    all_errors = list(global_errors)
    for group in per_errors:
        all_errors.extend(group)
    return {
        "project_id": str(d.get("project_id") or "") if isinstance(d, dict) else "",
        "project_name": str(d.get("project_name") or "") if isinstance(d, dict) else "",
        "items": items,
        "validation_errors": all_errors,
    }
