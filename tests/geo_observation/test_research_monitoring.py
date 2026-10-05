"""R2 公共调研(仅 completed;partial_success→零公共信号)+ R3 持续监测(证明不全→private_only,绝不 not_mentioned)。

必给证据 #1:partial_success 零公共信号。 #3:监测部分失败不生成 not_mentioned。
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


def _reg_research(raw_id_from_seed):
    conn = get_connection(); cur = conn.cursor()
    res = source_hooks.register_research_raw(cur, raw_id_from_seed)
    conn.commit(); conn.close()
    return res


def _reg_monitoring(rid):
    conn = get_connection(); cur = conn.cursor()
    res = source_hooks.register_monitoring_result(cur, rid)
    conn.commit(); conn.close()
    return res


def _promote_all():
    out = []
    while True:
        r = asyncio.run(promotion.process_next_pending(ctx=APPROVED, verifier=VNO, outcome_classifier=H.const_outcome()))
        if r is None:
            break
        out.append(r)
    return out


def _signals():
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM geo_observation_signals")
    rows = [dict(r) for r in cur.fetchall()]; conn.close()
    return rows


# ── R2 调研 ──
def test_research_completed_promotes():
    conn = get_connection()
    rid = H.seed_research(conn, 1, answer="推荐以下几家:大昀装修、xx装修。", round_status="completed")
    conn.close()
    assert _reg_research(rid) is not None
    res = _promote_all()
    assert [r["state"] for r in res] == ["promoted"]
    sigs = _signals()
    assert len(sigs) == 1
    # 调研满权重(base=effective=10000),无 contributor bucket(公共来源)
    assert sigs[0]["base_weight_bps"] == 10000 and sigs[0]["effective_weight_bps"] == 10000
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_contributor_buckets")
    assert cur.fetchone()["n"] == 0
    conn.close()


def test_research_partial_success_zero_public_signal():
    conn = get_connection()
    rid = H.seed_research(conn, 1, answer="推荐大昀装修。", round_status="partial_success", round_id="round_ps")
    conn.close()
    assert _reg_research(rid) is None            # 不可登记
    assert _promote_all() == []                  # 无 event
    assert _signals() == []                       # 零公共信号(证据 #1)


def test_research_failed_zero_signal():
    conn = get_connection()
    rid = H.seed_research(conn, 1, answer="推荐大昀装修。", round_status="failed", round_id="round_f")
    conn.close()
    assert _reg_research(rid) is None
    assert _signals() == []


# ── R3 监测 ──
def test_monitoring_full_batch_promotes():
    conn = get_connection()
    rid = H.seed_monitoring(conn, None, brand=601, owner=124,
                            answer="大昀装修在深圳口碑非常好,服务态度专业,施工质量可靠稳定,非常值得推荐给所有有装修需求的业主朋友优先考虑选择。",
                            status="completed", total_tests=4, completed_tests=4)
    conn.close()
    assert _reg_monitoring(rid) is not None
    res = _promote_all()
    assert [r["state"] for r in res] == ["promoted"]


def test_monitoring_incomplete_batch_private_only_no_not_mentioned():
    conn = get_connection()
    rid = H.seed_monitoring(conn, None, brand=602, owner=124, answer="大昀装修口碑好,推荐。",
                            status="completed", total_tests=4, completed_tests=2)  # 不完整批次
    conn.close()
    assert _reg_monitoring(rid) is not None       # task completed → 可登记
    res = _promote_all()
    assert [r["state"] for r in res] == ["private_only"]
    sigs = _signals()
    assert sigs == []                              # 零信号
    # 证据 #3:绝不生成 not_mentioned(根本无 signal)
    assert not any(s["target_outcome"] == "not_mentioned" for s in sigs)


def test_monitoring_empty_answer_private_only():
    conn = get_connection()
    rid = H.seed_monitoring(conn, None, brand=603, owner=124, answer="短",  # <50 字
                            status="completed", total_tests=4, completed_tests=4)
    conn.close()
    assert _reg_monitoring(rid) is not None
    assert [r["state"] for r in _promote_all()] == ["private_only"]
    assert _signals() == []


def test_monitoring_keyword_provenance_unresolved_private_only():
    conn = get_connection()
    # 用不同 quote_id 的关键词,使 (client_id, keyword) 找不到购买短句
    rid = H.seed_monitoring(conn, None, brand=604, owner=124,
                            answer="大昀装修在本地口碑不错,施工规范细致,非常值得推荐,建议有装修需求的业主可以多了解一下他们的服务和真实案例情况。",
                            status="completed", total_tests=4, completed_tests=4, quote_id="Q_task")
    cur = conn.cursor()
    cur.execute("UPDATE confirmed_keywords SET quote_id='Q_other'")  # 断开归属
    conn.commit(); conn.close()
    assert _reg_monitoring(rid) is not None
    assert [r["state"] for r in _promote_all()] == ["private_only"]
    assert _signals() == []


def test_monitoring_confirmed_non_mention_yields_not_mentioned():
    """对照:完整批次 + 品牌确实未提及(resolver NO)→ not_mentioned 是合法的(非"部分失败")。"""
    conn = get_connection()
    # 答案不含品牌名 → resolver NO → confirmed_non_mention → not_mentioned
    rid = H.seed_monitoring(conn, None, brand=605, owner=124,
                            answer="深圳本地有很多装修公司可以选择,建议大家多对比几家的资质、口碑和真实案例,根据自己的预算和需求综合考察之后再做决定。",
                            status="completed", total_tests=4, completed_tests=4)
    conn.close()
    assert _reg_monitoring(rid) is not None
    res = _promote_all()
    assert [r["state"] for r in res] == ["promoted"]
    sigs = _signals()
    assert len(sigs) == 1 and sigs[0]["target_outcome"] in ("not_mentioned", "criteria_only")
