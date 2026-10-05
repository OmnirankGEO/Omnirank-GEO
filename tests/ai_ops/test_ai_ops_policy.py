"""services/ai_ops/policy.py 测试。"""
import pytest

from db import ai_ops_db as aiops_db
from services.ai_ops import policy


# ---- 纯函数(无 DB) ----

def test_classify_risk():
    assert policy.classify_risk("diagnose") == "L0"
    assert policy.classify_risk("fix") == "L1"
    assert policy.classify_risk("restart_worker") == "L3"
    assert policy.classify_risk("deploy") == "L4"
    assert policy.classify_risk("unknown_action") == "L4"   # fail-safe 最高危


def test_requires_approval():
    assert policy.requires_approval("diagnose") is False
    assert policy.requires_approval("readonly_sql") is False   # L2
    assert policy.requires_approval("restart_worker") is True  # L3
    assert policy.requires_approval("deploy") is True          # L4
    assert policy.requires_approval("refund") is True          # 资金红线
    assert policy.requires_approval("wallet_adjust") is True


def test_report_not_in_worker_kinds():
    # P1-2:report 不给 Runner 领(在 Web 容器同步生成)
    assert "report" not in policy.WORKER_DEFAULT_KINDS
    assert "diagnose" in policy.WORKER_DEFAULT_KINDS
    assert "fix" in policy.WORKER_DEFAULT_KINDS


def test_is_auto_executable():
    assert policy.is_auto_executable("diagnose") is True
    assert policy.is_auto_executable("deploy") is False
    assert policy.is_auto_executable("refund") is False


def test_can_approve_requires_admin():
    assert policy.can_approve({"is_admin": True}, {"risk_level": "L4"}) is True
    assert policy.can_approve({"is_admin": False}, {"risk_level": "L3"}) is False
    assert policy.can_approve(None, {"risk_level": "L4"}) is False


# ---- can_worker_claim / kill switch(DB) ----

def test_chat_llm_policy_seeded_default_off(clean_ai_ops):
    # brief B.1:迁移种子必须含 ai_ops.chat_llm.enabled 且默认 false
    # (幂等 ON CONFLICT DO NOTHING;跑过旧迁移的环境重启后自动补上)
    policies = aiops_db.get_policies()
    assert policies.get('ai_ops.chat_llm.enabled') == {"enabled": False}
    assert aiops_db.is_flag_enabled('ai_ops.chat_llm.enabled') is False


def test_can_worker_claim_blocked_by_kill_switch(clean_ai_ops):
    aiops_db.set_policy("ai_ops.enabled", {"enabled": True})
    aiops_db.set_policy("ai_ops.kill_switch", {"enabled": True})
    ok, reason = policy.can_worker_claim({"kind": "diagnose"})
    assert ok is False and reason == "kill_switch_on"


def test_can_worker_claim_blocked_when_ai_ops_disabled(clean_ai_ops):
    # 默认 ai_ops.enabled=false → 总开关挡住,Worker 不自动执行任何任务(P0-2)
    ok, reason = policy.can_worker_claim({"kind": "diagnose"})
    assert ok is False and reason == "ai_ops_disabled"


def test_can_worker_claim_fix_disabled_even_when_enabled(clean_ai_ops):
    aiops_db.set_policy("ai_ops.enabled", {"enabled": True})   # 开总开关
    ok, reason = policy.can_worker_claim({"kind": "fix"})       # 但 codex.fix 默认关
    assert ok is False and reason == "codex_fix_disabled"


def test_can_worker_claim_diagnose_allowed_when_enabled(clean_ai_ops):
    aiops_db.set_policy("ai_ops.enabled", {"enabled": True})
    ok, reason = policy.can_worker_claim({"kind": "diagnose"})
    assert ok is True and reason == "ok"


def test_can_worker_claim_rejects_report(clean_ai_ops):
    # P1-2:即使开了总开关,Runner 也不领 report(Web 容器同步生成)
    aiops_db.set_policy("ai_ops.enabled", {"enabled": True})
    ok, reason = policy.can_worker_claim({"kind": "report"})
    assert ok is False and reason == "report_web_only"


# ---- scan_diff_risks(AI Ops 校验 · 纯函数) ----

def test_scan_diff_risks_clean():
    diff = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1,2 @@\n+def hello():\n+    return 1\n"
    assert policy.scan_diff_risks(diff) == []


def test_scan_diff_risks_red_line_file():
    diff = "diff --git a/middleware/billing.py b/middleware/billing.py\n+++ b/middleware/billing.py\n@@\n+x = 1\n"
    risks = policy.scan_diff_risks(diff)
    assert any(r.startswith("red_line:middleware/billing.py") for r in risks)


def test_scan_diff_risks_fund_and_db_write():
    diff = "+++ b/foo.py\n@@\n+cursor.execute('DELETE FROM users')\n+refund_amount = 100\n"
    risks = policy.scan_diff_risks(diff)
    assert "db_write" in risks
    assert "fund" in risks
