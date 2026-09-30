"""班次编录写入路径：状态单向推进、孔深区间不重叠、审批结论一处写入三处同步。

设计要点（对应现场报上来的三类毛病）：
- 一条班次日志只能 待填写 → 已填写 → 已审核，跳步必须写原因，已审核锁定，
  不再保留「退回补充」这种回退状态——补录只能以新区间新班次进入。
- 同一钻孔的班次按孔深区间 [深度起, 深度止) 单向推进，区间不允许重叠
  （首尾相接可以），从根上解决「补录后仍显示旧班次、深度错位」。
- 审批通过后由本服务同一把锁同步落到钻探台账、孔深曲线、班次待办三张表，
  前端不再各算一套；已完成的待办立即清掉。
- 现场终孔确认值优先于补充上报：终孔表有现场值时台账/曲线一律按现场值生效；
  既有班次（含已审核台账）保留原上报基准，不被补录改写。
- 同区间并发补录用「钻孔级操作序号 + 区间互斥校验」串行化，只有一个能成功；
  客户端带 operation_id 时服务端去重，掉线重发按操作序号接着续做。
"""
from __future__ import annotations

import functools
import threading
from datetime import datetime
from typing import Any, Callable

from app.store import store

MODULE = "drilling_log"
FINAL_DEPTH_MODULE = "drilling_log_final_depth"
LEDGER_MODULE = "drilling_log_ledger"
CURVE_MODULE = "drilling_log_curve"
TODO_MODULE = "drilling_log_todo"
JOURNAL_MODULE = "drilling_log_journal"

# 单向状态机：只能往后走，没有回退。
STATUS_TODO = "待填写"
STATUS_FILLED = "已填写"
STATUS_REVIEWED = "已审核"
STATUS_ORDER = [STATUS_TODO, STATUS_FILLED, STATUS_REVIEWED]

ACTION_TARGETS = {"填写日志": STATUS_FILLED, "提交审核": STATUS_REVIEWED}

# 上报基准：现场班报 vs 事后补录。
SOURCE_SITE = "现场记录"
SOURCE_BACKFILL = "补充上报"
FINAL_SOURCE_SITE = "现场终孔确认"
FINAL_SOURCE_BACKFILL = "补充上报"


class ShiftRuleError(Exception):
    """班次编录业务规则冲突：调用方转成 400/409 返回，并把原因讲清楚。"""

    def __init__(self, message: str, *, conflict: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.conflict = conflict


class _ReplayHit(Exception):
    """同一个 operation_id 已执行过：掉线重发时按原结果接着返回，不重复执行。"""

    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__("operation replayed")
        self.record = record


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _as_depth(value: Any, label: str, *, required: bool = True) -> float | None:
    if value is None or str(value).strip() == "":
        if required:
            raise ShiftRuleError(f"{label}必须填写，且为不小于 0 的数字")
        return None
    try:
        depth = float(value)
    except (TypeError, ValueError):
        raise ShiftRuleError(f"{label}必须是数字，收到的是「{value}」")
    if depth < 0:
        raise ShiftRuleError(f"{label}不能为负数")
    return depth


def _text(values: dict[str, Any], key: str) -> str:
    return str(values.get(key) or "").strip()


def _replayable(method: Callable[..., tuple[dict[str, Any], int]]) -> Callable[..., tuple[dict[str, Any], int, bool, str]]:
    """写方法统一包一层：命中 operation_id 去重时返回（原记录, 原序号, True, 说明）。"""

    @functools.wraps(method)
    def wrapper(self: "DrillingLogService", *args: Any, **kwargs: Any) -> tuple[dict[str, Any], int, bool, str]:
        try:
            entry, seq = method(self, *args, **kwargs)
            return entry, seq, False, ""
        except _ReplayHit as hit:
            replay_entry, replay_seq, message, _ = self._replay_result(hit.record)
            return replay_entry, replay_seq, True, message

    return wrapper


class DrillingLogService:
    """班次编录服务。所有写操作都走这一个类，外部不允许直接改 store 里的行。"""

    def __init__(self) -> None:
        # 进程内一把锁：内存仓库下保证「校验区间 + 落库 + 同步派生表」原子完成，
        # 两个账号同时补录同一区间时只有一个能过校验。
        self._lock = threading.RLock()
        self._bootstrapped = False

    # ------------------------------------------------------------------ 启动

    def bootstrap(self) -> None:
        """从种子班次重建台账/曲线/待办/操作序号，保证演示数据也只有一套口径。"""
        with self._lock:
            if self._bootstrapped:
                return
            for name in (LEDGER_MODULE, CURVE_MODULE, TODO_MODULE, JOURNAL_MODULE):
                store.mark_internal(name)
            store.mark_internal(FINAL_DEPTH_MODULE)
            store.rows(LEDGER_MODULE).clear()
            store.rows(CURVE_MODULE).clear()
            store.rows(TODO_MODULE).clear()
            store.rows(JOURNAL_MODULE).clear()

            # 先落终孔登记（按钻孔记一条操作序号基线），再重建班次派生数据。
            for row in store.rows(FINAL_DEPTH_MODULE):
                self._append_journal(
                    str(row["钻孔编号"]),
                    action="登记终孔深度",
                    entry_id=None,
                    operator=str(row.get("确认人") or "系统"),
                    payload={"生效深度": row.get("生效深度"), "深度来源": row.get("深度来源")},
                    note="按已登记终孔值建立操作序号基线",
                )
            for entry in sorted(store.rows(MODULE), key=lambda r: int(r["id"])):
                self._append_journal(
                    str(entry["钻孔编号"]),
                    action="建立班次",
                    entry_id=int(entry["id"]),
                    operator=str(entry.get("创建人") or "系统"),
                    payload={"深度起": entry["深度起"], "深度止": entry["深度止"], "班次": entry.get("班次")},
                    note="按既有班次建立操作序号基线",
                )
                self._sync_todo(entry)
                if entry["status"] == STATUS_REVIEWED:
                    self._sync_reviewed(entry)
            self._bootstrapped = True

    # ------------------------------------------------------------------ 查询

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        borehole: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        self.bootstrap()
        rows = list(store.rows(MODULE))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("日志编号", ""))]
        if borehole:
            rows = [row for row in rows if str(row.get("钻孔编号", "")) == borehole]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows.sort(key=lambda r: (str(r.get("钻孔编号")), float(r.get("深度起", 0)), int(r.get("id", 0))))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        self.bootstrap()
        return store.find(MODULE, entry_id)

    def summary(self) -> dict[str, Any]:
        """班次编录看板：状态计数 + 台账/曲线/待办同步是否齐全。"""
        self.bootstrap()
        rows = store.rows(MODULE)
        reviewed = [r for r in rows if r["status"] == STATUS_REVIEWED]
        ledger = store.rows(LEDGER_MODULE)
        curve = store.rows(CURVE_MODULE)
        todo = store.rows(TODO_MODULE)
        ledger_ids = {int(r["日志id"]) for r in ledger}
        curve_ids = {int(r["日志id"]) for r in curve}
        reviewed_ids = {int(r["id"]) for r in reviewed}
        synced = all(int(r["id"]) in ledger_ids and int(r["id"]) in curve_ids for r in reviewed)
        # 待办里只允许留未完成班次，残留已完成记录即为不一致。
        stale_todo = [t for t in todo if int(t["日志id"]) in reviewed_ids]
        return {
            "待填写": sum(1 for r in rows if r["status"] == STATUS_TODO),
            "已填写待审核": sum(1 for r in rows if r["status"] == STATUS_FILLED),
            "已审核": len(reviewed),
            "待办总数": len(todo),
            "台账条数": len(ledger),
            "曲线点数": len(curve),
            "同步一致": synced and not stale_todo,
        }

    # -------------------------------------------------------------- 补录班次

    @_replayable
    def backfill(
        self,
        values: dict[str, Any],
        *,
        operator: str = "",
        operation_id: str = "",
        expected_seq: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        """补录一个班次区间，落「待填写」。返回（新班次, 该钻孔最新操作序号）。"""
        self.bootstrap()
        operator = operator or _text(values, "operator") or "值班管理员"
        operation_id = operation_id or _text(values, "operation_id")
        if expected_seq is None and str(values.get("expected_seq") or "").strip() != "":
            expected_seq = int(values["expected_seq"])
        with self._lock:
            borehole = _text(values, "钻孔编号")
            if not borehole:
                raise ShiftRuleError("钻孔编号必须填写，班次区间按钻孔分别推进")

            # 并发闸门在前：序号过期时直接告知最新序号，避免拿参数错误掩盖冲突。
            seq = self._check_optimistic(borehole, expected_seq, operation_id)

            start = _as_depth(values.get("深度起"), "深度起")
            end = _as_depth(values.get("深度止"), "深度止")
            assert start is not None and end is not None
            if end <= start:
                raise ShiftRuleError(f"深度止（{end}）必须大于深度起（{start}），区间不能为空")

            shift = _text(values, "班次") or "白班 08:00-20:00"
            crew = _text(values, "钻探人员")
            rock = _text(values, "岩层描述")
            note = _text(values, "补充说明")
            water = _as_depth(values.get("水位深度"), "水位深度", required=False)

            # 同钻孔区间互斥：半开区间 [起, 止)，首尾相接允许，重叠拒绝。
            for other in store.rows(MODULE):
                if str(other.get("钻孔编号")) != borehole:
                    continue
                if start < float(other["深度止"]) and float(other["深度起"]) < end:
                    raise ShiftRuleError(
                        f"区间 {start:g}–{end:g}m 与班次 {other.get('日志编号')}"
                        f"（{float(other['深度起']):g}–{float(other['深度止']):g}m，"
                        f"状态：{other['status']}）重叠，跨班深度区间不允许重叠",
                        conflict=True,
                    )

            # 现场终孔确认优先：区间越过现场确认孔深时直接拦下，不允许把旧深度顶掉。
            confirmed = self._final_depth(borehole)
            if confirmed is not None and end > confirmed:
                raise ShiftRuleError(
                    f"深度止 {end:g}m 已超过{self._final_source(borehole)}的终孔深度 {confirmed:g}m；"
                    "现场终孔确认值优先，请按确认孔深补录"
                )

            rows = store.rows(MODULE)
            next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            same_borehole = [r for r in rows if str(r.get("钻孔编号")) == borehole]
            shift_no = max((int(r.get("班次序号", 0)) for r in same_borehole), default=0) + 1
            log_no = _text(values, "日志编号") or f"DRIL-{next_id:04d}"
            now = _now()
            entry = {
                "id": next_id,
                "日志编号": log_no,
                "钻孔编号": borehole,
                "班次": shift,
                "班次序号": shift_no,
                "深度起": start,
                "深度止": end,
                "回次进尺": round(end - start, 4),
                "岩层描述": rock,
                "水位深度": water,
                "钻探人员": crew,
                "上报基准": SOURCE_BACKFILL if note else SOURCE_SITE,
                "补充说明": note,
                "终孔超深": False,
                "status": STATUS_TODO,
                "pending": True,
                "abnormal": False,
                "审批结论": "",
                "审核人": "",
                "审核时间": "",
                "填写人": "",
                "填写时间": "",
                "创建人": operator,
                "创建时间": now,
                "更新时间": now,
            }
            rows.append(entry)
            seq = self._append_journal(
                borehole,
                action="补录班次",
                entry_id=next_id,
                operator=operator,
                payload={"深度起": start, "深度止": end, "班次": shift, "上报基准": entry["上报基准"]},
                note=note,
                operation_id=operation_id,
            )
            self._sync_todo(entry)
            return entry, seq

    # -------------------------------------------------------------- 状态推进

    @_replayable
    def advance(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any],
        *,
        operator: str = "",
        operation_id: str = "",
        expected_seq: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        """填写日志 / 提交审核（含跳步）。返回（更新后的班次, 最新操作序号）。"""
        self.bootstrap()
        operator = operator or _text(values, "operator") or "值班管理员"
        operation_id = operation_id or _text(values, "operation_id")
        if expected_seq is None and str(values.get("expected_seq") or "").strip() != "":
            expected_seq = int(values["expected_seq"])
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                raise ShiftRuleError(f"班次记录 {entry_id} 不存在或已归档")
            borehole = str(entry["钻孔编号"])
            # 去重闸门在前：掉线重发同一操作时，哪怕班次状态已前进，也按原结果返回。
            seq = self._check_optimistic(borehole, expected_seq, operation_id)
            if action not in ACTION_TARGETS:
                raise ShiftRuleError(f"动作「{action}」不属于班次编录可执行范围（只支持填写日志、提交审核）")
            if entry["status"] == STATUS_REVIEWED:
                raise ShiftRuleError(
                    f"班次 {entry.get('日志编号')} 已审核锁定，既有班次按原上报基准留存，不能再改"
                )

            target = ACTION_TARGETS[action]
            current_idx = STATUS_ORDER.index(entry["status"])
            target_idx = STATUS_ORDER.index(target)
            if target_idx <= current_idx:
                raise ShiftRuleError(
                    f"班次当前是「{entry['status']}」，状态只能待填写→已填写→已审核单向推进，"
                    f"不能执行「{action}」"
                )
            reason = _text(values, "原因") or str(values.get("remark") or "").strip()
            jumping = target_idx > current_idx + 1
            if jumping and not reason:
                raise ShiftRuleError(
                    f"从「{entry['status']}」直接到「{target}」属于跳步提交，必须填写跳步原因"
                )

            now = _now()

            if target == STATUS_FILLED:
                # 填写时允许把补录时没带全的班报内容补齐。
                for field in ("岩层描述", "钻探人员", "班次"):
                    value = _text(values, field)
                    if value:
                        entry[field] = value
                water = values.get("水位深度")
                if str(water or "").strip() != "":
                    entry["水位深度"] = _as_depth(water, "水位深度", required=False)
                end_override = values.get("深度止")
                if str(end_override or "").strip() != "":
                    new_end = _as_depth(end_override, "深度止")
                    assert new_end is not None
                    self._guard_interval_change(entry, float(entry["深度起"]), new_end)
                    entry["深度止"] = new_end
                    entry["回次进尺"] = round(new_end - float(entry["深度起"]), 4)
                entry["status"] = STATUS_FILLED
                entry["填写人"] = operator
                entry["填写时间"] = now
                entry["更新时间"] = now
                journal_action = "填写日志"
                journal_note = reason
                journal_payload: dict[str, Any] = {"目标状态": STATUS_FILLED}
            else:
                conclusion = _text(values, "审批结论") or reason
                if not conclusion:
                    raise ShiftRuleError("提交审核必须填写审批结论")
                entry["status"] = STATUS_REVIEWED
                entry["pending"] = False
                entry["审批结论"] = conclusion
                entry["审核人"] = operator
                entry["审核时间"] = now
                entry["更新时间"] = now
                journal_action = "跳步提交审核" if jumping else "提交审核"
                journal_note = conclusion if jumping else ""
                journal_payload = {"目标状态": STATUS_REVIEWED}
                if jumping and reason:
                    journal_payload["跳步原因"] = reason

            # 现场终孔确认值优先：原上报深度保留在班次上，生效深度按确认值封顶。
            confirmed = self._final_depth(borehole)
            entry["终孔超深"] = bool(confirmed is not None and float(entry["深度止"]) > confirmed)

            seq = self._append_journal(
                borehole,
                action=journal_action,
                entry_id=int(entry["id"]),
                operator=operator,
                payload=journal_payload,
                note=journal_note,
                operation_id=operation_id,
            )

            if target == STATUS_REVIEWED:
                self._sync_reviewed(entry)
            self._sync_todo(entry)
            return entry, seq

    # ---------------------------------------------------------- 终孔深度登记

    @_replayable
    def set_final_depth(
        self,
        values: dict[str, Any],
        *,
        operator: str = "",
        operation_id: str = "",
        expected_seq: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        """登记终孔深度。现场终孔确认一到就覆盖补充上报口径，并重算该孔台账/曲线。"""
        self.bootstrap()
        operator = operator or _text(values, "operator") or "值班管理员"
        operation_id = operation_id or _text(values, "operation_id")
        if expected_seq is None and str(values.get("expected_seq") or "").strip() != "":
            expected_seq = int(values["expected_seq"])
        with self._lock:
            borehole = _text(values, "钻孔编号")
            if not borehole:
                raise ShiftRuleError("钻孔编号必须填写")
            site = _as_depth(values.get("现场终孔确认深度"), "现场终孔确认深度", required=False)
            backfill = _as_depth(values.get("补充上报深度"), "补充上报深度", required=False)
            if site is None and backfill is None:
                raise ShiftRuleError("现场终孔确认深度、补充上报深度至少填写一个")

            seq = self._check_optimistic(borehole, expected_seq, operation_id)

            row = self._final_row(borehole)
            now = _now()
            # 现场值优先：只来补充上报、且已有现场确认时，不允许把口径改回去。
            if site is None and backfill is not None and row is not None and row.get("现场终孔确认深度") is not None:
                raise ShiftRuleError(
                    f"钻孔 {borehole} 已有现场终孔确认值 {float(row['现场终孔确认深度']):g}m，"
                    "现场终孔确认优先，补充上报不能覆盖"
                )
            if site is not None:
                effective, source = site, FINAL_SOURCE_SITE
            else:
                assert backfill is not None
                effective, source = backfill, FINAL_SOURCE_BACKFILL

            if row is None:
                row = {
                    "id": max((int(r.get("id", 0)) for r in store.rows(FINAL_DEPTH_MODULE)), default=0) + 1,
                    "钻孔编号": borehole,
                }
                store.rows(FINAL_DEPTH_MODULE).append(row)
            row.update({
                "现场终孔确认深度": site if site is not None else row.get("现场终孔确认深度"),
                "补充上报深度": backfill if backfill is not None else row.get("补充上报深度"),
                "生效深度": effective,
                "深度来源": source,
                "确认人": operator,
                "更新时间": now,
            })

            # 终孔口径变化：未审核班次只标记是否越界、不改原始区间；已审核台账重算生效列，
            # 原始上报列保留（既有班次按原上报基准留存）。
            for entry in store.rows(MODULE):
                if str(entry.get("钻孔编号")) != borehole:
                    continue
                entry["终孔超深"] = float(entry["深度止"]) > effective
                if entry["status"] == STATUS_REVIEWED:
                    self._sync_reviewed(entry)
                self._sync_todo(entry)
            self._rebuild_curve(borehole)

            seq = self._append_journal(
                borehole,
                action="登记终孔深度",
                entry_id=None,
                operator=operator,
                payload={"生效深度": effective, "深度来源": source},
                note="",
                operation_id=operation_id,
            )
            return row, seq

    # --------------------------------------------------------------- 派生表

    def list_ledger(self, *, borehole: str | None = None) -> list[dict[str, Any]]:
        """钻探台账：只认已审核班次，生效孔深受现场终孔确认封顶。"""
        self.bootstrap()
        rows = [dict(r) for r in store.rows(LEDGER_MODULE)]
        if borehole:
            rows = [r for r in rows if str(r.get("钻孔编号")) == borehole]
        rows.sort(key=lambda r: (str(r.get("钻孔编号")), float(r.get("深度起"))))
        return rows

    def get_curve(self, *, borehole: str | None = None) -> list[dict[str, Any]]:
        """孔深曲线：每个已审核班次一个累计孔深点，数据全部来自班次写入路径。"""
        self.bootstrap()
        rows = [dict(r) for r in store.rows(CURVE_MODULE)]
        if borehole:
            rows = [r for r in rows if str(r.get("钻孔编号")) == borehole]
        rows.sort(key=lambda r: (str(r.get("钻孔编号")), int(r.get("序号", 0))))
        return rows

    def list_todo(self, *, borehole: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        """班次待办：只保留未完成（待填写/已填写待审核）班次，审核即清除。"""
        self.bootstrap()
        rows = [dict(r) for r in store.rows(TODO_MODULE)]
        if borehole:
            rows = [r for r in rows if str(r.get("钻孔编号")) == borehole]
        if status:
            rows = [r for r in rows if str(r.get("状态")) == status]
        rows.sort(key=lambda r: (str(r.get("钻孔编号")), int(r.get("班次序号", 0))))
        return rows

    def list_journal(self, *, borehole: str | None = None) -> list[dict[str, Any]]:
        """操作序号流水：客户端掉线后按这里的最新序号续做。"""
        self.bootstrap()
        rows = [dict(r) for r in store.rows(JOURNAL_MODULE)]
        if borehole:
            rows = [r for r in rows if str(r.get("钻孔编号")) == borehole]
        rows.sort(key=lambda r: (str(r.get("钻孔编号")), int(r.get("seq"))))
        return rows

    def latest_seq(self, borehole: str) -> int:
        self.bootstrap()
        return self._latest_seq(borehole)

    # ------------------------------------------------------------- 内部方法

    def _final_row(self, borehole: str) -> dict[str, Any] | None:
        for row in store.rows(FINAL_DEPTH_MODULE):
            if str(row.get("钻孔编号")) == borehole:
                return row
        return None

    def _final_depth(self, borehole: str) -> float | None:
        row = self._final_row(borehole)
        return None if row is None else float(row["生效深度"])

    def _final_source(self, borehole: str) -> str:
        row = self._final_row(borehole)
        return str(row["深度来源"]) if row else "已登记"

    def _guard_interval_change(self, entry: dict[str, Any], start: float, end: float) -> None:
        """填写阶段修正深度止时，同样不能与别的班次重叠、不能越过现场终孔。"""
        if end <= start:
            raise ShiftRuleError(f"深度止（{end}）必须大于深度起（{start}），区间不能为空")
        for other in store.rows(MODULE):
            if int(other["id"]) == int(entry["id"]) or str(other.get("钻孔编号")) != str(entry["钻孔编号"]):
                continue
            if start < float(other["深度止"]) and float(other["深度起"]) < end:
                raise ShiftRuleError(
                    f"修正后的区间 {start:g}–{end:g}m 与班次 {other.get('日志编号')} 重叠，不允许保存"
                )
        confirmed = self._final_depth(str(entry["钻孔编号"]))
        if confirmed is not None and end > confirmed:
            raise ShiftRuleError(
                f"深度止 {end:g}m 超过{self._final_source(str(entry['钻孔编号']))}的终孔深度 {confirmed:g}m"
            )

    def _sync_reviewed(self, entry: dict[str, Any]) -> None:
        """审批结论落到台账与曲线（按日志id幂等 upsert，重发不会产生两套数据）。"""
        borehole = str(entry["钻孔编号"])
        confirmed = self._final_depth(borehole)
        raw_end = float(entry["深度止"])
        effective_end = min(raw_end, confirmed) if confirmed is not None else raw_end

        ledger_rows = store.rows(LEDGER_MODULE)
        ledger = next((r for r in ledger_rows if int(r["日志id"]) == int(entry["id"])), None)
        if ledger is None:
            ledger = {"id": max((int(r.get("id", 0)) for r in ledger_rows), default=0) + 1, "日志id": entry["id"]}
            ledger_rows.append(ledger)
        ledger.update({
            "钻孔编号": borehole,
            "日志编号": entry["日志编号"],
            "班次": entry.get("班次"),
            "班次序号": entry.get("班次序号"),
            "深度起": entry["深度起"],
            "深度止": raw_end,
            "生效深度止": round(effective_end, 4),
            "回次进尺": entry.get("回次进尺"),
            "岩层描述": entry.get("岩层描述"),
            "上报基准": entry.get("上报基准"),
            "补充说明": entry.get("补充说明"),
            "终孔超深": bool(confirmed is not None and raw_end > confirmed),
            "审批结论": entry.get("审批结论"),
            "审核人": entry.get("审核人"),
            "审核时间": entry.get("审核时间"),
            "同步时间": _now(),
        })

        self._rebuild_curve(borehole)

    def _rebuild_curve(self, borehole: str) -> None:
        """按该孔已审核班次重算孔深曲线（生效深度封顶），区间不重叠保证曲线单调。"""
        curve_rows = store.rows(CURVE_MODULE)
        for row in [r for r in curve_rows if str(r.get("钻孔编号")) == borehole]:
            curve_rows.remove(row)
        confirmed = self._final_depth(borehole)
        reviewed = sorted(
            (r for r in store.rows(MODULE)
             if str(r.get("钻孔编号")) == borehole and r["status"] == STATUS_REVIEWED),
            key=lambda r: (float(r["深度起"]), int(r["id"])),
        )
        cumulative = 0.0
        next_id = max((int(r.get("id", 0)) for r in curve_rows), default=0) + 1
        for index, entry in enumerate(reviewed, start=1):
            raw_end = float(entry["深度止"])
            cumulative = min(raw_end, confirmed) if confirmed is not None else raw_end
            curve_rows.append({
                "id": next_id,
                "日志id": entry["id"],
                "日志编号": entry["日志编号"],
                "钻孔编号": borehole,
                "序号": index,
                "班次序号": entry.get("班次序号"),
                "深度止": raw_end,
                "生效孔深": round(cumulative, 4),
                "终孔超深": bool(confirmed is not None and raw_end > confirmed),
                "同步时间": _now(),
            })
            next_id += 1

    def _sync_todo(self, entry: dict[str, Any]) -> None:
        """待办随班次状态增删：建立即待办，审核即清除（不再残留已完成记录）。"""
        todo_rows = store.rows(TODO_MODULE)
        existing = next((t for t in todo_rows if int(t["日志id"]) == int(entry["id"])), None)
        if entry["status"] == STATUS_REVIEWED:
            if existing is not None:
                todo_rows.remove(existing)
            return
        if existing is None:
            existing = {"id": max((int(t.get("id", 0)) for t in todo_rows), default=0) + 1, "日志id": entry["id"]}
            todo_rows.append(existing)
        existing.update({
            "钻孔编号": entry["钻孔编号"],
            "日志编号": entry["日志编号"],
            "班次": entry.get("班次"),
            "班次序号": entry.get("班次序号"),
            "深度起": entry.get("深度起"),
            "深度止": entry.get("深度止"),
            "状态": entry["status"],
            "待办事项": "提交审核" if entry["status"] == STATUS_FILLED else "填写班报内容",
            "上报基准": entry.get("上报基准"),
            "钻探人员": entry.get("钻探人员"),
            "终孔超深": bool(entry.get("终孔超深")),
        })

    # ------------------------------------------------- 操作序号 / 并发与续做

    def _latest_seq(self, borehole: str) -> int:
        return max(
            (int(r["seq"]) for r in store.rows(JOURNAL_MODULE) if str(r.get("钻孔编号")) == borehole),
            default=0,
        )

    def _check_optimistic(self, borehole: str, expected_seq: int | None, operation_id: str) -> int:
        """并发闸门：同序号冲突只放行一个；带 operation_id 的重发按原结果幂等返回。"""
        latest = self._latest_seq(borehole)
        if operation_id:
            for record in store.rows(JOURNAL_MODULE):
                if str(record.get("钻孔编号")) != borehole:
                    continue
                if str(record.get("operation_id") or "") == operation_id:
                    raise _ReplayHit(record)
        if expected_seq is not None and expected_seq != latest:
            raise ShiftRuleError(
                f"钻孔 {borehole} 的操作序号已推进到 {latest}（你提交的基线是 {expected_seq}），"
                "同一区间已有其他人先提交成功，请刷新后按最新序号续做",
                conflict=True,
            )
        return latest

    def _replay_result(self, record: dict[str, Any]) -> tuple[dict[str, Any], int, str, bool]:
        """命中去重流水：取出当时的班次/终孔登记当前状态，连同原序号原样返回。"""
        entry_id = record.get("日志id")
        entry = (
            store.find(MODULE, int(entry_id))
            if entry_id is not None
            else self._final_row(str(record["钻孔编号"]))
        )
        message = f"操作已在序号 {record['seq']} 执行过（{record.get('操作')}），按原结果返回，未重复写入"
        return entry if entry is not None else {}, int(record["seq"]), message, True

    def _append_journal(
        self,
        borehole: str,
        *,
        action: str,
        entry_id: int | None,
        operator: str,
        payload: dict[str, Any],
        note: str,
        operation_id: str = "",
    ) -> int:
        rows = store.rows(JOURNAL_MODULE)
        seq = max((int(r["seq"]) for r in rows if str(r.get("钻孔编号")) == borehole), default=0) + 1
        rows.append({
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "钻孔编号": borehole,
            "seq": seq,
            "操作": action,
            "日志id": entry_id,
            "操作人": operator,
            "内容": payload,
            "说明": note,
            "operation_id": operation_id,
            "时间": _now(),
        })
        return seq


service = DrillingLogService()
