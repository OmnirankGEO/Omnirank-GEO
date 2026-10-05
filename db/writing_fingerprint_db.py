"""W6 · 文章级写作指纹(真实回环归因地基)。

`writing_article_version_fingerprints` — 一行一篇文章,记录它由哪套 prompt/文体/结构参考生成:
  article_id / style_code / style_version_id / prompt_sha256 / structure_guidance_applied /
  strategy_id / strategy_version / industry_key / created_at,UNIQUE(article_id)。

为什么新建而非改 `writing_strategy_assignments`:后者只有 strategy/brand/quote 级(无 article_id /
style_version_id / prompt_sha),扛不住文章级归因;且改它会动其 quote 级语义。本表是纯 additive shadow,
文章生成完成时 fail-soft 写入(绝不阻断写作)。

SQL 4 维核验(建表):
  1. 列名:article_id/style_code/style_version_id/prompt_sha256/structure_guidance_applied/
     strategy_id/strategy_version/industry_key/created_at 均在场
  2. data_type:article_id BIGINT · prompt_sha256 CHAR(64) · structure_guidance_applied BOOLEAN ·
     strategy_id BIGINT NULL · created_at TIMESTAMPTZ
  3. 字段归属:全在本新表,不碰 writing_strategy_assignments
  4. dry-run:CREATE TABLE IF NOT EXISTS + UNIQUE(article_id) 幂等,无破坏性 SQL
"""
from __future__ import annotations

from typing import Any

from db.connection import get_connection, get_db


def init_writing_fingerprint_tables() -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS writing_article_version_fingerprints (
                id BIGSERIAL PRIMARY KEY,
                article_id BIGINT NOT NULL,
                style_code VARCHAR(60),
                style_family VARCHAR(60),
                style_version_id VARCHAR(200),
                prompt_sha256 CHAR(64),
                structure_guidance_applied BOOLEAN NOT NULL DEFAULT FALSE,
                strategy_id BIGINT,
                strategy_version VARCHAR(240),
                industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_writing_fingerprint_article UNIQUE (article_id)
            )
            """
        )
        # [WP9-P0-3] 发布快照落文体家族(既有表幂等补列)。
        cur.execute(
            "ALTER TABLE writing_article_version_fingerprints "
            "ADD COLUMN IF NOT EXISTS style_family VARCHAR(60)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_writing_fingerprint_sha "
            "ON writing_article_version_fingerprints(prompt_sha256)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_writing_fingerprint_style "
            "ON writing_article_version_fingerprints(style_code, industry_key, created_at)"
        )


def record_article_fingerprint(
    *,
    article_id: int,
    style_code: str | None,
    prompt_sha256: str | None,
    structure_guidance_applied: bool = False,
    industry_key: str = "general",
    style_version_id: str | None = None,
    strategy_id: int | None = None,
    strategy_version: str | None = None,
    style_family: str | None = None,
) -> bool:
    """文章生成完成时写指纹。幂等(同 article_id 覆盖)。调用方须 fail-soft 包裹,绝不阻断写作。

    [WP9-P0-3] 发布快照落文体家族:style_family 未显式传时由 style_code 派生
    (family_for_style),便于效果闭环按家族聚合。
    """
    if not article_id:
        return False
    if not style_family and style_code:
        try:
            from writing.article_style_contract import family_for_style
            style_family = family_for_style(style_code)
        except Exception:
            style_family = None
    init_writing_fingerprint_tables()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO writing_article_version_fingerprints
                (article_id, style_code, style_family, style_version_id, prompt_sha256,
                 structure_guidance_applied, strategy_id, strategy_version, industry_key)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (article_id) DO UPDATE SET
                style_code = EXCLUDED.style_code,
                style_family = EXCLUDED.style_family,
                style_version_id = EXCLUDED.style_version_id,
                prompt_sha256 = EXCLUDED.prompt_sha256,
                structure_guidance_applied = EXCLUDED.structure_guidance_applied,
                strategy_id = EXCLUDED.strategy_id,
                strategy_version = EXCLUDED.strategy_version,
                industry_key = EXCLUDED.industry_key
            """,
            (
                int(article_id), style_code, style_family, style_version_id,
                (prompt_sha256 or None), bool(structure_guidance_applied),
                strategy_id, strategy_version, industry_key or "general",
            ),
        )
    return True


def get_fingerprint(article_id: int) -> dict[str, Any] | None:
    init_writing_fingerprint_tables()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM writing_article_version_fingerprints WHERE article_id=%s",
            (int(article_id),),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_articles_by_prompt_sha(prompt_sha256: str, *, since_days: int | None = None) -> list[int]:
    """某 prompt 版本产出的所有 article_id(按 prompt_sha256 归因)。since_days 限最近 N 天。"""
    init_writing_fingerprint_tables()
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = [prompt_sha256]
        where = ["prompt_sha256=%s"]
        if since_days:
            where.append("created_at >= NOW() - (%s || ' days')::interval")
            params.append(int(since_days))
        cur.execute(
            f"SELECT article_id FROM writing_article_version_fingerprints WHERE {' AND '.join(where)}",
            tuple(params),
        )
        return [int(r["article_id"]) for r in cur.fetchall()]
    finally:
        conn.close()


def list_articles_by_style_version_id(
    version_id: str, *, since_days: int | None = None, until_iso: str | None = None
) -> list[int]:
    """某文体版本(style_version_id)产出的所有 article_id —— W6 归因的**稳定 join 键**。

    🔴 不用 prompt_sha256 归因:文章指纹的 prompt_sha256 = sha256(完全渲染后 system_prompt)
    (含每篇随机 dynamic_scores + 注入日期块 + 白标),与版本面 prompt_sha256=sha256(原始模板)
    口径不同,永不匹配。style_version_id 不随渲染漂移,是唯一稳定归因键。

    until_iso(可选):窗口锚点。传入时窗口 = [until - since_days, until),供旧版 cohort 用
    「新版上线时刻往前推」取样——旧版在新版上线后已停产,用「最近 N 天」窗口对它结构性为空。
    """
    if not version_id:
        return []
    init_writing_fingerprint_tables()
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = [version_id]
        where = ["style_version_id=%s"]
        if until_iso:
            if since_days:
                where.append("created_at >= %s::timestamptz - (%s || ' days')::interval")
                params.extend([str(until_iso), int(since_days)])
            where.append("created_at < %s::timestamptz")
            params.append(str(until_iso))
        elif since_days:
            where.append("created_at >= NOW() - (%s || ' days')::interval")
            params.append(int(since_days))
        cur.execute(
            f"SELECT article_id FROM writing_article_version_fingerprints WHERE {' AND '.join(where)}",
            tuple(params),
        )
        return [int(r["article_id"]) for r in cur.fetchall()]
    finally:
        conn.close()


def get_fingerprint_coverage(industry_key: str | None = None) -> dict[str, Any]:
    """指纹覆盖:已写指纹的文章数 / articles 总数(看板健康用)。"""
    init_writing_fingerprint_tables()
    conn = get_connection()
    try:
        cur = conn.cursor()
        if industry_key:
            cur.execute(
                "SELECT COUNT(*) AS c FROM writing_article_version_fingerprints WHERE industry_key=%s",
                (industry_key,),
            )
        else:
            cur.execute("SELECT COUNT(*) AS c FROM writing_article_version_fingerprints")
        fingerprinted = int((cur.fetchone() or {}).get("c") or 0)
        return {"fingerprinted_articles": fingerprinted, "industry_key": industry_key or "all"}
    finally:
        conn.close()
