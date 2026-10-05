import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "bootstrap-reviewed-release-deploy.sh"
DEPLOY = ROOT / "scripts" / "deploy-blue-green.sh"
ROLLBACK = ROOT / "scripts" / "rollback-blue-green.sh"


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=check,
    )


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", *args], cwd=cwd).stdout.strip()


def _bash_executable() -> str:
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if os.name == "nt" and git_bash.is_file():
        return str(git_bash)
    bash = shutil.which("bash")
    assert bash, "bash is required for deploy contract tests"
    return bash


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_old_server_script_is_bypassed_by_one_reviewed_bootstrap_invocation(tmp_path: Path):
    """服务器仍持有旧脚本时，首次调用必须直接执行审核 SHA 的完整门禁。"""

    source = tmp_path / "production-runtime"
    releases = tmp_path / "releases"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.name", "Deploy Contract Test")
    _git(source, "config", "user.email", "deploy-contract@example.invalid")
    _git(source, "config", "core.autocrlf", "false")

    scripts = source / "scripts"
    scripts.mkdir()
    (source / ".gitignore").write_text("*.tmpignored\n", encoding="utf-8", newline="\n")
    production_state = source / "production-state.txt"
    production_state.write_text("tracked baseline\n", encoding="utf-8", newline="\n")
    (scripts / "deploy-blue-green.sh").write_text(
        "#!/bin/bash\necho OLD_SCRIPT_EXECUTED\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(source, "add", ".gitignore", "scripts/deploy-blue-green.sh", "production-state.txt")
    _git(source, "commit", "-m", "old production deployment script")
    old_sha = _git(source, "rev-parse", "HEAD")

    (scripts / "deploy-blue-green.sh").write_bytes(DEPLOY.read_bytes())
    (scripts / "rollback-blue-green.sh").write_bytes(ROLLBACK.read_bytes())
    _git(source, "add", "scripts/deploy-blue-green.sh", "scripts/rollback-blue-green.sh")
    _git(source, "commit", "-m", "reviewed immutable release deployment script")
    deploy_sha = _git(source, "rev-parse", "HEAD")

    _git(source, "switch", "--detach", old_sha)
    production_state.write_text("tracked baseline\nproduction hot edit\n", encoding="utf-8", newline="\n")

    env = os.environ.copy()
    env.update(
        {
            "SOURCE_REPO": source.as_posix(),
            "RUNTIME_ROOT": source.as_posix(),
            "RELEASE_ROOT": releases.as_posix(),
            "DEPLOY_SHA": deploy_sha,
            "REVIEWED_BOOTSTRAP_SHA256": _sha256(BOOTSTRAP),
            "REVIEWED_DEPLOY_SCRIPT_SHA256": _sha256(DEPLOY),
            "REVIEWED_ROLLBACK_SCRIPT_SHA256": _sha256(ROLLBACK),
        }
    )
    result = _run(
        [_bash_executable(), BOOTSTRAP.as_posix(), "--verify-release-contract"],
        cwd=tmp_path,
        env=env,
    )

    assert "RELEASE_IDENTITY_VERIFIED" in result.stdout
    assert "PHASE_A_GATE_VERIFIED" in result.stdout
    assert "ACTIVATION_GATE_VERIFIED" in result.stdout
    assert "OLD_SCRIPT_EXECUTED" not in result.stdout
    assert _git(source, "rev-parse", "HEAD") == old_sha
    assert "production hot edit" in production_state.read_text(encoding="utf-8")
    assert _git(source, "status", "--porcelain") == "M production-state.txt"

    release = releases / deploy_sha
    release_script = release / "scripts" / "deploy-blue-green.sh"
    direct_env = env | {"DEPLOY_RELEASE_WORKTREE": release.as_posix()}

    wrong_hash = direct_env | {"REVIEWED_DEPLOY_SCRIPT_SHA256": "0" * 64}
    rejected_hash = _run(
        [_bash_executable(), release_script.as_posix(), "--verify-release-contract"],
        cwd=tmp_path,
        env=wrong_hash,
        check=False,
    )
    assert rejected_hash.returncode != 0
    assert "deploy script hash" in rejected_hash.stderr

    wrong_rollback_hash = direct_env | {"REVIEWED_ROLLBACK_SCRIPT_SHA256": "0" * 64}
    rejected_rollback_hash = _run(
        [_bash_executable(), release_script.as_posix(), "--verify-release-contract"],
        cwd=tmp_path,
        env=wrong_rollback_hash,
        check=False,
    )
    assert rejected_rollback_hash.returncode != 0
    assert "rollback script hash" in rejected_rollback_hash.stderr

    unexpected = release / "unexpected-local-change.txt"
    unexpected.write_text("must fail closed\n", encoding="utf-8", newline="\n")
    rejected_dirty = _run(
        [_bash_executable(), release_script.as_posix(), "--verify-release-contract"],
        cwd=tmp_path,
        env=direct_env,
        check=False,
    )
    assert rejected_dirty.returncode != 0
    assert "release worktree" in rejected_dirty.stderr
    unexpected.unlink()

    ignored = release / "hidden.tmpignored"
    ignored.write_text("must also fail closed\n", encoding="utf-8", newline="\n")
    rejected_ignored = _run(
        [_bash_executable(), release_script.as_posix(), "--verify-release-contract"],
        cwd=tmp_path,
        env=direct_env,
        check=False,
    )
    assert rejected_ignored.returncode != 0
    assert "被忽略" in rejected_ignored.stderr
    ignored.unlink()

    assert _git(release, "rev-parse", "HEAD") == deploy_sha
    assert _git(release, "status", "--porcelain", "--untracked-files=all") == ""


def test_deploy_sources_are_immutable_release_only():
    deploy = DEPLOY.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert re.search(r"git\s+(fetch|reset|checkout)\b", deploy) is None
    assert "frontend/vite.config.ts" not in deploy
    assert 'actual_head=$(git -C "$PROJECT_DIR" rev-parse HEAD)' in deploy
    assert 'actual_script_hash=$(sha256sum "$SCRIPT_PATH"' in deploy
    assert "status --porcelain --untracked-files=all" in deploy
    assert "ls-files --others --ignored --exclude-standard" in deploy
    assert 'PROJECT_DIR=$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd -P)' in deploy
    assert 'OMNIRANK_RUNTIME_ROOT="$RUNTIME_ROOT"' in deploy
    assert 'OMNIRANK_COMPOSE_PROJECT_NAME="$OMNIRANK_COMPOSE_PROJECT_NAME" docker compose' in deploy
    assert '--project-name "$OMNIRANK_COMPOSE_PROJECT_NAME"' in deploy

    assert 'fetch --no-tags "$DEPLOY_REMOTE" "$DEPLOY_SHA"' in bootstrap
    assert re.search(r"git\s+(reset|checkout)\b", bootstrap) is None
    assert 'worktree add --detach "$RELEASE_WORKTREE" "$DEPLOY_SHA"' in bootstrap
    assert 'bash "$DEPLOY_SCRIPT" "$@"' in bootstrap
    assert "REVIEWED_BOOTSTRAP_SHA256" in bootstrap
    assert "REVIEWED_DEPLOY_SCRIPT_SHA256" in bootstrap
    assert "REVIEWED_ROLLBACK_SCRIPT_SHA256" in bootstrap
    assert "ls-files --others --ignored --exclude-standard" in bootstrap

    assert "${OMNIRANK_RUNTIME_ROOT:-.}/data:/app/data" in compose
    assert "${OMNIRANK_RUNTIME_ROOT:-.}/.env" in compose
    assert compose.count("- ./config:/app/config") == 4
    assert "${OMNIRANK_RUNTIME_ROOT:-.}/config:/app/config" not in compose
