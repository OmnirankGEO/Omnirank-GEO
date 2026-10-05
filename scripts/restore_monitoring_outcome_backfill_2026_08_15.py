# -*- coding: utf-8 -*-
"""R2-2 · 历史 outcome 回填的**可执行恢复脚本**(按批次反做,2026-08-15)。

依赖 = `monitoring_outcome_backfill_journal` 旧值账本(**真依赖不是装饰**:
拆掉回填侧的账本写入,本脚本必然恢复 0 行 —— R2-2 反向判据锁死)。

恢复口径:
  - 只恢复**仍带回填版本戳**的行(outcome_resolver_version = 回填版本):
    回填之后被现役 resolver 重写过的行不动 —— 恢复不许反过来毁掉更新的解释;
  - 恢复 = 写回账本里的旧值三元组(old_target_outcome/confidence/version,
    含旧值为 NULL 的如实写回 NULL);
  - 账本行打 restored_at 恢复痕(幂等:已恢复的不重复恢复)。

用法:
    python scripts/restore_monitoring_outcome_backfill_2026_08_15.py --batch-id X           # dry-run
    python scripts/restore_monitoring_outcome_backfill_2026_08_15.py --batch-id X --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backfill_monitoring_outcome_2026_08_14 import BACKFILL_RESOLVER_VERSION  # noqa: E402


def run_restore(*, batch_id: str, apply: bool) -> dict[str, Any]:
    from db.connection import get_db

    totals = {"batch_id": batch_id, "apply": bool(apply),
              "journal_rows": 0, "restored": 0, "skipped_rewritten": 0,
              "already_restored": 0}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT j.id AS journal_id, j.monitoring_result_id,
                   j.old_target_outcome, j.old_resolver_confidence, j.old_resolver_version,
                   j.restored_at,
                   r.outcome_resolver_version AS current_version
              FROM monitoring_outcome_backfill_journal j
              JOIN monitoring_results r ON r.id = j.monitoring_result_id
             WHERE j.batch_id = %s
             ORDER BY j.id
            """,
            (batch_id,),
        )
        rows = [dict(r) for r in cur.fetchall() or []]
        totals["journal_rows"] = len(rows)
        for row in rows:
            if row.get("restored_at") is not None:
                totals["already_restored"] += 1
                continue
            if str(row.get("current_version") or "") != BACKFILL_RESOLVER_VERSION:
                # 回填后被现役 resolver 重写过 → 不动(恢复不毁更新的解释)
                totals["skipped_rewritten"] += 1
                continue
            if not apply:
                totals["restored"] += 1
                continue
            cur.execute(
                """
                UPDATE monitoring_results
                   SET target_outcome = %s,
                       outcome_resolver_confidence = %s,
                       outcome_resolver_version = %s
                 WHERE id = %s
                   AND outcome_resolver_version = %s
                """,
                (row.get("old_target_outcome"), row.get("old_resolver_confidence"),
                 row.get("old_resolver_version"), int(row["monitoring_result_id"]),
                 BACKFILL_RESOLVER_VERSION),
            )
            if int(getattr(cur, "rowcount", 0) or 0) > 0:
                totals["restored"] += 1
                cur.execute(
                    "UPDATE monitoring_outcome_backfill_journal SET restored_at = NOW() "
                    " WHERE id = %s",
                    (int(row["journal_id"]),),
                )
        if apply:
            conn.commit()
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--apply", action="store_true", help="真写(缺省 dry-run)")
    args = parser.parse_args()
    totals = run_restore(batch_id=args.batch_id, apply=args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {totals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
