"""[CTO-15.23 2026-05-18 P0 BUG 历史数据修] · 假阴回补脚本

老板 2026-05-18 报"罗平县皓琪监测 44% 下降 100%" · SSH 调研发现 6 brand 1856 个凌晨假阴 task ·
ec4492cd batch_monitor fallback 已修未来 · 此脚本回补历史 5-08 之后被错标 is_detected=0 的记录。

工作原理(跟 batch_monitor.py:155 fallback 逻辑一致):
  - 扫 monitoring_results WHERE is_detected=0 AND response_snippet 非空 AND tested_at >= cutoff
  - 通过 task_id → keyword_id → confirmed_keywords.target_brand 拿目标品牌名
  - 若 target_brand 在 response_snippet 中(大小写不敏感)→ UPDATE is_detected=1 + mention_type='fallback_text_match_backfill'
  - dry-run 模式:仅打印影响行数 · 不写 DB
  - --apply:真改 DB · 必须 Deploy-CTO admin 操作 + pg_dump 备份后才跑

用法(Deploy-CTO SSH 上 prod 跑):
  # 1. 备份
  docker exec omnirank-db pg_dump -U geo_admin -t monitoring_results geo_agentscope \\
    | gzip > /backup/monitoring_results_pre_p0_backfill_$(date +%Y%m%d_%H%M).sql.gz
  # 2. dry-run(看影响范围)
  docker cp scripts/p0_monitoring_false_negative_backfill.py omnirank-blue:/tmp/
  docker exec omnirank-blue python /tmp/p0_monitoring_false_negative_backfill.py
  # 3. 老板批后 apply
  docker exec omnirank-blue python /tmp/p0_monitoring_false_negative_backfill.py --apply

安全:
  - dry-run 默认 · 必须显式 --apply 才写 DB
  - WHERE 条件严格(is_detected=0 + 非空 snippet + cutoff)· 不会误改成功记录
  - mention_type 标 backfill_* 便于事后审计 + revert
"""

import sys
import os
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(description="P0 监测假阴回补脚本")
    parser.add_argument("--apply", action="store_true", help="真改 DB(默认 dry-run)")
    parser.add_argument("--since-days", type=int, default=14, help="回补几天内的数据(默认 14 天)")
    parser.add_argument("--limit", type=int, default=0, help="限制处理条数(0=不限 · debug 用)")
    args = parser.parse_args()

    from db.connection import get_connection

    cutoff = datetime.now() - timedelta(days=args.since_days)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n{'='*70}")
    print(f"P0 监测假阴回补脚本 · 模式={mode} · cutoff={cutoff.isoformat()}")
    print(f"{'='*70}")

    conn = get_connection()
    try:
        cur = conn.cursor()

        # 1. 扫候选记录(is_detected=0 + snippet 非空 + 时间窗内)
        # JOIN confirmed_keywords 拿 target_brand 通过 keyword_id 链路
        # 注意:monitoring_results.keyword_id 关联 confirmed_keywords.id(若 confirmed_keyword_id 不空)
        #       或 client_keywords.id(老数据)· 这里走 confirmed_keywords 主路径 · brand_name 从 quotes/brands JOIN
        candidate_sql = """
            SELECT
                mr.id AS result_id,
                mr.task_id,
                mr.keyword_id,
                mr.platform,
                mr.response_snippet,
                COALESCE(ck.brand_id, mt.brand_id) AS brand_id,
                b.name AS brand_name
            FROM monitoring_results mr
            LEFT JOIN monitoring_tasks mt ON mt.id = mr.task_id
            LEFT JOIN confirmed_keywords ck ON ck.id = mr.keyword_id OR ck.id = mr.confirmed_keyword_id
            LEFT JOIN brands b ON b.id = COALESCE(ck.brand_id, mt.brand_id)
            WHERE mr.is_detected = 0
              AND mr.response_snippet IS NOT NULL
              AND CHAR_LENGTH(mr.response_snippet) >= 50
              AND mr.tested_at >= %s
              AND b.name IS NOT NULL
        """
        params = [cutoff]
        if args.limit > 0:
            candidate_sql += " LIMIT %s"
            params.append(args.limit)

        cur.execute(candidate_sql, params)
        candidates = cur.fetchall()
        total = len(candidates)
        print(f"\n[扫描] 候选记录 {total} 条(is_detected=0 + snippet 非空 + 有 brand_name)")

        # 2. Python 端 fallback 逻辑判断(跟 batch_monitor.py:155 一致)
        to_update = []
        for r in candidates:
            snippet = r["response_snippet"] or ""
            brand = r["brand_name"] or ""
            if brand and (brand in snippet or brand.lower() in snippet.lower()):
                to_update.append(r["result_id"])

        hit_rate = 100.0 * len(to_update) / total if total > 0 else 0
        print(f"[匹配] 命中品牌名 fallback 的 {len(to_update)} 条 / {total} = {hit_rate:.1f}%")

        if not to_update:
            print("\n[结束] 0 条需要回补 · 退出")
            return

        # 3. 抽样 10 条展示(让 Deploy-CTO 肉眼校验)
        print("\n[抽样] 前 10 条候选(brand_name 在 snippet 中命中):")
        sample = candidates[:10]
        for r in sample:
            brand = r["brand_name"] or ""
            snippet = (r["response_snippet"] or "")[:120].replace("\n", " ")
            hit = brand and (brand in snippet or brand.lower() in snippet.lower())
            print(f"  task={r['task_id']} platform={r['platform']} brand={brand!r}")
            print(f"    snippet[:120]={snippet!r}")
            print(f"    hit={hit}")

        # 4. 写 DB 或 dry-run
        if not args.apply:
            print(f"\n[DRY-RUN] 跳过 UPDATE · 将影响 {len(to_update)} 条记录")
            print(f"            如需真改:加 --apply(必须先 pg_dump 备份)")
            return

        # 真改 DB
        print(f"\n[APPLY] 即将 UPDATE {len(to_update)} 条 monitoring_results · 5 秒后开始...")
        import time
        time.sleep(5)

        # 批量 UPDATE 用 ANY 数组防 SQL 注入 + 性能
        cur.execute(
            """
            UPDATE monitoring_results
            SET is_detected = 1,
                mention_type = 'fallback_text_match_backfill'
            WHERE id = ANY(%s)
              AND is_detected = 0
            """,
            (to_update,)
        )
        affected = cur.rowcount
        conn.commit()
        print(f"[完成] 实际 UPDATE {affected} 行")

        # 5. 重算受影响 task 的 detection_rate(防 update_task_status 时 stale)
        cur.execute(
            """
            SELECT DISTINCT task_id FROM monitoring_results
            WHERE id = ANY(%s)
            """,
            (to_update,)
        )
        affected_tasks = [r["task_id"] for r in cur.fetchall()]
        print(f"[重算] 涉及 {len(affected_tasks)} 个 task · 仅打日志 · update_task_status 走应用层不在此脚本范围")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
