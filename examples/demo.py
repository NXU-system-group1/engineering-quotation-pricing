#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工程报价系统 · 奉的报价模块 —— 端到端演示（零第三方依赖）。

识图结果字段契约尚未冻结，本演示仅用于胡 / 蒋 / 奉三方联调；
示例结构见 examples/recognition_demo.json，待确认口径见 docs/。
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from quotation import (  # noqa: E402
    QuoteSaveError,
    confirm_quote,
    export_quote_excel,
    generate_quote,
    get_quote,
    init_database,
    load_recognition_result,
    table_counts,
    update_quote_item_price,
    validate_recognition_result,
)
from quotation.db import connect  # noqa: E402

DB = str(ROOT / "output" / "quote_demo.db")
REC = str(ROOT / "examples" / "recognition_demo.json")
DRAFT_XLSX = str(ROOT / "output" / "报价明细_草案.xlsx")
CONFIRMED_XLSX = str(ROOT / "output" / "报价明细_已确认.xlsx")
DOC = ROOT / "docs" / "待确认问题与已知限制.md"

RED_LINES = [
    "1 历史价格只 INSERT，禁止 UPDATE/DELETE（DB 触发器 + 应用层双重约束）",
    "2 无匹配不生成价格，提示「暂无可靠价格，等待人工填写」",
    "3 相似匹配仅参考价，needs_confirmation=True，不计入正式总价",
    "4 每个推荐价可追溯（price_history_id / 来源项目 / 日期 / is_demo 等）",
    "5 面积/体积/长度/数量不混用；跨维度拦截，换算需已确认规则",
    "6 review_status != confirmed 的工程量不参与计价",
    "7 取整/损耗/跨视图合并/阈值/取价策略不猜测，显式挂起待蒋确认",
    "8 未确认价格不入库；确认回写单事务，失败 rollback 抛 QuoteSaveError",
]


def fmt(value: object) -> str:
    """None 显示为破折号，便于表格打印。"""
    return "—" if value is None else str(value)


def item_of(quote: dict, item_id: str) -> dict:
    """按 item_id 从报价结果中取明细行。"""
    return next(x for x in quote["items"] if x["item_id"] == item_id)


def main() -> int:
    """执行完整闭环演示，全部自检通过返回 0。"""
    print("=" * 78)
    print("工程报价系统 · 奉的报价模块 —— 端到端演示")
    print("免责声明：本演示全部使用模拟/脱敏历史价格，非真实商业报价，仅用于联调演示。")
    print("=" * 78)

    for name in ("_xlsx_smoke.xlsx", "_api_smoke.xlsx"):
        (ROOT / "output" / name).unlink(missing_ok=True)

    print("\n[1] 初始化数据库（force=True，仅演示用）")
    db_path = init_database(DB, force=True)
    counts = table_counts(db_path)
    print("    ", db_path)
    for table, number in counts.items():
        print(f"    {table:<18}{number}")
    assert counts["price_history"] == 27

    print("\n[2] 加载并校验识图结果")
    rec = load_recognition_result(REC)
    errors = validate_recognition_result(rec)
    print("    ", REC)
    print("    validate_recognition_result ->", errors if errors else "[]（无错误）")
    assert errors == []

    print("\n[3] 生成报价草案（胡 → 奉 的主接口）")
    q = generate_quote(rec, "customer_quote", DB)
    qid = q["quote_id"]
    print("    quote_id =", qid, "| status =", q["status"])
    header = ["序号", "项目名称", "工程量", "单位", "推荐单价", "确认单价",
              "匹配状态", "合价", "需人工确认", "原因摘要"]
    print("    " + " | ".join(header))
    print("    " + "-" * 108)
    for index, item in enumerate(q["items"], start=1):
        quantity = item["quantity"]
        reason = next(
            (p["reason"] for p in q["pending_items"]
             if p["item_id"] == item["item_id"]),
            "—",
        )
        cells = [
            str(index), item["item_name"], fmt(quantity["value"]),
            quantity["unit"], fmt(item["recommended_unit_price"]),
            fmt(item["final_unit_price"]), item["match_status"],
            fmt(item["amount"]),
            "是" if item["needs_confirmation"] else "否", reason,
        ]
        print("    " + " | ".join(cells))
    print("    " + "-" * 108)
    print("    total_amount       =", q["total_amount"], "（仅可计价行）")
    print("    provisional_amount =", q["provisional_amount"],
          "（相似匹配暂估，不计入总价）")
    print("    pending_items      =", len(q["pending_items"]))
    print("    warnings:")
    for warning in q["warnings"]:
        print("      -", warning)
    assert q["total_amount"] == 5538.12
    assert q["provisional_amount"] == 1680.0
    assert len(q["pending_items"]) == 4
    assert q["status"] == "draft"
    assert item_of(q, "item001")["amount"] == 5378.12
    assert item_of(q, "item006")["amount"] == 160.0

    print("\n[4] 导出草案 Excel")
    draft_path = export_quote_excel(q, DRAFT_XLSX)
    print("    ", draft_path)

    print("\n[5] 草案态历史价格条数")
    count5 = table_counts(DB)["price_history"]
    print("    price_history =", count5, "→ 未确认价格未写入历史价格库")
    assert count5 == 27

    print("\n[6] 人工改价（草案态，绝不写 price_history）")
    before2 = item_of(q, "item002")["amount"]
    before3 = item_of(q, "item003")["amount"]
    updated = update_quote_item_price(
        qid, "item002", 180.0, DB, remark="老板核定参考价"
    )
    updated = update_quote_item_price(
        qid, "item003", 55.0, DB, remark="人工填写"
    )
    after2 = item_of(updated, "item002")["amount"]
    after3 = item_of(updated, "item003")["amount"]
    print(f"    item002: {fmt(before2)} -> {fmt(after2)}"
          "（180.0 元 × 10 m2，match_status=manual）")
    print(f"    item003: {fmt(before3)} -> {fmt(after3)}（55.0 元 × 5 m2）")
    assert item_of(updated, "item002")["match_status"] == "manual"
    assert after2 == 1800.0 and after3 == 275.0
    assert table_counts(DB)["price_history"] == 27
    print("    改价后 price_history 仍为 27 → 未确认价格未写入历史价格库")

    print("\n[7] 未定价保护（allow_unpriced=False）")
    try:
        confirm_quote(qid, None, "老板（演示）", DB, allow_unpriced=False)
        raise AssertionError("应被 allow_unpriced=False 拦截")
    except ValueError as exc:
        message = str(exc)
        print("    ValueError:", message)
        assert "item004" in message and "item005" in message
        print("    仍有未定价项目：item004/item005，已阻止确认")

    print("\n[8] 人工确认回写（单事务、只 INSERT）")
    confirmed = confirm_quote(
        qid, None, "老板（演示）", DB,
        allow_unpriced=True, note="演示确认",
    )
    counts8 = table_counts(DB)
    print("    status =", confirmed["status"],
          "| history_records_added =", confirmed["history_records_added"],
          "| price_history =", counts8["price_history"])
    assert confirmed["status"] == "confirmed"
    assert confirmed["history_records_added"] == 4
    assert counts8["price_history"] == 31

    conn = connect(DB)
    old_rows = conn.execute(
        "SELECT id, unit_price, source_project, quote_date, source_type "
        "FROM price_history WHERE unit_price=100.00 AND source_type='demo_seed'"
    ).fetchall()
    print("    原历史价 100.00 仍在（旧价未被覆盖）：")
    for row in old_rows:
        print(f"      id={row['id']} unit_price={row['unit_price']} "
              f"source_project={row['source_project']} "
              f"quote_date={row['quote_date']} source_type={row['source_type']}")
    assert len(old_rows) == 1
    new_rows = conn.execute(
        "SELECT id, unit_price, source_type, source_ref, is_confirmed, is_demo "
        "FROM price_history WHERE source_type='quote_confirmation' "
        "AND source_ref=? ORDER BY id",
        (qid,),
    ).fetchall()
    print("    新增 4 条历史价（source_type / source_ref / is_confirmed）：")
    for row in new_rows:
        print(f"      id={row['id']} unit_price={row['unit_price']} "
              f"source_type={row['source_type']} source_ref={row['source_ref']} "
              f"is_confirmed={row['is_confirmed']} is_demo={row['is_demo']}")
    assert len(new_rows) == 4
    conn.close()

    print("\n[9] 导出已确认 Excel")
    final_path = export_quote_excel(get_quote(qid, DB), CONFIRMED_XLSX)
    print("    ", final_path)

    print("\n[10] 演示一次保存失败（非法/只读 database_path）")
    bad_path = str(ROOT / "output" / "quote_demo.db" / "nested.db")
    try:
        confirm_quote(qid, None, "老板（演示）", bad_path)
        raise AssertionError("应抛 QuoteSaveError")
    except QuoteSaveError as exc:
        print("    QuoteSaveError:", str(exc)[:120])
        assert "数据库保存失败" in str(exc)
        print("    事务已回滚，历史价格未写入")
    assert table_counts(DB)["price_history"] == 31

    print("\n[11] xlsx 自检（zipfile 可打开、各含 3 个 sheet）")
    for path in (draft_path, final_path):
        with zipfile.ZipFile(path) as zf:
            sheets = [
                name for name in zf.namelist()
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
            ]
            assert len(sheets) == 3, path
        print(f"    {Path(path).name}: 3 个 sheet OK")

    print("\n[12] 交付物清单与红线核对")
    for path in (DB, DRAFT_XLSX, CONFIRMED_XLSX):
        assert Path(path).exists(), path
        print("    output/", Path(path).name, "-> 已生成")
    for relative in (
        "quotation/__init__.py", "quotation/api.py", "quotation/schema.sql",
        "quotation/db.py", "quotation/seed.py", "quotation/models.py",
        "quotation/matching.py", "quotation/pricing.py", "quotation/xlsx.py",
        "data/seed_prices.json", "README.md", "INTERFACE.md",
        "docs/待确认问题与已知限制.md", "requirements.txt",
        "examples/recognition_demo.json", "examples/demo.py",
    ):
        assert (ROOT / relative).exists(), relative
    print("    代码/文档：quotation/（8 个 .py + schema.sql）、data/seed_prices.json、")
    print("              README.md / INTERFACE.md / docs/待确认问题与已知限制.md /")
    print("              requirements.txt / examples/ 均已就位")
    print("\n    8 条红线核对：")
    for line in RED_LINES:
        print("      ", line)
    doc_text = DOC.read_text(encoding="utf-8")
    issue_count = len(re.findall(r"^### \d+\.", doc_text, flags=re.MULTILINE))
    print("\n    待确认问题：", issue_count,
          "条（见 docs/待确认问题与已知限制.md；业务规则以蒋的裁定为准，本版未擅自猜测）")
    assert issue_count >= 10

    print("\n演示结束：全部自检通过，退出码 0。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
