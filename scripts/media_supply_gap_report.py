#!/usr/bin/env python
"""重算媒体供给缺口表（工单 T4 交付物的可重复版本）。

只读：只跑 SELECT，不写任何表。输出与
``docs/AI-CONTEXT/MEDIA_SUPPLY_GAP_2026-07-29.md`` 同口径。

    python scripts/media_supply_gap_report.py [--limit 50] [--json]

「可售」用推荐器 `_match_media` 真正使用的谓词，而不是 `can_geo=1`
（后者在生产命中 0 行 —— mhz_media 全表 can_geo 恒为 0）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.citation_domain_weights import family_for_domain, normalize_domain  # noqa: E402
from services.question_family_mix import inventory_keyword_for  # noqa: E402

#: 平台性质，不是可下单的软文媒体位 —— 单列出来免得被当成缺口。
NOT_PURCHASABLE = {
    "jd.com", "iesdouyin.com", "douyin.com", "baike.baidu.com", "taobao.com",
    "qq.com", "china.com", "koolearn.com",
}

SELLABLE_PREDICATE = """
    is_active
    AND price >= 5.0
    AND media_name NOT ILIKE '%%套餐%%'
    AND COALESCE(resource_type_name, '') <> '套餐系列'
"""


def _cited_domains(cur, limit: int) -> list[dict]:
    cur.execute(
        """
        SELECT a.domain AS domain, COUNT(*) AS cites
          FROM geo_research_article_citations c
          JOIN geo_research_articles a ON a.id = c.article_id
         WHERE COALESCE(a.domain, '') <> ''
         GROUP BY a.domain
         ORDER BY cites DESC
         LIMIT %s
        """,
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def _sellable_count(cur, keyword: str) -> int:
    if not keyword:
        return 0
    cur.execute(
        f"SELECT COUNT(*) AS n FROM mhz_media WHERE {SELLABLE_PREDICATE} AND media_name ILIKE %s",
        (f"%{keyword}%",),
    )
    row = cur.fetchone() or {}
    return int(row.get("n") or 0)


def build_gap_rows(limit: int = 50) -> list[dict]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        rows = _cited_domains(cur, max(1, min(500, limit)))

        # 折叠子域：news.sohu.com / business.sohu.com -> sohu.com
        folded: dict[str, dict] = {}
        for row in rows:
            raw = str(row.get("domain") or "")
            key = normalize_domain(raw) or raw
            bucket = folded.setdefault(key, {"domain": key, "cites": 0, "raw_hosts": []})
            bucket["cites"] += int(row.get("cites") or 0)
            bucket["raw_hosts"].append(raw)

        out: list[dict] = []
        keyword_cache: dict[str, int] = {}
        for key, bucket in folded.items():
            raw_hint = bucket["raw_hosts"][0]
            keyword = inventory_keyword_for(raw_hint)
            if keyword not in keyword_cache:
                keyword_cache[keyword] = _sellable_count(cur, keyword)
            sellable = keyword_cache[keyword]
            family = family_for_domain(raw_hint)
            out.append({
                "domain": key,
                "cites": bucket["cites"],
                "inventory_keyword": keyword,
                "sellable": sellable,
                "gap_score": round(bucket["cites"] / (sellable + 1), 1),
                "role": family.role if family else "",
                "role_label": family.role_label if family else "",
                "self_serve": bool(family and family.self_serve),
                "purchasable": key not in NOT_PURCHASABLE,
            })
        out.sort(key=lambda r: -r["gap_score"])
        return out
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    rows = build_gap_rows(args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(f"{'域':28} {'被引':>6} {'可售':>5} {'缺口分':>8}  角色")
    print("-" * 72)
    for row in rows:
        if not row["purchasable"]:
            continue
        print(
            f"{row['domain']:28} {row['cites']:>6} {row['sellable']:>5} "
            f"{row['gap_score']:>8}  {row['role_label']}"
            + ("  [可自助]" if row["self_serve"] else "")
        )
    skipped = [r["domain"] for r in rows if not r["purchasable"]]
    if skipped:
        print("\n不属于采购标的（平台性质）:", "、".join(skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
