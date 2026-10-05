"""Round-2 复审 7 个 P1 修复的判别测试(每条对应一个 P1)。"""
from __future__ import annotations

import asyncio

import pytest

from db.connection import get_connection
from services.brand_identity_resolver import BrandVerdict
from services.geo_observation import policy, promotion, readiness, reconciler, source_hooks
from services.geo_observation.promotion import DecisionContext, process_next_pending

import _helpers as H

VNO = H.const_verifier(BrandVerdict.NO)
LONG = "大昀装修在深圳口碑非常好,服务专业,施工质量稳定可靠,非常值得推荐给有装修需求的业主优先考虑选择。"


def _set_governance(*, promotion_enabled, legal="legal_v1", gold=False):
    conn = get_connection(); cur = conn.cursor()
    snap = policy.get_policy()
    newp = dict(snap["policy"]); newp["feature_flags"] = dict(newp["feature_flags"])
    newp["feature_flags"]["promotion_enabled"] = promotion_enabled
    v = policy.update_policy(cur, expected_version=snap["policy_version"], new_policy=newp,
                             reason="enable", request_id="rqp", operator_id="admin")
    v = policy.update_promotion_governance(cur, expected_version=v, promotion_legal_basis=legal,
                                           consent_policy_version="c1",
                                           reason="approve", request_id="rqg", operator_id="admin")
    if gold:
        # P1-1:金标准门只能由达契约§601 阈值的不可变评估证据派生(不再直接提交布尔值)
        policy.record_gold_evaluation(cur, expected_version=v, dataset_version="gold_ds_v1",
                                      sample_count=120, macro_f1_bps=9300, high_risk_false_reco=0,
                                      report_hash="report_hash_" + "a" * 16,
                                      reason="gold pass", request_id="rqgold", operator_id="admin")
    conn.commit(); conn.close()


# ── P1-1:worker 每次读新鲜 policy 治理(关闸立即生效),不冻结 ctx ──
def test_fresh_policy_governance_takes_effect():
    # 批准晋升 + 法务依据 → 用 research(无品牌/无 classifier)证明 ctx=None 走新鲜 policy
    _set_governance(promotion_enabled=True, legal="legal_v1")
    conn = get_connection()
    rid = H.seed_research(conn, 1, answer="推荐以下几家:大昀装修、xx。", round_status="completed", round_id="r_gov1")
    conn.close()
    c = get_connection(); source_hooks.register_research_raw(c.cursor(), rid); c.commit(); c.close()
    r = asyncio.run(process_next_pending())   # ctx=None → 读新鲜 policy
    assert r["state"] == "promoted"
    # 管理员关闭 promotion_enabled → 立即生效
    _set_governance(promotion_enabled=False, legal="legal_v1")
    conn = get_connection()
    rid2 = H.seed_research(conn, 2, answer="推荐大昀装修。", round_status="completed", round_id="r_gov2")
    conn.close()
    c = get_connection(); source_hooks.register_research_raw(c.cursor(), rid2); c.commit(); c.close()
    r2 = asyncio.run(process_next_pending())
    assert r2["state"] == "private_only" and "promotion_disabled" in r2["reasons"]


# ── P1-2:DeepSeek 血缘诚实(legacy/dashscope,不冒充官方 native)──
def test_deepseek_lineage_honest_legacy():
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "r_lin", owner=124, brand=501, run_status="committed", visibility="published",
                     engine="deepseek", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r_lin", H.now()); conn.commit()
    cur.execute("SELECT provider_key, surface_key, model_revision FROM geo_observation_events WHERE source_record_id='r_lin'")
    row = cur.fetchone(); conn.close()
    assert row["surface_key"] == "deepseek_dashscope_search_legacy"   # 非 native_with_search
    assert row["provider_key"] == "dashscope"                          # 非官方 deepseek
    assert row["model_revision"] is None                               # 历史版本不可证明 → 未知


# ── P1-3:自由文本行业受控字典门(无关键词→private_only;含关键词→映射受控 ID,客户名/项目名绝不入公共层)──
_GOLD_CTX = DecisionContext(True, "legal_v1", "c", None, outcome_gold_gate_passed=True)


def test_unknown_freetext_industry_private_only():
    # 无任何行业关键词的纯客户名/项目名 → canonical None → private_only,零信号
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 700, "大昀装修", 124, industry="客户张三的个人专属项目abc")
    H.seed_diagnosis(conn, "r_ind", owner=124, brand=700, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r_ind", H.now()); conn.commit(); conn.close()
    r = asyncio.run(process_next_pending(ctx=_GOLD_CTX, verifier=VNO, outcome_classifier=H.const_outcome()))
    assert r["state"] == "private_only" and "industry_not_in_allowlist" in r["reasons"]
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_signals"); assert cur.fetchone()["n"] == 0
    conn.close()


def test_freetext_industry_with_keyword_canonicalized_no_name_leak():
    # 含行业关键词的自由文本(夹带客户名)→ 映射受控 ID,客户名被丢弃,绝不进公共 signal
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 701, "大昀装修", 124, industry="贵州装修工程有限公司-张三")   # 含"装修"
    H.seed_diagnosis(conn, "r_ind2", owner=124, brand=701, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r_ind2", H.now()); conn.commit(); conn.close()
    r = asyncio.run(process_next_pending(ctx=_GOLD_CTX, verifier=VNO, outcome_classifier=H.const_outcome()))
    assert r["state"] == "promoted"
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT industry_key, prompt_family_key FROM geo_observation_signals ORDER BY signal_id DESC LIMIT 1")
    sig = cur.fetchone()
    assert sig["industry_key"] == "home_decoration"                       # 受控 ID(非自由文本)
    assert "张三" not in sig["industry_key"] and "张三" not in sig["prompt_family_key"]   # 客户名不泄露
    conn.close()


# ── P1-4:confirmed_mention 无金标准门 → pending_review(非关键词启发式自动晋升);实体不确定 → pending_review ──
def test_gold_gate_confirmed_mention_pending_review():
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "r_gg", owner=124, brand=501, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r_gg", H.now()); conn.commit(); conn.close()
    # gold_gate=False → confirmed_mention 只入 pending_review
    r = asyncio.run(process_next_pending(ctx=DecisionContext(True, "legal_v1", "c", None, outcome_gold_gate_passed=False),
                                         verifier=VNO))
    assert r["state"] == "pending_review" and "outcome_gold_gate_not_passed" in r["reasons"]


def test_ambiguous_entity_pending_review():
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 710, "晨光富士电梯", 124, industry="电梯")
    # 答案含混淆"富士电梯"(非专有前缀)+ 注入 YES(错)verifier → resolver 降级 UNKNOWN → ambiguous → pending_review
    ans = "本地有富士电梯和富士通电梯两个不同品牌,建议注意区分它们的资质与服务差异,多做对比后再决定选择。"
    H.seed_diagnosis(conn, "r_amb", owner=124, brand=710, run_status="committed", visibility="published",
                     question="电梯哪家好", engine="deepseek", answer=ans)
    source_hooks.register_paid_diagnosis(cur, "r_amb", H.now()); conn.commit(); conn.close()
    vy = H.const_verifier(BrandVerdict.YES, matched_text="富士电梯", window_index=1, start=0, end=4)
    r = asyncio.run(process_next_pending(ctx=DecisionContext(True, "legal_v1", "c", None, outcome_gold_gate_passed=True),
                                         verifier=vy, outcome_classifier=H.const_outcome()))
    assert r["state"] == "pending_review"
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_signals WHERE target_outcome='not_mentioned'")
    assert cur.fetchone()["n"] == 0    # 绝不当 not_mentioned
    conn.close()


# ── P1-5:readiness 抓未 VALIDATE 的 FK(convalidated=false 假绿)──
def test_readiness_catches_not_validated_constraint():
    conn = get_connection(); cur = conn.cursor()
    cur.execute("ALTER TABLE geo_observation_signals DROP CONSTRAINT fk_geo_obs_signal_event")
    cur.execute("ALTER TABLE geo_observation_signals ADD CONSTRAINT fk_geo_obs_signal_event "
                "FOREIGN KEY (event_id) REFERENCES geo_observation_events(id) NOT VALID")
    conn.commit()
    try:
        with pytest.raises(readiness.ObservationSchemaNotReady):
            readiness.verify_geo_observation_schema(cur)
    finally:
        cur.execute("ALTER TABLE geo_observation_signals VALIDATE CONSTRAINT fk_geo_obs_signal_event")
        conn.commit(); conn.close()


# ── P1-6:保留期到期 → 不可逆匿名化受限来源 + 删桶(signal 保留) ──
def test_retention_anonymizes_expired():
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "r_ret", owner=124, brand=501, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r_ret", H.now()); conn.commit(); conn.close()
    asyncio.run(process_next_pending(ctx=DecisionContext(True, "legal_v1", "c", None, outcome_gold_gate_passed=True),
                                     verifier=VNO, outcome_classifier=H.const_outcome()))
    # 把 retention_until 挪到过去
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE geo_observation_events SET retention_until=NOW() - interval '1 day' WHERE source_record_id='r_ret'")
    conn.commit(); conn.close()
    out = reconciler.run_reconciler()
    assert out["retention"]["anonymized"] >= 1
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT processing_state,rejection_codes,owner_user_id,brand_id,answer_hash,prompt_fingerprint FROM geo_observation_events WHERE id=(SELECT event_id FROM geo_observation_signals LIMIT 1)")
    ev = cur.fetchone()
    assert ev["processing_state"] == "withdrawn"
    assert "retention_expired" in ev["rejection_codes"]
    assert ev["owner_user_id"] is None and ev["brand_id"] is None and ev["answer_hash"] is None and ev["prompt_fingerprint"] is None
    cur.execute("SELECT count(*) AS n FROM geo_observation_contributor_buckets"); assert cur.fetchone()["n"] == 0
    cur.execute("SELECT count(*) AS n FROM geo_observation_signals"); assert cur.fetchone()["n"] == 1  # signal 保留
    cur.execute("SELECT count(*) AS n FROM geo_observation_audit WHERE action='retention_anonymize'")
    assert cur.fetchone()["n"] == 1
    conn.close()
    # 幂等:再跑不重复处理(owner/brand/hash 已全 NULL → 跳过);也不因 source_record_id 保留而被 reconcile_registration 重登记
    assert reconciler.run_reconciler()["retention"]["anonymized"] == 0
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_audit WHERE action='retention_anonymize'")
    assert cur.fetchone()["n"] == 1
    conn.close()


def test_retention_multi_event_no_unique_collision():
    """回归:多条同 source_type/source_table 的 event 同时到期匿名化,不得撞业务唯一键 / 不触发重登记 source_event_key 冲突。"""
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    for rt, sess in (("r_m1", "s1"), ("r_m2", "s2")):
        H.seed_diagnosis(conn, rt, owner=124, brand=501, run_status="committed", visibility="published",
                         answer=LONG, session_id=sess)
        source_hooks.register_paid_diagnosis(cur, rt, H.now())
    conn.commit()
    cur.execute("UPDATE geo_observation_events SET retention_until=NOW() - interval '1 day'")
    conn.commit(); conn.close()
    out = reconciler.run_reconciler()   # 不得抛 unique violation(唯一键/source_event_key)
    assert out["retention"]["anonymized"] >= 2
    # 再跑(含 registration/withdrawal/retention 三段)仍不抛、不重复
    out2 = reconciler.run_reconciler()
    assert out2["retention"]["anonymized"] == 0
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM geo_observation_events WHERE owner_user_id IS NULL AND brand_id IS NULL")
    assert cur.fetchone()["n"] >= 2
    conn.close()


def _api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.geo_observation_admin_api import router
    app = FastAPI()
    @app.middleware("http")
    async def _inj(request, call_next):
        request.state.user = {"is_admin": True, "username": "admin1"}
        return await call_next(request)
    app.include_router(router)
    return TestClient(app)


# ── 修复净增量 Finding 1:requeue from error 重置 attempts,可被重新处理(否则熔断器立即打回)──
def test_requeue_from_error_resets_attempts():
    _set_governance(promotion_enabled=True, legal="legal_v1")
    conn = get_connection()
    rid = H.seed_research(conn, 1, query="装修", answer="推荐以下几家:大昀装修。", round_status="completed", round_id="r_req")
    conn.close()
    c = get_connection(); source_hooks.register_research_raw(c.cursor(), rid); c.commit(); c.close()
    # 强制 attempts 超上限 → 熔断器打 error 终态
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE geo_observation_events SET attempts=%s WHERE source_type='research_round' RETURNING id",
                (promotion.MAX_PROMOTION_ATTEMPTS + 1,))
    eid = cur.fetchone()["id"]; conn.commit(); conn.close()
    assert asyncio.run(process_next_pending())["state"] == "error"
    # admin requeue(从 error)→ 重置 attempts=0 + pending
    r = _api_client().post(f"/api/admin/geo-observation/events/{eid}/review",
                           json={"decision": "requeue", "reason": "人工复核后重排", "request_id": "rqrq"})
    assert r.status_code == 200
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT attempts, processing_state FROM geo_observation_events WHERE id=%s", (eid,))
    row = cur.fetchone(); assert row["attempts"] == 0 and row["processing_state"] == "pending"; conn.close()
    # 下一 tick:真正重处理(不被熔断器立即打回 error)
    r2 = asyncio.run(process_next_pending())
    assert r2["state"] == "promoted"


# ── 修复净增量 Finding 2:policy 读失败被异常隔离(不 raise,不中断批)──
def test_policy_read_failure_isolated_no_raise():
    conn = get_connection()
    rid = H.seed_research(conn, 1, query="装修", answer="推荐大昀装修。", round_status="completed", round_id="r_perr")
    conn.close()
    c = get_connection(); source_hooks.register_research_raw(c.cursor(), rid); c.commit(); c.close()
    conn = get_connection(); cur = conn.cursor()
    cur.execute("DELETE FROM geo_observation_policy")   # get_policy 将 raise
    conn.commit(); conn.close()
    r = asyncio.run(process_next_pending())    # ctx=None → 读 policy 失败 → 被 except 隔离
    assert r["state"] == "exception"           # 不 raise,不中断批


# ── 修复净增量 PLAUSIBLE:LLM 期间管理员关闸 → 提交前重读新鲜门 → 在途 event 不放行 ──
def test_gate_closed_during_llm_blocks_inflight():
    _set_governance(promotion_enabled=True, legal="legal_v1", gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "r_close", owner=124, brand=501, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r_close", H.now()); conn.commit(); conn.close()

    async def _closing_clf(question, answer, identity):
        _set_governance(promotion_enabled=False, legal="legal_v1", gold=True)   # 管理员在 LLM 期间关闸
        from services.geo_observation.contracts import TargetOutcome
        return TargetOutcome.recommended
    r = asyncio.run(process_next_pending(verifier=VNO, outcome_classifier=_closing_clf))  # ctx=None → 提交前重读
    assert r["state"] == "private_only" and "promotion_disabled" in r["reasons"]


# ── 修复净增量(净增量审 P3 加固):熔断终态写遇瞬时 DB 故障也被隔离(不 raise 中断批),事件重领后仍判 error ──
def test_poison_breaker_terminal_write_failure_isolated(monkeypatch):
    conn = get_connection()
    rid = H.seed_research(conn, 1, query="装修", answer="推荐大昀装修。", round_status="completed", round_id="r_pbreak")
    conn.close()
    c = get_connection(); source_hooks.register_research_raw(c.cursor(), rid); c.commit(); c.close()
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE geo_observation_events SET attempts=%s WHERE source_type='research_round'",
                (promotion.MAX_PROMOTION_ATTEMPTS + 1,))
    conn.commit(); conn.close()
    # 注入熔断终态写瞬时故障 → 必须被隔离(返回 exception 而非 raise)
    def _boom(*a, **k):
        raise RuntimeError("transient db during breaker terminal write")
    monkeypatch.setattr(promotion.repository, "finish_event", _boom)
    r = asyncio.run(process_next_pending())
    assert r["state"] == "exception"          # 不 raise,批不中断
    # 摘掉故障 + 过期 lease → 重领 → 熔断器再判 error(LLM 不重烧:熔断在 Phase 2 之前)
    monkeypatch.undo()
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE geo_observation_events SET lease_until=NOW()-INTERVAL '1 hour' WHERE source_type='research_round'")
    conn.commit(); conn.close()
    r2 = asyncio.run(process_next_pending())
    assert r2["state"] == "error"


# ── P1-7:毒性事件熔断(attempts>上限 → error 终态,不再 poison-loop)──
def test_poison_event_circuit_breaker():
    from services.geo_observation import repository
    repository.register_event_standalone(dict(
        source_type="research_round", source_table="geo_research_raw", source_record_id="999", source_subkey="p",
        platform_key="deepseek", provider_key="dashscope", model_key="m",
        surface_key="deepseek_dashscope_search_legacy", session_mode="clean", observed_at=H.now(), industry_key="装修"))
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE geo_observation_events SET attempts=%s WHERE source_record_id='999'",
                (promotion.MAX_PROMOTION_ATTEMPTS + 1,))
    conn.commit(); conn.close()
    r = asyncio.run(process_next_pending())
    assert r["state"] == "error" and "max_attempts_exceeded" in r["reasons"]
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT processing_state FROM geo_observation_events WHERE source_record_id='999'")
    assert cur.fetchone()["processing_state"] == "error"   # 终态,不再被重领
    conn.close()
