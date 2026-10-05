"""[策略/绑定 · 包② · 2026-08-01] 工单 §3F 锁 · 行为级。

锁① 绑定候选**必须带 status='candidate' 切片**(全表 7095 vs 待确认 1390)
锁② 策略评估 fail-closed + 自相矛盾从严
锁③ 冲突项必须带 rule + evidence(禁笼统)
锁④ 🔴 自动激活默认开(Owner 2026-08-01 拍板)+ 反向:必须收得回来
锁⑤ 报告落库幂等(同 strategy + 同 reviewer 只一条)
"""
import json
import os
import pathlib

import psycopg2
import psycopg2.extras
import pytest

from services import article_ai_review as _air
from services import strategy_review_ai as sra

_REPO = pathlib.Path(__file__).resolve().parents[1]
_MIGRATION = _REPO / "scripts" / "migration_strategy_ai_review_2026_08_01.sql"

_DDL = """
CREATE TEMP TABLE geo_media_binding_candidates (
  id BIGSERIAL PRIMARY KEY,
  candidate_key VARCHAR(200), entity_key VARCHAR(200), industry_key VARCHAR(100),
  media_source VARCHAR(60), media_name TEXT, inventory_url TEXT,
  match_method VARCHAR(60), match_confidence NUMERIC(6,4),
  can_approve BOOLEAN, risk_flags JSONB, status VARCHAR(40), active BOOLEAN DEFAULT TRUE
) ON COMMIT DROP;
"""


@pytest.fixture(scope="module")
def _migrate():
    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    try:
        conn.autocommit = True
        conn.cursor().execute(_MIGRATION.read_text(encoding="utf-8"))
    finally:
        conn.close()


@pytest.fixture()
def cur(_migrate):
    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"],
                            cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        c = conn.cursor()
        c.execute(_DDL)
        yield c
    finally:
        conn.rollback()
        conn.close()


def _stub(monkeypatch, payload: dict):
    monkeypatch.setattr(_air, "_deepseek_key", lambda: "sk-test-not-real")
    monkeypatch.setattr(_air, "_post_chat", lambda *a: {"choices": [{"message": {
        "content": json.dumps(payload, ensure_ascii=False)}}]})


# ===========================================================================
# 锁① 绑定候选切片
# ===========================================================================
def test_lock1_binding_candidates_use_the_candidate_slice(cur):
    """🔴 生产实测:全表 7095 / 待确认 1390。不带切片会多铺 5705 条,
    其中 5564 条是 already approved —— 等于把已确认的再确认一遍。"""
    rows = [
        ("candidate", True), ("candidate", True), ("candidate", True),
        ("approved", True), ("approved", True),
        ("deleted", True),
        ("candidate", False),          # 已停用,不该入选
    ]
    for i, (status, active) in enumerate(rows, start=1):
        cur.execute(
            "INSERT INTO geo_media_binding_candidates (candidate_key, status, active) "
            "VALUES (%s,%s,%s)", (f"k{i}", status, active),
        )
    assert sra.count_binding_candidates(cur) == 3, "只有 active 的 candidate 该入选"

    # 🔴 反向对照:全表远多于切片 —— 证明切片真的在筛,不是恒等
    cur.execute("SELECT COUNT(*) AS n FROM geo_media_binding_candidates")
    assert int(cur.fetchone()["n"]) == len(rows) > 3


def test_lock1b_slice_condition_is_present_in_sql():
    """判据写在 SQL 里,别让谁顺手删掉 WHERE。"""
    body = "\n".join(
        l for l in sra.BINDING_CANDIDATE_SQL.split("\n") if not l.strip().startswith("--")
    )
    assert "status = 'candidate'" in body


# ===========================================================================
# 锁② fail-closed + 自相矛盾从严
# ===========================================================================
def test_lock2_fail_closed_on_call_failure(monkeypatch):
    monkeypatch.setattr(_air, "_deepseek_key", lambda: "sk-test-not-real")

    def _boom(*a):
        raise RuntimeError("down")

    monkeypatch.setattr(_air, "_post_chat", _boom)
    out = sra.evaluate_strategy_version({"guidance": "随便"})
    assert out["verdict"] == sra.VERDICT_NOT_CHECKED
    assert out["verdict"] != sra.VERDICT_PASS, "调用失败不得被当成通过"


def test_lock2b_self_contradicting_pass_is_downgraded(monkeypatch):
    """🔴 说 pass 却列了冲突 = 自相矛盾,一律从严按 reject。

    这条是刻意的:策略一旦激活会影响此后**所有**文章,
    模型含糊时宁可挡下重评,也不赌它"其实是想说通过"。
    """
    _stub(monkeypatch, {
        "verdict": "pass", "summary": "看起来还行",
        "conflicts": [{"rule": "广告法绝对化", "evidence": "策略要求写「行业第一」"}],
    })
    out = sra.evaluate_strategy_version({"guidance": "写行业第一"})
    assert out["verdict"] == sra.VERDICT_REJECT
    assert out["conflicts"]


def test_lock2c_clean_pass_is_still_a_pass(monkeypatch):
    """🔴 与 2b 成对:干净的策略必须真能通过。
    只有 2b 的话,把 verdict 恒设 reject 也全绿 —— 那是把功能做没了。"""
    _stub(monkeypatch, {"verdict": "pass", "summary": "无冲突且有数据支撑", "conflicts": []})
    out = sra.evaluate_strategy_version({"guidance": "披露排序依据"})
    assert out["verdict"] == sra.VERDICT_PASS
    assert out["conflicts"] == []


# ===========================================================================
# 锁③ 冲突项禁笼统
# ===========================================================================
def test_lock3_conflict_without_evidence_is_dropped(monkeypatch):
    _stub(monkeypatch, {
        "verdict": "reject", "summary": "有风险",
        "conflicts": [{"rule": "广告法", "evidence": ""}],   # 无落点
    })
    out = sra.evaluate_strategy_version({"guidance": "x"})
    assert out["verdict"] == sra.VERDICT_NOT_CHECKED, "笼统结论不作数"


def test_lock3b_conflict_with_evidence_is_kept(monkeypatch):
    _stub(monkeypatch, {
        "verdict": "reject", "summary": "命中绝对化",
        "conflicts": [
            {"rule": "广告法绝对化", "evidence": "「本品牌是行业第一」"},
            {"rule": "", "evidence": "缺 rule 的这条应被丢弃"},
        ],
    })
    out = sra.evaluate_strategy_version({"guidance": "x"})
    assert out["verdict"] == sra.VERDICT_REJECT
    assert len(out["conflicts"]) == 1
    assert "行业第一" in out["conflicts"][0]["evidence"]


# ===========================================================================
# 锁④ 🔴 自动激活默认开(Owner 拍板)· 反向:能一个环境变量收回
# ===========================================================================
def test_lock4_autoactivate_is_on_by_default(monkeypatch):
    """🔴 [Owner 2026-08-01 拍板] 默认**开**。

    背景:§3F"通过即激活"与 §1"平台侧规则变更保留一键确认"前后矛盾
    (复审确认是工单自身 v2→v3 改写没扫全文)。我首版按边界从严交了默认关
    并把理由摆出来,Owner 看过后决定打开 —— 按其裁定执行。
    改规则必同步改测试:本锁随之翻面。"""
    monkeypatch.delenv("STRATEGY_AI_AUTOACTIVATE", raising=False)
    assert sra.autoactivate_enabled() is True


def test_lock4b_autoactivate_can_be_turned_off(monkeypatch):
    """🔴 反向:必须**收得回来**。

    默认开之后,"能不能临时退回人工确认"就成了唯一的刹车 ——
    真出事时要能一个环境变量停掉,不必回滚代码。"""
    monkeypatch.setenv("STRATEGY_AI_AUTOACTIVATE", "false")
    assert sra.autoactivate_enabled() is False
    monkeypatch.setenv("STRATEGY_AI_AUTOACTIVATE", "0")
    assert sra.autoactivate_enabled() is False


def test_lock4c_reviewer_id_is_the_system_actor_not_a_string():
    """🔴 `writing_strategy_versions.reviewed_by` 是 BIGINT。
    工单原话"写 reviewed_by='ai:deepseek-chat@…'"类型上不可能 ——
    reviewer 串落报告表,主表按既有系统 actor 约定写 0。"""
    assert isinstance(sra.SYSTEM_ACTOR_ID, int)
    assert sra.STRATEGY_REVIEWER.startswith("ai:")


# ===========================================================================
# 锁⑤ 报告落库幂等
# ===========================================================================
def test_lock5_report_persist_is_idempotent(cur):
    v1 = {"verdict": "reject", "summary": "第一次", "conflicts": [{"rule": "r", "evidence": "e"}]}
    v2 = {"verdict": "pass", "summary": "重评后通过", "conflicts": []}
    id1 = sra.persist_strategy_review(cur, 77, v1)
    id2 = sra.persist_strategy_review(cur, 77, v2)
    assert id1 == id2, "同 strategy + 同 reviewer 必须是同一行(upsert)"

    cur.execute(
        "SELECT COUNT(*) AS n FROM geo_strategy_ai_review_reports WHERE strategy_id=77")
    assert int(cur.fetchone()["n"]) == 1

    cur.execute(
        "SELECT verdict, summary FROM geo_strategy_ai_review_reports WHERE strategy_id=77")
    row = cur.fetchone()
    assert row["verdict"] == "pass" and row["summary"] == "重评后通过", "重评应覆盖旧结论"
