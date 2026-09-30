"""班次编录写入路径的规则验证（不依赖 pytest，直接断言）。"""
from __future__ import annotations

import sys
import threading

sys.path.insert(0, "/tmp/pylibs")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.drilling_log import (  # noqa: E402
    ShiftRuleError,
    service,
)

client = TestClient(app)
passed = 0


def check(name: str, fn):
    global passed
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL {name}: {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        sys.exit(1)
    passed += 1
    print(f"PASS {name}")


# 种子数据：2 已审核 / 1 已填写 / 2 待填写；ZK-102 现场终孔 18，ZK-103 补充上报 20。
r = client.get("/api/drilling_log/summary").json()
assert r["同步一致"] is True


def t_state_machine_forward_only():
    # 新孔上补一条，再顺序推进。
    res = client.post("/api/drilling_log/backfill", json={
        "operator": "甲", "operation_id": "op-fwd",
        "values": {"钻孔编号": "ZK-201", "深度起": 0, "深度止": 5, "班次": "白班 08:00-20:00"},
    })
    assert res.status_code == 200, res.text
    eid = res.json()["entry"]["id"]
    # 待填写 → 直接再「填写」一次合法推进到已填写。
    res = client.post(f"/api/drilling_log/{eid}/actions", json={
        "operator": "甲", "values": {"action": "填写日志", "岩层描述": "土层"},
    })
    assert res.json()["entry"]["status"] == "已填写", res.text
    # 已填写再执行填写日志（非前进）必须被拦。
    res = client.post(f"/api/drilling_log/{eid}/actions", json={
        "operator": "甲", "values": {"action": "填写日志"},
    })
    assert res.status_code == 400 and "单向推进" in res.json()["detail"], res.text
    # 已审核后任何动作都锁定。
    res = client.post(f"/api/drilling_log/{eid}/actions", json={
        "operator": "乙", "values": {"action": "提交审核", "审批结论": "通过"},
    })
    assert res.json()["entry"]["status"] == "已审核"
    res = client.post(f"/api/drilling_log/{eid}/actions", json={
        "operator": "乙", "values": {"action": "填写日志"},
    })
    assert res.status_code == 400 and "锁定" in res.json()["detail"], res.text


check("状态只能 待填写→已填写→已审核，已审核锁定", t_state_machine_forward_only)


def t_jump_requires_reason():
    res = client.post("/api/drilling_log/backfill", json={
        "operator": "甲", "operation_id": "op-jump",
        "values": {"钻孔编号": "ZK-202", "深度起": 0, "深度止": 4},
    })
    eid = res.json()["entry"]["id"]
    # 待填写直接提交审核且无原因 → 400。
    res = client.post(f"/api/drilling_log/{eid}/actions", json={
        "operator": "乙", "values": {"action": "提交审核"},
    })
    assert res.status_code == 400 and "跳步" in res.json()["detail"], res.text
    # 带原因后放行，且流水里保留跳步原因。
    res = client.post(f"/api/drilling_log/{eid}/actions", json={
        "operator": "乙",
        "values": {"action": "提交审核", "审批结论": "班报经现场补核无误",
                   "原因": "交接班漏点填写，依据现场班报底稿跳步"},
    })
    assert res.status_code == 200, res.text
    journal = client.get("/api/drilling_log/journal", params={"borehole": "ZK-202"}).json()
    jump = [x for x in journal["items"] if x["操作"] == "跳步提交审核"]
    assert jump and "跳步原因" in jump[0]["内容"], journal


check("跳步提交必须说明原因并留痕", t_jump_requires_reason)


def t_interval_overlap_rejected():
    # ZK-202 已有 0-4 已审核。
    res = client.post("/api/drilling_log/backfill", json={
        "operator": "甲", "values": {"钻孔编号": "ZK-202", "深度起": 3, "深度止": 8},
    })
    assert res.status_code == 409 and "重叠" in res.json()["detail"], res.text
    for body, hint in [
        ({"钻孔编号": "ZK-202", "深度起": 4, "深度止": 4}, "不能为空"),
        ({"钻孔编号": "ZK-202", "深度起": 4, "深度止": 3}, "大于"),
    ]:
        res = client.post("/api/drilling_log/backfill", json={"operator": "甲", "values": body})
        assert res.status_code == 400 and hint in res.json()["detail"], (body, res.text)
    # 首尾相接 4-9 允许。
    res = client.post("/api/drilling_log/backfill", json={
        "operator": "甲", "operation_id": "op-abut",
        "values": {"钻孔编号": "ZK-202", "深度起": 4, "深度止": 9, "补充说明": "漏报补录"},
    })
    assert res.status_code == 200 and res.json()["entry"]["上报基准"] == "补充上报", res.text


check("跨班深度区间不允许重叠，相接允许", t_interval_overlap_rejected)


def t_approval_syncs_three_tables():
    # 用 ZK-201 已审核的 0-5；再补 5-8 走完整流程，检查三处同步。
    res = client.post("/api/drilling_log/backfill", json={
        "operator": "甲", "operation_id": "op-sync",
        "values": {"钻孔编号": "ZK-201", "深度起": 5, "深度止": 8},
    })
    eid = res.json()["entry"]["id"]
    todos_before = client.get("/api/drilling_log/todos", params={"borehole": "ZK-201"}).json()
    assert any(t["日志id"] == eid for t in todos_before["items"])
    client.post(f"/api/drilling_log/{eid}/actions", json={
        "operator": "甲", "values": {"action": "填写日志", "岩层描述": "砂岩"},
    })
    res = client.post(f"/api/drilling_log/{eid}/actions", json={
        "operator": "乙", "values": {"action": "提交审核", "审批结论": "同意归档"},
    })
    assert res.status_code == 200
    ledger = client.get("/api/drilling_log/ledger", params={"borehole": "ZK-201"}).json()
    curve = client.get("/api/drilling_log/curve", params={"borehole": "ZK-201"}).json()
    todos = client.get("/api/drilling_log/todos", params={"borehole": "ZK-201"}).json()
    led = [x for x in ledger["items"] if x["日志id"] == eid]
    assert led and led[0]["审批结论"] == "同意归档" and led[0]["审核人"] == "乙"
    assert [x["日志id"] for x in curve["items"]] == sorted(x["日志id"] for x in curve["items"])
    depths = [x["生效孔深"] for x in curve["items"]]
    assert depths == sorted(depths) and depths[-1] == 8.0, curve
    assert all(t["日志id"] != eid for t in todos["items"]), "审核后待办仍残留"
    assert client.get("/api/drilling_log/summary").json()["同步一致"] is True


check("审批结论同步台账、孔深曲线并清除待办", t_approval_syncs_three_tables)


def t_site_final_depth_priority_and_baseline_kept():
    # ZK-103：现有 0-6 已审核（台账原始 6.0）；终孔目前只有补充上报 20。
    led_before = [x for x in client.get("/api/drilling_log/ledger", params={"borehole": "ZK-103"}).json()["items"]]
    assert led_before and led_before[0]["深度止"] == 6.0 and led_before[0]["生效深度止"] == 6.0
    # 现场确认终孔 5.5（小于已审 6.0）：台账原上报 6.0 保留，生效列封顶 5.5。
    res = client.post("/api/drilling_log/final_depth", json={
        "operator": "现场值班员", "operation_id": "op-final-site",
        "values": {"钻孔编号": "ZK-103", "现场终孔确认深度": 5.5},
    })
    assert res.status_code == 200, res.text
    led = client.get("/api/drilling_log/ledger", params={"borehole": "ZK-103"}).json()["items"][0]
    assert led["深度止"] == 6.0 and led["生效深度止"] == 5.5 and led["终孔超深"] is True, led
    curve = client.get("/api/drilling_log/curve", params={"borehole": "ZK-103"}).json()["items"][0]
    assert curve["深度止"] == 6.0 and curve["生效孔深"] == 5.5 and curve["终孔超深"] is True
    # 既有班次行本身的深度仍是 6.0（原上报基准留存）。
    entry = client.get(f"/api/drilling_log/{4}").json()
    assert entry["深度止"] == 6.0 and entry["上报基准"] == "现场记录"
    # 已有现场确认后，补充上报想改回去 → 拒绝。
    res = client.post("/api/drilling_log/final_depth", json={
        "operator": "周补录",
        "values": {"钻孔编号": "ZK-103", "补充上报深度": 22},
    })
    assert res.status_code == 400 and "现场终孔确认优先" in res.json()["detail"], res.text
    # 新补录越过现场终孔的区间 → 拒绝（ZK-102 现场终孔 18，既有班次只到 8）。
    res = client.post("/api/drilling_log/backfill", json={
        "operator": "甲",
        "values": {"钻孔编号": "ZK-102", "深度起": 8, "深度止": 20},
    })
    assert res.status_code == 400 and "现场终孔确认的终孔深度" in res.json()["detail"], res.text


check("现场终孔确认优先，既有班次按原上报基准留存", t_site_final_depth_priority_and_baseline_kept)


def t_concurrent_same_interval_only_one_wins():
    results: list[tuple[str, int]] = []
    barrier = threading.Barrier(3)

    def worker(who: str, op: str):
        barrier.wait()
        res = client.post("/api/drilling_log/backfill", json={
            "operator": who, "operation_id": op,
            "values": {"钻孔编号": "ZK-301", "深度起": 0, "深度止": 7},
        })
        results.append((who, res.status_code))

    threads = [threading.Thread(target=worker, args=(w, f"op-conc-{w}")) for w in ("甲", "乙")]
    for t in threads:
        t.start()
    barrier.wait()  # 主线程占一个槽位，两个 worker 到达后同时放行
    for t in threads:
        t.join()
    oks = [code for _, code in results if code == 200]
    rejects = [code for _, code in results if code == 409]
    assert len(oks) == 1 and len(rejects) == 1, results
    # 落库只能有一条 0-7。
    rows = client.get("/api/drilling_log", params={"borehole": "ZK-301"}).json()["items"]
    assert len(rows) == 1, rows


check("两个账号同时补录同一区间只允许一个成功", t_concurrent_same_interval_only_one_wins)


def t_resume_after_dropout_by_seq():
    # 并发赢家已把 ZK-301 推进到 seq 1；乙拿着更早的序号提交，应被 409 告知最新序号。
    res = client.post("/api/drilling_log/backfill", json={
        "operator": "乙", "operation_id": "op-stale", "expected_seq": 0,
        "values": {"钻孔编号": "ZK-301", "深度起": 7, "深度止": 9},
    })
    assert res.status_code == 409 and "操作序号已推进" in res.json()["detail"], res.text
    # 掉线重发：同一个 operation_id 重放，不产生第二条数据，返回原结果与原序号。
    first = client.post("/api/drilling_log/backfill", json={
        "operator": "乙", "operation_id": "op-resume", "expected_seq": 1,
        "values": {"钻孔编号": "ZK-301", "深度起": 7, "深度止": 9},
    }).json()
    assert first["seq"] == 2, first
    replay = client.post("/api/drilling_log/backfill", json={
        "operator": "乙", "operation_id": "op-resume", "expected_seq": 1,
        "values": {"钻孔编号": "ZK-301", "深度起": 7, "深度止": 9},
    }).json()
    assert replay["replayed"] is True and replay["seq"] == first["seq"], (first, replay)
    rows = client.get("/api/drilling_log", params={"borehole": "ZK-301"}).json()["items"]
    assert [(r["深度起"], r["深度止"]) for r in rows] == [(0.0, 7.0), (7.0, 9.0)], rows
    # 按最新序号正常续做下一班。
    latest = client.get("/api/drilling_log/journal", params={"borehole": "ZK-301"}).json()["latest_seq"]["ZK-301"]
    res = client.post("/api/drilling_log/backfill", json={
        "operator": "乙", "operation_id": "op-next", "expected_seq": latest,
        "values": {"钻孔编号": "ZK-301", "深度起": 9, "深度止": 11},
    })
    assert res.status_code == 200 and res.json()["seq"] == latest + 1, res.text

    # 审核请求掉线重发：班次已审核时，同一 operation_id 仍按原结果幂等返回，不报锁定。
    target_id = [r for r in client.get("/api/drilling_log", params={"borehole": "ZK-301"}).json()["items"]
                 if r["深度起"] == 9][0]["id"]
    client.post(f"/api/drilling_log/{target_id}/actions", json={
        "operator": "甲", "values": {"action": "填写日志", "岩层描述": "x"},
    })
    review_res = client.post(f"/api/drilling_log/{target_id}/actions", json={
        "operator": "乙", "operation_id": "op-review-resume",
        "values": {"action": "提交审核", "审批结论": "通过"},
    })
    assert review_res.status_code == 200 and review_res.json()["entry"]["status"] == "已审核"
    replay_review = client.post(f"/api/drilling_log/{target_id}/actions", json={
        "operator": "乙", "operation_id": "op-review-resume",
        "values": {"action": "提交审核", "审批结论": "通过"},
    })
    assert replay_review.status_code == 200 and replay_review.json()["replayed"] is True, replay_review.text


check("掉线后按操作序号接着续做，重发幂等不重复", t_resume_after_dropout_by_seq)


def t_seed_fixed_shifts_and_todos():
    # 旧 bug 回归：种子里 DRIL-0002 已填写，补录不会把它显示成旧班次；
    # ZK-102 的待填写 0-8 仍在待办，已审核的不出现在待办。
    rows = client.get("/api/drilling_log", params={"borehole": "ZK-101"}).json()["items"]
    assert [(r["日志编号"], r["深度起"], r["深度止"], r["status"]) for r in rows] == [
        ("DRIL-0001", 0.0, 12.5, "已审核"),
        ("DRIL-0002", 12.5, 25.0, "已填写"),
    ], rows
    todos = client.get("/api/drilling_log/todos").json()["items"]
    # 三个种子钻孔的待办正好是 id 2/3/5；其他钻孔测试新建的待办不参与这里的回归判断。
    seed_todo_ids = {t["日志id"] for t in todos if t["钻孔编号"] in {"ZK-101", "ZK-102", "ZK-103"}}
    assert seed_todo_ids == {2, 3, 5}, todos
    # 已审核班次（id 1、4）绝不能出现在待办里。
    assert not any(t["日志id"] in {1, 4} for t in todos), todos
    # 待补录的 ZK-103 6-14 在现场确认 5.5 后已标记越界，提醒现场核对。
    assert [t for t in todos if t["日志id"] == 5][0]["终孔超深"] is True
    # ZK-102 现场终孔 18：补录 8-18 合法，18-20 越过现场终孔被拒。
    ok = client.post("/api/drilling_log/backfill", json={
        "operator": "甲", "operation_id": "op-102-ok",
        "values": {"钻孔编号": "ZK-102", "深度起": 8, "深度止": 18},
    })
    assert ok.status_code == 200, ok.text
    bad = client.post("/api/drilling_log/backfill", json={
        "operator": "甲",
        "values": {"钻孔编号": "ZK-102", "深度起": 18, "深度止": 20},
    })
    assert bad.status_code == 400, bad.text


check("种子班次按孔深区间排序、待办无已完成残留", t_seed_fixed_shifts_and_todos)


print(f"\n{passed} 组规则全部通过")
