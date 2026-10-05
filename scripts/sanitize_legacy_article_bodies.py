"""[存量救治]一次性清洗存量稿件正文里的内部记号(WP11 sanitizer)。

Owner 已批。**幂等**、**手动触发**(挂 admin 动作,不进 cron)。

## 干什么

存量 draft/待发布态文章的正文里混着内部记号(核验语言、BF-/EV- 编号、证据状态表、
待核验句式)。这些是生产过程的痕迹,不该出现在给客户看的稿子里。
本脚本用 `writing.body_internal_marker_sanitizer.sanitize_article_body` 把它们
挪到 `quality_warning` / `metadata`,正文即刻干净。

## 硬边界(违反任一即跳过,不商量)

1. **已发布 / 有不可变快照的文章一律跳过** —— 快照不可变是底线,清洗一篇已发布的
   稿子等于篡改已交付物。
2. **有在途发布订单的文章一律跳过** —— 这正是 P0 那个坑:窗口内改稿会让订单快照
   失配,把客户拖进死锁。别自己再踩一遍。
3. 清洗后哈希变了且该文有审核记录 → **自动触发 refresh_article_review**,
   否则发布口会 hash 失配(同上,还是 P0 那个坑)。

## 可回滚

每篇把清洗前正文备份进 `articles.metadata.sanitize_backup`(含时间戳与原哈希),
出问题可逐篇还原。

## 用法

    python scripts/sanitize_legacy_article_bodies.py --dry-run          # 只出清单
    python scripts/sanitize_legacy_article_bodies.py --dry-run --limit 50
    python scripts/sanitize_legacy_article_bodies.py --apply            # 真清洗

也可由 admin 动作调用 `run_sanitize_legacy_bodies(dry_run=...)`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

# 从 scripts/ 直接跑时项目根不在 sys.path —— 补上,否则脚本只能在 pytest 里跑
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("GEO-SanitizeLegacy")

#: 只碰这些状态的稿子。已发布/已交付的一律不在范围内。
ELIGIBLE_STATUSES = ("draft", "pending", "ready", "reviewing", "approved")

BACKUP_KEY = "sanitize_backup"
SANITIZE_VERSION = "legacy-body-sanitize-v1"


def _hash(text: Optional[str]) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _is_skippable(row: dict[str, Any]) -> Optional[str]:
    """返回跳过原因;None = 可以清洗。硬边界都在这里,一处判完。"""
    if row.get("first_published_at"):
        return "already_published"
    if str(row.get("status") or "").strip() not in ELIGIBLE_STATUSES:
        return f"status_not_eligible:{row.get('status')}"
    if row.get("has_immutable_snapshot"):
        return "immutable_snapshot"
    if row.get("has_active_publication"):
        return "active_publication_order"
    if not (row.get("content") or "").strip():
        return "empty_body"
    return None


def _select_candidates(cursor, limit: Optional[int]) -> list[dict[str, Any]]:
    """一次查全所有判据,避免逐篇 N+1 再去问"能不能动"。"""
    cursor.execute(
        f"""
        SELECT a.id, a.title, a.status, a.content, a.first_published_at,
               a.article_review_status,
               EXISTS (
                   SELECT 1 FROM mhz_publish_order_items i
                     JOIN mhz_publish_orders o ON o.id = i.order_id
                    WHERE o.article_id = a.id
                      AND i.status IN ('pending','submitting','submitted',
                                       'awaiting_sync','awaiting_confirmation','awaiting_action')
               ) AS has_active_publication,
               EXISTS (
                   SELECT 1 FROM mhz_publish_order_items i2
                     JOIN mhz_publish_orders o2 ON o2.id = i2.order_id
                    WHERE o2.article_id = a.id
                      AND i2.status IN ('published','success')
               ) AS has_immutable_snapshot
          FROM articles a
         ORDER BY a.id
         {"LIMIT %s" if limit else ""}
        """,
        (int(limit),) if limit else (),
    )
    return [dict(r) for r in (cursor.fetchall() or [])]


def run_sanitize_legacy_bodies(
    *, dry_run: bool = True, limit: Optional[int] = None, cursor=None,
) -> dict[str, Any]:
    """执行(或试跑)清洗。返回可直接贴进 EXIT 的统计 + 清单。

    幂等:已经干净的稿子 sanitize 后内容不变 → 不写库、不计入 changed。
    """
    from writing.body_internal_marker_sanitizer import sanitize_article_body

    own = cursor is None
    conn = None
    if own:
        from db.connection import get_connection

        conn = get_connection()
        cursor = conn.cursor()

    report: dict[str, Any] = {
        "version": SANITIZE_VERSION,
        "dry_run": bool(dry_run),
        "scanned": 0,
        "would_change": 0,
        "changed": 0,
        "skipped": {},
        "review_refreshed": 0,
        "items": [],
    }

    try:
        rows = _select_candidates(cursor, limit)
        report["scanned"] = len(rows)

        for row in rows:
            skip = _is_skippable(row)
            if skip:
                key = skip.split(":", 1)[0]
                report["skipped"][key] = report["skipped"].get(key, 0) + 1
                continue

            original = row.get("content") or ""
            cleaned, markers = sanitize_article_body(original)
            if cleaned == original:
                continue        # 已经干净 —— 幂等的关键

            report["would_change"] += 1
            report["items"].append({
                "article_id": row["id"],
                "title": (row.get("title") or "")[:60],
                "status": row.get("status"),
                "removed_chars": len(original) - len(cleaned),
                # sanitizer 返回的 dict 里既有列表也有标量(版本号等),只统计列表类
                "markers": {k: len(v) for k, v in (markers or {}).items()
                            if isinstance(v, (list, tuple)) and v},
            })

            if dry_run:
                continue

            # 每篇一个 SAVEPOINT:
            #   1) 一篇出错不毒化整批(psycopg2 里任一语句报错后事务即 aborted,
            #      后续 commit 实际等于 ROLLBACK —— 会把前面已改的稿子一起撤掉,
            #      而报表还在报"已清洗 N 篇" = 假成功);
            #   2) 正文改动与审核刷新**必须原子** —— 刷不了审核就连正文一起不改,
            #      否则留下"内容变了、审核还是旧的"的稿子,发布口 hash 失配,
            #      正是本轮 P0 那个坑。
            cursor.execute("SAVEPOINT sanitize_article")

            # 备份进 metadata(可逐篇回滚)
            backup = {
                "at": datetime.now(timezone.utc).isoformat(),
                "version": SANITIZE_VERSION,
                "original_hash": _hash(original),
                "original_content": original,
            }
            cursor.execute(
                """
                UPDATE articles
                   SET content = %s,
                       metadata = COALESCE(metadata, '{}'::jsonb)
                                  || jsonb_build_object(%s, %s::jsonb),
                       quality_warning = COALESCE(NULLIF(quality_warning, ''), %s),
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (cleaned, BACKUP_KEY, json.dumps(backup, ensure_ascii=False),
                 "存量清洗:已将内部记号移出正文", int(row["id"])),
            )
            # 哈希变了且有审核记录 → 必须重走审核,否则发布口 hash 失配(P0 那个坑)
            if row.get("article_review_status"):
                try:
                    from services.article_review_gate import refresh_article_review

                    refresh_article_review(int(row["id"]), cursor=cursor)
                    report["review_refreshed"] += 1
                except Exception as exc:
                    # 审核刷不了 → 整篇回退(含正文),宁可这篇不清洗
                    cursor.execute("ROLLBACK TO SAVEPOINT sanitize_article")
                    report["failed"] = report.get("failed", 0) + 1
                    report["failures"] = report.get("failures", [])
                    report["failures"].append({"article_id": row["id"], "error": str(exc)[:200]})
                    logger.warning(
                        "[sanitize] article=%s 重走审核失败,已回退该篇(正文未改): %s", row["id"], exc
                    )
                    continue

            cursor.execute("RELEASE SAVEPOINT sanitize_article")
            report["changed"] += 1

        if own and conn is not None and not dry_run:
            conn.commit()
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="存量稿件正文内部记号清洗(幂等)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只出清单,不写库")
    mode.add_argument("--apply", action="store_true", help="真的清洗")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report = run_sanitize_legacy_bodies(dry_run=bool(args.dry_run), limit=args.limit)

    if report["dry_run"]:
        print(f"扫描 {report['scanned']} 篇 · 将影响 {report['would_change']} 篇")
    else:
        print(f"扫描 {report['scanned']} 篇 · 命中 {report['would_change']} 篇 · "
              f"**实际清洗 {report['changed']} 篇** · 重走审核 {report['review_refreshed']} 篇 · "
              f"回退 {report.get('failed', 0)} 篇")
        for failure in report.get("failures", [])[:10]:
            print(f"  !! #{failure['article_id']} 回退: {failure['error']}")
    print(f"跳过明细: {report['skipped']}")
    for item in report["items"][:50]:
        print(f"  #{item['article_id']} [{item['status']}] -{item['removed_chars']}字 "
              f"{item['markers']} {item['title']}")
    if len(report["items"]) > 50:
        print(f"  ...(共 {len(report['items'])} 篇,此处只列前 50)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
