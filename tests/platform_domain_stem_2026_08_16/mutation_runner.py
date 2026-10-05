"""[补充单 2026-08-16] 变异运行器:每道锁亲手拆一次,看它转不转红。

补充单 §6 第 2 条点名要拆的那两条(拆去噪 / 拆词干判定),其中「拆去噪」已作废 ——
去噪本身被它自己的反向对照证伪并删除了(见 services/media_name_stem.py 失败尝试 ②)。
替代它的是 M1「拆掉主体归并」:退回旧判据「数不同媒体名」,china/dzwww/news.cn 必须转红。

🔴 Windows 行尾:读写两处都带 newline=""。
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
    "tests/platform_domain_stem_2026_08_16",
    "tests/platform_domain_match_2026_08_15",
    "tests/test_media_binding_candidates.py",
]


class Mut:
    def __init__(self, name, path, old, new, note=""):
        self.name, self.path, self.old, self.new, self.note = name, path, old, new, note


MUTATIONS = [
    Mut(
        "M1 拆掉主体归并(退回旧判据:数不同媒体名)",
        "services/media_name_stem.py",
        "    is_platform = len(outsider_stems) >= outsider_stem_min_distinct",
        "    is_platform = n >= min_names",
        note="🔴 补充单点名的那条:china/dzwww/news.cn 必须转回「平台」",
    ),
    Mut(
        "M2 判定量换回「异名占比」(第一版失败尝试)",
        "services/media_name_stem.py",
        "    is_platform = len(outsider_stems) >= outsider_stem_min_distinct",
        "    is_platform = ratio > 0.30",
        note="dzwww(32%) 会被误判平台、ifeng(11%) 会被误判非平台",
    ),
    Mut(
        "M3 阈值退回 2(P0-4 抽样据以调高的那一档)",
        "services/media_name_stem.py",
        "OUTSIDER_STEM_MIN_DISTINCT = 3",
        "OUTSIDER_STEM_MIN_DISTINCT = 2",
        note="itouchtv.cn(广东台一家)会重新被判平台",
    ),
    Mut(
        "M4 词干长改 4",
        "services/media_name_stem.py",
        "STEM_LEN = 3",
        "STEM_LEN = 4",
        note="「新华网上市公司」「新华网大首页」会被当成两个主体",
    ),
    Mut(
        "M5 样本不足也判平台",
        "services/media_name_stem.py",
        '        return StemVerdict(domain, False, f"样本不足({n} < {min_names})⇒ 不判平台,交人工",',
        '        return StemVerdict(domain, True, f"样本不足({n} < {min_names})⇒ 不判平台,交人工",',
        note="「无信息」被当成「是平台」",
    ),
    Mut(
        "M6 把降级的域塞回 A 桶",
        "services/media_binding_candidates.py",
        "def is_shared_platform_domain(domain: str) -> bool:",
        "SHARED_PLATFORM_DOMAINS = SHARED_PLATFORM_DOMAINS | STEM_DEMOTED_NON_PLATFORM\n\n\n"
        "def is_shared_platform_domain(domain: str) -> bool:",
        note="复检降级白做了;三集合互不相交也会破",
    ),
    Mut(
        "M7 🔴 把 B 桶塞进名单(上一包点名的形态,回归锁)",
        "services/media_binding_candidates.py",
        "def is_shared_platform_domain(domain: str) -> bool:",
        "SHARED_PLATFORM_DOMAINS = SHARED_PLATFORM_DOMAINS | B_BUCKET_MAINSTREAM_PORTALS\n\n\n"
        "def is_shared_platform_domain(domain: str) -> bool:",
        note="Owner 拍板 china.com/dzwww.com 留 B 桶,这条是回归锁",
    ),
    Mut(
        "M8 影响面脚本把「被拦」退化成「落在平台域上」",
        "scripts/platform_domain_impact_report.py",
        "    return not _has_name_evidence(_entity_obj(row), "
        '{"media_name": row.get("media_name") or ""})',
        "    return True",
        note="Review 点名的定义歧义:788 vs 54 就差在这一步",
    ),
    Mut(
        "M9 快照指纹掺回时间戳",
        "scripts/platform_domain_impact_report.py",
        '    count_keys = ["candidates_all", "candidates_active", "approved_active",\n'
        '                  "pending_active", "deleted_active", "domain_exact_active"]',
        '    count_keys = ["candidates_all", "max_updated_at"]',
        note="生产与本地快照会被判成不同快照,制造假差异",
    ),
    Mut(
        "M10 复检脚本把「样本不足」当「非平台」",
        "scripts/platform_domain_stem_recheck.py",
        '    if v.name_count < MIN_NAMES_FOR_JUDGEMENT:\n        return "样本不足"',
        '    if v.name_count < MIN_NAMES_FOR_JUDGEMENT:\n        return "非平台"',
        note="拿无信息的判定去改名单",
    ),
    Mut(
        "M11 抽样脚本去掉种子(不可复跑)",
        "scripts/platform_domain_stem_sample.py",
        "    rng = random.Random(seed)",
        "    rng = random.Random()",
        note="补充单 §4 明令抽样必须可复跑",
    ),
    Mut(
        "M13 🔴 撤掉 jia.com 手工降级(本单硬出口)",
        "services/media_binding_candidates.py",
        '    "jia.com",\n',
        "",
        note="jia.com 会重新被判平台 —— 硬出口锁必须转红",
    ),
    Mut(
        "M12 前端名单漂移(删一项)",
        "frontend/src/pages/Admin/GeoPlacementFlywheel.tsx",
        "  'csdn.net',\n",
        "",
        note="前后端漂移锁",
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
    if ap.parse_args().list:
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
            print(f"  {'✅ 杀死' if rc else '🔴 存活'}  {m.name}  (rc={rc})")
            if rc == 0:
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
