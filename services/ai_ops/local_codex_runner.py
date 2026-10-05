"""
AI Ops · 本机 Codex Runner · 2026-07-03

方案口径修正:本轮不做 Runner VM/容器,Codex 跑在老板本机。
本机不持生产 DB 密码,只通过 HTTPS Runner API(X-Runner-Token)与任务总线交互:
  heartbeat → claim(拿任务+服务器构建好的脱敏上下文)→ 本机 clean worktree 跑 Codex
  → artifacts 上传 → finish(带 patch 一律进人工审批 waiting_review,绝不自动 merge)/ fail

启动:
  python -m services.ai_ops.local_codex_runner            # 常驻,每 60s 轮询
  python -m services.ai_ops.local_codex_runner --once     # 跑一轮退出(smoke)

环境变量(本机配置,不进 Git):
  AI_OPS_SERVER_URL=https://<生产站点>          # 必填
  AI_OPS_RUNNER_TOKEN=<本机专用 token>          # 必填 · Deploy 在服务器 .env 配同值
  AI_OPS_LOCAL_CODEX_ENABLED=false              # false 只发心跳不领任务(默认)
  AI_OPS_LOCAL_CODEX_POLL_SECONDS=60            # 轮询间隔
  AI_OPS_WORKER_ID=local-codex-boss             # 心跳/领取标识
  AI_OPS_REPO_PATH / AI_OPS_WORKTREE_ROOT / AI_OPS_ARTIFACT_ROOT / AI_OPS_CODEX_BIN /
  AI_OPS_CODEX_MODEL / AI_OPS_CODEX_TIMEOUT_SECONDS   # 与 worker.py 同名同义

行为保证:
  - enabled=false → 只 heartbeat(控制塔显示"在线·未授权执行"),不 claim
  - 本机断网/关机 → 服务器任务留在 queued,不丢、不失败(服务器无超时失败逻辑)
  - Kill Switch/总开关关 → claim 返回不可领取,本机静默等待
  - clean worktree(HEAD-based)+ assert_no_secrets fail-closed;诊断只读/修复 workspace-write
  - 不自动 merge、不自动 deploy、不碰 SSH
"""

import argparse
import logging
import os
import shutil
import socket
import time
from typing import Optional

logger = logging.getLogger("AiOps-LocalRunner")

# http:// 仅允许的本地测试主机;生产必须 https://(token 明文跑公网 http 等于裸奔)
_HTTP_ALLOWED_HOSTS = ("localhost", "127.0.0.1")


def validate_server_url(url: str) -> str:
    """
    AI_OPS_SERVER_URL 安全校验:生产必须 https://;只有 localhost/127.0.0.1 允许
    http://(本地测试例外)。非法 → SystemExit 拒绝启动(不带着不安全配置跑起来)。
    """
    from urllib.parse import urlparse
    parsed = urlparse(url or "")
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and (parsed.hostname or "").lower() in _HTTP_ALLOWED_HOSTS:
        return url
    raise SystemExit(
        f"AI_OPS_SERVER_URL 必须是 https://(仅 localhost/127.0.0.1 允许 http:// 本地测试),"
        f"当前: {url!r}")


class LocalConfig:
    def __init__(self):
        self.server_url = (os.getenv("AI_OPS_SERVER_URL", "") or "").rstrip("/")
        self.token = os.getenv("AI_OPS_RUNNER_TOKEN", "") or ""
        self.enabled = os.getenv("AI_OPS_LOCAL_CODEX_ENABLED", "false").lower() in ("1", "true", "yes")
        self.poll_seconds = self._poll()
        self.worker_id = os.getenv("AI_OPS_WORKER_ID", "local-codex-boss")
        self.repo_path = os.getenv("AI_OPS_REPO_PATH", "")
        self.worktree_root = os.getenv("AI_OPS_WORKTREE_ROOT", "")
        self.artifact_root = os.getenv("AI_OPS_ARTIFACT_ROOT", "")
        self.codex_bin = os.getenv("AI_OPS_CODEX_BIN", "codex")
        self.codex_model = os.getenv("AI_OPS_CODEX_MODEL", "") or None
        self.timeout = int(os.getenv("AI_OPS_CODEX_TIMEOUT_SECONDS", "1800"))

    @staticmethod
    def _poll() -> int:
        raw = os.getenv("AI_OPS_LOCAL_CODEX_POLL_SECONDS", "60")
        try:
            return max(10, min(600, int(raw)))
        except (TypeError, ValueError):
            return 60

    def worktree_path(self, task_id: int) -> str:
        return os.path.join(self.worktree_root or ".", f"task-{task_id}")

    def artifact_dir(self, task_id: int) -> str:
        return os.path.join(self.artifact_root or ".", f"task-{task_id}")


class RunnerApi:
    """HTTPS Runner API 客户端。任何网络失败返回 None(断网静默,下轮重试,任务留服务器队列)。"""

    def __init__(self, config: LocalConfig):
        self.base = config.server_url + "/api/public/ai-ops-runner"
        self.token = config.token

    def post(self, path: str, payload: dict, timeout: int = 30) -> Optional[dict]:
        try:
            import requests
            resp = requests.post(
                self.base + path, json=payload,
                headers={"X-Runner-Token": self.token},
                timeout=timeout,
            )
            if resp.status_code in (401, 403, 503):
                logger.error("[local-runner] %s 被拒 HTTP %s: %s", path, resp.status_code, resp.text[:200])
                return None
            if resp.status_code >= 400:
                logger.warning("[local-runner] %s HTTP %s: %s", path, resp.status_code, resp.text[:200])
                return None
            return resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("[local-runner] %s 网络失败(下轮重试): %s", path, e)
            return None


def _sandbox_for(kind: str) -> str:
    return "workspace-write" if kind == "fix" else "read-only"


def send_heartbeat(config: LocalConfig, api: RunnerApi) -> None:
    """enabled=false 也照发:控制塔显示"在线·未授权执行"。失败只 warn。"""
    api.post("/heartbeat", {
        "worker_id": config.worker_id,
        "host": socket.gethostname(),
        "version": os.getenv("AI_OPS_RUNNER_VERSION", "local"),
        "env_enabled": config.enabled,
        "codex_available": bool(shutil.which(config.codex_bin)),
        "ssh_runner_enabled": False,
    }, timeout=10)


def execute_task(task: dict, context_md: str, config: LocalConfig, api: RunnerApi) -> None:
    """本机 clean worktree 跑 Codex → 上传产物 → finish/fail。异常上报 fail。"""
    from services.ai_ops import codex_runner

    task_id = task["id"]
    kind = task.get("kind", "diagnose")
    if not config.repo_path:
        api.post("/fail", {"worker_id": config.worker_id, "task_id": task_id,
                           "error": "本机 AI_OPS_REPO_PATH 未配置,无法创建 clean worktree"})
        return

    worktree = config.worktree_path(task_id)
    artifact_dir = config.artifact_dir(task_id)
    os.makedirs(artifact_dir, exist_ok=True)
    try:
        codex_runner.create_worktree(config.repo_path, worktree, ref="HEAD")
        codex_runner.assert_no_secrets(worktree)   # fail-closed:worktree 里发现 .env/*.pem 即拒跑

        directive = (task.get("instruction") or "").strip() or (
            "按上面的上下文诊断问题,给出根因和修复方案,不要改文件。" if kind != "fix"
            else "按上面的上下文修复问题,只改必要文件,跑相关测试,输出变更摘要。"
        )
        prompt = f"{context_md}\n\n---\n\n## 现在请执行\n{directive}"
        result = codex_runner.run_codex(
            worktree, prompt,
            sandbox=_sandbox_for(kind),
            last_message_path=os.path.join(artifact_dir, "codex_last_message.md"),
            timeout=config.timeout,
            codex_bin=config.codex_bin,
            model=config.codex_model,
            env={"PATH": os.getenv("PATH", ""), "HOME": os.getenv("HOME", os.getenv("USERPROFILE", ""))},
            forbid_paths=[config.repo_path],
        )
        last_message = result.get("last_message", "") or result.get("stdout", "")
        api.post("/artifacts", {
            "worker_id": config.worker_id, "task_id": task_id,
            "artifact_type": "codex_output", "title": "codex_last_message.md",
            "content_text": last_message[:200000],
            "metadata": {"returncode": result.get("returncode")},
        })

        patch_diff, patch_stat = "", ""
        if kind == "fix":
            diff_stat, diff = codex_runner.collect_git_diff(worktree)
            if diff.strip():
                patch_diff, patch_stat = diff[:500000], diff_stat[:5000]

        fin = api.post("/finish", {
            "worker_id": config.worker_id, "task_id": task_id,
            "summary": (last_message.strip()[:500]) or "Codex 执行完成",
            "returncode": result.get("returncode", 1),
            "patch_diff": patch_diff, "patch_stat": patch_stat,
        })
        logger.info("[local-runner] 任务 %s 完成: %s", task_id, (fin or {}).get("status", "(finish 上报失败)"))
    except Exception as e:  # noqa: BLE001
        logger.exception("[local-runner] 任务 %s 执行失败", task_id)
        api.post("/fail", {"worker_id": config.worker_id, "task_id": task_id, "error": str(e)[:2000]})
    finally:
        # 清理 worktree(路径必须在 worktree_root 下,绝不删主仓库)
        if config.worktree_root and os.path.realpath(worktree).startswith(
            os.path.realpath(config.worktree_root)
        ):
            try:
                from services.ai_ops import codex_runner as _cr
                _cr.remove_worktree(config.repo_path, worktree)
            except Exception:  # noqa: BLE001
                logger.warning("[local-runner] worktree 清理失败(可手动删): %s", worktree)


def run_once(config: LocalConfig, api: RunnerApi) -> bool:
    """一轮:心跳 →(enabled 才)领取并执行。返回是否处理了任务。"""
    send_heartbeat(config, api)
    if not config.enabled:
        logger.info("[local-runner] AI_OPS_LOCAL_CODEX_ENABLED=false,只心跳不领任务")
        return False
    resp = api.post("/claim", {"worker_id": config.worker_id})
    if not resp or not resp.get("task"):
        reason = (resp or {}).get("reason", "network_unreachable")
        if reason not in ("queue_empty",):
            logger.info("[local-runner] 未领取: %s", reason)
        return False
    execute_task(resp["task"], resp.get("context", ""), config, api)
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="AI Ops 本机 Codex Runner")
    parser.add_argument("--once", action="store_true", help="跑一轮退出(smoke)")
    args = parser.parse_args()

    config = LocalConfig()
    if not config.server_url or not config.token:
        raise SystemExit("必须配置 AI_OPS_SERVER_URL 和 AI_OPS_RUNNER_TOKEN(本机环境变量,不进 Git)")
    validate_server_url(config.server_url)   # https 强制(仅 localhost 允许 http),非法拒绝启动
    api = RunnerApi(config)
    logger.info("[local-runner] 启动 worker_id=%s server=%s enabled=%s poll=%ss",
                config.worker_id, config.server_url, config.enabled, config.poll_seconds)
    if args.once:
        did = run_once(config, api)
        logger.info("[local-runner] --once 完成 processed=%s", did)
        return
    while True:
        try:
            did = run_once(config, api)
            time.sleep(2 if did else config.poll_seconds)
        except KeyboardInterrupt:
            logger.info("[local-runner] 收到中断,退出")
            break
        except Exception as e:  # noqa: BLE001
            logger.exception("[local-runner] 主循环异常,继续: %s", e)
            time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
