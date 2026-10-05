"""
per-job DB claim 单测(WORKERS=4 · SPEC §2.2 · FF3 硬化)

注入 fake db.connection.get_db → 测 claim 赢/reclaim/被占/fail-closed/fail-open + 装饰器流(token/CAS)。
"""
import sys
import types

import pytest

from services import sched_claim as sc


# ---------------- 分桶 ----------------

def test_bucket_floors_same_period_together(monkeypatch):
    t = [1_000_000.0]
    monkeypatch.setattr(sc.time, "time", lambda: t[0])
    b1 = sc._bucket(3600)
    t[0] += 59.0
    b2 = sc._bucket(3600)
    t[0] += 3600.0
    b3 = sc._bucket(3600)
    assert b1 == b2 and b1 != b3


# ---------------- claim DB 路径(注入 fake get_db)----------------

def _stub_db(monkeypatch, insert_row, update_row=None):
    """fetchone 顺序:①INSERT RETURNING ②UPDATE reclaim RETURNING。"""
    class _C:
        def __init__(self):
            self.r = [insert_row, update_row]
        def cursor(self):
            return self
        def execute(self, q, p=None):
            pass
        def fetchone(self):
            return self.r.pop(0) if self.r else None
        def commit(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    mod = types.ModuleType("db.connection")
    mod.get_db = lambda: _C()
    monkeypatch.setitem(sys.modules, "db.connection", mod)


def test_claim_won_returns_token(monkeypatch):
    _stub_db(monkeypatch, insert_row=("t",))          # INSERT RETURNING 有行 → 赢
    tok = sc.claim_job_run("j", sc._bucket(3600))
    assert isinstance(tok, str) and tok


def test_claim_reclaims_stale_when_reclaimable(monkeypatch):
    # [FF10] 仅 reclaimable=True(天然幂等/协作式)才自动接管死 claim
    _stub_db(monkeypatch, insert_row=None, update_row=("t",))   # 冲突 → reclaim 死 claim 成功
    assert sc.claim_job_run("j", sc._bucket(3600), reclaimable=True) is not None


def test_claim_money_stale_not_reclaimed_flags_manual(monkeypatch):
    """[FF10 P0-1] 默认 reclaimable=False(money/副作用):死 claim **不复活**,转 retry_pending 返 None。"""
    # 冲突 → flag UPDATE 命中死 claim(RETURNING id)→ 但 claim_job_run 仍返 None(不给 token,不执行)
    _stub_db(monkeypatch, insert_row=None, update_row=(999,))
    assert sc.claim_job_run("j", sc._bucket(3600)) is None       # 默认 reclaimable=False


def test_claim_held_returns_none(monkeypatch):
    _stub_db(monkeypatch, insert_row=None, update_row=None)     # 冲突 + 活跃 claim 被他人持有 → skip
    assert sc.claim_job_run("j", sc._bucket(3600)) is None
    # reclaimable=True 且非死 claim(update 无命中)同样 skip
    _stub_db(monkeypatch, insert_row=None, update_row=None)
    assert sc.claim_job_run("j", sc._bucket(3600), reclaimable=True) is None


def test_heartbeat_returns_hold_status(monkeypatch):
    """[FF10 P0-1] heartbeat CAS 返回持有状态:True 持有 / False 确定丢失 / None DB 抖动。"""
    class _C:
        def __init__(self, rc):
            self.rowcount = rc
        def cursor(self):
            return self
        def execute(self, q, p=None):
            pass
        def commit(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _mk(rc):
        mod = types.ModuleType("db.connection")
        mod.get_db = lambda: _C(rc)
        monkeypatch.setitem(sys.modules, "db.connection", mod)

    _mk(1)
    assert sc.heartbeat_job_run("j", sc._bucket(3600), "TOK") is True
    _mk(0)
    assert sc.heartbeat_job_run("j", sc._bucket(3600), "TOK") is False    # 确定丢租
    modb = types.ModuleType("db.connection")
    def boom():
        raise RuntimeError("db blip")
    modb.get_db = boom
    monkeypatch.setitem(sys.modules, "db.connection", modb)
    assert sc.heartbeat_job_run("j", sc._bucket(3600), "TOK") is None     # 抖动不判丢租


def test_stale_claim_no_second_execution(monkeypatch):
    """[FF10 P0-1 判别] 死 claim(默认 reclaimable=False)→ claim 返 None → body **零次**执行(旧执行体不复活)。"""
    _stub_db(monkeypatch, insert_row=None, update_row=(999,))   # INSERT 冲突 + flag 死 claim 命中
    runs = {"n": 0}

    @sc.sched_claim("moneyjob", 300)   # 默认 reclaimable=False
    def body():
        runs["n"] += 1

    assert body() is None
    assert runs["n"] == 0              # 死 claim 未复活 · 第二次副作用零次


def test_lease_lost_default_false_outside_job():
    assert sc.lease_lost() is False    # 无活动 lease(未经 sched_claim 包裹)默认放行


def test_claim_fail_closed_default_and_fail_open(monkeypatch):
    mod = types.ModuleType("db.connection")
    def boom():
        raise RuntimeError("db down / pool exhausted")
    mod.get_db = boom
    monkeypatch.setitem(sys.modules, "db.connection", mod)
    # 默认 fail-CLOSED:claim 异常 → None(money/高风险不双跑)
    assert sc.claim_job_run("j", sc._bucket(3600)) is None
    # 显式 fail_open:放行(返回 token)
    assert sc.claim_job_run("j", sc._bucket(3600), fail_open=True) is not None


# ---------------- 装饰器流(monkeypatch claim/finish/heartbeat)----------------

def test_decorator_skips_when_claim_none(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(sc, "claim_job_run", lambda *a, **k: None)
    monkeypatch.setattr(sc, "heartbeat_job_run", lambda *a, **k: None)
    monkeypatch.setattr(sc, "finish_job_run", lambda *a, **k: calls.__setitem__("finish", 1))

    @sc.sched_claim("j", 3600)
    def body():
        calls["n"] += 1

    assert body() is None
    assert calls["n"] == 0 and "finish" not in calls


def test_decorator_runs_and_finishes_with_token(monkeypatch):
    seen = {}
    monkeypatch.setattr(sc, "claim_job_run", lambda *a, **k: "TOK")
    monkeypatch.setattr(sc, "heartbeat_job_run", lambda *a, **k: None)
    monkeypatch.setattr(sc, "finish_job_run",
                        lambda jn, sa, tok, status="done", last_error=None: seen.update(tok=tok, status=status))

    @sc.sched_claim("j", 3600)
    def body():
        return "ok"

    assert body() == "ok"
    assert seen["tok"] == "TOK" and seen["status"] == "done"    # finish CAS 用本次 token


def test_async_job_claim_awaits_and_finishes(monkeypatch):
    """[FF5] async job:返回 async wrapper · await 真正业务后才 finish。"""
    import asyncio
    seen = {}
    ran = {"n": 0}
    monkeypatch.setattr(sc, "claim_job_run", lambda *a, **k: "TOK")
    monkeypatch.setattr(sc, "heartbeat_job_run", lambda *a, **k: None)
    monkeypatch.setattr(sc, "finish_job_run",
                        lambda jn, sa, tok, status="done", last_error=None: seen.update(tok=tok, status=status))

    @sc.sched_claim("aj", 3600)
    async def abody():
        ran["n"] += 1
        return "async-ok"

    assert asyncio.iscoroutinefunction(abody)               # 仍是 async(BackgroundScheduler/_run_async 可驱动)
    assert asyncio.run(abody()) == "async-ok"
    assert ran["n"] == 1 and seen["status"] == "done" and seen["tok"] == "TOK"


def test_async_job_skips_when_claim_none(monkeypatch):
    import asyncio
    ran = {"n": 0}
    monkeypatch.setattr(sc, "claim_job_run", lambda *a, **k: None)
    monkeypatch.setattr(sc, "finish_job_run", lambda *a, **k: None)

    @sc.sched_claim("aj", 3600)
    async def abody():
        ran["n"] += 1

    assert asyncio.run(abody()) is None and ran["n"] == 0


def test_scheduler_sync_callable_awaits_async_result_and_propagates_error():
    """APScheduler thread jobs must finish the coroutine, not return it."""
    import asyncio
    import inspect

    calls = []

    async def ok_job():
        calls.append("ran")
        return "done"

    wrapped = sc.scheduler_sync_callable(ok_job)
    assert not inspect.iscoroutinefunction(wrapped)
    assert wrapped() == "done"
    assert calls == ["ran"]

    async def bad_job():
        raise RuntimeError("async job failed")

    with pytest.raises(RuntimeError, match="async job failed"):
        sc.scheduler_sync_callable(bad_job)()


def test_decorator_failed_status_reraises(monkeypatch):
    seen = {}
    monkeypatch.setattr(sc, "claim_job_run", lambda *a, **k: "TOK")
    monkeypatch.setattr(sc, "heartbeat_job_run", lambda *a, **k: None)
    monkeypatch.setattr(sc, "finish_job_run",
                        lambda jn, sa, tok, status="done", last_error=None: seen.update(status=status, err=last_error))

    @sc.sched_claim("j", 3600)
    def body():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        body()
    assert seen["status"] == "failed" and "boom" in (seen["err"] or "")
