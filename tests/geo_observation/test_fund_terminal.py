"""R1/R4 付费诊断终态矩阵:committed/exempt 晋升;released/withheld/manual/repair/refund/缺产物/NULL可见性/品牌不符 不晋升。

必给证据 #2:withheld/退款/缺产物零晋升。
"""
from __future__ import annotations

import asyncio

from db.connection import get_connection
from services.brand_identity_resolver import BrandVerdict
from services.geo_observation import promotion, source_hooks
from services.geo_observation.promotion import DecisionContext

import _helpers as H

APPROVED = DecisionContext(True, "legal_v1", "consent_v1", None, outcome_gold_gate_passed=True)
VNO = H.const_verifier(BrandVerdict.NO)


def _reg(run_token, **kw):
    conn = get_connection(); cur = conn.cursor()
    if kw.pop("seed_brand", True):
        H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, run_token, owner=124, brand=501, **kw)
    res = source_hooks.register_paid_diagnosis(cur, run_token, H.now())
    conn.commit(); conn.close()
    return res


def _states(run_token):
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT processing_state FROM geo_observation_events WHERE source_record_id=%s", (run_token,))
    out = [r["processing_state"] for r in cur.fetchall()]; conn.close()
    return out


def _promote_all():
    states = []
    while True:
        r = asyncio.run(promotion.process_next_pending(ctx=APPROVED, verifier=VNO, outcome_classifier=H.const_outcome()))
        if r is None:
            break
        states.append(r["state"])
    return states


def _signal_count():
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_signals")
    n = cur.fetchone()["n"]; conn.close()
    return n


# ── 可登记且可晋升 ──
def test_committed_published_promotes():
    _reg("r_c", run_status="committed", visibility="published", answer="推荐大昀装修")
    assert _promote_all() == ["promoted"]


def test_completed_exempt_promotes():
    _reg("r_e", run_status="completed_exempt", visibility="published", answer="推荐大昀装修")
    assert _promote_all() == ["promoted"]


# ── 不可登记(register 返回 [] · 零 event)──
def test_released_not_registerable():
    assert _reg("r_rel", run_status="released", visibility="withheld") == []
    assert _states("r_rel") == []


def test_settlement_manual_not_registerable():
    assert _reg("r_sm", run_status="settlement_manual") == []


def test_manual_resolving_not_registerable():
    assert _reg("r_mr", run_status="manual_resolving") == []


def test_delivery_repair_pending_not_registerable():
    assert _reg("r_dr", run_status="delivery_repair_pending") == []


def test_refunded_not_registerable():
    assert _reg("r_rf", run_status="committed", visibility="published", refund=True) == []


# ── 可登记但不可晋升 → private_only(证明不全绝不晋升)──
def test_null_visibility_private_only():
    # 严格:NULL 可见性不自动晋升(R1)
    _reg("r_nv", run_status="committed", visibility=None, answer="推荐大昀装修")
    assert _promote_all() == ["private_only"]
    assert _signal_count() == 0


def test_withheld_visibility_private_only():
    _reg("r_wh", run_status="committed", visibility="withheld", answer="推荐大昀装修")
    assert _promote_all() == ["private_only"]
    assert _signal_count() == 0


def test_empty_shell_product_private_only():
    _reg("r_es", run_status="committed", visibility="published", total_score=None, answer="推荐大昀装修")
    assert _promote_all() == ["private_only"]
    assert _signal_count() == 0


def test_brand_mismatch_private_only():
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "r_bm", owner=124, brand=501, run_status="committed", visibility="published")
    # 篡改 record.brand_id 与 run.brand_id 不符
    cur.execute("UPDATE diagnosis_records SET brand_id=999 WHERE run_token='r_bm'")
    conn.commit()
    source_hooks.register_paid_diagnosis(cur, "r_bm", H.now()); conn.commit(); conn.close()
    assert _promote_all() == ["private_only"]
    assert _signal_count() == 0
