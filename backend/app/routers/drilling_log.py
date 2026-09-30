"""班次编录接口：班次日志单向流转、区间补录互斥，审批结论同步台账/曲线/待办。

所有写操作共用 services.drilling_log 这一条写入路径，前端只读派生表，
不允许从多个接口各改一套。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import PageResult
from app.services.drilling_log import (
    ShiftRuleError,
    STATUS_ORDER,
    service,
)

router = APIRouter(prefix="/api/drilling_log", tags=["钻探日志"])

STATUSES = STATUS_ORDER
LIST_FIELDS = [
    "日志编号", "钻孔编号", "班次", "深度起", "深度止", "回次进尺",
    "岩层描述", "上报基准", "状态", "审批结论",
]


class WritePayload(BaseModel):
    """班次写操作入参：values 放业务字段，操作序号/幂等键放在外层。"""

    values: dict[str, Any] = Field(default_factory=dict)
    action: str | None = None
    operator: str | None = None
    operation_id: str | None = None
    expected_seq: int | None = None
    remark: str | None = None


def _fail(exc: ShiftRuleError) -> HTTPException:
    return HTTPException(status_code=409 if exc.conflict else 400, detail=exc.message)


# 静态路径必须声明在 /{entry_id} 之前，否则会被当成日志id捕获。


@router.get("/summary")
def summary() -> dict[str, Any]:
    """班次编录看板计数与台账/曲线/待办同步状态。"""
    return service.summary()


@router.get("/ledger")
def ledger(borehole: str | None = None) -> dict[str, Any]:
    """钻探台账：审批通过的班次才进台账，生效深度按现场终孔确认封顶。"""
    items = service.list_ledger(borehole=borehole)
    return {"items": items, "total": len(items)}


@router.get("/curve")
def curve(borehole: str | None = None) -> dict[str, Any]:
    """孔深曲线：数据由审批动作同步写入，不再由前端自行推算。"""
    items = service.get_curve(borehole=borehole)
    return {"items": items, "total": len(items)}


@router.get("/todos")
def todos(
    borehole: str | None = None,
    status: str | None = Query(default=None, description="待填写、已填写"),
) -> dict[str, Any]:
    """班次待办：审核通过即清除，不残留已完成记录。"""
    items = service.list_todo(borehole=borehole, status=status)
    return {"items": items, "total": len(items)}


@router.get("/journal")
def journal(borehole: str | None = None) -> dict[str, Any]:
    """操作序号流水：掉线后按最新序号和 operation_id 续做。"""
    items = service.list_journal(borehole=borehole)
    latest: dict[str, int] = {}
    for item in items:
        latest[str(item["钻孔编号"])] = max(latest.get(str(item["钻孔编号"]), 0), int(item["seq"]))
    return {"items": items, "total": len(items), "latest_seq": latest}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按日志编号检索"),
    status: str | None = Query(default=None, description="待填写、已填写、已审核"),
    borehole: str | None = Query(default=None, description="按钻孔编号过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按钻孔、日志编号与状态过滤班次日志；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if status and status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"状态只支持：{'、'.join(STATUSES)}")
    items, total = service.list_entries(
        keyword=keyword, status=status, borehole=borehole, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/backfill")
def backfill(payload: WritePayload) -> dict[str, Any]:
    """补录班次区间（待填写）。同钻孔区间重叠、越过现场终孔、序号过期都会被拦下。"""
    try:
        entry, seq, replayed, message = service.backfill(
            payload.values,
            operator=payload.operator or "",
            operation_id=payload.operation_id or "",
            expected_seq=payload.expected_seq,
        )
    except ShiftRuleError as exc:
        raise _fail(exc)
    return {
        "ok": True,
        "replayed": replayed,
        "message": message or f"班次 {entry.get('日志编号')} 已补录，等待填写班报",
        "seq": seq,
        "entry": entry,
    }


@router.post("/final_depth")
def final_depth(payload: WritePayload) -> dict[str, Any]:
    """登记终孔深度；现场终孔确认一到即覆盖补充上报口径并重算台账/曲线。"""
    try:
        row, seq, replayed, message = service.set_final_depth(
            payload.values,
            operator=payload.operator or "",
            operation_id=payload.operation_id or "",
            expected_seq=payload.expected_seq,
        )
    except ShiftRuleError as exc:
        raise _fail(exc)
    return {
        "ok": True,
        "replayed": replayed,
        "message": message or f"钻孔 {row.get('钻孔编号')} 终孔深度已按「{row.get('深度来源')}」生效",
        "seq": seq,
        "entry": row,
    }


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出班次编录清单及同步派生表，一次拿全，便于核对口径一致。"""
    entries, total = service.list_entries(page=1, size=10000)
    return {
        "module": "drilling_log",
        "total": total,
        "items": entries,
        "ledger": service.list_ledger(),
        "curve": service.get_curve(),
        "todos": service.list_todo(),
        "journal": service.list_journal(),
    }


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict[str, Any]:
    """读取单条班次记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"班次记录 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: WritePayload) -> dict[str, Any]:
    """填写日志 / 提交审核。只能单向推进，跳步必须在原因或审批结论里说明。"""
    action = (payload.action or str(payload.values.get("action") or "")).strip()
    try:
        entry, seq, replayed, message = service.advance(
            entry_id,
            action,
            {**payload.values, "remark": payload.remark},
            operator=payload.operator or "",
            operation_id=payload.operation_id or "",
            expected_seq=payload.expected_seq,
        )
    except ShiftRuleError as exc:
        raise _fail(exc)
    verb = "审核已通过" if action == "提交审核" else "班报已填写"
    return {
        "ok": True,
        "replayed": replayed,
        "message": message or f"班次 {entry.get('日志编号')} {verb}，台账/曲线/待办已同步",
        "seq": seq,
        "entry": entry,
    }
