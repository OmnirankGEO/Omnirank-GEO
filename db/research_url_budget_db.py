"""
Stage 3 URL 质量预算 · 持久化 CRUD (2026-07-16 P1 返工)

耐久断点/幂等批次: 每 round 的 Stage3 选中 URL 预算落 geo_research_url_budget 表,
status 驱动续跑(只处理 pending)。表结构见 scripts/migration_stage3_url_budget_2026_07_16.sql。

所有 SQL 参数化; 单条状态更新用 (round_id, url_hash, industry_name) 复合唯一键。
"""
import logging
from typing import Dict, List, Optional, Any

from db.connection import get_connection

logger = logging.getLogger("GEO-ResearchMonitor.UrlBudget")


def load_existing_article_url_hashes(url_hashes: List[str]) -> set:
    """查这批 url_hash 中哪些已在 geo_research_articles(任意行业)。

    [轮1审核 must_fix] 命中的 URL 在 stage3 走 reused_existing/crawled_dup —— 零 Jina
    抓取、零耗时。预算上限本质是 Jina 吞吐预算, 不该裁掉这些"免费"项(否则其
    citation 永不写入 → 引用覆盖率随 DB 成熟度无谓下降)。分批 IN 查询防超长。
    """
    if not url_hashes:
        return set()
    found: set = set()
    conn = get_connection()
    try:
        cur = conn.cursor()
        CHUNK = 1000
        uniq = list({h for h in url_hashes if h})
        for i in range(0, len(uniq), CHUNK):
            chunk = uniq[i:i + CHUNK]
            cur.execute(
                "SELECT DISTINCT url_hash FROM geo_research_articles WHERE url_hash = ANY(%s)",
                (chunk,),
            )
            for r in (cur.fetchall() or []):
                found.add(r['url_hash'] if isinstance(r, dict) else r[0])
        return found
    finally:
        conn.close()


def count_budget_rows(round_id: str) -> int:
    """本 round 已持久化的预算行数(0 = 尚未算过预算, 需首次计算)。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM geo_research_url_budget WHERE round_id = %s",
            (round_id,),
        )
        row = cur.fetchone()
        return int((row['n'] if isinstance(row, dict) else row[0]) or 0)
    finally:
        conn.close()


def load_batch_raw_rows_for_signals(batch_id: str) -> List[Dict[str, Any]]:
    """拉本 batch 的 raw 全量(仅信号列)供 url_budget 聚合。cite_url 非空。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT cite_url, engine, cite_position, is_answer_cited
              FROM geo_research_raw
             WHERE batch_id = %s
               AND cite_url IS NOT NULL
               AND cite_url <> ''
            """,
            (batch_id,),
        )
        return [dict(r) for r in (cur.fetchall() or [])]
    finally:
        conn.close()


def persist_url_budget(round_id: str, selected: List[Dict[str, Any]]) -> int:
    """批量持久化选中预算集(status='pending')。幂等: ON CONFLICT DO NOTHING —
    续跑重算得到同一集时不覆盖已有处理状态。返回实际新插入行数。

    selected 每项需含: url_hash, normalized_url, url, domain, industry_id,
    industry_name, prompt_id, platform, raw_id, rank_in_response, score, budget_rank。
    """
    if not selected:
        return 0
    conn = get_connection()
    inserted = 0
    try:
        cur = conn.cursor()
        for c in selected:
            cur.execute(
                """
                INSERT INTO geo_research_url_budget
                    (round_id, url_hash, normalized_url, url, domain,
                     industry_id, industry_name, prompt_id, platform,
                     raw_id, rank_in_response, quality_score, budget_rank, status)
                VALUES (%s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, 'pending')
                ON CONFLICT (round_id, url_hash, industry_name) DO NOTHING
                """,
                (
                    round_id,
                    c.get('url_hash'),
                    c.get('normalized_url'),
                    c.get('url') or c.get('normalized_url'),
                    c.get('domain'),
                    c.get('industry_id'),
                    c.get('industry_name') or '',
                    c.get('prompt_id'),
                    c.get('platform'),
                    c.get('raw_id'),
                    c.get('rank_in_response'),
                    float(c.get('score') or 0.0),
                    c.get('budget_rank'),
                ),
            )
            inserted += cur.rowcount
        conn.commit()
        return inserted
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def load_pending_budget(round_id: str) -> List[Dict[str, Any]]:
    """读本 round status='pending' 的预算项(按 budget_rank 升序 = 质量优先)。
    返回 crawl_one 需要的字段结构(与旧 stage2 kept 项对齐 + budget 标识)。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT url_hash, normalized_url, url, domain,
                   industry_id, industry_name, prompt_id, platform,
                   raw_id, rank_in_response, budget_rank
              FROM geo_research_url_budget
             WHERE round_id = %s AND status = 'pending'
             ORDER BY budget_rank ASC NULLS LAST, id ASC
            """,
            (round_id,),
        )
        out = []
        for r in (cur.fetchall() or []):
            d = dict(r)
            out.append({
                'url': d.get('url'),
                'normalized_url': d.get('normalized_url'),
                'url_hash': d.get('url_hash'),
                'domain': d.get('domain'),
                'industry_id': d.get('industry_id'),
                'industry_name': d.get('industry_name'),
                'prompt_id': d.get('prompt_id'),
                'platform': d.get('platform'),
                'raw_id': d.get('raw_id'),
                'rank_in_response': d.get('rank_in_response'),
                'budget_rank': d.get('budget_rank'),
            })
        return out
    finally:
        conn.close()


def mark_budget_status(
    round_id: str,
    url_hash: str,
    industry_name: Optional[str],
    status: str,
    reason: Optional[str] = None,
) -> bool:
    """更新单个预算项处理状态(耐久断点)。attempts 自增。

    终态守护: 已是 'done' 的行不被后续(如迟到 worker / 重复处理)改回非 done —
    WHERE 排除 status='done', 防终态被覆盖(要求6 语义在 URL 粒度的落实)。
    返回是否真更新了行。
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_research_url_budget
               SET status = %s,
                   reason = %s,
                   attempts = attempts + 1,
                   updated_at = NOW()
             WHERE round_id = %s
               AND url_hash = %s
               AND industry_name = COALESCE(%s, '')
               AND status <> 'done'
            """,
            (status, reason, round_id, url_hash, industry_name),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_budget_status_counts(round_id: str) -> Dict[str, int]:
    """本 round 各 status 计数(供 progress/summary 展示 + 断点判断)。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT status, COUNT(*) AS n
              FROM geo_research_url_budget
             WHERE round_id = %s
             GROUP BY status
            """,
            (round_id,),
        )
        return {r['status']: int(r['n']) for r in (cur.fetchall() or [])}
    finally:
        conn.close()
