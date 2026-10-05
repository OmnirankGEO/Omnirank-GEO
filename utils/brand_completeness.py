"""
brand_completeness — 品牌营销资料完善度统一算法
CTO-15.3 2026-04-20 · 多维度公式 · 100 分制
CTO-15.9 2026-04-25 · M1c T2 · 分级 industry_brief_status + 新增 market_insight 组

老板反馈:"这么多维度, 你要不要参考一下"
→ 整合 BrandDetailPage 所有字段(brand + profile 跨表), 5 组权重打分.

### 维度分布(100 分 · M1c T2 调整):

**A. 基础身份(30 分)**:
  - brand.name(非"XXX的创作空间_N"占位)— 10
  - brand.industry — 10
  - brand.company_name — 10

**B. 核心业务(30 分)**:
  - profile.business — 10
  - profile.target_users — 10
  - brand.cities — 5
  - profile.products 非空 list — 5

**C. 深度营销(20 分)· CTO-13.0 v3.6 S1.1**:
  - profile.company_intro — 4
  - profile.selling_points — 4
  - profile.success_cases — 4
  - profile.core_value — 4
  - profile.testimonials — 4

**D. 行业洞察分析(10 分 · M1c T2 从 20 → 10 · 细分级别)**:
  - industry_brief_status == 'done' AND industry_brief_confirmed == True → 10(代理签字)
  - industry_brief_status == 'done' AND NOT confirmed → 7(AI 补齐但未签)
  - industry_brief_status == 'running' → 2(进行中 · 兜底不为 0)
  - industry_brief_status in (None, 'idle', 'failed') → 0

**E. 市场洞察(10 分 · M1c T2 新增 · autofill 扩 5 字段之一)**:
  - profile.service_scope(local/national/hybrid)— 2
  - profile.local_competitors 非空 list — 2
  - profile.industry_brief.authority_sources 非空 list — 2(flatten 自 brief "术"层)
  - profile.industry_brief.hot_formats 非空 list — 2(flatten 自 brief "术"层)
  - profile.industry_brief.my_differentiation 非空 — 2

### 使用方式:
  from utils.brand_completeness import compute_brand_completeness
  score = compute_brand_completeness(brand_dict, profile_dict)
  # returns {"score": 0-100, "groups": {...}, "missing": [...]}
"""
from __future__ import annotations

import json
import re
from typing import Any

# 注册时系统占位 brand.name 模板: "XXX的创作空间_数字"
# 这是系统自动填的不算"用户填过"
_DEFAULT_NAME_RE = re.compile(r"的创作空间_\d+$")


def _is_filled_str(v: Any) -> bool:
    """字符串非空判定(含 trim)"""
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    return False


def _is_filled_list(v: Any) -> bool:
    """list/JSON 字符串非空判定"""
    if v is None:
        return False
    if isinstance(v, list):
        return len(v) > 0
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return False
        try:
            parsed = json.loads(s)
            return isinstance(parsed, list) and len(parsed) > 0
        except Exception:
            return False
    return False


def _is_real_brand_name(name: Any) -> bool:
    """brand.name 是否用户真填的(排除占位模板)"""
    if not _is_filled_str(name):
        return False
    return not _DEFAULT_NAME_RE.search(str(name))


def _load_brief_dict(brief: Any) -> dict[str, Any] | None:
    """兼容 profile.industry_brief(JSONB 在 Postgres 返 dict · 老数据可能是 JSON 字符串)"""
    if brief is None:
        return None
    if isinstance(brief, dict):
        return brief
    if isinstance(brief, str):
        s = brief.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def compute_brand_completeness(
    brand: dict | None,
    profile: dict | None = None,
) -> dict[str, Any]:
    """计算品牌营销资料完善度(0-100).

    Args:
        brand: brand 表行字段(必传)
        profile: client_profiles 表行字段(可选, 不传则 B/C/D/E 组按空算)

    Returns:
        {
          "score": int 0-100,
          "groups": {
            "identity": int 0-30,
            "business": int 0-30,
            "marketing": int 0-20,
            "deep_analysis": int 0-10,
            "market_insight": int 0-10,
          },
          "missing": list[str] 缺失字段名(给前端提示用),
          "industry_brief_state": "confirmed|ai_filled|running|idle|failed|empty",
        }
    """
    if not brand:
        return {
            "score": 0,
            "groups": {
                "identity": 0,
                "business": 0,
                "marketing": 0,
                "deep_analysis": 0,
                "market_insight": 0,
            },
            "missing": ["brand"],
            "industry_brief_state": "empty",
        }

    brand = brand or {}
    profile = profile or {}
    missing: list[str] = []

    # ===== A. 基础身份(30) =====
    identity = 0
    if _is_real_brand_name(brand.get("name")):
        identity += 10
    else:
        missing.append("name")
    if _is_filled_str(brand.get("industry")):
        identity += 10
    else:
        missing.append("industry")
    if _is_filled_str(brand.get("company_name")):
        identity += 10
    else:
        missing.append("company_name")

    # ===== B. 核心业务(30) =====
    business = 0
    if _is_filled_str(profile.get("business")):
        business += 10
    else:
        missing.append("business")
    if _is_filled_str(profile.get("target_users")):
        business += 10
    else:
        missing.append("target_users")
    if _is_filled_str(brand.get("cities")):
        business += 5
    else:
        missing.append("cities")
    if _is_filled_list(profile.get("products")):
        business += 5
    else:
        missing.append("products")

    # ===== C. 深度营销(20)· v3.6 S1.1 整合 =====
    marketing = 0
    for field in ("company_intro", "selling_points", "success_cases", "core_value", "testimonials"):
        if _is_filled_str(profile.get(field)):
            marketing += 4
        else:
            missing.append(field)

    # ===== D. 行业洞察分析(10)· M1c T2 细分级 · 从 20 → 10 =====
    # 真实 CTO-13.0 v3.7 已建 industry_brief_status(idle/running/done/failed) +
    # industry_brief_confirmed BOOLEAN · 本函数按这 2 字段打分
    deep_analysis = 0
    status = (profile.get("industry_brief_status") or "").strip().lower()
    confirmed = bool(profile.get("industry_brief_confirmed"))
    brief = profile.get("industry_brief")
    brief_dict = _load_brief_dict(brief)
    has_brief_content = bool(brief_dict)  # dict 非空 或 string 非空

    if status == "done" and has_brief_content:
        if confirmed:
            deep_analysis = 10  # 代理签字
            brief_state = "confirmed"
        else:
            deep_analysis = 7  # AI 补齐但未签
            brief_state = "ai_filled"
            missing.append("industry_brief_confirm")
    elif status == "running":
        deep_analysis = 2  # 进行中兜底
        brief_state = "running"
        missing.append("industry_brief_running")
    elif status == "failed":
        deep_analysis = 0
        brief_state = "failed"
        missing.append("industry_brief")
    else:
        deep_analysis = 0
        brief_state = "idle"
        missing.append("industry_brief")

    # ===== E. 市场洞察(10)· M1c T2 新增 · autofill 扩 5 字段 =====
    market_insight = 0
    # E.1 service_scope(local/national/hybrid)— 2
    if _is_filled_str(profile.get("service_scope")):
        market_insight += 2
    else:
        missing.append("service_scope")
    # E.2 local_competitors 非空 list — 2
    if _is_filled_list(profile.get("local_competitors")):
        market_insight += 2
    else:
        missing.append("local_competitors")
    # E.3 authority_sources(brief "术"层 flatten)— 2
    authority_sources = (brief_dict or {}).get("authority_sources")
    if _is_filled_list(authority_sources):
        market_insight += 2
    else:
        missing.append("authority_sources")
    # E.4 hot_formats(brief "术"层 flatten)— 2
    hot_formats = (brief_dict or {}).get("hot_formats")
    if _is_filled_list(hot_formats):
        market_insight += 2
    else:
        missing.append("hot_formats")
    # E.5 my_differentiation(brief "器"层 · 客户差异化)— 2
    diff = (brief_dict or {}).get("my_differentiation")
    if _is_filled_str(diff) or _is_filled_list(diff):
        market_insight += 2
    else:
        missing.append("my_differentiation")

    score = identity + business + marketing + deep_analysis + market_insight

    return {
        "score": score,
        "groups": {
            "identity": identity,
            "business": business,
            "marketing": marketing,
            "deep_analysis": deep_analysis,
            "market_insight": market_insight,
        },
        "missing": missing,
        "industry_brief_state": brief_state,
    }
