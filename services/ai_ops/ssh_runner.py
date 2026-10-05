"""
AI Ops SSH Runner · 2026-07-01

受控生产动作执行器。第一版只实现框架 + 审批状态 + allowlist 校验 + 安全门,
默认不接真实 SSH(只读命令可后续接)。

安全门(CTO 审核 P1-5 · 每条动作执行前都要过):
  0. 先查 Kill Switch:开启直接跳过 + 写 security 事件(急停对最危险路径也生效)。
  1. approval 必须 approved 且未过期。
  2. task 未取消。
  3. action_type 命中 allowlist,命令参数化,绝不字符串拼接用户输入。
  4. 重启/部署/回滚/DB写/nginx/env 第一版不进 allowlist,只生成审批计划,不执行。
  5. 命令、输出、审批人、时间全部写审计(事件 + artifact)。

生产容器名 / active port 必须从部署事实/配置读,不硬编码不存在的容器或端点。
"""

import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional

from db import ai_ops_db as aiops_db
from services.ai_ops import policy, redaction

logger = logging.getLogger("AiOps-SSHRunner")

# 只读 allowlist(第一版只允许这些只读动作;命令模板用占位符,由 command_plan 提供参数)
# 生产容器名 / active port 从环境读,不硬编码。
def _active_container() -> str:
    return os.getenv("AI_OPS_ACTIVE_CONTAINER", "")


def _active_port() -> str:
    return os.getenv("AI_OPS_ACTIVE_PORT", "")


READONLY_ALLOWLIST = {
    "prod_status": ["docker", "ps", "--format", "{{.Names}} {{.Status}}"],
    "log_tail": ["docker", "logs", "--tail", "{n}", "{container}"],
    "health_check": ["curl", "-fsS", "http://127.0.0.1:{port}/api/settings/health-check"],
}

# 明确不进第一版 allowlist(只生成审批计划,不执行)
NON_EXECUTABLE_ACTIONS = frozenset({
    "restart_worker", "deploy", "rollback", "db_write", "nginx", "env_change",
    "refund", "wallet_adjust", "settlement", "withdrawal", "commission",
})


class ApprovalNotExecutable(Exception):
    pass


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_expired(approval: dict) -> bool:
    exp = approval.get("expires_at")
    if not exp:
        return False
    if isinstance(exp, str):
        try:
            exp = datetime.fromisoformat(exp)
        except ValueError:
            return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return _now_utc() > exp


def can_execute(approval: dict) -> tuple[bool, str]:
    """执行前的全部安全门(每条动作都要过)。返回 (ok, reason)。"""
    # 0. Kill Switch 第一优先
    if policy.is_kill_switch_enabled():
        return False, "kill_switch_on"
    # 1. 已执行过 → 幂等拒绝(执行后 status=executed 且 executed_at 打点)
    if approval.get("executed_at") or approval.get("approval_status") == "executed":
        return False, "already_executed"
    # 2. approved + 未过期
    if approval.get("approval_status") != "approved":
        return False, "not_approved"
    if is_expired(approval):
        return False, "expired"
    # 2. task 未取消
    task = aiops_db.get_task(approval.get("task_id"))
    if not task or task.get("status") == "cancelled":
        return False, "task_cancelled"
    # 3. action_type 可执行
    action_type = approval.get("action_type", "")
    if action_type in NON_EXECUTABLE_ACTIONS:
        return False, "action_not_executable_v1"
    if action_type not in READONLY_ALLOWLIST:
        return False, "action_not_in_allowlist"
    return True, "ok"


def _build_command(action_type: str, params: dict) -> list[str]:
    """从 allowlist 模板 + 参数构造命令(参数化 · 不拼接用户输入)。"""
    template = READONLY_ALLOWLIST[action_type]
    resolved = []
    for part in template:
        if part == "{n}":
            n = int(params.get("n", 200))
            resolved.append(str(max(1, min(n, 5000))))
        elif part == "{container}":
            container = params.get("container") or _active_container()
            if not container:
                raise ApprovalNotExecutable("未配置 active container")
            resolved.append(container)
        elif part == "{port}":
            port = params.get("port") or _active_port()
            if not port:
                raise ApprovalNotExecutable("未配置 active port")
            resolved.append(str(port))
        else:
            resolved.append(part)
    return resolved


def execute_approved_action(
    approval_id: int,
    *,
    real_execute: bool = False,
    executor: Optional[Callable] = None,
) -> dict:
    """
    执行一条已审批动作。返回结果 dict。

    real_execute=False(默认)或 ssh_runner.enabled 关闭:只记录"计划执行"(不真跑)。
    real_execute=True 且 flag 开启:用 executor 跑只读命令(executor 可注入,测试用 fake)。
    """
    approval = aiops_db.get_approval(approval_id)
    if not approval:
        raise ApprovalNotExecutable("approval 不存在")
    task_id = approval["task_id"]

    ok, reason = can_execute(approval)
    if not ok:
        aiops_db.append_event(task_id, "ssh_skipped",
                              f"跳过执行 approval={approval_id}: {reason}", severity="security")
        return {"executed": False, "reason": reason}

    action_type = approval["action_type"]
    command = _build_command(action_type, approval.get("command_plan_jsonb") or {})

    runner_enabled = aiops_db.is_flag_enabled("ssh_runner.enabled", default=False)
    if not (real_execute and runner_enabled):
        # 第一版默认路径:只记录计划,不真跑
        aiops_db.append_event(
            task_id, "ssh_plan_recorded",
            f"审批通过 · 计划执行(未真跑): {' '.join(command)}",
            payload={"approval_id": approval_id, "command": command}, severity="security",
        )
        return {"executed": False, "reason": "runner_disabled_or_plan_only", "command": command}

    # 真执行只读命令(executor 注入)· 执行前再复检一次安全门
    ok2, reason2 = can_execute(approval)
    if not ok2:
        aiops_db.append_event(task_id, "ssh_skipped",
                              f"执行前复检失败 approval={approval_id}: {reason2}", severity="security")
        return {"executed": False, "reason": reason2}

    import subprocess
    run = executor or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=120))
    result = run(command)
    stdout = redaction.redact(getattr(result, "stdout", "") or "")
    stderr = redaction.redact(getattr(result, "stderr", "") or "")
    rc = getattr(result, "returncode", None)
    aiops_db.add_artifact(task_id, "sql_result" if action_type == "readonly_sql" else "command_plan",
                          title=f"{action_type} output",
                          content_text=(stdout + ("\n[stderr]\n" + stderr if stderr else "")))
    aiops_db.append_event(
        task_id, "ssh_executed",
        f"执行只读动作 {action_type} rc={rc}",
        payload={"approval_id": approval_id, "returncode": rc}, severity="security",
    )
    # 幂等:标记 executed,防 run_ssh_loop 每轮重复跑同一条已审批动作(P1-3)
    aiops_db.mark_approval_executed(approval_id)
    return {"executed": True, "returncode": rc, "stdout": stdout, "stderr": stderr}


def run_ssh_loop(poll_interval: int = 5) -> None:
    """SSH Runner 常驻:领取 approved 动作执行。每轮先查 Kill Switch。"""
    import time
    logger.info("[ssh_runner] 启动")
    while True:
        try:
            if policy.is_kill_switch_enabled():
                time.sleep(poll_interval)
                continue
            if not aiops_db.is_flag_enabled("ssh_runner.enabled", default=False):
                time.sleep(poll_interval)   # flag 关时不处理,避免 plan-only 每轮刷事件
                continue
            # 只取 approved(executed 的已转状态不会再出现),逐条真执行(内部幂等再兜一层)
            approvals = aiops_db.list_approvals(status="approved", limit=20)
            for ap in approvals:
                execute_approved_action(ap["id"], real_execute=True)
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa: BLE001
            logger.exception("[ssh_runner] 循环异常: %s", e)
            time.sleep(poll_interval)
