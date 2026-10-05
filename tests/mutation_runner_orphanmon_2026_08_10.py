"""P0 孤儿监测词修复 · 变异自检(WO_ORPHAN_MONITOR_FIX_2026-08-10 验收判据 2)。

三关缺一即判无效变异:
  1. 锚点唯一命中(命中 0 或 >1 = 变异没打在预期位置);
  2. 落盘后文件真变了;
  3. 至少一条锁转红。零转红 = 锁抓不到本 bug。

工单点名要配的三对(每处"删了必红" + "正常路径不误伤"的放行锁):
  ① 删建订阅调用      → 端到端锁必红   (放行锁 = test_normal_path_not_broken_by_fail_closed)
  ② EXISTS 改回读旧字段 → 孤儿态锁必红   (放行锁 = 有订阅必须显示开)
  ③ 归档联动删掉      → 僵尸锁必红     (放行锁 = 不误伤别的词)

🔴 文件读写一律走 bytes(Windows 上 read_text/write_text 会翻换行,本仓栽过两次)。
🔴 子进程钉 PYTHONPYCACHEPREFIX + 禁写字节码:变异 runner 被打断时残留的 .pyc
   会让下一轮基线假红(2026-08-09 Review P2 判过)。

跑法:TEST_DATABASE_URL=... python tests/mutation_runner_orphanmon_2026_08_10.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]

SELECTION_API = "api/selection_api.py"
MONITORING_DB = "db/monitoring_db.py"
MONITOR_BILLING = "services/monitor_billing.py"
SERVER_PY = "server.py"

T_MAIN = "tests/orphanmon_2026_08_10/test_orphan_monitor_fix_2026_08_10.py"

_PYC_DIR = REPO / ".mutation_pycache"

# (编号, 说明, 文件, 锚点, 替换)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ── ① 写入层 ──────────────────────────────────────────────────────
    ("M1", "① 删掉建订阅调用 → 退回修复前:只标 is_monitored,当场产孤儿",
     SELECTION_API,
     "                create_keyword_monitor_subscription_with_cursor(\n",
     "                (lambda *_a, **_k: None)(\n"),

    ("M2", "① 计费主体回落到别人(不再锚品牌 owner)→ 钱落错人头上",
     SELECTION_API,
     "            billing_user_id = resolve_monitor_billing_user_with_cursor(cursor, _brand_id)\n",
     "            billing_user_id = 1\n"),

    ("M3", "① fail-closed 拆掉:owner 不可确认也照标 is_monitored → 换个入口继续产孤儿",
     SELECTION_API,
     "            if not billing_user_id:\n",
     "            if False:\n"),

    # 🔴 [分诊留档] 原 M4(删掉幂等的 SELECT-first)实跑 **SURVIVED**,分诊 = **空操作变异**:
    #   生产有 `uniq_kms_keyword_active`(UNIQUE(keyword_id) WHERE status IN
    #   ('active','paused_low_balance')),删了 SELECT 之后 INSERT 会撞唯一索引,
    #   再被我新写的 SAVEPOINT 兜底 re-SELECT 回同一行 —— 可观察行为一模一样。
    #   幂等这件事**由数据库索引承重**,应用层那次 SELECT 只是省一次异常。
    #   换成打在真正有风险的地方:SAVEPOINT 没了会把调用方整笔事务打废。
    ("M4", "① 去掉 SAVEPOINT 兜底 → 唯一冲突时把调用方整笔事务打废(本仓前科)",
     MONITORING_DB,
     '    cur.execute("SAVEPOINT kms_create")\n',
     ""),

    # ── ② 展示层 ──────────────────────────────────────────────────────
    ("M5", "② EXISTS 改回读旧字段 → 孤儿词又显示成'开'(工单点名的那条)",
     MONITORING_DB,
     "                    EXISTS (\n"
     "                        SELECT 1 FROM keyword_monitor_subscriptions kms\n"
     "                         WHERE kms.keyword_id = confirmed_keywords.id\n"
     "                           AND kms.status IN ('active', 'paused_low_balance')\n"
     "                    ) as is_monitored,",
     "                    COALESCE(is_monitored, FALSE) as is_monitored,"),

    ("M6", "② 派生口径漏掉 paused_low_balance → 余额不足被显示成'关'",
     MONITORING_DB,
     "                           AND kms.status IN ('active', 'paused_low_balance')\n"
     "                    ) as is_monitored,",
     "                           AND kms.status IN ('active')\n"
     "                    ) as is_monitored,"),

    # ── ③ 归档联动 ────────────────────────────────────────────────────
    ("M7", "③ 归档联动删掉 → 订阅表僵尸 active(富士 ck2339 那种镜像态)",
     SERVER_PY,
     "                cancelled_subs = cancel_keyword_monitor_subscriptions_for_keyword_with_cursor(\n",
     "                cancelled_subs = (lambda *_a, **_k: 0)(\n"),

    ("M8", "③ 取消范围写歪:漏掉 paused_low_balance,只吃 active",
     MONITORING_DB,
     "         WHERE keyword_id = %s\n           AND status IN ('active', 'paused_low_balance')\n    \"\"\", (keyword_id,))\n    return cur.rowcount",
     "         WHERE keyword_id = %s\n           AND status IN ('active')\n    \"\"\", (keyword_id,))\n    return cur.rowcount"),

    ("M9", "③ 恢复又自动复开监测(静默复扣费 · 违按钮级确认扣费)",
     SERVER_PY,
     "                       SET monitoring_status = 'active',\n                           is_monitored = FALSE,\n"
     "                           archive_reason = NULL,\n                           archived_at = NULL\n"
     "                     WHERE id = %s\n                       AND monitoring_status = 'archived'",
     "                       SET monitoring_status = 'active',\n                           is_monitored = TRUE,\n"
     "                           archive_reason = NULL,\n                           archived_at = NULL\n"
     "                     WHERE id = %s\n                       AND monitoring_status = 'archived'"),

    ("M11", "③ 联动挪到 commit 之后 → 归档与取消不再同事务(中间崩=僵尸 active)",
     SERVER_PY,
     '                cancelled_subs = cancel_keyword_monitor_subscriptions_for_keyword_with_cursor(\n'
     '                    cur, int(row["id"]))\n'
     '            conn.commit()\n',
     '            conn.commit()\n'
     '            if row and src == "confirmed":\n'
     '                cancelled_subs = cancel_keyword_monitor_subscriptions_for_keyword_with_cursor(\n'
     '                    cur, int(row["id"]))\n'),

    # ── 计费主体口径 ──────────────────────────────────────────────────
    ("M10", "共享 resolver 回落操作者(不再 fail-closed)→ 与 server.py 那份漂移",
     MONITOR_BILLING,
     "    if not owner:\n",
     "    if False:\n"),
]


def _clean_pyc() -> None:
    shutil.rmtree(_PYC_DIR, ignore_errors=True)


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = str(_PYC_DIR)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_suite() -> bool:
    _clean_pyc()
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", T_MAIN, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1200, env=_child_env())
    return r.returncode == 0


def main() -> int:
    if not os.environ.get("TEST_DATABASE_URL"):
        print("⚠️  没有 TEST_DATABASE_URL:锁会 skip,而 skip 的锁杀不掉变异 —— 结果会是一片 SURVIVED。")
    if not run_suite():
        print("❌ 基线就红 —— 变异结果无意义,先修基线")
        return 2
    print("✅ 基线绿\n")

    results = []
    for mid, desc, rel, old, new in MUTATIONS:
        path = REPO / rel
        original = path.read_bytes()
        old_b, new_b = old.encode("utf-8"), new.encode("utf-8")
        hits = original.count(old_b)
        if hits != 1:
            results.append((mid, f"SKIP(锚点命中 {hits} ≠ 1)"))
            print(f"⚠️  {mid} SKIP:锚点命中 {hits} 次 · {desc}")
            continue
        mutated = original.replace(old_b, new_b)
        assert mutated != original
        try:
            path.write_bytes(mutated)
            green = run_suite()
        finally:
            path.write_bytes(original)
        if path.read_bytes() != original:
            print(f"🔴 {mid} 还原失败!{rel} 与原文不一致,人工介入")
            return 3
        if green:
            results.append((mid, "SURVIVED"))
            print(f"❌ {mid} SURVIVED(变异后仍绿,锁抓不到):{desc}")
        else:
            results.append((mid, "KILLED"))
            print(f"✅ {mid} KILLED:{desc}")

    killed = sum(1 for _, s in results if s == "KILLED")
    survived = [m for m, s in results if s == "SURVIVED"]
    skipped = [m for m, s in results if s.startswith("SKIP")]
    print(f"\n==== 变异结果:{killed} killed / {len(survived)} survived / {len(skipped)} skip ====")
    if survived:
        print("SURVIVED:", ", ".join(survived))
        return 1
    if skipped:
        print("SKIP(锚点问题,不算通过):", ", ".join(skipped))
        return 1
    _clean_pyc()
    print("✅ 全部变异被击杀,锁有判别力")
    return 0


if __name__ == "__main__":
    sys.exit(main())
