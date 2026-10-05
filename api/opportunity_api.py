"""
机会成本计算 API
用于诊断报告中动态计算AI搜索漏斗和机会流失
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import List, Optional
import logging

logger = logging.getLogger("Opportunity-API")

router = APIRouter(prefix="/api/opportunity", tags=["opportunity"])


def _require_auth(http_request: Request) -> dict:
    user = getattr(http_request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


class OpportunityRequest(BaseModel):
    """机会成本计算请求"""
    keywords: List[str]  # 要查询的关键词列表
    # 可选：自定义漏斗参数
    click_rate: Optional[float] = 0.02      # 默认2%
    inquiry_rate: Optional[float] = 0.25    # 默认25%
    deal_rate: Optional[float] = 0.20       # 默认20%


class OpportunityFromIndexRequest(BaseModel):
    """直接从指数计算（不调用5118 API）"""
    keyword: str
    pc_index: int
    mobile_index: int
    sem_price: Optional[float] = 0.0


@router.post("/calculate")
async def calculate_opportunity(request: OpportunityRequest, http_request: Request = None):
    if http_request: _require_auth(http_request)
    """
    查询5118获取关键词流量，计算AI搜索机会成本
    
    返回:
    - 每个关键词的漏斗数据
    - 年流失客户数
    - 机会成本公式
    """
    try:
        from tools.opportunity_calculator import OpportunityCalculator, FunnelConfig
        
        # 创建计算器（使用自定义参数）
        config = FunnelConfig(
            click_rate=request.click_rate,
            inquiry_rate=request.inquiry_rate,
            deal_rate=request.deal_rate
        )
        calculator = OpportunityCalculator(config)
        
        # 批量计算
        results = await calculator.calculate_batch(request.keywords)
        
        # 汇总数据
        total_ai_monthly = sum(r.ai_monthly for r in results)
        total_deals_yearly = sum(r.deals_yearly for r in results)
        
        return {
            "success": True,
            "keywords": [r.to_dict() for r in results],
            "summary": {
                "total_keywords": len(results),
                "total_ai_monthly_traffic": total_ai_monthly,
                "total_yearly_lost_deals": total_deals_yearly,
                "funnel_config": {
                    "click_rate": f"{config.click_rate * 100}%",
                    "inquiry_rate": f"{config.inquiry_rate * 100}%",
                    "deal_rate": f"{config.deal_rate * 100}%",
                    "ai_penetration": f"{config.ai_penetration * 100}%"
                },
                "formula": f"客单价 × {total_deals_yearly} = 年机会成本"
            }
        }
        
    except Exception as e:
        logger.error(f"机会成本计算失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/calculate-from-index")
async def calculate_from_index(request: OpportunityFromIndexRequest, http_request: Request = None):
    if http_request: _require_auth(http_request)
    """
    直接从5118指数计算（不调用API）
    用于前端已有5118数据或测试场景
    """
    try:
        from tools.opportunity_calculator import OpportunityCalculator
        
        calculator = OpportunityCalculator()
        result = calculator.calculate_from_index(
            keyword=request.keyword,
            pc_index=request.pc_index,
            mobile_index=request.mobile_index,
            sem_price=request.sem_price
        )
        
        return {
            "success": True,
            "data": result.to_dict(),
            "report_markdown": calculator.generate_report_section(result)
        }
        
    except Exception as e:
        logger.error(f"从指数计算失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/single/{keyword}")
async def calculate_single_keyword(keyword: str, http_request: Request = None):
    if http_request: _require_auth(http_request)
    """
    查询单个关键词的机会成本
    """
    try:
        from tools.opportunity_calculator import calculate_opportunity, OpportunityCalculator
        
        result = await calculate_opportunity(keyword)
        calculator = OpportunityCalculator()
        
        return {
            "success": True,
            "data": result.to_dict(),
            "report_markdown": calculator.generate_report_section(result)
        }
        
    except Exception as e:
        logger.error(f"单词查询失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "service": "opportunity-calculator"}
