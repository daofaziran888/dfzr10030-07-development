"""格式解析器接口：维护解析配置，覆盖提交配置、启用解析、废弃解析等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.parser import ParserService
from app.services.parser_batch import POLICIES, parser_batch_service

router = APIRouter(prefix="/api/parser", tags=["格式解析器"])

service = ParserService()

LIST_FIELDS = ["解析编号", "输入格式", "输出格式", "字段映射", "类型转换", "空值处理", "异常策略", "解析状态"]
STATUSES = ["草稿", "已配置", "生效中", "已废弃"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按解析编号检索"),
    status: str | None = Query(default=None, description="草稿、已配置、生效中、已废弃"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、解析编号关键字与状态过滤格式解析器列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="parser.csv"'})


@router.get("/mappings")
def list_mappings(input_format: str | None = Query(default=None)) -> dict:
    """字段映射版本清单：同一输入格式的多个可用版本会一起列出。"""
    return {"items": parser_batch_service.list_mappings(input_format), "policies": POLICIES}


@router.get("/inputs")
def list_inputs(
    input_format: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
) -> dict:
    """可批量选择的输入文件；操作人员可多选后一次提交。"""
    return {"items": parser_batch_service.list_inputs(input_format, keyword)}


@router.get("/batches")
def list_batches() -> dict:
    """解析批次清单：包含状态、字段顺序、校验状态与复核版本。"""
    return {"items": parser_batch_service.list_batches()}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int) -> dict:
    """单批次详情：解析批次清单 + 标准化字段包 + 映射快照。"""
    batch = parser_batch_service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"解析批次 {batch_id} 不存在或已归档")
    return batch


@router.get("/batches/{batch_id}/records")
def list_records(batch_id: int) -> dict:
    """业务记录回查：校验服务接力生成、格式解析服务按批次读取。"""
    batch = parser_batch_service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"解析批次 {batch_id} 不存在或已归档")
    return {"items": parser_batch_service.list_records(str(batch.get("批次编号")))}


@router.post("/batches")
def create_batch(payload: dict[str, object]) -> dict:
    """一次提交多条输入，逐条生成批次清单与字段包；任一条映射不唯一则整批不落地。"""
    inputs = payload.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        return {"ok": False, "message": "至少选择一条输入才能生成解析批次", "errors": []}
    policy = payload.get("policy") if isinstance(payload.get("policy"), str) else None
    batch, message, errors = parser_batch_service.create_batch(
        inputs, policy, str(payload.get("remark") or ""))
    return {
        "ok": batch is not None,
        "message": message,
        "batch": batch,
        "errors": errors,
    }


@router.post("/batches/{batch_id}/reparse")
def reparse_batch(batch_id: int) -> dict:
    """以批次冻结的旧映射版本为准重算，原摘要保留不动。"""
    batch, message = parser_batch_service.reparse_batch(batch_id)
    return {"ok": batch is not None, "message": message, "batch": batch}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条解析配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"解析配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条解析配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="解析配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条解析配置执行提交配置、启用解析、废弃解析；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
