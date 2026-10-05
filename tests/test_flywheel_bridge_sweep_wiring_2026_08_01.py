"""[包B] sweep 接线锁 —— 必须打在 `run_round_bridge` **入口链**上,不是直调函数。

背景:`sweep_stale_orphan_runs`(包④交付)全仓零调用点零测试引用 = 死函数。
本批把它接到桥跑入口(自愈式),所以锁也必须走入口:直接 `sweep_stale_orphan_runs(...)`
断言只能证明函数本身能跑,证不了"它真的被调到了"—— 那正是上一批漏过去的口子。

两条正向 + 三条边界:
  1. 注入 started_at=25h 前的 running 行 → 走 run_round_bridge(dry_run=False)→ 变 stale_orphan + error 文案匹配;
  2. 注入 1h 内的 running 行 → 不动(时间阈值真生效,不是按 status 一刀切);
  3. dry_run=True 的桥跑**不得**清扫(预览契约:零写库);
  4. sweep 抛异常时桥跑**不得**被打断(fail-open);
  5. health 快照带出 stale_orphan 计数(§B3)。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from db.connection import get_db
from db.flywheel_bridge_db import (
    STALE_ORPHAN_STATUS,
    get_flywheel_bridge_health,
    init_flywheel_bridge_tables,
)
from services.research_monitor import flywheel_bridge as fb

ROUND_ID = "sweepwire_round_2026_08_01"
BATCH_ID = f"batch_{ROUND_ID}"


@pytest.fixture()
def bridge_tables():
    """建桥跑表 + 入口链读到的最小上游表;每例前清空自己的行。"""
    # 🔴 run_round_bridge 入口在 sweep 之后会读 geo_research_raw(_round_industries)。
    #    这里必须调**真初始化器** `db.diagnosis_db.init_db()` —— 不能手搓一张精简版
    #    `CREATE TABLE IF NOT EXISTS geo_research_raw`:真初始化器同样是 IF NOT EXISTS,
    #    先建了残表就等于把它变成 no-op,同库后跑的用例全部缺列报错(隔离层 A/B 抓到过一次)。
    from db.diagnosis_db import init_db

    init_db()
    init_flywheel_bridge_tables()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM geo_flywheel_bridge_runs WHERE round_id LIKE %s", ("sweepwire_%",))
        cur.execute("DELETE FROM geo_research_raw WHERE batch_id = %s", (BATCH_ID,))
    yield
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM geo_flywheel_bridge_runs WHERE round_id LIKE %s", ("sweepwire_%",))
        cur.execute("DELETE FROM geo_research_raw WHERE batch_id = %s", (BATCH_ID,))


def _inject_running_run(*, age_hours: float, round_id: str) -> int:
    started = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO geo_flywheel_bridge_runs
                (round_id, batch_id, trigger_source, dry_run, status, started_at)
            VALUES (%s, %s, 'test', FALSE, 'running', %s)
            RETURNING id
            """,
            (round_id, f"batch_{round_id}", started),
        )
        return int(cur.fetchone()["id"])


def _row(run_id: int) -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, status, error, finished_at FROM geo_flywheel_bridge_runs WHERE id = %s",
            (run_id,),
        )
        return dict(cur.fetchone())


def _run_bridge(**kwargs):
    """走真入口。下游 stage 在测试库里大概率失败,但它们全在 _safe_stage 里 fail-soft,
    不影响我们要断言的入口段行为。"""
    return asyncio.run(fb.run_round_bridge(ROUND_ID, trigger_source="test", **kwargs))


# ---------------------------------------------------------------- 正向两条


def test_entry_sweeps_stale_orphan_run(bridge_tables):
    """25h 前的 running 行 → 经 run_round_bridge 入口自愈收成 stale_orphan。"""
    stale_id = _inject_running_run(age_hours=25, round_id="sweepwire_stale")
    assert _row(stale_id)["status"] == "running"

    _run_bridge(dry_run=False, page_size=50)

    row = _row(stale_id)
    assert row["status"] == STALE_ORPHAN_STATUS, (
        f"孤儿跑没被入口清扫(status={row['status']})—— sweep 没接上桥跑入口"
    )
    assert row["finished_at"] is not None, "收成终态却没落 finished_at"
    assert "未收尾" in (row["error"] or ""), f"error 文案不匹配: {row['error']!r}"


def test_entry_does_not_sweep_fresh_running_run(bridge_tables):
    """1h 内的 running 行不能动 —— 正在跑的那条被标死是最坏的误伤。"""
    fresh_id = _inject_running_run(age_hours=1, round_id="sweepwire_fresh")

    _run_bridge(dry_run=False, page_size=50)

    row = _row(fresh_id)
    assert row["status"] == "running", (
        f"1 小时内的 running 行被误清扫(status={row['status']})—— 时间阈值没生效"
    )
    assert row["finished_at"] is None
    assert row["error"] is None


# ---------------------------------------------------------------- 边界三条


def test_dry_run_bridge_does_not_sweep(bridge_tables):
    """dry_run 是预览,契约上零写库 —— 不能因为"顺手清扫"而破例。"""
    stale_id = _inject_running_run(age_hours=25, round_id="sweepwire_dry")

    _run_bridge(dry_run=True, page_size=50)

    assert _row(stale_id)["status"] == "running", "dry_run 桥跑写库了(清扫不该在预览里跑)"


def test_sweep_failure_does_not_block_bridge(bridge_tables, monkeypatch):
    """fail-open:清扫炸了也不能拖垮桥跑本体(清扫只是清扫)。"""
    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("模拟清扫失败")

    # 🔴 打在 flywheel_bridge 模块自己的名字上 —— 它是 `from ... import sweep_stale_orphan_runs`,
    #    patch 到 db.flywheel_bridge_db 上是打不中的(该坑已踩过)。
    monkeypatch.setattr(fb, "sweep_stale_orphan_runs", _boom)

    result = _run_bridge(dry_run=False, page_size=50)

    assert calls["n"] == 1, "入口根本没调 sweep(接线断了)"
    # 🔴 判别力关键:run_round_bridge 外层 except 会把任何异常吞成 {"status": "failed"},
    #    所以断 "status 非空 / 非 in_progress" 是恒真的假锁。必须断**桥跑真的跑完了 stage**:
    #    去掉 sweep 的 try/except → 异常穿到外层 → status="failed" 且 stages 为空 → 本条转红。
    assert result.get("status") in ("success", "partial"), (
        f"清扫异常把桥跑本体打断了(status={result.get('status')}, error={result.get('error')})"
    )
    assert result.get("stages"), "桥跑没跑到任何 stage —— 说明在入口就被清扫异常掀翻了"


def test_health_snapshot_exposes_stale_orphan_count(bridge_tables):
    """§B3:health 快照必须带出 stale_orphan 计数。

    `stuck_run_count` 只数 status='running',清扫一生效它就归 0 ——
    不单独带 stale_orphan,面板上"清扫跑没跑过"完全不可见。
    """
    stale_id = _inject_running_run(age_hours=25, round_id="sweepwire_health")
    before = get_flywheel_bridge_health()
    assert "stale_orphan_run_count" in before, "health 快照缺 stale_orphan_run_count"
    base = before["stale_orphan_run_count"]
    assert before["stuck_run_count"] >= 1

    _run_bridge(dry_run=False, page_size=50)

    after = get_flywheel_bridge_health()
    assert after["stale_orphan_run_count"] == base + 1, (
        f"清扫后 stale_orphan 计数没涨(before={base}, after={after['stale_orphan_run_count']})"
    )
    assert after["last_stale_orphan_at"] is not None
    assert "未收尾" in after["stale_orphan_message"]
    assert _row(stale_id)["status"] == STALE_ORPHAN_STATUS
