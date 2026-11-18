"""采集结果校验的批次核对能力：读取标准化字段包，回写解析批次并生成待复核清单。

校验服务把解析批次清单与标准化字段包当作只读输入：
- 不重新解析输入文件，也不改写字段值；
- 只按来源文件汇总字段缺失与类型异常；
- 结论回写到批次清单、字段包和业务记录，供格式解析服务回查。

同一批次并发复核时，只有第一份结论生效；后到的复核请求会带着
旧的复核版本被拒绝，历史结论留存但标记为失效。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.services.parser_batch import (
    BATCH_MODULE,
    CHECK_DONE,
    CHECK_STALE,
    parser_batch_service,
)
from app.store import store

REVIEW_PENDING = "待复核"

CONCLUSION_PASS = "通过"
CONCLUSION_REVIEW = "待复核"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ValidatorAuditService:
    """格式解析批次的核对与待复核清单生成。"""

    def __init__(self) -> None:
        self._locks_guard = threading.Lock()
        self._locks: dict[int, threading.Lock] = {}

    def _lock(self, batch_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(batch_id, threading.Lock())

    # ------------------------------------------------------------------ 读取
    def list_batches(self) -> list[dict[str, Any]]:
        """校验侧只看到批次可核对的摘要，不拿到可写引用。"""
        rows = []
        for batch in store.rows(BATCH_MODULE):
            package = batch.get("标准化字段包") or {}
            rows.append({
                "id": batch.get("id"),
                "批次编号": batch.get("批次编号"),
                "映射口径": batch.get("映射口径"),
                "状态": batch.get("状态"),
                "输入条数": batch.get("输入条数"),
                "成功条数": batch.get("成功条数"),
                "失败条数": batch.get("失败条数"),
                "字段顺序": list(package.get("字段顺序", [])),
                "字段类型表": dict(package.get("字段类型表", {})),
                "校验状态": batch.get("校验状态"),
                "复核状态": batch.get("复核状态"),
                "复核版本": batch.get("复核版本", 0),
                "有效复核版本": (batch.get("有效复核") or {}).get("复核版本"),
                "待复核条数": int((batch.get("校验摘要") or {}).get("待复核条数", 0)),
                "创建时间": batch.get("创建时间"),
                "校验时间": batch.get("校验时间"),
                "重算次数": batch.get("重算次数", 0),
            })
        return rows

    def list_reviews(self, batch_no: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows("parser_review")
        if batch_no:
            rows = [row for row in rows if row.get("批次编号") == batch_no]
        if status:
            rows = [row for row in rows if row.get("复核状态") == status]
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 核对
    def review(
        self,
        batch_id: int,
        *,
        reviewer: str | None = None,
        expected_version: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        batch = store.find(BATCH_MODULE, batch_id)
        if batch is None:
            return None, f"解析批次 {batch_id} 不存在或已归档"
        package = batch.get("标准化字段包") or {}
        if not package.get("字段顺序"):
            return None, "批次没有可核对的标准化字段包"

        with self._lock(batch_id):
            current_version = int(batch.get("复核版本", 0))
            if expected_version is not None and int(expected_version) != current_version:
                return None, (
                    f"批次已由其他复核生成有效结果（当前复核版本 v{current_version}），"
                    "本次结论未生效"
                )
            if batch.get("校验状态") == CHECK_DONE and expected_version is None and current_version:
                return None, (
                    f"批次已有有效复核结论 v{current_version}，如需重新核对请带上复核版本 "
                    f"{current_version} 重试"
                )

            summary, per_file, conclusions, marks, issues = self._aggregate(batch, package)
            version = current_version + 1
            records = self._build_records(batch, package, conclusions, marks, version)
            review_rows = self._build_review_rows(batch, issues, version, reviewer)
            parser_batch_service.record_validation(
                batch,
                summary=summary,
                per_file=per_file,
                conclusions=conclusions,
                marks=marks,
                records=records,
                review_rows=review_rows,
            )
            batch["核对人"] = str(reviewer or "").strip() or "值班校验"

        return {
            "批次编号": batch.get("批次编号"),
            "复核版本": version,
            "复核状态": batch.get("复核状态"),
            "校验状态": batch.get("校验状态"),
            "汇总": summary,
            "按来源文件": per_file,
            "结论": conclusions,
            "标记": marks,
            "业务记录数": len(records),
            "待复核清单": parser_batch_service.review_rows(str(batch.get("批次编号")), REVIEW_PENDING),
            "复核时间": batch.get("校验时间"),
        }, f"批次 {batch.get('批次编号')} 复核完成，复核版本 v{version}"

    def _aggregate(
        self,
        batch: dict[str, Any],
        package: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str], dict[str, dict[str, str]], list[dict[str, Any]]]:
        """按来源文件汇总缺失字段与类型异常，不改动字段值。"""
        per_file: list[dict[str, Any]] = []
        conclusions: dict[str, str] = {}
        marks: dict[str, dict[str, str]] = {}
        issues: list[dict[str, Any]] = []
        missing_total = 0
        type_total = 0
        uncovered = {
            str(item.get("输入编号")): set(item.get("映射未覆盖字段", []))
            for item in batch.get("items", [])
        }

        for item in package.get("条目", []):
            input_no = str(item.get("输入编号"))
            file_name = str(item.get("来源文件"))
            missing = [str(field) for field in item.get("缺失字段", [])]
            anomalies = [
                {"字段": str(entry.get("字段")), "原因": str(entry.get("原因"))}
                for entry in item.get("类型异常", [])
            ]
            item_marks: dict[str, str] = {}
            for field in missing:
                item_marks[field] = "缺失"
            for entry in anomalies:
                item_marks[entry["字段"]] = "类型异常"
            for field in package.get("字段顺序", []):
                item_marks.setdefault(str(field), "通过")

            needs_review = bool(missing or anomalies)
            conclusions[input_no] = CONCLUSION_REVIEW if needs_review else CONCLUSION_PASS
            marks[input_no] = item_marks
            missing_total += len(missing)
            type_total += len(anomalies)
            per_file.append({
                "输入编号": input_no,
                "来源文件": file_name,
                "映射版本": f"{item.get('映射编号')}@{item.get('映射版本')}",
                "缺失字段": missing,
                "类型异常": anomalies,
                "结论": conclusions[input_no],
            })
            uncovered_fields = uncovered.get(input_no, set())
            for field in missing:
                if field in uncovered_fields:
                    reason = f"当前映射没有覆盖「{field}」，字段包已按批次字段顺序补齐空值"
                else:
                    reason = f"「{field}」在标准化字段包中没有值"
                issues.append({
                    "输入编号": input_no,
                    "来源文件": file_name,
                    "字段": field,
                    "问题类型": "字段缺失",
                    "说明": reason,
                })
            for entry in anomalies:
                issues.append({
                    "输入编号": input_no,
                    "来源文件": file_name,
                    "字段": entry["字段"],
                    "问题类型": "类型异常",
                    "说明": entry["原因"],
                })

        summary = {
            "批次编号": batch.get("批次编号"),
            "输入条数": int(batch.get("输入条数", 0)),
            "缺失字段数": missing_total,
            "类型异常数": type_total,
            "待复核条数": sum(1 for value in conclusions.values() if value == CONCLUSION_REVIEW),
            "通过条数": sum(1 for value in conclusions.values() if value == CONCLUSION_PASS),
            "字段类型表": dict(package.get("字段类型表", {})),
            "核对时间": _now(),
        }
        return summary, per_file, conclusions, marks, issues

    def _build_records(
        self,
        batch: dict[str, Any],
        package: dict[str, Any],
        conclusions: dict[str, str],
        marks: dict[str, dict[str, str]],
        version: int,
    ) -> list[dict[str, Any]]:
        """校验服务接力生成业务记录；记录只引用批次与字段包，不复制可写状态。"""
        rows = store.rows("parser_record")
        start = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        items = {str(item.get("输入编号")): item for item in batch.get("items", [])}
        records: list[dict[str, Any]] = []
        for offset, entry in enumerate(package.get("条目", [])):
            input_no = str(entry.get("输入编号"))
            item = items.get(input_no, {})
            records.append({
                "id": start + offset,
                "记录编号": f"RCD-{batch.get('批次编号')}-{input_no}",
                "批次编号": batch.get("批次编号"),
                "输入编号": input_no,
                "来源文件": entry.get("来源文件"),
                "输入格式": item.get("输入格式"),
                "映射编号": entry.get("映射编号"),
                "映射版本": entry.get("映射版本"),
                "字段包摘要": item.get("当前摘要"),
                "原摘要": item.get("原摘要"),
                "字段": dict(entry.get("标准化字段", {})),
                "结论": conclusions.get(input_no, ""),
                "标记": dict(marks.get(input_no, {})),
                "复核版本": version,
                "核对时间": _now(),
            })
        return records

    def _build_review_rows(
        self,
        batch: dict[str, Any],
        issues: list[dict[str, Any]],
        version: int,
        reviewer: str | None,
    ) -> list[dict[str, Any]]:
        rows = store.rows("parser_review")
        start = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        created = _now()
        review_rows: list[dict[str, Any]] = []
        for offset, issue in enumerate(issues):
            review_rows.append({
                "id": start + offset,
                "复核编号": f"RV-{batch.get('批次编号')}-{offset + 1:02d}",
                "批次编号": batch.get("批次编号"),
                "输入编号": issue["输入编号"],
                "来源文件": issue["来源文件"],
                "字段": issue["字段"],
                "问题类型": issue["问题类型"],
                "说明": issue["说明"],
                "复核状态": REVIEW_PENDING,
                "复核版本": version,
                "核对人": str(reviewer or "").strip() or "值班校验",
                "创建时间": created,
            })
        return review_rows

    # ------------------------------------------------------------------ 结论查询
    def get_conclusion(self, batch_id: int) -> dict[str, Any] | None:
        """格式解析服务按批次回查校验结论；只返回当前有效的复核版本。"""
        batch = store.find(BATCH_MODULE, batch_id)
        if batch is None:
            return None
        effective = batch.get("有效复核")
        return {
            "批次编号": batch.get("批次编号"),
            "复核版本": batch.get("复核版本", 0),
            "有效": bool(effective and effective.get("有效")),
            "校验状态": batch.get("校验状态", CHECK_STALE if batch.get("复核历史") else "未校验"),
            "复核状态": batch.get("复核状态"),
            "校验摘要": dict(batch.get("校验摘要") or {}),
            "按来源文件": list((effective or {}).get("按来源文件", [])),
            "复核历史": [
                {
                    "复核版本": item.get("复核版本"),
                    "复核时间": item.get("复核时间"),
                    "有效": item.get("有效"),
                    "失效原因": item.get("失效原因"),
                }
                for item in batch.get("复核历史", [])
            ],
            "待复核清单": parser_batch_service.review_rows(str(batch.get("批次编号")), REVIEW_PENDING),
        }


validator_audit_service = ValidatorAuditService()
