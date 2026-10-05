"""飞轮 LLM 判断留痕账本(B 段 · 2026-07-29)。

工单通用约束里的两条硬要求落在这张表上:
  - "每个判断点设每日调用与成本上限,超限降级到规则兜底" → 上限从本表当日汇总算,不靠内存计数器
    (cron 会重启、蓝绿会换主,内存计数器一重启就把额度洗白);
  - "每次判断留痕可复核(输入摘要/输出/模型/版本入库)" → 一次判断一行,含规则兜底那次。

SQL 4 维核验(建表):
  1. 列名:point_key/source/provider/model/prompt_version/input_summary/output/
     input_tokens/output_tokens/cost_cny/latency_ms/error/created_at 均在场
  2. data_type:tokens INTEGER · cost_cny NUMERIC(12,6)(与 llm_call_logs 的元为单位口径一致)·
     input_summary/output JSONB · created_at TIMESTAMPTZ
  3. 字段归属:全部新列在本新表,不碰 llm_call_logs / 任何计费表 —— 本表是观测,不是账单
  4. dry-run:CREATE TABLE / INDEX IF NOT EXISTS 全幂等,无破坏性 SQL
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from db.connection import get_connection, get_db

logger = logging.getLogger("GEO-FlywheelJudgment")

SOURCE_LLM = "llm"
SOURCE_RULE = "rule"

_TABLE_READY = False


def init_flywheel_judgment_tables(force: bool = False) -> None:
    global _TABLE_READY
    if _TABLE_READY and not force:
        return
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS flywheel_judgment_log (
                id BIGSERIAL PRIMARY KEY,
                point_key VARCHAR(60) NOT NULL,
                source VARCHAR(12) NOT NULL,
                provider VARCHAR(40),
                model VARCHAR(120),
                prompt_version VARCHAR(40),
                input_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
                output JSONB NOT NULL DEFAULT '{}'::jsonb,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_cny NUMERIC(12,6) NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                fallback_reason TEXT,
                error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_flywheel_judgment_point_time "
            "ON flywheel_judgment_log(point_key, created_at DESC)"
        )
    _TABLE_READY = True


def record_judgment(
    *,
    point_key: str,
    source: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    prompt_version: Optional[str] = None,
    input_summary: Optional[dict[str, Any]] = None,
    output: Optional[Any] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_cny: float = 0.0,
    latency_ms: int = 0,
    fallback_reason: Optional[str] = None,
    error: Optional[str] = None,
) -> bool:
    """留痕。写失败只记日志 —— 观测层绝不阻断判断本身。"""
    try:
        init_flywheel_judgment_tables()
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO flywheel_judgment_log
                    (point_key, source, provider, model, prompt_version, input_summary, output,
                     input_tokens, output_tokens, cost_cny, latency_ms, fallback_reason, error)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s)
                """,
                (
                    point_key[:60], source[:12], (provider or None), (model or None),
                    (prompt_version or None),
                    json.dumps(input_summary or {}, ensure_ascii=False, default=str),
                    json.dumps(output if output is not None else {}, ensure_ascii=False, default=str),
                    int(input_tokens or 0), int(output_tokens or 0), float(cost_cny or 0),
                    int(latency_ms or 0), (fallback_reason or None),
                    (error or None)[:4000] if error else None,
                ),
            )
        return True
    except Exception as exc:
        logger.warning("[FlywheelJudgment] 留痕失败 point=%s: %s", point_key, exc)
        return False


def today_usage(point_key: str) -> dict[str, Any]:
    """当日(北京时区自然日)该判断点的 LLM 调用次数与花费。

    查询失败 → available=False。调用方必须按"无法证明没超额"处理,走规则兜底,
    绝不能"查不到就当零花费"接着刷 LLM。
    """
    try:
        init_flywheel_judgment_tables()
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(cost_cny), 0) AS cost_cny,
                       COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
                  FROM flywheel_judgment_log
                 WHERE point_key = %s
                   AND source = %s
                   AND (created_at AT TIME ZONE 'Asia/Shanghai')::date
                       = (NOW() AT TIME ZONE 'Asia/Shanghai')::date
                """,
                (point_key[:60], SOURCE_LLM),
            )
            row = cur.fetchone() or {}
        finally:
            conn.close()
        return {
            "available": True,
            "calls": int(row.get("calls") or 0),
            "cost_cny": float(row.get("cost_cny") or 0),
            "tokens": int(row.get("tokens") or 0),
        }
    except Exception as exc:
        logger.warning("[FlywheelJudgment] 当日用量查询失败 point=%s: %s", point_key, exc)
        return {"available": False, "calls": 0, "cost_cny": 0.0, "tokens": 0}


def list_judgments(point_key: str = "", limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 500))
    try:
        init_flywheel_judgment_tables()
        conn = get_connection()
        try:
            cur = conn.cursor()
            if point_key:
                cur.execute(
                    "SELECT * FROM flywheel_judgment_log WHERE point_key=%s "
                    "ORDER BY created_at DESC, id DESC LIMIT %s",
                    (point_key[:60], limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM flywheel_judgment_log ORDER BY created_at DESC, id DESC LIMIT %s",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall() or []]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[FlywheelJudgment] 判断明细查询失败: %s", exc)
        return []
