"""钻探日志（班次编录）接口。

写接口只有五个，全部落在 DrillingLogService 同一条写入路径上：
  POST /shifts            班前排程，登记待填写占位
  POST /{id}/fill         补录/填写编录（待填写 -> 已填写）
  POST /{id}/audit        审批（已填写 -> 已审核），结论同步台账/曲线/待办
  POST /{id}/jump-submit  跳步提交（必须带跳步原因）
  POST /final-depth       现场终孔确认（确认值优先于补充上报）

读侧除明细外，台账 / 孔深曲线 / 班次待办 / 操作同步都从同一底座实时派生。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.drilling_log import DrillingError, service

router = APIRouter(prefix="/api/drilling_log", tags=["钻探日志"])

STATUSES = ["待填写", "已填写", "已审核"]


def _call(action, *args):
    """把业务异常翻译成 HTTP 状态码：409 表示区间/并发冲突，其余为 400。"""
    try:
        return action(*args)
    except DrillingError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc


def _idem(payload: EntryPayload) -> str | None:
    key = str(payload.values.get("idem_key") or payload.idem_key or "").strip()
    return key or None


def _to_result(result: dict[str, Any]) -> ActionResult:
    return ActionResult(
        ok=bool(result.get("ok", True)),
        message=str(result.get("message", "")),
        entry=result.get("entry"),
        replayed=bool(result.get("replayed")),
        last_seq=result.get("last_seq"),
        operation=result.get("operation"),
        sync=result.get("sync"),
    )


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按日志编号检索"),
    status: str | None = Query(default=None, description="待填写、已填写、已审核"),
    borehole: str | None = Query(default=None, description="按钻孔编号过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按日志编号、状态、钻孔过滤班次编录；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, borehole=borehole, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/ledger")
def get_ledger(borehole: str | None = None) -> dict[str, Any]:
    """钻探台账：按孔汇总各班次审核进度与采用孔深（终孔确认值优先）。"""
    return service.ledger(borehole)


@router.get("/depth-curve")
def get_depth_curve(borehole: str = Query(description="钻孔编号")) -> dict[str, Any]:
    """孔深曲线：按班次推进顺序给出累计孔深点，末点与终孔确认值对齐。"""
    if not borehole.strip():
        raise HTTPException(status_code=400, detail="请提供钻孔编号")
    return service.depth_curve(borehole.strip())


@router.get("/pending")
def get_pending(borehole: str | None = None) -> dict[str, Any]:
    """班次待办：实时派生，审核完成即出列，不残留已完成记录。"""
    return service.pending_todos(borehole)


@router.get("/sync")
def get_sync(since: int = Query(default=0, ge=0, description="已收到的最大操作序号")) -> dict[str, Any]:
    """掉线续传：按操作序号拉增量，并回带台账与待办，保证多处口径一致。"""
    return service.sync(since)


@router.get("/ops")
def get_ops(since: int = Query(default=0, ge=0), limit: int = Query(default=100, le=500)) -> dict[str, Any]:
    """操作流水：查看每次写入的序号、操作人与动作。"""
    return service.list_ops(since, limit)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条班次编录明细；不存在时给出可读的错误说明。"""
    try:
        return service.get_entry(entry_id)
    except DrillingError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc


@router.post("/shifts", response_model=ActionResult)
def create_shift(payload: EntryPayload) -> ActionResult:
    """班前排程：登记一条待填写班次占位，区间即纳入同孔占用校验。"""
    return _to_result(_call(lambda: service.create_shift(payload.values, idem_key=_idem(payload))))


@router.post("/{entry_id}/fill", response_model=ActionResult)
def fill_shift(entry_id: int, payload: EntryPayload) -> ActionResult:
    """补录/填写编录：待填写 -> 已填写；只能补待填写班次，历史基准不动。"""
    return _to_result(_call(lambda: service.fill_shift(entry_id, payload.values, idem_key=_idem(payload))))


@router.post("/{entry_id}/audit", response_model=ActionResult)
def audit_shift(entry_id: int, payload: EntryPayload) -> ActionResult:
    """审批：已填写 -> 已审核，审批结论同步落台账、孔深曲线、班次待办。"""
    return _to_result(_call(lambda: service.audit_shift(entry_id, payload.values, idem_key=_idem(payload))))


@router.post("/{entry_id}/jump-submit", response_model=ActionResult)
def jump_submit(entry_id: int, payload: EntryPayload) -> ActionResult:
    """跳步提交：跳过中间档必须说明跳步原因，否则拒绝受理。"""
    return _to_result(_call(lambda: service.jump_submit(entry_id, payload.values, idem_key=_idem(payload))))


@router.post("/final-depth", response_model=ActionResult)
def confirm_final_depth(payload: EntryPayload) -> ActionResult:
    """现场终孔确认：确认值优先于补充上报，作为台账与曲线基准。"""
    return _to_result(_call(lambda: service.confirm_final_depth(payload.values, idem_key=_idem(payload))))


@router.get("/export/data")
def export_entries() -> dict[str, Any]:
    """导出班次编录清单及同源台账/待办快照。"""
    items, total = service.list_entries(page=1, size=10000)
    return {
        "module": "drilling_log",
        "total": total,
        "items": items,
        "ledger": service.ledger(),
        "pending": service.pending_todos(),
    }
