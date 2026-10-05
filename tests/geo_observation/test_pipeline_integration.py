"""End-to-end pipeline: register → promote/private_only, signal privacy, HMAC bucket."""
from __future__ import annotations

import asyncio

from db.connection import get_connection
from services.brand_identity_resolver import BrandVerdict
from services.geo_observation import promotion, source_hooks
from services.geo_observation.promotion import DecisionContext

import _helpers as H

APPROVED = DecisionContext(promotion_enabled=True, promotion_legal_basis="legal_v1_approved",
                           consent_policy_version="consent_v1", retention_until=None, outcome_gold_gate_passed=True)
NO_LEGAL = DecisionContext(promotion_enabled=True, promotion_legal_basis=None,
                           consent_policy_version="consent_v1", retention_until=None, outcome_gold_gate_passed=True)


def _register_diag(run_token, **kw):
    conn = get_connection()
    cur = conn.cursor()
    H.seed_brand(conn, kw.pop("brand", 501), kw.pop("brand_name", "大昀装修"), kw.pop("owner", 124))
    H.seed_diagnosis(conn, run_token, owner=124, brand=501, **kw)
    res = source_hooks.register_paid_diagnosis(cur, run_token, H.now())
    conn.commit()
    conn.close()
    return res


def _event_state(run_token):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT processing_state, promotion_legal_basis FROM geo_observation_events WHERE source_record_id=%s", (run_token,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def test_paid_diagnosis_committed_promotes_with_clean_signal():
    _register_diag("run_ok", run_status="committed", visibility="published",
                   answer="强烈推荐大昀装修,口碑好、值得选择。")
    # inject NO verifier to prove trusted_exact (brand name in answer) bypasses LLM;gold-gate 通过 + 注入 outcome
    res = asyncio.run(promotion.process_next_pending(ctx=APPROVED, verifier=H.const_verifier(BrandVerdict.NO),
                                                     outcome_classifier=H.const_outcome("recommended")))
    assert res["state"] == "promoted", res
    assert res["won_vote"] is True and res["signal_inserted"] is True

    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM geo_observation_signals")
    sig = dict(cur.fetchone())
    # 隐私:signal 无 owner/brand 列(schema 保证);outcome=recommended;effective=base(赢票)
    assert "owner_user_id" not in sig and "brand_id" not in sig
    assert sig["target_outcome"] == "recommended"
    assert sig["effective_weight_bps"] == sig["base_weight_bps"] == 4000
    # 桶存在且不泄露到 signal
    cur.execute("SELECT count(*) AS n FROM geo_observation_contributor_buckets")
    assert cur.fetchone()["n"] == 1
    # 审计
    cur.execute("SELECT count(*) AS n FROM geo_observation_audit WHERE action='promote'")
    assert cur.fetchone()["n"] == 1
    conn.close()


def test_no_legal_basis_forces_private_only():
    _register_diag("run_nolegal", run_status="committed", visibility="published")
    res = asyncio.run(promotion.process_next_pending(ctx=NO_LEGAL, verifier=H.const_verifier(BrandVerdict.YES,
                        matched_text=None)))
    assert res["state"] == "private_only", res
    assert "legal_basis_not_approved" in res["reasons"]
    rows = _event_state("run_nolegal")
    assert all(r["processing_state"] == "private_only" for r in rows)
    # 零 signal
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_signals")
    assert cur.fetchone()["n"] == 0
    conn.close()


def test_refunded_run_not_registerable():
    res = _register_diag("run_refunded", run_status="committed", visibility="published", refund=True)
    assert res == []   # refund present → not registerable
    rows = _event_state("run_refunded")
    assert rows == []
