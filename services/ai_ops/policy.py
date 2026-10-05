"""
AI Ops 策略 / 风险分级 · 2026-07-01

集中回答:某任务允许 Worker 领取吗?某动作要不要审批?风险几级?Kill Switch 开了吗?

风险分级(设计 §8):
  L0 只读诊断     · 无需审批
  L1 代码修复     · worktree 改代码 + 跑测试 + 出 diff;合并主分支要审批
  L2 生产只读     · 容器状态 / tail 日志 / 只读 SQL
  L3 低风险执行   · 重启非核心 worker / 健康检查 / 清临时任务;执行前审批
  L4 高风险执行   · 部署/回滚/DB写/钱包/退款/结算/提现/nginx/env;无自动执行,必审批

资金红线 & 生产红线:任何 L4 一律人工审批,AI 只出方案不执行。
"""

import re
from typing import Optional

from db import ai_ops_db as aiops_db

RISK_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}

# 红线文件:diff 一旦触及这些,一律升到 L4 人工审批(与 CLAUDE.md 红线一致)
RED_LINE_FILES = (
    "middleware/billing.py", "db/connection.py", "auth/middleware.py", "auth/jwt_utils.py",
)

# 修复 diff 内容风险扫描(只扫新增行 + 文件头)
_DIFF_RISK_PATTERNS = [
    ("fund", re.compile(r"(?i)wallet|refund|settlement|withdraw|commission|钱包|退款|结算|提现|佣金")),
    ("db_write", re.compile(r"(?i)\b(insert|update|delete|drop|alter|truncate)\b")),
    ("deploy", re.compile(r"(?i)docker\s+(compose|restart|build)|nginx|blue.?green|deploy|rollback")),
    ("secret", re.compile(r"(?i)\.env\b|\.pem\b|api[_-]?key|secret|apiv3|password|token")),
]


def scan_diff_risks(diff: str) -> list[str]:
    """
    AI Ops 校验:扫 Codex 产出的 git diff,返回命中的风险标签
    (red_line:<file> / fund / db_write / deploy / secret)。空列表=未命中。
    命中即意味着这份 diff 合并前必须人工审批(升 L4)。
    """
    if not diff:
        return []
    risks: list[str] = []
    header_text = "\n".join(
        ln for ln in diff.splitlines()
        if ln.startswith("+++ ") or ln.startswith("--- ") or ln.startswith("diff --git")
    )
    for f in RED_LINE_FILES:
        if f in header_text:
            risks.append(f"red_line:{f}")
    added = "\n".join(
        ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")
    )
    for label, pattern in _DIFF_RISK_PATTERNS:
        if pattern.search(added):
            risks.append(label)
    return sorted(set(risks))

# 动作类型 → 风险等级
ACTION_RISK = {
    "diagnose": "L0",
    "code_review": "L0",
    "report": "L0",
    "fix": "L1",
    "prod_status": "L2",
    "log_tail": "L2",
    "readonly_sql": "L2",
    "health_check": "L2",
    "restart_worker": "L3",
    "cleanup_temp": "L3",
    "ssh_command": "L3",
    # 以下一律 L4:无自动执行
    "deploy": "L4",
    "rollback": "L4",
    "db_write": "L4",
    "nginx": "L4",
    "env_change": "L4",
    "refund": "L4",
    "wallet_adjust": "L4",
    "settlement": "L4",
    "withdrawal": "L4",
    "commission": "L4",
}

# Worker(Runner)默认只领取这些 kind。
# 注意:report 不在其中(P1-2)——日报要读 faq_feedback 等业务表,而 Runner DB 账号只授权
# ai_ops_*;日报改为在 Web 容器同步生成(scheduler / 手动 API / 命令台),不由 Runner 跑。
WORKER_DEFAULT_KINDS = ["diagnose", "fix", "code_review"]

# 资金红线动作:永远 L4,永远不自动执行
FUND_ACTIONS = frozenset({"refund", "wallet_adjust", "settlement", "withdrawal", "commission", "db_write"})


def classify_risk(action_type: str) -> str:
    """未知动作按最高危 L4 处理(fail-safe)。"""
    return ACTION_RISK.get(action_type, "L4")


def requires_approval(action_type: str) -> bool:
    """L3/L4 需要审批;资金红线动作必审批。"""
    if action_type in FUND_ACTIONS:
        return True
    return RISK_ORDER.get(classify_risk(action_type), 4) >= RISK_ORDER["L3"]


def is_auto_executable(action_type: str) -> bool:
    """能否自动执行(不需要审批)。L4 一律 False。"""
    return not requires_approval(action_type)


def is_kill_switch_enabled() -> bool:
    return aiops_db.is_kill_switch_enabled()


def is_ai_ops_enabled() -> bool:
    return aiops_db.is_flag_enabled("ai_ops.enabled", default=False)


def can_worker_claim(task: dict) -> tuple[bool, str]:
    """Worker 能否领取执行该任务。返回 (allowed, reason)。"""
    if is_kill_switch_enabled():
        return False, "kill_switch_on"
    if not is_ai_ops_enabled():
        return False, "ai_ops_disabled"   # 总开关 · 默认 false 时 Worker 不自动执行任何任务
    kind = task.get("kind")
    if kind == "report":
        # 日报只在 Web 容器同步生成(要读 faq_feedback 等业务表),Runner 永不领(P1-2)
        return False, "report_web_only"
    if kind == "fix" and not aiops_db.is_flag_enabled("codex.fix.enabled", default=False):
        return False, "codex_fix_disabled"
    if kind == "diagnose" and not aiops_db.is_flag_enabled("codex.diagnose.enabled", default=True):
        return False, "codex_diagnose_disabled"
    if kind == "ssh_action" and not aiops_db.is_flag_enabled("ssh_runner.enabled", default=False):
        return False, "ssh_runner_disabled"
    return True, "ok"


def can_approve(user: Optional[dict], approval: dict) -> bool:
    """
    谁能批:L4(资金/部署/回滚/DB写)只有超级管理员/老板(is_admin)。
    L3 普通管理员也可批。本项目 admin 前端多以 is_admin 为主,这里保守要求 is_admin。
    """
    if not user or not user.get("is_admin"):
        return False
    return True
