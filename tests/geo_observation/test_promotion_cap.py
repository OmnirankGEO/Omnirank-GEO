"""R6 防刷唯一门(user+brand+family+platform+date,不含 source_type)。

必给证据 #4:跨三来源同客户同题同日只计一票 —— 诊断 + 监测(同 owner/brand/题族/平台/日)只 1 桶。
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
Q = "深圳装修公司推荐排名"   # 诊断与监测用同一问题 → 同 prompt_family_key
LONG = "大昀装修在深圳的口碑非常好,服务态度专业,施工质量稳定可靠,非常值得推荐给有装修需求的业主优先考虑选择。"


def _promote_all():
    out = []
    while True:
        r = asyncio.run(promotion.process_next_pending(ctx=APPROVED, verifier=VNO, outcome_classifier=H.const_outcome()))
        if r is None:
            break
        out.append(r)
    return out


def test_cross_source_same_customer_topic_day_one_vote():
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    # 诊断观测:owner 124 / brand 501 / platform deepseek / question Q
    H.seed_diagnosis(conn, "run_cap", owner=124, brand=501, run_status="committed", visibility="published",
                     question=Q, engine="deepseek", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "run_cap", H.now())
    # 监测观测:同 owner 124 / brand 501 / platform deepseek / 购买短句=Q
    rid = H.seed_monitoring(conn, None, brand=501, owner=124, keyword="深圳装修", monitoring_query=Q,
                            platform="deepseek", answer=LONG, status="completed", total_tests=4, completed_tests=4,
                            quote_id="Qcap")
    source_hooks.register_monitoring_result(cur, rid)
    conn.commit(); conn.close()

    results = _promote_all()
    assert all(r["state"] == "promoted" for r in results), results
    assert len(results) == 2

    conn = get_connection(); cur = conn.cursor()
    # 只 1 桶(跨 source_type 的 5 元组唯一门)
    cur.execute("SELECT count(*) AS n FROM geo_observation_contributor_buckets")
    assert cur.fetchone()["n"] == 1, "同客户同题同日跨来源应只 1 票"
    # 2 signal:一条 effective=base(赢票),一条 effective=0(稳定度样本)
    cur.execute("SELECT effective_weight_bps, base_weight_bps FROM geo_observation_signals ORDER BY effective_weight_bps DESC")
    rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0]["effective_weight_bps"] > 0 and rows[1]["effective_weight_bps"] == 0
    # 桶的 family_key 一致(证明确定性同题)
    cur.execute("SELECT DISTINCT prompt_family_key FROM geo_observation_signals")
    assert len({r["prompt_family_key"] for r in cur.fetchall()}) == 1
    conn.close()


def test_different_day_two_votes():
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "run_d1", owner=124, brand=501, run_status="committed", visibility="published",
                     question=Q, engine="deepseek", answer=LONG)
    H.seed_diagnosis(conn, "run_d2", owner=124, brand=501, run_status="committed", visibility="published",
                     question=Q, engine="deepseek", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "run_d1", H.now())
    source_hooks.register_paid_diagnosis(cur, "run_d2", H.now())
    conn.commit()
    # 把 run_d2 的 event observed_at 挪到昨天 → 不同 contribution_date → 两票
    cur.execute("UPDATE geo_observation_events SET observed_at = observed_at - interval '1 day' WHERE source_record_id='run_d2'")
    conn.commit(); conn.close()
    results = _promote_all()
    assert all(r["state"] == "promoted" for r in results)
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_contributor_buckets")
    assert cur.fetchone()["n"] == 2   # 不同日 → 两票
    conn.close()
