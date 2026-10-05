"""Round-1 审核 CONFIRMED 修复的判别测试(每条对应一个 finding)。"""
from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.connection import get_connection
from services.brand_identity_resolver import BrandVerdict
from services.geo_observation import privacy, promotion, reconciler, source_hooks
from services.geo_observation.promotion import DecisionContext
from services.geo_observation.source_hooks import _extract_diagnosis_observations

import _helpers as H

APPROVED = DecisionContext(True, "legal_v1", "consent_v1", None, outcome_gold_gate_passed=True)
NO_LEGAL = DecisionContext(True, None, "consent_v1", None, outcome_gold_gate_passed=True)
VNO = H.const_verifier(BrandVerdict.NO)
LONG = "大昀装修在深圳口碑非常好,服务专业,施工质量稳定可靠,非常值得推荐给有装修需求的业主优先考虑选择。"


def _promote_all(ctx=APPROVED):
    out = []
    while True:
        r = asyncio.run(promotion.process_next_pending(ctx=ctx, verifier=VNO, outcome_classifier=H.const_outcome()))
        if r is None:
            break
        out.append(r)
    return out


def _agg_eligible():
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_signals s JOIN geo_observation_events e "
                "ON e.id=s.event_id WHERE e.processing_state='promoted'")
    n = cur.fetchone()["n"]; conn.close(); return n


# ── #7 CRITICAL:research 非晋升决策不再崩溃(entity_state None) ──
def test_research_no_legal_basis_private_only_no_crash():
    conn = get_connection()
    rid = H.seed_research(conn, 1, answer="推荐以下几家:大昀装修、xx。", round_status="completed")
    conn.close()
    src_conn = get_connection(); cur = src_conn.cursor()
    source_hooks.register_research_raw(cur, rid); src_conn.commit(); src_conn.close()
    r = asyncio.run(promotion.process_next_pending(ctx=NO_LEGAL, verifier=VNO, outcome_classifier=H.const_outcome()))
    assert r["state"] == "private_only"   # 之前:AttributeError → stuck processing


# ── #5 HIGH:paid_diagnosis NULL brand_id → private_only,零信号/零桶 ──
def test_paid_diagnosis_null_brand_private_only():
    conn = get_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO diagnosis_runs(run_token,session_id,owner_user_id,brand_id,billing_mode,run_status,"
                "status_changed_at,finished_at) VALUES ('r_nb','s_nb',124,NULL,'paid','committed',NOW(),NOW())")
    cur.execute("INSERT INTO diagnosis_records(session_id,brand_id,run_token,result_visibility,total_score,raw_data_json)"
                " VALUES ('s_nb',NULL,'r_nb','published',72,%s)",
                (H.diag_raw_json("深圳装修哪家好", "deepseek", "本地有很多装修公司可选,建议多对比资质口碑与真实案例后再决定。"),))
    source_hooks.register_paid_diagnosis(cur, "r_nb", H.now()); conn.commit(); conn.close()
    states = [r["state"] for r in _promote_all()]
    assert states and all(s == "private_only" for s in states)
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_signals"); assert cur.fetchone()["n"] == 0
    cur.execute("SELECT count(*) AS n FROM geo_observation_contributor_buckets"); assert cur.fetchone()["n"] == 0
    conn.close()


# ── #8 HIGH:raw_data_json 顶层非 dict / 奇形不崩溃 ──
def test_raw_data_json_non_dict_no_crash():
    for blob in ('["q1","q2"]', "123", '"a string"', "true", "null", "[]", "not json at all", "{}"):
        assert _extract_diagnosis_observations(blob) == []
    # #3 真实形状能抽取
    real = H.diag_raw_json("深圳装修哪家好", "deepseek", LONG)
    obs = _extract_diagnosis_observations(real)
    assert len(obs) == 1 and obs[0]["engine"] == "deepseek" and obs[0]["answer"] == LONG
    # 引擎失败样本不产观测
    fail = H.diag_raw_json("q", "deepseek", "查询失败:超时")
    assert _extract_diagnosis_observations(fail) == []


# ── #4 MEDIUM:family_key 绝不含可读品牌/竞品(哈希) ──
def test_family_key_no_readable_brand_or_competitor():
    k_research = privacy.anonymize_prompt_family_key("大昀装修 对比 富士竞品牌 哪个好", [], "装修")
    assert "大昀装修" not in k_research and "富士竞品牌" not in k_research
    k_diag = privacy.anonymize_prompt_family_key("大昀装修和某某竞争对手比较", ["大昀装修"], "装修")
    assert "大昀装修" not in k_diag and "某某竞争对手" not in k_diag
    assert k_diag.startswith("装修:")


# ── #1 MEDIUM:admin requeue 不复活已撤回 event ──
def _api_client():
    app = FastAPI()
    from api.geo_observation_admin_api import router
    @app.middleware("http")
    async def _inj(request, call_next):
        request.state.user = {"is_admin": True, "username": "admin1"}
        return await call_next(request)
    app.include_router(router)
    return TestClient(app)


def test_admin_requeue_refuses_withdrawn_event():
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "r_wq", owner=124, brand=501, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r_wq", H.now()); conn.commit(); conn.close()
    _promote_all()
    assert _agg_eligible() == 1
    # 撤回(注销/抹除,源未退款)
    conn = get_connection(); cur = conn.cursor()
    from services.geo_observation import repository
    cur.execute("SELECT id FROM geo_observation_events WHERE source_record_id='r_wq'")
    eid = cur.fetchone()["id"]
    repository.withdraw_event(cur, eid, reason_codes=["user_erasure"]); conn.commit(); conn.close()
    assert _agg_eligible() == 0
    # admin requeue → 409,且不复活
    c = _api_client()
    r = c.post(f"/api/admin/geo-observation/events/{eid}/review",
               json={"decision": "requeue", "reason": "试图复活", "request_id": "rq-x"})
    assert r.status_code == 409
    _promote_all()  # 即便再跑 worker,也不应复活
    assert _agg_eligible() == 0


# ── #9 MEDIUM:晋升后可见性降级 → reconciler 撤回 → 聚合归零 ──
def test_visibility_downgrade_reconciler_withdraws():
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "r_dg", owner=124, brand=501, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r_dg", H.now()); conn.commit(); conn.close()
    _promote_all()
    assert _agg_eligible() == 1
    # 事后降级(run 仍 committed,无退款)
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE diagnosis_records SET result_visibility='withheld' WHERE run_token='r_dg'")
    conn.commit(); conn.close()
    out = reconciler.run_reconciler()
    assert out["withdrawals"]["withdrawn"] >= 1
    assert _agg_eligible() == 0


# ── Round-2 回归:family_key 哈希尾段含 11 位数字串不得误触 _PHONE 致晋升事务崩溃卡死 ──
def test_family_key_hash_phone_run_no_crash():
    import pytest as _pt
    from services.geo_observation.privacy import _PHONE
    fk = "装修:comparison:13703088407ee1a9"     # hex 尾段含 phone-run
    assert _PHONE.search(fk)                      # 前提:确含 phone-run
    privacy.assert_signal_clean({"prompt_family_key": fk, "source_domains": [], "search_query_theme_keys": []})  # 不 raise
    with _pt.raises(ValueError):                  # 可读前缀真含手机号仍 raise
        privacy.assert_signal_clean({"prompt_family_key": "深圳13800138000装修:x:abcdef0123456789"})
    # 端到端:该触发问题(其 family_key 哈希含 phone-run)promotion 干净终态,不卡 processing
    q = "深圳装修哪家好排名对比评测问题编号7873"
    assert _PHONE.search(privacy.anonymize_prompt_family_key(q, [], "装修"))
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "r_hp", owner=124, brand=501, run_status="committed", visibility="published",
                     question=q, engine="deepseek", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r_hp", H.now()); conn.commit(); conn.close()
    assert [r["state"] for r in _promote_all()] == ["promoted"]
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_events WHERE processing_state='processing'")
    assert cur.fetchone()["n"] == 0; conn.close()


# ── #2 LOW:多条 diagnosis_records/run_token 时 gate 与抽取绑定同一(最新)记录 ──
def test_multi_record_binds_latest_deterministically():
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    cur.execute("INSERT INTO diagnosis_runs(run_token,session_id,owner_user_id,brand_id,billing_mode,run_status,"
                "status_changed_at,finished_at) VALUES ('r_mr','s_mr',124,501,'paid','committed',NOW(),NOW())")
    # 旧记录:未发布空壳;新记录(id 更大):已发布完整 —— gate+抽取都应绑定新记录
    cur.execute("INSERT INTO diagnosis_records(session_id,brand_id,run_token,result_visibility,total_score,raw_data_json)"
                " VALUES ('s_mr_old',501,'r_mr',NULL,NULL,%s)", (H.diag_raw_json("旧问题","deepseek","旧答案短"),))
    cur.execute("INSERT INTO diagnosis_records(session_id,brand_id,run_token,result_visibility,total_score,raw_data_json)"
                " VALUES ('s_mr_new',501,'r_mr','published',72,%s)", (H.diag_raw_json("深圳装修哪家好","deepseek",LONG),))
    source_hooks.register_paid_diagnosis(cur, "r_mr", H.now()); conn.commit(); conn.close()
    # 绑定新记录(published+完整)→ 可晋升
    assert [r["state"] for r in _promote_all()] == ["promoted"]
