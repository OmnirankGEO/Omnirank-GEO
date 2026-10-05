"""包H 后端/跨层判据的**撕锁自证**(6 发)。

与前端那把 runner 同一条纪律:没有自证的锁与恒绿无法区分。
每一发都打在"这条判据声称守住的那一格"上,且**语法合法、语义精确**
(不整段替成废代码 —— 那只是 blunt kill,证明不了锁在守它声称守的东西)。

🔴 还原不用 ``git checkout``(会把同轮其它改动一起撤掉)——
   内存留原文,finally 写回,并核对字节数。

跑法::

    TEST_DATABASE_URL=... python tests/defensive_geo_pkgh_2026_08_23/_mutation_runner.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SUITE = os.path.join("tests", "defensive_geo_pkgh_2026_08_23")

MUTATIONS = [
    (
        "MUT-B1 · R-4:把 hasConflict 重新算进拒绝面(退回「冲突就删事实」)",
        os.path.join("services", "defensive_geo", "presentation", "recommended_facts.py"),
        '    has_conflict, notice = _conflict_fields(bool(entry.get("hasConflict")))',
        '    if entry.get("hasConflict"):\n'
        '        raise FactRejected("mutated: 冲突就拒")\n'
        '    has_conflict, notice = _conflict_fields(bool(entry.get("hasConflict")))',
        "test_accepted_fact_with_conflict_is_still_usable",
    ),
    (
        "MUT-B2 · Z-3.1:让 fact_collection 也能投 business_available",
        os.path.join("services", "defensive_geo", "presentation", "priority_availability.py"),
        '        return _manual("included_route_unavailable")\n\n'
        "    # ── 其余三个 intent",
        '        return AvailabilityVerdict(\n'
        '            availability="business_available", manual_reason_code=None,\n'
        '            handoff_route_key=None, basis_code="mutated",\n'
        '            user_label=copy_registry.translate("availability", "business_available"),\n'
        '        )\n\n'
        "    # ── 其余三个 intent",
        "test_fact_collection_never_business_available_across_full_matrix",
    ),
    (
        "MUT-B3 · U-9:假装监测快照也能重签发",
        os.path.join("services", "defensive_geo", "customer_links.py"),
        'REISSUABLE_KINDS: frozenset[str] = frozenset({"customer_portal"})',
        'REISSUABLE_KINDS: frozenset[str] = frozenset({"customer_portal", "monitoring_snapshot"})',
        "test_only_portal_is_reissuable",
    ),
    (
        "MUT-B4 · Z-3.3:prompt 用 excerpt(整句)而不是命中词",
        os.path.join("services", "defensive_geo", "legal_repair.py"),
        "    return tuple(h.term for h in find_absolute_violations(passage or \"\"))",
        "    return tuple(h.excerpt for h in find_absolute_violations(passage or \"\"))",
        "test_prompt_names_the_hit_terms",
    ),
    (
        "MUT-B5 · Z-4:把同角色同预算替换放进客户可见集合",
        os.path.join("frontend", "src", "pages", "DefensivePublish", "deliveryTodo.ts"),
        "    'media_downgraded_needs_reconfirm',",
        "    'media_downgraded_needs_reconfirm',\n    'media_replaced_same_tier',",
        "test_same_tier_replacement_notice_is_provider_only",
    ),
    (
        "MUT-B6 · X-2:只翻前端那个入口常量(复现终审 P0-1 那次「只摘前端」;"
        "合流后双端=true,故本发改为 true→false,镜像判据同样必须红)",
        os.path.join("frontend", "src", "pages", "DefensivePublish", "publishEntryGate.ts"),
        "export const PUBLISH_CUSTOMER_ENTRY_OPEN = true;",
        "export const PUBLISH_CUSTOMER_ENTRY_OPEN = false;",
        "test_publish_entry_flag_mirrors_backend",
    ),
]


# 🔴 Windows 控制台默认 GBK,直接 print emoji 会 UnicodeEncodeError ——
#    runner 自己崩掉会被读成"判据跑失败",而真相是终端编码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def run_suite(node: str | None = None) -> int:
    cmd = [sys.executable, "-m", "pytest", SUITE, "-q"]
    if node:
        cmd += ["-k", node]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    return r.returncode


def main() -> int:
    print("=== 基线(未注毒)===")
    if run_suite() != 0:
        print("  🔴 基线就不绿 —— 后面的自证无效")
        return 1
    print("  ✅ 基线全绿")

    survived = 0
    restore_broken = False
    for title, rel, frm, to, node in MUTATIONS:
        print(f"\n=== {title} ===")
        path = os.path.join(REPO, rel)
        with io.open(path, encoding="utf-8", newline="") as fh:
            original = fh.read()
        if frm not in original:
            # 🔴 锚点没找到记**没跑**,不记存活:处置完全不同。
            print(f"  🔴 注毒锚点没找到 —— 这一发**没跑**:{frm[:60]!r}")
            survived += 1
            continue
        try:
            with io.open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(original.replace(frm, to, 1))
            code = run_suite(node)
            if code == 0:
                print(f"  🔴 变异存活 —— {node} 没守住这一格")
                survived += 1
            else:
                print(f"  ✅ 转红({node})")
        finally:
            with io.open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(original)
            with io.open(path, encoding="utf-8", newline="") as fh:
                back = fh.read()
            if len(back) != len(original):
                # 🔴 不在 finally 里 return(会吞掉异常)。标记一下,循环外处理。
                print("  🔴 还原后字节数对不上 —— 立刻人工检查")
                restore_broken = True
        # 🔴 [窗口B 2026-08-28 · Review 照准就地修] 跳出**必须在 finally 之外**。
        #    原来这里是 `break` 写在 finally 体内 —— 与 `return` 一样会**丢弃
        #    正在传播的异常**(上一行注释已经知道 return 不行,只是漏了 break)。
        #    后果对 runner 特别贵:变异臂里 pytest 炸掉/还原失败抛出的异常被吞,
        #    循环正常退出 ⇒ 走到汇总 ⇒ 可能 return 0,
        #    于是「跑挂了」与「跑完了零存活」**同形**。
        if restore_broken:
            break

    print("\n================================================================")
    if survived:
        print(f"🔴 {survived} 发存活/没跑 —— 判据有恒绿的格子")
        return 1
    print(f"✅ 撕锁自证通过:{len(MUTATIONS)} 发全部转红")
    return 0


if __name__ == "__main__":
    sys.exit(main())
