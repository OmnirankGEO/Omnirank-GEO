"""
数据蒸馏 API 端点 V4.2

全部使用 brand_id 作为主参数（与全局 ClientContext 对齐）。
数据源：keyword_insights 表（V4.2 LLM 蒸馏结果）

端点列表：
- GET  /api/insights/competitor-radar  — 竞品心智份额
- GET  /api/insights/success-patterns  — 成功特征 + 平台偏好
- GET  /api/insights/source-analysis   — 信源权重排名
- GET  /api/insights/roi-verification  — 基准 vs 当前
- GET  /api/insights/summary           — Dashboard 摘要
- GET  /api/insights/keyword-trend     — 单关键词时序趋势
- POST /api/insights/feedback          — 质量反馈（👍/👎）
- POST /api/insights/synthesize        — LLM 洞察合成
"""

from fastapi import APIRouter, Query, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
import json

router = APIRouter(prefix="/api/insights", tags=["Data Distillation"])

# RBAC（fix/security-audit-p0 #18）— 8 个洞察接口的 brand_id 全部需校验归属
from auth.brand_access import require_brand_access


# ==========================================
# 1. 竞品雷达
# ==========================================

@router.get("/competitor-radar")
def get_competitor_radar(
    request: Request,
    brand_id: int = Query(..., description="品牌ID"),
    days: int = Query(30, description="回溯天数"),
):
    """竞品心智份额：品牌被提及次数 + 排名分布"""
    require_brand_access(request, brand_id)
    from db.distillation_db import get_competitor_radar
    data = get_competitor_radar(brand_id, days)
    return {"status": "success", "data": data}


# ==========================================
# 2. 成功特征分析
# ==========================================

@router.get("/success-patterns")
def get_success_patterns(
    request: Request,
    brand_id: int = Query(..., description="品牌ID"),
    days: int = Query(30, description="回溯天数"),
):
    """成功特征分析：从 keyword_insights 聚合回复结构 + 平台偏好"""
    require_brand_access(request, brand_id)
    from db.distillation_db import get_keyword_insights
    insights = get_keyword_insights(brand_id, days)

    if not insights:
        return {"status": "success", "data": {"patterns": [], "summary": "暂无数据"}}

    total = len(insights)
    
    # 平台偏好统计
    platform_stats = {}
    for ins in insights:
        platforms = ins.get("platforms_analyzed", [])
        if isinstance(platforms, str):
            try:
                platforms = json.loads(platforms)
            except (json.JSONDecodeError, TypeError):
                platforms = []
        
        for p in platforms:
            if p not in platform_stats:
                platform_stats[p] = {"total": 0, "detected": 0}
            platform_stats[p]["total"] += 1
        
        # 检查客户是否被提及
        cp = ins.get("client_position", {})
        if isinstance(cp, str):
            try:
                cp = json.loads(cp)
            except (json.JSONDecodeError, TypeError):
                cp = {}
        
        if cp.get("mentioned_in", 0) > 0:
            for p in platforms:
                if p in platform_stats:
                    platform_stats[p]["detected"] += 1
    
    # 计算检出率
    for p in platform_stats:
        t = platform_stats[p]["total"]
        d = platform_stats[p]["detected"]
        platform_stats[p]["detection_rate"] = round(d / t * 100, 1) if t > 0 else 0
    
    # 回复结构偏好
    structure_summary = {}
    for ins in insights:
        rp = ins.get("response_patterns", {})
        if isinstance(rp, str):
            try:
                rp = json.loads(rp)
            except (json.JSONDecodeError, TypeError):
                rp = {}
        
        prefs = rp.get("platform_preferences", {})
        for platform, desc in prefs.items():
            if platform not in structure_summary:
                structure_summary[platform] = []
            structure_summary[platform].append(desc)
    
    # 排名分布
    rank_dist = {"top1": 0, "top3": 0, "mentioned": 0, "absent": 0}
    for ins in insights:
        cp = ins.get("client_position", {})
        if isinstance(cp, str):
            try:
                cp = json.loads(cp)
            except (json.JSONDecodeError, TypeError):
                cp = {}
        
        best_rank = cp.get("best_rank")
        if best_rank == 1:
            rank_dist["top1"] += 1
        elif best_rank and best_rank <= 3:
            rank_dist["top3"] += 1
        elif cp.get("mentioned_in", 0) > 0:
            rank_dist["mentioned"] += 1
        else:
            rank_dist["absent"] += 1

    return {
        "status": "success",
        "data": {
            "total_insights": total,
            "platform_stats": platform_stats,
            "structure_summary": {
                p: list(set(descs))[:3] for p, descs in structure_summary.items()
            },
            "rank_distribution": rank_dist,
        }
    }


# ==========================================
# 3. 信源分析（V4.3 — 基于真实引用URL）
# ==========================================

@router.get("/source-analysis")
def get_source_analysis(
    request: Request,
    brand_id: int = Query(..., description="品牌ID"),
    days: int = Query(30, description="回溯天数"),
):
    """
    信源权重排名：基于 monitoring_results.search_citations 中的真实URL。
    AI引擎回复时附带的引用链接是最可靠的信源数据。
    """
    require_brand_access(request, brand_id)
    import re
    from urllib.parse import urlparse

    # --- 第一数据源：monitoring_results.search_citations（真实URL） ---
    from db.distillation_db import get_connection
    conn = get_connection()
    try:
        cursor = conn.cursor()
        from services.monitoring_identity_review import aggregate_eligible_sql
        cursor.execute(f"""
            SELECT mr.platform, mr.keyword, mr.search_citations
            FROM monitoring_results mr
            JOIN monitoring_tasks mt ON mr.task_id = mt.id
            WHERE mt.brand_id = %s
              AND {aggregate_eligible_sql('mr')}
              AND mr.tested_at >= NOW() - INTERVAL '%s days'
              AND mr.search_citations IS NOT NULL
              AND mr.search_citations <> ''
              AND mr.search_citations <> '[]'
        """, (brand_id, days))
        rows = cursor.fetchall()
        conn.close()

        domain_freq = {}  # domain -> {url, title, count, platforms, keywords}

        for row in rows:
            platform = row["platform"] or ""
            keyword = row["keyword"] or ""
            # search_citations 当前为 TEXT(JSON 字符串);若未来迁 JSONB(psycopg2 自动反序列化为 list)也兼容
            _sc = row["search_citations"]
            if isinstance(_sc, list):
                citations = _sc
            elif isinstance(_sc, str):
                try:
                    citations = json.loads(_sc)
                except (json.JSONDecodeError, TypeError):
                    continue
            else:
                continue

            if not isinstance(citations, list):
                continue

            for cit in citations:
                if not isinstance(cit, dict):
                    continue
                url = cit.get("url") or cit.get("link") or ""
                title = cit.get("title") or cit.get("site_name") or ""
                if not url or len(url) < 8:
                    continue

                # 提取域名作为聚合key
                try:
                    parsed = urlparse(url)
                    domain = parsed.netloc.lower().lstrip("www.")
                except Exception:
                    domain = url

                if not domain or len(domain) < 3:
                    continue

                if domain not in domain_freq:
                    domain_freq[domain] = {
                        "domain": domain,
                        "sample_url": url,
                        "title": title,
                        "count": 0,
                        "platforms": set(),
                        "keywords": set(),
                        "source": "search_citations",
                    }
                domain_freq[domain]["count"] += 1
                domain_freq[domain]["platforms"].add(platform)
                domain_freq[domain]["keywords"].add(keyword)
                # 保留较长的标题
                if len(title) > len(domain_freq[domain]["title"]):
                    domain_freq[domain]["title"] = title

        has_real_citations = len(domain_freq) > 0

        # --- 补充数据源：LLM蒸馏的 sources_cited（仅当无真实URL时降级使用） ---
        if not has_real_citations:
            from db.distillation_db import get_keyword_insights
            insights = get_keyword_insights(brand_id, days)

            _noise_pattern = re.compile(r'^\[?\d+\]?(\[?\d+\]?)*$')
            _noise_names = {'引用标记', '未知', '--', '无', '间接引用'}

            for ins in insights:
                sources = ins.get("sources_cited", [])
                if isinstance(sources, str):
                    try:
                        sources = json.loads(sources)
                    except (json.JSONDecodeError, TypeError):
                        continue

                for source in sources:
                    if not isinstance(source, dict):
                        continue
                    ref = source.get("url_or_reference", "").strip()
                    if not ref or len(ref) < 4:
                        continue
                    if _noise_pattern.match(ref):
                        continue
                    if ref in _noise_names:
                        continue

                    # 尝试提取域名
                    domain = source.get("domain") or ""
                    if not domain and ref.startswith("http"):
                        try:
                            domain = urlparse(ref).netloc.lower().lstrip("www.")
                        except Exception:
                            pass
                    key = domain if domain else ref

                    if key not in domain_freq:
                        domain_freq[key] = {
                            "domain": domain or None,
                            "sample_url": ref if ref.startswith("http") else None,
                            "title": ref if not ref.startswith("http") else "",
                            "count": 0,
                            "platforms": set(),
                            "keywords": set(),
                            "source": "llm_distilled",
                        }
                    domain_freq[key]["count"] += 1
                    cited_by = source.get("cited_by_platforms", [])
                    domain_freq[key]["platforms"].update(cited_by)

        # --- 格式化输出 ---
        sources_list = []
        for data in sorted(domain_freq.values(), key=lambda x: x["count"], reverse=True)[:50]:
            sources_list.append({
                "domain": data["domain"],
                "url": data.get("sample_url"),
                "title": data.get("title", ""),
                "count": data["count"],
                "platforms": list(data["platforms"]),
                "keyword_count": len(data.get("keywords", set())),
                "source": data.get("source", "unknown"),
            })

        return {
            "status": "success",
            "data": {
                "total_unique_sources": len(domain_freq),
                "has_real_citations": has_real_citations,
                "top_sources": sources_list,
            }
        }
    finally:
        try:
            conn.close()
        except Exception: pass


# ==========================================
# 4. ROI 验证
# ==========================================

@router.get("/roi-verification")
def get_roi_verification(
    request: Request,
    brand_id: int = Query(..., description="品牌ID"),
):
    """效果验证：基准期(30-60天前) vs 当前期(最近30天)"""
    require_brand_access(request, brand_id)
    from db.distillation_db import get_roi_verification
    data = get_roi_verification(brand_id)
    return {"status": "success", "data": data}


# ==========================================
# 5. Dashboard 摘要
# ==========================================

@router.get("/summary")
def get_insights_summary(
    request: Request,
    brand_id: int = Query(..., description="品牌ID"),
    days: int = Query(30, description="回溯天数"),
):
    """Dashboard 摘要：概览统计"""
    require_brand_access(request, brand_id)
    from db.distillation_db import get_insight_stats, get_competitor_radar

    stats = get_insight_stats(brand_id, days)
    radar = get_competitor_radar(brand_id, days)

    return {
        "status": "success",
        "data": {
            "stats": stats,
            "top_brands": radar.get("brands", [])[:5],
            "total_mentions": radar.get("total_mentions", 0),
            "total_keywords": radar.get("total_keywords", 0),
        }
    }


# ==========================================
# 5.5 关键词蒸馏列表（BUG-1 修复）
# ==========================================

@router.get("/keyword-insights-list")
def get_keyword_insights_list(
    request: Request,
    brand_id: int = Query(..., description="品牌ID"),
    days: int = Query(30, description="回溯天数"),
    limit: int = Query(100, description="最大返回数"),
):
    """关键词蒸馏详情列表：供成功模式面板展示每个关键词的蒸馏结果"""
    require_brand_access(request, brand_id)
    from db.distillation_db import get_keyword_insights
    insights = get_keyword_insights(brand_id, days, limit=limit)

    return {
        "status": "success",
        "data": {
            "insights": insights,
            "total": len(insights),
        }
    }


# ==========================================
# 6. 关键词时序趋势（新增）
# ==========================================

@router.get("/keyword-trend")
def get_keyword_trend(
    request: Request,
    brand_id: int = Query(..., description="品牌ID"),
    keyword: str = Query(..., description="关键词"),
    days: int = Query(90, description="回溯天数"),
):
    """单关键词时序趋势：追踪排名/提及变化"""
    require_brand_access(request, brand_id)
    from db.distillation_db import get_keyword_trend
    data = get_keyword_trend(brand_id, keyword, days)
    return {
        "status": "success",
        "data": {
            "keyword": keyword,
            "trend": data,
            "total_snapshots": len(data),
        }
    }


# ==========================================
# 7. 质量反馈（新增）
# ==========================================

class FeedbackRequest(BaseModel):
    insight_id: int
    quality_flag: str  # 'verified' | 'rejected'
    feedback_note: Optional[str] = None


@router.post("/feedback")
def submit_feedback(req: FeedbackRequest, request: Request):
    """用户质量反馈：👍 verified / 👎 rejected"""
    if req.quality_flag not in ("verified", "rejected"):
        raise HTTPException(400, "quality_flag 必须为 'verified' 或 'rejected'")

    # 校验用户对该洞察所属品牌的访问权限
    from db.distillation_db import update_quality_flag, get_insight_brand_id
    from auth.brand_access import require_brand_access
    brand_id = get_insight_brand_id(req.insight_id)
    if brand_id is not None:
        require_brand_access(request, brand_id)

    success = update_quality_flag(
        insight_id=req.insight_id,
        quality_flag=req.quality_flag,
        feedback_note=req.feedback_note,
    )
    
    if not success:
        raise HTTPException(404, f"未找到 insight_id={req.insight_id}")
    
    return {"status": "success", "message": f"已标记为 {req.quality_flag}"}


# ==========================================
# 8. LLM 洞察合成
# ==========================================

@router.post("/synthesize")
async def synthesize_insights(
    request: Request,
    brand_id: int = Query(..., description="品牌ID"),
    days: int = Query(30, description="回溯天数"),
):
    """
    LLM 洞察合成：从 keyword_insights 聚合生成中文分析摘要
    """
    require_brand_access(request, brand_id)
    from db.distillation_db import get_competitor_radar, get_insight_stats, get_keyword_insights

    # 1. 收集聚合数据
    radar = get_competitor_radar(brand_id, days)
    stats = get_insight_stats(brand_id, days)
    insights = get_keyword_insights(brand_id, days, limit=50)

    if not insights:
        return {
            "status": "success",
            "data": {"summary_text": "暂无足够数据生成洞察，请先执行监测任务。", "generated": False}
        }

    # 2. 构造结构化数据摘要（给 LLM 的上下文）
    top_brands = radar.get("brands", [])[:10]
    brand_summary = "\n".join([
        f"- {b['brand_name']}: 被提及{b['mention_count']}次, 份额{b['share_pct']}%"
        + (f", 平均排名{b['avg_rank']}" if b.get('avg_rank') else "")
        for b in top_brands
    ])

    # 收集优化建议
    all_hints = []
    for ins in insights:
        hints = ins.get("optimization_hints", [])
        if isinstance(hints, str):
            try:
                hints = json.loads(hints)
            except (json.JSONDecodeError, TypeError):
                hints = []
        all_hints.extend(hints[:2])  # 每个关键词取前 2 条

    hints_summary = "\n".join([f"- {h}" for h in list(set(all_hints))[:10]])

    context_text = f"""
## 监测数据摘要（最近{days}天）

### 品牌心智份额
{brand_summary}

### 统计概览
- 蒸馏关键词数: {stats.get('unique_keywords', 0)}
- 监测次数: {stats.get('total_tasks', 0)}
- 质量: auto={stats.get('auto_count', 0)}, verified={stats.get('verified_count', 0)}, degraded={stats.get('degraded_count', 0)}

### 优化建议汇总
{hints_summary if hints_summary else '暂无'}
""".strip()

    # 3. 调用 LLM 生成洞察
    try:
        from services.llm_service import get_llm_service
        llm = get_llm_service()

        prompt = f"""你是一位品牌数字营销分析师。根据以下AI搜索监测数据，生成一份简洁的洞察摘要（中文，3-5个要点，每个要点1-2句话）。

要求：
1. 用业务语言而非技术语言
2. 指出关键发现和可执行建议
3. 重点关注品牌竞争格局和优化方向
4. 每个要点用 🔍/📊/💡/⚠️ 等 emoji 开头

{context_text}

请输出洞察摘要："""

        response = await llm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=800,
        )
        summary_text = response.get("content", "").strip()

        # 4. 写入 dds_patterns.md（反哺写作用）
        _save_patterns_file(brand_id, summary_text, context_text)

        return {
            "status": "success",
            "data": {
                "summary_text": summary_text,
                "context_data": context_text,
                "generated": True,
            }
        }

    except Exception as e:
        # LLM 不可用时，返回结构化数据摘要
        fallback = f"📊 共蒸馏 {stats.get('unique_keywords', 0)} 个关键词，识别 {len(top_brands)} 个竞品品牌。\n"
        if top_brands:
            fallback += f"🔍 心智份额最高: {top_brands[0]['brand_name']} ({top_brands[0]['share_pct']}%)\n"
        fallback += f"💡 共 {len(all_hints)} 条优化建议待查看"

        return {
            "status": "success",
            "data": {
                "summary_text": fallback,
                "generated": False,
                "fallback_reason": str(e),
            }
        }


def _save_patterns_file(brand_id: int, summary_text: str, context_text: str):
    """将洞察写入 dds_patterns.md（反哺写作用）"""
    from pathlib import Path
    from datetime import datetime

    patterns_dir = Path(__file__).parent.parent / "data" / "knowledge" / "clients" / str(brand_id)
    patterns_dir.mkdir(parents=True, exist_ok=True)
    patterns_file = patterns_dir / "dds_patterns.md"

    content = f"""# 数据蒸馏洞察 — 品牌 {brand_id}
> 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}
> 此文件由数据蒸馏系统 V4.2 自动维护

## AI 洞察摘要
{summary_text}

## 原始统计数据
{context_text}
"""
    patterns_file.write_text(content, encoding="utf-8")
    print(f"[DDS] 已写入 dds_patterns.md → {patterns_file}")
