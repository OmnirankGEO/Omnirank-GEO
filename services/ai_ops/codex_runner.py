"""
AI Ops Codex Runner · 2026-07-01

用 `codex exec` 在 clean worktree 里跑诊断/修复。绝不对活 repo 根目录跑。

密钥隔离(CTO 审核 P0-1 · 硬约束):
  - 诊断和修复都必须在 clean tracked-only worktree 里跑;`--cd` 只能指向 worktree。
  - 跑 Codex 前先 assert_no_secrets():worktree 内存在 .env/.env.*/*.pem/apiclient_key.pem/pub_key.pem
    直接 fail closed(SecretLeakError),不继续执行。
  - `--sandbox read-only` 只禁写不禁读,所以密钥保护靠"无密钥 worktree",不是靠 sandbox。
  - 截图:先把签名 URL 下载成本地文件,再 `--image <local_file>`。

subprocess 可注入(runner 参数),测试用 fake 不真调 Codex。
"""

import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

# 禁止出现在 worktree 里的敏感文件(glob)
SENSITIVE_GLOBS = (".env", ".env.*", "*.pem")
SENSITIVE_EXPLICIT = ("apiclient_key.pem", "pub_key.pem")
# 白名单:tracked 的模板/示例 env 不含真实密钥,不算敏感(否则 clean worktree 带 .env.example 会误 fail closed)
BENIGN_ENV_FILES = frozenset({
    ".env.example", ".env.template", ".env.sample", ".env.dist", ".env.example.local",
})


class SecretLeakError(Exception):
    """worktree 里发现密钥文件,拒绝把它交给 Codex。"""


class WorktreeError(Exception):
    """worktree 创建/校验失败。"""


def find_secret_files(root: str) -> list[str]:
    """递归找 worktree 内的敏感文件,返回相对路径列表。"""
    base = Path(root)
    if not base.exists():
        raise WorktreeError(f"worktree 不存在: {root}")
    hits: set[str] = set()
    for pattern in SENSITIVE_GLOBS:
        for p in base.rglob(pattern):
            if p.is_file() and p.name not in BENIGN_ENV_FILES:
                hits.add(str(p.relative_to(base)))
    for name in SENSITIVE_EXPLICIT:
        for p in base.rglob(name):
            if p.is_file():
                hits.add(str(p.relative_to(base)))
    return sorted(hits)


def assert_no_secrets(root: str) -> None:
    """发现敏感文件即 fail closed。"""
    hits = find_secret_files(root)
    if hits:
        raise SecretLeakError(
            f"worktree {root} 内发现敏感文件,拒绝交给 Codex: {hits[:10]}"
        )


def create_worktree(
    repo_path: str,
    worktree_path: str,
    ref: str = "HEAD",
    runner: Callable = subprocess.run,
) -> None:
    """
    git worktree add <worktree_path> <ref>。默认 HEAD:只带已提交 tracked 文件,
    天然排除 .env/私钥/本地脏状态。要基于未提交改动须先人工 WIP commit/branch,再传 ref。
    """
    result = runner(
        ["git", "-C", repo_path, "worktree", "add", "--force", worktree_path, ref],
        capture_output=True, text=True, timeout=120,
    )
    if getattr(result, "returncode", 1) != 0:
        raise WorktreeError(f"git worktree add 失败: {getattr(result, 'stderr', '')}")


def remove_worktree(
    repo_path: str,
    worktree_path: str,
    runner: Callable = subprocess.run,
) -> None:
    """清理 worktree。删除前调用方须确认 worktree_path 在 AI_OPS_WORKTREE_ROOT 下,绝不删主仓库。"""
    try:
        runner(
            ["git", "-C", repo_path, "worktree", "remove", "--force", worktree_path],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        pass


def download_signed_image(url: str, dest_path: str, *, timeout: int = 30) -> str:
    """把截图签名 URL 下载成本地文件,返回本地路径(供 --image 用)。"""
    import requests
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path


def build_codex_cmd(
    *,
    codex_bin: str,
    worktree_path: str,
    sandbox: str,
    last_message_path: str,
    prompt: str,
    image_path: Optional[str] = None,
    model: Optional[str] = None,
) -> list[str]:
    """构造 codex exec 命令(参数化,不字符串拼接用户输入)。"""
    if sandbox not in ("read-only", "workspace-write", "danger-full-access"):
        raise ValueError(f"invalid sandbox: {sandbox}")
    cmd = [
        codex_bin, "exec",
        "--cd", worktree_path,
        "--sandbox", sandbox,
        "--json",
        "--output-last-message", last_message_path,
    ]
    if model:
        cmd += ["--model", model]
    if image_path:
        cmd += ["--image", image_path]
    cmd.append(prompt)
    return cmd


def run_codex(
    worktree_path: str,
    prompt: str,
    *,
    sandbox: str = "read-only",
    last_message_path: str,
    timeout: int = 1800,
    codex_bin: str = "codex",
    image_path: Optional[str] = None,
    model: Optional[str] = None,
    env: Optional[dict] = None,
    forbid_paths: Optional[list[str]] = None,
    runner: Callable = subprocess.run,
) -> dict:
    """
    在 worktree 里跑 codex exec。返回 {returncode, stdout, stderr, last_message, cmd}。

    安全前置(顺序不可换):
      1. forbid_paths 校验:worktree 不能解析成活 repo(禁止对活 repo 跑)。
      2. assert_no_secrets:worktree 内有 .env/*.pem 直接 fail closed。
    """
    real_wt = os.path.realpath(worktree_path)
    for forbidden in (forbid_paths or []):
        if os.path.realpath(forbidden) == real_wt:
            raise SecretLeakError(
                f"拒绝对活 repo 跑 Codex(--cd 必须是 worktree,不能是 {forbidden})"
            )
    assert_no_secrets(worktree_path)

    cmd = build_codex_cmd(
        codex_bin=codex_bin, worktree_path=worktree_path, sandbox=sandbox,
        last_message_path=last_message_path, prompt=prompt,
        image_path=image_path, model=model,
    )
    # 限制环境变量:只传显式给的最小 env(不继承宿主全部环境 → 少泄露)
    run_env = dict(env) if env is not None else None
    result = runner(cmd, capture_output=True, text=True, timeout=timeout, env=run_env)

    last_message = ""
    try:
        if os.path.exists(last_message_path):
            with open(last_message_path, encoding="utf-8") as f:
                last_message = f.read()
    except Exception:
        last_message = ""

    return {
        "returncode": getattr(result, "returncode", None),
        "stdout": getattr(result, "stdout", "") or "",
        "stderr": getattr(result, "stderr", "") or "",
        "last_message": last_message,
        "cmd": cmd,
    }


def collect_git_diff(worktree_path: str, runner: Callable = subprocess.run) -> tuple[str, str]:
    """
    修复任务跑完后收 diff。返回 (diff_stat, diff)。

    关键(P0-2):Codex 常新增文件(迁移/模块/测试)但不 `git add`,`git diff` 看不到 untracked。
    先 `git add -N .`(intent-to-add · 尊重 .gitignore · worktree 用后即删)让新文件进 diff,
    否则会漏判成 no_patch/succeeded,绕过"fix diff 一律审批"的安全承诺。
    """
    runner(["git", "-C", worktree_path, "add", "-N", "."],
           capture_output=True, text=True, timeout=60)
    stat = runner(["git", "-C", worktree_path, "diff", "--stat"],
                  capture_output=True, text=True, timeout=60)
    full = runner(["git", "-C", worktree_path, "diff"],
                  capture_output=True, text=True, timeout=60)
    return (getattr(stat, "stdout", "") or "", getattr(full, "stdout", "") or "")
