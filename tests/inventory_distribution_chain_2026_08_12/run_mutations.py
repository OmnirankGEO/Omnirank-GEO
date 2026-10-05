#!/usr/bin/env python3
"""变异验证 · 证明本包的判别锁**不是恒绿的**。

存在的理由:全绿本身零信息量 —— 一个什么都不断言的测试套件也全绿。
这里逐个把实现改坏,要求测试**必须转红**;存活(全绿)的变异说明那条锁形同虚设。

🔴 Windows 三坑(本仓踩过):
  1. 用 Python 做文本替换,不要用 PowerShell `-replace`(它会吃掉 `$'` 这类序列)
  2. 读写一律显式 encoding="utf-8" + newline=""(否则行尾污染整个 diff)
  3. 子进程环境要显式带 PYTHONIOENCODING,否则中文断言消息在 GBK 控制台炸

用法:
    TEST_DATABASE_URL=... python tests/inventory_distribution_chain_2026_08_12/run_mutations.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = "tests/inventory_distribution_chain_2026_08_12"

# (标签, 相对路径, 原文, 改坏后的文本, 期望被哪条锁抓到)
MUTATIONS = [
    (
        "M1 · lot 不消耗(复现 u133 漂移的形态)",
        "services/inventory_lot_ledger.py",
        "    allocations = resale.fifo_allocations(cur, int(owner_agent_user_id), points)",
        "    allocations = []",
        "test_admin_allocate_creates_binding_in_same_transaction / test_self_use_conversion_is_conserved",
    ),
    (
        "M2 · 差额守恒断言被拆掉",
        "services/inventory_lot_ledger.py",
        "    after = drift_points_of(cur, int(agent_user_id))\n    if after != int(before):",
        "    after = drift_points_of(cur, int(agent_user_id))\n    if False:",
        "test_assert_drift_unchanged_is_a_real_guard",
    ),
    (
        "M3 · 亮灯判据的补录分支不再要求 backfill 标记",
        "services/admin_user_governance.py",
        "              AND uga.evidence_jsonb->>'backfill' = 'true'\n"
        "              AND uga.evidence_jsonb->>'original_bound_at'\n"
        "                  = to_char(cab.bound_at, '{BINDING_BACKFILL_TIME_FORMAT}')",
        "              AND 1=1",
        "test_unmarked_audit_does_not_exempt_binding",
    ),
    (
        "M4 · 建绑定不写审计",
        "services/admin_user_governance.py",
        "    next_version = _advance_version(cur, user_id, \"commercial_binding\", current_version)\n"
        "    _write_audits(\n"
        "        cur, subject_user_id=user_id, scope=\"commercial_binding\",",
        "    next_version = _advance_version(cur, user_id, \"commercial_binding\", current_version)\n"
        "    _write_audits = (lambda *a, **kw: None)  # noqa: F811\n"
        "    _write_audits(\n"
        "        cur, subject_user_id=user_id, scope=\"commercial_binding\",",
        "test_binding_via_governance_service_does_not_light_attention",
    ),
    (
        "M5 · 原因可以为空",
        "services/admin_agent_inventory.py",
        "    if len(text) < 2:\n        raise InventoryAdminError(\"REASON_REQUIRED\"",
        "    if False:\n        raise InventoryAdminError(\"REASON_REQUIRED\"",
        "test_admin_adjust_rejects_blank_reason",
    ),
    (
        "M6 · 不再拦「把服务商当客户」",
        "services/admin_agent_inventory.py",
        "    if int(target[\"agent_level\"] or 0) >= 1:",
        "    if False:",
        "test_admin_allocate_rejects_service_provider_target_with_channel_guidance",
    ),
    (
        "M7 · 归属他人也允许划拨(工单三选一里被否掉的方案 3)",
        "services/admin_agent_inventory.py",
        "    if binding:\n        # 工单 §P0-1 三选一 → 选 (1) 拒绝并提示走换绑流程。",
        "    if False:\n        # 工单 §P0-1 三选一 → 选 (1) 拒绝并提示走换绑流程。",
        "test_admin_allocate_rejects_customer_bound_to_other_provider",
    ),
    (
        "M8 · 管理动作不留痕",
        "services/admin_agent_inventory.py",
        "        cur.execute(\"SAVEPOINT admin_inventory_action\")",
        "        cur.execute(\"SAVEPOINT admin_inventory_action\")\n"
        "        if True:\n            return -1",
        "test_admin_adjust_increase_records_operator_and_reason",
    ),
    (
        "M9 · 库存不是服务商也能调",
        "services/admin_agent_inventory.py",
        "    if int(wallet[\"agent_level\"] or 0) < 1:",
        "    if False:",
        "test_admin_adjust_rejects_non_provider_target",
    ),
    (
        "M10 · 自用转换只给一半(守恒被破坏)",
        "services/admin_agent_inventory.py",
        "                tool_points=paid_points, publish_points=0, bonus_points=bonus_points,\n"
        "                description=f\"库存转可用算力(自用)· {reason}\",",
        "                tool_points=paid_points // 2, publish_points=0, bonus_points=bonus_points,\n"
        "                description=f\"库存转可用算力(自用)· {reason}\",",
        "test_self_use_conversion_is_conserved",
    ),
    (
        "M11 · 进货系数下界失效",
        "services/channel_partner_requests.py",
        "    if bps < MIN_COST_MULTIPLIER_BPS:",
        "    if False:",
        "test_channel_request_rejects_below_cost_multiplier",
    ),
    (
        "M12 · 普通用户也能被当成下级服务商",
        "services/channel_partner_requests.py",
        "        if path[\"path\"] != \"channel_relationship\":",
        "        if False:",
        "test_preflight_routes_ordinary_user_to_binding_path",
    ),
    (
        "M13 · 补录顺手改绑定(工单明令禁止)",
        "services/admin_user_governance.py",
        "        current_version = _lock_version(cur, user_id, \"commercial_binding\", expected_version)\n"
        "        provider_user_id = int(binding[\"agent_user_id\"])",
        "        cur.execute(\n"
        "            \"UPDATE customer_agent_bindings SET bound_at=NOW() WHERE customer_user_id=%s\",\n"
        "            (int(user_id),),\n"
        "        )\n"
        "        current_version = _lock_version(cur, user_id, \"commercial_binding\", expected_version)\n"
        "        provider_user_id = int(binding[\"agent_user_id\"])",
        "test_backfill_never_writes_binding",
    ),
    (
        "M14 · 已有凭证也允许重复补录",
        "services/admin_user_governance.py",
        "        if not row or not bool(row[\"needs_attention\"]):",
        "        if False:",
        "test_backfill_refuses_when_not_lit",
    ),
    (
        "M15 · lot 漂移查询恒返回空(假判据)",
        "services/inventory_lot_ledger.py",
        "        if int(row[\"drift_points\"] or 0) != 0:",
        "        if False:",
        "test_lot_drift_rows_is_a_real_criterion",
    ),
    # ---- [R1 返修 §2] 消 lot 下沉到 allocate_offline 之后新增的两条 ----
    (
        "M16 · allocate_offline 不消 lot(漏账源复原)",
        "services/agent_inventory.py",
        "        from services.inventory_lot_ledger import consume_lots_fifo\n"
        "\n"
        "        consume_lots_fifo(cursor, agent_user_id, paid_points, reason=description)",
        "        pass",
        "test_allocate_offline_consumes_lot_and_keeps_drift / test_admin_allocate_consumes_lot_exactly_once",
    ),
    (
        "M17 · 消 lot 时把 bonus 也算进去(多消有价库存)",
        "services/agent_inventory.py",
        "        consume_lots_fifo(cursor, agent_user_id, paid_points, reason=description)",
        "        consume_lots_fifo(\n"
        "            cursor, agent_user_id, paid_points + bonus_points, reason=description\n"
        "        )",
        "test_allocate_offline_consumes_only_paid_not_bonus / test_agent_rebate_bonus_only_is_lot_noop",
    ),
]


def run_suite(env: dict) -> bool:
    """返回 True = 全绿。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "-p", "no:cacheprovider", "--no-header"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0


def main() -> int:
    # 🔴 [R1 返修] runner **自己**也要 UTF-8:上面 docstring 第 3 坑写的就是这件事,
    #    但原实现只给子进程带了 PYTHONIOENCODING,自己 print("✅") 在 GBK 控制台
    #    直接 UnicodeEncodeError —— 崩在收尾行,15/15 的结论差点没打出来。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):  # 非 TTY / 已被重定向
            pass
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if not env.get("TEST_DATABASE_URL"):
        print("🔴 需要 TEST_DATABASE_URL")
        return 2
    env.setdefault("DATABASE_URL", env["TEST_DATABASE_URL"])

    print("=== 基线:未变异必须全绿(否则后面的红分不清是变异还是本来就红)===")
    if not run_suite(env):
        print("🔴 基线就是红的,变异验证无意义")
        return 2
    print("✅ 基线全绿\n")

    killed, survived = [], []
    for label, rel_path, old, new, expected in MUTATIONS:
        path = ROOT / rel_path
        original = io.open(path, encoding="utf-8", newline="").read()
        if old not in original:
            # 🔴 改不上就当场报错,绝不静默跳过 ——
            #    "变异没生效却看到全绿"会被误读成"锁很强",本仓踩过。
            print(f"🔴 {label} · 锚点在 {rel_path} 里找不到,变异未生效(判据作废)")
            survived.append((label, "锚点失配"))
            continue
        io.open(path, "w", encoding="utf-8", newline="").write(original.replace(old, new, 1))
        try:
            green = run_suite(env)
        finally:
            io.open(path, "w", encoding="utf-8", newline="").write(original)
        if green:
            print(f"🔴 存活 {label}  ← 期望被 {expected} 抓到,但套件仍然全绿")
            survived.append((label, expected))
        else:
            print(f"✅ 杀死 {label}")
            killed.append(label)

    print(f"\n=== 结果:杀死 {len(killed)} / {len(MUTATIONS)} ===")
    for label, expected in survived:
        print(f"   存活:{label}(期望 {expected})")
    return 0 if not survived else 1


if __name__ == "__main__":
    sys.exit(main())
