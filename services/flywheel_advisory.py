"""Read-only flywheel advisory observability for admin review.

This module deliberately does not alter writing, publishing, billing, or media
takeover behavior. It only reads the research aggregation output and exposes a
small advisory mode stored in geo_research_config.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from psycopg2.extras import RealDictCursor

from db.connection import get_connection
from services.media_entity_flywheel import industry_filter_values, is_all_industry_scope


ADVISORY_MODE_KEY = "flywheel_advisory_mode"
DEFAULT_ADVISORY_MODE = "off"
ALLOWED_ADVISORY_MODES = {"off", "admin_shadow", "advisory"}
ENABLE_ADVISORY_CONFIRMATION = "ENABLE_FLYWHEEL_ADVISORY"


def _normalize_mode(value: Any) -> str:
    if value in (None, ""):
        return DEFAULT_ADVISORY_MODE
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return DEFAULT_ADVISORY_MODE
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, str):
                raw = decoded.strip()
        except Exception:
            pass
        return raw if raw in ALLOWED_ADVISORY_MODES else DEFAULT_ADVISORY_MODE
    return value if value in ALLOWED_ADVISORY_MODES else DEFAULT_ADVISORY_MODE


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _age_hours(value: Any) -> float | None:
    if not isinstance(value, datetime):
        return None
    # geo_engine_stats.last_updated 是 TIMESTAMP(无时区)→ psycopg2 返回 naive datetime。
    # 若直接用 datetime.now(timezone.utc)(aware)相减会 TypeError,导致 advisory 端点 500
    # 并经前端 Promise.all 拖垮整页。这里按 value 自身的 aware/naive 状态取同类 now。
    if value.tzinfo is None:
        now = datetime.now()
    else:
        now = datetime.now(value.tzinfo)
    return round(max((now - value).total_seconds(), 0) / 3600, 2)


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except Exception:
            return {}
    return {}


def _parse_sample_queries(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(item) for item in value[:6] if item not in (None, "")]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, list):
                return [str(item) for item in decoded[:6] if item not in (None, "")]
        except Exception:
            return [value[:180]]
    return []


def get_flywheel_advisory_mode() -> str:
    """Read the advisory mode from geo_research_config, defaulting closed."""
    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT value_json FROM geo_research_config WHERE key = %s LIMIT 1",
                (ADVISORY_MODE_KEY,),
            )
            row = cur.fetchone()
        return _normalize_mode(row.get("value_json") if row else None)
    except Exception:
        return DEFAULT_ADVISORY_MODE
    finally:
        if conn:
            conn.close()


def set_flywheel_advisory_mode(mode: str, *, operator_id: int | None = None) -> str:
    """Persist the advisory mode. Caller is responsible for admin checks."""
    normalized = _normalize_mode(mode)
    if normalized != mode:
        raise ValueError("invalid_advisory_mode")
    updated_by = str(operator_id) if operator_id is not None else None

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO geo_research_config (key, value_json, description, updated_by, updated_at)
                VALUES (%s, %s::jsonb, %s, %s, NOW())
                ON CONFLICT (key) DO UPDATE
                SET value_json = EXCLUDED.value_json,
                    description = EXCLUDED.description,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
                """,
                (
                    ADVISORY_MODE_KEY,
                    json.dumps(normalized, ensure_ascii=False),
                    "飞轮顾问模式：off/admin_shadow/advisory",
                    updated_by,
                ),
            )
        conn.commit()
        return normalized
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _latest_successful_round(cur: Any) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT round_id, batch_id, status, started_at, finished_at, summary_json
        FROM geo_research_round
        WHERE status IN ('completed', 'partial_success')
        ORDER BY finished_at DESC NULLS LAST, started_at DESC NULLS LAST
        LIMIT 1
        """
    )
    row = cur.fetchone()
    return dict(row) if row else None


def get_flywheel_observability() -> dict[str, Any]:
    """Return admin-safe aggregation health without exposing raw answer text."""
    mode = get_flywheel_advisory_mode()
    conn = None
    latest_round: dict[str, Any] | None = None
    raw_rows = 0
    stats_rows = 0
    latest_stats_at: Any = None
    candidate_count = 0
    failed_resumable_count = 0

    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            latest_round = _latest_successful_round(cur)
            if latest_round and latest_round.get("round_id"):
                batch_id = latest_round.get("batch_id") or f"batch_{latest_round['round_id']}"
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM geo_research_raw WHERE batch_id = %s",
                    (batch_id,),
                )
                raw_rows = _to_int((cur.fetchone() or {}).get("cnt"))

            cur.execute(
                """
                SELECT
                    COUNT(*) AS stats_rows,
                    MAX(last_updated) AS latest_stats_at,
                    COUNT(*) FILTER (
                        WHERE COALESCE(citation_count, 0) > 0
                           OR COALESCE(citation_rate, 0) > 0
                    ) AS candidate_count
                FROM geo_engine_stats
                """
            )
            stats = cur.fetchone() or {}
            stats_rows = _to_int(stats.get("stats_rows"))
            latest_stats_at = stats.get("latest_stats_at")
            candidate_count = _to_int(stats.get("candidate_count"))

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM geo_research_round WHERE status = %s",
                ("failed_resumable",),
            )
            failed_resumable_count = _to_int((cur.fetchone() or {}).get("cnt"))
    except Exception as exc:
        return {
            "mode": mode,
            "latest_round_id": None,
            "latest_round_status": "unavailable",
            "latest_round_finished_at": None,
            "raw_rows": 0,
            "stats_rows": 0,
            "latest_stats_at": None,
            "latest_stats_age_hours": None,
            "candidate_count": 0,
            "failed_resumable_count": 0,
            "total_cost_yuan": 0,
            "candidates_available": False,
            "shadow_only": True,
            "production_takeover": False,
            "error": type(exc).__name__,
        }
    finally:
        if conn:
            conn.close()

    summary = _parse_json_object(latest_round.get("summary_json") if latest_round else None)
    total_cost_yuan = _to_float(summary.get("total_cost_yuan") or summary.get("total_cost") or summary.get("cost_yuan"))

    return {
        "mode": mode,
        "latest_round_id": latest_round.get("round_id") if latest_round else None,
        "latest_round_status": latest_round.get("status") if latest_round else "missing",
        "latest_round_finished_at": _iso(latest_round.get("finished_at") if latest_round else None),
        "raw_rows": raw_rows,
        "stats_rows": stats_rows,
        "latest_stats_at": _iso(latest_stats_at),
        "latest_stats_age_hours": _age_hours(latest_stats_at),
        "candidate_count": candidate_count,
        "failed_resumable_count": failed_resumable_count,
        "total_cost_yuan": round(total_cost_yuan, 2),
        "candidates_available": bool(stats_rows and candidate_count),
        "shadow_only": True,
        "production_takeover": False,
    }


def get_advisory_candidates(industry: str = "", media_type: str = "", limit: int = 20) -> dict[str, Any]:
    """Read advisory candidates from geo_engine_stats.

    Mode off still returns observability, but no candidates.
    """
    mode = get_flywheel_advisory_mode()
    observability = get_flywheel_observability()
    if mode == "off":
        return {
            "status": "success",
            "mode": mode,
            "items": [],
            "observability": observability,
            "shadow_only": True,
            "production_takeover": False,
        }

    safe_limit = max(1, min(int(limit or 20), 100))
    industry_raw = (industry or "").strip()
    all_scope = is_all_industry_scope(industry_raw)
    # geo_engine_stats.industry 存的是调研写入的原始(通常中文)行业名,而调用方传入的
    # 可能是归一化 slug(如 tourism_hotel)。用 industry_filter_values 展开成同一行业的
    # 全部别名(含中文名),用 industry = ANY(%s) 匹配;否则选定具体行业时精确匹配 slug
    # 会恒返回空列表(P1-1)。all_scope(通用/全部行业)时不加行业过滤。
    industry_values = [] if all_scope else (industry_filter_values(industry_raw) or [industry_raw])
    # normalize_industry_key 会把 ASCII 小写化(如 "GEO优化服务"→"geo优化服务"),而库里存原始
    # 大小写;LOWER 两侧比较,避免含英文字母的行业名因大小写 miss(表仅数百行,无索引顾虑)。
    industry_values = [v.lower() for v in industry_values]
    platform_filter = (media_type or "").strip()
    if platform_filter in {"all", "media", "wemedia", "source"}:
        platform_filter = ""

    industry_clause = "TRUE" if all_scope else "LOWER(industry) = ANY(%s)"

    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            params: list[Any] = []
            if not all_scope:
                params.append(industry_values)
            params.extend([platform_filter, platform_filter, platform_filter, safe_limit])
            cur.execute(
                f"""
                SELECT
                    industry,
                    engine,
                    platform,
                    citation_count,
                    total_queries,
                    citation_rate,
                    avg_position,
                    sample_queries,
                    last_updated
                FROM geo_engine_stats
                WHERE {industry_clause}
                  AND (%s = '' OR platform = %s OR engine = %s)
                ORDER BY
                    citation_rate DESC NULLS LAST,
                    citation_count DESC NULLS LAST,
                    avg_position ASC NULLS LAST,
                    platform ASC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall() or []
    finally:
        if conn:
            conn.close()

    latest_round_id = observability.get("latest_round_id")
    items = []
    for row in rows:
        citation_rate = _to_float(row.get("citation_rate"))
        citation_count = _to_int(row.get("citation_count"))
        total_queries = _to_int(row.get("total_queries"))
        platform = row.get("platform") or "未知来源"
        engine = row.get("engine") or "AI 引擎"
        items.append({
            "industry": row.get("industry") or "",
            "engine": engine,
            "platform": platform,
            "citation_rate": citation_rate,
            "citation_count": citation_count,
            "total_queries": total_queries,
            "avg_position": _to_float(row.get("avg_position"), default=0.0),
            "sample_queries": _parse_sample_queries(row.get("sample_queries")),
            "suggestion": f"{platform} 在 {engine} 中被引用表现较好，可作为内部顾问建议参考。",
            "source": {
                "type": "flywheel_stats",
                "round_id": latest_round_id,
                "last_updated": _iso(row.get("last_updated")),
                "industry": row.get("industry") or "",
            },
        })

    return {
        "status": "success",
        "mode": mode,
        "items": items,
        "observability": observability,
        "shadow_only": True,
        "production_takeover": False,
    }
