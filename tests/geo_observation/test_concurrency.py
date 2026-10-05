"""并发与恢复:同源 20 并发恰一 event、20 worker 恰一晋升、claim 无双领、kill-9 lease 恢复+旧 worker 零写。

Redis 不在正确性路径(repository/promotion 不 import redis)→ Redis down 不改变 DB 收敛。
"""
from __future__ import annotations

import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from db.connection import get_connection
from services.brand_identity_resolver import BrandVerdict
from services.geo_observation import promotion, repository, source_hooks
from services.geo_observation.promotion import DecisionContext

import _helpers as H

APPROVED = DecisionContext(True, "legal_v1", "consent_v1", None, outcome_gold_gate_passed=True)
VNO = H.const_verifier(BrandVerdict.NO)
LONG = "大昀装修在深圳口碑非常好,服务专业,施工质量稳定可靠,非常值得推荐给有装修需求的业主优先考虑选择。"


def _fields(subkey="s"):
    return dict(source_type="research_round", source_table="geo_research_raw", source_record_id="42",
                source_subkey=subkey, platform_key="deepseek", provider_key="deepseek", model_key="m",
                surface_key="deepseek_native_with_search", session_mode="clean",
                observed_at=datetime.now(timezone.utc), industry_key="装修")


def test_same_source_20_concurrent_exactly_one_event():
    def _reg(_):
        try:
            _, created = repository.register_event_standalone(_fields())
            return created
        except Exception:  # noqa: BLE001
            return None
    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(_reg, range(20)))
    assert sum(1 for r in results if r) == 1     # 恰一次 created=True
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_events WHERE source_record_id='42'")
    assert cur.fetchone()["n"] == 1
    conn.close()


def test_claim_no_double_claim():
    # 5 pending events
    for i in range(5):
        repository.register_event_standalone(_fields(subkey=f"k{i}"))
    claimed = []
    def _claim(_):
        conn = get_connection(); cur = conn.cursor()
        row = repository.claim_next_event(cur); conn.commit(); conn.close()
        return row["id"] if row else None
    with ThreadPoolExecutor(max_workers=20) as ex:
        ids = [x for x in ex.map(_claim, range(20)) if x is not None]
    assert len(ids) == 5 and len(set(ids)) == 5   # 每 event 恰领一次,无双领


def test_20_workers_exactly_one_promotion():
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "run_conc", owner=124, brand=501, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "run_conc", H.now())
    conn.commit(); conn.close()

    def _worker(_):
        r = asyncio.run(promotion.process_next_pending(ctx=APPROVED, verifier=VNO, outcome_classifier=H.const_outcome()))
        return r["state"] if r else None
    with ThreadPoolExecutor(max_workers=20) as ex:
        states = list(ex.map(_worker, range(20)))
    assert sum(1 for s in states if s == "promoted") == 1   # 恰一晋升
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_signals")
    assert cur.fetchone()["n"] == 1
    conn.close()


def test_kill9_lease_recovery_and_stale_worker_zero_write():
    repository.register_event_standalone(_fields(subkey="lease"))
    conn = get_connection(); cur = conn.cursor()
    claimed = repository.claim_next_event(cur); conn.commit()
    old_token = claimed["lease_token"]; eid = claimed["id"]
    # 模拟 worker kill-9:不 finish,直接过期 lease
    cur.execute("UPDATE geo_observation_events SET lease_until=NOW() - interval '1 hour' WHERE id=%s", (eid,))
    conn.commit()
    # 新 worker 重新领取(kill-9 恢复)
    reclaimed = repository.claim_next_event(cur); conn.commit()
    assert reclaimed["id"] == eid and reclaimed["lease_token"] != old_token
    # 旧 worker 复活用旧 token finish → 零写
    assert repository.finish_event(cur, eid, old_token, "promoted") is False
    # 新 worker 用新 token finish → 成功
    assert repository.finish_event(cur, eid, reclaimed["lease_token"], "private_only") is True
    conn.commit(); conn.close()


def test_no_redis_on_correctness_path():
    for mod in ("services.geo_observation.repository", "services.geo_observation.promotion",
                "services.geo_observation.source_hooks"):
        m = importlib.import_module(mod)
        src = open(m.__file__, encoding="utf-8").read()
        assert "import redis" not in src and "redis_client" not in src, f"{mod} 不应依赖 Redis 保证正确性"
