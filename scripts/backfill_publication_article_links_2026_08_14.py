# -*- coding: utf-8 -*-
"""P0-3 · 发布→文章 content_hash 替代桥回填(J4b,2026-08-14)。

研究定稿 J4 红线桥:articles→发布物直连仅 74/1,468 = **5.0%(已配对子样本
口径)** —— mhz 已发布条目大量挂在 ``mhz_publish_orders.article_id IS NULL``
的订单上,归因引擎(strict_article_outcomes)的发布侧 UNION 拿不到 article_id,
被引事件就挂不回文章。

替代桥口径(**只补 NULL,幂等,歧义不猜**):
  - 候选 = article_id IS NULL 的 mhz_publish_orders,且其条目带
    ``submitted_content_snapshot_hash``(渠道提交时冻结的正文哈希 ——
    与 articles 侧 ``publication_snapshot->>'content_hash'`` /
    ``current_content_hash`` 同一族指纹);
  - 匹配 = 条目哈希在 articles 侧命中**恰好一篇**文章 → 补该订单 article_id;
    命中 0 篇 → 留空(照旧);命中 >1 篇 → **跳过并计数**(同稿多发/复制稿,
    硬选一篇就是编 lineage —— publication_url_article_ambiguous 同款纪律);
  - UPDATE 带 ``WHERE article_id IS NULL`` 幂等护栏,重跑收敛到 0。

用法:
    python scripts/backfill_publication_article_links_2026_08_14.py           # dry-run(默认)
    python scripts/backfill_publication_article_links_2026_08_14.py --apply   # 真写

分类:O1 观测面回填 —— 只影响归因覆盖率,发布/计费主链零依赖。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def resolve_article_for_hashes(hash_to_articles: dict[str, set[int]],
                               item_hashes: list[str]) -> tuple[int | None, str]:
    """订单条目哈希集合 → (article_id | None, reason)。纯函数,判别测试打这里。

    reason ∈ {"matched", "no_match", "ambiguous"}。
    订单内多条目命中不同文章同样判 ambiguous(一单只有一个 article_id 槽位,
    拆单归属不是回填能代拍的)。
    """
    matched: set[int] = set()
    for h in item_hashes:
        matched |= hash_to_articles.get(h, set())
    if not matched:
        return None, "no_match"
    if len(matched) > 1:
        return None, "ambiguous"
    return next(iter(matched)), "matched"


def run_backfill(*, apply: bool) -> dict[str, Any]:
    from db.connection import get_db

    totals: dict[str, Any] = {"orders_scanned": 0, "matched": 0, "written": 0,
                              "no_match": 0, "ambiguous": 0, "apply": bool(apply)}
    with get_db() as conn:
        cur = conn.cursor()
        # articles 侧指纹索引:publication_snapshot.content_hash 与 current_content_hash 两族。
        cur.execute(
            """
            SELECT id,
                   publication_snapshot->>'content_hash' AS snap_hash,
                   current_content_hash
              FROM articles
             WHERE publication_snapshot->>'content_hash' IS NOT NULL
                OR current_content_hash IS NOT NULL
            """
        )
        hash_to_articles: dict[str, set[int]] = {}
        for row in cur.fetchall() or []:
            for key in ("snap_hash", "current_content_hash"):
                h = str(row.get(key) or "").strip()
                if h:
                    hash_to_articles.setdefault(h, set()).add(int(row["id"]))

        cur.execute(
            """
            SELECT o.id AS order_id,
                   array_agg(DISTINCT i.submitted_content_snapshot_hash)
                       FILTER (WHERE i.submitted_content_snapshot_hash IS NOT NULL) AS item_hashes
              FROM mhz_publish_orders o
              JOIN mhz_publish_order_items i ON i.order_id = o.id
             WHERE o.article_id IS NULL
             GROUP BY o.id
            """
        )
        orders = [dict(r) for r in cur.fetchall() or []]
        for order in orders:
            totals["orders_scanned"] += 1
            hashes = [str(h) for h in (order.get("item_hashes") or []) if h]
            article_id, reason = resolve_article_for_hashes(hash_to_articles, hashes)
            if reason == "matched" and article_id is not None:
                totals["matched"] += 1
                if apply:
                    # 🔴 幂等护栏进 SQL:只补 NULL,绝不覆盖已有归属。
                    cur.execute(
                        "UPDATE mhz_publish_orders SET article_id = %s "
                        " WHERE id = %s AND article_id IS NULL",
                        (int(article_id), int(order["order_id"])),
                    )
                    totals["written"] += int(getattr(cur, "rowcount", 0) or 0)
            else:
                totals[reason] += 1
        if apply:
            conn.commit()
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真写(缺省 dry-run)")
    args = parser.parse_args()
    totals = run_backfill(apply=args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {totals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
