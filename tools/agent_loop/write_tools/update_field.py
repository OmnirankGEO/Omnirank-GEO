"""Update a concrete profile field with ai_inferred safety gates.

修复(v1.3 wire-up):
- H-P0-4 (Audit H) · 任意 kwarg 注入修 · 加 PROFILE_BASIC_ALLOWED_KEYS allowlist
  · 配合 owner_guard None check(H-P0-3)+ web_visit prompt injection 防御 web SSRF + url_check DNS rebinding(P0-15)三层防御
  · 修前 update_profile(profile_id, **{field: value}) 无 allowlist
    LLM 受 injection 可改 is_admin=true / role='admin' / owner_user_id 等敏感字段
"""

from __future__ import annotations

from typing import Any

from db.intake_db import SOCIAL_FIELD_KEYS, append_profile_social_field_items, update_profile_social_fields
from db.profile_db import update_profile
from db.profile_memory_db import record_profile_memory_event

# H-P0-4 fix · LLM 可改的 profile 基础字段白名单(防 kwarg 注入)
# 不允许改 owner_user_id / id / is_admin / role / brand_id / created_at / updated_at 等敏感字段
PROFILE_BASIC_ALLOWED_KEYS = {
    "name",  # 客户名
    "brand_name",  # 品牌名
    "industry",  # 行业
    "business",  # 业务描述
    "products",  # 产品
    "services",  # 服务
    "target_users",  # 目标客户
    "selling_points",  # 卖点
    "pain_points",  # 痛点
    "success_cases",  # 成功案例
    "testimonials",  # 证言
    "company_size",  # 公司规模
    "founded_year",  # 创立年份
    "location",  # 所在地
    "website",  # 网址
    "phone",  # 联系电话
    "email",  # 邮箱
    "description",  # 简介
}


async def update_profile_field(
    *,
    profile_id: str,
    field: str,
    value: str,
    source: str,
    confidence: float = 0.8,
    ctx_user_id: int | None = None,
) -> dict[str, Any]:
    # P1-5 (Audit B) + SSOT §2.1.b · ai_inferred 必经 pending_confirm · 不允许直改 profile
    if source == "ai_inferred":
        event_id = record_profile_memory_event(
            profile_id=profile_id,
            source="agent_loop",
            event_type="profile_field_update",
            title=f"待确认:{field}",
            text=value,
            dimension=field,
            canonical_concept=_concept_for_field(field),
            raw_payload={"tool": "update_profile_field", "field": field, "value": value, "source": source},
            confidence=confidence,
            user_id=ctx_user_id,
            review_status="pending",
        )
        return {"status": "pending_confirm", "event_id": event_id, "field": field}

    # user_explicit 路径 · 但仍要 allowlist 防注入(H-P0-4 fix)
    if field in SOCIAL_FIELD_KEYS:
        changed = append_profile_social_field_items(profile_id, field, [value], source="agent_loop")
        if not changed:
            changed = update_profile_social_fields(profile_id, {field: value})
    elif field in PROFILE_BASIC_ALLOWED_KEYS:
        # H-P0-4 fix · 仅 allowlist 白名单字段允许直写 profile 表
        changed = update_profile(profile_id, **{field: value})
    else:
        # 字段不在 allowlist · 拒绝(防 LLM 改 is_admin / role / owner_user_id 等)
        return {
            "status": "rejected_field_not_allowed",
            "field": field,
            "error": f"field '{field}' is not in PROFILE_BASIC_ALLOWED_KEYS or SOCIAL_FIELD_KEYS · "
                     f"不允许直改敏感 profile 字段 · 走 record_profile_memory_event pending_confirm 路径",
            "allowed_basic_keys_count": len(PROFILE_BASIC_ALLOWED_KEYS),
            "allowed_social_keys_count": len(SOCIAL_FIELD_KEYS),
        }
    return {"status": "auto", "changed": bool(changed), "field": field}


def _concept_for_field(field: str) -> str:
    if field in {"target_users", "target_audience", "customer_faq"}:
        return "target_customer"
    if field in {"persona_tone", "speaking_style", "creator_archetype", "preferred_script_structure"}:
        return "voice_style"
    if field in {"content_taboo", "disliked_style", "brand_constraints"}:
        return "guardrails"
    if field in {"products", "selling_points", "success_cases", "testimonials", "real_cases"}:
        return "offer_and_proof"
    return "business_identity"
