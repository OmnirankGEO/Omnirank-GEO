#!/usr/bin/env python
"""回填媒介盒子侧的 ``source_domain``(工单 §6)。

生产实测(2026-08-02):

======================  ==================  ==================
表                      mhz 侧              kyb 侧
======================  ==================  ==================
``mhz_media``           17,769 条 → 有域名 0  34,338 → 34,338
``mhz_wemedia``         43,168 条 → 有域名 0  82,039 → 82,037
======================  ==================  ==================

``source_domain`` 是媒体覆盖度分析的基础维度(「AI 被引 TOP218 域覆盖 85%」这类统计
全靠它),缺一半会让所有域名维度的结论偏。

--------------------------------------------------------------------------
🔴 为什么**不用**工单 §6 给的那条 SQL
--------------------------------------------------------------------------
工单 §6 写的是::

    UPDATE mhz_media SET source_domain =
      lower(regexp_replace(substring(case_link from '^https?://([^/:?#]+)'), '^www\\.', ''))

它取的是**完整 host**。但同一列里 kyb 侧的值是
``services.kuaiyibo.media_sync.normalize_source_domain`` 写的 **注册域(eTLD+1)**
—— 2026-08-02 生产实测:``baijiahao.baidu.com`` 存成 ``baidu.com``、
``mp.weixin.qq.com`` 存成 ``qq.com``,自媒体表 82,031 条里 **39,910 条**与完整 host 不同。

按工单那条 SQL 回填,同一列就会**按 provider 劈成两种语义**,而 §6 声称的用途
(对撞 AI 被引域)恰恰用的是注册域归一 —— 结果是回填之后覆盖度统计**更错**,
不是更对。所以这里复用与 kyb 完全相同的 ``normalize_source_domain``。

--------------------------------------------------------------------------
🔴 回填不会回头改变择优匹配
--------------------------------------------------------------------------
``services.media_provider_equivalence`` 的 mhz 侧域名是从 ``case_link`` **现算完整 host**,
**从不读 mhz.source_domain**。所以本脚本写什么都不可能改到映射结果。
该性质由 ``tests/test_media_provider_equivalence.py::test_backfill_cannot_change_matching`` 锁住。

用法::

    python scripts/backfill_mhz_source_domain_2026_08_02.py --dry-run   # 只看会写多少行
    python scripts/backfill_mhz_source_domain_2026_08_02.py --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill-source-domain")

BATCH = 2000
TABLES = ("mhz_media", "mhz_wemedia")


def backfill(table: str, *, apply: bool) -> dict[str, int]:
    from db.connection import get_connection
    from services.kuaiyibo.media_sync import normalize_source_domain

    conn = get_connection()
    stats = {"scanned": 0, "written": 0, "empty_result": 0}
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT id, case_link FROM {table}
             WHERE provider = 'mhz'
               AND COALESCE(source_domain, '') = ''
               AND case_link ~ '^https?://'
             ORDER BY id
            """
        )
        rows = [(int(r["id"]), r["case_link"]) for r in cur.fetchall()]
        stats["scanned"] = len(rows)

        pending: list[tuple[str, int]] = []
        for media_id, link in rows:
            domain = normalize_source_domain(link)
            if not domain:
                # 归一化归不出东西(畸形 URL)→ 跳过,**不写空串**。
                # 写空串会让「已回填」与「回填不出来」变得不可区分。
                stats["empty_result"] += 1
                continue
            pending.append((domain, media_id))

        if not apply:
            stats["written"] = len(pending)
            return stats

        from psycopg2.extras import execute_batch

        for i in range(0, len(pending), BATCH):
            chunk = pending[i:i + BATCH]
            execute_batch(
                cur,
                f"UPDATE {table} SET source_domain = %s "
                f" WHERE id = %s AND provider = 'mhz' AND COALESCE(source_domain,'') = ''",
                chunk,
                page_size=BATCH,
            )
            conn.commit()
            stats["written"] += len(chunk)
            logger.info("[%s] 已写 %d/%d", table, stats["written"], len(pending))
        return stats
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    total = {"scanned": 0, "written": 0, "empty_result": 0}
    for table in TABLES:
        s = backfill(table, apply=bool(args.apply))
        logger.info("[%s] 待回填=%d 可写=%d 归一化为空=%d",
                    table, s["scanned"], s["written"], s["empty_result"])
        for k in total:
            total[k] += s[k]
    logger.info("合计 待回填=%d 写入=%d 归一化为空=%d %s",
                total["scanned"], total["written"], total["empty_result"],
                "(dry-run,未写库)" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
