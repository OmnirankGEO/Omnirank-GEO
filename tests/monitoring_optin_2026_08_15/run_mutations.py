"""变异 runner —— 证明本包的锁有判别力(WO_MONITORING_OPTIN_DEFAULT_OFF_2026-08-15 §四.2)。

做法:逐个把修复**拆掉**,跑对应的锁,要求**转红**;然后复位,要求**转绿**。
存活(拆掉了还全绿)= 那条锁是摆设,退回返工。

🔴 Windows 行尾:本文件是"读文件→改字符串→写回"的典型形态,
   `newline=""` **读写两处都加** —— 少一处会把整份文件的 LF 全翻成 CRLF,
   于是"复位"之后 git 仍显示整文件被改(本仓踩过多次,同仓修过还照样重犯)。
   跑完自带 `git status --porcelain` 自检,不留残迹。

用法:
  TEST_DATABASE_URL=... python -X utf8 tests/monitoring_optin_2026_08_15/run_mutations.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = "tests/monitoring_optin_2026_08_15"

# M17 变异体 = 一个「无闸取词函数」。用 chr() 拼装,整段不含三引号、不含反斜杠转义 ——
# 本会话里带三引号的变异体把外层字符串提前终结过一次,反斜杠转义又被写入链路吃掉过一次。
_NL = chr(10)
_SQ = chr(39)
_M17_MUTANT = _NL.join([
    "def get_active_client_keywords():",
    "    conn = get_connection()",
    "    cur = conn.cursor()",
    "    cur.execute(" + _SQ + "SELECT e.id, e.keyword FROM extra_keywords e JOIN quotes q ON e.quote_id = q.id WHERE e.status = active" + _SQ + ")",
    "    return [dict(r) for r in cur.fetchall()]",
    "",
    "",
    "def get_expiring_tokens(days: int = 7)",
])

# (编号, 说明, 相对路径, 原文, 变异文, 该转红的测试选择器)
MUTATIONS = [
    (
        "M1", "拆掉闸本身(取词不再看 is_monitored)",
        "db/monitoring_db.py",
        '"AND COALESCE(e.is_monitored, FALSE) = TRUE" if for_dispatch else ""',
        '"" if for_dispatch else ""',
        [f"{SUITE}/test_monitoring_optin_default_off.py::test_A1_A2_quote_scope_gate_both_states",
         f"{SUITE}/test_monitoring_optin_default_off.py::test_A3_brand_scope_gate"],
    ),
    (
        "M2", "只修一条分支(quote 级加闸 · brand 级漏掉)—— 本单最要命的失败形态",
        "db/monitoring_db.py",
        "WHERE e.brand_id = %s AND e.status = 'active'\n                          {extra_dispatch_gate_sql}",
        "WHERE e.brand_id = %s AND e.status = 'active'",
        [f"{SUITE}/test_monitoring_optin_default_off.py::test_A3_brand_scope_gate",
         f"{SUITE}/test_optin_wiring.py::test_W2b_gate_sql_is_applied_to_both_bulk_branches"],
    ),
    (
        "M3", "只修另一条分支(brand 级加闸 · quote 级漏掉)",
        "db/monitoring_db.py",
        "                  -- [WO_MONITORING_OPTIN 2026-08-15] quote 级\"跑全部\" · 加开关闸(默认关)\n                  {extra_dispatch_gate_sql}\n",
        "",
        [f"{SUITE}/test_monitoring_optin_default_off.py::test_A1_A2_quote_scope_gate_both_states",
         f"{SUITE}/test_optin_wiring.py::test_W2b_gate_sql_is_applied_to_both_bulk_branches"],
    ),
    (
        "M4", "把默认值改成开(迁移 DEFAULT FALSE → TRUE)",
        "scripts/migration_monitoring_extra_optin_2026_08_15.sql",
        "ADD COLUMN IF NOT EXISTS is_monitored BOOLEAN NOT NULL DEFAULT FALSE;",
        "ADD COLUMN IF NOT EXISTS is_monitored BOOLEAN NOT NULL DEFAULT TRUE;",
        [f"{SUITE}/test_monitoring_optin_default_off.py::test_C1_column_exists_notnull_default_false",
         f"{SUITE}/test_monitoring_optin_default_off.py::test_C2_add_keyword_lands_unmonitored",
         f"{SUITE}/test_optin_wiring.py::test_W5_migration_has_no_dml"],
    ),
    (
        "M5", "恢复硬编码 FALSE as is_monitored(界面又画不出开关)",
        "db/monitoring_db.py",
        "COALESCE(extra_keywords.is_monitored, FALSE) as is_monitored,",
        "FALSE as is_monitored,",
        [f"{SUITE}/test_monitoring_optin_default_off.py::test_B1_display_follows_real_column",
         f"{SUITE}/test_optin_wiring.py::test_W3_hardcoded_false_is_gone"],
    ),
    (
        "M6", "闸的默认值改成不加闸(新调用方默认绕过)",
        "db/monitoring_db.py",
        "    for_dispatch: bool = True,",
        "    for_dispatch: bool = False,",
        [f"{SUITE}/test_monitoring_optin_default_off.py::test_A5_for_dispatch_default_true_and_report_optout"],
    ),
    (
        "M7", "前端逐行开关退回 confirmed-only(手动词又没开关)",
        "frontend/src/pages/Monitoring/components/KeywordTable.tsx",
        "                        {onToggleMonitor && (",
        "                        {kw.source === 'confirmed' && onToggleMonitor && (",
        [f"{SUITE}/test_optin_wiring.py::test_W6_frontend_switch_not_confirmed_only"],
    ),
    (
        "M8", "前端批量计数退回 confirmed-only",
        "frontend/src/pages/Monitoring/components/KeywordTable.tsx",
        "const enableable = keywords.filter(kw => !kw.is_monitored).length;",
        "const enableable = keywords.filter(kw => kw.source === 'confirmed' && !kw.is_monitored).length;",
        [f"{SUITE}/test_optin_wiring.py::test_W6_frontend_switch_not_confirmed_only"],
    ),
    (
        "M9", "迁移不登记 manifest(上线后永远不会跑)",
        "db/migration_manifest.py",
        '    "scripts/migration_monitoring_extra_optin_2026_08_15.sql",',
        "",
        [f"{SUITE}/test_optin_wiring.py::test_W4_migration_registered"],
    ),
    (
        "M10", "回填脚本不真写(apply 变成空转)",
        "scripts/backfill_monitoring_extra_optin_2026_08_15.py",
        "        if apply and need_update:",
        "        if False and need_update:",
        [f"{SUITE}/test_monitoring_optin_default_off.py::test_E1_E2_backfill_dryrun_then_apply_then_idempotent"],
    ),
    (
        "M11", "回填守卫拆掉(命中数不对也照写)",
        "scripts/backfill_monitoring_extra_optin_2026_08_15.py",
        "        if len(targets) != EXPECTED_TARGET_COUNT:",
        "        if False:",
        [f"{SUITE}/test_monitoring_optin_default_off.py::test_E3_guard_refuses_when_target_set_drifted"],
    ),
    (
        "M12", "孤儿收口整个不写(总数归不了零)",
        "scripts/backfill_orphan_monitor_2026_08_15.py",
        "        if apply:\n            cur.execute(\n                \"\"\"\n                UPDATE confirmed_keywords",
        "        if False:\n            cur.execute(\n                \"\"\"\n                UPDATE confirmed_keywords",
        [f"{SUITE}/test_orphan_backfill_discriminating_power.py::test_F2_F4_backfill_closes_orphans_by_evidence"],
    ),
    (
        # 🔴 这条打的是**真正承重**的那个守卫。
        #   第一版 M13 打的是 paid/unpaid 分流,结果**存活** —— 因为分流在写路径上是装饰,
        #   拦住有凭证那批的一直是这个 NOT EXISTS。变异逼出了实现的订正(见脚本 §3 注释),
        #   现在锁钉在承重点上:拆掉它 → 刚补好订阅的真客户也被一刀关掉 → F4 必红。
        "M13", "孤儿收口拆掉「已有 active 订阅就别关」守卫(真客户被误停)",
        "scripts/backfill_orphan_monitor_2026_08_15.py",
        """                 WHERE is_monitored = TRUE
                   AND NOT EXISTS (
                       SELECT 1 FROM keyword_monitor_subscriptions kms
                        WHERE kms.keyword_id = confirmed_keywords.id AND kms.status = 'active'
                   )
""",
        "                 WHERE is_monitored = TRUE\n",
        [f"{SUITE}/test_orphan_backfill_discriminating_power.py::test_F2_F4_backfill_closes_orphans_by_evidence"],
    ),
    (
        # 生产真形态那条分支的锁:清洗数据时把服务缺口一起洗掉,是这次真数据实测暴露的风险
        "M15", "「付费未监测」清单不落盘(服务缺口随数据清洗被抹掉)",
        "scripts/backfill_orphan_monitor_2026_08_15.py",
        "    if paid_but_unmonitored:",
        "    if False:",
        [f"{SUITE}/test_orphan_backfill_discriminating_power.py::test_F5_production_shape_confirmed_paid_at_is_reported_not_silently_dropped"],
    ),
    (
        "M16", "清单在关掉之后才取(取到空清单 · 缺口凭空消失)",
        "scripts/backfill_orphan_monitor_2026_08_15.py",
        "        paid_but_unmonitored = _paid_but_unmonitored(cur)",
        "        paid_but_unmonitored = []",
        [f"{SUITE}/test_orphan_backfill_discriminating_power.py::test_F5_production_shape_confirmed_paid_at_is_reported_not_silently_dropped"],
    ),
    (
        # Review 2026-08-15 追加:被删的第 5 个出口(无闸取词函数)一旦复活,结构锚必须抓到。
        # 旧的语义签名对它是瞎的(它不 SELECT entitlement_platforms / monitoring_query)。
        "M17", "复活无闸取词函数(语义签名看不见 · 结构锚必须看得见)",
        "db/monitoring_db.py",
        "def get_expiring_tokens(days: int = 7)",
        _M17_MUTANT,
        [f"{SUITE}/test_optin_wiring.py::test_W1_every_extra_keywords_site_is_classified",
         f"{SUITE}/test_optin_wiring.py::test_W1d_dead_ungated_fetcher_stays_deleted"],
    ),
    (
        # 归类表被删条目 → 唯一漏斗变成未归类 → W1 必红(证明表不是摆设)。
        "M18", "把唯一漏斗从归类表里移走(伪装成不存在)",
        "tests/monitoring_optin_2026_08_15/test_optin_wiring.py",
        '    ("db/monitoring_db.py", "get_keywords_for_monitoring"): "dispatch",',
        "",
        [f"{SUITE}/test_optin_wiring.py::test_W1_every_extra_keywords_site_is_classified"],
    ),
    (
        "M14", "加词端点不再明示未开启监测",
        "api/monitoring_api.py",
        '            "message": "已添加 · 未开启监测。点开关后才开始监测并按 130 算力/词/次 扣费。",',
        '            "message": "已添加。",',
        [f"{SUITE}/test_optin_wiring.py::test_W8_add_keyword_response_says_not_monitored"],
    ),
]


def _read(rel: str) -> str:
    # 🔴 newline="" 读:不让 Python 把 CRLF 归一成 LF(否则写回时会整份翻)
    with open(ROOT / rel, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write(rel: str, text: str) -> None:
    # 🔴 newline="" 写:不让 Python 把 LF 翻成 CRLF
    with open(ROOT / rel, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _pytest(selectors: list[str]) -> int:
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", "-q", "--no-header", *selectors],
        cwd=ROOT, capture_output=True, text=True,
    ).returncode


def main() -> int:
    if not os.environ.get("TEST_DATABASE_URL"):
        print("🔴 需要 TEST_DATABASE_URL —— 没有库的变异是假绿(锁会 skip,skip 不是红)")
        return 2

    # 基线:动手之前必须全绿,否则后面分不清"变异杀死"和"本来就红"
    base_all = _pytest([SUITE])
    if base_all != 0:
        print(f"🔴 基线不绿(rc={base_all}),先修基线再跑变异")
        return 2
    print("✅ 基线全绿")

    survived, killed = [], []
    for mid, desc, rel, old, new, selectors in MUTATIONS:
        original = _read(rel)
        if old not in original:
            print(f"🔴 {mid} 锚点不存在于 {rel} —— 变异没生效等于没测(判据失效)")
            return 2
        try:
            _write(rel, original.replace(old, new, 1))
            rc_mut = _pytest(selectors)
        finally:
            _write(rel, original)
        rc_restore = _pytest(selectors)

        if rc_mut == 0:
            survived.append((mid, desc))
            print(f"🔴 {mid} 存活(拆掉了还全绿):{desc}")
        elif rc_restore != 0:
            survived.append((mid, f"{desc} · 复位后没转回绿(锁不稳定)"))
            print(f"🔴 {mid} 复位后仍红:{desc}")
        else:
            killed.append(mid)
            print(f"✅ {mid} 被杀 · 复位转绿:{desc}")

    porcelain = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
    print("\n================ 变异结果 ================")
    print(f"杀死 {len(killed)}/{len(MUTATIONS)} · 存活 {len(survived)}")
    for mid, desc in survived:
        print(f"  存活 {mid}:{desc}")
    print(f"git status --porcelain 行数 = {len(porcelain.splitlines()) if porcelain else 0}")
    if porcelain:
        print(porcelain)
        print("🔴 变异 runner 留下了残迹(多半是行尾被翻)")
        return 1
    return 0 if not survived else 1


if __name__ == "__main__":
    sys.exit(main())
