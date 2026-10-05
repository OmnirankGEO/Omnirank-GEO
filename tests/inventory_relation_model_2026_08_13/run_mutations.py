"""变异测试 · 工单 §4.1 第 10 条。

存在的理由:**全绿证明不了判据有效**。把实现改坏,测试必须转红;
若改坏了还全绿,说明那条锁是恒真的假绿(本仓反复踩过:
「锁全绿是因为夹具是空的」「归并用『或』并特征 → 恒真」「if False 包住源码字符串锁」)。

用法:
    TEST_DATABASE_URL=... python tests/inventory_relation_model_2026_08_13/run_mutations.py

每个变异都写明**期望转红的那条测试** —— 只说"有测试红了"不够,
红错了地方等于没测到(本仓踩过「底臂被守卫拒跑 → 空集合把既有红显示成包独有」)。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# 🔴 Windows 控制台默认 GBK,输出 emoji/中文会 UnicodeEncodeError 直接崩掉 runner
#    (本仓 2026-08-08 已踩过一次)。修在脚本里,不指望每个调用方都记得传环境变量。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
TESTS = "tests/inventory_relation_model_2026_08_13"

# M3 的锚点含 `"""`,不能写进三引号里 —— 用行拼接构造,避免破坏本文件自身语法。
_UNDIRECTED_OLD = "\n".join([
    "           WHERE buyer_dealer_id=%s AND upstream_channel_account_id=%s",
    "             AND status='active' AND effective_to IS NULL" + '"""' + ",",
    "        (int(target_user_id), int(actor_user_id)),",
])
_UNDIRECTED_NEW = "\n".join([
    "           WHERE ((buyer_dealer_id=%s AND upstream_channel_account_id=%s)",
    "                 OR (buyer_dealer_id=%s AND upstream_channel_account_id=%s))",
    "             AND status='active' AND effective_to IS NULL" + '"""' + ",",
    "        (int(target_user_id), int(actor_user_id),",
    "         int(actor_user_id), int(target_user_id)),",
])

# (标签, 文件, 原文, 替换成, 期望转红的测试节点)
MUTATIONS = [
    (
        "M1 删掉关系解析里查 channel_pricing_relationships 的分支",
        "services/channel_partner_requests.py",
        "    channel = _fetch_directed_channel(cur, actor_id, target_id)",
        "    channel = None  # MUTANT",
        "test_02_downstream_partner_is_resolvable",
    ),
    (
        "M2 删掉 cost_multiplier_bps 读取 → 回落默认(计价锁)",
        "services/channel_partner_requests.py",
        '    bps = int(channel["cost_multiplier_bps"])',
        "    bps = MIN_COST_MULTIPLIER_BPS  # MUTANT 回落默认",
        "test_04_pricing_lock_uses_bound_multiplier",
    ),
    (
        # 🔴 SQL 与 params 必须**一起**改:只改 SQL 会因占位符个数不符报错 ——
        #    那样测试也会红,但红的是语法错,不是方向性失守 = 红对了地方红错了原因。
        "M3 🔴 把有向判定改成无向(也认上游方向)",
        "services/channel_partner_requests.py",
        _UNDIRECTED_OLD,
        _UNDIRECTED_NEW,
        "test_09_downstream_searching_upstream_is_indistinguishable_from_nonexistent",
    ),
    (
        "M4 🔴 让「不存在」与「陌生账号」可区分(拆掉不可区分性)",
        "services/channel_partner_requests.py",
        "        super().__init__(NO_RELATION_CODE, NO_RELATION_MESSAGE)",
        "        super().__init__(NO_RELATION_CODE, NO_RELATION_MESSAGE)\n        self.details = {'exists': True}  # MUTANT",
        "test_08_stranger_and_nonexistent_are_byte_identical",
    ),
    (
        "M5 🔴 删掉响应脱敏(把已绑定系数塞进返回体)",
        "services/channel_partner_requests.py",
        '        "price_cents": int(price_cents),',
        '        "price_cents": int(price_cents), "cost_multiplier_bps": bound_bps,  # MUTANT',
        "test_04d_bound_multiplier_never_appears_in_any_response",
    ),
    (
        "M6 搜索退回只查一张关系表(复现 Owner 指出的『找不到人』)",
        "api/agent_workbench_api.py",
        "    rows = owned_rows + channel_rows",
        "    rows = owned_rows  # MUTANT",
        "test_02b_lookup_sql_finds_downstream_by_phone",
    ),
    # ── P0 热修新增三条(WO_INVREL_P0_HOTFIX §6 第 6 条)──
    (
        "M8 🔴 把划拨动作从 allowed_actions 里删掉(退回被阻断的状态)",
        "services/channel_partner_requests.py",
        '            "allowed_actions": ["supply_downstream", "view_channel", "copy_purchase_message"],',
        '            "allowed_actions": ["view_channel", "copy_purchase_message"],  # MUTANT',
        "test_10_downstream_primary_action_is_executable_supply",
    ),
    (
        "M9 🔴 账本路径改成写可用钱包(下线丧失分销能力)",
        "services/agent_inventory.py",
        '        WHERE agent_user_id = %s\n    """, (down_paid, down_bonus, paid_points + bonus_points, downstream_id))',
        '        WHERE agent_user_id = -1\n    """, (down_paid, down_bonus, paid_points + bonus_points, downstream_id))  # MUTANT',
        "test_61_end_to_end_supply_conserves_both_sides",
    ),
    (
        "M10 🔴 删掉下线侧建 lot(制造新漂移)",
        "services/agent_inventory.py",
        "        supply_lot = credit_channel_supply_lot(",
        "        supply_lot = None if True else credit_channel_supply_lot(  # MUTANT",
        "test_61_end_to_end_supply_conserves_both_sides",
    ),
    (
        # 🔴 Deploy NO-GO 2026-08-13 返修锁:详情侧 DTO 少声明 = 该端点上线即 500。
        #    同型第二次(dto-forbid-extra-outlet-lock-2026-08-06),这次把锁打到**真出口**。
        "M11 🔴 详情侧 DTO 删掉灯字段声明(复现 NO-GO 那条上线即挂)",
        "schemas/admin_user_governance.py",
        "    needs_attention: bool = False\n    attention_label: Optional[str] = None\n\n\nclass EvidenceSummary",
        "    # MUTANT\n\n\nclass EvidenceSummary",
        "test_detail_payload_passes_the_endpoint_response_model",
    ),
    (
        "M7 运行时写关系表(违反 R2)",
        "api/agent_workbench_api.py",
        "        merged = dict(row)",
        "        cursor.execute(\"UPDATE channel_pricing_relationships SET cost_multiplier_bps=cost_multiplier_bps+1 WHERE upstream_channel_account_id=%s\", (int(agent_user_id),))  # MUTANT\n        merged = dict(row)",
        "test_05_runtime_paths_never_write_relationship_tables",
    ),
]


def run_tests() -> tuple[int, str]:
    # 🔴 子进程的输出必须**显式**按 utf-8 解码:Windows 默认 GBK,
    #    pytest 打出中文断言消息时读线程会 UnicodeDecodeError,stdout 变 None,
    #    随后 `stdout + stderr` 抛 TypeError —— runner 自己崩掉,看起来像"测试挂了"。
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TESTS, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    # 反向对照:变异前必须全绿。基线就红的话,后面"红了"毫无意义。
    rc, out = run_tests()
    if rc != 0:
        print("🔴 基线不绿,变异测试无意义:\n" + out[-3000:])
        return 1
    print("✅ 基线全绿\n")

    failures = []
    for label, rel_path, old, new, expected_test in MUTATIONS:
        path = ROOT / rel_path
        original = path.read_text(encoding="utf-8", newline="")
        if old not in original:
            failures.append(f"{label}: 锚点文本未命中 {rel_path} —— 变异没生效 = 假绿")
            print(f"🔴 {label}: 锚点未命中")
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8", newline="")
        try:
            rc, out = run_tests()
        finally:
            path.write_text(original, encoding="utf-8", newline="")
        if rc == 0:
            failures.append(f"{label}: 改坏了仍全绿 = 该条锁是假绿")
            print(f"🔴 {label}\n   → 改坏了仍全绿")
        elif expected_test not in out:
            failures.append(f"{label}: 红了,但红的不是 {expected_test}")
            print(f"🟡 {label}\n   → 红了但不是预期那条({expected_test})")
        else:
            print(f"✅ {label}\n   → 如期转红:{expected_test}")

    # 收尾必须回到全绿,证明还原干净
    rc, _ = run_tests()
    if rc != 0:
        failures.append("还原后基线不绿 —— 源码可能没恢复干净")

    print("\n" + "=" * 60)
    if failures:
        print("🔴 变异测试失败:")
        for f in failures:
            print("  · " + f)
        return 1
    print(f"✅ 全部 {len(MUTATIONS)} 个变异都如期转红,且还原后基线仍全绿")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
