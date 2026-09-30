"""班次编录（钻探日志）业务规则。

设计口径（所有写入都走本服务这一条路径，避免日志、台账、曲线、待办各算一套）：

1. 单向推进：一条班次日志只能 待填写 -> 已填写 -> 已审核，不允许回退；
   已填写/已审核记录按原上报基准留存，补录只能补「待填写」班次，不能改历史。
2. 孔深区间：班次以 [孔深起, 孔深止) 表示，同一钻孔内已占用区间不允许重叠
   （首尾相接合法）；只允许向更深方向推进。
3. 跳步提交：正常只能逐档推进；确需跳过中间档必须填写跳步原因。
4. 终孔确认值优先：现场终孔确认的终孔深度优先于补充上报值，台账与曲线以
   终孔确认值为准；已上报班次仍保留原基准，并在投影上标注差异。
5. 并发：同一区间的占位/补录用区间锁串行化，两个账号同时补录只允许一个成功，
   另一个收到 409；每次写入分配单调递增的操作序号（seq），客户端掉线后凭
   since 续传，同一操作重放（幂等键）不重复生效。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "drilling_log"

# 单向状态机：只能按下标递增方向推进
STATUS_PENDING = "待填写"
STATUS_FILLED = "已填写"
STATUS_AUDITED = "已审核"
STATUS_ORDER = [STATUS_PENDING, STATUS_FILLED, STATUS_AUDITED]

SHIFT_ORDER = ["白班", "夜班"]

REQUIRED_CREATE_FIELDS = ["日志编号", "钻孔编号", "班次", "班次日期", "孔深起", "孔深止"]
FILLABLE_FIELDS = ["孔深起", "孔深止", "回次进尺", "岩层描述", "水位深度", "钻探人员"]

# 既有钻孔的现场终孔确认值（现场终孔确认单），优先于补充上报
_SEED_FINAL_DEPTHS: dict[str, dict[str, Any]] = {
    "ZK-02": {
        "终孔深度": 78.5,
        "确认人": "现场终孔组",
        "确认时间": "2026-09-26 18:00",
        "备注": "现场终孔丈量确认，作为台账与曲线基准",
    },
}


class DrillingError(Exception):
    """业务规则违例：http_status 决定接口返回码（409 用于并发/区间冲突）。"""

    def __init__(self, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_depth(value: Any, field: str, *, required: bool = True) -> float | None:
    """把「40.2」「40.2m」之类的上报值解析成米；脏数据在这里就被拦住。"""
    if value is None or str(value).strip() == "":
        if required:
            raise DrillingError(f"{field}必须填写且为数字（单位：米）")
        return None
    text = str(value).strip().lower().replace("米", "").replace("m", "").strip()
    try:
        depth = float(text)
    except ValueError as exc:
        raise DrillingError(f"{field}「{value}」不是有效的深度数值") from exc
    if depth < 0:
        raise DrillingError(f"{field}不能为负数")
    return round(depth, 3)


def _shift_rank(shift: str) -> int:
    try:
        return SHIFT_ORDER.index(shift)
    except ValueError:
        return len(SHIFT_ORDER)


class DrillingLogService:
    """单例服务：内存仓库 + 一把写锁 + 单调操作序号。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._booted = False
        # 终孔确认值：钻孔 -> {终孔深度, 确认人, 确认时间, 备注, history:[...]}
        self._final_depths: dict[str, dict[str, Any]] = {}
        # 操作流水（含序号），是台账/曲线/待办的同一数据底座
        self._ops: list[dict[str, Any]] = []
        self._seq = 0
        # 幂等键 -> seq；同一操作重放直接返回原结果
        self._idempotency: dict[str, int] = {}
        self._next_id = 1

    # ------------------------------------------------------------------
    # 启动引导：把仓库里的既有班次规整成域模型，既有班次按原上报基准留存
    # ------------------------------------------------------------------
    def _bootstrap(self) -> None:
        if self._booted:
            return
        rows = store.rows(MODULE)
        max_id = 0
        for row in rows:
            max_id = max(max_id, int(row.get("id", 0)))
            start = _to_depth(row.get("孔深起"), "孔深起", required=False)
            end = _to_depth(row.get("孔深止"), "孔深止", required=False)
            row["孔深起"] = "" if start is None else f"{start:g}"
            row["孔深止"] = "" if end is None else f"{end:g}"
            status = str(row.get("status") or STATUS_PENDING)
            if status not in STATUS_ORDER:
                status = STATUS_PENDING
            row["status"] = status
            row["日志状态"] = status
            row["pending"] = status != STATUS_AUDITED
            row.setdefault("abnormal", False)
            row.setdefault("version", 1 if status != STATUS_PENDING else 0)
            row.setdefault("审批结论", None)
            row.setdefault("审核人", None)
            row.setdefault("补录原因", None)
            row.setdefault("跳步原因", None)
            row.setdefault("created_by", None)
            row.setdefault("updated_by", None)
            # 累计孔深以区间止深为准，纠正「按本班进尺填累计孔深」的错位
            if end is not None:
                row["钻进深度"] = f"{end:g}"
            self._record_op(
                action="既有数据导入",
                row=row,
                operator=row.get("updated_by") or row.get("created_by") or "系统",
                note="既有班次按原上报基准留存",
                persist=False,
            )
        self._next_id = max_id + 1
        self._final_depths.update(
            {borehole: dict(value, history=[dict(value)]) for borehole, value in _SEED_FINAL_DEPTHS.items()}
        )
        self._booted = True

    def _record_op(
        self,
        *,
        action: str,
        row: dict[str, Any] | None,
        operator: str,
        note: str = "",
        persist: bool = True,
    ) -> dict[str, Any]:
        op = {
            "seq": None,
            "time": _now(),
            "action": action,
            "operator": operator,
            "日志编号": (row or {}).get("日志编号"),
            "钻孔编号": (row or {}).get("钻孔编号"),
            "班次": (row or {}).get("班次"),
            "班次日期": (row or {}).get("班次日期"),
            "孔深起": (row or {}).get("孔深起"),
            "孔深止": (row or {}).get("孔深止"),
            "status": (row or {}).get("status"),
            "note": note,
        }
        if persist:
            # 操作序号只对真实写入分配；既有数据导入是基线，不占序号，
            # 否则客户端 since=0 续传会永远缺一段。
            self._seq += 1
            op["seq"] = self._seq
            self._ops.append(op)
        return op

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------
    def _find(self, entry_id: int) -> dict[str, Any]:
        row = store.find(MODULE, entry_id)
        if row is None:
            raise DrillingError(f"班次记录 {entry_id} 不存在或已归档", http_status=404)
        return row

    def _find_by_log_no(self, log_no: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("日志编号") or "").strip() == log_no:
                return row
        return None

    def _interval_of(self, row: dict[str, Any]) -> tuple[float | None, float | None]:
        start = _to_depth(row.get("孔深起"), "孔深起", required=False)
        end = _to_depth(row.get("孔深止"), "孔深止", required=False)
        return start, end

    def _sorted_shifts(self, borehole: str | None = None) -> list[dict[str, Any]]:
        rows = [r for r in store.rows(MODULE) if borehole is None or r.get("钻孔编号") == borehole]
        return sorted(
            rows,
            key=lambda r: (
                str(r.get("班次日期") or ""),
                _shift_rank(str(r.get("班次") or "")),
                int(r.get("id", 0)),
            ),
        )

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        borehole: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            self._bootstrap()
            rows = self._sorted_shifts()
            if keyword:
                rows = [r for r in rows if keyword in str(r.get("日志编号", ""))]
            if status:
                rows = [r for r in rows if r.get("status") == status]
            if borehole:
                rows = [r for r in rows if r.get("钻孔编号") == borehole]
            total = len(rows)
            start = max(page - 1, 0) * size
            return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any]:
        with self._lock:
            self._bootstrap()
            return self._find(entry_id)

    # ------------------------------------------------------------------
    # 区间校验：同一钻孔跨班深度区间不允许重叠，只允许向更深推进
    # ------------------------------------------------------------------
    def _assert_no_overlap(
        self,
        borehole: str,
        start: float,
        end: float,
        *,
        ignore_id: int | None = None,
        enforce_advance: bool = True,
    ) -> None:
        if end <= start:
            raise DrillingError(f"孔深止（{end:g}m）必须大于孔深起（{start:g}m）")
        max_end = 0.0
        for row in store.rows(MODULE):
            if row.get("钻孔编号") != borehole or int(row.get("id", 0)) == ignore_id:
                continue
            other_start, other_end = self._interval_of(row)
            # 待填写占位没有有效区间，不参与占用
            if other_start is None or other_end is None:
                continue
            # [a,b) 与 [c,d) 首尾相接（b==c 或 d==a）合法
            if start < other_end and other_start < end:
                raise DrillingError(
                    f"深度区间 [{start:g}, {end:g}) 与班次「{row.get('日志编号')}」"
                    f"（{row.get('班次日期')} {row.get('班次')}，"
                    f"[{other_start:g}, {other_end:g})）重叠，跨班深度区间不允许重叠",
                    http_status=409,
                )
            # 单向推进只对已落实（已填写/已审核）区间计数；待填写占位只是班前排程
            if row.get("status") != STATUS_PENDING:
                max_end = max(max_end, other_end)
        if enforce_advance and start < max_end:
            raise DrillingError(
                f"孔深起 {start:g}m 浅于同孔已编录最深 {max_end:g}m；班次只能按孔深单向推进，"
                "请从当前最深位置起接续（允许留空段，不允许回退覆盖）",
                http_status=409,
            )

    def _operator(self, values: dict[str, Any]) -> str:
        operator = str(values.get("operator") or values.get("钻探人员") or "").strip()
        return operator or "值班管理员"

    # ------------------------------------------------------------------
    # 写入路径
    # ------------------------------------------------------------------
    def create_shift(self, values: dict[str, Any], *, idem_key: str | None = None) -> dict[str, Any]:
        """为某钻孔的下一个班次登记一条「待填写」占位（班前排程）。"""
        with self._lock:
            self._bootstrap()
            return self._write(
                idem_key,
                lambda: self._create_shift(values),
                operator=self._operator(values),
            )

    def _create_shift(self, values: dict[str, Any]) -> dict[str, Any]:
        missing = [f for f in REQUIRED_CREATE_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            raise DrillingError(f"缺少必填字段：{'、'.join(missing)}")
        log_no = str(values["日志编号"]).strip()
        if self._find_by_log_no(log_no) is not None:
            raise DrillingError(f"日志编号「{log_no}」已存在，不能重复登记")
        borehole = str(values["钻孔编号"]).strip()
        shift = str(values["班次"]).strip()
        shift_date = str(values["班次日期"]).strip()
        start = _to_depth(values.get("孔深起"), "孔深起")
        end = _to_depth(values.get("孔深止"), "孔深止")
        assert start is not None and end is not None
        self._assert_no_overlap(borehole, start, end)

        row: dict[str, Any] = {
            "id": self._next_id,
            "日志编号": log_no,
            "钻孔编号": borehole,
            "班次": shift,
            "班次日期": shift_date,
            "孔深起": f"{start:g}",
            "孔深止": f"{end:g}",
            "钻进深度": "",
            "回次进尺": "",
            "岩层描述": "",
            "水位深度": "",
            "钻探人员": str(values.get("钻探人员") or "").strip(),
            "status": STATUS_PENDING,
            "日志状态": STATUS_PENDING,
            "pending": True,
            "abnormal": False,
            "version": 0,
            "审批结论": None,
            "审核人": None,
            "补录原因": None,
            "跳步原因": None,
            "created_by": self._operator(values),
            "updated_by": None,
        }
        self._next_id += 1
        store.rows(MODULE).append(row)
        op = self._record_op(action="登记班次占位", row=row, operator=row["created_by"])
        return self._result(row, op, "班次占位已登记，等待填写编录")

    def fill_shift(self, entry_id: int, values: dict[str, Any], *, idem_key: str | None = None) -> dict[str, Any]:
        """补录/填写编录：待填写 -> 已填写。只允许补待填写班次，历史班次原基准留存。"""
        with self._lock:
            self._bootstrap()
            return self._write(
                idem_key,
                lambda: self._fill_shift(entry_id, values),
                operator=self._operator(values),
            )

    def _fill_shift(self, entry_id: int, values: dict[str, Any]) -> dict[str, Any]:
        row = self._find(entry_id)
        operator = self._operator(values)
        if row["status"] != STATUS_PENDING:
            raise DrillingError(
                f"班次「{row.get('日志编号')}」已是「{row['status']}」，"
                "补录只能针对待填写班次；既有班次按原上报基准留存，不能覆盖改写"
                "（如与他人同时补录，说明该区间已被另一账号先行提交）",
                http_status=409,
            )
        borehole = str(row["钻孔编号"])
        # 区间以本次上报为准（占位区间可能只排了大概），但仍受同一把区间锁约束
        start = _to_depth(values.get("孔深起", row.get("孔深起")), "孔深起")
        end = _to_depth(values.get("孔深止", row.get("孔深止")), "孔深止")
        assert start is not None and end is not None
        self._assert_no_overlap(borehole, start, end, ignore_id=int(row["id"]))
        lithology = str(values.get("岩层描述") or "").strip()
        if not lithology:
            raise DrillingError("岩层描述为编录必填内容，不能空缺")

        reason = str(values.get("补录原因") or "").strip()
        row["孔深起"] = f"{start:g}"
        row["孔深止"] = f"{end:g}"
        row["钻进深度"] = f"{end:g}"
        advance = _to_depth(values.get("回次进尺"), "回次进尺", required=False)
        row["回次进尺"] = f"{advance:g}" if advance is not None else f"{end - start:g}"
        row["岩层描述"] = lithology
        water = _to_depth(values.get("水位深度"), "水位深度", required=False)
        row["水位深度"] = "" if water is None else f"{water:g}"
        crew = str(values.get("钻探人员") or row.get("钻探人员") or "").strip()
        if crew:
            row["钻探人员"] = crew
        row["status"] = STATUS_FILLED
        row["日志状态"] = STATUS_FILLED
        row["pending"] = True
        row["abnormal"] = False
        row["version"] = int(row.get("version", 0)) + 1
        row["补录原因"] = reason or None
        row["updated_by"] = operator
        op = self._record_op(
            action="补录编录" if reason else "填写编录",
            row=row,
            operator=operator,
            note=reason,
        )
        return self._result(row, op, "编录已补录，状态推进为已填写")

    def audit_shift(self, entry_id: int, values: dict[str, Any], *, idem_key: str | None = None) -> dict[str, Any]:
        """审批：已填写 -> 已审核。审批结论随写入同步落台账/曲线/待办。"""
        with self._lock:
            self._bootstrap()
            return self._write(
                idem_key,
                lambda: self._audit_shift(entry_id, values),
                operator=self._operator(values),
            )

    def _audit_shift(self, entry_id: int, values: dict[str, Any]) -> dict[str, Any]:
        row = self._find(entry_id)
        operator = self._operator(values)
        if row["status"] == STATUS_AUDITED:
            raise DrillingError(
                f"班次「{row.get('日志编号')}」已审核，不能重复审批（可能已由另一账号处理）",
                http_status=409,
            )
        if row["status"] != STATUS_FILLED:
            raise DrillingError(
                f"班次「{row.get('日志编号')}」当前为「{row['status']}」，"
                "须先补录编录成为已填写，才能审核（不允许跳步）"
            )
        conclusion = str(values.get("审批结论") or "").strip() or "编录与进尺相符，审核通过"
        row["审批结论"] = conclusion
        row["审核人"] = operator
        row["status"] = STATUS_AUDITED
        row["日志状态"] = STATUS_AUDITED
        row["pending"] = False  # 审核完成即离开待办，待办由状态实时派生，不残留
        row["abnormal"] = False
        row["version"] = int(row.get("version", 0)) + 1
        row["updated_by"] = operator
        op = self._record_op(action="审核通过", row=row, operator=operator, note=conclusion)
        result = self._result(row, op, "审批结论已同步到钻探台账、孔深曲线与班次待办")
        result["sync"] = self._sync_payload(int(op["seq"]))
        return result

    def jump_submit(self, entry_id: int, values: dict[str, Any], *, idem_key: str | None = None) -> dict[str, Any]:
        """跳步提交：跳过中间档必须说明原因，且编录内容齐全，直接推进到已审核。"""
        with self._lock:
            self._bootstrap()
            return self._write(
                idem_key,
                lambda: self._jump_submit(entry_id, values),
                operator=self._operator(values),
            )

    def _jump_submit(self, entry_id: int, values: dict[str, Any]) -> dict[str, Any]:
        row = self._find(entry_id)
        operator = self._operator(values)
        reason = str(values.get("跳步原因") or "").strip()
        if not reason:
            raise DrillingError("跳步提交必须填写跳步原因，说明跳过中间档的依据")
        if row["status"] == STATUS_AUDITED:
            raise DrillingError(
                f"班次「{row.get('日志编号')}」已审核，无需再跳步提交（可能已由另一账号处理）",
                http_status=409,
            )

        start = _to_depth(values.get("孔深起", row.get("孔深起")), "孔深起")
        end = _to_depth(values.get("孔深止", row.get("孔深止")), "孔深止")
        assert start is not None and end is not None
        self._assert_no_overlap(str(row["钻孔编号"]), start, end, ignore_id=int(row["id"]))
        lithology = str(values.get("岩层描述") or row.get("岩层描述") or "").strip()
        if not lithology:
            raise DrillingError("跳步提交同样要带齐编录内容，岩层描述不能空缺")

        row["孔深起"] = f"{start:g}"
        row["孔深止"] = f"{end:g}"
        row["钻进深度"] = f"{end:g}"
        advance = _to_depth(values.get("回次进尺"), "回次进尺", required=False)
        row["回次进尺"] = f"{advance:g}" if advance is not None else f"{end - start:g}"
        row["岩层描述"] = lithology
        water = _to_depth(values.get("水位深度"), "水位深度", required=False)
        if water is not None:
            row["水位深度"] = f"{water:g}"
        crew = str(values.get("钻探人员") or row.get("钻探人员") or "").strip()
        if crew:
            row["钻探人员"] = crew
        conclusion = str(values.get("审批结论") or "").strip() or "跳步提交，审核通过"
        row["跳步原因"] = reason
        row["审批结论"] = conclusion
        row["审核人"] = operator
        row["status"] = STATUS_AUDITED
        row["日志状态"] = STATUS_AUDITED
        row["pending"] = False
        row["abnormal"] = True  # 跳步属于需要留痕关注的非常规路径
        row["version"] = int(row.get("version", 0)) + 1
        row["updated_by"] = operator
        op = self._record_op(action="跳步提交审核", row=row, operator=operator, note=f"原因：{reason}；结论：{conclusion}")
        result = self._result(row, op, "跳步提交已受理（已记录原因），审批结论已同步台账/曲线/待办")
        result["sync"] = self._sync_payload(int(op["seq"]))
        return result

    def confirm_final_depth(self, values: dict[str, Any], *, idem_key: str | None = None) -> dict[str, Any]:
        """现场终孔确认：确认值优先于补充上报，落台账与曲线基准。"""
        with self._lock:
            self._bootstrap()
            return self._write(
                idem_key,
                lambda: self._confirm_final_depth(values),
                operator=self._operator(values),
            )

    def _confirm_final_depth(self, values: dict[str, Any]) -> dict[str, Any]:
        borehole = str(values.get("钻孔编号") or "").strip()
        if not borehole:
            raise DrillingError("缺少必填字段：钻孔编号")
        depth = _to_depth(values.get("终孔深度"), "终孔深度")
        assert depth is not None
        record = {
            "终孔深度": depth,
            "确认人": self._operator(values),
            "确认时间": _now(),
            "备注": str(values.get("备注") or "").strip(),
        }
        history = self._final_depths.setdefault(borehole, {"history": []})
        history.update(record)
        history["history"].append(dict(record))

        # 同步钻孔台账的终孔字段（与日志同一写入路径，避免台账另算一套）
        bore_row = next((r for r in store.rows("borehole") if r.get("钻孔编号") == borehole), None)
        if bore_row is not None:
            bore_row["终孔深度"] = f"{depth:g}"
            bore_row["status"] = "已终孔"
            bore_row["钻孔状态"] = "已终孔"
            bore_row["pending"] = False
            if not str(bore_row.get("终孔日期") or "").strip():
                bore_row["终孔日期"] = record["确认时间"][:10]

        self._seq += 1
        op = {
            "seq": self._seq,
            "time": record["确认时间"],
            "action": "终孔确认",
            "operator": record["确认人"],
            "日志编号": None,
            "钻孔编号": borehole,
            "班次": None,
            "班次日期": None,
            "孔深起": None,
            "孔深止": f"{depth:g}",
            "status": None,
            "note": f"现场终孔确认值 {depth:g}m 生效，优先于补充上报",
        }
        self._ops.append(op)
        result = self._result(None, op, f"钻孔 {borehole} 终孔确认值 {depth:g}m 已生效")
        result["终孔确认"] = dict(history)
        result["sync"] = self._sync_payload(int(op["seq"]))
        return result

    # ------------------------------------------------------------------
    # 投影：台账、孔深曲线、班次待办全部从同一底座实时派生
    # ------------------------------------------------------------------
    def ledger(self, borehole: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._bootstrap()
            boreholes = sorted({str(r.get("钻孔编号")) for r in store.rows(MODULE) if r.get("钻孔编号")})
            if borehole:
                boreholes = [b for b in boreholes if b == borehole]
            items: list[dict[str, Any]] = []
            for bh in boreholes:
                shifts = self._sorted_shifts(bh)
                reported = None
                for row in shifts:
                    _, end = self._interval_of(row)
                    if end is not None and (reported is None or end > reported):
                        reported = end
                final = self._final_depths.get(bh)
                final_depth = final["终孔深度"] if final else None
                effective = final_depth if final_depth is not None else reported
                items.append({
                    "钻孔编号": bh,
                    "班次总数": len(shifts),
                    "已审核班次数": sum(1 for r in shifts if r["status"] == STATUS_AUDITED),
                    "已填写班次数": sum(1 for r in shifts if r["status"] == STATUS_FILLED),
                    "待填写班次数": sum(1 for r in shifts if r["status"] == STATUS_PENDING),
                    "补充上报累计孔深": None if reported is None else round(reported, 3),
                    "现场终孔确认值": final_depth,
                    "台账采用孔深": round(effective, 3) if effective is not None else None,
                    "深度基准": "现场终孔确认" if final_depth is not None else "补充上报",
                    "确认人": final["确认人"] if final else None,
                    "确认时间": final["确认时间"] if final else None,
                })
            return {"items": items, "generated_at": _now()}

    def depth_curve(self, borehole: str) -> dict[str, Any]:
        with self._lock:
            self._bootstrap()
            points: list[dict[str, Any]] = []
            for row in self._sorted_shifts(borehole):
                start, end = self._interval_of(row)
                if start is None or end is None:
                    continue
                points.append({
                    "seq": len(points),
                    "日志编号": row.get("日志编号"),
                    "班次日期": row.get("班次日期"),
                    "班次": row.get("班次"),
                    "孔深起": round(start, 3),
                    "孔深止": round(end, 3),
                    "累计孔深": round(end, 3),
                    "状态": row.get("status"),
                    "基准": "原上报",
                })
            final = self._final_depths.get(borehole)
            if final is not None:
                depth = float(final["终孔深度"])
                # 终孔确认值优先：曲线末点收敛到确认值；确认值更深则补一个终孔点
                if points and abs(float(points[-1]["累计孔深"]) - depth) > 1e-6:
                    points.append({
                        "seq": len(points),
                        "日志编号": "FINAL",
                        "班次日期": final.get("确认时间", "")[:10],
                        "班次": "终孔确认",
                        "孔深起": round(float(points[-1]["累计孔深"]), 3),
                        "孔深止": round(depth, 3),
                        "累计孔深": round(depth, 3),
                        "状态": "已终孔",
                        "基准": "现场终孔确认",
                    })
                elif points:
                    points[-1]["累计孔深"] = round(depth, 3)
                    points[-1]["孔深止"] = round(depth, 3)
                    points[-1]["基准"] = "现场终孔确认"
            return {
                "钻孔编号": borehole,
                "points": points,
                "终孔确认": None if final is None else {k: v for k, v in final.items() if k != "history"},
                "generated_at": _now(),
            }

    def pending_todos(self, borehole: str | None = None) -> dict[str, Any]:
        """待办实时派生：只有未审核班次在列，审核完成立即消失，不残留。"""
        with self._lock:
            self._bootstrap()
            rows = [r for r in self._sorted_shifts(borehole) if r["status"] != STATUS_AUDITED]
            items = [{
                "日志编号": r.get("日志编号"),
                "钻孔编号": r.get("钻孔编号"),
                "班次日期": r.get("班次日期"),
                "班次": r.get("班次"),
                "孔深起": r.get("孔深起"),
                "孔深止": r.get("孔深止"),
                "钻探人员": r.get("钻探人员"),
                "待办动作": "补录编录" if r["status"] == STATUS_PENDING else "提交审核",
                "状态": r.get("status"),
            } for r in rows]
            return {"items": items, "total": len(items), "generated_at": _now()}

    # ------------------------------------------------------------------
    # 断线续传 / 同步：操作序号单调递增，凭 since 拉增量；幂等键防重复生效
    # ------------------------------------------------------------------
    def sync(self, since: int = 0) -> dict[str, Any]:
        with self._lock:
            self._bootstrap()
            return self._sync_payload(since)

    def _sync_payload(self, since: int) -> dict[str, Any]:
        return {
            "since": since,
            "last_seq": self._seq,
            "has_more": False,
            "operations": [dict(op) for op in self._ops if int(op["seq"]) > since],
            "ledger": self.ledger(),
            "pending": self.pending_todos(),
            "final_depths": {
                bh: {k: v for k, v in data.items() if k != "history"}
                for bh, data in self._final_depths.items()
            },
        }

    def list_ops(self, since: int = 0, limit: int = 100) -> dict[str, Any]:
        with self._lock:
            self._bootstrap()
            ops = [dict(op) for op in self._ops if int(op["seq"]) >= since]
            return {"items": ops[:limit], "last_seq": self._seq}

    def _write(self, idem_key: str | None, fn, *, operator: str) -> dict[str, Any]:
        """所有写操作统一入口：加锁、分配/校验幂等键、保证序号连续。"""
        if idem_key:
            replay_seq = self._idempotency.get(idem_key)
            if replay_seq is not None:
                replay = next((op for op in self._ops if int(op["seq"]) == replay_seq), None)
                return {
                    "ok": True,
                    "replayed": True,
                    "message": "该操作已生效，本次为幂等重放，未重复写入",
                    "entry": None,
                    "operation": replay,
                    "last_seq": self._seq,
                }
        result = fn()
        if idem_key and isinstance(result, dict) and result.get("operation"):
            self._idempotency[idem_key] = int(result["operation"]["seq"])
        return result

    def _result(self, row: dict[str, Any] | None, op: dict[str, Any], message: str) -> dict[str, Any]:
        return {
            "ok": True,
            "replayed": False,
            "message": message,
            "entry": dict(row) if row is not None else None,
            "operation": op,
            "last_seq": self._seq,
        }


service = DrillingLogService()
