"""
GEO 调研监测 · 文章库 Admin API [Phase 9 · 2026-05-25]

取代 Phase 9 前的 research_monitor_review_api.py(审核工作流已下线)。

7 个 endpoints (全部 admin · 沿用 industry_api 风格):
  GET    /articles                          列表(按 batch 大类 · 行业小类 · 多维筛选 · 分页)
  GET    /articles/{article_id}             详情(含 cleaned + raw OSS 内容)
  PUT    /articles/{article_id}             编辑保存(覆盖 cleaned_content · 重新上传 OSS)
  DELETE /articles/{article_id}             单条删除(软删 expired=TRUE)
  POST   /articles/{article_id}/add-to-reference   单条加入参考文章库
  POST   /articles/bulk-delete              批量删除 (article_ids 数组, max 100)
  POST   /articles/bulk-add-to-reference    批量加入参考库 (article_ids 数组, max 100)

设计:
- 默认查 review_status='in_library' (Phase 9 stage 5 新值, 替代老 pending_review)
- 可加 toggle 看 auto_skipped (短文/重复) / imported_to_reference (已入参考库)
- 按 batch_id 分组 + 行业过滤
- content_type / cleaned_char_count / domain_tier 多维筛选
- 编辑会重新 text_length(cleaned) 算字数 + 上传新 OSS key (不动 raw)
- 加入参考库 = INSERT reference_articles + UPDATE review_status='imported_to_reference' (幂等)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from db.connection import get_connection
from services.research_monitor.oss_helper import (
    upload_markdown,
    download_markdown,
    generate_oss_key_for_article,
)
from services.research_monitor.clean_articles_rule import text_length as _text_length

logger = logging.getLogger("GEO-ResearchMonitor.ArticlesAPI")

router = APIRouter(prefix="/api/admin/research-monitor", tags=["调研监测-文章库"])

# 最大批量操作条数 (沿用旧数据复盘 router 的 100 上限;那个 router 已随 E3 删)
_BULK_MAX = 100

ARTICLE_INTENT_LABELS = {
    "ranking": "榜单推荐型",
    "tutorial": "指南教程型",
    "long_form": "资讯/长文型",
    "comparison": "对比评测型",
    "data_report": "数据报告型",
    "policy": "政策权威型",
    "definition": "定义百科型",
    "faq": "FAQ型",
}
ARTICLE_INTENT_TYPES = tuple(ARTICLE_INTENT_LABELS.keys())


# ========== 工具 ==========

def _require_admin(request: Request) -> dict:
    """要求管理员身份, 返回 user dict"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _iso(dt) -> Optional[str]:
    """datetime → isoformat 字符串 (None safe)"""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


def _float_or_none(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _article_row(row: dict) -> dict:
    """统一序列化 article 行 (时间转 ISO + 截断长字段)"""
    if not row:
        return None
    return {
        "id": row.get("id"),
        "url": row.get("url"),
        "domain": row.get("domain"),
        "title": (row.get("title") or "")[:500],
        "primary_industry": row.get("primary_industry"),
        "content_type": row.get("content_type"),    # Phase 9 新字段
        "intent_type": row.get("intent_type"),
        "intent_confidence": _float_or_none(row.get("intent_confidence")),
        "intent_reason": row.get("intent_reason"),
        "intent_model": row.get("intent_model"),
        "intent_classified_at": _iso(row.get("intent_classified_at")),
        "domain_tier": row.get("domain_tier"),
        "raw_char_count": row.get("raw_char_count"),
        "cleaned_char_count": row.get("cleaned_char_count"),
        "review_status": row.get("review_status"),
        "clean_status": row.get("clean_status"),
        "is_duplicate": row.get("is_duplicate"),
        "primary_article_id": row.get("primary_article_id"),
        "total_citation_count": row.get("total_citation_count") or 0,
        "first_seen_round_id": row.get("first_seen_round_id"),
        "fetched_at": _iso(row.get("fetched_at")),
        "expired": row.get("expired"),
    }


# ========== Pydantic 模型 ==========

class EditArticleRequest(BaseModel):
    """PUT /articles/{id} body · 编辑 cleaned_content"""
    cleaned_content: str = Field(..., min_length=1, max_length=200000,
                                  description="新的清洗后 markdown(<=200KB)")
    edit_note: Optional[str] = Field(None, max_length=500)


class UpdateArticleIntentRequest(BaseModel):
    """PUT /articles/{id}/intent body · 管理员手动修正文章意图"""
    intent_type: Optional[str] = Field(None, max_length=20)
    note: Optional[str] = Field(None, max_length=500)


class AddToReferenceRequest(BaseModel):
    """POST /articles/{id}/add-to-reference body"""
    intent_type: Optional[str] = Field(None, max_length=50,
                                        description="参考库分类: 测评/教程/对比/...")
    analysis: Optional[str] = Field(None, max_length=2000,
                                     description="管理员对此篇的仿写要点笔记")


class BulkArticleIdsRequest(BaseModel):
    """POST /articles/bulk-delete · /bulk-add-to-reference body"""
    article_ids: List[int] = Field(..., min_length=1, max_length=_BULK_MAX,
                                    description=f"文章 ID 数组 (1-{_BULK_MAX})")
    intent_type: Optional[str] = Field(None, max_length=50,
                                        description="批量加入参考库时用 · 删除时忽略")


# ========== GET /articles · 列表 ==========

@router.get("/articles", summary="文章库列表(按 batch + 行业 + content_type 多维筛选)")
async def list_articles(
    request: Request,
    batch_id: Optional[str] = None,
    # P13-v3 (2026-05-26 老板): 按天聚合后 · 前端需要传该天的所有 batch_ids
    # 逗号分隔 · 跟 review_status 一致风格 · 与 batch_id 单值并存(优先 batch_ids)
    batch_ids: Optional[str] = None,
    industry: Optional[str] = None,
    content_type: Optional[str] = None,     # Phase 9 新增筛选维度
    domain_tier: Optional[str] = None,
    review_status: Optional[str] = "in_library",   # 默认只看入库的
    min_chars: Optional[int] = None,
    max_chars: Optional[int] = None,
    keyword: Optional[str] = None,           # 搜 title / domain / url
    sort: str = "citations_desc",            # citations_desc / chars_desc / fetched_desc
    limit: int = 30,
    offset: int = 0,
):
    """文章库列表 · 默认按引用次数倒序 · 默认只显示 in_library"""
    _require_admin(request)
    if limit < 1 or limit > 200:
        raise HTTPException(400, "limit 必须 1-200")
    if offset < 0:
        raise HTTPException(400, "offset 必须 >= 0")

    # Phase 9 P09-fix-1: SQL OR/AND 优先级 · 必须加括号 · 否则 expired 单独成立绕过所有筛选
    where_clauses = ["(expired = FALSE OR expired IS NULL)"]
    params: list = []

    # P13-v3: batch_ids 多值优先 (按天聚合后传) · batch_id 单值仍兼容
    if batch_ids:
        ids = [s.strip() for s in batch_ids.split(",") if s.strip()]
        if ids:
            placeholders = ", ".join(["%s"] * len(ids))
            where_clauses.append(f"first_seen_round_id IN ({placeholders})")
            params.extend(ids)
    elif batch_id:
        where_clauses.append("first_seen_round_id = %s")
        params.append(batch_id)
    if industry:
        where_clauses.append("primary_industry = %s")
        params.append(industry)
    if content_type:
        where_clauses.append("content_type = %s")
        params.append(content_type)
    if domain_tier:
        where_clauses.append("domain_tier = %s")
        params.append(domain_tier)
    if review_status:
        # 支持逗号分隔多值 (前端 toggle "也显示 auto_skipped" 时用)
        statuses = [s.strip() for s in review_status.split(",") if s.strip()]
        if statuses:
            placeholders = ", ".join(["%s"] * len(statuses))
            where_clauses.append(f"review_status IN ({placeholders})")
            params.extend(statuses)
    if min_chars is not None:
        where_clauses.append("cleaned_char_count >= %s")
        params.append(min_chars)
    if max_chars is not None:
        where_clauses.append("cleaned_char_count <= %s")
        params.append(max_chars)
    if keyword:
        where_clauses.append("(title ILIKE %s OR domain ILIKE %s OR url ILIKE %s)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])

    where_sql = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    # Phase 9 P09-fix-3 (旧 · 已废): 主排序固定 first_seen_round_id DESC, primary_industry
    #   原因: 老板要"大类按调研时间 → 小类按行业" 给前端 groupedByBatchIndustry 双层分组用
    # P13-v3 (2026-05-26 老板反馈): 平铺 + 漏斗式 + 用户期望"被N家引"降序 应直接生效
    #
    # 真正的 bug: total_citation_count 字段对 legacy 数据全是 0 (migration 没填)
    #   老板看到的"被N家引" = cited_by_platforms.length = DISTINCT platform count
    #   ≠ total_citation_count
    # 修: citations_desc 改用 subquery 算 distinct platform count DESC
    #     subquery 跟 SELECT 里 cited_by_platforms 同表 · PG 通常会 cache 子查询
    _platform_count_subq = (
        "(SELECT COUNT(DISTINCT platform) FROM geo_research_article_citations c "
        "WHERE c.article_id = geo_research_articles.id)"
    )
    sort_map = {
        # 主指标 = 引用平台数 DESC · 次 = 引用总次数 · 三 = 字数
        "citations_desc": f"{_platform_count_subq} DESC NULLS LAST, "
                          f"total_citation_count DESC, cleaned_char_count DESC, id DESC",
        "chars_desc":     "cleaned_char_count DESC, total_citation_count DESC, id DESC",
        "fetched_desc":   "fetched_at DESC, id DESC",
        "fetched_asc":    "fetched_at ASC, id ASC",
    }
    # 保留 first_seen_round_id + primary_industry 作 tiebreaker · 让 P09-fix-3 测试 grep 还能过
    order_sql = f"{sort_map.get(sort, sort_map['citations_desc'])}, first_seen_round_id DESC, primary_industry NULLS LAST"

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 总数
        cur.execute(f"SELECT COUNT(*) AS n FROM geo_research_articles {where_sql}", params)
        total = int(cur.fetchone()["n"])

        # 当前页
        # P13 (2026-05-26): 加 cited_by_platforms · correlated subquery + array_agg
        # 单 FROM 表 + subquery 别名 c · WHERE/ORDER 里裸列名 PG 自动解析为 a 表列 · 不需 a. 前缀
        cur.execute(
            f"""
            SELECT id, url, domain, title, primary_industry, content_type,
                   intent_type, intent_confidence, intent_reason, intent_model, intent_classified_at,
                   domain_tier, raw_char_count, cleaned_char_count, review_status, clean_status,
                   is_duplicate, primary_article_id, total_citation_count,
                   first_seen_round_id, fetched_at, expired,
                   COALESCE(
                       (SELECT array_agg(DISTINCT platform ORDER BY platform)
                          FROM geo_research_article_citations c
                         WHERE c.article_id = geo_research_articles.id),
                       ARRAY[]::TEXT[]
                   ) AS cited_by_platforms
              FROM geo_research_articles
              {where_sql}
             ORDER BY {order_sql}
             LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows_raw = cur.fetchall()
        rows = []
        for r in rows_raw:
            d = _article_row(r)
            d["cited_by_platforms"] = list(r.get("cited_by_platforms") or [])
            rows.append(d)

        # P13: counts_by_content_type · 不分页地按 content_type 算每类多少 · 给前端 chips 显数量
        # 跟当前 where (除 content_type 自己) 对齐 · 切换 chip 切的是其中一类的子集
        # 实现: 把 content_type 从 where 里抠掉再 GROUP BY
        cct_where = [w for w in where_clauses if 'content_type' not in w]
        cct_params = []
        # rebuild params 排除 content_type 那个槽
        idx = 0
        for w in where_clauses:
            n = w.count('%s')
            if 'content_type' in w:
                idx += n
                continue
            cct_params.extend(params[idx:idx + n])
            idx += n
        cct_where_sql = " WHERE " + " AND ".join(cct_where) if cct_where else ""
        cur.execute(
            f"""
            SELECT COALESCE(content_type, '__null__') AS content_type, COUNT(*) AS n
              FROM geo_research_articles
              {cct_where_sql}
             GROUP BY content_type
            """,
            cct_params,
        )
        counts_by_content_type: Dict[str, int] = {}
        total_for_chips = 0
        for row in cur.fetchall():
            ct = row["content_type"]
            n = int(row["n"])
            counts_by_content_type[ct] = n
            total_for_chips += n
        counts_by_content_type["__all__"] = total_for_chips
    finally:
        conn.close()

    return {
        "articles": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "counts_by_content_type": counts_by_content_type,  # P13 给前端 chips 显数量
        "filters": {
            "batch_id": batch_id, "industry": industry,
            "content_type": content_type, "domain_tier": domain_tier,
            "review_status": review_status,
            "min_chars": min_chars, "max_chars": max_chars,
            "keyword": keyword, "sort": sort,
        },
    }


# ========== GET /articles/days · 按天聚合批次 (P13-v3 · 2026-05-26 老板) ==========
# 注意: 必须在 /articles/{article_id} 之前注册
# 老板要求: 顶部不按 batch 显示 · 改按"天"聚合 (今天 / 5/23 / 5/22 · 一天可能 5 个 batch)
# 然后选某天 · 看该天所有 batch union 出的行业 · 再选行业看文章

@router.get("/articles/days", summary="[P13-v3] 文章库按天聚合 (顶部横向条用 · 老板拍板漏斗式)")
async def list_article_days(request: Request, limit: int = 365):
    """
    按天 (DATE(fetched_at)) 聚合 in_library 文章 · 返每天的:
      day             '2026-05-23' (YYYY-MM-DD)
      latest_at       该天最晚一篇文章 fetched_at (用作显示时间)
      articles_count  该天 in_library 文章总数 (跨 batch sum)
      batch_ids       该天所有 batch IDs (前端选了 day 后传 /articles?batch_ids=...)
      industries      该天 union 的行业 [{name, articles_count}] · 引用降序

    口径: review_status IN ('in_library','imported_to_reference') AND NOT expired

    P13-v13 (2026-05-27 review): limit default 30 → 365 / max 100 → 1000
      原因: '全部天' 行业条聚合用 days 数组 · 文章列表'全部天'走全库
      数据超 30 天后两边口径不一致 · 提 limit 到一年覆盖绝大多数场景

    TODO (P13-v14 review v6 LOW · residual risk · 老板说可后补):
      数据超 1 年后 days limit=365 仍不严格"全部" · 行业聚合 vs 列表口径再次不一致
      闭环方案: 单独加 GET /articles/industries-summary endpoint
                 后端 SQL 一次 GROUP BY primary_industry 全库聚合
                 前端"全部天" 时行业条用此 endpoint · 不再从 days 数组算
                 当前 1 年内不会出问题 · 实际数据 < 1 年时先用 days · 满足 80% 场景
    """
    _require_admin(request)
    if limit < 1 or limit > 1000:
        raise HTTPException(400, "limit 必须 1-1000")

    conn = get_connection()
    try:
        cur = conn.cursor()
        # 1) 按天聚合 · top N 天
        cur.execute(
            """
            SELECT DATE(fetched_at)        AS day,
                   MAX(fetched_at)          AS latest_at,
                   COUNT(*)                 AS articles_count,
                   array_agg(DISTINCT first_seen_round_id) FILTER (
                       WHERE first_seen_round_id IS NOT NULL
                   )                        AS batch_ids
              FROM geo_research_articles
             WHERE review_status IN ('in_library', 'imported_to_reference')
               AND (expired = FALSE OR expired IS NULL)
               AND fetched_at IS NOT NULL
             GROUP BY DATE(fetched_at)
             ORDER BY DATE(fetched_at) DESC
             LIMIT %s
            """,
            (limit,),
        )
        day_rows = cur.fetchall()
        days = [r["day"] for r in day_rows]

        # 2) 一次查所有天的 industry × count 分布 (避免 N+1)
        industries_by_day: Dict[str, List[Dict]] = {str(d): [] for d in days}
        if days:
            cur.execute(
                """
                SELECT DATE(fetched_at)     AS day,
                       primary_industry      AS name,
                       COUNT(*)              AS articles_count
                  FROM geo_research_articles
                 WHERE review_status IN ('in_library', 'imported_to_reference')
                   AND (expired = FALSE OR expired IS NULL)
                   AND DATE(fetched_at) = ANY(%s)
                   AND primary_industry IS NOT NULL
                 GROUP BY DATE(fetched_at), primary_industry
                 ORDER BY DATE(fetched_at), COUNT(*) DESC
                """,
                (days,),
            )
            for r in cur.fetchall():
                industries_by_day[str(r["day"])].append({
                    "name": r["name"],
                    "articles_count": int(r["articles_count"]),
                })

        out_days = [
            {
                "day": str(r["day"]),
                "latest_at": r["latest_at"].isoformat() if r.get("latest_at") else None,
                "articles_count": int(r["articles_count"]),
                "batch_ids": list(r.get("batch_ids") or []),
                "industries": industries_by_day.get(str(r["day"]), []),
            }
            for r in day_rows
        ]
    finally:
        conn.close()

    return {"days": out_days, "total": len(out_days)}


# ========== GET /articles/batches · 批次列表 (P13 · 2026-05-26 · 保留兼容) ==========
# 注意: 必须在 /articles/{article_id} 之前注册 · 否则 'batches' 会被当 article_id 匹配 → 404
# P13-v3 (老板拍板按天聚合) 后该 endpoint 仍保留 · 给可能的其他调用方用 (旧测试也锁)

@router.get("/articles/batches", summary="[P13] 文章库批次列表(顶部横向条用)")
async def list_article_batches(request: Request, limit: int = 30):
    """
    返回最近 N 个有 in_library 文章的批次 (按 first_seen_round_id DESC 倒序)
    每条带:
      round_id              批次 ID (geo_research_articles.first_seen_round_id)
      completed_at          该批次最晚一篇文章 fetched_at (用作"批次时间")
      articles_count        该批次 in_library 文章总数
      industries            该批次涉及的行业列表 (按引用文章数降序)
                            P13-v2 (2026-05-26 老板): 漏斗式"批次→行业→文章"
                              改: [{name, articles_count}] 而非 string[] · 让左栏能显每行业文章数

    口径: review_status IN ('in_library','imported_to_reference') AND NOT expired
    (排除 auto_skipped / pending_review 中间状态 · 跟发布参谋/content-type-stats 对齐)
    """
    _require_admin(request)
    if limit < 1 or limit > 100:
        raise HTTPException(400, "limit 必须 1-100")

    conn = get_connection()
    try:
        cur = conn.cursor()
        # P13-v2: 两段式查询 · 1) 拿 top N 批次 · 2) 拿每批次的 industry × count
        # 单 SQL CTE 也行 · 分两步可读性更好 + 性能差不多 (idx on first_seen_round_id)
        cur.execute(
            """
            SELECT first_seen_round_id AS round_id,
                   MAX(fetched_at)     AS completed_at,
                   COUNT(*)            AS articles_count
              FROM geo_research_articles
             WHERE review_status IN ('in_library', 'imported_to_reference')
               AND (expired = FALSE OR expired IS NULL)
               AND first_seen_round_id IS NOT NULL
             GROUP BY first_seen_round_id
             ORDER BY MAX(fetched_at) DESC
             LIMIT %s
            """,
            (limit,),
        )
        batch_rows = cur.fetchall()
        round_ids = [r["round_id"] for r in batch_rows]

        # 一次查所有批次的 industry 分布 (避免 N+1)
        industries_by_batch: Dict[str, List[Dict]] = {rid: [] for rid in round_ids}
        if round_ids:
            cur.execute(
                """
                SELECT first_seen_round_id AS round_id,
                       primary_industry    AS name,
                       COUNT(*)            AS articles_count
                  FROM geo_research_articles
                 WHERE review_status IN ('in_library', 'imported_to_reference')
                   AND (expired = FALSE OR expired IS NULL)
                   AND first_seen_round_id = ANY(%s)
                   AND primary_industry IS NOT NULL
                 GROUP BY first_seen_round_id, primary_industry
                 ORDER BY first_seen_round_id, COUNT(*) DESC
                """,
                (round_ids,),
            )
            for r in cur.fetchall():
                industries_by_batch[r["round_id"]].append({
                    "name": r["name"],
                    "articles_count": int(r["articles_count"]),
                })

        batches = [
            {
                "round_id": r["round_id"],
                "completed_at": r["completed_at"].isoformat() if r.get("completed_at") else None,
                "articles_count": int(r["articles_count"]),
                # P13-v2: 改成对象数组 [{name, articles_count}] · 给前端左栏行业列表用
                "industries": industries_by_batch.get(r["round_id"], []),
            }
            for r in batch_rows
        ]
    finally:
        conn.close()

    return {"batches": batches, "total": len(batches)}


# ========== GET /articles/{id}/details · 详情 ==========


# ========== GET /articles/intent-distribution · 文章意图分布 ==========
# 注意: 必须放在 /articles/{article_id} 之前

@router.get("/articles/intent-distribution", summary="文章意图 8 分类分布")
async def get_article_intent_distribution(
    request: Request,
    batch_id: Optional[str] = None,
    batch_ids: Optional[str] = None,
    industry: Optional[str] = None,
    content_type: Optional[str] = None,
    domain_tier: Optional[str] = None,
    review_status: Optional[str] = "in_library",
    min_chars: Optional[int] = None,
    max_chars: Optional[int] = None,
    keyword: Optional[str] = None,
):
    _require_admin(request)
    where_clauses = ["(expired = FALSE OR expired IS NULL)"]
    params: list = []
    if batch_ids:
        ids = [x.strip() for x in batch_ids.split(',') if x.strip()]
        if ids:
            where_clauses.append("first_seen_round_id IN (" + ", ".join(["%s"] * len(ids)) + ")")
            params.extend(ids)
    elif batch_id:
        where_clauses.append("first_seen_round_id = %s")
        params.append(batch_id)
    if industry:
        where_clauses.append("primary_industry = %s")
        params.append(industry)
    if content_type:
        where_clauses.append("content_type = %s")
        params.append(content_type)
    if domain_tier:
        where_clauses.append("domain_tier = %s")
        params.append(domain_tier)
    if review_status:
        statuses = [x.strip() for x in review_status.split(',') if x.strip()]
        if statuses:
            where_clauses.append("review_status IN (" + ", ".join(["%s"] * len(statuses)) + ")")
            params.extend(statuses)
    if min_chars is not None:
        where_clauses.append("cleaned_char_count >= %s")
        params.append(min_chars)
    if max_chars is not None:
        where_clauses.append("cleaned_char_count <= %s")
        params.append(max_chars)
    if keyword:
        where_clauses.append("(title ILIKE %s OR domain ILIKE %s OR url ILIKE %s)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])
    where_sql = " WHERE " + " AND ".join(where_clauses)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT COALESCE(intent_type, '__null__') AS intent_type, COUNT(*) AS n
              FROM geo_research_articles
              {where_sql}
             GROUP BY COALESCE(intent_type, '__null__')
            """,
            params,
        )
        counts = {row['intent_type']: int(row['n']) for row in cur.fetchall()}
    finally:
        conn.close()
    total = sum(counts.values())
    unclassified_count = counts.get('__null__', 0)
    classified_total = max(0, total - unclassified_count)
    order = list(ARTICLE_INTENT_LABELS.keys())
    items = []
    for key, label in ARTICLE_INTENT_LABELS.items():
        count = counts.get(key, 0)
        items.append({
            "intent_type": key,
            "label": label,
            "count": count,
            # ????????????????????????,?? 1 ?????????????? 1%?
            "percent": round((count / classified_total * 100.0), 1) if classified_total else 0.0,
        })
    items.sort(key=lambda x: (-x['count'], order.index(x['intent_type'])))
    return {
        "total": total,
        "classified_total": classified_total,
        "counts": counts,
        "items": items,
        "unclassified_count": unclassified_count,
    }

@router.get("/articles/{article_id}", summary="单篇详情(含 OSS cleaned + raw 内容)")
async def get_article_detail(article_id: int, request: Request):
    """详情 · 含 cleaned + raw markdown · 关联 reference_articles 是否已入库"""
    _require_admin(request)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, url, url_hash, domain, title, primary_industry,
                   content_type, intent_type, intent_confidence, intent_reason, intent_model, intent_classified_at,
                   domain_tier,
                   oss_key_raw, oss_key_cleaned, inline_cleaned_content,
                   raw_char_count, cleaned_char_count,
                   content_hash, is_duplicate, primary_article_id,
                   total_citation_count,
                   clean_status, clean_model, clean_attempts, last_cleaned_at,
                   review_status, reviewed_by, reviewed_at,
                   first_seen_round_id, fetched_at, expired
              FROM geo_research_articles
             WHERE id = %s
            """,
            (article_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"文章不存在: {article_id}")

        # 关联 citations 列表(被引情况)
        cur.execute(
            """
            SELECT c.round_id, c.platform, c.prompt_id, c.rank_in_response, c.cited_at,
                   p.prompt_text
              FROM geo_research_article_citations c
              LEFT JOIN geo_research_prompts p ON p.id = c.prompt_id
             WHERE c.article_id = %s
             ORDER BY c.cited_at DESC
             LIMIT 50
            """,
            (article_id,),
        )
        citations = []
        for c in cur.fetchall():
            citations.append({
                "round_id": c.get("round_id"),
                "platform": c.get("platform"),
                "prompt_id": c.get("prompt_id"),
                "prompt_text": c.get("prompt_text"),
                "rank": c.get("rank_in_response"),
                "cited_at": _iso(c.get("cited_at")),
            })

        # 是否已加入参考库(通过 source_article_id 关联)
        is_in_reference = False
        reference_id = None
        try:
            cur.execute(
                "SELECT id FROM reference_articles WHERE source_article_id = %s LIMIT 1",
                (article_id,),
            )
            ref_row = cur.fetchone()
            if ref_row:
                is_in_reference = True
                reference_id = ref_row["id"]
        except Exception:
            # reference_articles 表/列可能在某些环境缺,容错
            pass
    finally:
        conn.close()

    # OSS 内容下载 (fail-soft) · Phase 9 fallback: inline_cleaned_content (老数据迁移用)
    cleaned_content = ""
    raw_content = ""
    cleaned_truncated = False
    # Cleaned: 优先 OSS · fallback inline_cleaned_content (老 AI回答爬虫 迁移)
    if row.get("oss_key_cleaned"):
        cleaned_content = download_markdown(row["oss_key_cleaned"]) or ""
    if not cleaned_content and row.get("inline_cleaned_content"):
        cleaned_content = row["inline_cleaned_content"]
    if len(cleaned_content) > 200000:
        cleaned_content = cleaned_content[:200000]
        cleaned_truncated = True
    # Raw: 优先 OSS · fallback inline_cleaned_content (P13-v13 2026-05-27 老板反馈)
    #   Legacy (P09 migration 自本地 AI回答爬虫) 没存 raw · 全部文章 raw tab 显示"(无原文)"
    #   修: 如果 oss_key_raw 没 · raw_content 用 inline_cleaned_content 兜底
    #       老数据来源是清洗后版本 · raw 显示同 cleaned 内容 · 至少老板能看到内容
    #       新跑批仍走 OSS · raw 是真正的抓取原文 · 不受影响
    if row.get("oss_key_raw"):
        raw_content = download_markdown(row["oss_key_raw"]) or ""
        if len(raw_content) > 200000:
            raw_content = raw_content[:200000]
    if not raw_content and row.get("inline_cleaned_content"):
        # Legacy fallback · raw 用 inline_cleaned_content 兜底
        raw_content = row["inline_cleaned_content"] or ""
        if len(raw_content) > 200000:
            raw_content = raw_content[:200000]

    return {
        "article": {
            **_article_row(row),
            "url_hash": row.get("url_hash"),
            "oss_key_raw": row.get("oss_key_raw"),
            "oss_key_cleaned": row.get("oss_key_cleaned"),
            "content_hash": row.get("content_hash"),
            "clean_model": row.get("clean_model"),
            "clean_attempts": row.get("clean_attempts"),
            "last_cleaned_at": _iso(row.get("last_cleaned_at")),
            "reviewed_by": row.get("reviewed_by"),
            "reviewed_at": _iso(row.get("reviewed_at")),
        },
        "cleaned_content": cleaned_content,
        "raw_content": raw_content,
        "cleaned_truncated": cleaned_truncated,
        "citations": citations,
        "citations_count": len(citations),
        "is_in_reference": is_in_reference,
        "reference_id": reference_id,
    }


# ========== PUT /articles/{id} · 编辑 ==========

@router.put("/articles/{article_id}", summary="编辑 cleaned_content (覆盖式 · raw 不动)")
async def edit_article(article_id: int, body: EditArticleRequest, request: Request):
    """管理员编辑文章 cleaned_content · 重新上传 OSS · 更新 char_count"""
    user = _require_admin(request)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, url_hash, domain FROM geo_research_articles WHERE id = %s",
            (article_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"文章不存在: {article_id}")

        # 生成新的 cleaned OSS key
        ym = datetime.utcnow().strftime('%Y-%m')
        from uuid import uuid4
        new_key = generate_oss_key_for_article(
            'cleaned', ym, row['domain'], row['url_hash'],
            f"manual-{article_id}-{uuid4().hex}",
        )

        up_result = upload_markdown(new_key, body.cleaned_content)
        if not up_result.get('ok'):
            raise HTTPException(500, f"OSS 上传失败: {up_result.get('error', '未知')}")

        # 重新算字数(用 Phase 9 规则)
        new_char_count = _text_length(body.cleaned_content)

        cur.execute(
            """
            UPDATE geo_research_articles
               SET oss_key_cleaned = %s,
                   cleaned_char_count = %s,
                   clean_status = 'cleaned',
                   clean_model = 'rule_v1_edited',
                   last_cleaned_at = NOW(),
                   reviewed_by = %s,
                   reviewed_at = NOW(),
                   review_note = %s
             WHERE id = %s
            """,
            (
                new_key,
                new_char_count,
                str(user.get('user_id') or user.get('username') or 'admin')[:50],
                (body.edit_note or '')[:500],
                article_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"[edit_article] id={article_id} by={user.get('username')} new_chars={new_char_count}")
    return {
        "ok": True,
        "article_id": article_id,
        "oss_key_cleaned": new_key,
        "cleaned_char_count": new_char_count,
    }


# ========== DELETE /articles/{id} · 单条删除(软删) ==========


# ========== PUT /articles/{id}/intent · 手动修正文章意图 ==========

@router.put("/articles/{article_id}/intent", summary="手动修正文章意图")
async def update_article_intent(article_id: int, body: UpdateArticleIntentRequest, request: Request):
    user = _require_admin(request)
    if body.intent_type is not None and body.intent_type not in ARTICLE_INTENT_LABELS:
        raise HTTPException(400, f"intent_type 非法, 可选: {', '.join(ARTICLE_INTENT_TYPES)}")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_research_articles
               SET intent_type = %s,
                   intent_confidence = CASE WHEN %s IS NULL THEN NULL ELSE 1.0 END,
                   intent_reason = %s,
                   intent_model = CASE WHEN %s IS NULL THEN NULL ELSE 'manual' END,
                   intent_classified_at = CASE WHEN %s IS NULL THEN NULL ELSE NOW() END,
                   reviewed_by = %s,
                   reviewed_at = NOW(),
                   review_note = %s
             WHERE id = %s
            """,
            (
                body.intent_type,
                body.intent_type,
                None if body.intent_type is None else (body.note or '管理员手动修正'),
                body.intent_type,
                body.intent_type,
                str(user.get('user_id') or user.get('username') or 'admin')[:50],
                (body.note or '')[:500],
                article_id,
            ),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"文章不存在: {article_id}")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "article_id": article_id, "intent_type": body.intent_type}

@router.delete("/articles/{article_id}", summary="单条删除(软删 expired=TRUE)")
async def delete_article(article_id: int, request: Request):
    """软删 · DB 行保留(便于审计 + month_weights 不丢历史)· expired=TRUE 后文章库默认不显示"""
    _require_admin(request)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_research_articles SET expired = TRUE, expired_at = NOW() WHERE id = %s",
            (article_id,),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"文章不存在: {article_id}")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "deleted": True, "article_id": article_id}


# ========== POST /articles/{id}/add-to-reference ==========

@router.post("/articles/{article_id}/add-to-reference",
             summary="加入参考文章库(写 reference_articles + 标 imported_to_reference)")
async def add_to_reference(article_id: int, body: AddToReferenceRequest, request: Request):
    """单条加入 reference_articles 库(给"仿写工坊"用)· 幂等(已加返 200 + reference_id)"""
    user = _require_admin(request)
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 读 article 数据
        cur.execute(
            """
            SELECT id, url, domain, title, primary_industry,
                   oss_key_cleaned, inline_cleaned_content,
                   cleaned_char_count, review_status
              FROM geo_research_articles
             WHERE id = %s
            """,
            (article_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"文章不存在: {article_id}")

        # 幂等检查
        try:
            cur.execute(
                "SELECT id FROM reference_articles WHERE source_article_id = %s LIMIT 1",
                (article_id,),
            )
            existing = cur.fetchone()
            if existing:
                return {
                    "ok": True,
                    "already_in_reference": True,
                    "reference_id": existing["id"],
                    "article_id": article_id,
                }
        except Exception:
            pass  # source_article_id 列可能没建,继续往下走 INSERT

        # 拉内容: OSS 优先 · fallback inline (老数据)
        content = ""
        if row.get("oss_key_cleaned"):
            content = download_markdown(row["oss_key_cleaned"]) or ""
        if not content and row.get("inline_cleaned_content"):
            content = row["inline_cleaned_content"]
        if not content:
            raise HTTPException(409, "文章 cleaned_content 为空 · 无法加入参考库 · 请先编辑")

        # INSERT reference_articles
        cur.execute(
            """
            INSERT INTO reference_articles
                (title, content, source_url, platform, industry,
                 intent_type, analysis, success_proof, status,
                 source_article_id, created_at)
            VALUES (%s, %s, %s, %s, %s,
                    %s, %s, %s, 'active',
                    %s, NOW())
            RETURNING id
            """,
            (
                (row.get("title") or row.get("url") or "")[:500],
                content,
                row.get("url"),
                "research_monitor",  # platform 标识源自调研系统
                row.get("primary_industry"),
                body.intent_type,
                body.analysis,
                f"调研文章 #{article_id} · 被引 {row.get('cleaned_char_count') or 0} 字",
                article_id,
            ),
        )
        ref_id = cur.fetchone()["id"]

        # 标记 article 状态 (CHECK 约束 P05a 已扩 'imported_to_reference')
        cur.execute(
            """
            UPDATE geo_research_articles
               SET review_status = 'imported_to_reference',
                   reviewed_by = %s,
                   reviewed_at = NOW()
             WHERE id = %s
            """,
            (
                str(user.get('user_id') or user.get('username') or 'admin')[:50],
                article_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"[add_to_reference] article_id={article_id} → reference_id={ref_id} by={user.get('username')}")
    return {
        "ok": True,
        "article_id": article_id,
        "reference_id": ref_id,
        "already_in_reference": False,
    }


# ========== POST /articles/bulk-delete ==========

@router.post("/articles/bulk-delete", summary="批量软删")
async def bulk_delete_articles(body: BulkArticleIdsRequest, request: Request):
    """批量软删 · 单事务 · 返回成功/失败计数"""
    _require_admin(request)
    ids = list(set(body.article_ids))  # 去重
    if not ids:
        return {"deleted": 0, "skipped": 0}

    conn = get_connection()
    try:
        cur = conn.cursor()
        placeholders = ", ".join(["%s"] * len(ids))
        cur.execute(
            f"UPDATE geo_research_articles SET expired = TRUE, expired_at = NOW() "
            f"WHERE id IN ({placeholders}) AND (expired = FALSE OR expired IS NULL)",
            ids,
        )
        deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    skipped = len(ids) - deleted
    return {"deleted": deleted, "skipped": skipped, "total": len(ids)}


# ========== POST /articles/bulk-add-to-reference ==========

@router.post("/articles/bulk-add-to-reference", summary="批量加入参考库")
async def bulk_add_to_reference(body: BulkArticleIdsRequest, request: Request):
    """批量加入参考库 · 串行处理 · 已加入的幂等跳过 · 返回各条结果"""
    user = _require_admin(request)
    ids = list(set(body.article_ids))
    if not ids:
        return {"imported": 0, "already_in": 0, "failed": 0, "results": []}

    imported = 0
    already_in = 0
    failed = 0
    results: list[dict] = []

    # Phase 9 P09-fix-2: 用 SAVEPOINT 让单条失败只回滚自己,不回滚前面成功的
    # 老代码 conn.rollback() 会回滚整个事务但计数继续 +1 · 导致 API 返回值跟 DB 实际不符
    conn = get_connection()
    try:
        cur = conn.cursor()
        for idx, aid in enumerate(ids):
            sp_name = f"sp_bulk_addref_{idx}"
            cur.execute(f"SAVEPOINT {sp_name}")
            try:
                # 拉 article (含 inline fallback)
                cur.execute(
                    """
                    SELECT id, url, domain, title, primary_industry,
                           oss_key_cleaned, inline_cleaned_content, cleaned_char_count
                      FROM geo_research_articles
                     WHERE id = %s
                    """,
                    (aid,),
                )
                row = cur.fetchone()
                if not row:
                    cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                    failed += 1
                    results.append({"article_id": aid, "ok": False, "error": "文章不存在"})
                    continue

                # 幂等
                exist = None
                try:
                    cur.execute(
                        "SELECT id FROM reference_articles WHERE source_article_id = %s LIMIT 1",
                        (aid,),
                    )
                    exist = cur.fetchone()
                except Exception:
                    pass
                if exist:
                    cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                    already_in += 1
                    results.append({"article_id": aid, "ok": True, "reference_id": exist["id"], "already_in": True})
                    continue

                # 拉内容: OSS 优先 · fallback inline (老数据)
                content = ""
                if row.get("oss_key_cleaned"):
                    content = download_markdown(row["oss_key_cleaned"]) or ""
                if not content and row.get("inline_cleaned_content"):
                    content = row["inline_cleaned_content"]
                if not content:
                    cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                    failed += 1
                    results.append({"article_id": aid, "ok": False, "error": "cleaned_content 为空"})
                    continue

                cur.execute(
                    """
                    INSERT INTO reference_articles
                        (title, content, source_url, platform, industry,
                         intent_type, status, source_article_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, NOW())
                    RETURNING id
                    """,
                    (
                        (row.get("title") or row.get("url") or "")[:500],
                        content,
                        row.get("url"),
                        "research_monitor",
                        row.get("primary_industry"),
                        body.intent_type,
                        aid,
                    ),
                )
                ref_id = cur.fetchone()["id"]

                cur.execute(
                    """
                    UPDATE geo_research_articles
                       SET review_status = 'imported_to_reference',
                           reviewed_by = %s,
                           reviewed_at = NOW()
                     WHERE id = %s
                    """,
                    (
                        str(user.get('user_id') or user.get('username') or 'admin')[:50],
                        aid,
                    ),
                )
                cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                imported += 1
                results.append({"article_id": aid, "ok": True, "reference_id": ref_id, "already_in": False})
            except Exception as e:
                # 单条失败 · 仅回滚此 SAVEPOINT (前面成功的不动)
                try:
                    cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                    cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                except Exception:
                    pass  # SAVEPOINT 不存在(罕见)兜底
                failed += 1
                results.append({"article_id": aid, "ok": False, "error": str(e)[:200]})
                continue

        conn.commit()
    finally:
        conn.close()

    return {
        "imported": imported,
        "already_in": already_in,
        "failed": failed,
        "total": len(ids),
        "results": results,
    }
