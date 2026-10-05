#!/usr/bin/env python3
"""
[CTO-15.23 2026-05-05] 回填升级词丢失数据

背景:
  confirm_quote 旧版只遍历 cluster.core_keywords 标 is_selected
  客户从 covered_keywords 提升上来的关键词:id 在 selected_set 里
  但 keyword 在 cluster.covered_keywords 里 → 永远不被标 → mark_paid 漏写 confirmed_keywords
  影响:写作大厅 / 监测中心 / 报告 / 账单 全部漏

修复 commit: 7148cb1d (confirm_quote 加升级词搬运逻辑)
此脚本: 扫历史 session · 找漏写的升级词 · 补 INSERT confirmed_keywords

用法:
  默认 dry-run:
    docker exec omnirank-ai python3 /app/scripts/ops_backfill_promoted_keywords_2026_05_05.py

  真执行:
    docker exec omnirank-ai python3 /app/scripts/ops_backfill_promoted_keywords_2026_05_05.py --apply

逻辑:
  1. SELECT confirmed/active sessions WHERE clusters_data + final_keyword_ids 非空
  2. 解析 final_keyword_ids + clusters_data
  3. 找升级词:cluster.covered_keywords 中 id 在 final_ids 里的
  4. 校验:confirmed_keywords WHERE quote_id+keyword 是否已存在
  5. dry-run: 打印 · --apply: INSERT confirmed_keywords
"""
import argparse
import json
import sys
from datetime import datetime

sys.path.insert(0, '/app')

from db.connection import get_connection


def find_promoted_keywords(session: dict) -> list[dict]:
    """
    从一个 session 找出"应有但漏写"的升级词
    返回 [{quote_id, brand_id, keyword, tier, final_price, required_articles, category, intent}]
    """
    quote_id = session['quote_id']
    if not quote_id:
        return []

    final_ids_raw = session.get('final_keyword_ids')
    clusters_raw = session.get('clusters_data')
    tier = session.get('selected_tier') or 'standard'

    try:
        final_ids = set(json.loads(final_ids_raw)) if final_ids_raw else set()
        clusters_data = json.loads(clusters_raw) if clusters_raw else {}
    except (json.JSONDecodeError, TypeError):
        return []

    if not final_ids or not clusters_data.get('clusters'):
        return []

    promoted = []
    for cluster in clusters_data['clusters']:
        if not cluster.get('is_selected'):
            continue
        # cluster.core_keywords 不会漏(原循环已处理) · 只看 covered_keywords
        for ck in cluster.get('covered_keywords', []):
            if ck.get('id') not in final_ids:
                continue
            # 这是升级词 · 应在 confirmed_keywords 里
            kw_text = ck.get('keyword', '').strip()
            if not kw_text:
                continue
            tier_info = ck.get(tier, {}) if isinstance(ck.get(tier), dict) else {}
            promoted.append({
                'quote_id': quote_id,
                'brand_id': session.get('brand_id'),
                'keyword': kw_text,
                'category': cluster.get('business_tag') or '通用词',
                'tier': tier,
                'base_price': float(tier_info.get('price', 0) or 0),
                'final_price': float(tier_info.get('price', 0) or 0),
                'required_articles': int(tier_info.get('articles', 1) or 1),
                'intent': ck.get('intent', 'informational'),
                'funnel_stage': ck.get('funnel_stage', 'awareness'),
                'cluster_id': cluster.get('cluster_id'),
            })
    return promoted


def check_already_exists(cur, quote_id: int, keyword: str) -> bool:
    """检查 confirmed_keywords 是否已有该 (quote_id, keyword)"""
    cur.execute(
        "SELECT id FROM confirmed_keywords WHERE quote_id = %s AND keyword = %s",
        (quote_id, keyword)
    )
    return cur.fetchone() is not None


def main():
    parser = argparse.ArgumentParser(description='回填升级词丢失数据')
    parser.add_argument('--apply', action='store_true',
                        help='真执行 INSERT confirmed_keywords (默认仅 dry-run)')
    args = parser.parse_args()

    mode = '【真执行】' if args.apply else '【dry-run】'
    print(f"\n{'=' * 70}")
    print(f"  升级词丢失回填脚本 {mode}")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 70}\n")

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 拉所有 confirmed/active session 含 cluster_data + final_keyword_ids
        cur.execute("""
            SELECT id, quote_id, brand_id, final_keyword_ids, clusters_data,
                   selected_tier, status, confirmed_at
            FROM keyword_selection_sessions
            WHERE status IN ('confirmed', 'active')
              AND clusters_data IS NOT NULL
              AND final_keyword_ids IS NOT NULL
            ORDER BY confirmed_at DESC NULLS LAST
        """)
        sessions = [dict(r) for r in cur.fetchall()]
        print(f"扫描到 {len(sessions)} 个候选 session\n")

        total_found = 0
        total_already = 0
        total_to_backfill = 0
        backfill_records = []

        for sess in sessions:
            promoted = find_promoted_keywords(sess)
            if not promoted:
                continue

            sess_id = sess['id']
            quote_id = sess['quote_id']
            print(f"\n--- session_id={sess_id} quote_id={quote_id} (status={sess['status']}) ---")

            for kw in promoted:
                total_found += 1
                if check_already_exists(cur, quote_id, kw['keyword']):
                    total_already += 1
                    print(f"  ✓ 已存在: 「{kw['keyword']}」")
                else:
                    total_to_backfill += 1
                    backfill_records.append(kw)
                    print(f"  ✗ 漏写: 「{kw['keyword']}」 tier={kw['tier']} 价格={kw['final_price']} 篇数={kw['required_articles']}")

        print(f"\n{'=' * 70}")
        print(f"  汇总:")
        print(f"    找到升级词总数: {total_found}")
        print(f"    已正常写入: {total_already}")
        print(f"    需回填: {total_to_backfill}")
        print(f"{'=' * 70}\n")

        if total_to_backfill == 0:
            print("✓ 无需回填 · 历史数据全部正常")
            conn.close()
            return 0

        if not args.apply:
            print(f"[dry-run] 检测到 {total_to_backfill} 条需回填 · 加 --apply 真执行")
            conn.close()
            return 0

        # 真执行 · 单事务
        print(f"[apply] 开始回填 {total_to_backfill} 条 ...")
        try:
            for kw in backfill_records:
                cur.execute("""
                    INSERT INTO confirmed_keywords (
                        quote_id, brand_id, keyword, category, tier,
                        base_price, city_premium, final_price, competitor_count,
                        required_articles, intent, funnel_stage, status, is_core, cluster_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    kw['quote_id'], kw['brand_id'], kw['keyword'], kw['category'], kw['tier'],
                    kw['base_price'], 1.0, kw['final_price'], 0,
                    kw['required_articles'], kw['intent'], kw['funnel_stage'],
                    'pending', True, kw['cluster_id'],
                ))
                new_id = cur.fetchone()['id']
                print(f"  ✓ 回填: 「{kw['keyword']}」 → confirmed_keywords.id={new_id}")
            # 同步 quotes.total_keywords += 升级词数(按 quote_id 分组)
            kw_count_per_quote: dict[int, int] = {}
            for kw in backfill_records:
                kw_count_per_quote[kw['quote_id']] = kw_count_per_quote.get(kw['quote_id'], 0) + 1
            for qid, n in kw_count_per_quote.items():
                cur.execute(
                    "UPDATE quotes SET total_keywords = COALESCE(total_keywords, 0) + %s WHERE id = %s",
                    (n, qid)
                )
                print(f"  ✓ quote {qid}: total_keywords += {n}")
            conn.commit()
            print(f"\n✅ 回填完成 · 共 {total_to_backfill} 条")
        except Exception as e:
            conn.rollback()
            print(f"\n❌ 回填失败 · 已回滚: {e}")
            return 1

        cur.close()
        conn.close()
        return 0
    except Exception as e:
        print(f"\n❌ 脚本异常: {e}")
        import traceback
        traceback.print_exc()
        try:
            conn.close()
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
