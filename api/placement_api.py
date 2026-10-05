"""
媒体投放建议 API 路由
提供投放建议生成、媒体资源查询、数据上传与导出功能
"""

import os
import json
import uuid
import tempfile
import shutil
import threading
import asyncio
import io
import csv
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

from services.placement_service import get_placement_service

router = APIRouter(prefix="/api/placement", tags=["投放建议"])

import asyncio
import os

# 并发限制：投放生成调 LLM，限制同时运行数
# [并发-3 2026-06-10 · FABLE复审后改默认5灰度] 默认5=旧行为(fail-safe)·PLACEMENT_MAX_CONCURRENCY=100 + force-recreate 调高(部署最后一步)
_placement_semaphore = asyncio.Semaphore(int(os.getenv("PLACEMENT_MAX_CONCURRENCY", "5")))


# ========== Pydantic Models ==========

class GenerateRequest(BaseModel):
    """投放建议生成请求"""
    budget: str = "budget"  # 'budget' | 'balanced' | 'comprehensive'


class MediaSearchParams(BaseModel):
    """媒体资源搜索参数"""
    search: Optional[str] = None
    category: Optional[str] = None
    ai_engine: Optional[str] = None
    geo_only: bool = False
    max_price: Optional[float] = None
    limit: int = 100


# ========== API 路由 ==========

@router.get("/articles/{quote_id}", summary="获取已完成文章及投放建议")
async def get_completed_articles(quote_id: int, request: Request):
    """
    获取指定报价单下已完成的文章及其投放建议数据

    - quote_id: 报价单ID
    """
    from auth.brand_access import require_quote_access
    require_quote_access(request, quote_id)
    service = get_placement_service()
    try:
        articles = service.get_completed_articles(quote_id)
        return {
            "success": True,
            "quote_id": quote_id,
            "articles": articles,
            "total": len(articles)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取文章失败: {str(e)}")


@router.post("/generate/{quote_id}", summary="生成投放建议")
async def generate_recommendations(quote_id: int, request: Request, data: GenerateRequest = None):
    """
    为指定报价单下所有已完成文章生成 LLM 投放建议

    - quote_id: 报价单ID
    - budget: 预算策略 ('budget' | 'balanced' | 'comprehensive')
    """
    from auth.brand_access import require_quote_access
    require_quote_access(request, quote_id)
    if data is None:
        data = GenerateRequest()

    await _placement_semaphore.acquire()
    try:
        service = get_placement_service()
        recommendations = await service.generate_recommendations(quote_id, data.budget)
        return {
            "success": True,
            "quote_id": quote_id,
            "budget": data.budget,
            "recommendations": recommendations
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成投放建议失败: {str(e)}")
    finally:
        _placement_semaphore.release()


@router.get("/media-outlets", summary="搜索媒体资源")
async def get_media_outlets(
    search: Optional[str] = None,
    category: Optional[str] = None,
    ai_engine: Optional[str] = None,
    geo_only: bool = False,
    max_price: Optional[float] = None,
    limit: int = 100,
):
    """
    搜索/列出知识库中的媒体资源

    - search: 关键词搜索
    - category: 媒体类别筛选
    - ai_engine: AI引擎筛选
    - geo_only: 仅显示地域媒体
    - max_price: 最高价格筛选
    - limit: 返回数量上限
    """
    service = get_placement_service()
    try:
        outlets = service.get_media_outlets(
            search=search,
            category=category,
            ai_engine=ai_engine,
            geo_only=geo_only,
            max_price=max_price,
            limit=limit,
        )
        return {
            "success": True,
            "outlets": outlets,
            "total": len(outlets)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询媒体资源失败: {str(e)}")


@router.post("/upload-data", summary="上传媒体数据文件")
async def upload_media_data(request: Request, file: UploadFile = File(...)):
    """
    上传 CSV/XLSX/ZIP 文件,后台解析并导入媒体数据

    支持格式:.csv, .xlsx, .zip
    [P1-16 fix 2026-05-23 老板授权] Codex 跨 AI 审计 · 加 admin 校验防普通代理污染媒体库
    """
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".csv", ".xlsx", ".zip"):
        raise HTTPException(status_code=400, detail="仅支持 CSV/XLSX/ZIP 文件")

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件上传失败: {str(e)}")

    file_type = suffix.lstrip(".")
    service = get_placement_service()

    task_id = str(uuid.uuid4())

    def run_in_bg():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                service.process_upload(tmp_path, file.filename, file_type, task_id)
            )
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    thread = threading.Thread(target=run_in_bg, daemon=True)
    thread.start()

    return {
        "success": True,
        "task_id": task_id,
        "message": "文件已上传，正在后台分析..."
    }


@router.get("/upload-status/{task_id}", summary="查询上传处理状态")
async def get_upload_status(task_id: str):
    """
    查询文件上传后台处理状态

    - task_id: 上传时返回的任务ID
    """
    service = get_placement_service()
    try:
        status = service.get_upload_status(task_id)
        return {
            "success": True,
            "task_id": task_id,
            **status
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询状态失败: {str(e)}")


@router.get("/analysis-log", summary="获取优化日志")
async def get_analysis_log(limit: int = 50):
    """
    获取媒体投放优化日志列表

    - limit: 返回条数上限
    """
    service = get_placement_service()
    try:
        logs = service.get_analysis_logs(limit=limit)
        return {
            "success": True,
            "logs": logs,
            "total": len(logs)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取日志失败: {str(e)}")


@router.get("/stats", summary="获取媒体知识库统计")
async def get_media_stats():
    """获取媒体知识库的汇总统计数据"""
    service = get_placement_service()
    try:
        stats = service.get_media_stats()
        return {
            "success": True,
            **stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取统计失败: {str(e)}")


@router.get("/knowledge-check/{quote_id}", summary="检查文章是否引用客户知识库")
async def check_knowledge_usage(quote_id: int, request: Request):
    """
    检查已完成文章是否引用了客户知识库中的关键数据

    - 返回每篇文章的引用状态: pass / warning / fail
    - pass: 充分引用了知识库数据
    - warning: 部分引用，需人工核查
    - fail: 未引用知识库，数据可能不准确
    """
    from auth.brand_access import require_quote_access
    require_quote_access(request, quote_id)
    service = get_placement_service()
    try:
        # [并发-1 2026-06-10] check_knowledge_usage 内部是同步 httpx LLM 调用 + time.sleep(批间 2s / 429 退避 5-15s),
        #   N 篇文章核查可同步阻塞事件循环数分钟 → 高并发下卡全场所有用户。移入线程池不阻塞事件循环。
        import asyncio
        result = await asyncio.to_thread(service.check_knowledge_usage, quote_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"知识库检查失败: {str(e)}")


@router.post("/knowledge-fix/{quote_id}", summary="一键修复文章核查问题")
async def fix_knowledge_issues(quote_id: int, request: Request):
    """
    对核查发现问题的文章进行精准局部修复（不完全重写）

    - 竞品名称未验证 → 替换为已验证列表中的名称
    - 评分差距过大 → 调整至绅士原则范围
    - 评分过低 → 提升至≥87分
    - 价格/面积矛盾 → 改为知识库正确数据
    """
    from auth.brand_access import require_quote_access
    require_quote_access(request, quote_id)
    service = get_placement_service()
    try:
        # 可选：只修复指定的 topic_ids
        topic_ids = None
        if request:
            try:
                body = await request.json()
                topic_ids = body.get('topic_ids')
            except Exception:
                pass

        result = await service.fix_article_issues(quote_id, topic_ids)
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"修复失败: {str(e)}")


@router.patch("/issue/{issue_id}/dismiss", summary="标记/恢复 知识库核查 issue 为误报")
async def patch_dismiss_kb_issue(issue_id: int, request: Request):
    """[M 方案 · CTO-15.23 2026-05-06] 用户标记某条核查 issue 为误报(dismissed=True)
    或恢复为有效(dismissed=False)· 后续 LLM fix 跳过 dismissed 的 + 显式禁动那段文本

    body: {"dismissed": true/false}  缺省 true
    """
    from db.diagnosis_db import dismiss_kb_issue, get_connection
    try:
        # [GEO-R1-CAN-065] IDOR 修复：dismiss 按 issue 主键写库，必须先解析该 issue 归属的
        #   quote_id 并做归属校验(与 sibling check_knowledge_usage / fix_knowledge_issues 一致)。
        #   否则任意登录用户可 dismiss 他人租户的核查 issue。写端点 fail-closed(allow_null=False)。
        from auth.brand_access import require_quote_access
        conn = get_connection()
        try:
            _c = conn.cursor()
            _c.execute("SELECT quote_id FROM kb_check_issues WHERE id = %s", (issue_id,))
            _row = _c.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not _row:
            raise HTTPException(status_code=404, detail=f"issue {issue_id} 不存在")
        _quote_id = _row["quote_id"] if isinstance(_row, dict) else _row[0]
        require_quote_access(request, _quote_id, allow_null=False)

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        dismissed = bool(body.get('dismissed', True))
        ok = dismiss_kb_issue(issue_id, dismissed=dismissed)
        if not ok:
            raise HTTPException(status_code=404, detail=f"issue {issue_id} 不存在")
        return {"success": True, "issue_id": issue_id, "dismissed": dismissed}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"忽略 issue 失败: {str(e)}")


# ========== GEO调研数据 API ==========

@router.post("/research/upload", summary="上传GEO调研数据CSV")
async def upload_research_data(
    request: Request,
    file: UploadFile = File(...),
    researcher: str = Form(''),
    industry: str = Form('')
):
    """
    上传GEO引擎引用调研数据(自动识别三种格式)
    [P1-16 fix 2026-05-23 老板授权] 加 admin 校验

    格式A(实际调研):序号, 查询内容, AI平台, 引用编号, 引用标题, 引用链接, 引用来源平台, 引用摘要
    格式B(宽表模板):行业, 测试词, AI引擎, 引用来源1~8, 总引用数, 备注
    格式C（长表）：行业, 测试词, AI引擎, 引用平台, 引用位置

    - industry: 行业（CSV中无"行业"列时使用）
    """
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    suffix = Path(file.filename).suffix.lower()
    if suffix != '.csv':
        raise HTTPException(status_code=400, detail="仅支持 CSV 文件")

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件上传失败: {str(e)}")

    service = get_placement_service()
    try:
        result = service.import_research_csv(tmp_path, researcher=researcher, industry=industry)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"调研数据导入失败: {str(e)}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@router.get("/research/summary", summary="获取调研数据总览")
async def get_research_summary(request: Request):
    """获取已导入的GEO调研数据统计 · [BUG4 2026-06-05] geo服务=平台内部分类·仅 admin 可见"""
    service = get_placement_service()
    try:
        data = service.get_research_summary()
        # [BUG4] 非 admin 过滤掉「geo服务」行业(防暴露平台内部业务口径 · 前端也过滤·此为防绕过)
        user = getattr(request.state, "user", None)
        is_admin = bool(user and user.get("is_admin"))
        if not is_admin and isinstance(data.get("by_industry"), list):
            _before = len(data["by_industry"])
            data["by_industry"] = [
                x for x in data["by_industry"]
                if "geo服务" not in str(x.get("industry", "")).replace(" ", "").lower()
            ]
            _removed = _before - len(data["by_industry"])
            if _removed and isinstance(data.get("total_industries"), int):
                data["total_industries"] = max(0, data["total_industries"] - _removed)
        return {"success": True, **data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取调研数据失败: {str(e)}")


@router.get("/research/industry/{industry}", summary="获取行业引擎评分矩阵")
async def get_industry_scores(industry: str):
    """
    获取指定行业基于调研数据的引擎-平台评分矩阵

    返回各平台在不同AI引擎中的引用概率和加权得分
    """
    service = get_placement_service()
    try:
        scores = service.get_industry_engine_scores(industry)
        return {
            "success": True,
            "industry": industry,
            "has_data": len(scores) > 0,
            "platform_scores": scores
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取行业评分失败: {str(e)}")


@router.post("/research/cleanup", summary="清理调研数据（去重+坏数据）")
async def cleanup_research_data(request: Request, industry: str = Form('')):
    """清理调研数据：移除重复记录和坏数据（平台名过长等）"""
    _require_admin(request)
    service = get_placement_service()
    try:
        result = service.cleanup_research_data(industry=industry if industry else None)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清理失败: {str(e)}")


@router.delete("/research/industry/{industry}", summary="删除行业调研数据")
async def delete_industry_research(industry: str, request: Request):
    """删除指定行业的全部调研数据"""
    _require_admin(request)
    service = get_placement_service()
    try:
        result = service.delete_industry_data(industry)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")


@router.get("/research/industry/{industry}/batches", summary="列出某行业上传批次（含权重）")
async def list_industry_batches(industry: str, request: Request):
    """列出指定行业的所有上传批次，含 year_month 与按月权重（管理员）

    返回字段：
      batch_id / created_at / completed_at / researcher / status / engine
      rows_for_industry = 该批次在该行业贡献的实际行数
      year_month / weight = 该批次月份及当前生效权重（可被 month_weights 覆盖）
    """
    _require_admin(request)
    service = get_placement_service()
    try:
        return {"success": True, "industry": industry,
                "batches": service.list_industry_batches(industry)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取批次列表失败: {str(e)}")


@router.delete("/research/batches/{batch_id}", summary="撤回单次上传批次（管理员）")
async def delete_research_batch(batch_id: str, request: Request):
    """删除单个批次的所有 raw 数据 + 批次记录，自动重聚合受影响行业"""
    _require_admin(request)
    service = get_placement_service()
    try:
        result = service.delete_batch(batch_id)
        if not result.get("found", True):
            raise HTTPException(status_code=404, detail="批次不存在")
        return {"success": True, **result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"撤回失败: {str(e)}")


@router.get("/research/industries", summary="获取已有行业列表")
async def get_research_industries():
    """获取数据库中所有已导入调研数据的行业列表"""
    service = get_placement_service()
    try:
        industries = service.get_all_industries()
        return {"success": True, "industries": industries}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取行业列表失败: {str(e)}")


@router.get("/research/industry/{industry}/details", summary="[admin] 获取行业查询词详细引用数据")
async def get_industry_query_details(industry: str, request: Request):
    """
    [P14-v4 加 admin-only · 2026-05-27]
    返回 answer_text / cite_url / cite_excerpt 等敏感字段 · 严禁代理/销售访问.
    前端发布参谋已不调此接口 · 但为防代理从 Network 手抓 · 后端必须 admin 兜底.

    返回按查询词分组、引擎分组的完整引用列表和 AI 回答原文.
    """
    _require_admin(request)
    service = get_placement_service()
    try:
        details = service.get_research_query_details(industry)
        return {"success": True, "industry": industry, **details}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取详情失败: {str(e)}")


@router.get("/research/industry/{industry}/sources", summary="[admin] 获取行业引用来源交叉分析")
async def get_industry_source_analysis(industry: str, request: Request):
    """
    [P14-v4 加 admin-only · 2026-05-27]
    返回 url / title / excerpt 等敏感字段 · 严禁代理/销售访问.

    分析行业引用来源:哪些文章被多个 AI 引擎共同引用
      - cross_engine_sources : 多引擎交叉引用的来源
      - engine_unique_sources: 各引擎独占来源
      - platform_engine_matrix: 平台×引擎引用次数矩阵
    """
    _require_admin(request)
    service = get_placement_service()
    try:
        analysis = service.get_research_source_analysis(industry)
        return {"success": True, "industry": industry, **analysis}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取来源分析失败: {str(e)}")


# ============================================================================
# P11 (2026-05-26) 发布参谋重设计 · 2 个安全 endpoint
# 给代理/销售看决策视图 · 严禁返回 AI 原文 / cite_url / cite_excerpt
# 都是只读 + 非敏感 · 不需 _require_admin (登录态由全局中间件兜底)
# ============================================================================

@router.get(
    "/research/industry/{industry}/content-type-stats",
    summary="[P11] 行业文章库内容类型分布(发布参谋用)",
)
async def get_industry_content_type_stats(industry: str):
    """
    统计某行业入文章库的内容类型分布(7 分类: article/video/doc_tool/encyc/ecom/gov/other)

    口径:
      - geo_research_articles.primary_industry = industry
      - review_status IN ('in_library', 'imported_to_reference')
      - expired IS NOT TRUE
      - GROUP BY content_type

    返回:
      total / by_type[{content_type, count, ratio}] / uncategorized_count
    """
    service = get_placement_service()
    try:
        stats = service.get_industry_content_type_stats(industry)
        return {"success": True, **stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取内容类型分布失败: {str(e)}")


@router.get(
    "/research/industry/{industry}/prompts-preview",
    summary="[P11] 安全的调研题目预览(发布参谋用 · 不含 AI 原文)",
)
async def get_industry_prompts_preview(industry: str, limit: int = 50):
    """
    返回该行业的调研题目文本 + 命中引擎数 + 引用条数 · 不返回 AI 原文 / URL / 摘要 / 标题

    数据源优先级:
      1. geo_research_prompts (新流, admin 配置)
      2. geo_research_raw DISTINCT query (老 CSV fallback)

    ⚠️ 严禁复用 /details endpoint · 后者会返回敏感字段 不适合代理 UI
    """
    service = get_placement_service()
    # 防御性 clamp(避免代理传 limit=10000 DOS)
    safe_limit = max(1, min(int(limit or 50), 200))
    try:
        preview = service.get_industry_prompts_preview(industry, limit=safe_limit)
        return {"success": True, **preview}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取题目预览失败: {str(e)}")


@router.get("/research/template", summary="下载调研模板")
async def download_research_template():
    """下载GEO调研数据填写模板CSV（新版：含引用标题、摘要、原文）"""
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(['序号', '查询内容', 'AI平台', '引用编号', '引用标题',
                     '引用链接', '引用来源平台', '引用摘要', '回答原文'])
    writer.writerow(['1', '2026年买房注意事项', '豆包', '1',
                     '中央定调2026楼市!重磅信号落地', 'http://example.com/article1',
                     '今日头条', '截至2026年，全国超百城优化调控政策...',
                     '2026年买房注意事项全攻略...（AI完整回答）'])
    writer.writerow(['1', '2026年买房注意事项', '豆包', '2',
                     '2026年房地产市场分析', 'http://example.com/article2',
                     '知乎', '2026年楼市已进入政策托底阶段...',
                     '2026年买房注意事项全攻略...（AI完整回答）'])
    writer.writerow(['2', '2026年买房注意事项', 'Kimi', '1',
                     '买房必看：2026最新政策解读', 'http://example.com/article3',
                     '澎湃新闻', '住建部2026年明确提出...',
                     'Kimi对该问题的完整回答...'])

    output.seek(0)
    csv_bytes = output.getvalue().encode("utf-8")
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="geo_research_template.csv"'},
    )


@router.post("/export/{quote_id}", summary="导出投放建议CSV")
async def export_recommendations(quote_id: int, request: Request):
    """
    导出指定报价单的投放建议为 CSV 文件

    - quote_id: 报价单ID
    """
    from auth.brand_access import require_quote_access
    require_quote_access(request, quote_id)
    service = get_placement_service()
    try:
        recommendations = await service.generate_recommendations(quote_id, "balanced")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取投放建议失败: {str(e)}")

    if not recommendations:
        raise HTTPException(status_code=404, detail="暂无投放建议数据")

    output = io.StringIO()
    # Write UTF-8 BOM for Excel compatibility
    output.write("\ufeff")

    # Determine CSV columns from the first recommendation
    sample = recommendations[0]
    fieldnames = list(sample.keys()) if isinstance(sample, dict) else []

    if not fieldnames:
        raise HTTPException(status_code=500, detail="建议数据格式异常")

    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for rec in recommendations:
        if isinstance(rec, dict):
            writer.writerow(rec)

    output.seek(0)
    csv_bytes = output.getvalue().encode("utf-8")

    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="placement_{quote_id}.csv"'
        },
    )


# ============================================================
# 调研数据月份权重配置（仅管理员）
# 设计文档：docs/2026-04-30-调研数据月份权重-设计.md
# ============================================================
from datetime import datetime as _datetime


class WeightUpdateRequest(BaseModel):
    industry: str
    year_month: str            # 'YYYY-MM'
    weight: float
    note: Optional[str] = None


class ConfigUpdateRequest(BaseModel):
    decay_factor: Optional[float] = None
    min_queries_per_month: Optional[int] = None


def _require_admin(request: Request):
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    return user


@router.get("/research/config", summary="获取全局聚合配置（管理员）")
async def get_aggregation_config(request: Request):
    _require_admin(request)
    svc = get_placement_service()
    return svc._load_aggregation_config()


@router.put("/research/config", summary="更新全局聚合配置（自动重聚合，管理员）")
async def update_aggregation_config(request: Request, body: ConfigUpdateRequest):
    user = _require_admin(request)
    from db.diagnosis_db import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        if body.decay_factor is not None:
            if not (0 < body.decay_factor <= 1):
                raise HTTPException(400, "decay_factor 必须在 (0, 1] 之间")
            c.execute(
                "UPDATE geo_aggregation_config "
                "SET value=%s, updated_at=NOW(), updated_by=%s "
                "WHERE key='decay_factor'",
                (str(body.decay_factor), user.get("user_id")),
            )
        if body.min_queries_per_month is not None:
            if body.min_queries_per_month < 1:
                raise HTTPException(400, "min_queries_per_month 必须 >= 1")
            c.execute(
                "UPDATE geo_aggregation_config "
                "SET value=%s, updated_at=NOW(), updated_by=%s "
                "WHERE key='min_queries_per_month'",
                (str(body.min_queries_per_month), user.get("user_id")),
            )
        conn.commit()
    finally:
        try: conn.close()
        except Exception: pass

    svc = get_placement_service()
    updated = svc.aggregate_research_stats()         # 全行业重聚合
    return {"success": True, "updated_stats": updated}


@router.get("/research/weights", summary="某行业各月份权重明细（管理员）")
async def get_industry_month_weights(request: Request, industry: str):
    _require_admin(request)
    svc = get_placement_service()
    config = svc._load_aggregation_config()
    overrides = svc._load_month_weights([industry])

    from db.diagnosis_db import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT to_char(created_at, 'YYYY-MM') AS ym,
                   COUNT(DISTINCT query) AS q_count
            FROM geo_research_raw
            WHERE industry = %s
            GROUP BY ym
            ORDER BY ym DESC
        """, (industry,))
        result = []
        now = _datetime.now()
        for r in c.fetchall():
            ym = r['ym']
            default_w = svc._default_weight(svc._months_ago(ym, now), config['decay_factor'])
            override_w = overrides.get((industry, ym))
            result.append({
                "year_month":           ym,
                "query_count":          r['q_count'],
                "default_weight":       round(default_w, 4),
                "override_weight":      override_w,
                "is_overridden":        override_w is not None,
                "below_min_threshold":  r['q_count'] < config['min_queries_per_month'],
            })
        return result
    finally:
        try: conn.close()
        except Exception: pass


@router.put("/research/weights", summary="覆盖某月权重（自动重聚合该行业，管理员）")
async def upsert_month_weight(request: Request, body: WeightUpdateRequest):
    user = _require_admin(request)
    if not (0 <= body.weight <= 10):
        raise HTTPException(400, "weight 必须在 [0, 10] 之间")
    from db.diagnosis_db import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO geo_month_weights (industry, year_month, weight, note, updated_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (industry, year_month) DO UPDATE SET
                weight     = excluded.weight,
                note       = excluded.note,
                updated_at = NOW(),
                updated_by = excluded.updated_by
        """, (body.industry, body.year_month, body.weight, body.note, user.get("user_id")))
        conn.commit()
    finally:
        try: conn.close()
        except Exception: pass

    svc = get_placement_service()
    updated = svc.aggregate_research_stats([body.industry])
    return {"success": True, "updated_stats": updated}


@router.delete("/research/weights", summary="删除某月权重覆盖（恢复默认，管理员）")
async def delete_month_weight(request: Request, industry: str, year_month: str):
    _require_admin(request)
    from db.diagnosis_db import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "DELETE FROM geo_month_weights WHERE industry=%s AND year_month=%s",
            (industry, year_month),
        )
        conn.commit()
    finally:
        try: conn.close()
        except Exception: pass

    svc = get_placement_service()
    updated = svc.aggregate_research_stats([industry])
    return {"success": True, "updated_stats": updated}
