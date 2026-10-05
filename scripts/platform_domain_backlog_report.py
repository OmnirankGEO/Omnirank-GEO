"""[P0-3 2026-08-15] 分平台存量清单生成器 —— 供「分平台分批复核」后续单立项使用。

Owner 拍板 ②:**存量不回溯**。本脚本**只读、只出清单,一行数据都不改**
(全文没有任何 INSERT / UPDATE / DELETE;唯一的 SQL 是一条 SELECT)。

名单不在这里写死:直接 import `services.media_binding_candidates` 的
`SHARED_PLATFORM_DOMAINS` / `B_BUCKET_MAINSTREAM_PORTALS`,
归一化也复用生产同一对函数 —— 清单与线上判据不可能对不上。

用法:
    python scripts/platform_domain_backlog_report.py            # 打到 stdout
    python scripts/platform_domain_backlog_report.py --tsv out.tsv
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

#: 唯一的一条 SQL,只读。
QUERY = """
    SELECT c.status,
           COALESCE(c.media_name, '') AS media_name,
           c.entity_key,
           COALESCE(e.domain, '')     AS entity_domain
      FROM geo_media_binding_candidates c
      LEFT JOIN geo_media_entities e ON e.entity_key = c.entity_key
     WHERE COALESCE(c.active, TRUE)
"""


def collect() -> dict[str, dict]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(QUERY)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    agg: dict[str, dict] = defaultdict(lambda: {
        "names": set(), "entities": set(), "approved": 0, "candidate": 0,
        "rejected": 0, "deleted": 0, "total": 0,
    })
    for r in rows:
        reg = registrable_domain(host_domain(r["entity_domain"]))
        if not reg:
            continue
        b = agg[reg]
        b["names"].add(r["media_name"])
        b["entities"].add(r["entity_key"])
        b["total"] += 1
        if r["status"] in b:
            b[r["status"]] += 1
    return agg


def bucket_of(domain: str) -> str:
    if domain in SHARED_PLATFORM_DOMAINS:
        return "A-平台(已生效)"
    if domain in B_BUCKET_MAINSTREAM_PORTALS:
        return "B-主流媒体主站(待Owner)"
    return "其他"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tsv", help="额外写一份 TSV")
    ap.add_argument("--include-b", action="store_true", help="连 B 桶一起列(默认只列 A 桶)")
    args = ap.parse_args()

    agg = collect()
    wanted = set(SHARED_PLATFORM_DOMAINS)
    if args.include_b:
        wanted |= B_BUCKET_MAINSTREAM_PORTALS

    rows = []
    for d in sorted(wanted, key=lambda x: -(agg.get(x, {"approved": 0})["approved"])):
        v = agg.get(d)
        if not v:
            rows.append((d, 0, 0, 0, 0, 0, 0, 0, bucket_of(d)))
            continue
        rows.append((d, len(v["names"]), len(v["entities"]), v["approved"], v["candidate"],
                     v["rejected"], v["deleted"], v["total"], bucket_of(d)))

    print(f"{'注册域':26s} {'媒体名':>6s} {'实体':>5s} {'approved':>9s} {'candidate':>10s} "
          f"{'rejected':>9s} {'deleted':>8s} {'合计':>6s}  桶")
    for r in rows:
        print(f"{r[0]:26s} {r[1]:>6d} {r[2]:>5d} {r[3]:>9d} {r[4]:>10d} {r[5]:>9d} {r[6]:>8d} {r[7]:>6d}  {r[8]}")
    print(f"{'合计':26s} {'':>6s} {'':>5s} {sum(r[3] for r in rows):>9d} {sum(r[4] for r in rows):>10d}")
    print(f"\n分母:全库 active 候选 {sum(v['total'] for v in agg.values())} 条 · 注册域 {len(agg)} 个")
    print("🔴 本脚本只读:存量一行不动(Owner 2026-08-15 拍板 ②),供后续「分平台分批复核」单立项。")

    if args.tsv:
        # 🔴 Windows 行尾:写这一处也要带 newline=""
        with open(args.tsv, "w", encoding="utf-8", newline="") as fh:
            fh.write("registrable_domain\tdistinct_media_names\tentities\tapproved\t"
                     "candidate\trejected\tdeleted\ttotal\tbucket\n")
            for r in rows:
                fh.write("\t".join(str(x) for x in r) + "\n")
        print(f"→ 已写 {args.tsv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
