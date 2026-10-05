"""W3 · 写作模拟对比 shadow 表(原文 vs 修改后)。

`writing_style_simulations` — 一行一次「当前版 vs 候选版」对比:同一题目、同一演示品牌/quote、
同温度/同 max_tokens、结构参考状态两臂一致,仅 system prompt 的基础 prompt 不同(当前 / 候选)。
两篇样文 + 结构参考状态 + 成本口径 + W4 评审结果都存这里。

🔴 shadow 纪律:样文**绝不进 articles 主表、绝不进客户面**。本表纯内部对比看板用。
LLM 调用在服务层(走生产同源 build);本模块只持久化。init 懒加载 per-route。
"""
from __future__ import annotations

from db.schema_guard import add_column_if_missing
import json
from typing import Any

from psycopg2.extras import Json

from db.connection import get_connection, get_db


def _jsonb(value: Any) -> Json:
    return Json(value or {}, dumps=lambda obj: json.dumps(obj, ensure_ascii=False, default=str))


def init_writing_style_simulation_tables() -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS writing_style_simulations (
                id BIGSERIAL PRIMARY KEY,
                style_code VARCHAR(60) NOT NULL,
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                version_id VARCHAR(200),
                topic_title TEXT NOT NULL DEFAULT '',
                demo_quote_id BIGINT,
                demo_brand_name TEXT,
                current_article TEXT NOT NULL DEFAULT '',
                candidate_article TEXT NOT NULL DEFAULT '',
                current_word_count INTEGER NOT NULL DEFAULT 0,
                candidate_word_count INTEGER NOT NULL DEFAULT 0,
                structure_guidance_state VARCHAR(24) NOT NULL DEFAULT 'unknown',
                user_message_identical BOOLEAN,
                cost_note TEXT,
                review_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
                status VARCHAR(20) NOT NULL DEFAULT 'generated',
                error TEXT,
                created_by BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_writing_sim_style "
            "ON writing_style_simulations(style_code, industry_key, created_at DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_writing_sim_version "
            "ON writing_style_simulations(version_id)"
        )
        # [V1] 结构差异高亮徽章:两篇样文各跑 21 布尔特征(BOOLEAN_FEATURES)后的 diff(新增/缺失)。
        # 纯 additive 列 —— CREATE TABLE IF NOT EXISTS 对已存在生产库不加列,必须 ALTER(踩过 SQL 4 维核验红线)。
        # 禁止复用 review_summary(会被 update_simulation_review 整块覆盖 clobber)。
        add_column_if_missing(cur, "writing_style_simulations", "structure_diff",
                              "JSONB NOT NULL DEFAULT '{}'::jsonb")  # [WO_285b] 列缺失才 ALTER(请求路径可达)


def insert_simulation(
    *,
    style_code: str,
    industry_key: str,
    version_id: str | None,
    topic_title: str,
    demo_quote_id: int | None,
    demo_brand_name: str,
    current_article: str,
    candidate_article: str,
    structure_guidance_state: str,
    user_message_identical: bool | None,
    cost_note: str = "",
    status: str = "generated",
    error: str = "",
    created_by: int = 0,
    structure_diff: dict[str, Any] | None = None,
) -> int:
    init_writing_style_simulation_tables()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO writing_style_simulations
                (style_code, industry_key, version_id, topic_title, demo_quote_id, demo_brand_name,
                 current_article, candidate_article, current_word_count, candidate_word_count,
                 structure_guidance_state, user_message_identical, cost_note, status, error, created_by,
                 structure_diff)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                style_code, industry_key, version_id, topic_title[:500], demo_quote_id,
                (demo_brand_name or "")[:200], current_article or "", candidate_article or "",
                len(current_article or ""), len(candidate_article or ""),
                structure_guidance_state, user_message_identical, (cost_note or "")[:500],
                status, (error or "")[:1000], int(created_by or 0),
                _jsonb(structure_diff),
            ),
        )
        return int(cur.fetchone()["id"])


def update_simulation_review(simulation_id: int, review_summary: dict[str, Any]) -> bool:
    """W4 评审回填。只改 review_summary + status='reviewed',不动样文。"""
    init_writing_style_simulation_tables()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE writing_style_simulations SET review_summary=%s, status='reviewed' WHERE id=%s",
            (_jsonb(review_summary), int(simulation_id)),
        )
        return cur.rowcount > 0


def get_simulation(simulation_id: int) -> dict[str, Any] | None:
    init_writing_style_simulation_tables()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM writing_style_simulations WHERE id=%s", (int(simulation_id),))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_simulations(
    *,
    style_code: str | None = None,
    industry_key: str | None = None,
    version_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """列出对比(不含大正文字段,列表轻量)。version_id 过滤用于按精确候选版本取评审(防张冠李戴)。"""
    init_writing_style_simulation_tables()
    conn = get_connection()
    try:
        cur = conn.cursor()
        where = ["1=1"]
        params: list[Any] = []
        if style_code:
            where.append("style_code=%s")
            params.append(style_code)
        if industry_key:
            where.append("industry_key=%s")
            params.append(industry_key)
        if version_id:
            where.append("version_id=%s")
            params.append(version_id)
        params.append(max(1, min(int(limit or 20), 100)))
        cur.execute(
            f"""
            SELECT id, style_code, industry_key, version_id, topic_title, demo_brand_name,
                   current_word_count, candidate_word_count, structure_guidance_state,
                   user_message_identical, status, review_summary, structure_diff, created_at
            FROM writing_style_simulations
            WHERE {' AND '.join(where)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _normalize_autopick_industry(industry_key: str) -> str:
    """[review fix] general/all 族当「全行业」处理,归一为 ''(无行业过滤)。

    否则 general 会被当字面量 `ILIKE '%general%'` 匹配 —— 中文行业值永不命中,
    页面默认 scope(general)下自动挑 quote 恒空,看板点「生成对比样文」静默死亡。
    与 article_structure_analysis._load_rows 的 general 口径对齐。"""
    key = (industry_key or "").strip()
    if key.lower() in {"general", "all", "all_articles"}:
        return ""
    return key


def get_demo_quote(industry_key: str, demo_quote_id: int | None = None) -> dict[str, Any] | None:
    """取一个用于模拟的真实演示 quote(有 distilled_data 优先,避免 DistillerPipeline 缓存未命中触发额外 LLM)。

    指定 demo_quote_id → 直接取;否则在该行业挑一个有 distilled_data 的最新 quote。
    """
    industry_key = _normalize_autopick_industry(industry_key)
    conn = get_connection()
    try:
        cur = conn.cursor()
        if demo_quote_id:
            cur.execute(
                "SELECT id AS quote_id, brand_name, industry FROM quotes WHERE id=%s",
                (int(demo_quote_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        # 自动挑:该行业 · 有 distilled_data · 最新
        cur.execute(
            """
            SELECT id AS quote_id, brand_name, industry
            FROM quotes
            WHERE COALESCE(industry,'') <> ''
              AND (%s = '' OR industry ILIKE %s)
              AND distilled_data IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (industry_key or "", f"%{industry_key}%" if industry_key else "%"),
        )
        row = cur.fetchone()
        if row:
            return dict(row)
        # 回退:该行业任意最新 quote(可能触发一次 distill 成本,已在成本口径说明)
        cur.execute(
            """
            SELECT id AS quote_id, brand_name, industry
            FROM quotes
            WHERE COALESCE(industry,'') <> '' AND (%s = '' OR industry ILIKE %s)
            ORDER BY id DESC LIMIT 1
            """,
            (industry_key or "", f"%{industry_key}%" if industry_key else "%"),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
