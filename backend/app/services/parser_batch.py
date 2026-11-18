"""多格式批量解析：把多条输入按字段映射与类型转换编译成统一的解析批次包。

一次提交会在批次前置完成三件事，任何一步不通过都不会落半成品批次：

1. 为每条输入解析出唯一映射版本（两种口径见 ``POLICIES``）；
2. 汇总出整批唯一的字段类型表，同一字段不允许出现两种类型；
3. 冻结映射快照，后续重算只认这份旧版本，原摘要始终保留。

批次产物分两层：
- ``解析批次清单``：批次头、每条输入的映射版本、解析状态与失败原因；
- ``标准化字段包``：批次内稳定字段顺序 + 字段类型表 + 每条输入转换后的值。

校验服务只读这两份产物，回写校验结论并生成待复核清单，
解析服务再通过业务记录回查校验结果。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.store import store

MAPPING_MODULE = "parser_mapping"
INPUT_MODULE = "parser_input"
BATCH_MODULE = "parser_batch"
RECORD_MODULE = "parser_record"

POLICY_LATEST = "最新确认"
POLICY_HIT = "按输入命中"
POLICIES = [POLICY_LATEST, POLICY_HIT]

SUPPORTED_FORMATS = ["CSV", "JSON", "XML"]
MAPPING_USABLE = "可用"

ITEM_SUCCESS = "成功"
ITEM_FAILED = "失败"

BATCH_PARSED = "已解析"
BATCH_PARTIAL = "部分失败"
BATCH_FAILED = "解析失败"

CHECK_NONE = "未校验"
CHECK_DONE = "已校验"
CHECK_STALE = "复核失效"

DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _digest(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _mapping_key(mapping: dict[str, Any]) -> str:
    return f"{mapping.get('映射编号')}@{mapping.get('映射版本')}"


def _input_label(row: dict[str, Any]) -> str:
    return str(row.get("输入编号") or row.get("文件名") or row.get("id"))


def _type_name(raw: Any) -> str:
    return str(raw or "string").strip().lower()


def canonical_policy(value: str | None) -> str | None:
    """把页面/接口传进来的口径收敛成两个固定值，未知口径直接拒绝。"""
    text = str(value or "").strip()
    if not text:
        return None
    if "最新" in text or text.casefold() in {"latest", "newest"}:
        return POLICY_LATEST
    if "命中" in text or "历史" in text or "逐条" in text or text.casefold() in {"hit", "history"}:
        return POLICY_HIT
    if text in POLICIES:
        return text
    return None


def _get_path(payload: Any, path: str) -> Any:
    """JSON 映射支持 order.id 这样的点路径，先按完整键名找，再逐层下钻。"""
    if isinstance(payload, dict) and path in payload:
        return payload[path]
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _extract_csv(content: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        reader = csv.DictReader(io.StringIO(content.lstrip("﻿")))
        row = next(reader, None)
    except csv.Error as error:  # pragma: no cover - 极端畸形 CSV
        return None, f"CSV 解析失败：{error}"
    if row is None:
        return None, "CSV 解析失败：没有可解析的数据行"
    return {str(key).strip(): value for key, value in row.items() if key is not None}, None


def _extract_json(content: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        return None, f"JSON 解析失败：第 {error.lineno} 行 {error.msg}"
    if not isinstance(payload, dict):
        return None, "JSON 解析失败：顶层必须是对象"
    return payload, None


def _extract_xml(content: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        return None, f"XML 解析失败：{error}"
    values: dict[str, Any] = {}
    for child in root:
        values[child.tag] = child.text
    return values, None


EXTRACTORS = {"CSV": _extract_csv, "JSON": _extract_json, "XML": _extract_xml}


def _convert(raw: Any, target_type: str) -> tuple[Any, str | None]:
    """按映射声明的类型转换单个值；返回 (值, 失败原因)。"""
    if raw is None:
        return None, "值为空"
    text = str(raw).strip()
    if not text:
        return None, "值为空"
    if target_type == "string":
        return text, None
    if target_type == "decimal":
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError):
            return None, f"「{text}」无法转换为 decimal"
        return format(number, "f"), None
    if target_type == "int":
        try:
            return int(text), None
        except ValueError:
            try:
                number = Decimal(text)
            except (InvalidOperation, ValueError):
                return None, f"「{text}」无法转换为 int"
            if number != number.to_integral_value():
                return None, f"「{text}」无法转换为 int"
            return int(number), None
    if target_type == "datetime":
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S"), None
            except ValueError:
                continue
        return None, f"「{text}」无法转换为 datetime"
    return None, f"未知类型 {target_type}"


class ParserBatchService:
    """批量解析、批次追溯与业务记录回查。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seeded = False

    # ------------------------------------------------------------------ 查询
    def list_mappings(self, input_format: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(MAPPING_MODULE)
        if input_format:
            rows = [row for row in rows if str(row.get("输入格式")) == input_format]
        return sorted(
            rows,
            key=lambda row: (str(row.get("输入格式")), str(row.get("确认时间")), str(row.get("映射版本"))),
        )

    def list_inputs(self, input_format: str | None = None, keyword: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(INPUT_MODULE)
        if input_format:
            rows = [row for row in rows if str(row.get("输入格式")) == input_format]
        if keyword:
            needle = keyword.casefold()
            rows = [
                row for row in rows
                if needle in str(row.get("输入编号", "")).casefold()
                or needle in str(row.get("文件名", "")).casefold()
            ]
        return [dict(row) for row in rows]

    def list_batches(self) -> list[dict[str, Any]]:
        return [self._batch_view(row) for row in store.rows(BATCH_MODULE)]

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        entry = store.find(BATCH_MODULE, batch_id)
        return self._batch_view(entry) if entry else None

    def review_rows(self, batch_no: str, status: str | None = None) -> list[dict[str, Any]]:
        rows = [
            dict(row) for row in store.rows("parser_review")
            if row.get("批次编号") == batch_no
        ]
        if status:
            rows = [row for row in rows if row.get("复核状态") == status]
        return rows

    def list_records(self, batch_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(RECORD_MODULE)
        if batch_no:
            rows = [row for row in rows if str(row.get("批次编号")) == batch_no]
        return [dict(row) for row in rows]

    def _batch_view(self, row: dict[str, Any]) -> dict[str, Any]:
        """批次头 + 清单 + 字段包；副本输出，避免调用方误改内存仓库。"""
        view = {
            key: value for key, value in row.items()
            if key not in {"items", "标准化字段包"}
        }
        items = [dict(item) for item in row.get("items", [])]
        package = row.get("标准化字段包") or {}
        view["items"] = items
        view["标准化字段包"] = {
            "批次编号": row.get("批次编号"),
            "字段顺序": list(package.get("字段顺序", [])),
            "字段类型表": dict(package.get("字段类型表", {})),
            "校验状态": package.get("校验状态", CHECK_NONE),
            "校验摘要": package.get("校验摘要") or {},
            "条目": [dict(item) for item in package.get("条目", [])],
        }
        view["映射版本清单"] = list(row.get("映射版本清单", []))
        view["映射快照"] = dict(row.get("映射快照", {}))
        view["复核历史"] = [dict(item) for item in row.get("复核历史", [])]
        return view

    # ------------------------------------------------------------------ 解析
    def create_batch(
        self,
        input_ids: list[Any],
        policy: str | None,
        remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, list[dict[str, str]]]:
        """批量解析入口：先做整批前置校验，再逐条解析。

        返回 (批次, 说明, 逐条失败原因)；前置校验不通过时批次为 None，
        失败原因里能直接看出是哪条输入、哪份映射出了问题。
        """
        normalized_policy = canonical_policy(policy)
        if normalized_policy is None:
            return None, f"映射口径「{policy or ''}」不受支持，只允许：{'、'.join(POLICIES)}", []

        selected, errors = self._collect_inputs(input_ids)
        if errors:
            return None, "批次前置校验未通过，未生成任何批次", errors
        if not selected:
            return None, "至少选择一条输入才能生成解析批次", []

        resolved, resolve_errors = self._resolve_mappings(selected, normalized_policy)
        if resolve_errors:
            return None, "批次前置校验未通过，未生成任何批次", resolve_errors

        type_table, type_errors = self._build_type_table(resolved)
        if type_errors:
            return None, "批次内字段类型不一致，未生成任何批次", type_errors

        snapshots: dict[str, dict[str, Any]] = {}
        for item, mapping in resolved:
            snapshots[_mapping_key(mapping)] = self._snapshot(mapping)

        field_order = [field for field in type_table]
        with self._lock:
            batch_no = self._next_batch_no()
            batch = {
                "id": max((int(row.get("id", 0)) for row in store.rows(BATCH_MODULE)), default=0) + 1,
                "批次编号": batch_no,
                "映射口径": normalized_policy,
                "状态": BATCH_PARSED,
                "待校验": True,
                "异常": False,
                "创建时间": _now(),
                "备注": str(remark or "").strip(),
                "输入条数": len(selected),
                "成功条数": 0,
                "失败条数": 0,
                "映射版本清单": [
                    {
                        "输入编号": _input_label(item),
                        "文件名": item.get("文件名"),
                        "输入格式": item.get("输入格式"),
                        "映射编号": mapping.get("映射编号"),
                        "映射版本": mapping.get("映射版本"),
                        "映射确认时间": mapping.get("确认时间"),
                    }
                    for item, mapping in resolved
                ],
                "字段顺序": field_order,
                "字段类型表": dict(type_table),
                "映射快照": snapshots,
                "items": [],
                "标准化字段包": {},
                "原摘要": "",
                "当前摘要": "",
                "重算次数": 0,
                "最近重算时间": None,
                "校验状态": CHECK_NONE,
                "复核状态": "无",
                "复核版本": 0,
                "有效复核": None,
                "复核历史": [],
                "校验摘要": {},
                "业务记录数": 0,
            }
            self._parse_items(batch, resolved)
            original = _digest([item["原摘要"] for item in batch["items"]])
            batch["原摘要"] = original
            batch["当前摘要"] = original
            batch["标准化字段包"]["校验状态"] = CHECK_NONE
            store.rows(BATCH_MODULE).append(batch)
            view = self._batch_view(batch)

        message = (
            f"批次 {batch_no} 已生成：成功 {batch['成功条数']} 条，失败 {batch['失败条数']} 条，"
            f"口径为{normalized_policy}"
        )
        return view, message, []

    def _collect_inputs(
        self, input_ids: list[Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        ids: list[int] = []
        errors: list[dict[str, str]] = []
        seen_labels: set[str] = set()
        for raw in input_ids or []:
            try:
                input_id = int(raw)
            except (TypeError, ValueError):
                errors.append({"输入": str(raw), "失败原因": "输入编号不是有效数字"})
                continue
            if input_id in ids:
                errors.append({"输入": str(input_id), "失败原因": "同一条输入被重复选择"})
                continue
            row = store.find(INPUT_MODULE, input_id)
            if row is None:
                errors.append({"输入": str(input_id), "失败原因": "输入不存在或已归档"})
                continue
            label = _input_label(row)
            if label in seen_labels:
                errors.append({"输入": label, "失败原因": "同一条输入被重复选择"})
                continue
            seen_labels.add(label)
            ids.append(input_id)
        selected = [store.find(INPUT_MODULE, input_id) for input_id in ids]
        return [row for row in selected if row is not None], errors

    def _resolve_mappings(
        self,
        selected: list[dict[str, Any]],
        policy: str,
    ) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, str]]]:
        """批次前置：为每条输入锁定唯一映射版本，锁定失败整批不解析。"""
        mappings = store.rows(MAPPING_MODULE)
        resolved: list[tuple[dict[str, Any], dict[str, Any]]] = []
        errors: list[dict[str, str]] = []
        for row in selected:
            label = _input_label(row)
            input_format = str(row.get("输入格式") or "").strip()
            candidates = [
                mapping for mapping in mappings
                if str(mapping.get("输入格式")) == input_format
                and str(mapping.get("状态")) == MAPPING_USABLE
            ]
            if not candidates:
                errors.append({"输入": label, "失败原因": f"{input_format or '未知格式'} 没有可用映射版本"})
                continue
            if policy == POLICY_LATEST:
                mapping = max(
                    candidates,
                    key=lambda item: (str(item.get("确认时间", "")), int(item.get("id", 0))),
                )
            else:
                hit = str(row.get("命中映射编号") or "").strip()
                matches = [candidate for candidate in candidates if str(candidate.get("映射编号")) == hit]
                if not matches:
                    errors.append({
                        "输入": label,
                        "失败原因": f"命中映射 {hit or '（未登记）'} 不可用，无法保持历史映射连续",
                    })
                    continue
                mapping = max(
                    matches,
                    key=lambda item: (str(item.get("确认时间", "")), int(item.get("id", 0))),
                )
            resolved.append((row, mapping))
        return resolved, errors

    def _build_type_table(
        self,
        resolved: list[tuple[dict[str, Any], dict[str, Any]]],
    ) -> tuple[dict[str, str], list[dict[str, str]]]:
        """整批只允许一张字段类型表；同字段两种类型视为边界冲突。"""
        table: dict[str, str] = {}
        owners: dict[str, list[str]] = {}
        errors: list[dict[str, str]] = []
        for row, mapping in resolved:
            label = _input_label(row)
            mapping_label = _mapping_key(mapping)
            for source_field, target_field in self._pairs(mapping):
                field_type = _type_name(mapping.get("类型转换", {}).get(target_field))
                previous = table.get(target_field)
                if previous is None:
                    table[target_field] = field_type
                    owners[target_field] = [mapping_label]
                elif previous != field_type:
                    errors.append({
                        "输入": label,
                        "失败原因": (
                            f"字段「{target_field}」在 {mapping_label} 中为 {field_type}，"
                            f"与 {owners[target_field][0]} 的 {previous} 不一致"
                        ),
                    })
                else:
                    owners[target_field].append(mapping_label)
                raw_types = mapping.get("类型转换", {})
                if target_field not in raw_types:
                    errors.append({
                        "输入": label,
                        "失败原因": f"映射 {mapping_label} 未声明字段「{target_field}」的类型转换",
                    })
        return table, errors

    @staticmethod
    def _pairs(mapping: dict[str, Any]) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for entry in mapping.get("字段映射", []):
            source = str(entry.get("来源字段") or "").strip()
            target = str(entry.get("目标字段") or "").strip()
            if source and target:
                pairs.append((source, target))
        return pairs

    @staticmethod
    def _snapshot(mapping: dict[str, Any]) -> dict[str, Any]:
        return {
            "映射编号": mapping.get("映射编号"),
            "映射版本": mapping.get("映射版本"),
            "输入格式": mapping.get("输入格式"),
            "确认时间": mapping.get("确认时间"),
            "字段映射": [dict(entry) for entry in mapping.get("字段映射", [])],
            "类型转换": dict(mapping.get("类型转换", {})),
            "必需字段": list(mapping.get("必需字段", [])),
        }

    def _parse_items(
        self,
        batch: dict[str, Any],
        resolved: list[tuple[dict[str, Any], dict[str, Any]]],
    ) -> None:
        type_table = batch["字段类型表"]
        field_order = batch["字段顺序"]
        package_items: list[dict[str, Any]] = []
        for row, mapping in resolved:
            snapshot = batch["映射快照"][_mapping_key(mapping)]
            item = self._parse_one(row, snapshot, type_table, field_order)
            batch["items"].append(item)
            package_items.append({
                "输入编号": item["输入编号"],
                "来源文件": item["文件名"],
                "映射编号": item["映射编号"],
                "映射版本": item["映射版本"],
                "标准化字段": dict(item["标准化字段"]),
                "缺失字段": list(item["缺失字段"]),
                "类型异常": [dict(entry) for entry in item["类型异常"]],
                "校验结论": item.get("校验结论", ""),
                "校验标记": {},
            })
        success = sum(1 for item in batch["items"] if item["状态"] == ITEM_SUCCESS)
        batch["成功条数"] = success
        batch["失败条数"] = len(batch["items"]) - success
        if batch["失败条数"] == 0:
            batch["状态"] = BATCH_PARSED
        elif success == 0:
            batch["状态"] = BATCH_FAILED
        else:
            batch["状态"] = BATCH_PARTIAL
        batch["标准化字段包"] = {
            "批次编号": batch["批次编号"],
            "字段顺序": list(field_order),
            "字段类型表": dict(type_table),
            "校验状态": CHECK_NONE,
            "校验摘要": {},
            "条目": package_items,
        }

    def _parse_one(
        self,
        row: dict[str, Any],
        mapping: dict[str, Any],
        type_table: dict[str, str],
        field_order: list[str],
    ) -> dict[str, Any]:
        source = str(row.get("内容") or "")
        item: dict[str, Any] = {
            "输入编号": _input_label(row),
            "文件名": row.get("文件名"),
            "输入格式": row.get("输入格式"),
            "映射编号": mapping.get("映射编号"),
            "映射版本": mapping.get("映射版本"),
            "映射确认时间": mapping.get("确认时间"),
            "映射快照摘要": _digest(mapping),
            "输入摘要": _digest(source),
            "标准化字段": {},
            "缺失字段": [],
            "映射未覆盖字段": [],
            "类型异常": [],
            "状态": ITEM_SUCCESS,
            "失败原因": "",
            "校验结论": "",
            "校验标记": {},
        }
        extractor = EXTRACTORS.get(str(row.get("输入格式") or ""))
        if extractor is None:
            item["状态"] = ITEM_FAILED
            item["失败原因"] = f"不支持的输入格式：{row.get('输入格式')}"
            item["原摘要"] = _digest({"input": source, "mapping": mapping})
            item["当前摘要"] = item["原摘要"]
            return item
        raw_values, error = extractor(source)
        if error:
            item["状态"] = ITEM_FAILED
            item["失败原因"] = error
            item["原摘要"] = _digest({"input": source, "mapping": mapping})
            item["当前摘要"] = item["原摘要"]
            return item

        mapped_targets: set[str] = set()
        values: dict[str, Any] = {}
        for source_field, target_field in self._pairs(mapping):
            mapped_targets.add(target_field)
            raw = _get_path(raw_values, source_field)
            target_type = type_table.get(target_field, _type_name(mapping.get("类型转换", {}).get(target_field)))
            if raw is None or not str(raw).strip():
                values[target_field] = None
                item["缺失字段"].append(target_field)
                continue
            converted, convert_error = _convert(raw, target_type)
            if convert_error:
                values[target_field] = raw
                item["类型异常"].append({"字段": target_field, "原因": convert_error})
            else:
                values[target_field] = converted

        for field in field_order:
            if field in mapped_targets:
                continue
            values[field] = None
            item["映射未覆盖字段"].append(field)
            item["缺失字段"].append(field)

        item["标准化字段"] = {field: values.get(field) for field in field_order}
        required_missing = [
            field for field in mapping.get("必需字段", [])
            if field in item["缺失字段"]
        ]
        reasons: list[str] = []
        if item["类型异常"]:
            reasons.append(
                "类型转换失败：" + "；".join(f"{entry['字段']} {entry['原因']}" for entry in item["类型异常"])
            )
        if required_missing:
            reasons.append(f"缺少必需字段：{'、'.join(required_missing)}")
        if item["映射未覆盖字段"]:
            reasons.append(f"当前映射未覆盖字段：{'、'.join(item['映射未覆盖字段'])}")
        if item["类型异常"]:
            item["状态"] = ITEM_FAILED
        item["失败原因"] = "；".join(reasons)
        item["原摘要"] = _digest({"input": source, "mapping": mapping})
        item["当前摘要"] = item["原摘要"]
        return item

    def _next_batch_no(self) -> str:
        today = datetime.now().strftime("%Y%m%d")
        prefix = f"PB-{today}-"
        existing = [
            str(row.get("批次编号", "")) for row in store.rows(BATCH_MODULE)
            if str(row.get("批次编号", "")).startswith(prefix)
        ]
        sequence = len(existing) + 1
        return f"{prefix}{sequence:04d}"

    # ------------------------------------------------------------------ 重算
    def reparse_batch(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        """按批次冻结的旧映射快照重算；原摘要不动，只刷新当前摘要。"""
        batch = store.find(BATCH_MODULE, batch_id)
        if batch is None:
            return None, f"解析批次 {batch_id} 不存在或已归档"
        resolved: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in batch.get("items", []):
            row = None
            for candidate in store.rows(INPUT_MODULE):
                if _input_label(candidate) == item.get("输入编号"):
                    row = candidate
                    break
            if row is None:
                continue
            snapshot = batch["映射快照"].get(f"{item.get('映射编号')}@{item.get('映射版本')}")
            if snapshot is None:
                continue
            resolved.append((row, snapshot))

        lock = self._batch_lock(batch_id)
        with lock:
            original = batch.get("原摘要")
            old_digests = {item["输入编号"]: item["原摘要"] for item in batch.get("items", [])}
            batch["items"] = []
            self._parse_items(batch, resolved)
            for item in batch["items"]:
                item["原摘要"] = old_digests.get(item["输入编号"], item["原摘要"])
            batch["当前摘要"] = _digest([item["当前摘要"] for item in batch["items"]])
            batch["重算次数"] = int(batch.get("重算次数", 0)) + 1
            batch["最近重算时间"] = _now()
            self.invalidate_validation(batch, reason="批次已按原映射版本重算")
            self._purge_records(batch["批次编号"])
            for review in store.rows("parser_review"):
                if review.get("批次编号") == batch["批次编号"] and review.get("复核状态") == "待复核":
                    review["复核状态"] = "已失效"
                    review["失效原因"] = "批次已按原映射版本重算"
            batch["业务记录数"] = 0
            view = self._batch_view(batch)
            view["原摘要不变"] = view["原摘要"] == original
        return view, f"批次 {batch['批次编号']} 已按原映射版本重算，原摘要保留"

    # ------------------------------------------------------------------ 校验协作
    def invalidate_validation(self, batch: dict[str, Any], reason: str) -> None:
        """批次数据变化后，原有复核结论不再有效，历史保留但标记失效。"""
        effective = batch.get("有效复核")
        if effective:
            effective["有效"] = False
            effective["失效原因"] = reason
        batch["有效复核"] = None
        batch["复核状态"] = "已失效" if effective else batch.get("复核状态", "无")
        if batch.get("校验状态") == CHECK_DONE:
            batch["校验状态"] = CHECK_STALE
        for item in batch.get("items", []):
            item["校验结论"] = ""
            item["校验标记"] = {}
        for package_item in (batch.get("标准化字段包") or {}).get("条目", []):
            package_item["校验结论"] = ""
            package_item["校验标记"] = {}
        if batch.get("标准化字段包"):
            batch["标准化字段包"]["校验状态"] = CHECK_STALE
            batch["标准化字段包"]["校验摘要"] = {}
        batch["校验摘要"] = {}

    def record_validation(
        self,
        batch: dict[str, Any],
        *,
        summary: dict[str, Any],
        per_file: list[dict[str, Any]],
        conclusions: dict[str, str],
        marks: dict[str, dict[str, str]],
        records: list[dict[str, Any]],
        review_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """校验结果回写解析批次清单与标准化字段包，并生成业务记录。"""
        previous = batch.get("有效复核")
        if previous:
            previous["有效"] = False
            previous["失效原因"] = "已有更新的复核版本"
        version = int(batch.get("复核版本", 0)) + 1
        checked_at = _now()
        result = {
            "复核版本": version,
            "复核时间": checked_at,
            "有效": True,
            "汇总": summary,
            "按来源文件": per_file,
            "业务记录数": len(records),
        }
        batch["复核版本"] = version
        batch["有效复核"] = result
        batch["复核历史"].append(result)
        batch["校验状态"] = CHECK_DONE
        batch["复核状态"] = "已生成"
        batch["待校验"] = False
        batch["校验摘要"] = dict(summary)
        batch["校验时间"] = checked_at
        batch["业务记录数"] = len(records)
        batch["异常"] = bool(summary.get("待复核条数"))
        for item in batch.get("items", []):
            item["校验结论"] = conclusions.get(item["输入编号"], "")
            item["校验标记"] = marks.get(item["输入编号"], {})
            item["复核版本"] = version
        package = batch.setdefault("标准化字段包", {})
        package["校验状态"] = CHECK_DONE
        package["校验摘要"] = dict(summary)
        package["复核版本"] = version
        for package_item in package.get("条目", []):
            package_item["校验结论"] = conclusions.get(package_item["输入编号"], "")
            package_item["校验标记"] = marks.get(package_item["输入编号"], {})

        store.rows(RECORD_MODULE).extend(records)
        existing_reviews = store.rows("parser_review")
        for row in existing_reviews:
            if row.get("批次编号") == batch["批次编号"] and row.get("复核状态") == "待复核":
                row["复核状态"] = "已失效"
                row["失效原因"] = f"批次已生成复核版本 v{version}"
        existing_reviews.extend(review_rows)
        return result

    # ------------------------------------------------------------------ 并发
    _locks: dict[int, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def _batch_lock(self, batch_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(batch_id, threading.Lock())

    def _purge_records(self, batch_no: str) -> None:
        rows = store.rows(RECORD_MODULE)
        rows[:] = [row for row in rows if row.get("批次编号") != batch_no]


parser_batch_service = ParserBatchService()
