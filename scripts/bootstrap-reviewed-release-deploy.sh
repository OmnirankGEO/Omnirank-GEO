#!/bin/bash
# 从外部可信副本启动首次 reviewed release 部署。
# 本脚本只创建/复用独立 detached worktree；绝不清理、重置或切换生产 runtime checkout。
set -euo pipefail

umask 027

SOURCE_REPO="${SOURCE_REPO:-/opt/omnirank/geo_agentscope}"
RUNTIME_ROOT="${RUNTIME_ROOT:-$SOURCE_REPO}"
RELEASE_ROOT="${RELEASE_ROOT:-/opt/omnirank/releases}"
DEPLOY_REMOTE="${DEPLOY_REMOTE:-origin}"
DEPLOY_SHA="${DEPLOY_SHA:-}"
REVIEWED_BOOTSTRAP_SHA256="${REVIEWED_BOOTSTRAP_SHA256:-}"
REVIEWED_DEPLOY_SCRIPT_SHA256="${REVIEWED_DEPLOY_SCRIPT_SHA256:-}"
REVIEWED_ROLLBACK_SCRIPT_SHA256="${REVIEWED_ROLLBACK_SCRIPT_SHA256:-}"
BOOTSTRAP_PATH=$(readlink -f "${BASH_SOURCE[0]}")

fatal() {
    echo "❌ reviewed release bootstrap: $*" >&2
    exit 1
}

require_sha256() {
    local name="$1"
    local value="$2"
    [[ "$value" =~ ^[0-9a-f]{64}$ ]] || fatal "$name 必须是 64 位小写 SHA-256"
}

[[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] || fatal "DEPLOY_SHA 必须是审核确认的 40 位小写 commit SHA"
require_sha256 "REVIEWED_BOOTSTRAP_SHA256" "$REVIEWED_BOOTSTRAP_SHA256"
require_sha256 "REVIEWED_DEPLOY_SCRIPT_SHA256" "$REVIEWED_DEPLOY_SCRIPT_SHA256"
require_sha256 "REVIEWED_ROLLBACK_SCRIPT_SHA256" "$REVIEWED_ROLLBACK_SCRIPT_SHA256"

ACTUAL_BOOTSTRAP_SHA256=$(sha256sum "$BOOTSTRAP_PATH" | awk '{print $1}')
[ "$ACTUAL_BOOTSTRAP_SHA256" = "$REVIEWED_BOOTSTRAP_SHA256" ] \
    || fatal "bootstrap hash 不匹配审核包"

[ -d "$SOURCE_REPO" ] || fatal "SOURCE_REPO 不存在: $SOURCE_REPO"
SOURCE_REPO=$(cd "$SOURCE_REPO" && pwd -P)
[ -d "$RUNTIME_ROOT" ] || fatal "RUNTIME_ROOT 不存在: $RUNTIME_ROOT"
RUNTIME_ROOT=$(cd "$RUNTIME_ROOT" && pwd -P)
git -C "$SOURCE_REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || fatal "SOURCE_REPO 不是 Git worktree"

# 只在本地 object database 缺少审核 SHA 时按完整 SHA fetch；不更新分支、不触碰工作树。
if ! git -C "$SOURCE_REPO" cat-file -e "$DEPLOY_SHA^{commit}" 2>/dev/null; then
    echo "审核 commit 本地不存在，只 fetch 不可变 DEPLOY_SHA..."
    git -C "$SOURCE_REPO" fetch --no-tags "$DEPLOY_REMOTE" "$DEPLOY_SHA"
fi
RESOLVED_SHA=$(git -C "$SOURCE_REPO" rev-parse "$DEPLOY_SHA^{commit}")
[ "$RESOLVED_SHA" = "$DEPLOY_SHA" ] || fatal "DEPLOY_SHA 未解析为同一 commit"

mkdir -p "$RELEASE_ROOT"
RELEASE_ROOT=$(cd "$RELEASE_ROOT" && pwd -P)
RELEASE_WORKTREE="${RELEASE_WORKTREE:-$RELEASE_ROOT/$DEPLOY_SHA}"

if [ -e "$RELEASE_WORKTREE" ]; then
    git -C "$RELEASE_WORKTREE" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        || fatal "既有 release 路径不是 Git worktree: $RELEASE_WORKTREE"
else
    git -C "$SOURCE_REPO" worktree add --detach "$RELEASE_WORKTREE" "$DEPLOY_SHA"
fi
RELEASE_WORKTREE=$(cd "$RELEASE_WORKTREE" && pwd -P)
[ "$RELEASE_WORKTREE" != "$SOURCE_REPO" ] || fatal "release worktree 不得等于生产 checkout"
[ "$RELEASE_WORKTREE" != "$RUNTIME_ROOT" ] || fatal "release worktree 不得等于 runtime root"

RELEASE_HEAD=$(git -C "$RELEASE_WORKTREE" rev-parse HEAD)
[ "$RELEASE_HEAD" = "$DEPLOY_SHA" ] || fatal "release HEAD=$RELEASE_HEAD，不等于 DEPLOY_SHA"
[ -z "$(git -C "$RELEASE_WORKTREE" status --porcelain --untracked-files=all)" ] \
    || fatal "release worktree 存在意外改动"
[ -z "$(git -C "$RELEASE_WORKTREE" ls-files --others --ignored --exclude-standard)" ] \
    || fatal "release worktree 存在被忽略的额外文件"

DEPLOY_SCRIPT="$RELEASE_WORKTREE/scripts/deploy-blue-green.sh"
[ -f "$DEPLOY_SCRIPT" ] || fatal "审核 commit 缺少 deploy-blue-green.sh"
ACTUAL_DEPLOY_SCRIPT_SHA256=$(sha256sum "$DEPLOY_SCRIPT" | awk '{print $1}')
[ "$ACTUAL_DEPLOY_SCRIPT_SHA256" = "$REVIEWED_DEPLOY_SCRIPT_SHA256" ] \
    || fatal "deploy script hash 不匹配审核包"
ROLLBACK_SCRIPT="$RELEASE_WORKTREE/scripts/rollback-blue-green.sh"
[ -f "$ROLLBACK_SCRIPT" ] || fatal "审核 commit 缺少 rollback-blue-green.sh"
ACTUAL_ROLLBACK_SCRIPT_SHA256=$(sha256sum "$ROLLBACK_SCRIPT" | awk '{print $1}')
[ "$ACTUAL_ROLLBACK_SCRIPT_SHA256" = "$REVIEWED_ROLLBACK_SCRIPT_SHA256" ] \
    || fatal "rollback script hash 不匹配审核包"

echo "✅ reviewed release 已锁定"
echo "  DEPLOY_SHA=$DEPLOY_SHA"
echo "  release=$RELEASE_WORKTREE"
echo "  runtime=$RUNTIME_ROOT（不会执行工作树覆盖）"

exec env \
    DEPLOY_SHA="$DEPLOY_SHA" \
    REVIEWED_DEPLOY_SCRIPT_SHA256="$REVIEWED_DEPLOY_SCRIPT_SHA256" \
    REVIEWED_ROLLBACK_SCRIPT_SHA256="$REVIEWED_ROLLBACK_SCRIPT_SHA256" \
    DEPLOY_RELEASE_WORKTREE="$RELEASE_WORKTREE" \
    RUNTIME_ROOT="$RUNTIME_ROOT" \
    bash "$DEPLOY_SCRIPT" "$@"
