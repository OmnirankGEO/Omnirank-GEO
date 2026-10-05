"""
M3 客户行为事件存储 (CTO-C 2026-04-26 · feat/m3-customer-signals)

设计:
  - 单表 m3_customer_events 统一 4 公开 token 链路埋点
    (public_report / public_quote / selection / portal)
  - event_key 后端去重 (UNIQUE 索引保证幂等)
  - 永远不存原始 IP / UA / token · 只存 hash
  - 老板拍板字段 (2026-04-26):
      id / brand_id / quote_id / diagnosis_id / token_hash /
      source / event_type / event_key / metadata /
      ip_hash / user_agent_hash / occurred_at / created_at

老板红线:
  - 不动 5 公开 token 语义
  - daily_salt = sha256(JWT_SECRET + YYYY-MM-DD UTC) · 不靠 env 手动轮换
  - 公开端口永远不能写入原始 IP/UA/token (上层 api 层校验)

事件白名单 (第一批 7 类 · 不做更细):
  opened / dwell_30s / dwell_120s / saw_price / cta_click /
  submitted_keywords / renewed_interest

来源白名单 (4 类):
  public_report / public_quote / selection / portal
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from db.connection import get_connection

logger = logging.getLogger("GEO-M3-Events-DB")


ALLOWED_EVENT_TYPES = {
    "opened",
    "dwell_30s",
    "dwell_120s",
    "saw_price",
    "cta_click",
    "submitted_keywords",
    "renewed_interest",
    # CTO-F 2026-04-27 · 写作资料确认链路
    "material_confirmed",
    "material_feedback",
    # PR-B (CTO-15.23 2026-05-03) · 客户决策页 lead 留资 · /agent/leads 时间线消费
    "lead_submitted",
    # Phase 06 (CTO-15.23 2026-05-03) · 写作大厅快速写作创建
    "quick_write_created",
    # Phase 06 (CTO-15.23 2026-05-03) · 客户资料跨入口更新事件(T8 cache invalidation)
    "brand_profile_updated",
}

ALLOWED_SOURCES = {
    "public_report",
    "public_quote",
    "selection",
    "portal",
    # CTO-F 2026-04-27 · /m/:token 写作资料确认链
    "material_confirm",
    # Phase 06 (CTO-15.23 2026-05-03) · 代理端录入(快速写作 / 我的客户 / 诊断 / M3 等)
    "agent_intake",
}

RETENTION_DAYS = 365


# ============================================================
# Schema (幂等)
# ============================================================

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS m3_customer_events (
    id BIGSERIAL PRIMARY KEY,
    brand_id INTEGER,
    quote_id INTEGER,
    diagnosis_id INTEGER,
    token_hash TEXT,
    source VARCHAR(32) NOT NULL,
    event_type VARCHAR(48) NOT NULL,
    event_key TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    ip_hash TEXT,
    user_agent_hash TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

CREATE_INDEX_SQLS = [
    "CREATE INDEX IF NOT EXISTS idx_m3_events_brand_ts "
    "ON m3_customer_events (brand_id, occurred_at DESC) WHERE brand_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_m3_events_quote_ts "
    "ON m3_customer_events (quote_id, occurred_at DESC) WHERE quote_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_m3_events_diag_ts "
    "ON m3_customer_events (diagnosis_id, occurred_at DESC) WHERE diagnosis_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_m3_events_token_ts "
    "ON m3_customer_events (token_hash, occurred_at DESC) WHERE token_hash IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_m3_events_source_type_ts "
    "ON m3_customer_events (source, event_type, occurred_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_m3_events_event_key "
    "ON m3_customer_events (event_key) WHERE event_key IS NOT NULL",
]


def init_table() -> None:
    """初始化表 + 索引 (幂等 · 可重复跑)"""
    conn = get_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(CREATE_TABLE_SQL)
        for sql in CREATE_INDEX_SQLS:
            try:
                cur.execute(sql)
            except Exception as e:
                logger.warning(f"[M3-Events] index 跳过(并发已存在?): {e}")
        logger.info("[M3-Events] m3_customer_events 表初始化完成")
    except Exception as e:
        logger.warning(f"[M3-Events] init_table 失败: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# Hash helpers
# ============================================================

_FALLBACK_SALT = "m3_customer_events_local_dev_DO_NOT_USE_IN_PROD"
_warned_no_secret = False


def _get_app_secret() -> str:
    """读取服务端固定 secret · 优先 JWT_SECRET (项目已有)"""
    global _warned_no_secret
    secret = os.environ.get("JWT_SECRET") or os.environ.get("APP_SECRET")
    if secret:
        return secret
    if not _warned_no_secret:
        logger.warning(
            "[M3-Events] JWT_SECRET / APP_SECRET 未设置 · "
            "ip_hash 走 fallback salt(开发模式 · 生产请配 JWT_SECRET)"
        )
        _warned_no_secret = True
    return _FALLBACK_SALT


def _get_daily_salt() -> str:
    """daily_salt = sha256(APP_SECRET + ":" + YYYY-MM-DD UTC)"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return hashlib.sha256(
        (_get_app_secret() + ":" + today).encode("utf-8")
    ).hexdigest()


def hash_ip(ip: Optional[str]) -> Optional[str]:
    """SHA256(daily_salt + ip)[:32] · 跨天不可关联"""
    if not ip:
        return None
    salt = _get_daily_salt()
    return hashlib.sha256((salt + "|" + str(ip)).encode("utf-8")).hexdigest()[:32]


def hash_ua(ua: Optional[str]) -> Optional[str]:
    """SHA256(ua)[:32] · UA 跨天可关联可接受 (识别浏览器类别)"""
    if not ua:
        return None
    return hashlib.sha256(str(ua).encode("utf-8")).hexdigest()[:32]


def hash_token(token: Optional[str]) -> Optional[str]:
    """SHA256(APP_SECRET + ':' + token)[:32] · token 是稳定标识 · 不带 daily salt"""
    if not token:
        return None
    secret = _get_app_secret()
    return hashlib.sha256(
        (secret + ":token:" + str(token)).encode("utf-8")
    ).hexdigest()[:32]


# ============================================================
# Insert (idempotent via event_key UNIQUE index)
# ============================================================

def insert_event(
    *,
    source: str,
    event_type: str,
    event_key: Optional[str] = None,
    brand_id: Optional[int] = None,
    quote_id: Optional[int] = None,
    diagnosis_id: Optional[int] = None,
    token_hash: Optional[str] = None,
    ip_hash: Optional[str] = None,
    user_agent_hash: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[int]]:
    """
    插入事件 · event_key UNIQUE 索引保证幂等 (同 key 重复静默)

    Returns:
        (inserted, event_id)
        - 新插入: (True, id)
        - 幂等去重: (False, None)
        - 拒绝(白名单失败): (False, None)
    """
    if source not in ALLOWED_SOURCES:
        logger.warning(f"[M3-Events] 拒绝未知 source: {source}")
        return False, None
    if event_type not in ALLOWED_EVENT_TYPES:
        logger.warning(f"[M3-Events] 拒绝未知 event_type: {event_type}")
        return False, None

    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO m3_customer_events
                (brand_id, quote_id, diagnosis_id, token_hash, source, event_type,
                 event_key, metadata, ip_hash, user_agent_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (event_key) WHERE event_key IS NOT NULL
                DO NOTHING
            RETURNING id
            """,
            (
                brand_id, quote_id, diagnosis_id, token_hash,
                source, event_type, event_key, metadata_json,
                ip_hash, user_agent_hash,
            ),
        )
        row = cur.fetchone()
        conn.commit()
        if row:
            evid = row.get("id") if isinstance(row, dict) else row[0]
            return True, int(evid) if evid is not None else None
        return False, None
    except Exception as e:
        logger.warning(
            f"[M3-Events] insert 失败 src={source} type={event_type} key={event_key}: {e}"
        )
        try:
            conn.rollback()
        except Exception:
            pass
        return False, None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# Query
# ============================================================

def _serialize_row(r: Any) -> Dict[str, Any]:
    d = dict(r) if not isinstance(r, dict) else r
    occurred = d.get("occurred_at")
    created = d.get("created_at")
    metadata = d.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    return {
        "id": d.get("id"),
        "brand_id": d.get("brand_id"),
        "quote_id": d.get("quote_id"),
        "diagnosis_id": d.get("diagnosis_id"),
        "source": d.get("source"),
        "event_type": d.get("event_type"),
        "metadata": metadata or {},
        "occurred_at": occurred.isoformat() if hasattr(occurred, "isoformat") else (str(occurred) if occurred else None),
        "created_at": created.isoformat() if hasattr(created, "isoformat") else (str(created) if created else None),
    }


def query_events(
    *,
    brand_id: Optional[int] = None,
    quote_id: Optional[int] = None,
    diagnosis_id: Optional[int] = None,
    sources: Optional[List[str]] = None,
    days: int = 30,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """读时间序事件列表 (occurred_at DESC) · 不返回 token/ip/ua hash"""
    conds: List[str] = []
    params: List[Any] = []

    if brand_id is not None:
        conds.append("brand_id = %s")
        params.append(brand_id)
    if quote_id is not None:
        conds.append("quote_id = %s")
        params.append(quote_id)
    if diagnosis_id is not None:
        conds.append("diagnosis_id = %s")
        params.append(diagnosis_id)
    if sources:
        valid = [s for s in sources if s in ALLOWED_SOURCES]
        if valid:
            placeholders = ",".join(["%s"] * len(valid))
            conds.append(f"source IN ({placeholders})")
            params.extend(valid)

    days_safe = max(1, min(int(days or 30), 365))
    conds.append("occurred_at >= NOW() - (%s || ' days')::INTERVAL")
    params.append(str(days_safe))

    where_clause = " AND ".join(conds) if conds else "TRUE"
    limit_safe = max(1, min(int(limit or 100), 500))

    sql = f"""
        SELECT id, brand_id, quote_id, diagnosis_id, source, event_type,
               metadata, occurred_at, created_at
        FROM m3_customer_events
        WHERE {where_clause}
        ORDER BY occurred_at DESC
        LIMIT %s
    """
    params.append(limit_safe)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []
        return [_serialize_row(r) for r in rows]
    except Exception as e:
        logger.warning(f"[M3-Events] query 失败: {e}")
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def summary_events(
    *,
    brand_id: Optional[int] = None,
    quote_id: Optional[int] = None,
    days: int = 30,
) -> Dict[str, Any]:
    """聚合摘要 · 给 M3 信号时间线 + M3 今日销售页 why-now 算法用

    返回结构:
      {
        "per_source": {
          "public_report": {"counts": {"opened": 3, "dwell_30s": 1}, "last_at": "..."},
          ...
        },
        "latest_overall_at": ISO|null,
        "opened_24h": {"public_report": 2, ...},
        "reopened_24h": bool,  # 任意一个 source 24h 内 opened ≥ 2
      }
    """
    days_safe = max(1, min(int(days or 30), 365))
    base_conds: List[str] = ["occurred_at >= NOW() - (%s || ' days')::INTERVAL"]
    base_params: List[Any] = [str(days_safe)]
    if brand_id is not None:
        base_conds.append("brand_id = %s")
        base_params.append(brand_id)
    if quote_id is not None:
        base_conds.append("quote_id = %s")
        base_params.append(quote_id)
    where_clause = " AND ".join(base_conds)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT source, event_type, COUNT(*) AS cnt, MAX(occurred_at) AS last_at
            FROM m3_customer_events
            WHERE {where_clause}
            GROUP BY source, event_type
            """,
            tuple(base_params),
        )
        rows = cur.fetchall() or []

        per_source: Dict[str, Dict[str, Any]] = {}
        latest_overall_iso: Optional[str] = None
        for r in rows:
            d = dict(r) if not isinstance(r, dict) else r
            src = d.get("source") or "unknown"
            etype = d.get("event_type") or "unknown"
            cnt = int(d.get("cnt") or 0)
            last_at = d.get("last_at")
            last_iso = last_at.isoformat() if hasattr(last_at, "isoformat") else None
            if src not in per_source:
                per_source[src] = {"counts": {}, "last_at": None}
            per_source[src]["counts"][etype] = cnt
            cur_last = per_source[src]["last_at"]
            if last_iso and (not cur_last or last_iso > cur_last):
                per_source[src]["last_at"] = last_iso
            if last_iso and (not latest_overall_iso or last_iso > latest_overall_iso):
                latest_overall_iso = last_iso

        # 24h 内 opened (按 source 分桶)
        rd_conds: List[str] = [
            "event_type = 'opened'",
            "occurred_at >= NOW() - INTERVAL '24 hours'",
        ]
        rd_params: List[Any] = []
        if brand_id is not None:
            rd_conds.append("brand_id = %s")
            rd_params.append(brand_id)
        if quote_id is not None:
            rd_conds.append("quote_id = %s")
            rd_params.append(quote_id)
        rd_where = " AND ".join(rd_conds)
        cur.execute(
            f"""
            SELECT source, COUNT(*) AS cnt
            FROM m3_customer_events
            WHERE {rd_where}
            GROUP BY source
            """,
            tuple(rd_params),
        )
        opened_24h: Dict[str, int] = {}
        for r in cur.fetchall() or []:
            d = dict(r) if not isinstance(r, dict) else r
            src = d.get("source") or "unknown"
            opened_24h[src] = int(d.get("cnt") or 0)

        return {
            "per_source": per_source,
            "latest_overall_at": latest_overall_iso,
            "opened_24h": opened_24h,
            "reopened_24h": any(v >= 2 for v in opened_24h.values()),
        }
    except Exception as e:
        logger.warning(f"[M3-Events] summary 失败: {e}")
        return {
            "per_source": {},
            "latest_overall_at": None,
            "opened_24h": {},
            "reopened_24h": False,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# Cleanup (scheduler 每日 03:15 跑)
# ============================================================

def cleanup_expired(retention_days: int = RETENTION_DAYS) -> int:
    """删除 retention_days 之前的事件 · 返删除行数"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM m3_customer_events "
            "WHERE occurred_at < NOW() - (%s || ' days')::INTERVAL",
            (str(retention_days),),
        )
        n = cur.rowcount or 0
        conn.commit()
        if n > 0:
            logger.info(f"[M3-Events] cleanup 删除 {n} 行 (>{retention_days} 天)")
        return n
    except Exception as e:
        logger.warning(f"[M3-Events] cleanup 失败: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass
