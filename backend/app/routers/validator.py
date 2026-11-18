"""采集结果校验接口：维护校验规则，覆盖提交测试、启用规则、停用规则等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.parser_batch import parser_batch_service
from app.services.validator import ValidatorService
from app.services.validator_audit import validator_audit_service

router = APIRouter(prefix="/api/validator", tags=["采集结果校验"])

service = ValidatorService()

LIST_FIELDS = ["规则编号", "校验字段", "校验类型", "期望值", "容错范围", "采样比例", "阻断级别", "规则状态"]
STATUSES = ["草稿", "待测试", "已启用", "已停用"]


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
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待测试、已启用、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、规则编号关键字与状态过滤采集结果校验列表。"""
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
        headers={"Content-Disposition": 'attachment; filename="validator.csv"'})


@router.get("/batches")
def list_batches() -> dict:
    """校验侧看到的解析批次摘要：只读，不拿到可写引用。"""
    return {"items": validator_audit_service.list_batches()}


@router.get("/batches/{batch_id}/conclusion")
def get_conclusion(batch_id: int) -> dict:
    """格式解析服务回查入口：只返回当前有效的复核结论。"""
    conclusion = validator_audit_service.get_conclusion(batch_id)
    if conclusion is None:
        raise HTTPException(status_code=404, detail=f"解析批次 {batch_id} 不存在或已归档")
    return conclusion


@router.post("/batches/{batch_id}/review")
def review_batch(batch_id: int, payload: dict[str, object] | None = None) -> dict:
    """读取标准化字段包并回写解析批次；每批并发复核只保留一份有效结论。"""
    body = payload or {}
    expected = body.get("expected_version")
    try:
        expected_version = int(expected) if expected is not None else None
    except (TypeError, ValueError):
        return {"ok": False, "message": "复核版本必须是整数"}
    result, message = validator_audit_service.review(
        batch_id,
        reviewer=str(body.get("reviewer") or "") or None,
        expected_version=expected_version,
    )
    return {"ok": result is not None, "message": message, "result": result}


@router.get("/reviews")
def list_reviews(batch_no: str | None = Query(default=None), status: str | None = Query(default=None)) -> dict:
    """待复核清单；失效的历史条目保留但不再参与复核。"""
    return {"items": validator_audit_service.list_reviews(batch_no, status)}


@router.get("/records")
def list_records(batch_no: str | None = Query(default=None)) -> dict:
    """校验服务接力生成的业务记录。"""
    return {"items": parser_batch_service.list_records(batch_no)}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条校验规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"校验规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条校验规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="校验规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条校验规则执行提交测试、启用规则、停用规则；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
