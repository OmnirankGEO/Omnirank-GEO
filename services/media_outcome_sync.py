"""[B5-1] 媒体实体效果回流(闭环)。

背景:compute_media_entity_shadow_score 已含 0.10 outcome 权重,但 outcome_rollup
(published_count / citation_lift_30d)在所有自动路径都是空 dict —— 纯缺回写数据源。

本任务:对有域名 + 有快照的媒体实体,按真实发布/引用数据算
  - published_count:该实体映射到的媒介盒子库存(geo_media_inventory_mappings→mhz)已发布订单数;
  - citation_lift_30d:该实体域名在 geo_research_raw 近 window_days 引用数 − 前一个等长窗口引用数;
回写进最新快照的 evidence.outcome_rollup,并按 SSOT 公式重算 outcome_score / shadow_score
(其余维度分 evidence/quality/inventory 保留)。

纪律:幂等(重跑不双计,直接覆盖最新快照)· advisory 锁防双跑 · fail-soft · 无发布数据零副作用。
红线:只写飞轮快照表,不 import billing/charge/deduct,不改报价/投放。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("GEO-MediaOutcomeSync")


def _count_published_for_entity(cur, entity_id: int) -> int:
    """该实体映射到的 mhz 库存已发布订单数(fail-soft:表缺/查询失败 → 0)。

    用 SAVEPOINT 隔离:mhz 相关表若缺失(如某些环境)导致查询失败,不得把整个外层事务打成 aborted,
    否则后续 _citation_lift / UPDATE 会被连坐(current transaction is aborted)。
    """
    try:
        cur.execute("SAVEPOINT sp_pub")
        cur.execute("""
            SELECT COUNT(*) AS c
            FROM geo_media_inventory_mappings m
            JOIN mhz_publish_order_items it
              ON it.media_id = m.inventory_id
            WHERE m.entity_id = %s
              AND m.media_source = 'mhz_media'
              AND it.published_at IS NOT NULL
        """, (entity_id,))
        result = int((cur.fetchone() or {}).get("c") or 0)
        cur.execute("RELEASE SAVEPOINT sp_pub")
        return result
    except Exception:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT sp_pub")
        except Exception:
            pass
        return 0


def _citation_lift(cur, domain: str, window_days: int) -> int:
    """geo_research_raw 该域名近 window_days 引用数 − 前一等长窗口引用数(可正可负;fail-soft → 0)。

    [review fix] 同 _count_published_for_entity 用 SAVEPOINT 隔离:本查询若失败(表缺/超时等)
    不得把外层事务打成 aborted,否则后续 UPDATE 连坐。补齐 commit message 声称的"子查询 SAVEPOINT 隔离"。
    """
    try:
        cur.execute("SAVEPOINT sp_lift")
        cur.execute("""
            SELECT
              COUNT(*) FILTER (
                WHERE created_at >= NOW() - (%s || ' days')::INTERVAL
              ) AS recent,
              COUNT(*) FILTER (
                WHERE created_at >= NOW() - (%s || ' days')::INTERVAL
                  AND created_at <  NOW() - (%s || ' days')::INTERVAL
              ) AS prior
            FROM geo_research_raw
            WHERE LOWER(cited_platform) IN (%s, %s)
        """, (window_days, window_days * 2, window_days, domain, f"www.{domain}"))
        r = cur.fetchone() or {}
        result = int(r.get("recent") or 0) - int(r.get("prior") or 0)
        cur.execute("RELEASE SAVEPOINT sp_lift")
        return result
    except Exception:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT sp_lift")
        except Exception:
            pass
        return 0


def sync_media_entity_outcome_rollups(window_days: int = 30, limit: int = 500) -> dict:
    """效果回流主任务。返回 {scanned, updated}。"""
    from db.connection import get_connection
    from services.media_entity_flywheel import (
        normalize_domain, outcome_score_from_rollup, blend_shadow_score,
    )

    conn = get_connection()
    scanned = 0
    updated = 0
    try:
        cur = conn.cursor()
        # 事务级 advisory 锁防双跑(与 writing_strategy flywheel 同款,COMMIT 自动释放)
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("media_entity_outcome_sync",))

        cur.execute("""
            SELECT DISTINCT ON (e.id)
                   e.id AS entity_id, e.domain,
                   s.id AS snapshot_id, s.evidence_score, s.quality_score,
                   s.inventory_score, s.evidence
            FROM geo_media_entities e
            JOIN media_entity_score_snapshots s ON s.entity_id = e.id
            WHERE e.domain IS NOT NULL AND e.domain <> ''
            ORDER BY e.id, s.created_at DESC
            LIMIT %s
        """, (int(limit),))
        rows = cur.fetchall()

        for r in rows:
            scanned += 1
            domain = normalize_domain(r["domain"] or "")
            if not domain:
                continue
            published_count = _count_published_for_entity(cur, r["entity_id"])
            citation_lift = _citation_lift(cur, domain, window_days)
            if published_count == 0 and citation_lift == 0:
                continue  # 无回流信号 → 不动(零副作用)

            outcome_rollup = {"published_count": published_count, "citation_lift_30d": citation_lift}
            outcome_score = round(outcome_score_from_rollup(outcome_rollup), 2)
            ev_s = float(r["evidence_score"] or 0)
            q_s = float(r["quality_score"] or 0)
            inv_s = float(r["inventory_score"] or 0)
            new_shadow = round(max(0.0, min(100.0, blend_shadow_score(ev_s, q_s, inv_s, outcome_score))), 2)

            evidence = r["evidence"] if isinstance(r["evidence"], dict) else {}
            evidence = dict(evidence)
            evidence["outcome_rollup"] = outcome_rollup

            cur.execute("""
                UPDATE media_entity_score_snapshots
                SET outcome_score = %s, shadow_score = %s, evidence = %s::jsonb
                WHERE id = %s
            """, (outcome_score, new_shadow, json.dumps(evidence, ensure_ascii=False), r["snapshot_id"]))
            updated += 1

        conn.commit()
        logger.info(f"[B5-1] media outcome sync: scanned={scanned} updated={updated} window={window_days}d")
        return {"scanned": scanned, "updated": updated, "window_days": window_days}
    except Exception as e:
        logger.warning(f"[B5-1] media outcome sync 失败(不阻塞): {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return {"scanned": scanned, "updated": updated, "error": str(e)[:200]}
    finally:
        conn.close()
