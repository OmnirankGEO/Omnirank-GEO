"""
AI Ops Worker · 2026-07-01

独立进程(不在 FastAPI request 线程里跑 Codex):
  领取任务 → 构造脱敏上下文 → clean worktree → codex exec → 存 artifact/事件 → 回写状态。

启动:
  python -m services.ai_ops.worker            # 常驻循环
  python -m services.ai_ops.worker --once     # 领一条跑完退出
  python -m services.ai_ops.worker --once --task-id 12   # 只跑指定任务(需已 queued)
  python -m services.ai_ops.worker --once --dry-run      # 只构造上下文,不真调 Codex

运行拓扑:装在专用 AI Ops Runner VM/独立容器,持 clean tracked-only checkout(无密钥),
最小凭证,不给生产 SSH / 生产 DB 写权限。详见 AI_OPS_RUNNER_TOPOLOGY 文档。
"""

import argparse
import logging
import os
import threading
import time

from db import ai_ops_db as aiops_db
from services.ai_ops import codex_runner, context_builder, policy

logger = logging.getLogger("AiOps-Worker")


class WorkerConfig:
    def __init__(self):
        self.worker_id = os.getenv("AI_OPS_WORKER_ID", "local-dev-1")
        self.repo_path = os.getenv("AI_OPS_REPO_PATH", "")
        self.worktree_root = os.getenv("AI_OPS_WORKTREE_ROOT", "")
        self.artifact_root = os.getenv("AI_OPS_ARTIFACT_ROOT", "")
        self.codex_bin = os.getenv("AI_OPS_CODEX_BIN", "codex")
        self.codex_model = os.getenv("AI_OPS_CODEX_MODEL", "") or None
        self.timeout = int(os.getenv("AI_OPS_CODEX_TIMEOUT_SECONDS", "1800"))
        self.enabled = os.getenv("AI_OPS_ENABLED", "false").lower() in ("1", "true", "yes")

    def artifact_dir(self, task_id: int) -> str:
        return os.path.join(self.artifact_root or ".", f"task-{task_id}")

    def worktree_path(self, task_id: int) -> str:
        return os.path.join(self.worktree_root or ".", f"task-{task_id}")


def _sandbox_for(kind: str) -> str:
    return "workspace-write" if kind == "fix" else "read-only"


def _heartbeat_interval() -> int:
    """
    读 AI_OPS_HEARTBEAT_SECONDS(默认 45)。非法值回落 45,并 clamp 到 [5, 60]:
    上限 60 保证 interval*2 ≤ get_runner_status 的 stale 窗口 120s,不会在线/离线摆动;
    非数字绝不抛异常杀 worker。
    """
    raw = os.getenv("AI_OPS_HEARTBEAT_SECONDS", "45")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning("[worker] AI_OPS_HEARTBEAT_SECONDS=%r 非法,回落 45", raw)
        return 45
    return max(5, min(60, val))


def start_heartbeat_thread(config: WorkerConfig) -> threading.Thread:
    """
    独立 daemon 线程发心跳(复审 P2):主循环跑 Codex 时 subprocess.run 可阻塞到
    AI_OPS_CODEX_TIMEOUT_SECONDS(默认 1800s),内联心跳会断流 → 控制塔把"忙碌"
    误显示成"离线"。线程先立即发一次(启动即在线可见)再按 interval 循环。
    emit_heartbeat 自吞异常,线程永不崩;daemon=True 随主进程退出。
    db.connection 是 ThreadedConnectionPool,跨线程取连接安全。
    """
    interval = _heartbeat_interval()

    def _loop():
        while True:
            emit_heartbeat(config)
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="aiops-heartbeat")
    t.start()
    return t


def emit_heartbeat(config: WorkerConfig) -> None:
    """
    写一次 Worker 心跳(P1-B)。让控制塔知道 Runner 在线/未授权执行。
    - env AI_OPS_ENABLED=false 也照常心跳(证明"在线但未授权执行")。
    - codex_available 用 `which <codex_bin>` 真实探测,不写任何密钥。
    - 失败只 warn,绝不影响主循环(心跳挂不能拖垮任务处理)。
    """
    try:
        import shutil
        import socket
        codex_ok = bool(shutil.which(config.codex_bin))
        ssh_enabled = os.getenv("AI_OPS_SSH_RUNNER_ENABLED", "false").lower() in ("1", "true", "yes")
        aiops_db.upsert_heartbeat(
            config.worker_id,
            host=socket.gethostname(),
            version=os.getenv("AI_OPS_RUNNER_VERSION", ""),
            env_enabled=config.enabled,
            codex_available=codex_ok,
            ssh_runner_enabled=ssh_enabled,
            note="",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[worker] 心跳写入失败(忽略): %s", e)


def run_task(task: dict, config: WorkerConfig, *, dry_run: bool = False) -> None:
    """跑一条任务。异常向上抛,由调用方标 failed。"""
    task_id = task["id"]
    kind = task.get("kind", "diagnose")

    # report 任务不走 Codex/worktree:纯 DB 读写生成日报。
    if kind == "report":
        if dry_run:
            aiops_db.update_task_status(task_id, "succeeded", summary="dry-run:report")
            return
        from services.ai_ops import report_builder
        rid = report_builder.build_report_for_task(task)
        aiops_db.append_event(task_id, "report_generated", f"日报已生成 report_id={rid}")
        aiops_db.update_task_status(task_id, "succeeded", summary=f"日报 report_id={rid}")
        return

    # 1) 构造脱敏上下文(从任务快照 · Runner 不回读 faq_feedback 业务表)
    context_md = context_builder.build_ops_context(task)
    aiops_db.add_artifact(task_id, "ops_context", title="ops_context.md", content_text=context_md)
    aiops_db.append_event(task_id, "context_collected", "上下文已构造(从任务快照 · 已脱敏)")

    if dry_run:
        aiops_db.append_event(task_id, "dry_run", "dry-run:跳过 Codex 执行")
        aiops_db.update_task_status(task_id, "succeeded", summary="dry-run 完成:只构造了上下文")
        return

    if not config.repo_path:
        raise RuntimeError("AI_OPS_REPO_PATH 未配置,无法创建 clean worktree")

    # 2) clean worktree(HEAD-based · 天然排 .env/私钥/脏状态)
    worktree = config.worktree_path(task_id)
    artifact_dir = config.artifact_dir(task_id)
    os.makedirs(artifact_dir, exist_ok=True)
    codex_runner.create_worktree(config.repo_path, worktree, ref="HEAD")
    aiops_db.update_task_status(task_id, None, worktree_path=worktree)
    aiops_db.append_event(task_id, "worktree_created", f"worktree: {worktree}")

    try:
        # 3) 截图:先下载成本地文件再 --image(不把签名 URL 塞进 context)
        image_path = None
        screenshot_key = ((task.get("source_context_jsonb") or {}).get("screenshot_key")
                          if isinstance(task.get("source_context_jsonb"), dict) else None)
        if screenshot_key:
            try:
                from services.oss_service import generate_feedback_signed_url
                signed = generate_feedback_signed_url(screenshot_key)
                if signed:
                    image_path = codex_runner.download_signed_image(
                        signed, os.path.join(artifact_dir, "feedback_screenshot.png"))
                    aiops_db.append_event(task_id, "screenshot_downloaded",
                                          "截图已下载成本地文件供 --image")
            except Exception as e:  # noqa: BLE001
                logger.warning("[worker] 下载截图失败(跳过): %s", e)

        # 4) 跑 Codex(诊断只读 / 修复 workspace-write)· forbid 活 repo
        # P0-1:把完整 ops_context 直接喂给 Codex(不是只给 task.instruction),
        # 让 Codex 拿到原始问题/截图说明/红线规则/期望产出后再主诊断/主修复。
        sandbox = _sandbox_for(kind)
        directive = (task.get("instruction") or "").strip() or (
            "按上面的上下文诊断问题,给出根因和修复方案,不要改文件。" if kind != "fix"
            else "按上面的上下文修复问题,只改必要文件,跑相关测试,输出变更摘要。"
        )
        prompt = f"{context_md}\n\n---\n\n## 现在请执行\n{directive}"
        last_msg_path = os.path.join(artifact_dir, "codex_last_message.md")
        aiops_db.append_event(task_id, "codex_started", f"codex exec · sandbox={sandbox}")
        result = codex_runner.run_codex(
            worktree, prompt,
            sandbox=sandbox,
            last_message_path=last_msg_path,
            timeout=config.timeout,
            codex_bin=config.codex_bin,
            image_path=image_path,
            model=config.codex_model,
            env={"PATH": os.getenv("PATH", ""), "HOME": os.getenv("HOME", os.getenv("USERPROFILE", ""))},
            forbid_paths=[config.repo_path],
        )

        last_message = result.get("last_message", "") or result.get("stdout", "")
        aiops_db.add_artifact(task_id, "codex_output", title="codex_last_message.md",
                              content_text=last_message[:200000])
        aiops_db.append_event(
            task_id, "codex_finished",
            f"codex 返回 rc={result.get('returncode')}",
            payload={"returncode": result.get("returncode")},
            severity="info" if result.get("returncode") == 0 else "warn",
        )

        # 5) 修复任务:收 diff + AI Ops 校验 + 强制人工审批才能合并
        if kind == "fix":
            diff_stat, diff = codex_runner.collect_git_diff(worktree)
            if diff.strip():
                risks = policy.scan_diff_risks(diff)
                aiops_db.add_artifact(task_id, "patch", title="git diff",
                                      content_text=diff[:500000],
                                      metadata={"stat": diff_stat, "risks": risks})
                aiops_db.append_event(task_id, "patch_saved", f"diff:\n{diff_stat[:2000]}",
                                      payload={"risks": risks})
                # 设计 L1:合并主分支必须人工审批,Worker 从不自动 merge。
                # 触红线文件/资金/DB写/部署/密钥 → 升 L4 + security 事件。
                risky = bool(risks)
                aiops_db.create_approval(
                    task_id, "merge_fix",
                    risk_level="L4" if risky else "L3",
                    requested_reason=(f"Codex 修复 diff 待合并 · 校验命中风险: {risks}" if risky
                                      else "Codex 修复 diff 待人工审批后合并"),
                    command_plan={"diff_stat": diff_stat, "risks": risks},
                )
                aiops_db.append_event(
                    task_id, "review_flagged" if risky else "review_pending",
                    (f"diff 触风险,合并前必须人工审批: {risks}" if risky else "diff 待人工审批合并"),
                    payload={"risks": risks}, severity="security" if risky else "info",
                )
                # create_approval 已把任务置 waiting_approval;不自动标 succeeded
                return
            aiops_db.append_event(task_id, "no_patch", "Codex 未产生代码改动")

        summary = (last_message.strip()[:500]) or "Codex 执行完成"
        ok = result.get("returncode") == 0
        aiops_db.update_task_status(
            task_id, "succeeded" if ok else "failed",
            summary=summary,
            result={"returncode": result.get("returncode"), "kind": kind},
        )
    finally:
        # 6) 清理 worktree(路径必须在 worktree_root 下,绝不删主仓库)
        if config.worktree_root and os.path.realpath(worktree).startswith(
            os.path.realpath(config.worktree_root)
        ):
            codex_runner.remove_worktree(config.repo_path, worktree)


def process_one(config: WorkerConfig, *, task_id: int = None, dry_run: bool = False) -> bool:
    """领取并跑一条任务。返回是否处理了任务。"""
    if policy.is_kill_switch_enabled():
        logger.info("[worker] Kill Switch 开启,不领取任务")
        return False
    if not config.enabled:
        # 进程级硬开关(env AI_OPS_ENABLED)· 与 DB 策略 ai_ops.enabled 双层(都要开)
        logger.info("[worker] AI_OPS_ENABLED=false,worker 不领取任务")
        return False
    if not policy.is_ai_ops_enabled():
        # P1-1:DB 总开关关闭时,claim 前就返回,绝不把任务短暂改 running / 写 claimed 事件
        logger.info("[worker] DB ai_ops.enabled=false,不领取任务")
        return False

    if task_id is not None:
        task = aiops_db.get_task(task_id)
        if not task or task.get("status") != "queued":
            logger.warning("[worker] 任务 %s 不存在或不是 queued", task_id)
            return False
        allowed, reason = policy.can_worker_claim(task)
        if not allowed:
            logger.info("[worker] 任务 %s 不允许领取: %s", task_id, reason)
            return False
        aiops_db.update_task_status(task_id, "running", assigned_worker_id=config.worker_id)
        task = aiops_db.get_task(task_id)
    else:
        task = aiops_db.claim_next_task(config.worker_id, allowed_kinds=policy.WORKER_DEFAULT_KINDS)
        if not task:
            return False
        allowed, reason = policy.can_worker_claim(task)
        if not allowed:
            aiops_db.update_task_status(task["id"], "queued")  # 放回队列
            aiops_db.append_event(task["id"], "requeued", f"暂不允许领取: {reason}", severity="warn")
            return False

    try:
        run_task(task, config, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001
        logger.exception("[worker] 任务 %s 失败", task["id"])
        aiops_db.append_event(task["id"], "error", f"任务失败: {e}", severity="error")
        aiops_db.update_task_status(task["id"], "failed", summary=f"worker 异常: {e}")
    return True


def run_forever(config: WorkerConfig) -> None:
    logger.info("[worker] 启动 worker_id=%s repo=%s", config.worker_id, config.repo_path)
    # 心跳走独立 daemon 线程:主循环被 Codex 长任务阻塞时心跳不断流(复审 P2),
    # Kill Switch 期间也持续发(控制塔看到 Runner 在线但被冻结)。
    start_heartbeat_thread(config)
    while True:
        try:
            if policy.is_kill_switch_enabled():
                time.sleep(5)
                continue
            did = process_one(config)
            time.sleep(2 if not did else 0.2)
        except KeyboardInterrupt:
            logger.info("[worker] 收到中断,退出")
            break
        except Exception as e:  # noqa: BLE001
            logger.exception("[worker] 主循环异常,继续: %s", e)
            time.sleep(5)


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="AI Ops Worker")
    parser.add_argument("--once", action="store_true", help="领一条跑完退出")
    parser.add_argument("--task-id", type=int, default=None, help="只跑指定 queued 任务")
    parser.add_argument("--dry-run", action="store_true", help="只构造上下文不调 Codex")
    args = parser.parse_args()

    config = WorkerConfig()
    if args.once or args.task_id is not None:
        # provision/dry-run smoke 也发一次心跳,让控制塔立刻看到 Runner 在线(即便 env 关)
        emit_heartbeat(config)
        did = process_one(config, task_id=args.task_id, dry_run=args.dry_run)
        logger.info("[worker] --once 完成 processed=%s", did)
    else:
        run_forever(config)


if __name__ == "__main__":
    main()
