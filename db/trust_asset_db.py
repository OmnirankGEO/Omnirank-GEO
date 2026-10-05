"""
[P0-D 信任资产/引用难度因子 2026-06-14] 品牌级信任资产快照 DB 层(独立批)

设计要点(对齐设计文档 §4.3 + 红线纪律):
  ① 独立快照表 brand_trust_asset_snapshot — 大证据 JSON 不堆 brands(诊断主表)/ client_profiles
     (社媒客户档案已 20+ 字段)· 范式对标 competitor_snapshots / publish_decision_snapshots。
  ② latest active 模型 — 每品牌 ≤1 条 is_active(唯一 partial 索引兜底并发)· append-only。
  ③ 写:事务内先 UPDATE 旧 active→FALSE 再 INSERT 新行(is_active=TRUE)。
  ④ 读:WHERE brand_id AND is_active ORDER BY generated_at DESC LIMIT 1。
  ⑤ 全部 fail-soft:任何 DB 异常返回 None / 不抛(绝不阻塞诊断/报价)。

🔴 红线:不碰 billing / auth / db.connection · 新表 IF NOT EXISTS(无 DROP/RENAME/TRUNCATE)。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("GEO-TrustAssetDB")


# ============================================================
# DDL(canonical · init_db 兜底复用此清单 · 全 IF NOT EXISTS 幂等)
# ============================================================
TRUST_ASSET_DDL_STATEMENTS: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS brand_trust_asset_snapshot (
        id                        BIGSERIAL PRIMARY KEY,
        brand_id                  INTEGER NOT NULL,
        is_active                 BOOLEAN DEFAULT TRUE,
        trust_asset_source        VARCHAR(16) DEFAULT 'collected',
        trust_asset_evidence      JSONB NOT NULL DEFAULT '{}'::jsonb,
        trust_asset_score         REAL,
        citation_readiness_score  REAL,
        missing_trust_assets      JSONB DEFAULT '[]'::jsonb,
        verified_trust_assets     JSONB DEFAULT '[]'::jsonb,
        trust_asset_needs_review  BOOLEAN DEFAULT FALSE,
        generated_at              TIMESTAMPTZ DEFAULT NOW(),
        created_at                TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    # 🔴 唯一 active 约束:防并发采集出现同品牌两条 active → 报价读哪条不稳定。
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_trust_asset_active "
    "ON brand_trust_asset_snapshot(brand_id) WHERE is_active",
    # 读路径(latest active 查询)。
    "CREATE INDEX IF NOT EXISTS idx_trust_asset_read "
    "ON brand_trust_asset_snapshot(brand_id, is_active, generated_at DESC)",
]


def _conn():
    from db.connection import get_connection
    return get_connection()


def init_trust_asset_table() -> None:
    """启动时调用:幂等创建表 + 索引(任何异常只 log 不抛)。"""
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        for stmt in TRUST_ASSET_DDL_STATEMENTS:
            cur.execute(stmt)
        conn.commit()
    except Exception as exc:
        logger.warning("init_trust_asset_table 失败(忽略 · 表可能已存在或权限): %s", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def resolve_brand_id(brand_name: str) -> Optional[int]:
    """品牌名 → brand_id(归一化口径与 batch_pricing._lock_brand_industry 一致:精确 name 匹配)。"""
    if not brand_name:
        return None
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM brands WHERE name = %s LIMIT 1", (brand_name,))
        row = cur.fetchone()
        if not row:
            return None
        return int(row["id"] if isinstance(row, dict) else row[0])
    except Exception as exc:
        logger.debug("resolve_brand_id(%r) 失败: %s", brand_name, exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def get_active_trust_snapshot(
    brand_id: Optional[int] = None,
    brand_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """读品牌 latest active 快照 raw row(dict)· 无则 None · 任何异常返回 None(fail-soft)。
    brand_id 优先;否则用 brand_name 解析。"""
    if brand_id is None and brand_name:
        brand_id = resolve_brand_id(brand_name)
    if brand_id is None:
        return None
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, brand_id, is_active, trust_asset_source, trust_asset_evidence,
                   trust_asset_score, citation_readiness_score, missing_trust_assets,
                   verified_trust_assets, trust_asset_needs_review, generated_at, created_at
            FROM brand_trust_asset_snapshot
            WHERE brand_id = %s AND is_active
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (int(brand_id),),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.debug("get_active_trust_snapshot(brand_id=%s) 失败: %s", brand_id, exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def save_trust_snapshot(brand_id: int, payload: Dict[str, Any]) -> Optional[int]:
    """写新 latest active 快照(事务:降旧 active → INSERT 新行)· 返回新行 id · 失败返回 None。

    payload 键(缺省安全):
      trust_asset_source / trust_asset_evidence(dict)/ trust_asset_score(float)/
      citation_readiness_score(float)/ missing_trust_assets(list)/ verified_trust_assets(list)/
      trust_asset_needs_review(bool)
    """
    if brand_id is None:
        return None
    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        # 事务内:先降旧 active 再 INSERT(唯一 partial 索引兜底并发)
        cur.execute(
            "UPDATE brand_trust_asset_snapshot SET is_active = FALSE "
            "WHERE brand_id = %s AND is_active",
            (int(brand_id),),
        )
        cur.execute(
            """
            INSERT INTO brand_trust_asset_snapshot
              (brand_id, is_active, trust_asset_source, trust_asset_evidence,
               trust_asset_score, citation_readiness_score, missing_trust_assets,
               verified_trust_assets, trust_asset_needs_review, generated_at)
            VALUES (%s, TRUE, %s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
            """,
            (
                int(brand_id),
                str(payload.get("trust_asset_source", "collected"))[:16],
                json.dumps(payload.get("trust_asset_evidence") or {}, ensure_ascii=False),
                _f(payload.get("trust_asset_score")),
                _f(payload.get("citation_readiness_score")),
                json.dumps(payload.get("missing_trust_assets") or [], ensure_ascii=False),
                json.dumps(payload.get("verified_trust_assets") or [], ensure_ascii=False),
                bool(payload.get("trust_asset_needs_review", False)),
            ),
        )
        row = cur.fetchone()
        conn.commit()
        return int(row["id"] if isinstance(row, dict) else row[0]) if row else None
    except Exception as exc:
        logger.warning("save_trust_snapshot(brand_id=%s) 失败(回滚): %s", brand_id, exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_json_list(v) -> List[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            j = json.loads(v)
            return j if isinstance(j, list) else []
        except Exception:
            return []
    return []


# 缺采集/缺背书口径所用资产类人话 label(与 trust_asset_collector.ASSET_LABELS 同源思想·此处只取展示)
def normalize_trust_asset_for_pricing(
    row: Optional[Dict[str, Any]],
    *,
    stale_days: int = 30,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """raw 快照 row → 报价消费用归一化 dict(纯函数 · 无 DB · 可单测)。

    口径(设计 §5.1):
      - row 为 None(没采到)→ source='missing' + trust_asset_needs_review=True(内部标·不抬价)。
      - row.source='collected' 且 generated_at 超 stale_days → 降级 source='stale'(用旧值 + 内部 review)。
      - verified/missing 只透传【人话 label】(不带 url/domain → 客户面可见无泄露)。
    """
    if not row:
        return {
            "source": "missing",
            "trust_asset_score": None,
            "citation_readiness_score": None,
            "trust_asset_needs_review": True,
            "verified_labels": [],
            "missing_labels": [],
        }

    source = str(row.get("trust_asset_source") or "collected")
    needs_review = bool(row.get("trust_asset_needs_review", False))

    # 新鲜度:collected 超龄 → stale(用旧值 + 内部 review · 不阻塞报价 · §4.4)
    gen = row.get("generated_at")
    if source == "collected" and gen is not None:
        ref = now or datetime.now(timezone.utc)
        try:
            if isinstance(gen, datetime):
                g = gen if gen.tzinfo else gen.replace(tzinfo=timezone.utc)
                if (ref - g) > timedelta(days=int(stale_days)):
                    source = "stale"
                    needs_review = True
        except Exception:
            pass

    verified = _parse_json_list(row.get("verified_trust_assets"))
    missing = _parse_json_list(row.get("missing_trust_assets"))
    verified_labels = [str(a.get("label")) for a in verified
                       if isinstance(a, dict) and a.get("label")]
    missing_labels = [str(a.get("label")) for a in missing
                      if isinstance(a, dict) and a.get("label")]

    return {
        "source": source,
        "trust_asset_score": _f(row.get("trust_asset_score")),
        "citation_readiness_score": _f(row.get("citation_readiness_score")),
        "trust_asset_needs_review": needs_review,
        "verified_labels": verified_labels,
        "missing_labels": missing_labels,
    }
