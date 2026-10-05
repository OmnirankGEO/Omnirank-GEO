#!/usr/bin/env python
"""按需铸造第一步 · 八个变异(工单 §6)。每个变异必须让指定判别锁转红。

用法(worktree 根目录):
    DEALER_RESALE_TEST_DATABASE_URL=... TEST_DATABASE_URL=... ALLOW_DESTRUCTIVE_TEST_DB=1 \
    python scripts/mutation_ondemand_minting_2026_07_29.py

🔴 还原方式:内存里备份原文件字节,finally 写回。
   **不用 git checkout / git stash 还原** —— 那会连带冲掉工作区里尚未提交的改动
   (同类事故已发生过)。脚本退出前会逐文件比对字节确认已还原。

🔴 变异 ⑥ 的正确做法:工单指出"把分布断言改成恒真"不是有效变异(恒真只会更绿)。
   这里变异的是**实现**(让 mint_distribution_status 恒判 healthy),
   而判别锁 9 靠**数据层注入**「有订单但零铸造」的样本来判——所以它必然转红。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "services" / "inventory_minting_guard.py"
RESALE = ROOT / "services" / "dealer_inventory_resale.py"
INVENTORY = ROOT / "services" / "agent_inventory.py"
LOCKS = "tests/dealer_inventory_resale/test_ondemand_minting_2026_07_29.py"


MUTATIONS = [
    {
        "id": "①",
        "name": "去掉订单号强制(护栏 3)",
        "expect_red": ["test_lock5_orphan_mint_is_rejected_and_alerted",
                       "test_lock5_bonus_path_shares_the_same_guard"],
        "edits": [(
            GUARD,
            "    if not order_id or not _order_exists(cursor, order_id):",
            "    if False:  # MUTATION-1",
        )],
    },
    {
        "id": "②",
        "name": "去掉一致性校验(护栏 1)",
        "expect_red": ["test_lock4_mint_amount_over_declaration_is_rejected"],
        "edits": [
            (
                GUARD,
                "    if plan_declared_points is not None and int(plan_declared_points) != points:",
                "    if False:  # MUTATION-2a",
            ),
            (
                GUARD,
                "    if capacity <= 0 or points > capacity:",
                "    if False:  # MUTATION-2b",
            ),
        ],
    },
    {
        "id": "③",
        "name": "单笔上限调到无穷(护栏 2)",
        "expect_red": ["test_lock6_single_mint_over_cap_is_rejected"],
        "edits": [(
            GUARD,
            "    if points > max_single:",
            "    if False:  # MUTATION-3",
        )],
    },
    {
        "id": "④",
        "name": "铸造不写流水(破守恒)",
        "expect_red": ["test_lock8_conservation_holds_before_and_after_mint"],
        "edits": [(
            RESALE,
            """        \"\"\"INSERT INTO agent_inventory_transactions
           (agent_user_id,type,pool,points,balance_paid_after,balance_bonus_after,
            related_order_id,description,lot_id,cost_basis_cents)
           VALUES (%s,'manufacturer_origin_in','paid',%s,%s,%s,%s,%s,%s,%s)\"\"\",""",
            '        """SELECT %s,%s,%s,%s,%s,%s,%s,%s""",  # MUTATION-4',
        )],
    },
    {
        "id": "⑤",
        "name": "bonus 另写一份护栏(不复用同一实现)",
        "expect_red": ["test_lock5_bonus_path_shares_the_same_guard",
                       "test_admin_adjust_requires_an_order_too"],
        "edits": [(
            INVENTORY,
            """    from services import inventory_minting_guard as guard

    guard.assert_mint_allowed(
        cursor,
        source=source,
        agent_user_id=int(agent_user_id),
        pool=str(pool),
        points=int(points),
        related_order_id=related_order_id,
    )""",
            """    # MUTATION-5:各写一份
    if int(points) <= 0:
        raise ValueError("bonus 自建护栏:算力必须为正")""",
        )],
    },
    {
        "id": "⑦",
        "name": "去掉结算幂等短路(同订单可双铸)",
        "expect_red": ["test_lock1b_same_order_settled_twice_does_not_double_mint"],
        "edits": [(
            RESALE,
            '    if plan["state"] == "settled":',
            "    if False:  # MUTATION-7",
        )],
    },
    {
        "id": "⑧",
        "name": "平台跳总是铸全额(不先回收余额)",
        "expect_red": ["test_lock1c_platform_balance_is_consumed_before_minting"],
        "edits": [(
            RESALE,
            """    recycled = fifo_available_allocations(
        cur, int(platform_seller_user_id), points,
        platform_campaign_id=platform_campaign_id,
        platform_campaign_product_code=platform_campaign_product_code,
        platform_campaign_root_catalog_version=platform_campaign_root_catalog_version,
        exclude_source_kinds=PLATFORM_DORMANT_SOURCE_KINDS,
        for_update=for_update,
    )""",
            """    recycled = {  # MUTATION-8
        "allocations": [], "existing_inventory_points": 0,
        "jit_shortfall_points": points, "existing_cost_basis_cents": 0,
    }""",
        )],
    },
    {
        "id": "⑥",
        "name": "分布检查实现恒判健康(锁 9 靠数据注入,必红)",
        "expect_red": ["test_lock9_mint_distribution_tracks_orders",
                       "test_lock9_detects_mint_exceeding_orders"],
        "edits": [(
            GUARD,
            """    if actual == expected:
        form = None
    elif actual < expected:
        form = "silent_zero_mint"
    else:
        form = "mint_exceeds_orders\"""",
            '    form = None  # MUTATION-6',
        )],
    },
]


def run_locks(node_ids: list[str]) -> tuple[int, str]:
    env = dict(os.environ)
    args = [sys.executable, "-m", "pytest", "-p", "no:randomly", "-q", "--tb=no"]
    args += [f"{LOCKS}::{name}" for name in node_ids]
    proc = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def reset_test_db() -> None:
    subprocess.run(
        ["docker", "exec", "omnirank-dealer-r3-pg", "psql", "-U", "dealer_test",
         "-d", "postgres", "-At", "-c",
         "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
         "WHERE datname='dealer_resale_test' AND pid<>pg_backend_pid()"],
        capture_output=True, text=True,
    )


def main() -> int:
    # Windows 控制台默认 GBK,强制 UTF-8 输出,免得 emoji/中文把结论打断
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    originals = {path: path.read_bytes() for path in (GUARD, RESALE, INVENTORY)}
    failures: list[str] = []
    try:
        reset_test_db()
        code, out = run_locks([m for mu in MUTATIONS for m in mu["expect_red"]])
        if code != 0:
            print("BASELINE 不绿,变异结论无意义:")
            print(out[-3000:])
            return 2
        print("BASELINE: 目标锁全绿\n")

        for mutation in MUTATIONS:
            for path, old, new in mutation["edits"]:
                text = path.read_text(encoding="utf-8")
                if old not in text:
                    failures.append(f"{mutation['id']} 变异锚点未命中 {path.name}")
                    break
                path.write_text(text.replace(old, new, 1), encoding="utf-8")
            else:
                reset_test_db()
                code, out = run_locks(mutation["expect_red"])
                verdict = "KILLED(锁转红)" if code != 0 else "SURVIVED(锁仍绿)"
                print(f"变异 {mutation['id']} {mutation['name']} → {verdict}")
                if code == 0:
                    failures.append(f"{mutation['id']} {mutation['name']} SURVIVED")
                    print(out[-1500:])
            for path in originals:
                path.write_bytes(originals[path])
    finally:
        for path, content in originals.items():
            path.write_bytes(content)
        for path, content in originals.items():
            assert path.read_bytes() == content, f"{path} 未还原!"
        print("\n所有被变异文件已按字节还原。")

    if failures:
        print("\n❌ 变异结论:")
        for item in failures:
            print("  -", item)
        return 1
    print("\n✅ 八个变异全部 KILLED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
