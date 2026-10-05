"""
v3.4 物料中心 API — GEO 物料字段管理

字段架构（v2.1 12 项三层）:
  🔴 必填核心 5 项 (60 分):
    - company_intro
    - selling_points
    - cases
    - competitors_real
    - industry + city (在 client_profiles 主表，不在 geo_assets)

  🟡 选填增强 7 项 (+28 分):
    - real_data_points
    - brand_story
    - milestones
    - team_core
    - testimonials
    - service_flow
    - price_packages

  🟢 AI 自动挖 3 项 (+12 分):
    - target_keywords
    - industry_position
    - target_customer_profile

存储：client_profiles.industry_brief.geo_assets (JSONB)
"""

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

# [GEO-R8-CAN-003] 归属校验统一走 brand_access（owner + 分配代理 + admin），
# 不再用 client_profiles.user_id（该列不存在 → 旧逻辑对所有非 admin 恒 403）。
from auth.brand_access import require_profile_access

logger = logging.getLogger("GEO-Assets-API")
router = APIRouter(prefix="/api/profiles", tags=["GEO 物料中心"])


# ============================================================
# 字段定义和完整度评分
# ============================================================

REQUIRED_FIELDS = ("company_intro", "selling_points", "cases", "competitors_real")
RECOMMENDED_FIELDS = (
    "real_data_points", "brand_story", "milestones",
    "team_core", "testimonials", "service_flow", "price_packages",
)
AI_AUTO_FIELDS = ("target_keywords", "industry_position", "target_customer_profile")

REQUIRED_WEIGHT = 12   # 5 项 × 12 = 60 分（含 industry/city，但这两个已在主表）
RECOMMENDED_WEIGHT = 4  # 7 项 × 4 = 28 分
AI_AUTO_WEIGHT = 4      # 3 项 × 4 = 12 分


def calculate_completeness_score(geo_assets: dict, profile: dict) -> tuple[int, dict]:
    """
    计算物料完成度评分 0-100

    Returns:
        (score, detail) where detail = {
          "filled_required": [...],
          "missing_required": [...],
          "filled_recommended": [...],
          "missing_recommended": [...],
          "ai_auto_filled": [...],
        }
    """
    score = 0
    detail: dict[str, list[str]] = {
        "filled_required": [],
        "missing_required": [],
        "filled_recommended": [],
        "missing_recommended": [],
        "ai_auto_filled": [],
        "missing_ai_auto": [],
    }

    # 必填 4 项 + industry/city（5 项 = 60 分）
    for field in REQUIRED_FIELDS:
        if _is_filled(geo_assets.get(field)):
            score += REQUIRED_WEIGHT
            detail["filled_required"].append(field)
        else:
            detail["missing_required"].append(field)
    # industry / city 来自 profile 主表
    industry = profile.get("industry") or geo_assets.get("industry")
    city = profile.get("city") or geo_assets.get("city")
    if industry and city:
        score += REQUIRED_WEIGHT
        detail["filled_required"].append("industry+city")
    else:
        detail["missing_required"].append("industry+city")

    # 选填 7 项
    for field in RECOMMENDED_FIELDS:
        if _is_filled(geo_assets.get(field)):
            score += RECOMMENDED_WEIGHT
            detail["filled_recommended"].append(field)
        else:
            detail["missing_recommended"].append(field)

    # AI 自动挖 3 项
    for field in AI_AUTO_FIELDS:
        if _is_filled(geo_assets.get(field)):
            score += AI_AUTO_WEIGHT
            detail["ai_auto_filled"].append(field)
        else:
            detail["missing_ai_auto"].append(field)

    return min(100, score), detail


def _is_filled(val: Any) -> bool:
    """判断字段是否已填写（非空）"""
    if val is None:
        return False
    if isinstance(val, str):
        return len(val.strip()) > 0
    if isinstance(val, (list, dict)):
        return len(val) > 0
    return True


# ============================================================
# Pydantic 模型
# ============================================================

class CompanyIntro(BaseModel):
    text: Optional[str] = Field(None, max_length=5000)
    file_url: Optional[str] = None
    updated_at: Optional[str] = None


class SellingPoint(BaseModel):
    name: str
    evidence: Optional[str] = None


class SellingPointsBlock(BaseModel):
    absolute_pain_point: Optional[str] = Field(None, max_length=500)
    core_selling_points: Optional[list[SellingPoint]] = None
    unique_value: Optional[str] = Field(None, max_length=1000)
    use_scenarios: Optional[str] = Field(None, max_length=2000)
    industry_terms: Optional[list[str]] = None


class CaseItem(BaseModel):
    client_desc: str = Field(..., max_length=200)
    result: str = Field(..., max_length=500)
    timeline: Optional[str] = Field(None, max_length=100)


class CompetitorReal(BaseModel):
    name: str = Field(..., max_length=100)
    strengths: Optional[str] = Field(None, max_length=500)
    limitations: Optional[str] = Field(None, max_length=500)


class RealDataPoints(BaseModel):
    founded_year: Optional[int] = None
    served_clients_count: Optional[int] = None
    team_size: Optional[str] = Field(None, max_length=50)
    certifications: Optional[list[str]] = None
    awards: Optional[list[str]] = None


class BrandStory(BaseModel):
    founding_story: Optional[str] = Field(None, max_length=3000)
    core_philosophy: Optional[str] = Field(None, max_length=1000)


class Milestone(BaseModel):
    year: int
    event: str = Field(..., max_length=200)


class TeamMember(BaseModel):
    name: str
    title: str
    background: Optional[str] = Field(None, max_length=500)


class Testimonial(BaseModel):
    client_name: str
    quote: str = Field(..., max_length=500)
    title: Optional[str] = None


class ServiceStage(BaseModel):
    step: str
    duration: Optional[str] = None
    deliverable: Optional[str] = None


class ServiceFlow(BaseModel):
    stages: Optional[list[ServiceStage]] = None


class PricePackage(BaseModel):
    name: str
    price_range: str
    suitable_for: Optional[str] = None
    includes: Optional[list[str]] = None


class TargetCustomerProfile(BaseModel):
    scale: Optional[str] = None
    industry_focus: Optional[list[str]] = None
    budget_range: Optional[str] = None
    prerequisite: Optional[str] = None


class GeoAssetsUpdate(BaseModel):
    """部分更新 — 任意字段都可独立 PATCH"""
    company_intro: Optional[CompanyIntro] = None
    selling_points: Optional[SellingPointsBlock] = None
    cases: Optional[list[CaseItem]] = None
    competitors_real: Optional[list[CompetitorReal]] = None

    real_data_points: Optional[RealDataPoints] = None
    brand_story: Optional[BrandStory] = None
    milestones: Optional[list[Milestone]] = None
    team_core: Optional[list[TeamMember]] = None
    testimonials: Optional[list[Testimonial]] = None
    service_flow: Optional[ServiceFlow] = None
    price_packages: Optional[list[PricePackage]] = None

    target_keywords: Optional[list[str]] = None
    industry_position: Optional[str] = None
    target_customer_profile: Optional[TargetCustomerProfile] = None


# ============================================================
# 工具
# ============================================================

def _get_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


# [GEO-R8-CAN-003] 旧 _get_profile_safe 已删除：它基于 client_profiles.user_id
# 判定归属，但该表无 user_id 列（profile.get("user_id") 恒 None），导致每个非 admin
# 用户 None != user_id → 恒 403，物料中心对分配代理和 owner 全部误拒。
# 现统一改用 auth.brand_access.require_profile_access(request, profile_id)：
# 经 profile.brand_id → brands.owner_user_id + 分配列表 + admin 三路授权（fail-closed）。


# ============================================================
# 端点
# ============================================================

@router.get("/{profile_id}/geo-assets")
async def get_geo_assets(profile_id: int, request: Request):
    """读取 GEO 物料中心字段 + 完整度评分"""
    user = _get_user(request)
    # [GEO-R8-CAN-003] brand_access 归属校验（owner/分配代理/admin，NULL brand fail-closed）
    profile = require_profile_access(request, str(profile_id))

    industry_brief = profile.get("industry_brief") or {}
    if isinstance(industry_brief, str):
        try:
            industry_brief = json.loads(industry_brief)
        except Exception:
            industry_brief = {}

    geo_assets = industry_brief.get("geo_assets") or {}
    score, detail = calculate_completeness_score(geo_assets, profile)

    return {
        "profile_id": profile_id,
        "geo_assets": geo_assets,
        "completeness_score": score,
        "completeness_detail": detail,
        "industry": profile.get("industry"),
        "city": profile.get("city"),
    }


@router.patch("/{profile_id}/geo-assets")
async def update_geo_assets(profile_id: int, payload: GeoAssetsUpdate, request: Request):
    """部分更新 GEO 物料字段（JSONB merge）"""
    from db.connection import get_db

    user = _get_user(request)
    # [GEO-R8-CAN-003] brand_access 归属校验（owner/分配代理/admin，NULL brand fail-closed）
    profile = require_profile_access(request, str(profile_id))

    update_dict = payload.model_dump(exclude_none=True, mode="json")

    # [GEO-R8-CAN-005] 读-改-写全部放进同一事务，并对目标行加 SELECT ... FOR UPDATE 悲观锁，
    # 消除并发 PATCH 的 lost update：之前 read（get_profile 独立连接）与 write 分离且无行锁，
    # 两个 PATCH 各自读到旧 industry_brief 再整体覆盖，后写者会抹掉先写者改的字段。
    # get_db() autocommit=False，行锁持有到 with 块结束 commit 才释放。
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT industry_brief FROM client_profiles WHERE id = %s FOR UPDATE",
                (profile_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="档案不存在")

            # 1. 读锁定后的最新 industry_brief（不能沿用授权时读到的旧快照）
            existing_brief = row.get("industry_brief") or {}
            if isinstance(existing_brief, str):
                try:
                    existing_brief = json.loads(existing_brief)
                except Exception:
                    existing_brief = {}
            geo_assets = existing_brief.get("geo_assets") or {}

            # 2. 应用更新（dict merge，新值覆盖旧值）
            geo_assets.update(update_dict)

            # 3. 重新算完成度
            score, detail = calculate_completeness_score(geo_assets, profile)

            # 4. 写回（仍在锁内）
            existing_brief["geo_assets"] = geo_assets
            existing_brief["geo_completeness_score"] = score
            cursor.execute(
                "UPDATE client_profiles SET industry_brief = %s, updated_at = NOW() WHERE id = %s",
                (json.dumps(existing_brief), profile_id),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[GEO-Assets] 更新失败: {e}")
        raise HTTPException(status_code=500, detail=f"更新失败: {e}")

    # 5. 落操作日志
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_action_logs (
                  user_id, action_type, action_detail,
                  user_confirmed_at, ip_address
                ) VALUES (%s, 'geo_assets_updated', %s, NOW(), %s)
            """, (
                user["user_id"],
                json.dumps({
                    "profile_id": profile_id,
                    "updated_fields": list(update_dict.keys()),
                    "score": score,
                }),
                request.client.host if request.client else "unknown",
            ))
    except Exception as e:
        logger.warning(f"[GEO-Assets] 操作日志写入失败: {e}")

    return {
        "success": True,
        "profile_id": profile_id,
        "completeness_score": score,
        "completeness_detail": detail,
        "updated_fields": list(update_dict.keys()),
    }
