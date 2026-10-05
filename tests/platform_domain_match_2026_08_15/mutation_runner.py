"""[平台域口径 2026-08-15] 变异运行器:每道锁亲手拆一次,看它转不转红。

「过了」和「没判别力」在报告上长得一模一样,只有拆掉修复看对应用例翻红才能分开。

🔴 Windows 行尾:读写两处都带 `newline=""`(同仓早修过一次,新 runner 照样重犯过)。

用法:
    python tests/platform_domain_match_2026_08_15/mutation_runner.py
    python tests/platform_domain_match_2026_08_15/mutation_runner.py --list
退出码:0 = 全部被杀死;1 = 有存活;2 = 基线就不干净
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SUITE = [
    "tests/platform_domain_match_2026_08_15",
    "tests/test_media_binding_candidates.py",
    "tests/flywheel_integration/test_t6_binding_approve_all.py",
    "tests/flywheel_integration/test_t6b_auto_approve.py",
]


class Mut:
    def __init__(self, name, path, old, new, note=""):
        self.name, self.path, self.old, self.new, self.note = name, path, old, new, note


MUTATIONS = [
    Mut(
        "M1 拆掉注册域归一化(退回 host 级 = 原 bug 本体)",
        "services/media_binding_candidates.py",
        "    return registrable_domain(host_domain(domain)) in SHARED_PLATFORM_DOMAINS",
        "    return host_domain(domain) in SHARED_PLATFORM_DOMAINS",
        note="子域用例必须转红",
    ),
    Mut(
        "M2 只留注册域、拆掉 host 级前置(jina 包装失效)",
        "services/media_binding_candidates.py",
        "    return registrable_domain(host_domain(domain)) in SHARED_PLATFORM_DOMAINS",
        "    return registrable_domain(domain) in SHARED_PLATFORM_DOMAINS",
        note="两级归一化缺一不可 —— r.jina.ai 包装用例必须转红",
    ),
    Mut(
        "M3 🔴 把 B 桶塞进名单(工单点名的形态)",
        "services/media_binding_candidates.py",
        "def is_shared_platform_domain(domain: str) -> bool:",
        "SHARED_PLATFORM_DOMAINS = SHARED_PLATFORM_DOMAINS | B_BUCKET_MAINSTREAM_PORTALS\n\n\n"
        "def is_shared_platform_domain(domain: str) -> bool:",
        note="B 桶反向用例 + 存量清单分桶必须转红",
    ),
    Mut(
        "M4 精确相等改成裸后缀匹配(同尾不同域被误吃)",
        "services/media_binding_candidates.py",
        "    return registrable_domain(host_domain(domain)) in SHARED_PLATFORM_DOMAINS",
        "    _d = host_domain(domain)\n"
        "    return any(_d == h or _d.endswith(h) for h in SHARED_PLATFORM_DOMAINS)",
        note="notcsdn.net / fake163.com 必须转红",
    ),
    Mut(
        "M5 名单退回原 11 项(A 桶扩充没生效)",
        "services/media_binding_candidates.py",
        '    "smzdm.com",          # 什么值得买',
        '    "__A_BUCKET_DISABLED__",',
        note="A 桶用例必须转红",
    ),
    Mut(
        "M6 名单里保留 host 级写法(死规则复活)",
        "services/media_binding_candidates.py",
        '    "baidu.com",          # 原 baijiahao.baidu.com',
        '    "baijiahao.baidu.com",',
        note="「名单元素必须是注册域」那条锁必须转红",
    ),
    Mut(
        "M7 待审列表不重算(新口径不经三出口生效)",
        "db/media_entity_flywheel_db.py",
        '    revalidated = {int(r["id"]): r for r in revalidate_binding_candidate_rows(pending)}\n'
        '    return [revalidated.get(int(r["id"]), r) for r in rows]',
        "    return rows",
        note="接线锁必须转红",
    ),
    Mut(
        "M8 存量也跟着重算(违反 Owner 拍板 ②)",
        "db/media_entity_flywheel_db.py",
        '    pending = [r for r in rows if str(r.get("status") or "candidate") == "candidate"]',
        "    pending = list(rows)",
        note="存量不回溯用例必须转红",
    ),
    Mut(
        "M9 前端名单漂移(改一项)",
        "frontend/src/pages/Admin/GeoPlacementFlywheel.tsx",
        "  'csdn.net',\n",
        "",
        note="前后端漂移锁必须转红",
    ),
]


def read(p):
    with open(os.path.join(ROOT, p), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write(p, t):
    with open(os.path.join(ROOT, p), "w", encoding="utf-8", newline="") as fh:
        fh.write(t)


def run():
    return subprocess.run([sys.executable, "-m", "pytest", *SUITE, "-q"], cwd=ROOT,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for m in MUTATIONS:
            print(f"{m.name}  —— {m.note}")
        return 0

    rc = run()
    print(f"=== 基线 rc={rc} {'OK' if rc == 0 else '🔴 基线就红,变异结果不可信'} ===")
    if rc != 0:
        return 2

    survived = []
    for m in MUTATIONS:
        orig = read(m.path)
        if m.old not in orig:
            print(f"🔴 {m.name}: 锚点没找到 —— 变异未真正注入(判据不可用)")
            survived.append(m.name + " [锚点丢失]")
            continue
        try:
            write(m.path, orig.replace(m.old, m.new, 1))
            rc = run()
            killed = rc != 0
            print(f"  {'✅ 杀死' if killed else '🔴 存活'}  {m.name}  (rc={rc})")
            if not killed:
                survived.append(m.name)
        finally:
            write(m.path, orig)

    rc = run()
    print(f"=== 复位后回归 rc={rc} {'OK' if rc == 0 else '🔴 复位没复干净'} ===")
    if rc != 0:
        return 2
    if survived:
        print(f"\n🔴 存活 {len(survived)}:")
        for s in survived:
            print("   -", s)
        return 1
    print(f"\n✅ {len(MUTATIONS)}/{len(MUTATIONS)} 变异全部被杀死")
    return 0


if __name__ == "__main__":
    sys.exit(main())
