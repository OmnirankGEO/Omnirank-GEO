"""监测订阅计费主体回填 · audit P0-4 返修 · 2026-06-10

存量错位:prod 18 个 active/paused 订阅 user_id=1(admin 帮客户点开通时挂到了 admin 名下),
真实计费主体应是 brands.owner_user_id(品牌归属人)。挂 admin → admin 免扣 → 每天白烧
4 引擎成本 + 漏收 + 免扣后照记假流水(audit P0-4)。

写入侧已根治(server.py enable / batch-enable 锚 owner + fail-closed),本脚本回填存量。

回填口径(= 返修清单 A.#4.3 原文 SQL):
  UPDATE keyword_monitor_subscriptions s SET user_id = b.owner_user_id
  FROM brands b WHERE s.brand_id = b.id
    AND s.status IN ('active','paused_low_balance')
    AND b.owner_user_id IS NOT NULL AND b.owner_user_id <> s.user_id;

用法:
  python -X utf8 scripts/monitor_billing_owner_backfill_2026_06_10.py            # 默认 dry-run(只读核行数)
  python -X utf8 scripts/monitor_billing_owner_backfill_2026_06_10.py --apply    # 真写入(单事务 · 失败回滚)

退出码:0 = 完成 / 2 = DB 错
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from typing import List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))


def _connect():
    from db.connection import get_connection
    return get_connection()


_CANDIDATE_SQL = """
    SELECT s.id AS subscription_id, s.user_id AS old_user_id,
           s.brand_id, b.owner_user_id AS new_user_id, s.status,
           s.keyword_id, s.quote_id
    FROM keyword_monitor_subscriptions s
    JOIN brands b ON b.id = s.brand_id
    WHERE s.status IN ('active', 'paused_low_balance')
      AND b.owner_user_id IS NOT NULL
      AND b.owner_user_id <> s.user_id
    ORDER BY s.id
"""

# 返修清单 A.#4.3 原文 SQL(集合更新 · 与候选 SELECT 同口径)
_UPDATE_SQL = """
    UPDATE keyword_monitor_subscriptions s
    SET user_id = b.owner_user_id, updated_at = CURRENT_TIMESTAMP
    FROM brands b
    WHERE s.brand_id = b.id
      AND s.status IN ('active', 'paused_low_balance')
      AND b.owner_user_id IS NOT NULL
      AND b.owner_user_id <> s.user_id
"""


def run(apply: bool = False) -> dict:
    try:
        conn = _connect()
    except Exception as e:
        return {"ok": False, "error": f"DB 连接失败: {e}"}
    try:
        cur = conn.cursor()
        cur.execute(_CANDIDATE_SQL)
        candidates: List[dict] = [dict(r) for r in cur.fetchall()]
        if not apply:
            # dry-run:只读 · 不改库
            try:
                conn.rollback()
            except Exception:
                pass
            return {
                "ok": True,
                "apply": False,
                "candidates_total": len(candidates),
                "candidates": candidates[:50],
            }
        # apply:单事务 · UPDATE 后核行数 · 失败回滚
        cur.execute(_UPDATE_SQL)
        affected = cur.rowcount
        if affected != len(candidates):
            conn.rollback()
            return {
                "ok": False,
                "error": f"UPDATE 影响 {affected} 行 != 候选 {len(candidates)} 行 · 已回滚(口径不一致防误写)",
                "candidates_total": len(candidates),
                "update_affected": affected,
            }
        conn.commit()
        return {
            "ok": True,
            "apply": True,
            "candidates_total": len(candidates),
            "update_affected": affected,
            "candidates": candidates[:50],
        }
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": f"执行失败已回滚: {e}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="监测订阅计费主体回填 · audit P0-4 返修")
    ap.add_argument("--apply", action="store_true", help="真写入(默认 dry-run 只读)")
    args = ap.parse_args()
    report = run(apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
