"""第 1 轮回归测试：报价计算内核（db / seed / models / matching / pricing）。

约束与范围：
- 仅使用 Python 标准库 unittest，零第三方依赖。
- 只测试已完成的底层模块；不导入 / 不测试 api.py、xlsx.py、examples/ 及包级对外接口。
- 每个测试方法使用独立临时 SQLite 库，互不干扰；Windows 下先关闭所有连接，
  再清理临时目录，避免文件被占用导致 cleanup 失败。
- 并发变更约定：不对「墙面乳胶漆 50000 cm2 → 5 m2 → 160.00 元」这一行的
  match_status 写任何断言，只断言金额、计价工程量与 needs_confirmation。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

# 保证从任意工作目录执行时都能导入 quotation 包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quotation import db, matching, models, pricing, seed  # noqa: E402

FIVE_TABLES = {
    "item_catalog",
    "price_history",
    "pricing_rules",
    "quote_records",
    "quote_items",
}

# price_source 约定的 14 个可追溯字段
SOURCE_KEYS = {
    "price_history_id", "source_project", "quote_date", "price_type",
    "price_type_label", "pricing_unit", "unit_price", "is_confirmed",
    "is_demo", "source_type", "source_ref", "construction_note",
    "remark", "currency",
}


def _normalized_item(*, item_name, value, qtype, unit,
                     material="", process="", spec="",
                     review_status="confirmed", item_id="I-QA-001"):
    """经 models 归一化，产出计算层可直接消费的单条 item（贴近真实链路）。"""
    payload = {
        "project_id": "P-QA",
        "project_name": "QA 回归测试项目",
        "items": [{
            "item_id": item_id,
            "item_name": item_name,
            "material": material,
            "process": process,
            "spec": spec,
            "quantity": {
                "value": value,
                "type": qtype,
                "unit": unit,
                "review_status": review_status,
            },
        }],
    }
    return models.normalize_recognition_result(payload)["items"][0]


class _DatabaseMixin:
    """连接生命周期管理：类级只读连接 + 每个测试独立临时库。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._class_tmp = tempfile.TemporaryDirectory(prefix="qa_quotation_cls_")
        cls.class_db_path = os.path.join(cls._class_tmp.name, "class.db")
        seed.init_database(cls.class_db_path, force=True)
        cls.class_conn = db.connect(cls.class_db_path)

    @classmethod
    def tearDownClass(cls):
        cls.class_conn.close()  # Windows：先关连接
        cls._class_tmp.cleanup()  # 再清理目录
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="qa_quotation_case_")
        self.db_path = os.path.join(self._tmp.name, "case.db")
        seed.init_database(self.db_path, force=True)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()
        super().tearDown()


class TestDatabaseAndAppendOnly(_DatabaseMixin, unittest.TestCase):
    """A. 数据库结构与「历史价格只追加」红线。"""

    def test_01_five_tables_and_seed_counts(self):
        rows = db.fetch_all(
            self.conn, "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {row["name"] for row in rows}
        self.assertTrue(
            FIVE_TABLES.issubset(names), f"缺少表：{FIVE_TABLES - names}"
        )
        counts = seed.table_counts(self.db_path)
        self.assertEqual(counts, {
            "item_catalog": 10,
            "price_history": 27,
            "pricing_rules": 3,
            "quote_records": 0,
            "quote_items": 0,
        })

    def test_02_price_history_rejects_update_and_delete(self):
        first = db.fetch_one(
            self.conn,
            "SELECT id, unit_price FROM price_history ORDER BY id LIMIT 1",
        )
        target_id, old_price = first["id"], first["unit_price"]
        self.assertEqual(seed.table_counts(self.db_path)["price_history"], 27)

        # UPDATE 必须被触发器 ABORT，且旧值不变
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            self.conn.execute(
                "UPDATE price_history SET unit_price=999999 WHERE id=?",
                (target_id,),
            )
        self.assertIn("禁止", str(ctx.exception))
        self.conn.rollback()
        after = db.fetch_one(
            self.conn, "SELECT unit_price FROM price_history WHERE id=?",
            (target_id,),
        )
        self.assertEqual(after["unit_price"], old_price, "UPDATE 后旧价被覆盖")

        # DELETE 必须被触发器 ABORT，且行数不变、行仍存在
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            self.conn.execute(
                "DELETE FROM price_history WHERE id=?", (target_id,)
            )
        self.assertIn("禁止", str(ctx.exception))
        self.conn.rollback()
        self.assertEqual(seed.table_counts(self.db_path)["price_history"], 27)
        self.assertIsNotNone(db.fetch_one(
            self.conn, "SELECT 1 FROM price_history WHERE id=?", (target_id,)
        ))

    def test_03_all_seed_prices_are_demo_and_desensitized(self):
        rows = db.fetch_all(self.conn, "SELECT * FROM price_history")
        self.assertEqual(len(rows), 27)
        for row in rows:
            with self.subTest(price_history_id=row["id"]):
                self.assertEqual(int(row["is_demo"]), 1)
                self.assertEqual(str(row["source_type"]), "demo_seed")
                project = str(row["source_project"])
                self.assertTrue(
                    "模拟" in project or "脱敏" in project,
                    f"来源项目缺少模拟/脱敏标识：{project!r}",
                )

    def test_04_init_database_idempotent_and_force_rebuild(self):
        # force 重建使用独立文件路径，避免 Windows 下被本用例连接占用的文件
        path = os.path.join(self._tmp.name, "idempotent.db")
        returned = seed.init_database(path, force=True)
        self.assertTrue(Path(returned).is_file())
        baseline = seed.table_counts(path)
        self.assertEqual(baseline["price_history"], 27)

        seed.init_database(path)  # 非 force：第二次
        seed.init_database(path)  # 非 force：第三次
        self.assertEqual(
            seed.table_counts(path), baseline, "重复初始化重复灌入了种子"
        )

        seed.init_database(path, force=True)  # 强制删库重建
        rebuilt = seed.table_counts(path)
        self.assertEqual(rebuilt, baseline)
        self.assertEqual(rebuilt["price_history"], 27)

    def test_05_transaction_rollback_on_exception(self):
        quote_id = "Q-QA-ROLLBACK-001"
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with db.transaction(self.conn):
                db.execute(
                    self.conn,
                    "INSERT INTO quote_records "
                    "(quote_id, project_id, project_name, price_type, generated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (quote_id, "P-QA", "回滚测试", "customer_quote",
                     "2025-01-01T00:00:00"),
                )
                raise RuntimeError("boom")
        self.assertIsNone(db.fetch_one(
            self.conn, "SELECT 1 FROM quote_records WHERE quote_id=?",
            (quote_id,),
        ))
        self.assertEqual(seed.table_counts(self.db_path)["quote_records"], 0)


class TestRecognitionParsing(unittest.TestCase):
    """B. 识图结果加载、单位归一、校验与归一化（纯逻辑，不依赖数据库）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="qa_quotation_json_")
        cls.sample = {
            "project_id": "P-QA-001",
            "project_name": "示例工程",
            "items": [{
                "item_id": "I-001",
                "item_name": "墙面PVC饰面",
                "quantity": {
                    "value": 12.5, "type": "area", "unit": "m2",
                    "review_status": "confirmed",
                },
            }],
        }
        cls.json_text = json.dumps(cls.sample, ensure_ascii=False)
        cls.json_path = os.path.join(cls._tmp.name, "recognition.json")
        Path(cls.json_path).write_text(cls.json_text, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_06_normalize_unit_table_driven(self):
        cases = [
            (("m2", "㎡", "平方米", "m^2", "平米", "M2", "m²"), ("area", "m2")),
            (("cm2", "cm²", "平方厘米"), ("area", "cm2")),
            (("m3", "m³", "立方米", "方"), ("volume", "m3")),
            (("m", "米", "延米", "公尺"), ("length", "m")),
            (("cm", "厘米", "公分"), ("length", "cm")),
        ]
        for units, expected in cases:
            for unit in units:
                with self.subTest(unit=unit):
                    self.assertEqual(models.normalize_unit(unit), expected)
        for unit in ("套", "个", "件", "项", "台", "块"):
            with self.subTest(unit=unit):
                self.assertEqual(models.normalize_unit(unit), ("count", unit))
        for unit in ("kg", "千瓦", "xyz", "", None):
            with self.subTest(unit=unit):
                self.assertIsNone(models.normalize_unit(unit))

    def test_07_load_recognition_result_three_forms(self):
        self.assertEqual(
            models.load_recognition_result(self.sample), self.sample
        )
        self.assertEqual(
            models.load_recognition_result(self.json_text), self.sample
        )
        self.assertEqual(
            models.load_recognition_result(self.json_path), self.sample
        )

    def test_08_validate_detects_all_error_kinds_without_raising(self):
        def doc(items):
            return {"project_id": "P-QA", "items": items}

        def item(**overrides):
            base = {
                "item_id": "I-001",
                "item_name": "标准项目",
                "quantity": {
                    "value": 10, "type": "area", "unit": "m2",
                    "review_status": "confirmed",
                },
            }
            base.update(overrides)
            return base

        def quantity(**overrides):
            q = {
                "value": 1, "type": "area", "unit": "m2",
                "review_status": "confirmed",
            }
            q.update(overrides)
            return q

        cases = [
            ("缺 project_id", {"items": []}, "project_id"),
            ("items 非 list",
             {"project_id": "P-QA", "items": "oops"}, "items 必须是 list"),
            ("缺 item_id/item_name",
             doc([{"quantity": quantity()}]), "item_id"),
            ("quantity 缺失",
             doc([{"item_id": "I-001", "item_name": "N"}]), "quantity 字段"),
            ("value 为负", doc([item(quantity=quantity(value=-1))]), "为负数"),
            ("value 非数值",
             doc([item(quantity=quantity(value="abc"))]), "非数值"),
            ("type 未知",
             doc([item(quantity=quantity(type="whatever"))]), "type 未知"),
            ("unit 未知",
             doc([item(quantity=quantity(unit="kg"))]), "unit 未知"),
            ("维度不一致",
             doc([item(quantity=quantity(type="area", unit="m"))]),
             "维度不一致"),
            ("review_status 非法",
             doc([item(quantity=quantity(review_status="approved"))]),
             "review_status 非法"),
        ]
        for label, payload, keyword in cases:
            with self.subTest(case=label):
                errors = models.validate_recognition_result(payload)
                self.assertIsInstance(errors, list)
                self.assertTrue(
                    any(keyword in e for e in errors),
                    f"{label} 未检出，实际错误：{errors}",
                )
        # 非 dict 输入同样不抛异常，只返回错误列表
        self.assertIsInstance(models.validate_recognition_result(None), list)

    def test_09_normalize_keeps_canonical_quantity_and_raw(self):
        payload = {
            "project_id": "P-001",
            "project_name": "示例工程",
            "items": [{
                "item_id": "I-001",
                "item_name": "墙面PVC饰面",
                "material": "PVC",
                "process": "贴面",
                "spec": "",
                "quantity": {
                    "value": 53.7812, "type": "面积", "unit": "㎡",
                    "review_status": "confirmed",
                },
                "dimensions": {"长": 10, "宽": 5},
                "evidence": ["img_001.png"],
                "自定义字段": "保留我",
            }],
        }
        self.assertEqual(models.validate_recognition_result(payload), [])
        result = models.normalize_recognition_result(payload)
        item = result["items"][0]
        self.assertEqual(result["project_id"], "P-001")
        self.assertEqual(result["validation_errors"], [])
        self.assertEqual(item["validation_errors"], [])
        self.assertEqual(item["quantity"]["unit"], "m2")
        self.assertEqual(item["quantity"]["type"], "area")
        self.assertEqual(item["quantity"]["value"], 53.7812)
        self.assertEqual(item["quantity"]["review_status"], "confirmed")
        self.assertEqual(item["raw"]["dimensions"], {"长": 10, "宽": 5})
        self.assertEqual(item["raw"]["evidence"], ["img_001.png"])
        self.assertEqual(item["raw"]["自定义字段"], "保留我")
        self.assertEqual(item["dimensions"], {"长": 10, "宽": 5})

    def test_10_folded_quantity_fallback_with_parse_notes(self):
        payload = {
            "project_id": "P-001",
            "items": [{
                "item_id": "I-001",
                "item_name": "墙面PVC饰面",
                "material": "PVC",
                "process": "贴面",
                "value": 10,
                "type": "area",
                "unit": "m2",
                "review_status": "confirmed",
            }],
        }
        self.assertEqual(models.validate_recognition_result(payload), [])
        item = models.normalize_recognition_result(payload)["items"][0]
        self.assertEqual(item["quantity"]["value"], 10)
        self.assertEqual(item["quantity"]["type"], "area")
        self.assertEqual(item["quantity"]["unit"], "m2")
        self.assertEqual(item["quantity"]["review_status"], "confirmed")
        self.assertTrue(
            any("折平" in note or "兜底" in note
                for note in item["parse_notes"]),
            f"缺少折平兜底提示：{item['parse_notes']}",
        )


class TestThreeStateMatching(_DatabaseMixin, unittest.TestCase):
    """C. 三态匹配与价格来源可追溯。"""

    def test_11_exact_match_uses_latest_confirmed_price(self):
        item = _normalized_item(
            item_name="墙面PVC饰面", material="PVC", process="贴面",
            value=10, qtype="area", unit="m2",
        )
        result = matching.match_price(self.conn, item, "customer_quote")
        self.assertEqual(result["match_status"], "exact")
        self.assertEqual(result["recommended_unit_price"], 100.0)
        self.assertEqual(result["pricing_unit"], "m2")
        self.assertNotIn(result["recommended_unit_price"], (86.0, 92.5))
        source = result["price_source"]
        self.assertIsInstance(source, dict)
        self.assertEqual(set(source), SOURCE_KEYS)
        self.assertEqual(len(SOURCE_KEYS), 14)
        self.assertEqual(int(source["is_demo"]), 1)
        self.assertEqual(int(source["is_confirmed"]), 1)
        row = db.fetch_one(
            self.conn, "SELECT * FROM price_history WHERE id=?",
            (source["price_history_id"],),
        )
        self.assertIsNotNone(row)
        self.assertEqual(float(row["unit_price"]), 100.0)
        self.assertEqual(int(row["is_demo"]), 1)

    def test_12_alias_normalization_still_exact(self):
        item = _normalized_item(
            item_name="PVC饰面板墙面", material="PVC", process="贴面",
            value=10, qtype="area", unit="m2",
        )
        result = matching.match_price(self.conn, item, "customer_quote")
        self.assertEqual(result["match_status"], "exact")
        self.assertEqual(result["recommended_unit_price"], 100.0)

    def test_13_fuzzy_match_is_reference_only(self):
        item = _normalized_item(
            item_name="木饰面板墙面", material="木质", process="安装",
            value=10, qtype="area", unit="m2",
        )
        result = matching.match_price(self.conn, item, "customer_quote")
        self.assertEqual(result["match_status"], "fuzzy")
        self.assertTrue(result["candidates"])
        joined = "".join(result["messages"])
        self.assertTrue(
            "仅供参考" in joined or "人工确认" in joined, joined
        )
        self.assertIsNotNone(result["recommended_unit_price"])
        for candidate in result["candidates"]:
            self.assertEqual(
                int(candidate["price_source"]["is_confirmed"]), 1
            )

    def test_14_no_match_waits_for_manual_input(self):
        item = _normalized_item(
            item_name="玻璃隔断安装", material="玻璃", process="安装",
            value=10, qtype="area", unit="m2",
        )
        result = matching.match_price(self.conn, item, "customer_quote")
        self.assertEqual(result["match_status"], "none")
        self.assertIsNone(result["recommended_unit_price"])
        self.assertEqual(result["candidates"], [])
        self.assertTrue(
            any("暂无可靠价格，等待人工填写" in m for m in result["messages"]),
            result["messages"],
        )

    def test_15_invalid_price_type_lists_four_legal_values(self):
        item = _normalized_item(
            item_name="墙面PVC饰面", material="PVC", process="贴面",
            value=10, qtype="area", unit="m2",
        )
        with self.assertRaises(ValueError) as ctx:
            matching.match_price(self.conn, item, "bogus")
        message = str(ctx.exception)
        self.assertEqual(len(models.PRICE_TYPES), 4)
        for legal in models.PRICE_TYPES:
            self.assertIn(legal, message)

    def test_16_unconfirmed_price_never_auto_recommended(self):
        rows = db.fetch_all(
            self.conn,
            "SELECT ph.* FROM price_history ph "
            "JOIN item_catalog ic ON ic.id = ph.item_id "
            "WHERE ic.canonical_name=? AND ph.price_type=?",
            ("墙面PVC饰面", "outsource_cost"),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["is_confirmed"]), 0)
        self.assertEqual(float(rows[0]["unit_price"]), 78.0)
        confirmed = db.fetch_one(
            self.conn,
            "SELECT COUNT(*) AS n FROM price_history ph "
            "JOIN item_catalog ic ON ic.id = ph.item_id "
            "WHERE ic.canonical_name=? AND ph.price_type=? "
            "AND ph.is_confirmed=1",
            ("墙面PVC饰面", "outsource_cost"),
        )
        self.assertEqual(int(confirmed["n"]), 0)

        item = _normalized_item(
            item_name="墙面PVC饰面", material="PVC", process="贴面",
            value=10, qtype="area", unit="m2",
        )
        result = matching.match_price(self.conn, item, "outsource_cost")
        self.assertNotEqual(result["match_status"], "exact")
        self.assertIsNone(result["recommended_unit_price"])
        self.assertTrue(
            any("暂无可靠价格，等待人工填写" in m for m in result["messages"])
        )


class TestAmountCalculation(unittest.TestCase):
    """D-1. Decimal 合价与 ROUND_HALF_UP（纯逻辑）。"""

    def test_17_calculate_amount_two_decimals(self):
        self.assertEqual(
            pricing.calculate_amount(Decimal("53.7812"), Decimal("100.00")),
            Decimal("5378.12"),
        )
        self.assertEqual(
            pricing.calculate_amount(Decimal("0.005"), Decimal("100")),
            Decimal("0.50"),
        )

    def test_17b_calculate_amount_half_up_boundary(self):
        self.assertEqual(
            pricing.calculate_amount(Decimal("0.125"), Decimal("1")),
            Decimal("0.13"),
        )
        self.assertEqual(
            pricing.calculate_amount(Decimal("0.124"), Decimal("1")),
            Decimal("0.12"),
        )


class TestUnitAndPricing(_DatabaseMixin, unittest.TestCase):
    """D-2. 单位校验、跨维度拦截、换算规则与正式合价完整链路。"""

    def test_18_full_chain_exact_confirmed(self):
        item = _normalized_item(
            item_name="墙面PVC饰面", material="PVC", process="贴面",
            value=53.7812, qtype="area", unit="m2",
            review_status="confirmed",
        )
        self.assertEqual(item["validation_errors"], [])
        row = pricing.price_item(self.conn, item, "customer_quote")
        self.assertEqual(row["amount"], 5378.12)
        self.assertEqual(row["match_status"], "exact")
        self.assertIs(row["needs_confirmation"], False)
        self.assertEqual(row["final_unit_price"], 100.0)
        self.assertEqual(row["quantity"]["value"], 53.7812)
        self.assertEqual(row["pricing_quantity"], 53.7812)

    def test_19_cross_dimension_blocked(self):
        item = _normalized_item(
            item_name="墙面PVC饰面", material="PVC", process="贴面",
            value=12, qtype="volume", unit="m3",
        )
        self.assertEqual(item["validation_errors"], [])
        row = pricing.price_item(self.conn, item, "customer_quote")
        self.assertIsNone(row["amount"])
        self.assertIs(row["needs_confirmation"], True)
        joined = "".join(row["messages"])
        self.assertIn("不属于同一维度", joined)
        self.assertIn("禁止直接相乘", joined)
        self.assertNotEqual(row["amount"], 1200.0)

    def test_20_unconfirmed_quantity_suspended(self):
        item = _normalized_item(
            item_name="墙面乳胶漆", material="乳胶漆", process="涂刷",
            value=20, qtype="area", unit="m2", review_status="pending",
        )
        self.assertEqual(item["validation_errors"], [])
        row = pricing.price_item(self.conn, item, "customer_quote")
        self.assertIsNone(row["amount"])
        self.assertTrue(
            any("未经人工确认" in m for m in row["messages"]),
            row["messages"],
        )

    def test_21_confirmed_conversion_rule(self):
        item = _normalized_item(
            item_name="墙面乳胶漆", material="乳胶漆", process="涂刷",
            value=50000, qtype="area", unit="cm2",
            review_status="confirmed",
        )
        self.assertEqual(item["validation_errors"], [])
        row = pricing.price_item(self.conn, item, "customer_quote")
        # 并发变更中：该行 match_status 即将由 none 改为 exact，本轮不断言
        self.assertAlmostEqual(row["pricing_quantity"], 5.0, places=9)
        self.assertAlmostEqual(row["amount"], 160.00, places=9)
        self.assertIs(row["needs_confirmation"], False)

    def test_22_unconfirmed_conversion_rule_blocked(self):
        item = _normalized_item(
            item_name="不锈钢护栏", material="不锈钢", process="安装",
            value=300, qtype="length", unit="cm",
        )
        self.assertEqual(item["validation_errors"], [])
        row = pricing.price_item(self.conn, item, "customer_quote")
        self.assertIsNone(row["amount"])
        self.assertTrue(
            any("换算规则未经确认" in m for m in row["messages"]),
            row["messages"],
        )

    def test_23_fuzzy_never_counts_as_formal_amount(self):
        item = _normalized_item(
            item_name="木饰面板墙面", material="木质", process="安装",
            value=10, qtype="area", unit="m2",
        )
        row = pricing.price_item(self.conn, item, "customer_quote")
        self.assertIsNone(row["amount"])
        self.assertIs(row["needs_confirmation"], True)
        if row["provisional_amount"] is not None:
            self.assertNotEqual(row["provisional_amount"], row["amount"])

    def test_24_no_match_never_fabricates_price(self):
        item = _normalized_item(
            item_name="玻璃隔断安装", material="玻璃", process="安装",
            value=10, qtype="area", unit="m2",
        )
        row = pricing.price_item(self.conn, item, "customer_quote")
        self.assertIsNone(row["amount"])
        self.assertIsNone(row["final_unit_price"])
        self.assertIsNone(row["recommended_unit_price"])

    def test_25_check_unit_same_unit_and_confirmed_rule(self):
        same = pricing.check_unit(self.conn, "area", "m2", "m2", 10)
        self.assertTrue(same["ok"])
        self.assertEqual(same["factor"], Decimal("1"))
        self.assertEqual(same["pricing_quantity"], Decimal("10"))
        self.assertIsNone(same["rule_id"])

        converted = pricing.check_unit(
            self.conn, "area", "cm2", "m2", 50000
        )
        self.assertTrue(converted["ok"])
        self.assertEqual(converted["factor"], Decimal("0.0001"))
        self.assertEqual(converted["pricing_quantity"], Decimal("5"))
        self.assertIsNotNone(converted["rule_id"])

        unconfirmed = pricing.check_unit(
            self.conn, "length", "cm", "m", 300
        )
        self.assertFalse(unconfirmed["ok"])
        self.assertIsNone(unconfirmed["pricing_quantity"])
        self.assertIn("换算规则未经确认", unconfirmed["reason"])

    def test_26_check_unit_cross_dimension(self):
        result = pricing.check_unit(
            self.conn, "volume", "m3", "m2", 12
        )
        self.assertFalse(result["ok"])
        self.assertIsNone(result["pricing_quantity"])
        self.assertIn("不属于同一维度", result["reason"])
        self.assertIn("禁止直接相乘", result["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
