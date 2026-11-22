"""第 2 轮回归测试：接口层 api.py + xlsx.py + 顶层导出（验收整个模块）。

约束：
- 仅标准库 unittest / tempfile / zipfile / xml.etree.ElementTree / sqlite3。
- 只新建 tests/ 下文件；不修改 quotation/、examples/、data/、output/、文档。
- 严禁使用 output/quote_demo.db；每测试用独立临时库 init_database(force=True)。
- 不 import / 不运行 examples/demo.py，不依赖 README。
- 固定夹具：6 条识图项目，price_type='customer_quote'。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import quotation  # noqa: E402
from quotation import api, db, models, seed, xlsx  # noqa: E402

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _recognition_fixture() -> dict:
    """固定 6 条识图项目夹具（与任务约定一致，另给 item001 加原始字段）。"""
    return {
        "project_id": "P-QA-API-001",
        "project_name": "QA接口回归项目",
        "items": [
            {
                "item_id": "item001", "item_name": "墙面PVC饰面",
                "material": "PVC", "process": "贴面", "spec": "",
                "quantity": {"value": 53.7812, "type": "area", "unit": "m2",
                             "review_status": "confirmed"},
                "施工部位": "一层大堂", "evidence": ["img_001.png"],
            },
            {
                "item_id": "item002", "item_name": "木饰面板墙面",
                "material": "木质", "process": "安装", "spec": "",
                "quantity": {"value": 10, "type": "area", "unit": "m2",
                             "review_status": "confirmed"},
            },
            {
                "item_id": "item003", "item_name": "玻璃隔断安装",
                "material": "玻璃", "process": "安装", "spec": "",
                "quantity": {"value": 5, "type": "area", "unit": "m2",
                             "review_status": "confirmed"},
            },
            {
                "item_id": "item004", "item_name": "墙面PVC饰面",
                "material": "PVC", "process": "贴面", "spec": "",
                "quantity": {"value": 12, "type": "volume", "unit": "m3",
                             "review_status": "confirmed"},
            },
            {
                "item_id": "item005", "item_name": "墙面乳胶漆",
                "material": "乳胶漆", "process": "涂刷", "spec": "",
                "quantity": {"value": 20, "type": "area", "unit": "m2",
                             "review_status": "pending"},
            },
            {
                "item_id": "item006", "item_name": "墙面乳胶漆",
                "material": "乳胶漆", "process": "涂刷", "spec": "",
                "quantity": {"value": 50000, "type": "area", "unit": "cm2",
                             "review_status": "confirmed"},
            },
        ],
    }


class _ApiMixin:
    """每个测试独立临时库；所有直连查询用完即关，先关连接再清理目录。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="qa_api_")
        self.db_path = os.path.join(self._tmp.name, "api.db")
        seed.init_database(self.db_path, force=True)
        self.fixture = _recognition_fixture()

    def tearDown(self):
        self._tmp.cleanup()  # 所有 api 调用内部均已 close 连接
        super().tearDown()

    def _query(self, sql, params=()):
        conn = db.connect(self.db_path)
        try:
            return db.fetch_all(conn, sql, params)
        finally:
            conn.close()

    def _query_one(self, sql, params=()):
        conn = db.connect(self.db_path)
        try:
            return db.fetch_one(conn, sql, params)
        finally:
            conn.close()

    def _generate(self, recognition=None, price_type="customer_quote"):
        return api.generate_quote(
            recognition if recognition is not None else self.fixture,
            price_type, self.db_path,
        )

    def _generate_priced(self):
        """草案态对 item002/item003 人工填价，使 4 行有最终单价、2 行挂起。"""
        result = self._generate()
        qid = result["quote_id"]
        api.update_quote_item_price(qid, "item002", 180.0, self.db_path)
        api.update_quote_item_price(qid, "item003", 55.0, self.db_path)
        return api.get_quote(qid, self.db_path)


class TestApiGenerateAndRead(_ApiMixin, unittest.TestCase):
    """用例 1-4、15-16：生成草案、落库边界、往返读取、畸形输入。"""

    def test_01_generate_quote_summary(self):
        result = self._generate()
        for key in (
            "quote_id", "project_id", "project_name", "price_type",
            "price_type_label", "generated_at", "status", "currency", "items",
            "total_amount", "provisional_amount", "pending_items", "warnings",
            "database_path",
        ):
            self.assertIn(key, result)
        self.assertEqual(len(result["items"]), 6)
        self.assertEqual(result["total_amount"], 5538.12)
        self.assertEqual(result["provisional_amount"], 1680.0)
        self.assertEqual(len(result["pending_items"]), 4)
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["price_type_label"], "对客报价")

        reasons = [p["reason"] for p in result["pending_items"]]
        self.assertTrue(any("相似匹配" in r for r in reasons), reasons)
        self.assertTrue(
            any("暂无可靠价格" in r or "无匹配" in r for r in reasons), reasons
        )
        self.assertTrue(
            any("计价单位" in r or "换算规则" in r or "维度" in r
                for r in reasons), reasons
        )
        self.assertTrue(any("工程量" in r for r in reasons), reasons)
        warnings = "".join(result["warnings"])
        self.assertIn("模拟/脱敏", warnings)
        self.assertIn("待蒋确认", warnings)

    def test_02_draft_does_not_write_price_history(self):
        result = self._generate()
        self.assertEqual(seed.table_counts(self.db_path)["price_history"], 27)
        records = self._query("SELECT * FROM quote_records")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "draft")
        self.assertEqual(records[0]["quote_id"], result["quote_id"])
        self.assertEqual(len(self._query("SELECT * FROM quote_items")), 6)

    def test_03_get_quote_round_trip(self):
        result = self._generate()
        loaded = api.get_quote(result["quote_id"], self.db_path)
        self.assertEqual(len(loaded["items"]), 6)
        item1 = next(i for i in loaded["items"] if i["item_id"] == "item001")
        self.assertIsInstance(item1["price_source"], dict)
        self.assertIsNotNone(item1["price_source"]["price_history_id"])
        self.assertEqual(float(item1["price_source"]["unit_price"]), 100.0)
        self.assertIsInstance(item1["messages"], list)
        self.assertIsInstance(item1["candidates"], list)
        self.assertIsInstance(item1["quantity"], dict)
        self.assertEqual(item1["quantity"]["unit"], "m2")
        self.assertEqual(item1["quantity"]["type"], "area")
        self.assertIn("施工部位", item1["raw"])
        self.assertEqual(item1["raw"]["施工部位"], "一层大堂")
        self.assertEqual(item1["raw"]["evidence"], ["img_001.png"])
        self.assertEqual(float(loaded["total_amount"]), 5538.12)

    def test_04_invalid_price_type_and_unknown_quote(self):
        with self.assertRaises(ValueError) as ctx:
            self._generate(price_type="bogus")
        self.assertEqual(len(models.PRICE_TYPES), 4)
        for legal in models.PRICE_TYPES:
            self.assertIn(legal, str(ctx.exception))
        with self.assertRaises(ValueError):
            api.get_quote("Q-NOT-EXIST", self.db_path)

    def test_15_malformed_recognition_result_no_crash(self):
        cases = [
            ("缺 quantity",
             {"item_id": "bad1", "item_name": "缺工程量"}),
            ("value 为字符串",
             {"item_id": "bad2", "item_name": "值非数值",
              "quantity": {"value": "abc", "type": "area", "unit": "m2",
                           "review_status": "confirmed"}}),
            ("unit 未知",
             {"item_id": "bad3", "item_name": "单位未知",
              "quantity": {"value": 1, "type": "area", "unit": "kg",
                           "review_status": "confirmed"}}),
        ]
        for label, item in cases:
            with self.subTest(case=label):
                payload = {"project_id": "P-MALFORMED", "items": [item]}
                result = self._generate(recognition=payload)  # 期望不抛异常
                row = result["items"][0]
                self.assertTrue(row["validation_errors"])
                self.assertIsNone(row["amount"])
                self.assertTrue(
                    any("识图结果校验未通过" in p["reason"]
                        for p in result["pending_items"])
                )

    def test_16_none_row_never_priced_after_round_trip(self):
        result = self._generate()
        item3 = next(i for i in result["items"] if i["item_id"] == "item003")
        self.assertEqual(item3["match_status"], "none")
        loaded = api.get_quote(result["quote_id"], self.db_path)
        row = next(i for i in loaded["items"] if i["item_id"] == "item003")
        self.assertIsNone(row["recommended_unit_price"])
        self.assertIsNone(row["final_unit_price"])
        self.assertIsNone(row["amount"])
        self.assertTrue(row["needs_confirmation"])


class TestApiManualPricing(_ApiMixin, unittest.TestCase):
    """用例 5-6：草案态人工改价与边界保护。"""

    def test_05_manual_price_override(self):
        result = self._generate()
        qid = result["quote_id"]
        updated = api.update_quote_item_price(qid, "item002", 180.0, self.db_path)
        item2 = next(i for i in updated["items"] if i["item_id"] == "item002")
        self.assertEqual(item2["match_status"], "manual")
        self.assertEqual(float(item2["final_unit_price"]), 180.0)
        self.assertEqual(float(item2["amount"]), 1800.0)
        self.assertIsNone(item2["price_source"])
        self.assertEqual(seed.table_counts(self.db_path)["price_history"], 27)
        record = self._query_one(
            "SELECT total_amount FROM quote_records WHERE quote_id=?", (qid,)
        )
        self.assertEqual(float(record["total_amount"]), 7338.12)

        api.update_quote_item_price(qid, "item003", 55.0, self.db_path)
        item3 = next(
            i for i in api.get_quote(qid, self.db_path)["items"]
            if i["item_id"] == "item003"
        )
        self.assertEqual(float(item3["amount"]), 275.0)

    def test_06_update_price_validation_boundaries(self):
        qid = self._generate()["quote_id"]
        for bad in (0, -1, "abc", None):
            with self.subTest(unit_price=bad):
                with self.assertRaises(ValueError):
                    api.update_quote_item_price(qid, "item002", bad, self.db_path)
        with self.assertRaises(ValueError):
            api.update_quote_item_price(qid, "item999", 100.0, self.db_path)

        api.update_quote_item_price(qid, "item002", 180.0, self.db_path)
        api.update_quote_item_price(qid, "item003", 55.0, self.db_path)
        api.confirm_quote(qid, None, "老板", self.db_path, allow_unpriced=True)
        with self.assertRaises(ValueError):
            api.update_quote_item_price(qid, "item002", 200.0, self.db_path)


class TestApiConfirmFlow(_ApiMixin, unittest.TestCase):
    """用例 7-11：未定价保护、只追加回写、重复确认、事务原子性。"""

    def test_07_unpriced_protection(self):
        qid = self._generate_priced()["quote_id"]
        with self.assertRaises(ValueError) as ctx:
            api.confirm_quote(qid, None, "老板", self.db_path,
                              allow_unpriced=False)
        self.assertIn("item004", str(ctx.exception))
        self.assertIn("item005", str(ctx.exception))

    def test_08_confirm_writes_append_only_history(self):
        qid = self._generate_priced()["quote_id"]
        snapshot = self._query(
            "SELECT id, unit_price, quote_date, source_type "
            "FROM price_history ORDER BY id"
        )
        self.assertEqual(len(snapshot), 27)
        self.assertTrue(any(float(r["unit_price"]) == 100.0 for r in snapshot))

        result = api.confirm_quote(
            qid, None, "老板", self.db_path,
            allow_unpriced=True, note="确认备注",
        )
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["history_records_added"], 4)
        self.assertEqual(seed.table_counts(self.db_path)["price_history"], 31)

        after = self._query(
            "SELECT id, unit_price, quote_date, source_type "
            "FROM price_history ORDER BY id"
        )
        self.assertEqual(
            [tuple(r) for r in after[:27]],
            [tuple(r) for r in snapshot],
            "确认回写覆盖了前 27 条旧价",
        )
        self.assertTrue(any(float(r["unit_price"]) == 100.0 for r in after))

        new_rows = self._query(
            "SELECT * FROM price_history WHERE id > ? ORDER BY id",
            (snapshot[-1]["id"],),
        )
        self.assertEqual(len(new_rows), 4)
        for row in new_rows:
            self.assertEqual(row["source_type"], "quote_confirmation")
            self.assertEqual(row["source_ref"], qid)
            self.assertEqual(int(row["is_confirmed"]), 1)

        record = self._query_one(
            "SELECT status, confirmed_by, confirmed_at "
            "FROM quote_records WHERE quote_id=?", (qid,)
        )
        self.assertEqual(record["status"], "confirmed")
        self.assertTrue(str(record["confirmed_by"]))
        self.assertTrue(str(record["confirmed_at"]))

    def test_09_duplicate_confirm_rejected(self):
        qid = self._generate_priced()["quote_id"]
        api.confirm_quote(qid, None, "老板", self.db_path, allow_unpriced=True)
        with self.assertRaises(ValueError):
            api.confirm_quote(qid, None, "老板", self.db_path,
                              allow_unpriced=True)
        self.assertEqual(seed.table_counts(self.db_path)["price_history"], 31)

    def test_10_transaction_atomicity_on_history_insert_failure(self):
        qid = self._generate_priced()["quote_id"]
        before = self._query_one(
            "SELECT final_unit_price, match_status FROM quote_items "
            "WHERE quote_id=? AND item_id=?", (qid, "item002")
        )
        self.assertEqual(float(before["final_unit_price"]), 180.0)

        conn = db.connect(self.db_path)
        try:
            conn.executescript(
                "CREATE TRIGGER trg_force_fail BEFORE INSERT ON price_history "
                "WHEN NEW.source_type='quote_confirmation' "
                "BEGIN SELECT RAISE(ABORT,'forced failure'); END;"
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(api.QuoteSaveError) as ctx:
            api.confirm_quote(
                qid, [{"item_id": "item002", "final_unit_price": 200.0}],
                "老板", self.db_path, allow_unpriced=True,
            )
        self.assertIn("数据库保存失败", str(ctx.exception))
        self.assertIn("已回滚", str(ctx.exception))

        record = self._query_one(
            "SELECT status FROM quote_records WHERE quote_id=?", (qid,)
        )
        self.assertEqual(record["status"], "draft")
        self.assertEqual(seed.table_counts(self.db_path)["price_history"], 27)
        after = self._query_one(
            "SELECT final_unit_price, match_status FROM quote_items "
            "WHERE quote_id=? AND item_id=?", (qid, "item002")
        )
        self.assertEqual(
            float(after["final_unit_price"]), float(before["final_unit_price"])
        )
        self.assertEqual(after["match_status"], before["match_status"])

    def test_11_invalid_database_path_raises_quote_save_error(self):
        blocker = os.path.join(self._tmp.name, "notadir.db")
        Path(blocker).write_text("我不是目录", encoding="utf-8")
        bad_path = os.path.join(blocker, "child.db")
        with self.assertRaises(api.QuoteSaveError) as ctx:
            api.confirm_quote("Q-NOPE", None, "老板", bad_path,
                              allow_unpriced=True)
        self.assertIn("数据库保存失败", str(ctx.exception))


class TestExcelExport(_ApiMixin, unittest.TestCase):
    """用例 12-13：OOXML 结构、数值单元格、XML 转义。"""

    def test_12_export_quote_excel_structure(self):
        result = self._generate()
        out = os.path.join(self._tmp.name, "quote.xlsx")
        returned = api.export_quote_excel(result, out)
        self.assertTrue(Path(returned).is_file())

        with zipfile.ZipFile(returned, "r") as zf:
            names = zf.namelist()
            for index in (1, 2, 3):
                self.assertIn(f"xl/worksheets/sheet{index}.xml", names)
            for name in names:
                if name.endswith(".xml") or name.endswith(".rels"):
                    ET.fromstring(zf.read(name))  # 所有 XML 部件均可解析
            sheet_texts = {
                index: zf.read(f"xl/worksheets/sheet{index}.xml").decode("utf-8")
                for index in (1, 2, 3)
            }

        sheet1 = sheet_texts[1]
        self.assertIn("5378.12", sheet1)
        self.assertIn("合计", sheet1)
        self.assertIn("完全匹配", sheet1)
        self.assertIn("<v>5378.12</v>", sheet1)  # 金额为数值单元格
        self.assertIn("模拟/脱敏", sheet_texts[3])

        root1 = ET.fromstring(sheet1.encode("utf-8"))
        money_cells = []
        for cell in root1.iter(f"{{{MAIN_NS}}}c"):
            v = cell.find(f"{{{MAIN_NS}}}v")
            if v is not None and v.text == "5378.12":
                money_cells.append(cell)
        self.assertTrue(money_cells, "找不到数值型合价单元格")
        for cell in money_cells:
            self.assertNotEqual(cell.get("t"), "inlineStr")

        root2 = ET.fromstring(sheet_texts[2].encode("utf-8"))
        rows = root2.findall(f".//{{{MAIN_NS}}}row")
        self.assertGreaterEqual(len(rows), 2, "待确认项目 sheet 无数据行")

    def test_13_xml_escape_boundary(self):
        special = 'A & B <tag> "双引号" \'单引号\' 中文'
        out = os.path.join(self._tmp.name, "escape.xlsx")
        returned = xlsx.write_xlsx(
            [{
                "name": "特殊&<字符>",
                "columns": [
                    {"title": "文本", "width": 50, "kind": "text"},
                    {"title": "数值", "width": 14, "kind": "money"},
                ],
                "rows": [[special, 123.45]],
            }],
            out,
        )
        with zipfile.ZipFile(returned, "r") as zf:
            workbook = ET.fromstring(zf.read("xl/workbook.xml"))
            self.assertTrue(list(workbook), "workbook.xml 部件为空")
            root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        texts = [node.text or "" for node in root.iter(f"{{{MAIN_NS}}}t")]
        self.assertIn(special, texts, "特殊字符文本未无损还原")
        values = [node.text for node in root.iter(f"{{{MAIN_NS}}}v")]
        self.assertIn("123.45", values)


class TestPackageExports(unittest.TestCase):
    """用例 14：顶层 __all__ 导出完整性。"""

    def test_14_top_level_exports(self):
        from quotation import (
            QuoteSaveError, PRICE_TYPES, confirm_quote, export_quote_excel,
            generate_quote, get_quote, init_database,
            update_quote_item_price, validate_recognition_result,
        )
        names = (
            "generate_quote", "get_quote", "update_quote_item_price",
            "confirm_quote", "export_quote_excel", "init_database",
            "validate_recognition_result", "QuoteSaveError", "PRICE_TYPES",
        )
        for name in names:
            self.assertIn(name, quotation.__all__)
            self.assertTrue(hasattr(quotation, name), name)
        for func in (
            generate_quote, get_quote, update_quote_item_price,
            confirm_quote, export_quote_excel, init_database,
            validate_recognition_result,
        ):
            self.assertTrue(callable(func))
        self.assertTrue(issubclass(QuoteSaveError, Exception))
        self.assertEqual(len(PRICE_TYPES), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
