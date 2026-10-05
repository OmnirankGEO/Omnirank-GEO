"""
P14 (2026-05-27) · GEO 调研监测后台 · 引用明细 admin API

定位:
  - 老板/管理员审计层 · 不是代理决策视图
  - 数据源 geo_research_raw (query × engine × cited_url 一行一条 · 共享 answer_text)
  - 严格 admin-only · 不返回任何字段给代理/销售页面

3 个 endpoint:
  1. GET  /citations/industries-summary   左列行业列表 (industry / queries / engines / citations / last_at)
  2. GET  /citations/queries              中列 query 聚合 (industry 必填 · 支持 q/engine/batch_id/limit/offset)
  3. GET  /citations/query-detail         右列展开 (industry + query 必 · 按 engine 分组 · 每 engine 最新 5 个 answer)

engine 归一规则 (P14 决策点 1B):
  raw.engine 历史脏数据 8 种 (豆包/doubao/Kimi/kimi/DeepSeek/deepseek/千问/qwen)
  → 归一到 4 个规范名 (豆包/Kimi/DeepSeek/千问) 再聚合
  · 原始 raw_engine 字段保留 · 前端可放 tooltip 排查脏数据 · 但主 UI 用归一名
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from db.connection import get_connection

logger = logging.getLogger("GEO-ResearchMonitor.CitationsAPI")

router = APIRouter(prefix="/api/admin/research-monitor", tags=["调研监测-引用明细"])


# ==================== 鉴权 ====================

def _require_admin(request: Request) -> dict:
    """从 request.state.user 拿 admin 用户; 未登录 401, 非 admin 403"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ==================== engine 归一 (决策点 1B) ====================
# 用一个 SQL CASE 把脏数据映射到规范名 · 复用在所有 DISTINCT/GROUP 子句
# 原始 raw.engine 仍可单独返回供 tooltip 显示
_ENGINE_NORMALIZE_SQL = """
    CASE
        WHEN LOWER(engine) IN ('doubao', '豆包') THEN '豆包'
        WHEN LOWER(engine) IN ('kimi') THEN 'Kimi'
        WHEN LOWER(engine) IN ('deepseek') THEN 'DeepSeek'
        WHEN LOWER(engine) IN ('qwen', '千问') THEN '千问'
        ELSE engine
    END
"""

# 规范引擎全集 (前端写死的 ENGINE_NAMES 镜像)
CANONICAL_ENGINES = ['豆包', 'Kimi', 'DeepSeek', '千问']


def _normalize_engine(raw_engine: Optional[str]) -> str:
    """Python 端归一 · 跟 SQL CASE 等价 · 用于按答案分组聚合时"""
    if not raw_engine:
        return ''
    e = raw_engine.lower()
    if e in ('doubao', '豆包'):
        return '豆包'
    if e == 'kimi':
        return 'Kimi'
    if e == 'deepseek':
        return 'DeepSeek'
    if e in ('qwen', '千问'):
        return '千问'
    return raw_engine


# ==================== 工具 ====================

def _row(cur, idx_or_row) -> Dict:
    """RealDictCursor 已返回 dict · 兼容传入"""
    if idx_or_row is None:
        return {}
    if isinstance(idx_or_row, dict):
        return idx_or_row
    return dict(idx_or_row)


def _answer_md5(answer_text: Optional[str]) -> str:
    if not answer_text:
        return ''
    return hashlib.md5(answer_text.encode('utf-8', errors='replace')).hexdigest()


# ==================== 1. 行业汇总 ====================

@router.get("/citations/industries-summary", summary="引用明细-左列行业汇总")
async def industries_summary(request: Request) -> Dict:
    """
    左列行业列表 · 一行一行业:
      industry / query_count / engine_count (归一后 0~4) / citation_count / last_cited_at
    按 citation_count DESC 排
    """
    _require_admin(request)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT industry,
                   COUNT(DISTINCT query) AS query_count,
                   COUNT(DISTINCT {_ENGINE_NORMALIZE_SQL}) AS engine_count,
                   COUNT(*) AS citation_count,
                   MAX(created_at) AS last_cited_at
              FROM geo_research_raw
             WHERE industry IS NOT NULL AND industry <> ''
             GROUP BY industry
             ORDER BY citation_count DESC, industry ASC
        """)
        rows = cur.fetchall() or []
    except Exception as e:
        logger.exception(f"[citations 行业汇总] 失败: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="查询行业汇总失败")
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()

    industries: List[Dict] = []
    for r in rows:
        d = _row(cur, r)
        industries.append({
            "industry": d.get("industry"),
            "query_count": int(d.get("query_count") or 0),
            "engine_count": int(d.get("engine_count") or 0),
            "citation_count": int(d.get("citation_count") or 0),
            "last_cited_at": d.get("last_cited_at").isoformat() if d.get("last_cited_at") else None,
        })

    return {"industries": industries, "total": len(industries)}


# ==================== 2. query 聚合列表 ====================

@router.get("/citations/queries", summary="引用明细-中列 query 列表")
async def list_queries(
    request: Request,
    industry: str = Query(..., min_length=1, description="行业名(必填)"),
    q: Optional[str] = Query(None, description="按 query 文本模糊搜索"),
    engine: Optional[str] = Query(None, description="按归一 engine 名过滤(豆包/Kimi/DeepSeek/千问)"),
    batch_id: Optional[str] = Query(None, description="按批次 ID 过滤"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Dict:
    """
    指定行业下的 query 列表 · 一行一 (industry,query):
      query / citation_count / engine_count / engines{engine→count}(归一)
    """
    _require_admin(request)

    if engine is not None and engine not in CANONICAL_ENGINES:
        raise HTTPException(
            status_code=400,
            detail=f"engine 必须是 {CANONICAL_ENGINES} 之一(归一后)",
        )

    # 动态 where
    where_clauses = ["industry = %s"]
    params: List[Any] = [industry]
    if q:
        where_clauses.append("query ILIKE %s")
        params.append(f"%{q}%")
    if engine:
        # 归一过滤 · 用 CASE 套同样规则
        where_clauses.append(f"({_ENGINE_NORMALIZE_SQL}) = %s")
        params.append(engine)
    if batch_id:
        where_clauses.append("batch_id = %s")
        params.append(batch_id)
    where_sql = " AND ".join(where_clauses)

    conn = get_connection()
    try:
        cur = conn.cursor()

        # total
        cur.execute(
            f"SELECT COUNT(DISTINCT query) AS c FROM geo_research_raw WHERE {where_sql}",
            params,
        )
        total = int(_row(cur, cur.fetchone()).get("c") or 0)

        # 主表: 每 query 聚合
        cur.execute(
            f"""
            SELECT query,
                   COUNT(*) AS citation_count,
                   COUNT(DISTINCT {_ENGINE_NORMALIZE_SQL}) AS engine_count,
                   MAX(created_at) AS last_cited_at
              FROM geo_research_raw
             WHERE {where_sql}
             GROUP BY query
             ORDER BY citation_count DESC, query ASC
             LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall() or []

        # 拿这一页 queries 的 engine 分布
        page_queries = [_row(cur, r).get("query") for r in rows]
        engine_dist: Dict[str, Dict[str, int]] = {}  # query → {engine_canonical: count}
        if page_queries:
            cur.execute(
                f"""
                SELECT query,
                       {_ENGINE_NORMALIZE_SQL} AS engine_norm,
                       COUNT(*) AS cnt
                  FROM geo_research_raw
                 WHERE {where_sql}
                   AND query = ANY(%s)
                 GROUP BY query, engine_norm
                """,
                params + [list(page_queries)],
            )
            for r in cur.fetchall() or []:
                d = _row(cur, r)
                q_name = d.get("query")
                eng = d.get("engine_norm")
                cnt = int(d.get("cnt") or 0)
                if q_name not in engine_dist:
                    engine_dist[q_name] = {}
                engine_dist[q_name][eng] = cnt

    except Exception as e:
        logger.exception(f"[citations queries] 失败 industry={industry}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="查询 query 列表失败")
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()

    queries: List[Dict] = []
    for r in rows:
        d = _row(cur, r)
        q_name = d.get("query")
        queries.append({
            "query": q_name,
            "citation_count": int(d.get("citation_count") or 0),
            "engine_count": int(d.get("engine_count") or 0),
            "last_cited_at": d.get("last_cited_at").isoformat() if d.get("last_cited_at") else None,
            "engines": engine_dist.get(q_name, {}),  # {豆包: 48, Kimi: 25, ...}
        })

    return {
        "industry": industry,
        "queries": queries,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ==================== 3. query 详情 (按 engine 分组 · 每组 5 个 answer) ====================

_ANSWERS_PER_ENGINE_DEFAULT = 5


@router.get("/citations/query-detail", summary="引用明细-右列 query 详情")
async def query_detail(
    request: Request,
    industry: str = Query(..., min_length=1),
    query: str = Query(..., min_length=1),
    batch_id: Optional[str] = Query(None, description="按批次 ID 过滤"),
    answers_per_engine: int = Query(_ANSWERS_PER_ENGINE_DEFAULT, ge=1, le=50),
    engine: Optional[str] = Query(None, description="只看某 engine(归一名) · 配合加载更多"),
    engine_offset: int = Query(0, ge=0, description="某 engine 下跳过几个 answer · 加载更多用"),
) -> Dict:
    """
    返回结构:
      {
        industry, query,
        engines: [
          { engine: '豆包', total_answers: 12, has_more: true, offset, limit,
            answers: [
              { answer_md5, answer_text, batch_id, raw_engine, created_at,
                cite_count, cites: [{platform, url, title, excerpt, position, raw_id, article_id}] },
              ...
            ],
          }
        ]
      }

    P14 v2 (review fix HIGH+MEDIUM):
      - HIGH: SQL 层做聚合 + 窗口分页 · 不再 Python 端切片整个 query 的 raw 行
        · Step 1: CTE 聚合 → ROW_NUMBER PARTITION BY engine ORDER BY created_at DESC
        · Step 2: 只回查窗口内 answer 的 cites (按 batch+md5 收窄)
      - MEDIUM: cites LEFT JOIN geo_research_article_citations 返回 article_id (nullable)
    """
    _require_admin(request)

    if engine is not None and engine not in CANONICAL_ENGINES:
        raise HTTPException(
            status_code=400,
            detail=f"engine 必须是 {CANONICAL_ENGINES} 之一(归一后)",
        )

    # 公共 where (industry+query [+batch_id])
    where_clauses = ["industry = %s", "query = %s"]
    base_params: List[Any] = [industry, query]
    if batch_id:
        where_clauses.append("batch_id = %s")
        base_params.append(batch_id)
    base_where_sql = " AND ".join(where_clauses)

    # ============== Step 1: SQL 聚合 + 窗口分页 ==============
    # CTE 嵌套:
    #   base    : 给 raw 行打上 engine_norm + answer_md5
    #   answers : 按 (engine_norm, batch_id, answer_md5) 聚合 · 一行一 answer
    #   ranked  : 给每个 engine 内按 created_at DESC 排序 + 行号
    # 最后过滤窗口
    if engine:
        # 只取一个 engine 的 [offset, offset+limit)
        engine_filter_sql = "WHERE engine_norm = %s"
        engine_filter_params = [engine]
        window_filter_sql = "WHERE engine_norm = %s AND rn > %s AND rn <= %s"
        window_filter_params = [engine, engine_offset, engine_offset + answers_per_engine]
    else:
        # 默认: 每个 engine 各取前 limit (不支持 all-engine offset · 加载更多必须指定 engine)
        engine_filter_sql = ""
        engine_filter_params = []
        window_filter_sql = "WHERE rn <= %s"
        window_filter_params = [answers_per_engine]

    answers_sql = f"""
        WITH base AS (
            SELECT {_ENGINE_NORMALIZE_SQL} AS engine_norm,
                   engine AS raw_engine,
                   batch_id,
                   MD5(COALESCE(answer_text, '')) AS answer_md5,
                   answer_text,
                   created_at
              FROM geo_research_raw
             WHERE {base_where_sql}
        ),
        answers AS (
            SELECT engine_norm,
                   batch_id,
                   answer_md5,
                   MIN(answer_text) AS answer_text,
                   MIN(raw_engine)  AS raw_engine,
                   MAX(created_at)  AS created_at
              FROM base
              {engine_filter_sql}
             GROUP BY engine_norm, batch_id, answer_md5
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY engine_norm
                       -- P14 v3 (review LOW fix): NULLS LAST 防 legacy created_at=NULL 排前面
                       -- · 导致 "最新 5 个" 实际把空时间戳挤上来
                       ORDER BY created_at DESC NULLS LAST, answer_md5
                   ) AS rn,
                   COUNT(*) OVER (PARTITION BY engine_norm) AS total_in_engine
              FROM answers
        )
        SELECT engine_norm, batch_id, answer_md5, answer_text, raw_engine,
               created_at, rn, total_in_engine
          FROM ranked
          {window_filter_sql}
         ORDER BY engine_norm, rn
    """

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            answers_sql,
            base_params + engine_filter_params + window_filter_params,
        )
        answer_rows = [dict(r) for r in (cur.fetchall() or [])]

        # ============== Step 2: 回查窗口内 answer 的 cites ==============
        # 用 (batch_id, answer_md5) 集合收窄 · LEFT JOIN article_citations 拿 article_id
        # 同 batch+md5 在不同 engine 下不会重复 (raw 行的 engine 不同)
        if answer_rows:
            batch_ids = list({a["batch_id"] for a in answer_rows if a["batch_id"] is not None})
            md5s = list({a["answer_md5"] for a in answer_rows})
            # batch_id 可能有 NULL · 拆两个 SQL 简单处理
            null_batch_md5s = [a["answer_md5"] for a in answer_rows if a["batch_id"] is None]

            cite_rows: List[Dict] = []

            if batch_ids:
                cur.execute(
                    f"""
                    SELECT raw.id AS raw_id,
                           {_ENGINE_NORMALIZE_SQL} AS engine_norm,
                           raw.batch_id,
                           MD5(COALESCE(raw.answer_text, '')) AS answer_md5,
                           raw.cited_platform, raw.cite_position, raw.cite_url,
                           raw.cite_title, raw.cite_excerpt,
                           ac.article_id
                      FROM geo_research_raw raw
                      LEFT JOIN geo_research_article_citations ac
                             ON ac.raw_id = raw.id
                     WHERE raw.industry = %s AND raw.query = %s
                       AND raw.batch_id = ANY(%s)
                       AND MD5(COALESCE(raw.answer_text, '')) = ANY(%s)
                       AND raw.cite_url IS NOT NULL AND raw.cite_url <> ''
                     ORDER BY raw.cite_position NULLS LAST, raw.id
                    """,
                    [industry, query, batch_ids, md5s],
                )
                cite_rows.extend(dict(r) for r in (cur.fetchall() or []))

            if null_batch_md5s:
                cur.execute(
                    f"""
                    SELECT raw.id AS raw_id,
                           {_ENGINE_NORMALIZE_SQL} AS engine_norm,
                           raw.batch_id,
                           MD5(COALESCE(raw.answer_text, '')) AS answer_md5,
                           raw.cited_platform, raw.cite_position, raw.cite_url,
                           raw.cite_title, raw.cite_excerpt,
                           ac.article_id
                      FROM geo_research_raw raw
                      LEFT JOIN geo_research_article_citations ac
                             ON ac.raw_id = raw.id
                     WHERE raw.industry = %s AND raw.query = %s
                       AND raw.batch_id IS NULL
                       AND MD5(COALESCE(raw.answer_text, '')) = ANY(%s)
                       AND raw.cite_url IS NOT NULL AND raw.cite_url <> ''
                     ORDER BY raw.cite_position NULLS LAST, raw.id
                    """,
                    [industry, query, null_batch_md5s],
                )
                cite_rows.extend(dict(r) for r in (cur.fetchall() or []))
        else:
            cite_rows = []
    except Exception as e:
        logger.exception(
            f"[citations query-detail] 失败 industry={industry} query={query}: "
            f"{type(e).__name__}: {e}"
        )
        raise HTTPException(status_code=500, detail="查询 query 详情失败")
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()

    # ============== 装配输出 ==============
    # 按 (engine_norm, batch_id, answer_md5) 索引 answer · cite 归属到对应 answer
    answer_index: Dict[str, Dict] = {}  # key → answer record
    totals_by_engine: Dict[str, int] = {}
    for a in answer_rows:
        eng = a["engine_norm"]
        key = f"{eng}|{a['batch_id'] or ''}|{a['answer_md5']}"
        answer_index[key] = {
            "answer_md5": a["answer_md5"],
            "answer_text": a["answer_text"] or "",
            "batch_id": a["batch_id"],
            "raw_engine": a["raw_engine"],
            "created_at": a["created_at"].isoformat() if a.get("created_at") else None,
            "cites": [],
        }
        # total_in_engine 在所有窗口内行里都一样 · 直接覆盖
        totals_by_engine[eng] = int(a["total_in_engine"] or 0)

    for c in cite_rows:
        key = f"{c['engine_norm']}|{c['batch_id'] or ''}|{c['answer_md5']}"
        ans = answer_index.get(key)
        if not ans:
            # cite 对应的 answer 不在窗口内 · 跳过 (理论上不应发生因为我们用窗口内 md5 收窄)
            continue
        ans["cites"].append({
            "raw_id": c.get("raw_id"),
            "platform": c.get("cited_platform"),
            "position": c.get("cite_position"),
            "url": c.get("cite_url"),
            "title": c.get("cite_title"),
            "excerpt": c.get("cite_excerpt"),
            "article_id": c.get("article_id"),
        })

    # 按 engine 装组
    out_engines: List[Dict] = []
    target_engines = [engine] if engine else CANONICAL_ENGINES
    for eng_name in target_engines:
        eng_answers: List[Dict] = []
        for a in answer_rows:
            if a["engine_norm"] != eng_name:
                continue
            key = f"{eng_name}|{a['batch_id'] or ''}|{a['answer_md5']}"
            rec = answer_index[key]
            rec["cite_count"] = len(rec["cites"])
            eng_answers.append(rec)

        total = totals_by_engine.get(eng_name, 0)
        # has_more 判定: 窗口下界 + 已取数 < total
        # 默认模式 engine_offset=0 · 直接 total > limit; 指定 engine 模式 · 比较 offset+取到数
        taken = len(eng_answers)
        out_engines.append({
            "engine": eng_name,
            "total_answers": total,
            "answers": eng_answers,
            "has_more": total > engine_offset + taken,
            "offset": engine_offset if engine else 0,
            "limit": answers_per_engine,
        })

    return {
        "industry": industry,
        "query": query,
        "engines": out_engines,
    }
