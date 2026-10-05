"""Reconciler:补登记漏挂终态源 + 撤回(退款/阻断)后聚合贡献归零。

必给证据 #6:撤回后聚合贡献归零(signals 不可变,但 join event 只取 promoted → 0)。
"""
from __future__ import annotations

import asyncio

from db.connection import get_connection
from services.brand_identity_resolver import BrandVerdict
from services.geo_observation import promotion, reconciler, source_hooks
from services.geo_observation.promotion import DecisionContext

import _helpers as H

APPROVED = DecisionContext(True, "legal_v1", "consent_v1", None, outcome_gold_gate_passed=True)
VNO = H.const_verifier(BrandVerdict.NO)
LONG = "大昀装修在深圳口碑非常好,服务专业,施工质量稳定可靠,非常值得推荐给有装修需求的业主优先考虑选择。"


def _agg_eligible_count():
    """模拟 AI-3 聚合资格:join event 只取 promoted。"""
    conn = get_connection(); cur = conn.cursor()
    cur.execute(
        "SELECT count(*) AS n FROM geo_observation_signals s "
        "JOIN geo_observation_events e ON e.id = s.event_id WHERE e.processing_state='promoted'"
    )
    n = cur.fetchone()["n"]; conn.close()
    return n


def _promote_all():
    while asyncio.run(promotion.process_next_pending(ctx=APPROVED, verifier=VNO, outcome_classifier=H.const_outcome())) is not None:
        pass


def test_reconcile_registration_backfills_missing_events():
    conn = get_connection()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "run_missing", owner=124, brand=501, run_status="committed", visibility="published",
                     answer=LONG)
    conn.close()
    # 无 event(未接线 hook)→ reconciler 补登记
    out = reconciler.run_reconciler()
    assert out["registration"]["paid_diagnosis"] >= 1
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_events WHERE source_record_id='run_missing'")
    assert cur.fetchone()["n"] >= 1
    # 幂等:再跑不重复登记
    reconciler.run_reconciler()
    cur.execute("SELECT count(*) AS n FROM geo_observation_events WHERE source_record_id='run_missing'")
    before = cur.fetchone()["n"]
    reconciler.run_reconciler()
    cur.execute("SELECT count(*) AS n FROM geo_observation_events WHERE source_record_id='run_missing'")
    assert cur.fetchone()["n"] == before
    conn.close()


def test_ingest_gate_skips_new_registration_but_keeps_maintenance():
    conn = get_connection()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(
        conn,
        "run_ingest_off",
        owner=124,
        brand=501,
        run_status="committed",
        visibility="published",
        answer=LONG,
    )
    conn.close()

    out = reconciler.run_reconciler(registration_enabled=False)
    assert out["registration"]["enabled"] is False
    assert out["withdrawals"]["withdrawn"] == 0
    assert "anonymized" in out["retention"]

    conn = get_connection(); cur = conn.cursor()
    cur.execute(
        "SELECT count(*) AS n FROM geo_observation_events "
        "WHERE source_record_id='run_ingest_off'"
    )
    assert cur.fetchone()["n"] == 0
    conn.close()


def test_refund_withdraws_and_zeroes_aggregate_contribution():
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "run_wd", owner=124, brand=501, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "run_wd", H.now())
    conn.commit(); conn.close()
    _promote_all()
    assert _agg_eligible_count() == 1     # 晋升后可聚合

    # 后来退款
    conn = get_connection(); cur = conn.cursor()
    cur.execute(
        "INSERT INTO diagnosis_refund_records(run_token,freeze_task_ref,freeze_id,freeze_backend,owner_user_id,"
        "points,refund_tx_ref,operator) VALUES ('run_wd','diag_run_wd',1,'legacy',124,100,'tx1','ops')")
    conn.commit(); conn.close()

    out = reconciler.run_reconciler()
    assert out["withdrawals"]["withdrawn"] >= 1

    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT processing_state, withdrawn_at FROM geo_observation_events WHERE source_record_id='run_wd'")
    ev = cur.fetchone()
    assert ev["processing_state"] == "withdrawn" and ev["withdrawn_at"] is not None
    # signals 不可变(仍在)
    cur.execute("SELECT count(*) AS n FROM geo_observation_signals")
    assert cur.fetchone()["n"] == 1
    conn.close()
    # 证据 #6:聚合贡献归零
    assert _agg_eligible_count() == 0

    # 撤回幂等
    out2 = reconciler.run_reconciler()
    assert out2["withdrawals"]["withdrawn"] == 0


def test_blocked_state_withdraws():
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "run_blk", owner=124, brand=501, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "run_blk", H.now())
    conn.commit(); conn.close()
    _promote_all()
    # run 后来转 released(资金退回)
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE diagnosis_runs SET run_status='released', status_changed_at=NOW() WHERE run_token='run_blk'")
    conn.commit(); conn.close()
    reconciler.run_reconciler()
    assert _agg_eligible_count() == 0
