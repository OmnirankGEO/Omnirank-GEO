"""
历史数据回填脚本 v1.0

对已有的 monitoring_results 执行蒸馏提取，
写入 response_insights 表。

使用方式：
    python -m tools.distillation.backfill
"""

import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent.parent / "db" / "geo_diagnosis.db"


def _get_brand_mapping() -> dict:
    """构建 quote_id → (brand_id, brand_name) 映射表"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.execute("SELECT id, brand_id, brand_name FROM quotes WHERE brand_id IS NOT NULL")
    mapping = {}
    for row in cursor.fetchall():
        mapping[str(row[0])] = (row[1], row[2])
    conn.close()
    print(f"[Backfill] 加载 {len(mapping)} 条 quote→brand 映射")
    return mapping


def backfill_all(dry_run: bool = False, batch_size: int = 100) -> dict:
    """
    回填所有历史 monitoring_results 到 response_insights

    Args:
        dry_run: True 则只统计，不写入
        batch_size: 每批处理数量

    Returns:
        {"total": N, "processed": N, "saved": N, "skipped": N, "errors": N}
    """
    from tools.distillation.extractor import extract_insights
    from db.distillation_db import save_insight, init_distillation_tables

    # 确保表存在
    init_distillation_tables()

    # 加载映射
    brand_mapping = _get_brand_mapping()

    # 获取所有未蒸馏的结果
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Legacy local databases predate the durable identity-review columns.  They
    # contain no pending cells, so keep those databases readable; once the new
    # columns exist, the shared aggregate gate is mandatory for backfills too.
    from services.monitoring_identity_review import aggregate_eligible_sql

    result_columns = {
        str(row[1]) for row in cursor.execute("PRAGMA table_info(monitoring_results)")
    }
    identity_columns = {
        "identity_review_state",
        "response_status",
        "mention_type",
    }
    identity_gate = (
        aggregate_eligible_sql("mr")
        if identity_columns.issubset(result_columns)
        else "1 = 1"
    )

    # 查找尚未蒸馏的结果（不在 response_insights 中的 result_id）
    cursor.execute(f"""
        SELECT mr.id, mr.task_id, mr.keyword, mr.platform,
               mr.full_response, mr.is_detected, mr.mention_type,
               mt.client_id
        FROM monitoring_results mr
        JOIN monitoring_tasks mt ON mr.task_id = mt.id
        LEFT JOIN response_insights ri ON mr.id = ri.result_id
        WHERE ri.id IS NULL
          AND mr.full_response IS NOT NULL
          AND LENGTH(mr.full_response) > 50
          AND {identity_gate}
        ORDER BY mr.id ASC
    """)

    results = [dict(row) for row in cursor.fetchall()]
    conn.close()

    total = len(results)
    processed = 0
    saved = 0
    skipped = 0
    errors = 0

    print(f"[Backfill] 找到 {total} 条待回填记录")

    if dry_run:
        print("[Backfill] dry_run 模式，不执行写入")
        # 统计有多少能找到 brand_id
        with_brand = sum(1 for r in results if r["client_id"] in brand_mapping)
        print(f"[Backfill] 其中 {with_brand} 条有 brand_id 映射")
        return {"total": total, "with_brand_id": with_brand, "dry_run": True}

    for i, result in enumerate(results):
        try:
            result_id = result["id"]
            client_id = result.get("client_id", "")
            full_response = result.get("full_response", "")

            # 映射 brand_id
            brand_info = brand_mapping.get(client_id)
            brand_id = brand_info[0] if brand_info else None
            brand_name = brand_info[1] if brand_info else ""

            if not brand_id:
                skipped += 1
                continue

            # 提取特征
            insights = extract_insights(
                full_response=full_response,
                client_brand=brand_name,
            )

            # 保存
            save_insight(
                result_id=result_id,
                brand_id=brand_id,
                client_id=client_id,
                brands_mentioned=insights["brands_mentioned"],
                client_rank=insights["client_rank"],
                client_rank_type=insights["client_rank_type"],
                urls_cited=insights["urls_cited"],
                keywords_used=insights["keywords_used"],
                sentiment=insights["sentiment"],
                recommendation_strength=insights["recommendation_strength"],
                raw_structure=insights["raw_structure"],
                algorithm_version="v1_backfill",
                extraction_method=insights["extraction_method"],
                extraction_confidence=insights["extraction_confidence"],
            )
            saved += 1
            processed += 1

        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"[Backfill] 错误 (result_id={result.get('id')}): {e}")

        # 进度报告
        if (i + 1) % batch_size == 0:
            print(f"[Backfill] 进度: {i+1}/{total} (已保存: {saved}, 跳过: {skipped}, 错误: {errors})")

    summary = {
        "total": total,
        "processed": processed,
        "saved": saved,
        "skipped": skipped,
        "errors": errors,
    }
    print(f"[Backfill] 完成: {summary}")
    return summary


if __name__ == "__main__":
    import sys

    if "--dry-run" in sys.argv:
        backfill_all(dry_run=True)
    else:
        backfill_all()
