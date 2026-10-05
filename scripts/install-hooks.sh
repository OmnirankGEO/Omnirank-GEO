#!/bin/bash
# install-hooks.sh · 装 git pre-commit hook · 防 SQL 累犯
# CTO-15.23 2026-05-07 · 体检师 v2 P1-1
#
# 用法:bash scripts/install-hooks.sh
# 卸载:rm .git/hooks/pre-commit

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_SRC="$REPO_ROOT/scripts/git-hooks/pre-commit"
HOOK_DST="$REPO_ROOT/.git/hooks/pre-commit"

if [ ! -f "$HOOK_SRC" ]; then
    echo "❌ 找不到 hook 模板 $HOOK_SRC"
    exit 1
fi

# 装到共享 hooks dir(common dir · worktree 也用这个)
# 用 git rev-parse --git-common-dir 拿主 repo .git 路径
COMMON_DIR=$(git rev-parse --git-common-dir)
HOOK_DST="$COMMON_DIR/hooks/pre-commit"

mkdir -p "$(dirname "$HOOK_DST")"
cp "$HOOK_SRC" "$HOOK_DST"
chmod +x "$HOOK_DST"

echo "✅ pre-commit hook 已装到 $HOOK_DST"
echo "   测试:故意写一行 \`u.is_admin\` 的 SELECT 看是否拦截"
echo "   跳过(紧急情况):git commit --no-verify"
