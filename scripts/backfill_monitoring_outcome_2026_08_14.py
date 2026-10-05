# -*- coding: utf-8 -*-
"""P0-3/R2-2 · monitoring_results 存量 target_outcome 解析回填(2026-08-15 R2 重构)。

研究定稿 J9 红线桥:target_outcome 有效解析 1,387/92,979 = **1.5%(已配对
子样本口径)**;生产 legacy_unknown 91,592 行,其中 **63,594 行 full_response
为空**。这是大面积重写历史解释,不是补标签(R2-2),因此:

  1. **旧值账本先行**:每个待改行先落
     `monitoring_outcome_backfill_journal`(旧值/旧版本/批次 ID/分类输入摘要,
     同一事务),配可执行恢复脚本
     `scripts/restore_monitoring_outcome_backfill_2026_08_15.py`;
  2. **空答案独立态**:full_response 为空 → `no_answer_unjudgeable`
     (「无答案不可判」),**不塞进任何业务分类**(也不算 engine_error ——
     那是把"没答案"编成"引擎错");该态不进 D6-B/三桥率的有效分母;
  3. 只碰未解析行(target_outcome IS NULL 或 'legacy_unknown' 哨兵),
     UPDATE 带同款幂等护栏,重跑收敛到 0;
  4. resolver 版本独立(BACKFILL_RESOLVER_VERSION),可按版本整体复核/恢复;
  5. **自动 apply 默认关闭**(api/scheduler 的 cron 受
     GEO_OUTCOME_BACKFILL_AUTO_APPLY 闸,默认 off,直到本项单独签发);
     手动 --apply 仍可在演练库/签发后使用。

用法:
    python scripts/backfill_monitoring_outcome_2026_08_14.py                      # dry-run
    python scripts/backfill_monitoring_outcome_2026_08_14.py --apply --batch-id X
    python scripts/backfill_monitoring_outcome_2026_08_14.py --apply --batch 2000 --max-batches 10

分类:O1 观测面回填 —— 失败/中断只影响本批,监测/发布/计费主链零依赖。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BACKFILL_RESOLVER_VERSION = "target-outcome-v1.1-legacy-backfill-2026-08-14"
#: [R2-2] 空答案独立态:无答案不可判,不属于十分类里的任何实质结论。
NO_ANSWER_OUTCOME = "no_answer_unjudgeable"


def classify_legacy_row(row: dict[str, Any], brand_name: str) -> tuple[str, float] | None:
    """单行回填判定(纯函数,判别测试打这里)。

    返回 (outcome, confidence);无法诚实判定(拿不到品牌名)→ None = 跳过。
    🔴 [R2-2] full_response 为空 → NO_ANSWER_OUTCOME:生产 63,594 行空答案,
    判成 engine_error/refused 都是编造实质结论 —— 无答案就是无答案。
    """
    from services.monitoring_lineage import classify_target_outcome

    target_brand = str(row.get("target_brand_snapshot") or "").strip() or str(brand_name or "").strip()
    if not target_brand:
        return None
    full_response = str(row.get("full_response") or "")
    if not full_response.strip():
        return NO_ANSWER_OUTCOME, 1.0
    response_status = str(row.get("response_status") or "").strip()
    if response_status == "legacy_unknown":
        response_status = ""  # DDL 哨兵默认值 = 该列在写入时代不存在,视同缺失
    if not response_status:
        # legacy 行早于 response_status 列:非空回答 = 请求成功(推断不是发明)。
        response_status = "success"
    return classify_target_outcome(
        response_status=response_status,
        target_brand=target_brand,
        full_response=full_response,
        is_detected=bool(row.get("is_detected")),
        mention_type=str(row.get("mention_type") or "none"),
        search_citations=str(row.get("search_citations") or ""),
    )


def input_summary_of(row: dict[str, Any]) -> dict[str, Any]:
    """分类输入摘要(进账本):形态位 + 正文指纹,不存正文原文。"""
    full_response = str(row.get("full_response") or "")
    return {
        "response_empty": not full_response.strip(),
        "response_len": len(full_response),
        "response_sha256": hashlib.sha256(full_response.encode("utf-8")).hexdigest() if full_response else None,
        "is_detected": bool(row.get("is_detected")),
        "mention_type": str(row.get("mention_type") or "none"),
        "response_status_raw": row.get("response_status"),
        "brand_source": ("snapshot" if str(row.get("target_brand_snapshot") or "").strip()
                         else "task_brand"),
    }


def run_backfill(*, apply: bool, batch: int, max_batches: int,
                 batch_id: str | None = None) -> dict[str, Any]:
    from db.connection import get_db

    resolved_batch_id = batch_id or f"mob-{uuid.uuid4().hex[:12]}"
    totals = {"scanned": 0, "written": 0, "journaled": 0, "skipped_no_brand": 0,
              "batches": 0, "apply": bool(apply), "batch_id": resolved_batch_id,
              "resolver_version": BACKFILL_RESOLVER_VERSION}
    last_id = 0
    for _ in range(max(1, max_batches)):
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT r.id, r.full_response, r.response_status, r.is_detected,
                       r.mention_type, r.search_citations, r.target_brand_snapshot,
                       r.target_outcome AS old_outcome,
                       r.outcome_resolver_confidence AS old_confidence,
                       r.outcome_resolver_version AS old_version,
                       b.name AS brand_name
                  FROM monitoring_results r
                  JOIN monitoring_tasks t ON t.id = r.task_id
                  LEFT JOIN brands b ON b.id = t.brand_id
                 WHERE (r.target_outcome IS NULL OR r.target_outcome = 'legacy_unknown')
                   AND r.id > %s
                 ORDER BY r.id
                 LIMIT %s
                """,
                (last_id, max(1, batch)),
            )
            rows = [dict(r) for r in cur.fetchall() or []]
            if not rows:
                break
            totals["batches"] += 1
            for row in rows:
                totals["scanned"] += 1
                last_id = max(last_id, int(row["id"]))
                verdict = classify_legacy_row(row, str(row.get("brand_name") or ""))
                if verdict is None:
                    totals["skipped_no_brand"] += 1
                    continue
                outcome, confidence = verdict
                if not apply:
                    totals["written"] += 1  # dry-run:会写的行数
                    continue
                # 🔴 [R2-2] 账本先行,同一事务:旧值/批次/输入摘要没落下,不许改行。
                cur.execute(
                    """
                    INSERT INTO monitoring_outcome_backfill_journal (
                        batch_id, monitoring_result_id,
                        old_target_outcome, old_resolver_confidence, old_resolver_version,
                        new_target_outcome, new_resolver_confidence, new_resolver_version,
                        input_summary
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (monitoring_result_id, batch_id) DO NOTHING
                    """,
                    (resolved_batch_id, int(row["id"]),
                     row.get("old_outcome"), row.get("old_confidence"), row.get("old_version"),
                     outcome, float(confidence), BACKFILL_RESOLVER_VERSION,
                     json.dumps(input_summary_of(row), ensure_ascii=False)),
                )
                totals["journaled"] += int(getattr(cur, "rowcount", 0) or 0)
                # 🔴 幂等护栏进 SQL:只改仍未解析的行(并发/重跑都不会覆盖已解析值)。
                cur.execute(
                    """
                    UPDATE monitoring_results
                       SET target_outcome = %s,
                           outcome_resolver_confidence = %s,
                           outcome_resolver_version = %s
                     WHERE id = %s
                       AND (target_outcome IS NULL OR target_outcome = 'legacy_unknown')
                    """,
                    (outcome, confidence, BACKFILL_RESOLVER_VERSION, int(row["id"])),
                )
                totals["written"] += int(getattr(cur, "rowcount", 0) or 0)
            if apply:
                conn.commit()
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真写(缺省 dry-run)")
    parser.add_argument("--batch", type=int, default=5000)
    parser.add_argument("--max-batches", type=int, default=4)
    parser.add_argument("--batch-id", default=None, help="批次 ID(恢复脚本按它反做)")
    args = parser.parse_args()
    totals = run_backfill(apply=args.apply, batch=args.batch,
                          max_batches=args.max_batches, batch_id=args.batch_id)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {totals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
