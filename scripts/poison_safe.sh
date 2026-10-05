#!/usr/bin/env bash
# 注毒/还原的安全外壳(窗口 C · 2026-09-10)
#
# 🔴 存在的理由:`git checkout -- <file>` 还原的是 **HEAD**,不是"下毒前那份"。
#    文件若有未提交改动,还原会把它们一起冲掉 —— 树干净、判据绿、零信号。
#    我今天在同一个陷阱上栽了**三次**,其中两次是在刚写完"要先提交"的记忆之后。
#    ⇒ 规矩记不住,就把它变成**跑起来会自己报错的东西**。
#
# 用法:
#   scripts/poison_safe.sh <文件> <旧串> <新串> <毒名> <跑判据的命令>
#
# 它会:
#   1. 目标文件**有未提交改动就拒绝下毒**(先提交,或先 commit 成 WIP);
#   2. 断言毒锚存在**且唯一**(锚不唯一 = 可能打中别处,那种"没红"会被误读成锁没牙);
#   3. 下毒后自证 sha256 变了;
#   4. 还原后自证 sha256 **逐字节**回到下毒前。

set -u

# 🔴 出任何读数之前先核树:主仓工作树停在落后 2547 提交的旧分支,
#    在错树上注毒会得到一份关于另一个世界的"证据"(2026-09-10 绊倒两个窗口)。
source /c/AI-Test/RV_TREECHECK.sh || exit 1
f="$1"; old="$2"; new="$3"; name="$4"; runner="$5"

if [ -n "$(git status --porcelain -- "$f")" ]; then
  echo "  ✗ 拒绝下毒:$f 有未提交改动。"
  echo "    git checkout -- 会回到 HEAD,把它们一起冲掉(今天已栽三次)。"
  echo "    先 commit(哪怕是 WIP),再注毒。"
  exit 2
fi

before=$(sha256sum "$f" | cut -d' ' -f1)

python - "$f" "$old" "$new" <<'PY'
import io, sys
p, o, n = sys.argv[1:4]
s = io.open(p, encoding="utf-8").read()
assert o in s, "毒锚未命中: %r" % o[:60]
assert s.count(o) == 1, "毒锚不唯一(%d 处)—— 会打中别处,而那种'没红'会被误读成锁没牙" % s.count(o)
io.open(p, "w", encoding="utf-8", newline="").write(s.replace(o, n, 1))
PY
[ $? -ne 0 ] && { echo "  ✗ 毒没下成(锚的问题,不是锁的问题)"; exit 3; }

after=$(sha256sum "$f" | cut -d' ' -f1)
[ "$after" = "$before" ] && { echo "  ✗ 字节没变 —— 毒没下成"; exit 3; }

echo "  毒[$name] 已下成 → $(eval "$runner")"

git checkout -- "$f"
back=$(sha256sum "$f" | cut -d' ' -f1)
if [ "$back" = "$before" ]; then
  echo "     还原 ✓ 逐字节一致"
else
  echo "     ✗✗ 还原失败:sha 不一致。$f 现在既不是毒版也不是原版,立即人工核。"
  exit 4
fi
