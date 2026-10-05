"""[B3-1] 引擎权重候选管道 DB 层。

geo_engine_weights 是"全局单向量"(engine UNIQUE · 无 industry/status 列 · prod 长期 0 行 →
_get_engine_weights 走硬编码兜底)。它没有候选/审核生命周期,因此权重候选走独立 staging 表
geo_engine_weight_candidates:Stage 7 聚合后按各行业引用份额生成候选 → 人工审核 →
通过才 UPSERT 进 geo_engine_weights(第一个写路径)。

红线:本模块不 import/调用任何 billing/charge/freeze/deduct。审核 UPSERT 只改 geo_engine_weights 权重。
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, Optional

from db.connection import get_connection

logger = logging.getLogger("GEO-EngineWeightCandidates")


def init_engine_weight_candidate_tables() -> None:
    """幂等建表(与 scripts/migration_geo_engine_weight_candidates_2026_07_03.sql 同口径)。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS geo_engine_weight_candidates (
                id              BIGSERIAL PRIMARY KEY,
                industry        TEXT NOT NULL,
                engine          TEXT NOT NULL,
                current_weight  REAL,
                suggested_weight REAL NOT NULL,
                evidence        JSONB NOT NULL DEFAULT '{}'::jsonb,
                status          VARCHAR(20) NOT NULL DEFAULT 'candidate',
                reviewed_by     BIGINT,
                review_note     TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                reviewed_at     TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_gewc_open
                ON geo_engine_weight_candidates (industry, engine)
                WHERE status = 'candidate'
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_gewc_status
                ON geo_engine_weight_candidates (status, created_at DESC)
        """)
        conn.commit()
    finally:
        conn.close()


def _current_global_weights(cur) -> dict[str, float]:
    """现行全局权重:geo_engine_weights 有数据取它,否则硬编码兜底(与 _get_engine_weights 同口径)。"""
    cur.execute("SELECT engine, weight FROM geo_engine_weights")
    rows = cur.fetchall()
    if rows:
        base = {r["engine"]: float(r["weight"]) for r in rows}
    else:
        base = {}
    try:
        from services.placement_service import ENGINE_WEIGHTS
        for eng, w in ENGINE_WEIGHTS.items():
            base.setdefault(eng, float(w))
    except Exception:
        pass
    return base


def generate_engine_weight_candidates(
    industries: Optional[list[str]] = None,
    *,
    min_citations: int = 30,
    rel_threshold: float = 0.10,
) -> int:
    """按各行业各引擎真实引用份额生成/刷新权重候选。

    - 引用份额 = 该引擎引用总数 / 该行业全引擎引用总数(0..1)。
    - 与现行权重相对差异 < rel_threshold → 不生成(避免噪音候选)。
    - 同 (industry,engine) 至多一条 open 候选(ON CONFLICT 刷新);最近一条 rejected 若 ~同值 → 跳过(不重复提)。
    - 只对 canonical 引擎(在现行权重字典里的 key)生成,避免脏别名污染。
    返回本次新建/刷新的候选数。best-effort,失败抛出由调用方(stage7 hook)吞掉。
    """
    init_engine_weight_candidate_tables()
    conn = get_connection()
    created = 0
    try:
        cur = conn.cursor()
        current = _current_global_weights(cur)

        params: list[Any] = []
        where = ""
        if industries:
            ph = ",".join(["%s"] * len(industries))
            where = f"WHERE industry IN ({ph})"
            params = list(industries)
        cur.execute(f"""
            SELECT industry, engine,
                   SUM(citation_count) AS cites,
                   SUM(total_queries)  AS samples
            FROM geo_engine_stats
            {where}
            GROUP BY industry, engine
        """, params)

        by_industry: dict[str, dict[str, tuple]] = defaultdict(dict)
        for r in cur.fetchall():
            by_industry[r["industry"]][r["engine"]] = (int(r["cites"] or 0), int(r["samples"] or 0))

        for industry, eng_map in by_industry.items():
            total_cites = sum(c for c, _ in eng_map.values())
            if total_cites < min_citations:
                continue
            for engine, (cites, samples) in eng_map.items():
                if engine not in current:  # 只对 canonical 引擎提候选
                    continue
                share = cites / total_cites if total_cites else 0.0
                cur_w = float(current.get(engine, 0.0))
                # 差异不足阈值 → 不生成噪音候选
                if cur_w > 0 and abs(share - cur_w) / cur_w < rel_threshold:
                    continue
                if cur_w == 0 and abs(share) < 0.02:
                    continue
                suggested = round(share, 4)
                # 同值 rejected 去重
                cur.execute("""
                    SELECT status, suggested_weight FROM geo_engine_weight_candidates
                    WHERE industry=%s AND engine=%s ORDER BY created_at DESC LIMIT 1
                """, (industry, engine))
                last = cur.fetchone()
                if last and last["status"] == "rejected" and abs(float(last["suggested_weight"]) - suggested) < 0.02:
                    continue
                evidence = json.dumps({
                    "citation_share": round(share, 4),
                    "engine_citations": cites,
                    "industry_total_citations": total_cites,
                    "samples": samples,
                }, ensure_ascii=False)
                cur.execute("""
                    INSERT INTO geo_engine_weight_candidates
                        (industry, engine, current_weight, suggested_weight, evidence, status)
                    VALUES (%s, %s, %s, %s, %s::jsonb, 'candidate')
                    ON CONFLICT (industry, engine) WHERE status='candidate'
                    DO UPDATE SET suggested_weight=EXCLUDED.suggested_weight,
                                  current_weight=EXCLUDED.current_weight,
                                  evidence=EXCLUDED.evidence,
                                  created_at=NOW()
                """, (industry, engine, cur_w, suggested, evidence))
                created += 1
        conn.commit()
        return created
    finally:
        conn.close()


def list_engine_weight_candidates(status: str = "candidate", limit: int = 100) -> list[dict[str, Any]]:
    init_engine_weight_candidate_tables()
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = []
        where = ""
        if status:
            where = "WHERE status = %s"
            params.append(status)
        params.append(max(1, min(int(limit or 100), 500)))
        cur.execute(f"""
            SELECT id, industry, engine, current_weight, suggested_weight, evidence,
                   status, reviewed_by, review_note, created_at, reviewed_at
            FROM geo_engine_weight_candidates
            {where}
            ORDER BY created_at DESC
            LIMIT %s
        """, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def review_engine_weight_candidate(candidate_id: int, reviewer_id: int, decision: str, note: str = "") -> dict[str, Any]:
    """审核一条权重候选。approve → UPSERT geo_engine_weights(第一个写路径)+ 标记 approved;reject → 标记 rejected。

    审计:候选行记录 reviewed_by/review_note/reviewed_at;geo_engine_weights.source 记候选与审核人。
    """
    if decision not in ("approve", "reject"):
        return {"status": "bad_decision"}
    init_engine_weight_candidate_tables()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM geo_engine_weight_candidates WHERE id=%s", (candidate_id,))
        cand = cur.fetchone()
        if not cand:
            return {"status": "not_found"}
        if cand["status"] != "candidate":
            return {"status": "already_reviewed", "candidate_status": cand["status"]}

        new_weight = None
        if decision == "approve":
            new_weight = float(cand["suggested_weight"])
            cur.execute("""
                INSERT INTO geo_engine_weights (engine, weight, source, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (engine) DO UPDATE SET
                    weight = EXCLUDED.weight,
                    source = EXCLUDED.source,
                    updated_at = NOW()
            """, (cand["engine"], new_weight,
                  f"flywheel_candidate:{candidate_id}:reviewer:{reviewer_id}"))
            new_status = "approved"
        else:
            new_status = "rejected"

        cur.execute("""
            UPDATE geo_engine_weight_candidates
            SET status=%s, reviewed_by=%s, review_note=%s, reviewed_at=NOW()
            WHERE id=%s
        """, (new_status, reviewer_id, note, candidate_id))
        conn.commit()
        return {
            "status": "success",
            "decision": decision,
            "engine": cand["engine"],
            "industry": cand["industry"],
            "new_weight": new_weight,
        }
    finally:
        conn.close()
