"""Round-3 复审 3 个 P1 修复的判别测试:
  P1-1 金标准门证据派生(禁自我批准可信布尔值);
  P1-2 关闸与公开晋升同事务串行化(消除 TOCTOU 穿闸);
  P1-3 弱化 CHECK 假绿(readiness 强校验:TRUE OR 重言式/额外非法枚举/错列)。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os

import psycopg2
import psycopg2.errors
import pytest

from db.connection import get_connection
from services.brand_identity_resolver import BrandVerdict
from services.geo_observation import entity_review, policy, promotion, readiness, source_hooks
from services.geo_observation import repository as obs_repo
from services.geo_observation.entity_review import make_official_verifier
from services.geo_observation.promotion import process_next_pending

import _helpers as H

VNO = H.const_verifier(BrandVerdict.NO)
LONG = "大昀装修在深圳口碑非常好,服务专业,施工质量稳定可靠,非常值得推荐给有装修需求的业主优先考虑选择。"

_OUTCOME_ENUMS = ("recommended", "conditionally_recommended", "candidate_only", "mentioned_only", "criteria_only",
                  "refused_no_evidence", "refused_risk", "not_mentioned", "entity_ambiguous", "engine_error")
_OUTCOME_CANON = "target_outcome IN (" + ",".join("'%s'" % e for e in _OUTCOME_ENUMS) + ")"
_OUTCOME_ARRAY = "ARRAY[" + ",".join("'%s'" % e for e in _OUTCOME_ENUMS) + "]"


def _raw():
    return psycopg2.connect(os.environ["TEST_DATABASE_URL"])


def _set_gov(*, promotion_enabled, legal="legal_v1", gold=False):
    conn = get_connection(); cur = conn.cursor()
    snap = policy.get_policy()
    newp = dict(snap["policy"]); newp["feature_flags"] = dict(newp["feature_flags"])
    newp["feature_flags"]["promotion_enabled"] = promotion_enabled
    v = policy.update_policy(cur, expected_version=snap["policy_version"], new_policy=newp,
                             reason="enable", request_id="r3rp", operator_id="admin")
    v = policy.update_promotion_governance(cur, expected_version=v, promotion_legal_basis=legal,
                                           consent_policy_version="c1", reason="approve",
                                           request_id="r3rg", operator_id="admin")
    if gold:
        policy.record_gold_evaluation(cur, expected_version=v, dataset_version="ds_gov",
                                      sample_count=120, macro_f1_bps=9300, high_risk_false_reco=0,
                                      report_hash="rh_gov_" + "a" * 16, reason="gold", request_id="r3g", operator_id="admin")
    conn.commit(); conn.close()


# ═══════════════ P1-1:金标准门证据派生,禁自我批准 ═══════════════
def test_gold_gate_null_evidence_rejected_by_db_check():
    """DB CHECK 兜底:裸 UPDATE 无证据强开门 → check_violation(即便服务端算错也无法落库)。"""
    conn = get_connection(); cur = conn.cursor()
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE geo_observation_policy SET outcome_gold_gate_passed=TRUE WHERE singleton_id=1")
    conn.rollback(); conn.close()


def test_gold_gate_partial_evidence_rejected_by_db_check():
    """只给部分证据(缺 report_hash / 样本不足)也强开 → check_violation(NULL 安全:COALESCE 兜底不放行)。"""
    conn = get_connection(); cur = conn.cursor()
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("""UPDATE geo_observation_policy
                          SET outcome_gold_gate_passed=TRUE, gold_sample_count=120, gold_macro_f1_bps=9300,
                              gold_high_risk_false_reco=0, gold_dataset_version='ds'
                        WHERE singleton_id=1""")   # 缺 gold_report_hash
    conn.rollback(); conn.close()


@pytest.mark.parametrize("bad,idx", [({"sample_count": 99}, 0), ({"macro_f1_bps": 8999}, 1),
                                     ({"high_risk_false_reco": 1}, 2)])
def test_gold_gate_insufficient_evidence_derives_false(bad, idx):
    """样本<100 / F1<9000 / 高风险>0 任一 → 服务端派生 gate_passed=False(不接受强开)。"""
    conn = get_connection(); cur = conn.cursor()
    snap = policy.get_policy()
    kw = dict(dataset_version="ds_bad%d" % idx, sample_count=120, macro_f1_bps=9300, high_risk_false_reco=0,
              report_hash="rh_bad_%d_%s" % (idx, "z" * 12))
    kw.update(bad)
    res = policy.record_gold_evaluation(cur, expected_version=snap["policy_version"], reason="r",
                                        request_id="r3bad%d" % idx, operator_id="admin", **kw)
    conn.commit()
    assert res["gate_passed"] is False
    assert policy.get_policy()["outcome_gold_gate_passed"] is False
    conn.close()


def test_gold_gate_sufficient_evidence_derives_true():
    """恰达阈值(≥100 / ≥9000 / =0 + 数据集/哈希齐)→ 服务端派生 True + 证据快照落表。"""
    conn = get_connection(); cur = conn.cursor()
    snap = policy.get_policy()
    res = policy.record_gold_evaluation(cur, expected_version=snap["policy_version"], dataset_version="ds_ok",
                                        sample_count=100, macro_f1_bps=9000, high_risk_false_reco=0,
                                        report_hash="rh_ok_" + "y" * 16, reason="r", request_id="r3ok", operator_id="admin")
    conn.commit()
    assert res["gate_passed"] is True
    snap2 = policy.get_policy()
    assert snap2["outcome_gold_gate_passed"] is True
    assert snap2["gold_sample_count"] == 100 and snap2["gold_macro_f1_bps"] == 9000
    # 不可变评估记录已 append
    cur = get_connection().cursor()
    cur.execute("SELECT gate_passed FROM geo_observation_gold_eval WHERE report_hash=%s", ("rh_ok_" + "y" * 16,))
    assert cur.fetchone()["gate_passed"] is True
    conn.close()


def test_api_promotion_governance_forbids_gold_boolean():
    """API extra='forbid':PUT promotion-governance 带 outcome_gold_gate_passed → 422(无自我批准通道)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.geo_observation_admin_api import router
    app = FastAPI()

    @app.middleware("http")
    async def _inj(request, call_next):
        request.state.user = {"is_admin": True, "username": "a"}
        return await call_next(request)
    app.include_router(router)
    c = TestClient(app)
    v = policy.get_policy()["policy_version"]
    r = c.put("/api/admin/geo-observation/policy/promotion-governance",
              json={"expected_policy_version": v, "promotion_legal_basis": "legal_v1",
                    "outcome_gold_gate_passed": True, "reason": "hack attempt", "request_id": "r3hack"})
    assert r.status_code == 422


def test_api_gold_evaluation_endpoint_derives_gate():
    """POST /policy/gold-evaluation:只提交证据,服务端派生 gate_passed 并返回。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.geo_observation_admin_api import router
    app = FastAPI()

    @app.middleware("http")
    async def _inj(request, call_next):
        request.state.user = {"is_admin": True, "username": "a"}
        return await call_next(request)
    app.include_router(router)
    c = TestClient(app)
    v = policy.get_policy()["policy_version"]
    r = c.post("/api/admin/geo-observation/policy/gold-evaluation",
               json={"expected_policy_version": v, "dataset_version": "ds_api", "sample_count": 150,
                     "macro_f1_bps": 9500, "high_risk_false_reco": 0, "report_hash": "rh_api_" + "b" * 16,
                     "reason": "gold eval", "request_id": "r3api"})
    assert r.status_code == 200 and r.json()["gate_passed"] is True


# ── P1(复审第 3 轮):同证据身份不同指标不得改写结论 + gold_eval 不可变 ──
_SAME_HASH = "rh_same_" + "q" * 12


def test_gold_eval_same_identity_diff_metrics_rejected():
    """同 (dataset_version, report_hash) 先失败后通过 → GoldEvaluationConflict,policy 与评估表都不被刷成通过。"""
    conn = get_connection(); cur = conn.cursor()
    snap = policy.get_policy()
    v1 = policy.record_gold_evaluation(cur, expected_version=snap["policy_version"], dataset_version="dsX",
                                       sample_count=99, macro_f1_bps=8999, high_risk_false_reco=1,
                                       report_hash=_SAME_HASH, reason="fail", request_id="rqf", operator_id="admin")
    conn.commit(); conn.close()
    assert v1["gate_passed"] is False and policy.get_policy()["outcome_gold_gate_passed"] is False
    # 同身份换通过指标 → 拒绝(不改写)
    conn = get_connection(); cur = conn.cursor()
    with pytest.raises(policy.GoldEvaluationConflict):
        policy.record_gold_evaluation(cur, expected_version=policy.get_policy()["policy_version"], dataset_version="dsX",
                                      sample_count=100, macro_f1_bps=9000, high_risk_false_reco=0,
                                      report_hash=_SAME_HASH, reason="pass", request_id="rqp", operator_id="admin")
    conn.rollback(); conn.close()
    snap3 = policy.get_policy()
    assert snap3["outcome_gold_gate_passed"] is False and snap3["gold_sample_count"] == 99
    c = get_connection().cursor()
    c.execute("SELECT sample_count, gate_passed FROM geo_observation_gold_eval WHERE report_hash=%s", (_SAME_HASH,))
    row = c.fetchone()
    assert row["sample_count"] == 99 and row["gate_passed"] is False   # 不可变行未被改写


def test_gold_eval_same_identity_same_metrics_idempotent():
    """同身份同指标重复提交 → 幂等成功(policy 派生自持久化行,一致)。"""
    conn = get_connection(); cur = conn.cursor()
    for rq in ("i1", "i2"):
        r = policy.record_gold_evaluation(cur, expected_version=policy.get_policy()["policy_version"],
                                          dataset_version="dsIdem", sample_count=100, macro_f1_bps=9000,
                                          high_risk_false_reco=0, report_hash="rh_idem_" + "w" * 12,
                                          reason="ok", request_id=rq, operator_id="admin")
        conn.commit()
        assert r["gate_passed"] is True
    conn.close()


def _audit_epoch(cur):
    cur.execute("SELECT count(*) AS n FROM geo_observation_audit WHERE action='gold_evaluation_record'")
    n = cur.fetchone()["n"]
    cur.execute("SELECT value FROM system_settings WHERE key='geo_observation_policy_epoch'")
    e = (cur.fetchone() or {}).get("value")
    return n, e


def test_gold_evaluation_true_idempotent_zero_extra_writes():
    """P2:同证据用**最新版本**重试 → 真幂等零额外写(policy_version 不再 bump / 无重复审计 / epoch 不再翻)。"""
    h = "rh_idem3_" + "z" * 12
    conn = get_connection(); cur = conn.cursor()
    v0 = policy.get_policy()["policy_version"]
    policy.record_gold_evaluation(cur, expected_version=v0, dataset_version="dsIdem3", sample_count=120,
                                  macro_f1_bps=9300, high_risk_false_reco=0, report_hash=h,
                                  reason="first", request_id="q1", operator_id="admin")
    conn.commit()
    v1 = policy.get_policy()["policy_version"]
    audit1, epoch1 = _audit_epoch(cur)
    assert v1 == v0 + 1   # 首次推进版本
    # 二次:同证据 + 最新版本 → 幂等,零额外写
    res2 = policy.record_gold_evaluation(cur, expected_version=v1, dataset_version="dsIdem3", sample_count=120,
                                         macro_f1_bps=9300, high_risk_false_reco=0, report_hash=h,
                                         reason="retry", request_id="q2", operator_id="admin")
    conn.commit()
    audit2, epoch2 = _audit_epoch(cur)
    assert res2["policy_version"] == v1 and policy.get_policy()["policy_version"] == v1   # 版本不再推进
    assert audit2 == audit1        # 无重复审计
    assert epoch2 == epoch1        # epoch 未再翻
    conn.close()


def test_api_gold_evaluation_identity_conflict_409():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.geo_observation_admin_api import router
    app = FastAPI()

    @app.middleware("http")
    async def _inj(request, call_next):
        request.state.user = {"is_admin": True, "username": "a"}
        return await call_next(request)
    app.include_router(router)
    c = TestClient(app)
    base = dict(dataset_version="dsAPI2", report_hash="rh_api2_" + "k" * 12, reason="eval submit", request_id="x")
    r1 = c.post("/api/admin/geo-observation/policy/gold-evaluation",
                json={"expected_policy_version": policy.get_policy()["policy_version"],
                      "sample_count": 50, "macro_f1_bps": 100, "high_risk_false_reco": 5, **base})
    assert r1.status_code == 200 and r1.json()["gate_passed"] is False
    r2 = c.post("/api/admin/geo-observation/policy/gold-evaluation",
                json={"expected_policy_version": policy.get_policy()["policy_version"],
                      "sample_count": 120, "macro_f1_bps": 9300, "high_risk_false_reco": 0, **base})
    assert r2.status_code == 409 and r2.json()["detail"]["code"] == "GOLD_EVALUATION_IDENTITY_CONFLICT"


def test_gold_eval_immutable_update_delete_rejected():
    """gold_eval append-only:已落库评估行 UPDATE/DELETE 被 DB 触发器拒。"""
    h = "rh_imm_" + "m" * 12
    conn = get_connection(); cur = conn.cursor()
    cur.execute("""INSERT INTO geo_observation_gold_eval
        (dataset_version, sample_count, macro_f1_bps, high_risk_false_reco, report_hash, gate_passed)
        VALUES ('dsImm', 100, 9000, 0, %s, TRUE)""", (h,))
    conn.commit(); conn.close()
    for sql in ("UPDATE geo_observation_gold_eval SET gate_passed=FALSE WHERE report_hash=%s",
                "DELETE FROM geo_observation_gold_eval WHERE report_hash=%s"):
        c = get_connection(); cc = c.cursor()
        with pytest.raises(psycopg2.errors.RaiseException):
            cc.execute(sql, (h,))
        c.rollback(); c.close()


def test_gold_eval_gate_passed_consistency_enforced():
    """gold_eval.gate_passed 必与指标一致:裸 INSERT 指标不达标却 passed=TRUE → CHECK 拒(防伪造通过评估)。"""
    conn = get_connection(); cur = conn.cursor()
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("""INSERT INTO geo_observation_gold_eval
            (dataset_version, sample_count, macro_f1_bps, high_risk_false_reco, report_hash, gate_passed)
            VALUES ('dsInc', 50, 100, 5, 'rh_inc_'||repeat('z',12), TRUE)""")
    conn.rollback(); conn.close()


_GOLD_RESTORE_SQL = """ALTER TABLE geo_observation_policy ADD CONSTRAINT ck_geo_obs_policy_gold_gate CHECK (
    outcome_gold_gate_passed = FALSE OR (
        COALESCE(gold_sample_count,-1) >= 100 AND COALESCE(gold_macro_f1_bps,-1) >= 9000
        AND COALESCE(gold_high_risk_false_reco,-1) = 0
        AND gold_dataset_version IS NOT NULL AND gold_report_hash IS NOT NULL))"""


@pytest.mark.parametrize("weak_check,label", [
    ("TRUE", "check_true"),                                                          # 全弱化
    ("outcome_gold_gate_passed = FALSE OR gold_report_hash IS NOT NULL", "report_hash_only"),   # 单探针(全NULL)会漏
    ("""outcome_gold_gate_passed = FALSE OR (COALESCE(gold_sample_count,-1) >= 1
        AND COALESCE(gold_macro_f1_bps,-1) >= 1 AND COALESCE(gold_high_risk_false_reco,-1) = 0
        AND gold_dataset_version IS NOT NULL AND gold_report_hash IS NOT NULL)""", "thresholds_lowered"),  # 降阈值·NULL安全,单探针漏
])
def test_readiness_catches_gold_gate_weakening(weak_check, label):
    """金标准 CHECK 定义级精确匹配:CHECK(TRUE)/部分弱化(report_hash-only)/降阈值(1/1)全被拦(单向量探针会漏后两者)。"""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("ALTER TABLE geo_observation_policy DROP CONSTRAINT ck_geo_obs_policy_gold_gate")
    cur.execute("ALTER TABLE geo_observation_policy ADD CONSTRAINT ck_geo_obs_policy_gold_gate CHECK (%s)" % weak_check)
    conn.commit()
    rconn = get_connection()
    try:
        with pytest.raises(readiness.ObservationSchemaNotReady):
            readiness.verify_geo_observation_schema(rconn.cursor())
    finally:
        rconn.rollback(); rconn.close()   # 释放 readiness 连接的 ACCESS SHARE,免阻塞下面的 ALTER
        cur.execute("ALTER TABLE geo_observation_policy DROP CONSTRAINT ck_geo_obs_policy_gold_gate")
        cur.execute(_GOLD_RESTORE_SQL)
        conn.commit(); conn.close()


# 恢复用 %-free 函数体(避免 psycopg2 的 % 处理 + plpgsql 占位符纠缠);readiness 功能反查只需触发器 RAISE,消息内容不限。
_IMMUT_FN_RESTORE = """CREATE OR REPLACE FUNCTION geo_obs_gold_eval_immutable() RETURNS trigger AS $imm$
BEGIN
    RAISE EXCEPTION 'geo_observation_gold_eval is append-only immutable; UPDATE/DELETE forbidden';
END;
$imm$ LANGUAGE plpgsql;"""


@pytest.mark.parametrize("tamper,restore,label", [
    ("ALTER TABLE geo_observation_gold_eval DISABLE TRIGGER trg_geo_obs_gold_eval_immutable",
     "ALTER TABLE geo_observation_gold_eval ENABLE TRIGGER trg_geo_obs_gold_eval_immutable", "disable_trigger"),
    ("CREATE OR REPLACE FUNCTION geo_obs_gold_eval_immutable() RETURNS trigger AS $g$ BEGIN RETURN NEW; END; $g$ LANGUAGE plpgsql",
     _IMMUT_FN_RESTORE, "gut_function_body"),
    # 探针感知型:只对旧探针 sentinel(created_by='probe')RAISE、放行真行 → 新探针不写 sentinel+探真行 → 仍检出
    ("""CREATE OR REPLACE FUNCTION geo_obs_gold_eval_immutable() RETURNS trigger AS $g$
        BEGIN IF TG_OP='UPDATE' AND NEW.created_by='probe' THEN RAISE EXCEPTION 'blocked'; END IF;
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'blocked'; END IF; RETURN NEW; END; $g$ LANGUAGE plpgsql""",
     _IMMUT_FN_RESTORE, "probe_aware_created_by_sentinel"),
])
def test_readiness_catches_append_only_neutered(tamper, restore, label):
    """append-only 被任一方式弄失效(DISABLE 触发器 / CREATE OR REPLACE 掏空函数体)→ readiness 语义功能反查拦下。
    (存在/tgenabled 句法核验漏后者;功能反查一网打尽。)"""
    conn = get_connection(); cur = conn.cursor()
    cur.execute(tamper)
    conn.commit()
    rconn = get_connection()
    try:
        with pytest.raises(readiness.ObservationSchemaNotReady):
            readiness.verify_geo_observation_schema(rconn.cursor())
    finally:
        rconn.rollback(); rconn.close()
        cur.execute(restore)
        conn.commit(); conn.close()


def test_readiness_passes_on_canonical_gold_check():
    """正例:规范金标准 CHECK 下 readiness 不误报(精确匹配不 false-positive)。"""
    conn = get_connection()
    try:
        readiness.verify_geo_observation_schema(conn.cursor())   # 不抛
    finally:
        conn.rollback(); conn.close()


_GOLD_DERIVED_RESTORE = """ALTER TABLE geo_observation_gold_eval ADD CONSTRAINT chk_geo_obs_gold_derived CHECK (
    gate_passed = (sample_count >= 100 AND macro_f1_bps >= 9000 AND high_risk_false_reco = 0
                   AND length(dataset_version) > 0 AND length(report_hash) > 0))"""
_GOLD_FK_RESTORE = """ALTER TABLE geo_observation_policy ADD CONSTRAINT fk_geo_obs_policy_gold_eval
    FOREIGN KEY (gold_dataset_version, gold_report_hash) REFERENCES geo_observation_gold_eval (dataset_version, report_hash)"""


def test_readiness_catches_gold_derived_weakened():
    """chk_geo_obs_gold_derived 被同名换成 CHECK(TRUE) → readiness 定义级 pin 拦下(否则可写指标不达标却 passed=true)。"""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("ALTER TABLE geo_observation_gold_eval DROP CONSTRAINT chk_geo_obs_gold_derived")
    cur.execute("ALTER TABLE geo_observation_gold_eval ADD CONSTRAINT chk_geo_obs_gold_derived CHECK (TRUE)")
    conn.commit()
    rconn = get_connection()
    try:
        with pytest.raises(readiness.ObservationSchemaNotReady):
            readiness.verify_geo_observation_schema(rconn.cursor())
    finally:
        rconn.rollback(); rconn.close()
        cur.execute("ALTER TABLE geo_observation_gold_eval DROP CONSTRAINT chk_geo_obs_gold_derived")
        cur.execute(_GOLD_DERIVED_RESTORE)
        conn.commit(); conn.close()


def test_readiness_catches_fk_retargeted_to_fake_table():
    """policy→gold_eval 外键被改指 fake_gold_eval → readiness 定义级 pin 拦下(否则策略指针可指伪造评估表)。"""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS fake_gold_eval (dataset_version TEXT, report_hash TEXT, UNIQUE (dataset_version, report_hash))")
    cur.execute("ALTER TABLE geo_observation_policy DROP CONSTRAINT fk_geo_obs_policy_gold_eval")
    cur.execute("""ALTER TABLE geo_observation_policy ADD CONSTRAINT fk_geo_obs_policy_gold_eval
        FOREIGN KEY (gold_dataset_version, gold_report_hash) REFERENCES fake_gold_eval (dataset_version, report_hash)""")
    conn.commit()
    rconn = get_connection()
    try:
        with pytest.raises(readiness.ObservationSchemaNotReady):
            readiness.verify_geo_observation_schema(rconn.cursor())
    finally:
        rconn.rollback(); rconn.close()
        cur.execute("ALTER TABLE geo_observation_policy DROP CONSTRAINT fk_geo_obs_policy_gold_eval")
        cur.execute(_GOLD_FK_RESTORE)
        cur.execute("DROP TABLE IF EXISTS fake_gold_eval")
        conn.commit(); conn.close()


# ═══════════════ P1-2:关闸与公开晋升同事务串行化 ═══════════════
def test_commit_decision_locks_policy_for_share_in_txn():
    """源码锚:commit_decision 在同事务对 policy 行 FOR SHARE;process_next_pending 生产路径(ctx=None)启用。"""
    src = inspect.getsource(promotion.commit_decision)
    assert "FOR SHARE" in src and "geo_observation_policy" in src
    psrc = inspect.getsource(promotion.process_next_pending)
    assert "governance_in_txn" in psrc and "ctx is None" in psrc


def test_policy_for_share_blocks_admin_for_update():
    """真锁冲突:晋升在途持 policy 行 FOR SHARE 时,管理员关闸 FOR UPDATE NOWAIT 立即失败 → 二者串行化。"""
    a = _raw(); ca = a.cursor()
    ca.execute("SELECT 1 FROM geo_observation_policy WHERE singleton_id=1 FOR SHARE")   # 事务开着,持 share 锁
    b = _raw(); cb = b.cursor()
    try:
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            cb.execute("SELECT 1 FROM geo_observation_policy WHERE singleton_id=1 FOR UPDATE NOWAIT")
    finally:
        b.rollback(); b.close(); a.rollback(); a.close()


def test_gate_closed_before_commit_blocks_promotion_production_path():
    """生产路径(ctx=None):管理员关闸提交返回后,commit_decision 同事务 FOR SHARE 读到关后值 → 不晋升。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection()
    rid = H.seed_research(conn, 1, query="装修", answer="推荐大昀装修。", round_status="completed", round_id="r3_close")
    conn.close()
    c = get_connection(); source_hooks.register_research_raw(c.cursor(), rid); c.commit(); c.close()
    _set_gov(promotion_enabled=False, gold=True)   # 关闸(提交返回)
    r = asyncio.run(process_next_pending())         # ctx=None → in-txn FOR SHARE 读到关后
    assert r["state"] == "private_only" and "promotion_disabled" in r["reasons"]


def test_gate_open_production_path_promotes():
    """生产路径正例:开闸 + legal + gold → 调研事件 in-txn 读到开闸值 → promoted(证明 FOR SHARE 读的是真值非恒关)。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection()
    rid = H.seed_research(conn, 2, query="装修", answer="推荐大昀装修。", round_status="completed", round_id="r3_open")
    conn.close()
    c = get_connection(); source_hooks.register_research_raw(c.cursor(), rid); c.commit(); c.close()
    r = asyncio.run(process_next_pending())
    assert r["state"] == "promoted"


def test_gold_gate_closed_during_llm_blocks_promotion():
    """gold TOCTOU 闭合:confirmed_mention 事件 Phase-2 门开→调 LLM;LLM 期间管理员用不达标证据关 gold 门;
    提交前 in-txn FOR SHARE 重读 gold=False → AND 语义落 pending_review(不晋升推荐)。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 501, "大昀装修", 124)
    H.seed_diagnosis(conn, "r3_goldclose", owner=124, brand=501, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r3_goldclose", H.now()); conn.commit(); conn.close()

    async def _closing_clf(question, answer, identity):
        c = get_connection(); cc = c.cursor()
        snap = policy.get_policy()
        policy.record_gold_evaluation(cc, expected_version=snap["policy_version"], dataset_version="ds_close",
                                      sample_count=10, macro_f1_bps=100, high_risk_false_reco=9,
                                      report_hash="rh_close_" + "c" * 12, reason="close", request_id="r3close",
                                      operator_id="admin")
        c.commit(); c.close()
        from services.geo_observation.contracts import TargetOutcome
        return TargetOutcome.recommended
    r = asyncio.run(process_next_pending(verifier=VNO, outcome_classifier=_closing_clf))
    assert r["state"] == "pending_review" and "outcome_gold_gate_not_passed" in r["reasons"]


def test_confirmed_mention_gold_open_promotes_production_path():
    """gold AND 语义正例:门全程开 → confirmed_mention 推荐经 in-txn 读仍 True → promoted(证明未过度拦截)。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 502, "大昀装修", 124)
    H.seed_diagnosis(conn, "r3_goldopen", owner=124, brand=502, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r3_goldopen", H.now()); conn.commit(); conn.close()
    r = asyncio.run(process_next_pending(verifier=VNO, outcome_classifier=H.const_outcome("recommended")))
    assert r["state"] == "promoted"


async def test_lease_heartbeat_prevents_double_paid_model_call():
    """P1-2:慢付费模型下双 worker 竞争——Phase-2 伴飞续租保住租约,外部 outcome 模型恰好调用一次(无重复扣费)。
    对照:不续租则 3s 租约在 5s 慢模型中途过期→另一 worker 重领→第二次付费调用。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 601, "大昀装修", 124)
    H.seed_diagnosis(conn, "r3_hb", owner=124, brand=601, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r3_hb", H.now()); conn.commit(); conn.close()
    calls = []

    async def slow_clf(question, answer, identity):
        calls.append(1)
        await asyncio.sleep(5)   # 慢付费模型(超 3s 租约)
        from services.geo_observation.contracts import TargetOutcome
        return TargetOutcome.recommended

    async def _w1():
        return await process_next_pending(verifier=VNO, outcome_classifier=slow_clf, lease_seconds=3)

    async def _w2():
        for _ in range(16):   # 轮询重领 ~8s(跨过无续租时 3s 租约会过期的窗口)
            r = await process_next_pending(verifier=VNO, outcome_classifier=slow_clf, lease_seconds=3)
            if r is not None:
                return r
            await asyncio.sleep(0.5)
        return None

    r1, r2 = await asyncio.gather(_w1(), _w2())
    assert len(calls) == 1, f"付费模型被调用 {len(calls)} 次(应恰好 1 次)"
    states = [(r or {}).get("state") for r in (r1, r2)]
    assert "promoted" in states


def test_active_cas_before_paid_call_catches_lease_stolen_in_source_read():
    """P1 端到端:旧 worker 在**源读阶段**租约被另一 worker 重领(token 被覆盖)→ 守卫链检出 → 旧 worker
    零付费调用(state=lease_lost)。确定性复现(无线程/时序竞态)。
    注:本用例品牌为 trusted_exact(不调 verifier),故它验证的是"偷租→零付费→lease_lost"的端到端行为,
    对"第一处 CAS(verifier 前)vs 第二处 CAS(outcome 前)"**无判别力**——判别力分别由下方
    test_first_cas_before_verifier_skips_paid_verifier_* 与 test_second_cas_before_outcome_skips_clf_* 覆盖。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 603, "大昀装修", 124)
    H.seed_diagnosis(conn, "r3_steal", owner=124, brand=603, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r3_steal", H.now()); conn.commit(); conn.close()

    from services.geo_observation.contracts import TargetOutcome
    paid_calls = []

    async def counting_clf(question, answer, identity):
        paid_calls.append(1)
        return TargetOutcome.recommended

    orig_reload = promotion._reload_observation

    def steal_during_source_read(cur2, claimed):
        res = orig_reload(cur2, claimed)   # 真源读先完成
        # 模拟另一 worker 在源读窗口内重领:覆盖 lease_token + 续租(旧 worker 原 token 从此失效)
        c = get_connection(); cc = c.cursor()
        cc.execute(
            "UPDATE geo_observation_events SET lease_token='00000000-0000-0000-0000-0000000000ff', "
            "lease_until=NOW()+make_interval(secs => 300) WHERE id=%s", (claimed["id"],))
        c.commit(); c.close()
        return res

    promotion._reload_observation = steal_during_source_read
    try:
        r = asyncio.run(process_next_pending(verifier=VNO, outcome_classifier=counting_clf, lease_seconds=3))
    finally:
        promotion._reload_observation = orig_reload

    assert len(paid_calls) == 0, f"租约被抢后仍调付费模型 {len(paid_calls)} 次(应为 0)"
    assert r["state"] == "lease_lost", f"应因租约丢失零写,实为 {r}"


def test_first_cas_before_verifier_skips_paid_verifier_when_lease_stolen():
    """P1 判别(**第一处** CAS,verifier 前):混淆品牌 → resolve_entity 会调**付费 verifier**(生产第一次付费调用)。
    源读窗口内租约被重领 → resolve_entity 前的主动 CAS 检出 → **verifier 零调用** + outcome 零调用 → lease_lost。
    判别力:删掉第一处 CAS(promotion.py:417),偷租后 resolve_entity 会真调 verifier → verifier_calls==1 → 本用例失败。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 606, "晨光富士电梯", 124, industry="电梯")
    ans = "本地有富士电梯和富士通电梯两个不同品牌,建议注意区分它们的资质与服务差异,多做对比后再决定选择方案更稳妥。"
    H.seed_diagnosis(conn, "r3_verif_steal", owner=124, brand=606, run_status="committed", visibility="published",
                     question="电梯哪家好", engine="deepseek", answer=ans)
    source_hooks.register_paid_diagnosis(cur, "r3_verif_steal", H.now()); conn.commit(); conn.close()

    from services.brand_identity_resolver import VerificationResult
    from services.geo_observation.contracts import TargetOutcome
    verifier_calls = []
    clf_calls = []

    async def counting_verifier(*, identity, evidence_windows):
        verifier_calls.append(1)
        return VerificationResult(BrandVerdict.NO, "")

    async def counting_clf(question, answer, identity):
        clf_calls.append(1)
        return TargetOutcome.recommended

    orig_reload = promotion._reload_observation

    def steal_during_source_read(cur2, claimed):
        res = orig_reload(cur2, claimed)
        c = get_connection(); cc = c.cursor()
        cc.execute("UPDATE geo_observation_events SET lease_token='00000000-0000-0000-0000-0000000000ff', "
                   "lease_until=NOW()+make_interval(secs => 300) WHERE id=%s", (claimed["id"],))
        c.commit(); c.close()
        return res

    promotion._reload_observation = steal_during_source_read
    try:
        r = asyncio.run(process_next_pending(verifier=counting_verifier, outcome_classifier=counting_clf, lease_seconds=3))
    finally:
        promotion._reload_observation = orig_reload

    assert len(verifier_calls) == 0, f"第一处 CAS 失效:付费 verifier 被调用 {len(verifier_calls)} 次(应为 0)"
    assert len(clf_calls) == 0, f"outcome 模型不应被调用,实为 {len(clf_calls)}"
    assert r["state"] == "lease_lost", f"应因租约丢失零写,实为 {r}"


def test_second_cas_before_outcome_skips_clf_when_lease_stolen():
    """P1 判别(**第二处** CAS,outcome 前):trusted_exact 品牌 → 第一处 CAS 先通过、resolve_entity 判 confirmed_mention;
    在 resolve_entity 之后、outcome 模型之前偷租(钩在 classify_outcome 首调)→ 第二处 CAS 检出 → **outcome 零调用** → lease_lost。
    判别力:删掉第二处 CAS(promotion.py:431),偷租后仍会调 outcome 模型 → clf_calls==1 → 本用例失败。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 607, "大昀装修", 124)
    H.seed_diagnosis(conn, "r3_clf_steal", owner=124, brand=607, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r3_clf_steal", H.now()); conn.commit(); conn.close()

    from services.geo_observation.contracts import TargetOutcome
    clf_calls = []

    async def counting_clf(question, answer, identity):
        clf_calls.append(1)
        return TargetOutcome.recommended

    orig_classify = promotion.classify_outcome
    stole = {"done": False}

    def steal_then_classify(*a, **k):
        # 首次 classify_outcome 调用发生在 resolve_entity 之后、outcome 模型之前 → 此刻偷租
        if not stole["done"]:
            stole["done"] = True
            c = get_connection(); cc = c.cursor()
            cc.execute("UPDATE geo_observation_events SET lease_token='00000000-0000-0000-0000-0000000000ff', "
                       "lease_until=NOW()+make_interval(secs => 300) WHERE processing_state='processing'")
            c.commit(); c.close()
        return orig_classify(*a, **k)

    promotion.classify_outcome = steal_then_classify
    try:
        r = asyncio.run(process_next_pending(verifier=VNO, outcome_classifier=counting_clf, lease_seconds=3))
    finally:
        promotion.classify_outcome = orig_classify

    assert stole["done"], "classify_outcome 未被调用(前置路径异常,测试未触达第二处 CAS)"
    assert len(clf_calls) == 0, f"第二处 CAS 失效:outcome 模型被调用 {len(clf_calls)} 次(应为 0)"
    assert r["state"] == "lease_lost", f"应因租约丢失零写,实为 {r}"


def test_two_workers_barrier_paid_model_exactly_once():
    """P1 真双 worker + **确定性屏障**(无墙钟竞态):w1 领取后在源读阶段阻塞(threading.Event 等待),测试显式
    过期其租约 → w2 真实重领并完成**唯一**付费调用(promoted)→ 释放 w1 → w1 醒来调用前主动 CAS 检出租约丢 → 跳过。
    付费模型调用总数恰为 1;states=[lease_lost, promoted]。不依赖任何 sleep 时序(lease=30s,唯一重领由显式 SQL 过期驱动)。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 604, "大昀装修", 124)
    H.seed_diagnosis(conn, "r3_2w", owner=124, brand=604, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r3_2w", H.now()); conn.commit(); conn.close()

    import threading
    from services.geo_observation.contracts import TargetOutcome

    paid_calls = []
    _pc_lock = threading.Lock()
    w1_in_source_read = threading.Event()
    w1_may_resume = threading.Event()

    async def counting_clf(question, answer, identity):
        with _pc_lock:
            paid_calls.append(1)
        return TargetOutcome.recommended

    orig_reload = promotion._reload_observation
    _first = {"seen": False}
    _first_lock = threading.Lock()

    def barrier_first_reload(cur2, claimed):
        with _first_lock:
            is_first = not _first["seen"]
            _first["seen"] = True
        res = orig_reload(cur2, claimed)
        if is_first:
            w1_in_source_read.set()          # 通知:w1 已领取 + 已完成源读(阻塞点)
            w1_may_resume.wait(timeout=20)   # 阻塞 w1(其事件循环/伴飞随之阻塞),等测试放行
        return res

    results = {}

    def _run_once(name):
        results[name] = asyncio.run(
            process_next_pending(verifier=VNO, outcome_classifier=counting_clf, lease_seconds=30))

    def _run_poll(name):
        async def poll():
            for _ in range(100):
                r = await process_next_pending(verifier=VNO, outcome_classifier=counting_clf, lease_seconds=30)
                if r is not None:
                    return r
                await asyncio.sleep(0.02)
            return None
        results[name] = asyncio.run(poll())

    promotion._reload_observation = barrier_first_reload
    t1 = threading.Thread(target=_run_once, args=("w1",))
    t2 = threading.Thread(target=_run_poll, args=("w2",))
    try:
        t1.start()
        assert w1_in_source_read.wait(timeout=20), "w1 未进入源读阻塞点"
        # w1 此刻阻塞在源读(伴飞无法续)→ 显式过期租约,使 w2 可原子重领(唯一重领来源,非墙钟)
        c = get_connection(); cc = c.cursor()
        cc.execute("UPDATE geo_observation_events SET lease_until = NOW() - make_interval(secs => 10) "
                   "WHERE processing_state='processing'")
        c.commit(); c.close()
        t2.start(); t2.join(timeout=20)   # w2 真实重领 + 唯一付费调用 + promote
        w1_may_resume.set()               # 放行 w1 → 其调用前主动 CAS 用原 token(已被 w2 覆盖)→ lease_lost
        t1.join(timeout=20)
    finally:
        w1_may_resume.set()
        promotion._reload_observation = orig_reload

    assert not t1.is_alive() and not t2.is_alive(), "worker 线程超时未结束"
    assert len(paid_calls) == 1, f"付费模型调用 {len(paid_calls)} 次(应恰 1)"
    states = sorted(str((results.get(n) or {}).get("state")) for n in ("w1", "w2"))
    assert states == ["lease_lost", "promoted"], f"状态应为 [lease_lost, promoted],实为 {states}"


# ═══════════ 第6次 NO-GO:守卫下沉到真实 provider POST(httpx)边界 + 付费幂等状态机 ═══════════
# 说明:生产 verifier/outcome 走 entity_review._paid_safe_deepseek_post —— 守卫紧贴每一笔 httpx.AsyncClient.post
# (含多 key failover 的每一笔 + 400 fallback 的第二笔)。测试在 **httpx.post + get_deepseek_api_keys** 两个真实
# 边界打桩,驱动真实 helper(而非替换整个 post 函数),覆盖多 key failover 循环。
_STEAL_TOKEN = "00000000-0000-0000-0000-0000000000ff"
_CONFUSE_ANS = "本地有富士电梯和富士通电梯两个不同品牌,建议注意区分它们的资质与服务差异,多做对比后再决定选择方案更稳妥。"
_VERIF_NO_JSON = json.dumps({"verdict": "NO", "reason": "x", "matched_text": "",
                             "window_index": None, "matched_start": None, "matched_end": None})
_OUTCOME_OK_JSON = json.dumps({"outcome": "recommended", "reason": "ok"})


class _FakeResp:
    def __init__(self, status_code, content=""):
        self.status_code = status_code
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _steal_lease():
    c = get_connection(); cc = c.cursor()
    cc.execute("UPDATE geo_observation_events SET lease_token=%s, lease_until=NOW()+make_interval(secs => 300) "
               "WHERE processing_state='processing'", (_STEAL_TOKEN,))
    c.commit(); c.close()


def _install_fake_deepseek(monkeypatch, *, keys, behaviors):
    """在真实付费边界打桩:get_deepseek_api_keys→keys;httpx.AsyncClient.post→按 behaviors[idx] 返回/抛。
    behaviors[idx] 是无参 callable,返回 _FakeResp 或 raise(httpx 异常)。返回记录每次真实 httpx.post 的 posts 列表。"""
    import httpx
    from services.llm import deepseek_key_pool
    monkeypatch.setattr(deepseek_key_pool, "get_deepseek_api_keys", lambda role=None: list(keys))
    posts = []

    async def fake_post(self, url, **kwargs):
        idx = len(posts)
        posts.append(1)
        b = behaviors[idx] if idx < len(behaviors) else behaviors[-1]
        return b()
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return posts


def _anchor_of(source_record_id):
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT paid_call_started_at FROM geo_observation_events WHERE source_record_id=%s", (source_record_id,))
    row = cur.fetchone(); conn.close()
    return row["paid_call_started_at"] if row else "MISSING"


def test_guard_skips_verifier_post_when_lease_stolen_during_identity_load(monkeypatch):
    """P1(test-a):粗粒度 CAS 通过后、resolve_entity 同步加载品牌身份期间租约被重领 → **真实 verifier httpx.post 前**
    守卫检出 → verifier POST 零次。混淆品牌走生产路径(_paid_safe_deepseek_post);偷租注入在 resolve_entity 入口。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 608, "晨光富士电梯", 124, industry="电梯")
    H.seed_diagnosis(conn, "r6_idload", owner=124, brand=608, run_status="committed", visibility="published",
                     question="电梯哪家好", engine="deepseek", answer=_CONFUSE_ANS)
    source_hooks.register_paid_diagnosis(cur, "r6_idload", H.now()); conn.commit(); conn.close()

    posts = _install_fake_deepseek(monkeypatch, keys=["k1"], behaviors=[lambda: _FakeResp(200, _VERIF_NO_JSON)])

    orig_resolve = entity_review.resolve_entity

    async def stealing_resolve(*args, **kwargs):
        _steal_lease()   # 模拟:for_brand 同步身份读阶段,另一 worker 重领
        return await orig_resolve(*args, **kwargs)
    monkeypatch.setattr(promotion, "resolve_entity", stealing_resolve)

    r = asyncio.run(process_next_pending(lease_seconds=3))
    assert len(posts) == 0, f"租约在身份读阶段丢失,verifier httpx.post 应为 0,实为 {len(posts)}"
    assert r["state"] == "lease_lost", f"应 lease_lost,实为 {r}"
    assert _anchor_of("r6_idload") is None, "守卫在 POST 前因租约丢失阻断,不应落耐久锚"


def test_guard_skips_second_post_when_lease_stolen_before_400_fallback(monkeypatch):
    """P1(test-b):verifier HTTP 400 去 response_format 重试的**第二次真实 httpx.post 前**再被守卫拦。
    第一笔返回 400 并在其后偷租 → 第二笔前守卫检出 → 第二笔 0 次(总 httpx.post=1)。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 609, "晨光富士电梯", 124, industry="电梯")
    H.seed_diagnosis(conn, "r6_400", owner=124, brand=609, run_status="committed", visibility="published",
                     question="电梯哪家好", engine="deepseek", answer=_CONFUSE_ANS)
    source_hooks.register_paid_diagnosis(cur, "r6_400", H.now()); conn.commit(); conn.close()

    def b0():
        _steal_lease()          # 第一笔 POST 后偷租
        return _FakeResp(400)   # 触发去 response_format 的第二笔
    posts = _install_fake_deepseek(monkeypatch, keys=["k1"],
                                   behaviors=[b0, lambda: _FakeResp(200, _VERIF_NO_JSON)])

    r = asyncio.run(process_next_pending(lease_seconds=3))
    assert len(posts) == 1, f"400 后第二笔 httpx.post 前应被守卫拦,总 POST 应为 1,实为 {len(posts)}"
    assert r["state"] == "lease_lost", f"应 lease_lost,实为 {r}"


def test_no_second_post_when_phase3_blocked_and_reclaimed(monkeypatch):
    """P1(test-c):outcome 模型已付费返回后,Phase 3 取锁前被重领 → Phase-3 FOR UPDATE 锁+校验检出 → 零写 lease_lost,
    **不再发起第二笔 httpx.post**(总 POST=1)。耐久锚 paid_call_started_at 已在首笔 POST 前落库 → 重领将路由人工。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 610, "大昀装修", 124)
    H.seed_diagnosis(conn, "r6_p3", owner=124, brand=610, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r6_p3", H.now()); conn.commit(); conn.close()

    posts = _install_fake_deepseek(monkeypatch, keys=["k1"], behaviors=[lambda: _FakeResp(200, _OUTCOME_OK_JSON)])

    orig_lock = obs_repo.lock_lease_for_commit

    def steal_then_lock(cur2, event_id, token):
        _steal_lease()   # 模拟:模型返回后 Phase 3 取锁前被另一 worker 重领
        return orig_lock(cur2, event_id, token)   # 原 token 已被覆盖 → False
    monkeypatch.setattr(obs_repo, "lock_lease_for_commit", steal_then_lock)

    r = asyncio.run(process_next_pending(lease_seconds=3))   # trusted_exact → 首笔付费 = outcome 模型
    assert len(posts) == 1, f"outcome 模型应恰 1 笔 httpx.post、无第二笔,实为 {len(posts)}"
    assert r["state"] == "lease_lost", f"Phase-3 锁校验应零写 lease_lost,实为 {r}"
    assert _anchor_of("r6_p3") is not None, "首笔付费 POST 前应已落 paid_call_started_at"


def test_reclaim_after_paid_call_routes_manual_no_repay(monkeypatch):
    """P1(test-d · 付费幂等状态机):已发起付费调用(paid_call_started_at 非空)的事件被重领 → 一律路由人工
    pending_review(paid_call_result_unknown),**绝不自动再次付费**(httpx.post 零次)。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 611, "大昀装修", 124)
    H.seed_diagnosis(conn, "r6_reclaim", owner=124, brand=611, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r6_reclaim", H.now())
    cur.execute("UPDATE geo_observation_events SET paid_call_started_at=NOW(), processing_state='pending', "
                "lease_token=NULL, lease_until=NULL WHERE source_record_id='r6_reclaim'")
    conn.commit(); conn.close()

    posts = _install_fake_deepseek(monkeypatch, keys=["k1"], behaviors=[lambda: _FakeResp(200, _OUTCOME_OK_JSON)])

    r = asyncio.run(process_next_pending(lease_seconds=30))
    assert len(posts) == 0, f"重领已发起付费调用的事件绝不再付费,httpx.post 应为 0,实为 {len(posts)}"
    assert r["state"] == "pending_review", f"应路由人工 pending_review,实为 {r}"
    assert "paid_call_result_unknown" in (r.get("reasons") or []), f"应带 paid_call_result_unknown,实为 {r}"


def test_readtimeout_no_failover_second_key_routes_manual(monkeypatch):
    """P1(test-f · req#5):两 key,第一笔 ReadTimeout(响应未知,服务端可能已计费)→ **绝不换 key 重发第二笔** →
    转人工 pending_review(paid_call_result_unknown)。真实 helper:驱动 _paid_safe_deepseek_post 的多 key 循环。"""
    import httpx
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 613, "大昀装修", 124)
    H.seed_diagnosis(conn, "r6_rt", owner=124, brand=613, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r6_rt", H.now()); conn.commit(); conn.close()

    def b_timeout():
        raise httpx.ReadTimeout("read timeout")   # 响应未知
    posts = _install_fake_deepseek(monkeypatch, keys=["k1", "k2"],
                                   behaviors=[b_timeout, lambda: _FakeResp(200, _OUTCOME_OK_JSON)])

    r = asyncio.run(process_next_pending(lease_seconds=30))   # trusted_exact → 首笔付费 = outcome 模型
    assert len(posts) == 1, f"ReadTimeout 后绝不换 key 重发第二笔,总 httpx.post 应为 1,实为 {len(posts)}"
    assert r["state"] == "pending_review", f"响应未知应转人工 pending_review,实为 {r}"
    assert "paid_call_result_unknown" in (r.get("reasons") or []), f"应带 paid_call_result_unknown,实为 {r}"
    assert _anchor_of("r6_rt") is not None, "响应未知前守卫已落耐久锚(重领亦转人工)"


def test_no_key_zero_post_and_not_paid_call_unknown(monkeypatch):
    """P1(test-g · req#6):无 key → 真实 httpx.post 为 0,**不进 paid_call_result_unknown**、**不写耐久锚**
    (请求根本未发出=未计费,不是"响应未知")。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 614, "大昀装修", 124)
    H.seed_diagnosis(conn, "r6_nokey", owner=124, brand=614, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r6_nokey", H.now()); conn.commit(); conn.close()

    posts = _install_fake_deepseek(monkeypatch, keys=[], behaviors=[lambda: _FakeResp(200, _OUTCOME_OK_JSON)])

    r = asyncio.run(process_next_pending(lease_seconds=30))
    assert len(posts) == 0, f"无 key → 真实 httpx.post 应为 0,实为 {len(posts)}"
    assert "paid_call_result_unknown" not in (r.get("reasons") or []), \
        f"无 key 未计费,不得转 paid_call_result_unknown,实为 {r}"
    assert _anchor_of("r6_nokey") is None, "无 key 请求未发出,绝不写耐久锚"


def test_guard_leaselost_not_swallowed_by_resolver(monkeypatch):
    """P1 不变量锁定(第6次 NO-GO 复审 P2):LeaseLost **必须是 BaseException** —— verifier httpx.post 前守卫 raise 的
    LeaseLost 不得被 BrandIdentityResolver.resolve() 的宽 except Exception 洗白成 UNKNOWN 后继续。
    判别:守卫在 verifier POST 触发 → BaseException 直接上抛则 resolve_entity 之后的 classify_outcome **不被调用**;
    若被洗白成 Exception,resolver 返回 UNKNOWN→resolve_entity 返回→classify_outcome 会被调用。故断言其零调用
    (翻回 Exception 本测试即失败,不再靠 Phase-3 兜底掩盖差异)。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 612, "晨光富士电梯", 124, industry="电梯")
    H.seed_diagnosis(conn, "r6_swallow", owner=124, brand=612, run_status="committed", visibility="published",
                     question="电梯哪家好", engine="deepseek", answer=_CONFUSE_ANS)
    source_hooks.register_paid_diagnosis(cur, "r6_swallow", H.now()); conn.commit(); conn.close()

    posts = _install_fake_deepseek(monkeypatch, keys=["k1"], behaviors=[lambda: _FakeResp(200, _VERIF_NO_JSON)])

    classify_calls = []
    orig_classify = promotion.classify_outcome

    def counting_classify(*a, **k):
        classify_calls.append(1)
        return orig_classify(*a, **k)
    monkeypatch.setattr(promotion, "classify_outcome", counting_classify)

    orig_resolve = entity_review.resolve_entity

    async def stealing_resolve(*args, **kwargs):
        _steal_lease()   # 偷租 → verifier httpx.post 前守卫将 raise LeaseLost
        return await orig_resolve(*args, **kwargs)
    monkeypatch.setattr(promotion, "resolve_entity", stealing_resolve)

    r = asyncio.run(process_next_pending(lease_seconds=3))
    assert len(posts) == 0, f"守卫应在真实 verifier httpx.post 前 raise,POST 应为 0,实为 {len(posts)}"
    assert len(classify_calls) == 0, (
        "守卫 LeaseLost 被 resolver 洗白后继续走到了 classify_outcome —— LeaseLost 必须是 BaseException 直接上抛,"
        f"不得被 resolver 的 except Exception 吞没(classify_outcome 被调用 {len(classify_calls)} 次)")
    assert r["state"] == "lease_lost", f"应直接 lease_lost,实为 {r}"


def test_verifier_readtimeout_unknown_not_swallowed_by_resolver(monkeypatch):
    """P1 不变量锁定:ProviderResultUnknown **必须是 BaseException** —— **verifier** 路径的 ReadTimeout(响应未知,
    可能已计费)不得被 BrandIdentityResolver.resolve() 的宽 except Exception 洗白成 UNKNOWN 而错失转人工。
    混淆品牌 → verifier httpx.post ReadTimeout → 直接上抛穿透 resolver → pending_review(paid_call_result_unknown)。
    判别:翻回 Exception 则 resolver 洗白 → verifier UNKNOWN → 非 paid_call_result_unknown → 本测试失败。"""
    import httpx
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 615, "晨光富士电梯", 124, industry="电梯")
    H.seed_diagnosis(conn, "r6_vrt", owner=124, brand=615, run_status="committed", visibility="published",
                     question="电梯哪家好", engine="deepseek", answer=_CONFUSE_ANS)
    source_hooks.register_paid_diagnosis(cur, "r6_vrt", H.now()); conn.commit(); conn.close()

    def b_timeout():
        raise httpx.ReadTimeout("read timeout")
    posts = _install_fake_deepseek(monkeypatch, keys=["k1"], behaviors=[b_timeout])

    r = asyncio.run(process_next_pending(lease_seconds=30))
    assert len(posts) == 1, f"verifier 首笔 httpx.post 应恰 1(ReadTimeout 不换 key),实为 {len(posts)}"
    assert r["state"] == "pending_review", (
        f"verifier 响应未知应穿透 resolver 转人工 —— ProviderResultUnknown 必须是 BaseException(否则被洗白),实为 {r}")
    assert "paid_call_result_unknown" in (r.get("reasons") or []), f"应带 paid_call_result_unknown,实为 {r}"


def _install_fake_llm_track(monkeypatch):
    """替换 tools.llm_call_tracker.llm_track,忠实复刻真实语义(metadata 合并**拷贝**为 tracker.metadata,
    finally 落库的是 tracker.metadata 而非调用方原 dict)。返回捕获的 tracker 列表(每笔真实 POST 一个)。"""
    from contextlib import asynccontextmanager
    import tools.llm_call_tracker as _tracker_mod

    captured = []

    class _FakeTracker:
        def __init__(self, metadata):
            self.metadata = dict(metadata or {})   # 复刻 _apply_tracking_context 的合并拷贝语义
            self.records = []

        def record(self, **kw):
            self.records.append(kw)

    @asynccontextmanager
    async def fake_llm_track(caller, platform, *, model=None, metadata=None, **kw):
        t = _FakeTracker(metadata)
        captured.append(t)
        yield t

    monkeypatch.setattr(_tracker_mod, "llm_track", fake_llm_track)
    return captured


def test_paid_call_log_carries_event_correlation_and_unknown_mark(monkeypatch):
    """P2(核账关联):每笔真实付费 POST 的 llm_track metadata 必须携带非敏感 event_id/request_id/
    call_purpose/attempt_no;响应未知(ReadTimeout)那笔在**同一记账行**标 provider_result_unknown=true ——
    并发多个 paid_call_result_unknown 事件时可与成本日志逐笔精确对账(不再按时间猜)。
    判别力:去掉 track_ctx 线程 → metadata 缺 event_id → 失败;去掉 unknown 标记 → 缺 provider_result_unknown → 失败。"""
    import httpx
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 616, "大昀装修", 124)
    H.seed_diagnosis(conn, "r6_meta", owner=124, brand=616, run_status="committed", visibility="published", answer=LONG)
    source_hooks.register_paid_diagnosis(cur, "r6_meta", H.now())
    cur.execute("SELECT id FROM geo_observation_events WHERE source_record_id='r6_meta'")
    event_id = cur.fetchone()["id"]
    conn.commit(); conn.close()

    trackers = _install_fake_llm_track(monkeypatch)

    def b_timeout():
        raise httpx.ReadTimeout("read timeout")
    _install_fake_deepseek(monkeypatch, keys=["k1", "k2"], behaviors=[b_timeout])

    r = asyncio.run(process_next_pending(lease_seconds=30))   # trusted_exact → 首笔付费 = outcome 模型
    assert r["state"] == "pending_review" and "paid_call_result_unknown" in r["reasons"], f"前置:应转人工,实为 {r}"
    assert len(trackers) == 1, f"应恰 1 笔记账(ReadTimeout 不换 key),实为 {len(trackers)}"
    md = trackers[0].metadata
    assert md.get("event_id") == event_id, f"metadata 应携带 event_id={event_id},实为 {md}"
    assert str(md.get("request_id", "")).startswith("promote:"), f"metadata 应携带 request_id(promote:*),实为 {md}"
    assert md.get("call_purpose") == "outcome_classify", f"metadata 应标 call_purpose,实为 {md}"
    assert md.get("attempt_no") == 1, f"metadata 应标 attempt_no=1,实为 {md}"
    assert md.get("provider_result_unknown") is True, (
        f"响应未知那笔必须在同一记账行标 provider_result_unknown=true(供人工精确对账),实为 {md}")


def test_paid_call_log_verifier_purpose_and_no_unknown_on_success(monkeypatch):
    """P2(核账关联·正常路径):混淆品牌 → verifier 付费 POST 成功(200)→ metadata 标 call_purpose=entity_verify
    + event_id/attempt_no,且**无** provider_result_unknown(只有响应未知才标)。"""
    _set_gov(promotion_enabled=True, gold=True)
    conn = get_connection(); cur = conn.cursor()
    H.seed_brand(conn, 617, "晨光富士电梯", 124, industry="电梯")
    H.seed_diagnosis(conn, "r6_meta2", owner=124, brand=617, run_status="committed", visibility="published",
                     question="电梯哪家好", engine="deepseek", answer=_CONFUSE_ANS)
    source_hooks.register_paid_diagnosis(cur, "r6_meta2", H.now())
    cur.execute("SELECT id FROM geo_observation_events WHERE source_record_id='r6_meta2'")
    event_id = cur.fetchone()["id"]
    conn.commit(); conn.close()

    trackers = _install_fake_llm_track(monkeypatch)
    _install_fake_deepseek(monkeypatch, keys=["k1"], behaviors=[lambda: _FakeResp(200, _VERIF_NO_JSON)])

    r = asyncio.run(process_next_pending(lease_seconds=30))
    assert len(trackers) == 1, f"verifier 应恰 1 笔记账,实为 {len(trackers)}"
    md = trackers[0].metadata
    assert md.get("event_id") == event_id and md.get("call_purpose") == "entity_verify" and md.get("attempt_no") == 1, \
        f"verifier 记账应携带 event_id/entity_verify/attempt_no=1,实为 {md}"
    assert "provider_result_unknown" not in md, f"成功响应不得标 provider_result_unknown,实为 {md}"
    assert r["state"] != "pending_review" or "paid_call_result_unknown" not in (r.get("reasons") or []), \
        f"成功路径不应转付费未知态,实为 {r}"


# ═══════════════ P1-3:弱化 CHECK 假绿(readiness 强校验) ═══════════════
def _remutate_outcome(conn, check_body):
    cur = conn.cursor()
    cur.execute("ALTER TABLE geo_observation_signals DROP CONSTRAINT chk_geo_obs_signal_outcome")
    cur.execute("ALTER TABLE geo_observation_signals ADD CONSTRAINT chk_geo_obs_signal_outcome CHECK (%s)" % check_body)
    conn.commit()


def _restore_outcome(conn):
    cur = conn.cursor()
    cur.execute("ALTER TABLE geo_observation_signals DROP CONSTRAINT IF EXISTS chk_geo_obs_signal_outcome")
    cur.execute("ALTER TABLE geo_observation_signals ADD CONSTRAINT chk_geo_obs_signal_outcome CHECK (%s)" % _OUTCOME_CANON)
    conn.commit()


@pytest.mark.parametrize("weakened,label", [
    ("TRUE OR " + _OUTCOME_CANON, "tautology_or_true"),
    (_OUTCOME_CANON[:-1] + ",'evil_extra')", "extra_illegal_enum"),
    (_OUTCOME_CANON.replace("target_outcome", "sentiment"), "wrong_col"),
    ("(" + _OUTCOME_CANON + ") IS NOT NULL", "tautology_is_not_null"),   # 白名单拦(黑名单会漏)
    (_OUTCOME_CANON + " AND TRUE", "tautology_and_true"),
    ("target_outcome <> ANY(" + _OUTCOME_ARRAY + ")", "operator_swap_neq"),   # 恒真:骨架含 <>
    ("target_outcome >= ANY(" + _OUTCOME_ARRAY + ")", "operator_swap_gte"),
    ("target_outcome = ANY(ARRAY[target_outcome," + ",".join("'%s'" % e for e in _OUTCOME_ENUMS) + "])",
     "self_ref_tautology"),   # x=ANY(ARRAY[x,...]) 恒真:骨架含额外 col 引用
])
def test_readiness_catches_weakened_outcome_check(weakened, label):
    """substring 假绿三攻击(恒真/额外枚举/错列)必须被 readiness 强校验拦下。"""
    conn = get_connection()
    _remutate_outcome(conn, weakened)
    try:
        with pytest.raises(readiness.ObservationSchemaNotReady):
            readiness.verify_geo_observation_schema(conn.cursor())
    finally:
        _restore_outcome(conn); conn.close()


def test_readiness_passes_on_canonical_outcome_check():
    """正例:规范 CHECK 下 readiness 不误报(强校验不 false-positive)。"""
    conn = get_connection()
    _restore_outcome(conn)   # 确保规范
    try:
        readiness.verify_geo_observation_schema(conn.cursor())   # 不抛
    finally:
        conn.close()
