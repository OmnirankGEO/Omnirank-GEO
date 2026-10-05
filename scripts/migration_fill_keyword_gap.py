#!/usr/bin/env python3
"""migration_fill_keyword_gap — 一次性回填 confirmed_keywords 空白行(A.6 BUG)

背景(CTO-15.18 · 2026-04-28 · Deploy-CTO 实证 quote 274):
    `services/quote_keyword_sync.py::sync_quote_keywords_to_confirmed`
    在 session.pricing_data.keywords 空时 fallback 返 (0, 0)
    导致 paid quote 的 confirmed_keywords 表空白
    但 quotes.total_keywords / quotes.markdown 仍然有数据(数据严重不一致)

修复(已 commit):sync helper 加 markdown fallback · 提词 layer=NULL 落库

本 migration:扫所有 paid 或 active quote · 凡 confirmed_keywords 空白
但 markdown 有词的 · 用 _extract_keywords_from_markdown 回填

幂等:
    - 已有 confirmed_keywords 行的 quote 跳过
    - 没有 markdown 的 quote 跳过
    - 提不出词的 quote 跳过

使用:
    docker exec omnirank-ai python scripts/migration_fill_keyword_gap.py
    docker exec omnirank-ai python scripts/migration_fill_keyword_gap.py --dry-run  # 只看不改
    docker exec omnirank-ai python scripts/migration_fill_keyword_gap.py --quote-id 274  # 单条

输出:
    报表 · 扫描 N 条 · 回填 M 条 · 跳过 K 条 · 失败 X 条
    audit_log 写 'migration_fill_keyword_gap' · 谁回填了哪些词
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Tuple

# 让本脚本能直接 docker exec python 跑(无需 PYTHONPATH)
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migration-fill-keyword-gap")


def find_quotes_with_gap(quote_id_filter: int | None = None) -> List[dict]:
    """找到 confirmed_keywords 空白但 quote 状态 paid/active 且 markdown 非空的 quote"""
    from db.diagnosis_db import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        if quote_id_filter:
            cur.execute(
                """
                SELECT q.id, q.brand_id, q.brand_name, q.status, q.service_status,
                       q.markdown, q.monthly_price
                FROM quotes q
                WHERE q.id = %s
                  AND (q.deleted_at IS NULL)
                """,
                (quote_id_filter,),
            )
        else:
            cur.execute(
                """
                SELECT q.id, q.brand_id, q.brand_name, q.status, q.service_status,
                       q.markdown, q.monthly_price
                FROM quotes q
                WHERE (q.status IN ('paid', 'pending_payment', 'active', 'draft')
                       OR q.service_status = 'active')
                  AND (q.deleted_at IS NULL)
                  AND q.markdown IS NOT NULL
                  AND LENGTH(q.markdown) > 50
                  AND NOT EXISTS (
                      SELECT 1 FROM confirmed_keywords ck
                      WHERE ck.quote_id = q.id
                        AND (ck.deleted_at IS NULL)
                  )
                ORDER BY q.id
                """
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def fill_one(quote: dict, dry_run: bool = False) -> Tuple[int, str]:
    """回填一个 quote · 返 (回填词数, 状态字符串)"""
    from services.quote_keyword_sync import _extract_keywords_from_markdown, _build_brand_ctx, _attach_layer
    from db.diagnosis_db import save_confirmed_keywords

    md_text = quote.get("markdown") or ""
    md_kws = _extract_keywords_from_markdown(md_text)
    if not md_kws:
        return 0, "no_keywords_in_markdown"

    if dry_run:
        return len(md_kws), f"dry_run · 将回填 {len(md_kws)} 词:{', '.join(md_kws[:5])}{'...' if len(md_kws) > 5 else ''}"

    brand_ctx = _build_brand_ctx(quote["id"])
    tier = "standard"
    md_kw_list = [{
        "keyword": kw,
        "category": "通用词",
        "tier": tier,
        "base_price": 0,
        "city_premium": 1.0,
        "final_price": 0,
        "competitor_count": 0,
        "required_articles": 0,
        "intent": "informational",
        "funnel_stage": "awareness",
        "is_core": False,
    } for kw in md_kws]
    try:
        save_confirmed_keywords(quote["id"], _attach_layer(md_kw_list, brand_ctx))
    except Exception as e:
        return 0, f"save_failed: {e}"

    # audit_log
    try:
        from db.auth_db import create_audit_log
        create_audit_log(
            user_id=None,
            username="migration_fill_keyword_gap",
            action="migration_fill_keyword_gap",
            module="quotes",
            entity_type="quote",
            entity_id=quote["id"],
            summary=f"回填 quote {quote['id']} ({quote.get('brand_name')}) {len(md_kws)} 词 · CTO-15.18 A.6",
            after={"keywords": md_kws, "source": "markdown_fallback"},
        )
    except Exception as _ae:
        logger.warning(f"audit_log 失败 quote={quote['id']}: {_ae}")

    return len(md_kws), f"filled {len(md_kws)} keywords"


def main():
    ap = argparse.ArgumentParser(description="一次性回填 confirmed_keywords 空白行(A.6 BUG)")
    ap.add_argument("--dry-run", action="store_true", help="只扫描不写 · 看会改什么")
    ap.add_argument("--quote-id", type=int, default=None, help="只处理单个 quote_id")
    args = ap.parse_args()

    logger.info(f"开始 · dry_run={args.dry_run} quote_id={args.quote_id or 'ALL'}")
    quotes = find_quotes_with_gap(args.quote_id)
    logger.info(f"扫到 {len(quotes)} 个 quote 有 confirmed_keywords 空白 + markdown 非空")

    total_filled = 0
    skipped = 0
    failed = 0
    for q in quotes:
        try:
            n, msg = fill_one(q, dry_run=args.dry_run)
            if n > 0:
                total_filled += n
                logger.info(f"  ✅ quote={q['id']} ({q.get('brand_name')}): {msg}")
            else:
                skipped += 1
                logger.info(f"  ⏭️  quote={q['id']} ({q.get('brand_name')}): {msg}")
        except Exception as e:
            failed += 1
            logger.error(f"  ❌ quote={q['id']} ({q.get('brand_name')}): {e}")

    logger.info(
        f"完工 · {len(quotes)} 扫描 · {total_filled} 词回填 · {skipped} 跳过 · {failed} 失败"
        + (f" (dry-run · 实际未写)" if args.dry_run else "")
    )


if __name__ == "__main__":
    main()
