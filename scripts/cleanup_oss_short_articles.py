"""
一次性清理脚本 · 删 OSS 上字数 < ARTICLE_MIN_CHARS_FOR_REVIEW 的废文件

背景:
  D 阶段(2026-05-08)首轮跑批暴露的设计 bug — stage 4 清洗后立即上 OSS,
  字数判断在 stage 5(已经晚了)。导致 477 篇全传 OSS, 但只有 108 篇真进
  pending_review, 369 篇是 review_status='auto_skipped' 的废文件。

  round_runner.py:550 已修(字数预判) · 此脚本清旧账。

用法:
    python scripts/cleanup_oss_short_articles.py --dry-run    # 预演 (默认)
    python scripts/cleanup_oss_short_articles.py --full       # 真删
"""
import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

from db.connection import get_connection
from services.research_monitor.oss_helper import OssHelper

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger("OSS-Cleanup")

ARTICLE_MIN_CHARS_FOR_REVIEW = 3000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', default=True)
    parser.add_argument('--full', action='store_true', help='真删 OSS + 清 DB')
    args = parser.parse_args()
    if args.full:
        args.dry_run = False

    mode = 'DRY-RUN' if args.dry_run else 'FULL'
    logger.info(f"=== 模式 {mode} · 清理 cleaned_char_count < {ARTICLE_MIN_CHARS_FOR_REVIEW} 的 OSS 废文件 ===")

    # 1. 找出所有 oss_key_cleaned IS NOT NULL AND cleaned_char_count < 3000 的 article
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, url, cleaned_char_count, oss_key_cleaned, review_status, first_seen_round_id
              FROM geo_research_articles
             WHERE oss_key_cleaned IS NOT NULL
               AND cleaned_char_count < %s
            """,
            (ARTICLE_MIN_CHARS_FOR_REVIEW,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    logger.info(f"发现 {len(rows)} 篇待清理 article (cleaned_char_count < {ARTICLE_MIN_CHARS_FOR_REVIEW} 但有 OSS key)")

    if not rows:
        logger.info("无需清理 · 退出")
        return

    # 2. 按 review_status 分组统计
    by_status = {}
    for r in rows:
        s = r['review_status']
        by_status[s] = by_status.get(s, 0) + 1
    logger.info("按 review_status 分布:")
    for s, c in sorted(by_status.items()):
        logger.info(f"  {s}: {c}")

    # 3. 字数分布抽样
    char_buckets = {'0-500': 0, '500-1000': 0, '1000-2000': 0, '2000-3000': 0}
    for r in rows:
        cc = r['cleaned_char_count'] or 0
        if cc < 500:
            char_buckets['0-500'] += 1
        elif cc < 1000:
            char_buckets['500-1000'] += 1
        elif cc < 2000:
            char_buckets['1000-2000'] += 1
        else:
            char_buckets['2000-3000'] += 1
    logger.info("字数分布:")
    for b, c in char_buckets.items():
        logger.info(f"  {b}: {c}")

    if args.dry_run:
        logger.info("=== DRY-RUN 完成 · 未真删 · 用 --full 真跑 ===")
        return

    # 4. 真删 OSS + 清 DB
    helper = OssHelper.get_instance()
    deleted_oss = 0
    failed_oss = 0
    cleared_db = 0

    for i, r in enumerate(rows, 1):
        oss_key = r['oss_key_cleaned']

        # 删 OSS
        try:
            helper.bucket.delete_object(oss_key)
            deleted_oss += 1
        except Exception as e:
            logger.warning(f"[{i}/{len(rows)}] OSS 删除失败 article_id={r['id']} key={oss_key}: {e}")
            failed_oss += 1
            # OSS 删失败仍清 DB 字段(避免下次扫到再试,记 warning 即可)

        # 清 DB 字段
        c = get_connection()
        try:
            cc = c.cursor()
            cc.execute(
                "UPDATE geo_research_articles SET oss_key_cleaned=NULL WHERE id=%s",
                (r['id'],),
            )
            c.commit()
            cleared_db += 1
        finally:
            c.close()

        if i % 50 == 0:
            logger.info(f"  进度 {i}/{len(rows)} · OSS 删 {deleted_oss} 失败 {failed_oss} · DB 清 {cleared_db}")

    logger.info(f"=== 清理完成 · OSS 删 {deleted_oss}/{len(rows)} · OSS 失败 {failed_oss} · DB 清 {cleared_db} ===")


if __name__ == '__main__':
    main()
