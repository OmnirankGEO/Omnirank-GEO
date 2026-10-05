"""
监测乱入修 · backfill is_monitored 老数据 · 一次性脚本
[CTO-15.23 2026-05-08]

老数据状态:
  confirmed_keywords.is_monitored 默认 FALSE(刚加的字段)
  但客户已付费的 quote 应该 is_monitored=TRUE

backfill 来源:
  keyword_selection_sessions.final_keyword_ids JSONB · 客户实际选中的 id 列表
  status='confirmed' 的 session 即客户已付费

usage:
  cd /c/AI-Test/AgentsCope-07
  python scripts/backfill_is_monitored_2026_05_08.py --dry-run     # 看影响行数
  python scripts/backfill_is_monitored_2026_05_08.py --execute     # 真跑

输出:scripts/backfill_is_monitored_result.json
"""
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import get_connection
from services.selected_keyword_resolver import (
    resolve_selected_keyword_texts,
    safe_json_list,
)


PAID_STATUSES = {"confirmed", "paid", "active"}


def build_quote_targets_from_sessions(rows, *, include_quoted: bool = False) -> dict:
    """Build quote_id -> selected keyword text targets from session rows."""
    allowed_statuses = set(PAID_STATUSES)
    if include_quoted:
        allowed_statuses.add("quoted")

    result = {
        "sessions_scanned": 0,
        "selected_ids_total": 0,
        "selected_keywords_total": 0,
        "quote_to_keywords": {},
        "skipped_sessions": [],
    }
    for row in rows:
        status = row.get("status")
        quote_id = row.get("quote_id")
        if status not in allowed_statuses or not quote_id:
            result["skipped_sessions"].append({
                "quote_id": quote_id,
                "status": status,
                "reason": "status_not_in_scope" if quote_id else "missing_quote_id",
            })
            continue

        selected_ids = safe_json_list(row.get("final_keyword_ids")) or safe_json_list(row.get("selected_keyword_ids"))
        if not selected_ids:
            result["skipped_sessions"].append({
                "quote_id": quote_id,
                "status": status,
                "reason": "missing_selected_ids",
            })
            continue

        keywords = resolve_selected_keyword_texts(
            selected_ids,
            keywords_snapshot=row.get("keywords_snapshot"),
            pricing_data=row.get("pricing_data"),
            clusters_data=row.get("clusters_data"),
        )
        if not keywords:
            result["skipped_sessions"].append({
                "quote_id": quote_id,
                "status": status,
                "reason": "ids_not_resolved_to_keyword_text",
                "selected_ids": selected_ids,
            })
            continue

        bucket = result["quote_to_keywords"].setdefault(int(quote_id), [])
        for keyword in keywords:
            if keyword not in bucket:
                bucket.append(keyword)
        result["sessions_scanned"] += 1
        result["selected_ids_total"] += len(selected_ids)
        result["selected_keywords_total"] += len(keywords)
    return result


def collect_quote_targets(*, include_quoted: bool = False) -> dict:
    """从 keyword_selection_sessions 拉客户实际选中的 keyword 文本列表"""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT quote_id, status, final_keyword_ids, selected_keyword_ids,
                   keywords_snapshot, pricing_data, clusters_data
              FROM keyword_selection_sessions
             WHERE status IN ('confirmed', 'paid', 'active', 'quoted')
               AND (final_keyword_ids IS NOT NULL OR selected_keyword_ids IS NOT NULL)
         """)
        return build_quote_targets_from_sessions(cur.fetchall(), include_quoted=include_quoted)


def execute_update(quote_to_keywords: dict, dry_run: bool = True, reset_unselected: bool = True) -> dict:
    """跑 UPDATE · dry-run 会显式验证 target 表命中数再 ROLLBACK"""
    affected_total = 0
    matching_total = 0
    reset_total = 0
    missing_by_quote = {}
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN;")
            for quote_id, keywords in quote_to_keywords.items():
                if not keywords:
                    continue

                cur.execute("""
                    SELECT keyword
                      FROM confirmed_keywords
                     WHERE quote_id = %s AND keyword = ANY(%s)
                """, (quote_id, keywords))
                matched_keywords = [
                    row.get("keyword") if isinstance(row, dict) else row[0]
                    for row in cur.fetchall()
                ]
                matching_total += len(matched_keywords)
                missing = sorted(set(keywords) - set(matched_keywords))
                if missing:
                    missing_by_quote[str(quote_id)] = missing
                if not matched_keywords:
                    continue

                if reset_unselected:
                    cur.execute("""
                        UPDATE confirmed_keywords
                           SET is_monitored = FALSE
                         WHERE quote_id = %s
                           AND COALESCE(is_monitored, FALSE) = TRUE
                    """, (quote_id,))
                    reset_total += cur.rowcount

                cur.execute("""
                    UPDATE confirmed_keywords
                       SET is_monitored = TRUE,
                           monitoring_status = COALESCE(monitoring_status, 'active'),
                           archived_at = NULL,
                           archive_reason = NULL
                     WHERE quote_id = %s AND keyword = ANY(%s)
                """, (quote_id, matched_keywords))
                affected_total += cur.rowcount
            if quote_to_keywords and matching_total == 0:
                raise RuntimeError(
                    "backfill target verification matched 0 confirmed_keywords rows; "
                    "refusing to update to avoid another empty monitoring incident"
                )
            if dry_run:
                cur.execute("ROLLBACK;")
                print(f"[dry-run] 可命中 {matching_total} 行 · 可标记 {affected_total} 行 · 可重置 {reset_total} 行 · 已 ROLLBACK")
            else:
                cur.execute("COMMIT;")
                print(f"[execute] 标记 {affected_total} 行 is_monitored=TRUE · 重置 {reset_total} 行 · 已 COMMIT")
        except Exception as e:
            cur.execute("ROLLBACK;")
            print(f"[error] {e} · 已 ROLLBACK")
            raise
    return {
        "affected_rows": affected_total,
        "matching_rows": matching_total,
        "reset_rows": reset_total,
        "missing_keywords": missing_by_quote,
        "dry_run": dry_run,
        "reset_unselected": reset_unselected,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只看影响行数 · 不实际 UPDATE")
    parser.add_argument("--execute", action="store_true", help="真跑 UPDATE")
    parser.add_argument("--include-quoted", action="store_true", help="把 quoted session 也纳入回填(默认不纳入)")
    parser.add_argument("--no-reset-unselected", action="store_true", help="不先把同 quote 未选词重置为 is_monitored=FALSE")
    args = parser.parse_args()

    if not args.dry_run and not args.execute:
        print("必须指定 --dry-run 或 --execute")
        sys.exit(1)

    print(f"=== backfill is_monitored ===")
    print(f"[{datetime.now().isoformat()}] 扫描 sessions...")
    targets = collect_quote_targets(include_quoted=args.include_quoted)
    quote_count = len(targets["quote_to_keywords"])
    total_keywords = sum(len(v) for v in targets["quote_to_keywords"].values())
    print(f"  已确认 sessions:{targets['sessions_scanned']}")
    print(f"  quote 数: {quote_count}")
    print(f"  待标记 keyword 文本数: {total_keywords}")
    print(f"  include_quoted: {args.include_quoted}")
    print(f"  reset_unselected: {not args.no_reset_unselected}")

    print(f"\n[{datetime.now().isoformat()}] 执行 UPDATE...")
    update_result = execute_update(
        targets["quote_to_keywords"],
        dry_run=args.dry_run,
        reset_unselected=not args.no_reset_unselected,
    )

    out = {
        "audit_time": datetime.now().isoformat(),
        "mode": "dry-run" if args.dry_run else "execute",
        "sessions_scanned": targets["sessions_scanned"],
        "quotes_affected": quote_count,
        "selected_ids_total": targets["selected_ids_total"],
        "selected_keywords_total": targets["selected_keywords_total"],
        "target_keywords_total": total_keywords,
        "rows_updated": update_result["affected_rows"],
        "matching_rows": update_result["matching_rows"],
        "reset_rows": update_result["reset_rows"],
        "missing_keywords": update_result["missing_keywords"],
        "skipped_sessions": targets["skipped_sessions"],
        "include_quoted": args.include_quoted,
        "reset_unselected": not args.no_reset_unselected,
    }
    out_path = Path(__file__).resolve().parent / "backfill_is_monitored_result.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存:{out_path}")


if __name__ == "__main__":
    main()
