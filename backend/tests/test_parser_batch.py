"""批量解析与校验协作回归测试：只用标准库 unittest，直接跑服务层。

运行：backend/.venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import threading
import unittest
from typing import Any

from app.seed import SEED_ROWS
from app.services.parser_batch import (
    POLICY_HIT,
    POLICY_LATEST,
    parser_batch_service,
)
from app.services.validator_audit import validator_audit_service
from app.store import store

BATCH = "parser_batch"
RECORD = "parser_record"
REVIEW = "parser_review"


class BatchTestCase(unittest.TestCase):
    """每个用例从干净的示例数据出发，互不影响。"""

    def setUp(self) -> None:
        store._tables = {name: [copy.deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()}
        self.__class__._locks = {}

    def create(self, ids: list[int], policy: str):
        return parser_batch_service.create_batch(ids, policy, "测试")

    # ---------------------------------------------------------------- 批次前置
    def test_two_policies_keep_results_and_differ_in_mapping_versions(self) -> None:
        latest, _, errors = self.create([1, 2, 3], POLICY_LATEST)
        self.assertEqual(errors, [])
        self.assertIsNotNone(latest)
        hit, _, errors = self.create([1, 2, 3], POLICY_HIT)
        self.assertEqual(errors, [])
        self.assertIsNotNone(hit)

        self.assertEqual(
            {item["映射版本"] for item in latest["items"]}, {"v2"},
            "按最新确认映射解析时整批映射版本应统一",
        )
        self.assertEqual(
            [item["映射版本"] for item in hit["items"]], ["v2", "v2", "v1"],
            "按输入命中映射解析时应保持每条输入与历史映射连续",
        )
        # 两种口径下业务字段值一致
        strip = lambda batch: [item["标准化字段"] for item in batch["items"]]
        self.assertEqual(strip(latest), strip(hit))
        self.assertEqual(latest["字段类型表"], hit["字段类型表"])

    def test_type_conflict_rejects_whole_batch_with_per_input_reason(self) -> None:
        # JSON v2 把金额声明成 string，与 XML v2 的 decimal 冲突
        store.find("parser_mapping", 4)["类型转换"]["金额"] = "string"
        batch, message, errors = self.create([2, 3], POLICY_LATEST)
        self.assertIsNone(batch)
        self.assertIn("字段类型不一致", message)
        self.assertTrue(errors)
        self.assertTrue(all("金额" in error["失败原因"] for error in errors))
        self.assertEqual(store.rows(BATCH), [], "前置校验失败不应落半成品批次")

    def test_missing_hit_mapping_rejects_batch(self) -> None:
        store.find("parser_input", 3)["命中映射编号"] = "MAP-XML-999"
        batch, _, errors = self.create([1, 3], POLICY_HIT)
        self.assertIsNone(batch)
        self.assertEqual(errors[0]["输入"], "IN-XML-01")
        self.assertIn("MAP-XML-999", errors[0]["失败原因"])

    def test_duplicate_or_unknown_input_rejected(self) -> None:
        batch, message, errors = self.create([1, 1], POLICY_LATEST)
        self.assertIsNone(batch)
        self.assertIn("重复", errors[0]["失败原因"])
        batch, _, errors = self.create([999], POLICY_LATEST)
        self.assertIsNone(batch)
        self.assertIn("不存在", errors[0]["失败原因"])

    def test_unsupported_policy_rejected(self) -> None:
        batch, message, _ = self.create([1], "随便")
        self.assertIsNone(batch)
        self.assertIn("不受支持", message)

    # ---------------------------------------------------------------- 逐条解析
    def test_item_failure_is_traceable_to_mapping_and_field(self) -> None:
        batch, _, errors = self.create([1, 5], POLICY_HIT)
        self.assertEqual(errors, [])
        self.assertEqual(batch["状态"], "部分失败")
        failed = [item for item in batch["items"] if item["状态"] == "失败"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["输入编号"], "IN-XML-02")
        self.assertEqual(failed[0]["映射编号"], "MAP-XML-002")
        self.assertIn("金额", failed[0]["失败原因"])
        self.assertIn("decimal", failed[0]["失败原因"])

    def test_field_package_keeps_field_order_and_types(self) -> None:
        batch, _, _ = self.create([1, 3], POLICY_LATEST)
        package = batch["标准化字段包"]
        self.assertEqual(package["字段顺序"], ["订单号", "金额", "下单时间", "客户", "优惠金额"])
        self.assertEqual(set(package["字段类型表"].values()), {"string", "decimal", "datetime"})
        for item in package["条目"]:
            self.assertEqual(list(item["标准化字段"].keys()), package["字段顺序"])
            self.assertIn("映射版本", item)
            self.assertIn("来源文件", item)

    # ---------------------------------------------------------------- 校验协作
    def test_validation_summarises_by_source_file_and_writes_back(self) -> None:
        batch, _, _ = self.create([1, 4, 5], POLICY_HIT)
        result, message = validator_audit_service.review(batch["id"], reviewer="验收员")
        self.assertIsNotNone(result)
        self.assertIn("复核完成", message)
        self.assertEqual(result["汇总"]["类型异常数"], 1)
        self.assertGreaterEqual(result["汇总"]["缺失字段数"], 1)
        files = {row["来源文件"]: row for row in result["按来源文件"]}
        self.assertEqual(files["orders_202611_legacy.xml"]["结论"], "待复核")
        self.assertIn("金额", [entry["字段"] for entry in files["orders_202611_legacy.xml"]["类型异常"]])

        stored = parser_batch_service.get_batch(batch["id"])
        self.assertEqual(stored["校验状态"], "已校验")
        self.assertEqual(stored["复核版本"], 1)
        self.assertTrue(stored["有效复核"]["有效"])
        self.assertTrue(stored["标准化字段包"]["校验摘要"])

        records = parser_batch_service.list_records(batch["批次编号"])
        self.assertEqual(len(records), len(batch["items"]))
        self.assertTrue(all(record["复核版本"] == 1 for record in records))
        reviews = parser_batch_service.review_rows(batch["批次编号"], "待复核")
        self.assertTrue(reviews)
        self.assertTrue(all(row["批次编号"] == batch["批次编号"] for row in reviews))

    def test_validation_only_reads_field_values(self) -> None:
        batch, _, _ = self.create([1, 2], POLICY_LATEST)
        before = [dict(item["标准化字段"]) for item in batch["items"]]
        validator_audit_service.review(batch["id"])
        after = [dict(item["标准化字段"]) for item in parser_batch_service.get_batch(batch["id"])["items"]]
        self.assertEqual(before, after)

    def test_concurrent_reviews_leave_single_effective_result(self) -> None:
        batch, _, _ = self.create([1, 2, 3], POLICY_HIT)
        results: list[tuple[Any, str]] = []

        def worker() -> None:
            results.append(validator_audit_service.review(batch["id"]))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        succeeded = [message for result, message in results if result is not None]
        self.assertEqual(len(succeeded), 1, "每批并发复核只允许一份有效结果")
        conclusion = validator_audit_service.get_conclusion(batch["id"])
        self.assertEqual(conclusion["复核版本"], 1)
        self.assertEqual(sum(1 for row in conclusion["复核历史"] if row["有效"]), 1)

    def test_review_requires_expected_version_to_replace(self) -> None:
        batch, _, _ = self.create([1], POLICY_LATEST)
        validator_audit_service.review(batch["id"])
        result, message = validator_audit_service.review(batch["id"])
        self.assertIsNone(result)
        self.assertIn("已有有效复核结论", message)
        result, message = validator_audit_service.review(batch["id"], expected_version=1)
        self.assertIsNotNone(result)
        self.assertIn("v2", message)

    # ---------------------------------------------------------------- 重算
    def test_reparse_uses_old_snapshot_and_keeps_original_digest(self) -> None:
        batch, _, _ = self.create([1, 3], POLICY_LATEST)
        validator_audit_service.review(batch["id"])
        original = batch["原摘要"]
        versions_before = [(item["映射编号"], item["映射版本"]) for item in batch["items"]]

        # 新映射版本确认后，旧批次重算仍以冻结快照为准
        store.rows("parser_mapping").append({
            "id": 99, "映射编号": "MAP-XML-003", "输入格式": "XML", "映射版本": "v3",
            "字段映射": [{"来源字段": "id", "目标字段": "订单号"}],
            "类型转换": {"订单号": "string"}, "必需字段": ["订单号"], "状态": "可用",
            "确认时间": "2026-12-01 00:00:00", "备注": "新版本",
        })
        stored, message = parser_batch_service.reparse_batch(batch["id"])
        self.assertIn("原摘要保留", message)
        self.assertEqual(stored["原摘要"], original)
        self.assertTrue(stored["原摘要不变"])
        self.assertEqual(
            [(item["映射编号"], item["映射版本"]) for item in stored["items"]], versions_before,
        )
        self.assertEqual(stored["重算次数"], 1)
        self.assertEqual(stored["校验状态"], "复核失效")
        self.assertIsNone(stored["有效复核"])
        self.assertEqual(parser_batch_service.list_records(batch["批次编号"]), [])
        history = validator_audit_service.get_conclusion(batch["id"])["复核历史"]
        self.assertEqual(len(history), 1)
        self.assertFalse(history[0]["有效"])
        self.assertIn("重算", history[0]["失效原因"])

    def test_original_and_current_digest_diverge_when_input_file_changes(self) -> None:
        batch, _, _ = self.create([1], POLICY_LATEST)
        original = batch["原摘要"]
        store.find("parser_input", 1)["内容"] = (
            "订单号,金额,下单时间,客户,优惠金额\nA-9999,1.00,2026-11-17 10:20:00,知行科技,0.00\n"
        )
        stored, _ = parser_batch_service.reparse_batch(batch["id"])
        self.assertEqual(stored["原摘要"], original, "原摘要必须保留旧版本结果")
        self.assertNotEqual(stored["当前摘要"], stored["原摘要"])
        self.assertEqual(stored["items"][0]["标准化字段"]["订单号"], "A-9999")


if __name__ == "__main__":
    unittest.main()
