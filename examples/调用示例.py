# -*- coding: utf-8 -*-
"""调用示例：识图结果 -> 报价 -> 导出 Excel。

运行方式（在项目根目录执行）：
    python examples/调用示例.py

想换成你自己的识图结果：把 REC_PATH 改成你的 JSON 文件路径即可，
JSON 结构参考 examples/recognition_demo.json。
"""
import sys
from pathlib import Path

# 保证从任意工作目录运行都能 import 到 quotation 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quotation import (  # noqa: E402
    load_recognition_result,
    validate_recognition_result,
    generate_quote,
    export_quote_excel,
    init_database,
)

DB_PATH = "output/我的报价.db"
REC_PATH = "examples/recognition_demo.json"
OUT_XLSX = "output/我的报价.xlsx"
PRICE_TYPE = "customer_quote"  # 四类之一：material_cost / self_construction_cost / outsource_cost / customer_quote


def main():
    # 1. 初始化数据库（已存在则保持原样，不会覆盖历史价；不要传 force=True）
    init_database(DB_PATH)

    # 2. 读取并校验识图结果
    recognition_result = load_recognition_result(REC_PATH)
    errors = validate_recognition_result(recognition_result)
    print("[1] 识图结果校验：", "无错误" if not errors else errors)

    # 3. 调用主接口生成报价草案
    q = generate_quote(recognition_result, PRICE_TYPE, DB_PATH)
    print("[2] 报价单号：", q["quote_id"])
    print("    状态：", q["status"])
    print("    总价：", q["total_amount"])
    print("    相似匹配暂估（不计入总价）：", q["provisional_amount"])
    print("    待确认项目数：", len(q["pending_items"]))
    for p in q["pending_items"]:
        print("      -", p["item_id"], p["item_name"], "|", p["reason"])

    # 4. 导出 Excel
    export_quote_excel(q, OUT_XLSX)
    print("[3] Excel 已导出：", OUT_XLSX)


if __name__ == "__main__":
    main()
