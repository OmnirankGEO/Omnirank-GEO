"""W5 · 语料导出审计 + 日限(一键获取被采纳范文集的安全边界)。

`writing_corpus_export_audit` — 一行一次导出:operator / 筛选条件 / 篇数 / 时间。用于:
  1. 每次导出写审计(合规追溯 · 谁在什么条件下导出了多少篇)
  2. 单日导出上限(env WRITING_CORPUS_EXPORT_DAILY_CAP,默认 500 篇)防批量抓取

语料为公开抓取正文(无客户数据)。shadow/审计-only,不调 LLM。
"""
from __future__ import annotations

import json
import os
from typing import Any

from psycopg2.extras import Json

from db.connection import get_connection, get_db

DAILY_EXPORT_CAP = max(1, int(os.getenv("WRITING_CORPUS_EXPORT_DAILY_CAP", "500")))


def _jsonb(value: Any) -> Json:
    return Json(value or {}, dumps=lambda obj: json.dumps(obj, ensure_ascii=False, default=str))


def init_writing_corpus_export_tables() -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS writing_corpus_export_audit (
                id BIGSERIAL PRIMARY KEY,
                operator_id BIGINT NOT NULL DEFAULT 0,
                filters JSONB NOT NULL DEFAULT '{}'::jsonb,
                exported_count INTEGER NOT NULL DEFAULT 0,
                exported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_corpus_export_audit_day "
            "ON writing_corpus_export_audit(exported_at)"
        )


def get_today_export_count() -> int:
    """今日(自然日 · CST 近似用 NOW()::date)已导出总篇数,用于日限判断。"""
    init_writing_corpus_export_tables()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(exported_count),0) AS c FROM writing_corpus_export_audit "
            "WHERE exported_at::date = NOW()::date"
        )
        return int((cur.fetchone() or {}).get("c") or 0)
    finally:
        conn.close()


def record_export(operator_id: int, filters: dict[str, Any], exported_count: int) -> None:
    init_writing_corpus_export_tables()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO writing_corpus_export_audit (operator_id, filters, exported_count) VALUES (%s,%s,%s)",
            (int(operator_id or 0), _jsonb(filters), int(exported_count or 0)),
        )
