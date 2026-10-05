"""[P0-1/P0-2 2026-08-15] 变异运行器:亲手拆掉每一道锁,看它转不转红。

理由:「过了」和「没判别力」在报告上长得一模一样。只有把修复逐条拆掉、看对应的锁翻红,
才能证明这些用例测的是要测的东西。

🔴 Windows 行尾:读写两处都带 `newline=""`。同仓早就修过一次,新 runner 照样重犯过 ——
   少了任何一处,整个文件会被静默翻成 CRLF,变异跑完 `git status` 就不是干净的了。

用法:
    python tests/flywheel_binding_liveness_2026_08_15/mutation_runner.py          # 全部变异
    python tests/flywheel_binding_liveness_2026_08_15/mutation_runner.py --list
退出码:0 = 全部变异都被杀死;1 = 有存活变异(锁没判别力)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 GBK,中文/emoji 直接崩
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PY_SUITE = ["tests/flywheel_binding_liveness_2026_08_15", "tests/flywheel_integration/test_t6_binding_approve_all.py"]
PW_CMD = ["npx", "playwright", "test", "--config", "playwright.flywheel-binding.config.cjs"]


class Mutation:
    def __init__(self, name: str, path: str, old: str, new: str, kind: str = "py", note: str = ""):
        self.name, self.path, self.old, self.new, self.kind, self.note = name, path, old, new, kind, note


MUTATIONS = [
    Mutation(
        "M1 审核判定抄回 verify(不再调 evaluate_candidate_live)",
        "services/media_binding_candidates.py",
        """    verdict = evaluate_candidate_live(entity, reloaded_inventory, candidate_payload)
    if verdict["block_reason"]:
        raise ValueError(verdict["block_reason"])
    return verdict["candidate"]""",
        """    candidate_payload = candidate_payload or {}
    trusted_row = normalize_inventory_row(reloaded_inventory)
    candidates = build_binding_candidates(entity, [trusted_row], limit=1)
    if not candidates:
        raise ValueError(BLOCK_NO_MATCH)
    candidate = candidates[0]
    if not candidate.get("can_approve"):
        raise ValueError("、".join(candidate.get("risk_flags") or []) or BLOCK_FALLBACK)
    return candidate""",
        note="这就是「两处各写一遍」的原形态",
    ),
    Mutation(
        "M2 选集退回读快照列(原 bug 本体)",
        "db/media_entity_flywheel_db.py",
        """    rows = _list_candidate_rows_for_revalidation(REVALIDATION_SCAN_LIMIT)
    fresh = revalidate_binding_candidate_rows(rows)
    picked = [r for r in fresh
              if not r.get("live_block_reason")""",
        """    rows = _list_candidate_rows_for_revalidation(REVALIDATION_SCAN_LIMIT)
    fresh = list(rows)
    picked = [r for r in fresh
              if r.get("can_approve")""",
        note="还原生产死锁:选集信 08-05 的快照",
    ),
    Mutation(
        "M3 重算结果不覆盖快照 can_approve",
        "db/media_entity_flywheel_db.py",
        """        merged["live_block_reason"] = reason
        merged["can_approve"] = not reason""",
        """        merged["live_block_reason"] = reason
        merged["can_approve"] = bool(row.get("can_approve"))""",
        note="算了但不用 = 假装同源",
    ),
    Mutation(
        "M4 把「下架的 4 条」写死排除(而非实时判定)",
        "db/media_entity_flywheel_db.py",
        """    rows = _list_candidate_rows_for_revalidation(REVALIDATION_SCAN_LIMIT)
    fresh = revalidate_binding_candidate_rows(rows)
    picked = [r for r in fresh
              if not r.get("live_block_reason")""",
        """    rows = _list_candidate_rows_for_revalidation(REVALIDATION_SCAN_LIMIT)
    _HARDCODED_BAD = {20564, 20565, 20693, 20694}
    fresh = [r for r in rows if int(r.get("id") or 0) not in _HARDCODED_BAD]
    picked = [r for r in fresh
              if r.get("can_approve")""",
        note="🔴 工单点名的假修法:不重算,只把那 4 条写死排除。反向对照必须抓到",
    ),
    Mutation(
        "M5 待审列表不走实时判据(前端标签仍读陈旧快照)",
        "db/media_entity_flywheel_db.py",
        """    revalidated = {int(r["id"]): r for r in revalidate_binding_candidate_rows(pending)}
    return [revalidated.get(int(r["id"]), r) for r in rows]""",
        """    return rows""",
        note="服务层修了、列表出口没接上",
    ),
    Mutation(
        "M6 失败项不带媒体名(原因等于又丢了)",
        "api/media_entity_flywheel_api.py",
        """        "media_name": src.get("media_name") or "",""",
        """        "media_name": "",""",
        note="前端只能显示一串 id",
    ),
    Mutation(
        "M7 toast 分档拆掉(全失败照样绿勾)",
        "frontend/src/pages/Admin/GeoPlacementFlywheel.tsx",
        """    if (approved === 0) {
      // 全失败 = 这次点击什么都没做成,不许显示成功样式
      toast.error(`${failures.length} 条全部未成功，一条也没通过 · 原因见下方「未成功明细」`);
      return;
    }
    toast.warning(`已通过 ${approved} 条，${failures.length} 条未成功 · 原因见下方「未成功明细」`);""",
        """    toast.success(`已通过 ${approved} 条,${failures.length} 条未成功`);""",
        kind="pw",
        note="还原修前那一行(GeoPlacementFlywheel.tsx:1631)",
    ),
    Mutation(
        "M8 失败明细面板不渲染(原因回到只活在 toast 里)",
        "frontend/src/pages/Admin/GeoPlacementFlywheel.tsx",
        """                {bindingFailures && bindingFailures.failures.length > 0 && (""",
        """                {false && bindingFailures && bindingFailures.failures.length > 0 && (""",
        kind="pw",
        note="接线断掉 = 死功能",
    ),
]


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write(path: str, text: str) -> None:
    with open(os.path.join(ROOT, path), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def run_suite(kind: str) -> int:
    if kind == "pw":
        return subprocess.run(PW_CMD, cwd=os.path.join(ROOT, "frontend"),
                              shell=(os.name == "nt"),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    return subprocess.run([sys.executable, "-m", "pytest", *PY_SUITE, "-q"], cwd=ROOT,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for m in MUTATIONS:
            print(f"{m.name}  [{m.kind}]  {m.note}")
        return 0

    print("=== 基线:未变异时两套都必须绿(否则下面的「转红」没有意义) ===")
    for kind in ("py", "pw"):
        rc = run_suite(kind)
        print(f"  baseline {kind}: rc={rc} {'OK' if rc == 0 else '🔴 基线就是红的,变异结果不可信'}")
        if rc != 0:
            return 2

    survived = []
    for m in MUTATIONS:
        original = read(m.path)
        if m.old not in original:
            print(f"🔴 {m.name}: 锚点没找到 —— 变异未真正注入(判据不可用)")
            survived.append(m.name + " [锚点丢失]")
            continue
        try:
            write(m.path, original.replace(m.old, m.new, 1))
            rc = run_suite(m.kind)
            killed = rc != 0
            print(f"  {'✅ 杀死' if killed else '🔴 存活'}  {m.name}  (rc={rc})")
            if not killed:
                survived.append(m.name)
        finally:
            write(m.path, original)  # 复位

    print("\n=== 复位后回归:必须重新全绿 ===")
    for kind in ("py", "pw"):
        rc = run_suite(kind)
        print(f"  restored {kind}: rc={rc} {'OK' if rc == 0 else '🔴 复位没复干净'}")
        if rc != 0:
            return 2

    if survived:
        print(f"\n🔴 存活变异 {len(survived)}:")
        for s in survived:
            print("   -", s)
        return 1
    print(f"\n✅ {len(MUTATIONS)}/{len(MUTATIONS)} 变异全部被杀死")
    return 0


if __name__ == "__main__":
    sys.exit(main())
