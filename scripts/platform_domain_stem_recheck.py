"""[补充单 P0-2 2026-08-16] 用词干判据复检现有 A/B 桶名单 —— 只读、只出表,不改名单。

A 桶是用旧判据(数媒体名)选出来的,必须用新尺子(数发布主体)复检一遍。
名单从 `services.media_binding_candidates` import,归一化复用生产同一对函数 —— 表与线上判据不可能漂移。

三分类,**不是两分类**:
    平台 / 非平台 / 样本不足(去重媒体名 < 5)
🔴 「样本不足」**不等于**「非平台」。它只说明没有信息,一律**保持现状**并单独列出交 Owner,
   绝不拿一个无信息的判定去改名单。

用法:
    python scripts/platform_domain_stem_recheck.py                 # 打表
    python scripts/platform_domain_stem_recheck.py --tsv out.tsv   # 另存 TSV
    python scripts/platform_domain_stem_recheck.py --samples 8     # 每域展示几个媒体名
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 GBK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db.connection import get_connection  # noqa: E402
from services.citation_domain_weights import normalize_domain as registrable_domain  # noqa: E402
from services.media_binding_candidates import (  # noqa: E402
    B_BUCKET_MAINSTREAM_PORTALS,
    SHARED_PLATFORM_DOMAINS,
)
from services.media_entity_flywheel import normalize_domain as host_domain  # noqa: E402
from services.media_name_stem import MIN_NAMES_FOR_JUDGEMENT, classify_domain  # noqa: E402

#: 唯一一条 SQL,只读。分母写在查询里:active 候选 + 有 entity 的行。
QUERY = """
    SELECT COALESCE(c.media_name, '') AS media_name,
           COALESCE(e.domain, '')     AS entity_domain,
           c.status
      FROM geo_media_binding_candidates c
      JOIN geo_media_entities e ON e.entity_key = c.entity_key
     WHERE COALESCE(c.active, TRUE)
"""


def load_names() -> tuple[dict[str, set[str]], dict[str, dict[str, int]]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(QUERY)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    names: dict[str, set[str]] = defaultdict(set)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        d = registrable_domain(host_domain(r["entity_domain"]))
        if not d:
            continue
        if r["media_name"]:
            names[d].add(r["media_name"])
        counts[d][r["status"]] += 1
    return names, counts


def verdict_label(v) -> str:
    if v.name_count < MIN_NAMES_FOR_JUDGEMENT:
        return "样本不足"
    return "平台" if v.is_platform else "非平台"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tsv")
    ap.add_argument("--samples", type=int, default=6)
    args = ap.parse_args()

    names, counts = load_names()
    rows = []
    for bucket, domains in (("A", sorted(SHARED_PLATFORM_DOMAINS)),
                            ("B", sorted(B_BUCKET_MAINSTREAM_PORTALS))):
        for d in domains:
            v = classify_domain(d, names.get(d, []))
            label = verdict_label(v)
            expected = "平台" if bucket == "A" else "非平台"
            if label == "样本不足":
                action = "保持现状(无信息)"
            elif label == expected:
                action = "一致"
            elif bucket == "A":
                action = "🔴 A→B(判为非平台)"
            else:
                action = "🔴 待 Owner 点名(判为平台,不自动上移)"
            rows.append({
                "bucket": bucket, "domain": d, "verdict": label, "action": action,
                "names": v.name_count,
                "dominant": v.dominant_stem, "dominant_n": v.dominant_count,
                "outsider_stems": v.distinct_outsider_stems,
                "approved": counts.get(d, {}).get("approved", 0),
                "candidate": counts.get(d, {}).get("candidate", 0),
                "samples": " | ".join(v.name_samples[: args.samples]),
                "outsiders": " | ".join(v.outsider_samples[: args.samples]),
            })

    hdr = f"{'桶':<3s}{'注册域':<24s}{'判定':<8s}{'名字':>5s}{'主导词干':>9s}{'共享':>5s}{'异名主体':>7s}{'appr':>6s}{'待审':>6s}  处置"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['bucket']:<3s}{r['domain']:<24s}{r['verdict']:<8s}{r['names']:>5d}"
              f"{r['dominant'] or '-':>9s}{r['dominant_n']:>5d}{r['outsider_stems']:>7d}"
              f"{r['approved']:>6d}{r['candidate']:>6d}  {r['action']}")

    print()
    for bucket in ("A", "B"):
        sub = [r for r in rows if r["bucket"] == bucket]
        from collections import Counter
        print(f"  {bucket} 桶 {len(sub)} 项:{dict(Counter(r['verdict'] for r in sub))}")
    moves = [r for r in rows if r["action"].startswith("🔴")]
    print(f"\n  需要处置的 {len(moves)} 项(逐条附媒体名样例,供肉眼复核):")
    for r in moves:
        print(f"    [{r['bucket']}] {r['domain']}  {r['verdict']}  —— {r['action']}")
        print(f"        名字样例: {r['samples']}")
        if r["outsiders"]:
            print(f"        异名样例: {r['outsiders']}")

    short = [r for r in rows if r["verdict"] == "样本不足"]
    print(f"\n  样本不足 {len(short)} 项(保持现状,不据此改名单):"
          f" {', '.join(r['domain'] for r in short)}")

    if args.tsv:
        # 🔴 Windows 行尾:写这一处也带 newline=""
        cols = ["bucket", "domain", "verdict", "action", "names", "dominant", "dominant_n",
                "outsider_stems", "approved", "candidate", "samples", "outsiders"]
        with open(args.tsv, "w", encoding="utf-8", newline="") as fh:
            fh.write("\t".join(cols) + "\n")
            for r in rows:
                fh.write("\t".join(str(r[c]).replace("\t", " ") for c in cols) + "\n")
        print(f"\n→ 已写 {args.tsv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
