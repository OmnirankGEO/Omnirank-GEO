"""[裸 URL 回落 2026-08-16] 变异运行器 —— 每道锁亲手拆一次。

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
    "tests/platform_domain_rawurl_2026_08_16",
    "tests/platform_domain_stem_2026_08_16",
    "tests/platform_domain_match_2026_08_15",
    "tests/test_media_binding_candidates.py",
]


class Mut:
    def __init__(self, name, path, old, new, note=""):
        self.name, self.path, self.old, self.new, self.note = name, path, old, new, note


MUTATIONS = [
    Mut("N1 拆掉域回落(库存拿不到域时不再从媒体名提)",
        "services/media_binding_candidates.py",
        "                              or domain_from_name(_display_name(row)) or \"\"),",
        "                              or \"\"),",
        note="🔴 那 36 条退回 name_alias 放行 —— 本单主锁"),
    Mut("N2 URL 形态的名字重新算作名称证据",
        "services/media_binding_candidates.py",
        "    raw_name = _display_name(row)\n    if is_url_like_name(raw_name):\n        return False\n",
        "    raw_name = _display_name(row)\n",
        note="风险标被抵消 ⇒ 照样放行"),
    Mut("N3 URL 探测器放宽成「非空即 URL」",
        "services/media_binding_candidates.py",
        "    if not text or _CJK_RE.search(text):\n        return False",
        "    if not text:\n        return False",
        note="中文媒体名会被当成 URL,名称证据被抹掉"),
    Mut("N4 拆掉保守默认(两处都拿不到域时静默放行)",
        "services/media_binding_candidates.py",
        '        if not (entity_domain or match.get("domain")):\n'
        '            risk_flags.append("无法确定域名归属需人工核对")\n',
        "",
        note="工单 §2.2 明令不许默认 true"),
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
    print(f"=== 基线 rc={rc} {'OK' if rc == 0 else '🔴 基线就红'} ===")
    if rc != 0:
        return 2
    survived = []
    for m in MUTATIONS:
        orig = read(m.path)
        if m.old not in orig:
            print(f"🔴 {m.name}: 锚点没找到 —— 变异未真正注入")
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
        print(f"\n🔴 存活 {len(survived)}: {survived}")
        return 1
    print(f"\n✅ {len(MUTATIONS)}/{len(MUTATIONS)} 变异全部被杀死")
    return 0


if __name__ == "__main__":
    sys.exit(main())
