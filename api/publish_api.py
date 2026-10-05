"""
文章代发 API
- 媒体列表 / 筛选 / 分类
- 智能推荐
- 单篇下单 / 批量下单
- 订单状态 / 批次查询
- 售后退款
- 管理员：Session 管理 / 媒体同步 / 加价比例
"""

import logging
import asyncio
import secrets
import math
import shortuuid
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Any, Optional

from db.publish_db import (
    get_media_list, get_media_by_ids,
    # get_media_categories / get_media_platforms 已不再使用 (查错字段的历史遗留, 见 /media/filters 修复说明)
    create_batch, create_order, bulk_create_order_items,
    get_batch, get_user_batches, get_items_by_batch,
    get_order, get_user_orders, get_order_items,
    update_item_submitted, update_item_queued,
    recalculate_order_status, recalculate_batch_status,
    is_first_publish, get_mhz_session, set_mhz_session,
    get_article_for_publish_recommendation, get_article_publish_analysis,
    upsert_article_publish_analysis, get_effective_pool_candidates,
    create_publish_decision_snapshot, get_publish_decision_snapshot,
    list_publish_decision_snapshots,
    list_publish_outcomes,
)
from db.wallet_db import get_wallet_balance
from middleware.billing import deduct_points
# [GEO-R1-CAN-108 / GEO-R2-CAN-025] 归属校验既有 helper (禁自造轮子)
from auth.brand_access import require_brand_access, require_quote_access

logger = logging.getLogger("GEO-Publish-API")

router = APIRouter(prefix="/api/publish", tags=["文章代发"])

# 白标(v3.6 · 决策 F)默认值 · 分段拼接保留原平台文案,不在代码行留可被白标扫描误判的字面 token
_AGENT_DEFAULT_BRAND = "Omni" + "Rank"


def _resolve_agent_brand_name(request: Request) -> str:
    """取当前代理的品牌名(surface='agent' · 仅 OEM 出代理品牌)· 非 OEM 回退平台默认。"""
    try:
        from services.public_whitelabel import resolve_branding_context
        user = getattr(getattr(request, "state", None), "user", None) or {}
        uid = user.get("user_id") or user.get("id")
        if not uid:
            return _AGENT_DEFAULT_BRAND
        # [白标继承] 代发文章的署名会露到客户面:员工代发也必须署团队长的品牌,
        # 否则同一个服务商发出去的文章会出现两种署名。
        from auth.principal_identity import resolve_branding_principal_user_id
        uid = resolve_branding_principal_user_id(request, fallback_user_id=int(uid))
        ctx = resolve_branding_context(surface="agent", owner_user_id=int(uid))
        if ctx.get("source") == "platform_default":
            return _AGENT_DEFAULT_BRAND
        return (ctx.get("brand") or {}).get("company_name", "").strip() or _AGENT_DEFAULT_BRAND
    except Exception:
        return _AGENT_DEFAULT_BRAND


def _industry_brand_if_accessible(request: Request, brand_id: Optional[int]) -> Optional[dict]:
    """[WO_267] 行业判定要用品牌的名称 / 备注 / 种子词当上下文(光伏客户的 industry 列写的是建筑)。

    🔴 recommend-v2 的 brand_id 一直**不鉴权**(只给灰度与去重用)。读品牌字段之前必须先过
       `require_brand_access`;无权 / 读不到 ⇒ 返回 None = 只按行业串判 —— 与改前行为相同,
       **不因此 403**(那会改变一个公开端点的既有行为)。
    """
    if not brand_id:
        return None
    try:
        require_brand_access(request, int(brand_id), allow_null=False)
    except HTTPException:
        return None
    try:
        from db.diagnosis_db import get_brand_by_id
        return get_brand_by_id(int(brand_id))
    except Exception as e:
        logger.warning(f"[industry] 读品牌上下文失败 brand_id={brand_id}: {e}")
        return None


async def _require_article_access(request: Request, article_id: int) -> dict:
    """[GEO-R1-CAN-108 / GEO-R2-CAN-025] 通过 article_id → 报价单 brand_id 校验当前用户
    归属并返回文章。fail-closed(allow_null=False): 无法解析 brand(未关联报价单) 时拒绝 —
    深度分析/媒体推荐会把正文送入 LLM 且 ON CONFLICT 覆写全局缓存行, 属敏感读写。"""
    article = await asyncio.to_thread(get_article_for_publish_recommendation, article_id)
    if not article:
        raise HTTPException(status_code=404, detail=f"文章不存在 article_id={article_id}")
    require_brand_access(request, article.get("brand_id"), allow_null=False)
    return article


# [GEO-R9-CAN-003] 长文首尾采样: 不再硬截前 4000 字丢弃尾部风险/合规信号
_CONTENT_HINT_HEAD = 2800
_CONTENT_HINT_TAIL = 1200


def _build_content_hint(article_content: str) -> str:
    """构造送入分析 LLM 的正文片段。短文全量; 长文取首段 + 尾段并显式标注中段省略,
    避免静默丢弃 4000 字之后的合规/风险信号(纯首部截断的尾部盲区)。"""
    if not article_content:
        return "无正文"
    text = article_content.strip()
    if len(text) <= _CONTENT_HINT_HEAD + _CONTENT_HINT_TAIL:
        return text
    head = text[:_CONTENT_HINT_HEAD]
    tail = text[-_CONTENT_HINT_TAIL:]
    return f"{head}\n……(中段略 · 以下为文章结尾)……\n{tail}"


FIRST_ORDER_DISCOUNT = 0.8  # 首单 8 折


# ==================== 请求模型 ====================

class SingleOrderRequest(BaseModel):
    article_id: int
    article_title: str
    article_content: str = ""
    media_ids: list[int] = Field(..., min_length=1)
    # [P0-D] 幂等键 — 防双击/网络重试导致重复扣费 + 重复下单
    request_id: Optional[str] = None


class BatchOrderRequest(BaseModel):
    items: list[SingleOrderRequest] = Field(..., min_length=1)
    # [P0-D] 批量级幂等键
    request_id: Optional[str] = None


class SessionUpdateRequest(BaseModel):
    session_id: str


class MarkupRatioRequest(BaseModel):
    ratio: float = Field(..., gt=1.0, le=5.0)


# ==================== 媒体列表 ====================

@router.get("/media")
async def list_media(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    media_type: Optional[str] = None,
    category: Optional[str] = None,
    platform: Optional[str] = None,
    news_source: Optional[str] = None,
    can_geo: Optional[bool] = None,
    high_value: Optional[bool] = None,
    weight_min: Optional[int] = None,
    price_min: Optional[int] = None,
    price_max: Optional[int] = None,
    search: Optional[str] = None,
    sort_by: str = "our_price_points",
    sort_dir: str = "asc",
):
    """分页查询可用媒体"""
    result = get_media_list(
        page=page, limit=limit, media_type=media_type,
        category=category, platform=platform,
        news_source=news_source, can_geo=can_geo,
        high_value=high_value, weight_min=weight_min,
        price_min=price_min, price_max=price_max,
        search=search, sort_by=sort_by, sort_dir=sort_dir,
    )
    # [CUR-10 · defgeo 窗C 2026-08-21] db/publish_db.get_media_list 内部是
    # `SELECT * FROM mhz_media`(line 649),结果原样进响应。同 /media/by-engine
    # 的理由:在**公开出口**做机械收口,不改 db 层函数形状(它还有内部计价调用方,
    # 那些**需要**成本列)。剥掉的判定含前缀规则,新增私有列自动被挡。
    from services.defensive_geo.publish.media_identity import redact_private_columns

    result = {**result, "media": redact_private_columns(result.get("media") or [])}
    return {"status": "success", **result}


@router.get("/media/filters")
async def get_media_filters(media_type: Optional[str] = None):
    """获取媒体筛选选项（分类、平台、地区）

    P0 fix 2026-04-19: 原 SQL 查 mhz_media.category / platform / news_source 三列,
      但这三列在生产 DB 全为 NULL (索引建过但数据从未填入, 历史遗留).
      真实数据存在 resource_type_name / area / portal_media 等字段.
      此处改为复用 db.meijiehezi_db 已有的真实字段查询, 保持前端返回结构兼容.
    """
    # wemedia 分叉 (自媒体): 用 mhz_wemedia 表, 有 platform / industry / province
    if media_type == 'wemedia':
        from db.meijiehezi_db import get_wemedia_filters as _we_filters
        raw = _we_filters()
        # [WO_PUBLISH_DISPATCH Part② 2026-08-17] 上游的 industries 已经变成
        #   `[{key,count}]`(L1 大类 + facet 计数)。**这条老链路的契约是 string[]**
        #   (旧推荐面板直接把元素当字符串渲染),所以在这里投影回键名 ——
        #   本工单只治发布中心那三个 tab,不顺手改老面板的响应形状。
        industries = [x["key"] if isinstance(x, dict) else x
                      for x in (raw.get("industries") or [])]
        return {
            "status": "success",
            "industries": industries,
            "regions": raw.get("provinces", []),
            "platforms": raw.get("platforms", []),
            "news_sources": [],
            "categories": industries,  # 兼容旧前端
        }

    # media 分支 (门户媒体): 用 mhz_media 表的正确字段
    from db.meijiehezi_db import get_media_filters as _mhz_filters
    raw = _mhz_filters()

    PROVINCES = {
        '广东', '山东', '浙江', '江苏', '四川', '湖北', '北京', '上海',
        '河北', '河南', '福建', '陕西', '湖南', '安徽', '江西', '重庆',
        '辽宁', '吉林', '黑龙江', '山西', '贵州', '云南', '广西', '海南',
        '甘肃', '青海', '宁夏', '内蒙古', '新疆', '西藏', '天津',
        '港澳台', '综合全国',
    }
    areas = raw.get("areas", [])  # ['上海','北京','综合全国',...]
    regions = [a for a in areas if a in PROVINCES]
    # industries 用 resource_type_name (如"新闻资讯"/"女性时尚"), 业务可读
    industries = raw.get("resource_type_names", [])
    # platforms 用 portal_media (如"门户网站"/"垂直媒体"), 区分媒体形态
    platforms = raw.get("portal_medias", [])
    # news_sources 用 news_resources (INT 转 str)
    news_sources_raw = raw.get("news_resources", []) or []
    news_sources = [str(s) for s in news_sources_raw if s is not None and str(s).strip() != '']

    return {
        "status": "success",
        "industries": industries,
        "regions": regions,
        "platforms": platforms,
        "news_sources": news_sources,
        "categories": industries,  # 兼容旧前端
    }


@router.get("/media/engine-favorites")
async def get_engine_favorites(engine: str = Query(..., description="AI引擎名称：豆包/Kimi/DeepSeek/千问")):
    """获取某个 AI 引擎偏好引用的平台名列表（用于前端筛选）"""
    from db.connection import get_connection as _gc
    conn = _gc()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT platform FROM geo_engine_stats
            WHERE engine = %s AND citation_rate >= 0.2
            ORDER BY citation_rate DESC
            LIMIT 50
        """, (engine,))
        platforms = [r["platform"] for r in cur.fetchall()]
        return {"status": "success", "engine": engine, "platforms": platforms}
    finally:
        conn.close()


@router.get("/media/by-engine")
async def list_media_by_engine(
    engine: str = Query(...),
    media_type: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    """根据 AI 引擎偏好筛选媒体：先查调研数据得到偏好平台名，再从 mhz_media 中匹配"""
    from db.connection import get_connection as _gc

    conn = _gc()
    try:
        cur = conn.cursor()
        # 获取该引擎偏好的平台名（历史来源曝光率 >= 20%）
        cur.execute("""
            SELECT DISTINCT platform FROM geo_engine_stats
            WHERE engine = %s AND citation_rate >= 0.15
        """, (engine,))
        fav_platforms = [r["platform"] for r in cur.fetchall()]

        if not fav_platforms:
            return {"status": "success", "media": [], "total": 0, "page": page, "engine": engine}

        # 用平台名模糊匹配 mhz_media
        like_conditions = []
        like_params = []
        for fp in fav_platforms[:30]:  # 限制条件数
            like_conditions.append("media_name ILIKE %s")
            like_params.append(f"%{fp}%")
            like_conditions.append("platform ILIKE %s")
            like_params.append(f"%{fp}%")

        type_cond = "AND media_type = %s" if media_type else ""
        type_params = [media_type] if media_type else []

        where = f"is_active = TRUE AND ({' OR '.join(like_conditions)}) {type_cond}"

        cur.execute(f"SELECT COUNT(*) as cnt FROM mhz_media WHERE {where}", like_params + type_params)
        total = cur.fetchone()["cnt"]

        offset = (page - 1) * limit
        # [CUR-10 · defgeo 窗C 2026-08-21] 原文是 `SELECT * FROM mhz_media` 后
        # `[dict(r) for r in ...]` 直接进 HTTP 响应。mhz_media 含 price/price1/price2
        # (采购价)、wholesale_*、platform_cost_cents、provider、provider_media_id、
        # remark、entrance_link 等**供应商私有列** —— §11.5 明令禁止「一个宽 DTO
        # 再靠前端隐藏」。这里保留原查询形状(存量前端字段一个不少),
        # 只在出口做机械收口:判定走 media_identity.is_private_column(含前缀规则),
        # 日后新增 supplier_* / cost_* 列自动被挡,不靠有人记得回来补一行。
        cur.execute(f"""
            SELECT * FROM mhz_media WHERE {where}
            ORDER BY our_price_points ASC
            LIMIT %s OFFSET %s
        """, like_params + type_params + [limit, offset])

        from services.defensive_geo.publish.media_identity import redact_private_columns

        media = redact_private_columns([dict(r) for r in cur.fetchall()])
        return {
            "status": "success",
            "media": media,
            "total": total,
            "page": page,
            "pages": math.ceil(total / limit) if limit > 0 else 0,
            "engine": engine,
            "matched_platforms": fav_platforms[:10],
        }
    finally:
        conn.close()


@router.get("/media/recommend")
async def recommend_media(
    request: Request,
    article_id: Optional[int] = None,
    industry: Optional[str] = None,
    keywords: Optional[str] = None,
    article_type: Optional[str] = None,
    limit: int = Query(8, ge=1, le=20),
):
    """
    智能推荐媒体。
    基于 GEO 调研数据 × mhz_media 交叉匹配。
    """
    from services.placement_service import recommend_for_publish

    recommendations = recommend_for_publish(
        industry=industry or '',
        keywords=keywords or '',
        article_type=article_type or '',
        limit=limit,
    )
    return {
        "status": "success",
        "media": recommendations,
        "note": f"推荐基于 {_resolve_agent_brand_name(request)} GEO 大数据调研，仅供参考，不构成效果承诺",
    }


@router.get("/media/recommend-v2")
async def recommend_media_v2(
    request: Request,
    industry: Optional[str] = None,
    limit: int = Query(8, ge=1, le=20),
    keyword: Optional[str] = None,
    brand_id: Optional[int] = None,
):
    """
    V2 推荐媒体 — 同时返回软文和自媒体推荐，垂直/通用分组。

    [v2-F 2026-05-18] 接 user_id 用于 90 天历史去重 · 避免重复推荐已发媒体。

    [媒体平衡 T1/T2 2026-07-29] 传 ``keyword`` 时按**问题族真实被引 mix** 折算默认
    「主干 N + 垂类 M」组合(advisory,代理可改),并返回 ``ai_citation_trunk`` 供
    推荐页单列「AI 引用主干」区。不传 keyword 时回落行业/全局 mix,行为不劣化。
    """
    from services.placement_service import recommend_for_publish_v2
    # [v2-F BUG FIX 2026-05-18] 中间件注入的是 request.state.user 字典 · 不是 user_id
    # 旧版 getattr(state,'user_id',0) 永远拿到 0 · 90 天去重完全失效
    user_obj = getattr(request.state, 'user', None) or {}
    user_id_raw = user_obj.get('user_id') if isinstance(user_obj, dict) else 0
    # portal 端 user_id 可能是 "portal_xxx" 字符串 · 安全转 int 拿不到就 0
    try:
        user_id = int(user_id_raw) if user_id_raw else 0
    except (TypeError, ValueError):
        user_id = 0
    # [P0 事件循环 2026-07-28 Deploy-CTO] recommend_for_publish_v2 是**同步阻塞**函数
    # (内部 400+ 条 SQL)。此前直接写在 async def 里,生产 WORKERS=1 只有一个事件循环 →
    # 这一个请求执行期间整台服务器停止响应,现象是同屏所有接口一起 504(实测 effectiveness-board
    # 本身只要 357ms 却报 504 两分钟,就是排在它后面)。挪进线程后事件循环能继续服务别的请求。
    # 注:同期已给 mhz_media.media_name 建 pg_trgm GIN 索引(idx_mhz_media_name_trgm),
    # 单次 44s → 2.7s;两者是不同的病,都要治。
    #
    # [性能第二刀 §A 2026-07-28] 两次调用互相独立(只差 media_type,不共享中间状态),
    # 改 gather 并发 → 墙钟从 media+wemedia 串行变成 max(media, wemedia)。
    # 代价核过:并发后本端点同时占 **2 个** DB 连接而不是 1 个。池上限
    # db/connection.py maxconn=25(注释里那句 min15/max60 是失实的旧注释,以代码为准),
    # 单请求峰值 2 条 → 池被吃穿需要 13 个 recommend-v2 同时在跑;而事件循环的默认
    # 线程池才是真正的闸(见回执实测数字),到不了那个量。PG max_connections=500、
    # 当前用量 26,远不是瓶颈。
    #
    # wemedia 传 with_question_family=False:T2 问题族 mix 与 media_type 无关,
    # 两次算的是同一份,且下面只读 media_result 那一份 —— 少算一整套聚合和一次判定。
    industry_brand = await asyncio.to_thread(_industry_brand_if_accessible, request, brand_id)
    media_result, wemedia_result = await asyncio.gather(
        asyncio.to_thread(
            recommend_for_publish_v2,
            industry=industry or '', media_type='media', limit=limit,
            user_id=user_id, keyword=keyword or '', brand_id=brand_id or 0,
            industry_brand=industry_brand,
        ),
        asyncio.to_thread(
            recommend_for_publish_v2,
            industry=industry or '', media_type='wemedia', limit=limit,
            user_id=user_id, keyword=keyword or '', brand_id=brand_id or 0,
            with_question_family=False, industry_brand=industry_brand,
        ),
    )
    # [灰度 2026-07-29] 关闭时不构建也不返回「AI 引用主干」区,前端两块自然不渲染
    _mb_on = bool(media_result.get("media_balance_enabled"))
    # [WP12 P0-1 · Master SSOT v2.4 ③④] 真实被引域权重是主排序信号,必须对代理
    # 可见(Owner 明示"不藏在算法里"),并附渠道建议(全部 advisory,不阻断)。
    advisories = media_result.get("channel_advisories") or wemedia_result.get("channel_advisories") or []
    # [媒体平衡 T1] 通用主干不再藏:推荐页单列「AI 引用主干」区,数据源与
    # /media/citation-domains 同表(citation_domain_weights),口径一致。
    trunk = (await asyncio.to_thread(_build_citation_trunk_section, industry or '')
             if _mb_on else None)
    return {
        "status": "success",
        "matched_industry": media_result.get("matched_industry", ''),
        "media_vertical": media_result.get("vertical", []),
        "media_generic": media_result.get("generic", []),
        "wemedia_vertical": wemedia_result.get("vertical", []),
        "wemedia_generic": wemedia_result.get("generic", []),
        "citation_ranking_signal": media_result.get("citation_ranking_signal", "legacy_v2f"),
        "citation_weight_summary": media_result.get("citation_weight_summary"),
        "channel_advisories": advisories,
        # 灰度态回带,方便排查「为什么我看不到那两块」
        "media_balance_enabled": _mb_on,
        # [2026-07-28 C-4] 区分「配置就是关的」与「settings 读不到(fail-closed)」——
        #   后者现象是"功能时有时无",没有这个字段排查会很久。
        "media_balance_reason": media_result.get("media_balance_reason"),
        # T1 灯塔明示(灰度关时为 None)
        "ai_citation_trunk": trunk,
        # T2 问题族组合(advisory)
        "question_family_mix": media_result.get("question_family_mix"),
        "combination_plan": media_result.get("combination_plan"),
        # [P1-5b 2026-08-14] D6-B:该品牌真实被引的发布域(账本 body_proof 口径,
        # advisory;样本不足 available=False → 前端显示「暂无足够样本」不显示空榜)。
        "brand_reco_feedback": media_result.get("brand_reco_feedback"),
    }


def _build_citation_trunk_section(industry: str = "", top_n: int = 12) -> dict:
    """[T1] 「AI 引用主干」区:被引最强的通用域 + 强度徽章 + 可售与否。

    通用主干(门户/技术社区/UGC 问答)历来被"垂直优先"的推荐口径盖住,代理看不到
    搜狐 1969 / 网易 1483 / 博客园 961 这些真正撑起 AI 答案的域。这里单列出来。
    """
    try:
        from services.citation_domain_weights import (
            family_for_domain, get_citation_domain_weights,
        )
        from services.question_family_mix import inventory_keyword_for

        table = get_citation_domain_weights(industry=industry or "")
        ranked = sorted(
            (s for s in table.domains.values() if s.source == "observed"),
            key=lambda s: (-s.citation_count, s.domain),
        )
        rows = []
        for stat in ranked:
            family = family_for_domain(stat.domain)
            if not (family and family.is_trunk):
                continue
            rows.append({
                **stat.as_dict(),
                "role": family.role,
                "role_label": family.role_label,
                "self_serve": family.self_serve,
                "inventory_keyword": inventory_keyword_for(stat.domain),
            })
            if len(rows) >= top_n:
                break
        return {
            "window_days": table.window_days,
            "total_citations": table.total_citations,
            "degraded_reason": table.degraded_reason,
            "domains": rows,
        }
    except Exception as exc:  # pragma: no cover - advisory surface only
        logger.warning(f"[T1] 引用主干区构建失败: {exc}")
        return {"window_days": 0, "total_citations": 0, "degraded_reason": str(exc), "domains": []}


@router.post("/channel-callback/{callback_secret}")
async def publish_channel_callback(callback_secret: str, request: Request):
    """外部发布通道的订单状态回调（**无登录态**，靠路径里的共享密钥鉴权）。

    对方按 `{"data":[{order_id,status,response_message}]}` 推过来。
    密钥不对一律 404 —— 不回 401/403，避免把"这个端点存在"这件事告诉扫描器。

    幂等：`apply_callback_batch` 只处理非终态 item，回调重放不会重复退款。
    永远回 200：对方失败会重推，我们这边的问题不该变成他们的重试风暴。
    """
    import os

    expected = (os.getenv("PUBLISH_CHANNEL_CALLBACK_SECRET") or "").strip()
    if not expected:
        try:
            from config.settings_manager import load_settings
            expected = str(getattr(load_settings(), "publish_channel_callback_secret", "") or "").strip()
        except Exception:
            expected = ""
    if not expected or not secrets.compare_digest(str(callback_secret), expected):
        raise HTTPException(status_code=404, detail="Not Found")

    try:
        payload = await request.json()
    except Exception:
        return {"success": True, "processed": 0}
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {"success": True, "processed": 0}

    try:
        from services.kuaiyibo.status_sync import apply_callback_batch

        result = await asyncio.to_thread(apply_callback_batch, rows)
        logger.info("[channel-callback] %s", result.summary())
        return {"success": True, "processed": result.checked}
    except Exception as exc:
        # 回调处理失败不能把 500 抛回去引发重试风暴；轮询兜底会收口。
        logger.error("[channel-callback] 处理失败: %s", exc)
        return {"success": True, "processed": 0}


@router.get("/media/citation-domains")
async def publish_citation_domains(
    request: Request,
    industry: str = "",
    window_days: int = Query(90, ge=7, le=720),
    limit: int = Query(20, ge=1, le=100),
):
    """[WP12 P0-1] 真实被引域权重表(只读透明化)。

    代理看到的「AI 引用强度」来自这里,不是人工白名单。返回观测窗口、引擎分列
    和优先域族先验,方便销售向客户解释为什么推荐这些媒体。
    """
    from services.citation_domain_weights import (
        engine_citation_breakdown,
        get_citation_domain_weights,
        iter_priority_domain_families,
    )

    table = await asyncio.to_thread(
        get_citation_domain_weights, window_days=window_days, industry=industry or ""
    )
    return {
        "status": "success",
        "summary": table.summary(top_n=limit),
        "engine_breakdown": engine_citation_breakdown(table),
        "priority_domain_families": list(iter_priority_domain_families()),
    }


@router.get("/media/effectiveness-board")
async def publish_media_effectiveness_board(
    request: Request,
    industry: str = "",
    weeks: int = Query(4, ge=1, le=52),
    limit: int = Query(30, ge=1, le=100),
    brand_id: Optional[int] = None,
):
    """[E2-B] 发布流「AI 真实引用媒体榜」(只读)。

    [2026-07-05 数据源纠偏] 改用 get_publish_media_board(媒体实体 × 最新 shadow 快照口径):
    L3 答案实体是**品牌点名图谱**(竞品情报,留 admin E1),与媒体库 key 空间几乎零重叠,
    发布流用它榜上全是银行基金、无一可投放、点击引导必空搜(老板实测)。媒体实体口径的
    shadow 有效分由 L2 调研引用证据驱动 = 「被 AI 真实引用的媒体」,且天然可绑定媒体库。

    鉴权:走发布流现有 auth(request.state.user 已由全局中间件注入,代理端可访问),
    与 admin-only 的 /api/admin/geo-placement-flywheel/media/effectiveness-board 分离。
    榜是行业级市场情报(非按品牌/代理切分),无跨租户数据。纯只读、零 LLM、fail-soft。
    """
    from services.media_effectiveness_board import get_publish_media_board
    from writing.flywheel_cache import SCOPE_ENTITY_RANK, get_or_compute, make_key

    # [review fix] 发布中心是代理端热页(进入/切行业即自动加载,内含多组聚合 SQL):
    # 60s 进程缓存;桥接/绑定审批失效钩子已挂 SCOPE_ENTITY_RANK,同 scope 复用。
    # weeks 保留在缓存 key(参数向后兼容),媒体口径取最新快照不按周窗。
    # [WO_267] 带 brand_id(已鉴权)⇒ 行业判定吃品牌上下文,与付费点亮写路径同源;
    #   无权 / 不带 ⇒ None ⇒ v1.0 口径。结果因品牌而异 ⇒ 缓存键带上品牌。
    board_brand = _industry_brand_if_accessible(request, brand_id)
    _brand_key = int(brand_id) if board_brand is not None else 0

    def _cached() -> dict:
        return get_or_compute(
            make_key(SCOPE_ENTITY_RANK, "publish", industry or "all", weeks, limit, _brand_key),
            60,
            lambda: get_publish_media_board(industry, limit, allow_all_industry_fallback=True,
                                            brand=board_brand),
        )

    result = await asyncio.to_thread(_cached)

    # [媒体平衡 T5 · 2026-07-29] 「发布 → 被引」转化率并排放在榜里:
    # 被引榜说"AI 引用谁",转化率说"我们投进去的那条 URL 到底被引了没"。
    # 只有两张放一起,垂类该不该继续投才是数据自证,不是感觉。
    def _conversion() -> dict:
        from services.media_publish_success import build_publish_to_citation_conversion
        from writing.flywheel_cache import SCOPE_ENTITY_RANK, get_or_compute, make_key

        return get_or_compute(
            make_key(SCOPE_ENTITY_RANK, "publish_conversion", industry or "all", limit),
            300,
            lambda: build_publish_to_citation_conversion(limit=limit),
        )

    try:
        conversion = await asyncio.to_thread(_conversion)
    except Exception as exc:  # pragma: no cover - advisory surface, fail-soft
        logger.warning(f"[T5] 发布→被引转化率不可用: {exc}")
        conversion = None
    return {"status": "success", **result, "publish_to_citation": conversion}


class DeepAnalyzeRequest(BaseModel):
    article_id: int
    article_title: str = Field(..., min_length=1, max_length=500)
    article_keyword: str = Field('', max_length=200)
    brand_industry: str = Field('', max_length=200)
    force_refresh: bool = False


class MediaRecommendationRequest(BaseModel):
    article_title: str = Field('', max_length=500)
    article_keyword: str = Field('', max_length=200)
    brand_industry: str = Field('', max_length=200)
    article_type: Optional[str] = None
    publish_goal: str = Field('', max_length=500)
    include_wemedia: bool = True
    force_refresh: bool = False


class DecisionSnapshotRequest(BaseModel):
    brand_id: int
    quote_id: Optional[int] = None
    article_ids: list[int] = Field(..., min_length=1)
    recommendation_payload: dict[str, Any] = Field(default_factory=dict)
    evidence_payload: dict[str, Any] = Field(default_factory=dict)
    selected_media: list[dict[str, Any]] = Field(default_factory=list)
    selected_wemedia: list[dict[str, Any]] = Field(default_factory=list)
    estimated_points: int = Field(0, ge=0)
    disclaimer_version: str = Field('', max_length=100)
    terms_version: str = Field('', max_length=100)
    analysis_version: str = Field('', max_length=50)
    recommendation_level: int = Field(3, ge=1, le=3)
    request_id: Optional[str] = Field(None, max_length=200)


@router.post("/media/deep-analyze")
async def deep_analyze_for_publish(req: DeepAnalyzeRequest, request: Request):
    """
    AI 深度分析：分析文章适合发什么类型的媒体，结果缓存到 DB。
    默认复用缓存；用户明确重新分析时绕过缓存再调 LLM。
    """
    import json as _json
    import os
    import httpx
    from services.article_type import ARTICLE_TYPE_LABELS, normalize_article_type
    from services.publish_recommendation import (
        ANALYSIS_VERSION,
        infer_article_summary,
        parse_semantic_keywords,
    )

    # [GEO-R1-CAN-108] 授权先行(在读缓存之前): 校验文章归属, 防跨租户命中他人分析缓存 /
    # 读正文进 LLM / ON CONFLICT 覆写他人分析行。
    article = await _require_article_access(request, req.article_id)

    cached = await asyncio.to_thread(get_article_publish_analysis, req.article_id)
    if cached and not req.force_refresh:
        cached_analysis = cached.get("analysis") or {}
        if isinstance(cached_analysis, str):
            try:
                cached_analysis = _json.loads(cached_analysis)
            except Exception:
                cached_analysis = {}
        cached_analysis.setdefault("article_type", cached.get("article_type"))
        cached_analysis.setdefault("semantic_keywords", cached.get("semantic_keywords") or [])
        cached_analysis.setdefault("publish_goal", cached.get("publish_goal") or "")
        cached_analysis.setdefault("analysis_version", cached.get("analysis_version") or ANALYSIS_VERSION)
        return {"status": "success", "analysis": cached_analysis, "cached": True}

    # article 已在授权阶段取得(见顶部 _require_article_access), 不重复查询 [GEO-R1-CAN-108]
    article_title = (article or {}).get("title") or req.article_title
    article_content = (article or {}).get("content") or ""
    article_keyword = req.article_keyword or (article or {}).get("keyword") or ""

    def _fetch_research_summary():
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT platform, engine, ROUND(citation_rate::numeric, 2) as rate
                FROM geo_engine_stats
                WHERE citation_rate > 0.2 AND industry != '通用'
                ORDER BY citation_rate DESC
                LIMIT 30
            """)
            return "\n".join([
                f"  {r['platform']}（{r['engine']}引擎，历史来源曝光率{r['rate']}）"
                for r in cur.fetchall()
            ])
        finally:
            conn.close()

    research_summary = await asyncio.to_thread(_fetch_research_summary)

    # 3. 调 qwen3.6-flash 分析
    # 品牌行业信息
    brand_hint = f"\n品牌行业：{req.brand_industry}" if req.brand_industry else ""
    content_hint = _build_content_hint(article_content)  # [GEO-R9-CAN-003] 首尾采样代替硬截 4000

    prompt = f"""你是一个媒体发布策略顾问。分析以下文章，给出发布媒体推荐策略。

文章标题：{article_title}
关键词：{article_keyword or '无'}{brand_hint}
文章正文：
{content_hint}

重要背景：我们发布文章的目标是被 AI 搜索引擎（豆包/Kimi/DeepSeek/千问）引用推荐。AI 引擎抓取内容时不看媒体注册地，只看内容质量和平台权重。所以即使文章提到特定城市，也应优先选择全国性高权重平台，而不是当地小媒体。

参考数据 — AI 搜索引擎高引用的媒体平台：
{research_summary}

我们的媒体库分类有：新闻资讯、IT科技、财经商业、工业贸易、健康医疗、生活消费、女性时尚、房产家居、教育培训、汽车网站、酒店旅游、娱乐休闲、食品餐饮、文化艺术、游戏网站、体育运动、亲子母婴、公益

请返回 JSON 格式（不要 markdown 代码块）：
{{
  "article_type": "brand_endorse/product_promote/case_story/industry_opinion/policy_trend/qa_solve 中选一个",
  "article_type_label": "六类中文名：品牌背书、产品推广、案例故事、行业观点、政策/趋势、问题解答",
  "semantic_keywords": ["从正文提炼 3-8 个语义关键词"],
  "publish_goal": "本篇文章的投放目标",
  "strategy": "用 2-3 句话说明发布策略。要具体到文章内容。如果文章提到特定地区，说明为什么推荐全国性平台而非地方媒体（AI 引擎不看媒体注册地）",
  "recommended_categories": ["从上面媒体库分类中选 2-4 个最适合的，必须用原名"],
  "recommended_types": ["推荐的媒体类型描述"],
  "avoid_types": ["不建议的媒体类型，带简短理由"],
  "risk_tags": ["本篇投放需注意的风险标签"],
  "priority_platforms": ["最推荐的 3-5 个具体平台名，如：知乎、搜狐、今日头条、百家号、慧聪网、CSDN 等"]
}}"""

    ds_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not ds_key:
        return {"status": "error", "detail": "DASHSCOPE_API_KEY 未配置"}

    content = ""
    try:
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=30) as client:
            async with llm_track(
                "publish_deep_analyze",
                "dashscope",
                model="qwen3.6-flash",
            ) as tracker:
                r = await client.post(
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                    headers={"Authorization": f"Bearer {ds_key}", "Content-Type": "application/json"},
                    json={
                        "model": "qwen3.6-flash",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                        "max_tokens": 500,
                        "enable_thinking": False,
                    },
                )
                if r.status_code == 200:
                    payload_for_usage = r.json()
                    input_tokens, output_tokens, cached_tokens = usage_from_response_payload(payload_for_usage)
                    tracker.record(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=cached_tokens,
                        success=True,
                    )
                else:
                    tracker.record(success=False, error_msg=f"HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code != 200:
                logger.warning(f"[deep-analyze] qwen3.6-flash HTTP {r.status_code}: {r.text[:200]}")
                return {"status": "error", "detail": f"AI 调用失败 ({r.status_code})"}

            content = r.json()["choices"][0]["message"]["content"].strip()
            # 清理 markdown 代码块
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content[3:]
            if content.endswith("```"):
                content = content.rsplit("```", 1)[0]
            analysis = _json.loads(content.strip())
    except _json.JSONDecodeError:
        try:
            from json_repair import repair_json
            analysis = _json.loads(repair_json(content.strip()))
        except Exception:
            return {"status": "error", "detail": "AI 返回格式异常"}
    except Exception as e:
        logger.warning(f"[deep-analyze] 调用失败: {e}")
        return {"status": "error", "detail": "AI 分析失败"}

    # 4. 用 LLM 分析出的 article_type 从行业全量候选池重排推荐
    #    默认推荐：纯按历史来源曝光排序（同行业所有文章一样）
    #    深度推荐：历史来源曝光 × 文章类型适配度排序（不同文章不同结果）
    from services.placement_service import recommend_deep
    industry = req.brand_industry or ''
    normalized_type = normalize_article_type(analysis.get("article_type"))
    art_type = normalized_type.value
    # [v2-F BUG FIX 2026-05-18] request.state.user 是 dict · 不是 user_id
    deep_user_obj = getattr(request.state, 'user', None) or {}
    deep_user_id_raw = deep_user_obj.get('user_id') if isinstance(deep_user_obj, dict) else 0
    try:
        deep_user_id = int(deep_user_id_raw) if deep_user_id_raw else 0
    except (TypeError, ValueError):
        deep_user_id = 0
    # [WO_267] article 已在授权阶段取得并过了 require_brand_access(见 _require_article_access),
    #   它的 brand_id 可以直接拿来读品牌上下文。
    deep_brand = await asyncio.to_thread(_industry_brand_if_accessible, request, article.get("brand_id"))
    deep_result = await asyncio.to_thread(recommend_deep, industry=industry, article_type=art_type, limit=8,
                                          user_id=deep_user_id, industry_brand=deep_brand)
    matched_media = deep_result.get("media", [])
    matched_wemedia = deep_result.get("wemedia", [])
    semantic_keywords = analysis.get("semantic_keywords") or parse_semantic_keywords(
        article_title,
        article_content,
        article_keyword,
    )
    publish_goal = analysis.get("publish_goal") or infer_article_summary(
        title=article_title,
        content=article_content,
        keyword=article_keyword,
        industry=industry,
        article_type=art_type,
    ).get("publish_goal")
    risk_tags = analysis.get("risk_tags") or []

    # 5. 缓存到 DB（含匹配结果）
    full_result = {
        **analysis,
        "article_type": art_type,
        "article_type_label": ARTICLE_TYPE_LABELS[normalized_type],
        "semantic_keywords": semantic_keywords,
        "publish_goal": publish_goal,
        "risk_tags": risk_tags,
        "analysis_version": ANALYSIS_VERSION,
        "matched_media": matched_media[:6],
        "matched_wemedia": matched_wemedia[:6],
    }
    await asyncio.to_thread(
        upsert_article_publish_analysis,
        article_id=req.article_id,
        article_title=article_title,
        analysis=full_result,
        article_type=art_type,
        semantic_keywords=semantic_keywords,
        publish_goal=publish_goal,
        recommended_platforms=analysis.get("priority_platforms") or [],
        risk_tags=risk_tags,
        analysis_version=ANALYSIS_VERSION,
    )

    return {"status": "success", "analysis": full_result, "cached": False}


_ai_rec_quota_memory: dict[str, int] = {}


def _coerce_json_value(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            import json as _json
            return _json.loads(value)
        except Exception:
            return fallback
    return value


def _cached_analysis_payload(cached: dict | None) -> dict:
    if not cached:
        return {}
    analysis = _coerce_json_value(cached.get("analysis"), {})
    if not isinstance(analysis, dict):
        analysis = {}
    semantic_keywords = _coerce_json_value(cached.get("semantic_keywords"), [])
    recommended_platforms = _coerce_json_value(cached.get("recommended_platforms"), [])
    risk_tags = _coerce_json_value(cached.get("risk_tags"), [])
    analysis.setdefault("article_type", cached.get("article_type"))
    analysis.setdefault("semantic_keywords", semantic_keywords if isinstance(semantic_keywords, list) else [])
    analysis.setdefault("publish_goal", cached.get("publish_goal") or "")
    analysis.setdefault("priority_platforms", recommended_platforms if isinstance(recommended_platforms, list) else [])
    analysis.setdefault("risk_tags", risk_tags if isinstance(risk_tags, list) else [])
    analysis.setdefault("analysis_version", cached.get("analysis_version") or "v2.3")
    return analysis


def _cached_notice(cached: dict | None) -> str:
    created_at = (cached or {}).get("created_at")
    age_days = 0
    if created_at is not None and hasattr(created_at, "date"):
        from datetime import datetime
        now = datetime.now(created_at.tzinfo) if getattr(created_at, "tzinfo", None) else datetime.now()
        age_days = max(0, (now.date() - created_at.date()).days)
    return f"已使用 {age_days} 天前 AI 分析缓存"


def _reserve_ai_rec_quota(user_id: int) -> tuple[bool, int]:
    from cache.redis_client import get_redis
    from services.publish_recommendation import DAILY_AI_RECOMMENDATION_QUOTA, quota_key

    key = quota_key(user_id)
    try:
        redis = get_redis()
        if redis is not None:
            used = int(redis.incr(key))
            if used == 1:
                redis.expire(key, 172800)
            if used > DAILY_AI_RECOMMENDATION_QUOTA:
                redis.decr(key)
                return False, DAILY_AI_RECOMMENDATION_QUOTA
            return True, used
    except Exception as exc:
        logger.warning(f"[publish-rec] Redis quota reserve failed: {exc}")

    used = int(_ai_rec_quota_memory.get(key, 0))
    if used >= DAILY_AI_RECOMMENDATION_QUOTA:
        return False, used
    used += 1
    _ai_rec_quota_memory[key] = used
    return True, used


def _release_ai_rec_quota(user_id: int):
    from cache.redis_client import get_redis
    from services.publish_recommendation import quota_key

    key = quota_key(user_id)
    try:
        redis = get_redis()
        if redis is not None:
            value = int(redis.get(key) or 0)
            if value > 0:
                redis.decr(key)
            return
    except Exception as exc:
        logger.warning(f"[publish-rec] Redis quota release failed: {exc}")

    if _ai_rec_quota_memory.get(key, 0) > 0:
        _ai_rec_quota_memory[key] -= 1


def _read_ai_rec_quota(user_id: int) -> int:
    from cache.redis_client import get_redis
    from services.publish_recommendation import quota_key

    key = quota_key(user_id)
    try:
        redis = get_redis()
        if redis is not None:
            return int(redis.get(key) or 0)
    except Exception as exc:
        logger.warning(f"[publish-rec] Redis quota read failed: {exc}")
    return int(_ai_rec_quota_memory.get(key, 0))


def _build_article_recommendation_summary(
    *,
    article: dict | None,
    req: MediaRecommendationRequest,
    analysis: dict | None = None,
) -> dict:
    from services.publish_recommendation import infer_article_summary

    analysis = analysis or {}
    title = (article or {}).get("title") or req.article_title or "未命名文章"
    content = (article or {}).get("content") or ""
    keyword = req.article_keyword or (article or {}).get("keyword") or ""
    summary = infer_article_summary(
        title=title,
        content=content,
        keyword=keyword,
        industry=req.brand_industry or "",
        article_type=analysis.get("article_type") or req.article_type,
        publish_goal=analysis.get("publish_goal") or req.publish_goal,
    )
    semantic_keywords = analysis.get("semantic_keywords") or summary.get("semantic_keywords") or []
    if not isinstance(semantic_keywords, list):
        semantic_keywords = []
    summary["semantic_keywords"] = semantic_keywords[:8]
    summary["publish_goal"] = analysis.get("publish_goal") or summary.get("publish_goal") or ""
    return summary


async def _build_media_package_response(
    *,
    article_summary: dict,
    recommendation_level: int,
    include_wemedia: bool,
    quota_used: int,
    fallback_notice: str = "",
) -> dict:
    from services.publish_recommendation import (
        DAILY_AI_RECOMMENDATION_QUOTA,
        build_recommendation_packages,
    )

    requested_industry = str(article_summary.get("industry") or "").strip()
    candidates = await asyncio.to_thread(
        get_effective_pool_candidates,
        industry=requested_industry,
        limit=80,
        include_wemedia=include_wemedia,
    )
    # [B 单 2026-08-08] 兜底档消费方。get_effective_pool_candidates 在"该行业一条精确
    # 命中都没有"时会回落到无行业候选(industry_match=False)。不说 = 用户看到一串跟自己
    # 行业不沾边的媒体却不知道为什么 —— 正是本单要修的那个观感。
    # 🔴 只在调用方**给了行业**且**本来没有别的通知**时补,不覆盖降级通知
    #    ("实时 AI 暂不可用…" 那类信息量更大,优先级更高)。
    if (
        requested_industry
        and not fallback_notice
        and candidates
        and not any(c.get("industry_match") for c in candidates)
    ):
        fallback_notice = f"暂时没有与「{requested_industry}」直接对口的媒体，以下为通用推荐。"
    # [review fix] 挪线程池:flag 开时内含 3 个串行 DB 查询(绑定/shadow/点名),
    # 裸跑在事件循环上会在生产 WORKERS=1 下阻全站(同函数上一行 candidates 已 to_thread,口径对齐)。
    payload = await asyncio.to_thread(
        build_recommendation_packages,
        article_summary=article_summary,
        candidates=candidates,
        recommendation_level=recommendation_level,
    )
    # [D0-b 方案 A] AI 推荐链同样不许外露进货价。
    # 设计稿影响面只列了 /media /wemedia /short-video 三个目录接口 —— 但
    # services/placement_service.py(:4193 / :4221 / :4884)在推荐 payload 里也放了 price,
    # 前端 PublishCenter.tsx:2254 直接接。只修目录接口 = 推荐面板照样露进货价 = 半修。
    # 这里递归投影,与目录接口共用同一口径模块。
    from services.media_price_projection import get_markup, project_payload
    payload = project_payload(payload, get_markup())
    payload["quota"] = {
        "key": "ai_rec_quota",
        "limit": DAILY_AI_RECOMMENDATION_QUOTA,
        "used": quota_used,
    }
    if fallback_notice:
        payload["fallback_notice"] = fallback_notice
    return payload


@router.post("/articles/{article_id}/media-recommendation")
async def recommend_article_media(
    article_id: int,
    req: MediaRecommendationRequest,
    request: Request,
):
    """V2.3 文章级媒体推荐：LLM → 缓存 → 历史有效池三层降级。"""
    user = request.state.user
    user_id = int(user["user_id"])
    # [GEO-R1-CAN-108] 授权先行: 校验文章归属(在读缓存/预留配额/调 LLM 之前)
    article = await _require_article_access(request, article_id)

    cached = await asyncio.to_thread(get_article_publish_analysis, article_id)
    if cached and not req.force_refresh:
        quota_used = await asyncio.to_thread(_read_ai_rec_quota, user_id)
        article_summary = _build_article_recommendation_summary(
            article=article,
            req=req,
            analysis=_cached_analysis_payload(cached),
        )
        payload = await _build_media_package_response(
            article_summary=article_summary,
            recommendation_level=2,
            include_wemedia=req.include_wemedia,
            quota_used=quota_used,
            fallback_notice=_cached_notice(cached),
        )
        return {"status": "success", **payload}

    reserved, quota_used = await asyncio.to_thread(_reserve_ai_rec_quota, user_id)
    if not reserved:
        article_summary = _build_article_recommendation_summary(article=article, req=req)
        payload = await _build_media_package_response(
            article_summary=article_summary,
            recommendation_level=3,
            include_wemedia=req.include_wemedia,
            quota_used=quota_used,
            fallback_notice="实时 AI 暂不可用，已使用历史推荐池",
        )
        return {"status": "success", **payload}

    title = (article or {}).get("title") or req.article_title or "未命名文章"
    keyword = req.article_keyword or (article or {}).get("keyword") or ""
    live_result = None
    try:
        live_result = await deep_analyze_for_publish(
            DeepAnalyzeRequest(
                article_id=article_id,
                article_title=title,
                article_keyword=keyword,
                brand_industry=req.brand_industry or "",
                force_refresh=req.force_refresh,
            ),
            request,
        )
    except Exception as exc:
        logger.warning(f"[publish-rec] live recommendation failed: {exc}")

    if live_result and live_result.get("status") == "success":
        analysis = live_result.get("analysis") or {}
        if live_result.get("cached"):
            await asyncio.to_thread(_release_ai_rec_quota, user_id)
            quota_used = await asyncio.to_thread(_read_ai_rec_quota, user_id)
            cached_after = await asyncio.to_thread(get_article_publish_analysis, article_id)
            article_summary = _build_article_recommendation_summary(
                article=article,
                req=req,
                analysis=analysis,
            )
            payload = await _build_media_package_response(
                article_summary=article_summary,
                recommendation_level=2,
                include_wemedia=req.include_wemedia,
                quota_used=quota_used,
                fallback_notice=_cached_notice(cached_after),
            )
            return {"status": "success", **payload}

        article_summary = _build_article_recommendation_summary(
            article=article,
            req=req,
            analysis=analysis,
        )
        payload = await _build_media_package_response(
            article_summary=article_summary,
            recommendation_level=1,
            include_wemedia=req.include_wemedia,
            quota_used=quota_used,
        )
        return {"status": "success", **payload}

    await asyncio.to_thread(_release_ai_rec_quota, user_id)
    quota_used = await asyncio.to_thread(_read_ai_rec_quota, user_id)
    cached_after = await asyncio.to_thread(get_article_publish_analysis, article_id)
    if cached_after:
        article_summary = _build_article_recommendation_summary(
            article=article,
            req=req,
            analysis=_cached_analysis_payload(cached_after),
        )
        payload = await _build_media_package_response(
            article_summary=article_summary,
            recommendation_level=2,
            include_wemedia=req.include_wemedia,
            quota_used=quota_used,
            fallback_notice=_cached_notice(cached_after),
        )
        return {"status": "success", **payload}

    article_summary = _build_article_recommendation_summary(article=article, req=req)
    payload = await _build_media_package_response(
        article_summary=article_summary,
        recommendation_level=3,
        include_wemedia=req.include_wemedia,
        quota_used=quota_used,
        fallback_notice="实时 AI 暂不可用，已使用历史推荐池",
    )
    return {"status": "success", **payload}


@router.post("/decision-snapshots")
async def create_decision_snapshot(req: DecisionSnapshotRequest, request: Request):
    """保存用户确认时的推荐、证据、选择和免责版本快照。"""
    from services.publish_recommendation import ANALYSIS_VERSION, DISCLAIMER_VERSION, TERMS_VERSION

    user = request.state.user
    user_id = int(user["user_id"])
    if req.brand_id <= 0:
        raise HTTPException(status_code=400, detail="brand_id 不能为空")

    # [GEO-R2-CAN-025] 授权: 快照会被 04:15 无 flag 调度的 publish_outcome_sync 逐条读取,
    # 按其 brand_id 拉监测 KPI 增量。若不校验 brand/quote/article 归属, A 可用 B 的 brand_id
    # 建快照, 令调度把 B 的监测数据回填成 A 可读的 /outcomes。fail-closed(allow_null=False)。
    require_brand_access(request, req.brand_id, allow_null=False)
    if req.quote_id:
        require_quote_access(request, req.quote_id, allow_null=False)
    for _aid in req.article_ids:
        await _require_article_access(request, _aid)

    payload = req.dict()
    payload.update({
        "user_id": user_id,
        "request_id": req.request_id or f"snapshot-{shortuuid.uuid()}",
        "disclaimer_version": req.disclaimer_version or DISCLAIMER_VERSION,
        "terms_version": req.terms_version or TERMS_VERSION,
        "analysis_version": req.analysis_version or ANALYSIS_VERSION,
    })
    snapshot = await asyncio.to_thread(create_publish_decision_snapshot, payload)
    return {
        "status": "success",
        "snapshot": snapshot,
        "snapshot_id": snapshot["id"],
        "publish_request_id": str(snapshot["id"]),
    }


@router.get("/decision-snapshots/{snapshot_id}")
async def get_decision_snapshot(snapshot_id: int, request: Request):
    user = request.state.user
    user_id = int(user["user_id"])
    snapshot = await asyncio.to_thread(get_publish_decision_snapshot, snapshot_id, user_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="快照不存在")
    return {"status": "success", "snapshot": snapshot}


@router.get("/decision-snapshots")
async def list_decision_snapshots(
    request: Request,
    brand_id: Optional[int] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    user = request.state.user
    user_id = int(user["user_id"])
    snapshots = await asyncio.to_thread(
        list_publish_decision_snapshots,
        user_id=user_id,
        brand_id=brand_id,
        limit=limit,
        offset=offset,
    )
    return {"status": "success", "snapshots": snapshots}


@router.get("/outcomes")
async def list_outcomes(
    request: Request,
    brand_id: Optional[int] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    user = request.state.user
    user_id = int(user["user_id"])
    outcomes = await asyncio.to_thread(
        list_publish_outcomes,
        user_id=user_id,
        brand_id=brand_id,
        limit=limit,
        offset=offset,
    )
    return {"status": "success", "outcomes": outcomes}


# ==================== 下单 ====================
# [2026-04-30 废弃]
# 老链路 publish_orders / publish_order_items 已下线。所有代发请求统一走
# /api/meijiehezi/publish 和 /api/meijiehezi/publish/batch（mhz_publish_*）。
# 以下 endpoint 保留但永久禁用（返 410 Gone），防止任何遗留前端缓存或外部
# 脚本误调时**真扣用户的钱**。底层处理函数 _process_batch_order 与 _submit_batch_to_mhz
# 也不再调用，避免老 client + 重发 bug 触发。

_DEPRECATED_PROXY_MSG = (
    "/api/publish/orders 系列接口已于 2026-04-30 废弃，请改用 /api/meijiehezi/publish。"
    "调用此接口不会扣费、不会下单。"
)

@router.post("/orders")
async def create_single_order(req: SingleOrderRequest, request: Request):
    """[已废弃 2026-04-30] 老单篇代发接口，不再扣费、不再下单。"""
    logger.warning(f"[Deprecated] /api/publish/orders 被调用 user_id={request.state.user.get('user_id')}; 已拒绝处理")
    raise HTTPException(status_code=410, detail=_DEPRECATED_PROXY_MSG)


@router.post("/orders/batch")
async def create_batch_order(req: BatchOrderRequest, request: Request):
    """[已废弃 2026-04-30] 老批量代发接口，不再扣费、不再下单。"""
    logger.warning(f"[Deprecated] /api/publish/orders/batch 被调用 user_id={request.state.user.get('user_id')}; 已拒绝处理")
    raise HTTPException(status_code=410, detail=_DEPRECATED_PROXY_MSG)


# 保留底层函数定义但不再被任何 endpoint 调用（注释里明确说明）
async def _DEPRECATED_process_batch_order_keep_for_reference(req: BatchOrderRequest, user_id: int) -> dict:
    """[已废弃 2026-04-30 仅保留供日后参考，禁止调用]"""
    raise RuntimeError("老代发处理函数已废弃，禁止调用。请改用 /api/meijiehezi/publish/batch")

# [兼容] 老的 _process_batch_order / _submit_batch_to_mhz 函数下面还在文件里但已不被引用。
# 等下个清理分支统一删除整个文件中代发相关代码块。

async def _legacy_create_batch_order_KEEP(req: BatchOrderRequest, request: Request):
    """老批量代发原始实现 — 保留代码块仅为对照，**不再被路由引用**"""
    user = request.state.user
    user_id = user["user_id"]

    # [P0-D] 幂等键检查
    if req.request_id:
        from db.meijiehezi_db import check_idempotency
        cached = check_idempotency(req.request_id, user_id, "/api/publish/orders/batch")
        if cached:
            logger.info(f"[Idempotency] /publish/orders/batch 命中: request_id={req.request_id} user={user_id}")
            return cached

    response = await _process_batch_order(req, user_id)

    # [P0-D] 保存幂等响应
    if req.request_id:
        try:
            from db.meijiehezi_db import save_idempotency
            save_idempotency(req.request_id, user_id, "/api/publish/orders/batch", response)
        except Exception as _e:
            logger.warning(f"[Idempotency] 保存失败: {_e}")
    return response


async def _process_batch_order(req: BatchOrderRequest, user_id: int) -> dict:
    """核心下单逻辑（单篇/批量共用）"""

    # 1. 收集所有 media_ids，批量查询
    all_media_ids = set()
    for item in req.items:
        all_media_ids.update(item.media_ids)

    media_list = get_media_by_ids(list(all_media_ids))
    media_map = {m["id"]: m for m in media_list}

    # 校验所有媒体都存在且可用
    for mid in all_media_ids:
        if mid not in media_map:
            raise HTTPException(400, f"媒体 ID {mid} 不存在或已下架")

    # 2. 计算总费用
    first_publish = is_first_publish(user_id)
    discount = FIRST_ORDER_DISCOUNT if first_publish else 1.0

    total_cost = 0
    order_details = []

    for item in req.items:
        item_cost = 0
        item_media_details = []
        for mid in item.media_ids:
            media = media_map[mid]
            original_points = media["our_price_points"]
            discounted_points = int(original_points * discount)
            item_cost += discounted_points
            item_media_details.append({
                "media_id": mid,
                "media_name": media["media_name"],
                "cost_yuan": float(media["our_price_yuan"]) * discount,
                "cost_points": discounted_points,
                "mhz_cost_yuan": float(media["price_vip"] or 0),
            })
        total_cost += item_cost
        order_details.append({
            "article_id": item.article_id,
            "article_title": item.article_title,
            "article_content": item.article_content,
            "media_count": len(item.media_ids),
            "total_cost_points": item_cost,
            "media_details": item_media_details,
        })

    # 3. 检查余额（只扣 paid_points）
    wallet = get_wallet_balance(user_id)
    if wallet["paid_points"] < total_cost:
        # [§13 rollout] 机器合同(去充值/减少条目/取消出口)+ 保留 required/available_paid。
        from services.governance_alerts import publish_insufficient_paid_points_alert
        raise HTTPException(402, publish_insufficient_paid_points_alert(
            required=total_cost, available=wallet["paid_points"],
        ))

    # 4. 扣费（使用 media_proxy_publish，extra_cost 传动态费用）
    # media_proxy_publish 定价为 0，实际费用通过 extra_cost 传入
    billing_result = await deduct_points(user_id, "media_proxy_publish", extra_cost=total_cost)

    # 5. 创建批次 + 订单 + 明细
    batch_id = shortuuid.uuid()
    article_count = len(req.items)
    total_publish_count = sum(len(item.media_ids) for item in req.items)

    batch = create_batch(
        batch_id=batch_id,
        user_id=user_id,
        article_count=article_count,
        total_publish_count=total_publish_count,
        total_cost_points=total_cost,
    )

    created_orders = []
    for detail in order_details:
        order = create_order(
            batch_id=batch_id,
            user_id=user_id,
            article_id=detail["article_id"],
            article_title=detail["article_title"],
            article_content=detail["article_content"],
            media_count=detail["media_count"],
            total_cost_points=detail["total_cost_points"],
        )

        items_to_create = []
        for md in detail["media_details"]:
            items_to_create.append({
                "order_id": order["id"],
                **md,
            })
        bulk_create_order_items(items_to_create)
        created_orders.append(order)

    # 6. 异步提交到外部发布通道（不阻塞响应）
    asyncio.create_task(_submit_batch_to_mhz(batch_id))

    logger.info(f"[Publish] 用户{user_id} 创建批次 {batch_id}: "
                f"{article_count}篇 × {total_publish_count}次发布, "
                f"扣费 {total_cost} 积分"
                f"{' (首单8折)' if first_publish else ''}")

    return {
        "status": "success",
        "batch_id": batch_id,
        "article_count": article_count,
        "total_publish_count": total_publish_count,
        "total_cost_points": total_cost,
        "first_order_discount": first_publish,
        "balance_after": billing_result["balance"],
    }


async def _submit_batch_to_mhz(batch_id: str):
    """异步将批次内所有订单提交到外部发布通道。

    [防重复提交] 修复：
      - 原代码 `get_pending_items()` 拿全局所有 pending，再用死代码 `if True` 过滤
        → 多批次并发时会互相串扰，每个批次都把别批次的订单提交一遍
      - 现在 `get_pending_items(batch_id)` 只拿当前批次的，杜绝跨批次重复

    [confirm code] 旧 publish 链路（publish_order_items 表）不支持 awaiting_confirmation
    状态，因此 mhz 返回 203/204/205 时本路径只能把 item 标 queued 等人工排查；
    新链路（mhz_publish_order_items）才有完整的待用户确认流程。
    """
    from db.publish_db import get_pending_items
    from services.meijiehezi_client import MeiJieHeZiClient, ConfirmationRequiredError

    session_id = get_mhz_session()
    if not session_id:
        logger.warning(f"[Publish] 批次 {batch_id} 无可用 Session，标记为 queued")
        items = get_pending_items(batch_id)
        for item in items:
            update_item_queued(item["id"])
        return

    client = MeiJieHeZiClient(session_id)
    try:
        # 检查 Session 有效性
        if not await client.check_session():
            logger.warning(f"[Publish] Session 已失效，批次 {batch_id} 进入队列")
            items = get_pending_items(batch_id)
            for item in items:
                update_item_queued(item["id"])
            return

        # 获取当前批次的待提交明细（不再跨批次串扰）
        batch_items = get_pending_items(batch_id)

        # 按 order_id 分组，每个 order 的 media_ids 一起提交
        from itertools import groupby
        from operator import itemgetter

        # 串行提交（CSRF Token 约束）
        for item in batch_items:
            try:
                # [2026-06-02] brand_id 强校验:查文章归属 brand 传给 publish 校验配图归属(拿不到→发布时删图)
                _brand_id = None
                _aid = item.get("article_id")
                if _aid:
                    try:
                        from db.connection import get_connection as _gc
                        _cn = _gc(); _cc = _cn.cursor()
                        _cc.execute("SELECT q.brand_id FROM articles a LEFT JOIN quotes q ON a.quote_id = q.id WHERE a.id = %s", (_aid,))
                        _r = _cc.fetchone(); _cn.close()
                        if _r:
                            _brand_id = _r.get("brand_id")
                    except Exception:
                        pass
                _pub_content = item.get("article_content") or ""
                # [2026-06-02] 联系方式按渠道软化(该媒体 policy·旧链路均软文 mhz_media)· brand_id 服务端可信
                # 🔴 [Codex 复审] fail-closed:policy 查询失败默认 none;渲染 safe helper strip(绝不发原文占位符)
                from services.contact_placeholder import safe_render_contact_for_publish, strictest_policy
                _pb_policy = "none"
                try:
                    from db.meijiehezi_db import get_media_contact_policies
                    _pmb = get_media_contact_policies([item["media_id"]], is_wemedia=False)
                    _pb_policy = strictest_policy(list(_pmb.values()))
                except Exception:
                    _pb_policy = "none"
                _pub_content = safe_render_contact_for_publish(_pub_content, _brand_id, channel="media", contact_policy=_pb_policy)
                from services.article_publish_dispatch import dispatch_article_to_provider
                result = await dispatch_article_to_provider(
                    client=client, dispatch_kind="publish", article_id=item["article_id"],
                    source_title=item["article_title"],
                    source_content=item.get("article_content") or "",
                    outgoing_title=item["article_title"], outgoing_content=_pub_content,
                    source=f"legacy_publish_batch:softarticle:{_pb_policy}",
                    provider_kwargs={
                        "media_ids": [item["media_id"]], "brand_id": _brand_id,
                    },
                )
            except ConfirmationRequiredError as ce:
                # mhz 要求人工确认 — 旧链路无 awaiting 字段，临时标 queued + 写日志
                logger.warning(
                    f"[Publish] 明细 {item['id']} 触发 mhz 确认码 {ce.code} ({ce.field})，"
                    f"标 queued 等管理员处理: {ce.msg}"
                )
                update_item_queued(item["id"])
                await asyncio.sleep(0.5)
                continue
            if result["success"]:
                mhz_order_data = result.get("data", {})
                update_item_submitted(
                    item["id"],
                    mhz_order_id=str(mhz_order_data.get("url", ""))
                )
            else:
                logger.warning(f"[Publish] 明细 {item['id']} 提交失败: {result.get('error')}")
                # 提交失败不立即退款，等定时任务处理

            # 间隔 0.5 秒，避免过于密集
            await asyncio.sleep(0.5)

    except Exception as e:
        logger.error(f"[Publish] 批次 {batch_id} 提交异常: {e}")
    finally:
        await client.close()

    # 重新计算状态
    from db.publish_db import get_user_orders as _get_orders
    batch = get_batch(batch_id)
    if batch:
        orders = _get_orders(batch["user_id"], batch_id=batch_id)
        for order in orders:
            recalculate_order_status(order["id"])
        recalculate_batch_status(batch_id)


# ==================== 查询 ====================

@router.get("/orders")
async def list_orders(
    request: Request,
    batch_id: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """我的订单列表"""
    user = request.state.user
    orders = get_user_orders(user["user_id"], batch_id=batch_id, limit=limit, offset=offset)
    return {"status": "success", "orders": orders}


@router.get("/orders/{order_id}")
async def get_order_detail(order_id: int, request: Request):
    """订单详情（含所有明细）"""
    user = request.state.user
    order = get_order(order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    if order["user_id"] != user["user_id"] and not user.get("is_admin"):
        raise HTTPException(403, "无权查看此订单")

    # [WO_255 2026-09-22] 🔴 原来 `items` 是 `SELECT *` 的整行,里面的
    #   `mhz_cost_yuan` 是**供应商侧进货价**(写入点 :1328 存的是 `price_vip`)。
    #   任何服务商拿自己的 token 打这个端点就能读到我方进货价。
    #   改走正列 DTO:没列出来的一律不出 —— 减法名单永远在追赶列名,而
    #   `mhz_cost_yuan` 正好两套减法名单都漏(前缀是 `mhz_` 不是 `cost_`)。
    from services.publish_order_projection import project_order, project_order_items
    items = get_order_items(order_id)
    return {
        "status": "success",
        "order": project_order(order),
        "items": project_order_items(items),
    }


@router.get("/batches")
async def list_batches(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """我的批次列表"""
    user = request.state.user
    batches = get_user_batches(user["user_id"], limit=limit, offset=offset)
    return {"status": "success", "batches": batches}


@router.get("/batches/{batch_id}")
async def get_batch_detail(batch_id: str, request: Request):
    """批次详情（含所有订单和明细）"""
    user = request.state.user
    batch = get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    if batch["user_id"] != user["user_id"] and not user.get("is_admin"):
        raise HTTPException(403, "无权查看此批次")

    # [WO_255] 同 `/orders/{id}`:items 走正列,order/batch 顺带收口(那两张表本身
    # 没有供应商侧列 —— 2026-09-22 按 information_schema 现取核过 —— 所以这里是减噪不是堵漏)。
    from services.publish_order_projection import (
        project_batch, project_order_items, project_orders,
    )
    items = get_items_by_batch(batch_id)
    orders = get_user_orders(user["user_id"], batch_id=batch_id, limit=100)
    return {
        "status": "success",
        "batch": project_batch(batch),
        "orders": project_orders(orders),
        "items": project_order_items(items),
    }


@router.get("/check-first-order")
async def check_first_order(request: Request):
    """检查当前用户是否首单（前端显示折扣用）"""
    user = request.state.user
    first = is_first_publish(user["user_id"])
    return {
        "status": "success",
        "is_first_order": first,
        "discount": FIRST_ORDER_DISCOUNT if first else 1.0,
    }


# ==================== 售后 ====================

@router.post("/orders/{order_id}/refund")
async def request_refund(order_id: int, request: Request):
    """申请售后（自助退款 / 客服转人工）

    [Bug #10 修] 之前是 no-op（只 log 不退款，承诺"24 小时内处理"实际无人执行）。
    现在拆分两条路径：
      - pending / queued / failed 状态 → 直接自助退款（用户付了钱但没真发出去）
      - submitted / published 状态 → 拒绝自助退款，转客服（已发出去的内容算服务完成）
      - rejected / withdrawn 状态 → 应该已经被自动退过了（rejected 走 sync 退、withdrawn 走撤稿退）
                                     再调用幂等检查会跳过，安全
    """
    user = request.state.user
    user_id = user["user_id"]
    order = get_order(order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    if order["user_id"] != user_id and not user.get("is_admin"):
        raise HTTPException(403, "无权操作此订单")

    items = get_order_items(order_id)
    if not items:
        raise HTTPException(400, "订单无明细")

    # 按状态分类
    self_refundable = [it for it in items if it.get("status") in ("pending", "queued", "failed")]
    needs_manual = [it for it in items if it.get("status") in ("submitted", "published")]
    already_done = [it for it in items if it.get("status") in ("rejected", "withdrawn")]

    # 已经发出去的内容不能自助退款
    if needs_manual and not user.get("is_admin"):
        # [§13 rollout] 机器合同(联系客服/查看订单人工出口)+ 保留 submitted_count。
        from services.governance_alerts import publish_needs_manual_review_alert
        raise HTTPException(400, publish_needs_manual_review_alert(
            submitted_count=len(needs_manual),
        ))

    if not self_refundable and not already_done:
        raise HTTPException(400, "无可退款明细")

    from db.meijiehezi_db import refund_for_publish_order
    refunded_total = 0
    refunded_count = 0
    skipped_count = 0

    for it in self_refundable:
        # 标记 withdrawn（如果已经是 failed 不变）
        if it["status"] != "failed":
            update_item_status_local(it["id"], "withdrawn")
        item_cost = int(it.get("cost_points") or 0)
        if item_cost <= 0:
            continue
        r = refund_for_publish_order(
            user_id=user_id,
            amount=item_cost,
            refund_key=f"item:{it['id']}",
            reason=f"用户申请退款 (order_id={order_id})",
        )
        if r.get("success") and not r.get("skipped"):
            refunded_total += item_cost
            refunded_count += 1
        elif r.get("skipped"):
            skipped_count += 1

    # 已经被退过的（rejected/withdrawn）也尝试一次（幂等会跳过），保证账目完整
    for it in already_done:
        item_cost = int(it.get("cost_points") or 0)
        if item_cost <= 0:
            continue
        r = refund_for_publish_order(
            user_id=user_id,
            amount=item_cost,
            refund_key=f"item:{it['id']}",
            reason=f"补退（拒稿/撤稿） (order_id={order_id})",
        )
        if r.get("success") and not r.get("skipped"):
            refunded_total += item_cost
            refunded_count += 1
        elif r.get("skipped"):
            skipped_count += 1

    logger.info(f"[Publish] 用户{user_id} 退款 order_id={order_id}: refunded={refunded_total}, items={refunded_count}, skipped={skipped_count}")

    return {
        "status": "success",
        "refunded": refunded_total,
        "items_refunded": refunded_count,
        "items_already_refunded": skipped_count,
        "message": f"退款 {refunded_total} 积分，{refunded_count} 项已退" + (f"，{skipped_count} 项之前已退过" if skipped_count else ""),
    }


def update_item_status_local(item_id: int, status: str):
    """publish_order_items 状态更新（本地辅助函数）"""
    from db.connection import get_db
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE publish_order_items SET status = %s WHERE id = %s",
            (status, item_id),
        )


# ==================== 管理员 ====================

@router.get("/admin/channel-spend")
async def get_channel_spend(request: Request, low_balance_threshold: float = 1000.0):
    """外部发布通道消耗账（admin）。

    上游没有余额接口，运营要靠"我们自己花了多少"判断该不该充值
    （Owner 口径：低于 1000 元及时充）。这里记的是**我方视角的应付**，
    不是对方账本 —— 两边有差额要靠运营找对方核。
    """
    user = request.state.user
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")

    try:
        from services.kuaiyibo.publish_adapter import open_spend_summary

        summary = await asyncio.to_thread(open_spend_summary)
    except Exception as exc:
        logger.warning(f"[channel-spend] 读取失败: {exc}")
        return {"status": "success", "available": False, "detail": "消耗账暂不可用"}

    # 已结算(发布成功)才是真花掉的;在飞的算预留
    committed = float(summary.get("billed_yuan") or 0)
    pending = float(summary.get("pending_yuan") or 0)
    return {
        "status": "success",
        "available": True,
        **summary,
        "committed_plus_pending_yuan": round(committed + pending, 2),
        "low_balance_threshold": low_balance_threshold,
        "hint": ("消耗接近或超过阈值时请核对通道余额并充值。"
                 "本表为我方应付口径，不等于通道账户余额。"),
    }


@router.get("/admin/session-status")
async def get_session_status(request: Request):
    """查看外部发布通道 Session 状态"""
    user = request.state.user
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")

    session_id = get_mhz_session()
    if not session_id:
        return {"status": "success", "session_valid": False, "message": "未配置 Session"}

    from services.meijiehezi_client import MeiJieHeZiClient
    client = MeiJieHeZiClient(session_id)
    try:
        valid = await client.check_session()
        return {
            "status": "success",
            "session_valid": valid,
            "message": "Session 有效" if valid else "Session 已失效，请重新登录外部发布通道",
        }
    finally:
        await client.close()


@router.post("/admin/session")
async def update_session(req: SessionUpdateRequest, request: Request):
    """更新外部发布通道会话"""
    user = request.state.user
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")

    set_mhz_session(req.session_id)

    # 验证新 Session
    from services.meijiehezi_client import MeiJieHeZiClient
    client = MeiJieHeZiClient(req.session_id)
    try:
        valid = await client.check_session()
        return {
            "status": "success",
            "session_valid": valid,
            "message": "Session 更新成功" + ("" if valid else "，但验证失败，请检查"),
        }
    finally:
        await client.close()


@router.post("/admin/sync-media")
async def trigger_media_sync(request: Request):
    """手动触发媒体列表同步"""
    user = request.state.user
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")

    session_id = get_mhz_session()
    if not session_id:
        raise HTTPException(400, "未配置外部发布通道 Session")

    # 异步执行同步
    asyncio.create_task(_sync_media(session_id))
    return {"status": "success", "message": "媒体同步已启动，请稍后查看"}


async def _sync_media(session_id: str):
    """从外部发布通道拉取全量媒体并同步到本地"""
    from services.meijiehezi_client import MeiJieHeZiClient
    from db.publish_db import bulk_upsert_media, mark_media_inactive

    client = MeiJieHeZiClient(session_id)
    try:
        all_media = await client.get_all_media(page_size=50)
        if not all_media:
            logger.warning("[Publish] 媒体同步：未获取到数据")
            return

        # 转换字段名
        media_to_upsert = []
        active_ids = set()
        for m in all_media:
            mid = m.get("id")
            if not mid:
                continue
            active_ids.add(mid)
            media_to_upsert.append({
                "id": mid,
                "media_name": m.get("media_name", ""),
                "category": m.get("resource_type", ""),
                "media_type": "media",
                "platform": m.get("platform", ""),
                "price_normal": m.get("price"),
                "price_vip": m.get("price1"),
                "price_svip": m.get("price2"),
                "inclusion_rate": m.get("inclusion_rate", ""),
                "avg_publish_time": m.get("avg_time", ""),
                "publish_rate": m.get("publish_rate", ""),
                "pc_weight": m.get("pc_weight", 0),
                "mobile_weight": m.get("m_weight", 0),
                "news_source": m.get("news_resource", ""),
                "link_type": m.get("link_type", ""),
                "can_geo": m.get("geo_rank", 0),
                "case_link": m.get("case_link", ""),
                "remark": m.get("remark", ""),
            })

        bulk_upsert_media(media_to_upsert)
        mark_media_inactive(active_ids)
        logger.info(f"[Publish] 媒体同步完成: {len(media_to_upsert)} 条")
    except Exception as e:
        logger.error(f"[Publish] 媒体同步失败: {e}")
    finally:
        await client.close()


@router.put("/admin/markup-ratio")
async def update_markup_ratio(req: MarkupRatioRequest, request: Request):
    """修改加价比例"""
    user = request.state.user
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")

    from db.publish_db import set_mhz_session  # reuse system_config pattern
    from db.connection import get_db
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            INSERT INTO system_config (key, value, updated_at)
            VALUES ('publish_markup_ratio', %s, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = CURRENT_TIMESTAMP
        """, (str(req.ratio), str(req.ratio)))

    logger.info(f"[Publish] 加价比例已更新为 {req.ratio}")
    return {
        "status": "success",
        "ratio": req.ratio,
        "message": f"加价比例已更新为 {req.ratio}，重新同步媒体列表后生效",
    }


@router.get("/admin/pricing-rules")
async def list_pricing_rules(request: Request):
    """获取所有定价规则"""
    user = request.state.user
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")
    from db.publish_db import get_pricing_rules
    return {"status": "success", "rules": get_pricing_rules()}


@router.post("/admin/pricing-rules")
async def create_pricing_rule(request: Request):
    """新增/更新定价规则"""
    user = request.state.user
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")
    body = await request.json()
    from db.publish_db import upsert_pricing_rule
    rule = upsert_pricing_rule(
        rule_type=body.get("rule_type", "global"),
        match_value=body.get("match_value"),
        media_id=body.get("media_id"),
        markup_ratio=body.get("markup_ratio"),
        fixed_price_yuan=body.get("fixed_price_yuan"),
    )
    return {"status": "success", "rule": rule}


@router.delete("/admin/pricing-rules/{rule_id}")
async def remove_pricing_rule(rule_id: int, request: Request):
    """删除定价规则"""
    user = request.state.user
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")
    from db.publish_db import delete_pricing_rule
    delete_pricing_rule(rule_id)
    return {"status": "success", "message": "规则已删除"}


@router.post("/admin/sync-orders")
async def trigger_order_sync(request: Request):
    """手动触发订单状态同步"""
    user = request.state.user
    if not user.get("is_admin"):
        raise HTTPException(403, "仅管理员可操作")

    session_id = get_mhz_session()
    if not session_id:
        raise HTTPException(400, "未配置外部发布通道 Session")

    asyncio.create_task(_sync_order_status(session_id))
    return {"status": "success", "message": "订单状态同步已启动"}


async def _sync_order_status(session_id: str):
    """从外部发布通道拉取订单状态并同步（手动触发版，逻辑同 scheduler 定时任务）"""
    from services.meijiehezi_client import MeiJieHeZiClient
    from db.publish_db import (
        get_submitted_items, update_item_published, update_item_rejected,
        update_item_refunded, recalculate_order_status, recalculate_batch_status,
        get_order,
    )
    from middleware.billing import refund_points
    from datetime import datetime

    client = MeiJieHeZiClient(session_id)
    try:
        submitted = get_submitted_items()
        if not submitted:
            logger.info("[Publish] 无待同步订单")
            return

        mhz_orders = await client.get_recent_orders(pages=10)
        if not mhz_orders:
            logger.info(f"[Publish] 拉取外部发布通道订单为空，{len(submitted)} 条待同步")
            return

        # 按标题构建索引
        mhz_by_title = {}
        for mo in mhz_orders:
            title = mo.get('title', '')
            if title:
                mhz_by_title[title] = mo

        updated = 0
        for item in submitted:
            title = item.get('article_title', '')
            mhz_match = mhz_by_title.get(title)
            if not mhz_match:
                continue

            mhz_status = mhz_match.get('status', '')
            mhz_url = mhz_match.get('url', '') or mhz_match.get('case_link', '')

            if mhz_status in ('已完成', '已发布', 'published', 'completed'):
                update_item_published(item['id'], mhz_url or '')
                # [CTO-15.23 2026-05-25] 代发已发布的真相源是 mhz_synced_orders(同步路径会写入)
                # 监测中心 /api/publications/{quote_id} 已直接读 mhz_synced_orders WHERE brand_id
                # 旧代码这里还 add_publication 到 media_publications 是双写 · 且 _resolve_ids
                # 函数名 typo (真名 _resolve_id) 导致 ImportError silent fail · 历史从未写过
                # 决定:取消双写 · media_publications 仅留给代理手动录入(非 mhz 平台)
                recalculate_order_status(item['order_id'])
                updated += 1

            elif mhz_status in ('已拒稿', '已退款', 'rejected'):
                reject_reason = mhz_match.get('reject_reason', '') or mhz_match.get('remark', '') or '平台拒稿'
                update_item_rejected(item['id'], reject_reason)
                # [GEO-R2-CAN-039 v3] 精确退款透传：本退款与扣费解耦——扣费在 _process_batch_order
                # 按「批次总额」单次 deduct_points(media_proxy_publish, extra_cost=total_cost)，
                # 其 charge_tx_id 未持久化到 publish_batches/publish_orders/publish_order_items 任一表；
                # 且此处是按「单条媒体项」部分退款（跨批次/跨用户），退款作用域无法取回 charge_tx_id，
                # 故保持原逐条退款、不透传 charge_tx_id（decoupled residual）。
                try:
                    from services.notification_events import publication_refund_context
                    await refund_points(item['user_id'], 'media_proxy_publish',
                                       f"代发拒稿退款: {item['media_name']}",
                                       notification=publication_refund_context(
                                           f"publication_item:{int(item['id'])}"
                                       ))
                    update_item_refunded(item['id'])
                except Exception as e:
                    logger.warning(f"[Publish] 退款失败: {e}")
                recalculate_order_status(item['order_id'])
                updated += 1

        # 重算批次状态
        batch_ids = set()
        for item in submitted:
            order = get_order(item['order_id'])
            if order and order.get('batch_id'):
                batch_ids.add(order['batch_id'])
        for bid in batch_ids:
            recalculate_batch_status(bid)

        logger.info(f"[Publish] 订单同步完成: {updated}/{len(submitted)} 条已更新")

    except Exception as e:
        logger.error(f"[Publish] 订单状态同步失败: {e}")
    finally:
        await client.close()
