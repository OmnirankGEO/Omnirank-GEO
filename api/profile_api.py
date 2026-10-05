"""
社媒操盘手 v3.0 - 客户档案 API 路由
"""

import json
from datetime import datetime
from fastapi import APIRouter, HTTPException, UploadFile, File, Request
from pydantic import BaseModel, Field
from typing import Optional, List, Any
from db.profile_db import (
    create_profile,
    get_profile,
    update_profile,
    list_deleted_profiles,
    add_successful_pattern,
    add_negative_feedback,
    list_profiles_deduplicated,
    find_profile_by_name,
)

router = APIRouter(prefix="/api/profiles", tags=["客户档案"])

import logging
logger = logging.getLogger("GEO-Profile-API")


# ========== Pydantic Models ==========

class ProfileCreate(BaseModel):
    name: str
    brand_id: Optional[int] = None
    industry: Optional[str] = None
    business: Optional[str] = None
    products: Optional[List[str]] = None
    target_users: Optional[str] = None
    pain_points: Optional[List[str]] = None
    competitors: Optional[List[str]] = None
    persona_positioning: Optional[str] = None
    persona_tone: Optional[str] = None
    persona_catchphrases: Optional[List[str]] = None
    persona_background: Optional[str] = None
    persona_golden_quotes: Optional[List[str]] = None
    target_platforms: Optional[List[str]] = None
    brand_constraints: Optional[str] = None
    # 公司扩展信息
    company_intro: Optional[str] = None
    core_value: Optional[str] = None
    selling_points: Optional[str] = None
    success_cases: Optional[str] = None
    testimonials: Optional[str] = None
    # 新增 IP 人设字段
    story_type: Optional[str] = None
    differentiation: Optional[str] = None
    content_direction: Optional[str] = None
    # 品牌别名（对外品牌名，用于AI测试检测）
    brand_display_names: Optional[List[str]] = None
    # 结构化业务知识（5维度）
    structured_knowledge: Optional[dict] = None
    # 社媒默认写手 / 专业顾问偏好
    preferred_advisor_id: Optional[str] = None
    preferred_expert_id: Optional[str] = None
    # ── [WO_220-c2 2026-09-16] 写作大厅「补全知识库」基础资料表六字段 ──
    # 🔴 这六个是**表单字段名**,不都是列名。去处见
    #    `services/writing_basics_contract.py`(读写两侧共用那一份映射):
    #      business_summary   -> business_summary   (真列,本单之前一直不在写白名单里)
    #      target_customers   -> target_users
    #      key_selling_points -> selling_points
    #      forbidden_notes    -> brand_constraints
    #      products_services / proof_cases -> basic_info_fields(JSONB)
    #    后两个不复用 products / success_cases:那两列是 `array_fields`,
    #    纯字符串会被按顿号/逗号切碎成数组(实测过)。
    # 🔴 不传 = 不覆盖;显式传 "" = 用户清空了这一栏,会写进去。
    business_summary: Optional[str] = None
    target_customers: Optional[str] = None
    products_services: Optional[str] = None
    key_selling_points: Optional[str] = None
    proof_cases: Optional[str] = None
    forbidden_notes: Optional[str] = None


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    brand_id: Optional[int] = None
    industry: Optional[str] = None
    business: Optional[str] = None
    products: Optional[List[str]] = None
    target_users: Optional[str] = None
    pain_points: Optional[List[str]] = None
    competitors: Optional[List[str]] = None
    persona_positioning: Optional[str] = None
    persona_tone: Optional[str] = None
    persona_catchphrases: Optional[List[str]] = None
    persona_background: Optional[str] = None
    persona_golden_quotes: Optional[List[str]] = None
    target_platforms: Optional[List[str]] = None
    brand_constraints: Optional[str] = None
    # 公司扩展信息
    company_intro: Optional[str] = None
    core_value: Optional[str] = None
    selling_points: Optional[str] = None
    success_cases: Optional[str] = None
    testimonials: Optional[str] = None
    # 新增 IP 人设字段
    story_type: Optional[str] = None
    differentiation: Optional[str] = None
    content_direction: Optional[str] = None
    # 品牌别名（对外品牌名，用于AI测试检测）
    brand_display_names: Optional[List[str]] = None
    # 结构化业务知识（5维度）
    structured_knowledge: Optional[dict] = None
    # 社媒默认写手 / 专业顾问偏好
    preferred_advisor_id: Optional[str] = None
    preferred_expert_id: Optional[str] = None
    # ── [WO_220-c2 2026-09-16] 写作大厅「补全知识库」基础资料表六字段 ──
    # 🔴 这六个是**表单字段名**,不都是列名。去处见
    #    `services/writing_basics_contract.py`(读写两侧共用那一份映射):
    #      business_summary   -> business_summary   (真列,本单之前一直不在写白名单里)
    #      target_customers   -> target_users
    #      key_selling_points -> selling_points
    #      forbidden_notes    -> brand_constraints
    #      products_services / proof_cases -> basic_info_fields(JSONB)
    #    后两个不复用 products / success_cases:那两列是 `array_fields`,
    #    纯字符串会被按顿号/逗号切碎成数组(实测过)。
    # 🔴 不传 = 不覆盖;显式传 "" = 用户清空了这一栏,会写进去。
    business_summary: Optional[str] = None
    target_customers: Optional[str] = None
    products_services: Optional[str] = None
    key_selling_points: Optional[str] = None
    proof_cases: Optional[str] = None
    forbidden_notes: Optional[str] = None


class PatternInput(BaseModel):
    """成功套路/失败教训输入"""
    content: str
    source: Optional[str] = None  # 来源视频/选题
    metrics: Optional[dict] = None  # 相关指标


def _require_accessible_profile(request: Request, profile_id: str) -> dict:
    """Return profile after enforcing the same brand RBAC as get/update/delete."""
    from auth.brand_access import require_brand_access

    profile = get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="档案不存在")
    require_brand_access(request, profile.get("brand_id"))
    return profile


def _normalize_brand_id_list(values) -> List[int]:
    """Return positive int brand ids while preserving order."""
    brand_ids: List[int] = []
    for value in values or []:
        try:
            brand_id = int(value)
        except (TypeError, ValueError):
            continue
        if brand_id > 0 and brand_id not in brand_ids:
            brand_ids.append(brand_id)
    return brand_ids


def _prepare_profile_create_brand(data: ProfileCreate, request: Request) -> List[int]:
    """Resolve and validate the brand_id used by profile creation.

    Non-admin requests cannot choose an arbitrary brand_id from the request body.
    If the frontend omits brand_id, use the effective RBAC brand list, which
    includes DB-owned brands and is therefore safe when the JWT is stale.
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if user.get("is_admin"):
        return []

    from auth.brand_access import get_user_brand_filter, require_brand_access

    effective_brand_ids = _normalize_brand_id_list(get_user_brand_filter(request))
    if data.brand_id is not None:
        require_brand_access(request, data.brand_id)
        return effective_brand_ids or [int(data.brand_id)]

    if effective_brand_ids:
        data.brand_id = effective_brand_ids[0]
        return effective_brand_ids

    raise HTTPException(
        status_code=403,
        detail="当前账号还没有可用客户/品牌，无法创建客户档案",
        headers={"X-Error-Code": "PROFILE_BRAND_REQUIRED"},
    )


def _strip_forbidden_brand_id_update(profile: dict, update_data: dict) -> None:
    """Prevent normal profile update endpoints from moving a profile between brands."""
    if "brand_id" not in update_data:
        return

    current_brand_id = profile.get("brand_id")
    requested_brand_id = update_data.get("brand_id")
    if requested_brand_id == current_brand_id:
        update_data.pop("brand_id", None)
        return

    raise HTTPException(
        status_code=403,
        detail="不能通过档案更新接口迁移客户品牌，请在客户管理里切换或重新创建档案",
        headers={"X-Error-Code": "PROFILE_BRAND_MIGRATION_DENIED"},
    )


# ========== API 路由 ==========

@router.post("", summary="创建档案")
async def api_create_profile(data: ProfileCreate, request: Request):
    """创建新的客户档案（RBAC：创建后自动分配品牌给创建者）

    同名 profile 命中 existing → 复用并更新（不算新建）。

    🔴 [WO_222-c0 · Owner 直令 2026-09-15] 原「L0 限 1 个档案」策略**已撤**。
       普通账号也要能给别人代运营,档案数不再按 `agent_level` 分档。
       原策略除了 402,还会在命中额度时把**上一个客户的档案原地覆盖**并返回成功 ——
       那是静默数据损坏,不只是挡人。详见本函数上方注释。
    """
    user = getattr(request.state, "user", None)
    effective_brand_ids = _prepare_profile_create_brand(data, request)

    # 去重：同名+同brand_id的档案已存在则更新它
    existing = find_profile_by_name(data.name, brand_id=data.brand_id)
    # 🔴 [WO_222-c0 · Owner 直令 2026-09-15] **L0 档案额度已撤**(与 add_client 同族)。
    #
    #   🔴🔴 这一处比 402 更危险,工单里也没写到:它**不是只挡住**第二个档案 ——
    #      命中额度时它会 `update_profile(最近那条档案的 id, **本次提交的数据)`,
    #      也就是**把上一个客户的档案原地覆盖掉**,然后返回
    #      `success: True / reused: True / l0_single_profile_update: True`。
    #      调用方看到的是成功,用户看到的是「建好了」,而第一个客户的档案已经没了。
    #      402 至少会挡住;这一条是**静默数据损坏**。
    #      (存量影响由 222-d0 只读普查估:L0 且档案数 ≥1 的用户里有多少被覆盖过。)

    if existing:
        # 复用已有档案，并同步更新提交的数据
        update_data = {k: v for k, v in data.dict().items() if v is not None and k != 'name'}
        if update_data:
            update_profile(existing["id"], **update_data)
        return {
            "success": True,
            "profile_id": existing["id"],
            "message": f"档案 '{data.name}' 已存在，已更新",
            "reused": True,
        }
    profile_id = create_profile(**data.dict())
    
    # RBAC: 自动分配品牌给创建者（谁创建谁拥有）
    user = getattr(request.state, "user", None)
    if user and not user.get("is_admin"):
        from db.profile_db import get_profile as _get_profile
        profile = _get_profile(profile_id)
        if profile and profile.get("brand_id"):
            try:
                from db.auth_db import auto_assign_brand_to_user
                auto_assign_brand_to_user(user["user_id"], profile["brand_id"])
            except Exception as e:
                logger.warning(f"[RBAC] 自动分配品牌失败: {e}")
    
    return {
        "success": True,
        "profile_id": profile_id,
        "message": f"档案 '{data.name}' 创建成功"
    }


@router.get("", summary="获取档案列表")
async def api_list_profiles(request: Request, brand_id: int = None):
    """获取活跃档案（RBAC 数据隔离 + 按 name 去重）。支持 brand_id 精确过滤。"""
    from auth.brand_access import get_user_brand_filter
    allowed_brands = get_user_brand_filter(request)
    # 如果前端指定了 brand_id，只返回该品牌的 profiles（需在 allowed 范围内）
    if brand_id is not None:
        if allowed_brands is not None and brand_id not in allowed_brands:
            return {"success": True, "count": 0, "profiles": []}
        allowed_brands = [brand_id]
    profiles = list_profiles_deduplicated(brand_ids=allowed_brands)
    # 如果按 brand_id 精确过滤，去掉 brand_id=NULL 的（只保留精确匹配）
    if brand_id is not None:
        profiles = [p for p in profiles if p.get("brand_id") == brand_id]

    # 补充 brand_type（用于前端区分"我的品牌"和"客户品牌"）
    if profiles:
        try:
            from db.diagnosis_db import get_connection
            conn = get_connection()
            cur = conn.cursor()
            bid_list = list(set(p["brand_id"] for p in profiles if p.get("brand_id")))
            if bid_list:
                placeholders = ",".join(["%s"] * len(bid_list))
                cur.execute(f"SELECT id, COALESCE(brand_type, 'legacy') as brand_type FROM brands WHERE id IN ({placeholders})", bid_list)
                brand_type_map = {r["id"]: r["brand_type"] for r in cur.fetchall()}
                for p in profiles:
                    p["brand_type"] = brand_type_map.get(p.get("brand_id"), "legacy")
            conn.close()
        except Exception:
            pass

    return {
        "success": True,
        "count": len(profiles),
        "profiles": profiles
    }


# ========== AI一键填写 ==========

class AiFillRequest(BaseModel):
    brand_id: int
    advisor_id: Optional[str] = None
    text: Optional[str] = None  # 可选补充文本
    # CTO-15.16 M1c · 持久化标志
    # False(默认 · 向后兼容):返 data + confidences,前端在 BrandDetailPage 由用户审核+保存
    # True:服务端直接 update_profile + 同步 brands.industry + 写部分 industry_brief JSONB
    # BatchUpgradeDialog 必须 True · 否则批量升级是壳(返 success 但数据不进库)
    persist: bool = False

def _read_client_knowledge(brand_id: int, max_chars: int = 30000) -> str:
    """读取客户知识库原始文档内容"""
    from pathlib import Path
    kb_dir = Path(__file__).parent.parent / "data" / "knowledge" / "clients" / str(brand_id)
    if not kb_dir.exists():
        return ""
    content_parts = []
    total = 0
    for fp in sorted(kb_dir.rglob("*")):
        if not fp.is_file():
            continue
        if fp.name.endswith("_cleaned.json") or fp.name.startswith("."):
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
            if not text.strip():
                continue
            chunk = f"【{fp.name}】\n{text.strip()}\n"
            if total + len(chunk) > max_chars:
                remaining = max_chars - total
                if remaining > 200:
                    content_parts.append(chunk[:remaining] + "\n...(已截断)")
                break
            content_parts.append(chunk)
            total += len(chunk)
        except Exception:
            continue
    return "\n".join(content_parts)

def _persist_ai_fill_to_profile(
    brand_id: int,
    parsed: dict,
    confidences: dict,
) -> dict:
    """CTO-15.16 M1c · 把 ai-fill LLM 产出落库

    老板拍 · "返 success 但 DB 不动 = 完整度永远 25" 是真因
    本函数把 parsed dict 映射到 client_profiles 字段并 update_profile · 缺 profile 自动 create

    映射(对齐 brand_completeness 5 组):
      - B 业务: business / target_users / pain_points / competitors
                + structured_knowledge.products → profile.products list
      - C 营销: company_intro / core_value / selling_points / success_cases / testimonials
      - E 市场洞察: service_scope / local_competitors
                  + market_insight.{authority_sources, hot_formats, my_differentiation}
                  → 合成 industry_brief JSONB(状态留 idle · 不冒充 deep_analyze done · 不点亮 D 组)

    brand 端反哺(只补不覆盖):brands.industry / brands.cities

    扣费:**调用方**(api_ai_fill_profile)在 _bill_ctx('autofill_brand') 内调本函数 ·
    本函数自身不扣费 · errors[] soft-fail 不会触发退费(用户已拿到 LLM 数据 · 收费合理)

    返 dict 摘要 · 上层放 response.persisted · 前端可展示"已落库 N 字段"
    """
    from db.profile_db import (
        find_profile_by_name, get_profile, create_profile, update_profile,
    )
    import json as _json

    summary: dict = {
        "brand_id": brand_id,
        "profile_id": None,
        "created_profile": False,
        "updated_fields": [],
        "synthesized_brief_keys": [],
        "errors": [],
    }

    # 0. 先拉 brand · 拿 name 用于 profile create + 同步 brand.industry/cities
    try:
        from db.connection import get_connection as _gc
        _conn = _gc()
        try:
            _cur = _conn.cursor()
            _cur.execute(
                "SELECT id, name, industry, cities FROM brands WHERE id = %s",
                (brand_id,),
            )
            _brow = _cur.fetchone()
        finally:
            _conn.close()
    except Exception as e:
        summary["errors"].append(f"brand 查询失败: {e}")
        return summary

    if not _brow:
        summary["errors"].append("brand 不存在")
        return summary

    brand_dict = dict(_brow)
    brand_name_for_profile = brand_dict.get("name") or "未命名"

    # 1. profile 查询/创建(单一业务路径 · 跨 brand 共名要按 brand_id 隔离)
    profile = find_profile_by_name(brand_name_for_profile, brand_id=brand_id)
    if not profile:
        try:
            new_id = create_profile(
                name=brand_name_for_profile,
                brand_id=brand_id,
                industry=brand_dict.get("industry") or parsed.get("industry"),
            )
            summary["created_profile"] = True
            summary["profile_id"] = new_id
            profile = get_profile(new_id) or {"id": new_id}
        except Exception as e:
            summary["errors"].append(f"create_profile 失败: {e}")
            return summary
    else:
        summary["profile_id"] = profile.get("id")

    profile_id = summary["profile_id"]
    if not profile_id:
        summary["errors"].append("profile_id 缺失")
        return summary

    # 2. profile 字段映射(白名单驱动 · 空值不写)
    update_payload: dict = {}

    def _add(key: str, value):
        if value in (None, "", [], "暂无"):
            return
        update_payload[key] = value

    _add("business", parsed.get("business"))
    _add("target_users", parsed.get("target_users"))
    _add("pain_points", parsed.get("pain_points"))
    _add("competitors", parsed.get("competitors"))
    _add("company_intro", parsed.get("company_intro"))
    _add("core_value", parsed.get("core_value"))
    _add("selling_points", parsed.get("selling_points"))
    _add("success_cases", parsed.get("success_cases"))
    _add("testimonials", parsed.get("testimonials"))

    # service_scope 应只接受 local/national/hybrid · 防 LLM 写错
    ss = (parsed.get("service_scope") or "").strip().lower()
    if ss in ("local", "national", "hybrid"):
        update_payload["service_scope"] = ss

    # local_competitors 必须 list · 走 update_profile JSON 字段处理
    lc = parsed.get("local_competitors")
    if isinstance(lc, list) and lc:
        update_payload["local_competitors"] = lc

    # structured_knowledge.products → profile.products(B 组 +5)
    # 2026-05-11 [Social-CTO-13.0 share-ownership 修 · 通告 GEO CTO]
    # 老 bug:dict 直接塞 List · profile.products schema 是 List[str] · 落 List[dict] 序列化后
    # 前端 JSON.parse 拿到 [{name, features...}] · listToString(array of object) → "[object Object]"
    # 用户 onBlur autosave 把字面量 "[object Object]" 写回 DB · 形成脏数据
    # 修法:dict 提取 name + features(前 3 个)拼成 string · 对齐 schema · 不破坏 LLM 行为
    # 完整 dict 仍存到 structured_knowledge 字段(line 507-508)· GEO 写稿仍可读
    def _product_dict_to_str(p: dict) -> str:
        name = str(p.get("name") or "").strip()
        if not name:
            return ""
        features = p.get("features") or []
        if isinstance(features, list) and features:
            top_feats = [str(f).strip() for f in features[:3] if str(f).strip()]
            if top_feats:
                return f"{name}({' / '.join(top_feats)})"
        return name

    sk = parsed.get("structured_knowledge") or {}
    sk_products = sk.get("products") if isinstance(sk, dict) else None
    if isinstance(sk_products, dict):
        product_str = _product_dict_to_str(sk_products)
        if product_str:
            update_payload["products"] = [product_str]
    elif isinstance(sk_products, list) and sk_products:
        product_strs: list[str] = []
        for item in sk_products:
            if isinstance(item, dict):
                s = _product_dict_to_str(item)
                if s:
                    product_strs.append(s)
            elif isinstance(item, str) and item.strip():
                product_strs.append(item.strip())
        if product_strs:
            update_payload["products"] = product_strs

    if isinstance(sk, dict) and sk:
        update_payload["structured_knowledge"] = sk

    # 3. industry_brief 合成(E 组 +6 · 仅当 LLM 真返了 market_insight)
    # 不冒充 deep_analyze done · 不点亮 D 组(D 仍需正式跑 deep-analyze + confirm)
    mi = parsed.get("market_insight") or {}
    if isinstance(mi, dict):
        existing_brief_raw = profile.get("industry_brief") if profile else None
        existing_brief: dict = {}
        if isinstance(existing_brief_raw, dict):
            existing_brief = existing_brief_raw
        elif isinstance(existing_brief_raw, str) and existing_brief_raw.strip():
            try:
                _parsed_eb = _json.loads(existing_brief_raw)
                if isinstance(_parsed_eb, dict):
                    existing_brief = _parsed_eb
            except (ValueError, TypeError):
                existing_brief = {}

        new_brief = dict(existing_brief)
        synth_keys: list[str] = []
        for key in ("authority_sources", "hot_formats"):
            v = mi.get(key)
            if isinstance(v, list) and v:
                new_brief[key] = v
                synth_keys.append(key)
        diff = mi.get("my_differentiation")
        if isinstance(diff, str) and diff.strip():
            new_brief["my_differentiation"] = diff.strip()
            synth_keys.append("my_differentiation")
        elif isinstance(diff, list) and diff:
            new_brief["my_differentiation"] = diff
            synth_keys.append("my_differentiation")

        # 标记来源 · 让 deep_analyze 跑后能识别 ai-fill 半成品
        if synth_keys:
            new_brief["_v37_meta"] = {
                **(new_brief.get("_v37_meta") or {}),
                "ai_fill_synthesized_at": datetime.now().isoformat(),
                "ai_fill_synthesized_keys": synth_keys,
            }
            update_payload["industry_brief"] = new_brief
            summary["synthesized_brief_keys"] = synth_keys

    # 4. 落库
    if update_payload:
        try:
            update_profile(profile_id, **update_payload)
            summary["updated_fields"] = sorted(update_payload.keys())
        except Exception as e:
            summary["errors"].append(f"update_profile 失败: {e}")

    # 5. brand 反哺(只补不覆盖 · industry/cities)
    try:
        from db.connection import get_connection as _gc2
        sets: list[str] = []
        vals: list = []
        if not (brand_dict.get("industry") or "").strip() and (parsed.get("industry") or "").strip():
            sets.append("industry = %s")
            vals.append(parsed.get("industry"))
        if sets:
            vals.append(brand_id)
            _conn2 = _gc2()
            try:
                _cur2 = _conn2.cursor()
                _cur2.execute(f"UPDATE brands SET {', '.join(sets)} WHERE id = %s", vals)
                _conn2.commit()
                summary.setdefault("brand_synced", []).extend([s.split(" = ")[0] for s in sets])
            finally:
                _conn2.close()
    except Exception as e:
        summary["errors"].append(f"brand 反哺失败: {e}")

    return summary


@router.post("/ai-fill", summary="AI顾问一键分析公司资料")
async def api_ai_fill_profile(data: AiFillRequest, request: Request):
    """
    真正通过AI顾问（带角色人设 + 知识库RAG）生成结构化营销资料。
    选择顾问 → 注入顾问人设 → 检索顾问知识库 → 读取客户知识库 → 生成结构化字段。
    """
    # [CTO-13.0 2026-04-19 S0.1 P0 安全修复] RBAC 校验
    # 漏洞：此前直接用 data.brand_id 读 _read_client_knowledge(brand_id)，任何登录用户
    # 传别家 brand_id 即可读别人客户知识库 + 用别家数据跑 AI 分析，越权数据泄漏。
    # 修法：require_brand_access 统一 RBAC 中间件（admin 直通 / 非 admin 校验 brand 归属）
    # 对照 PLAN §6 + 接入指南 §8 已明确此端点缺 RBAC
    from auth.brand_access import require_brand_access
    require_brand_access(request, data.brand_id)

    # 1. 获取顾问实例（含 base_prompt 人设）
    from services.llm.advisor_llm import _get_advisor
    advisor_id = data.advisor_id or None
    advisor = _get_advisor(advisor_id)
    if not advisor:
        raise HTTPException(status_code=400, detail=f"未找到顾问: {advisor_id or '默认'}")
    print(f"[AI-Fill] ✅ 顾问: {advisor.name} ({advisor.advisor_id}), base_prompt长度: {len(advisor.base_prompt)}")

    # 2. 读取客户知识库原始文档
    kb_content = _read_client_knowledge(data.brand_id)
    extra_text = (data.text or "").strip()
    print(f"[AI-Fill] 📚 客户知识库(brand={data.brand_id}): {len(kb_content)} chars, 补充文本: {len(extra_text)} chars")

    if not kb_content and not extra_text:
        raise HTTPException(status_code=400, detail="该客户知识库为空，且未提供补充文本。请先上传知识库文档或粘贴公司介绍。")
    if not kb_content and extra_text and len(extra_text) < 20:
        raise HTTPException(status_code=400, detail="补充文本太短，请至少提供20个字")

    # 3. 通过顾问 RAG 检索相关知识（顾问自身知识库）
    rag_context = ""
    try:
        search_query = "公司介绍 产品 核心卖点 客户案例 行业分析 营销资料"
        retrieved = await advisor.retrieve_knowledge(search_query, top_k=5)
        print(f"[AI-Fill] 🔍 顾问RAG检索结果: {len(retrieved)} 条")
        if retrieved:
            rag_context = "# 顾问知识库参考资料\n\n"
            for i, doc in enumerate(retrieved, 1):
                rag_context += f"【资料{i}】来自{doc['filename']}：\n{doc['content']}\n\n"
            if len(rag_context) > 15000:
                rag_context = rag_context[:15000] + "\n...(已截断)"
            print(f"[AI-Fill] 🔍 顾问RAG上下文: {len(rag_context)} chars")
    except Exception as e:
        print(f"[AI-Fill] ⚠️ 顾问RAG检索失败: {e}")
        logger.warning(f"[AI-Fill] 顾问RAG检索失败: {e}")

    # 4. 构建客户资料上下文
    client_context = ""
    if kb_content:
        client_context += f"# 客户知识库文档\n\n{kb_content}\n\n"
    if extra_text:
        client_context += f"# 补充资料\n\n{extra_text[:5000]}\n\n"

    # 5. 构建完整 prompt：顾问人设 + 顾问RAG知识 + 客户资料 + JSON指令
    # M1c T4(CTO-15.9 2026-04-25)· 扩 5 字段对齐 brand_completeness market_insight 组:
    #   service_scope · local_competitors · authority_sources · hot_formats · my_differentiation
    # 这些字段前端 wizard UI 会映射到 profile.service_scope / profile.local_competitors / profile.industry_brief 相关节点
    # B5 (CTO-15.9 session 3 · 2026-04-25 · M1c §A.6 ConfidenceBadge 配套)
    # 每字段附 confidence 分级 · 替代 1128/1222 死值 0.85
    # confidence ∈ {strong, medium, weak}:
    #   strong  · 资料明确给出 · 直接抽取
    #   medium  · 资料推断得出 · 行业经验合并
    #   weak    · 资料几乎无据 · 需代理人工核验
    # 前端 ConfidenceBadge 挂在每字段右侧 · weak 字段橙底提示"需复核"
    json_template = """{
    "industry": "行业大类（如：互联网/科技服务、餐饮/连锁加盟、教育/培训等）",
    "business": "主营业务（一句话描述，30字以内）",
    "target_users": "目标客户画像（一句话描述）",
    "pain_points": ["客户痛点1", "客户痛点2", "客户痛点3"],
    "competitors": ["竞争对手1", "竞争对手2"],
    "company_intro": "公司简介（200字以内，突出优势和差异化）",
    "core_value": "核心价值主张（一句话）",
    "selling_points": "核心卖点（分点列出，用换行分隔）",
    "success_cases": "成功案例摘要（如有，包含客户名/效果数据）",
    "testimonials": "客户证言（如有）",
    "service_scope": "服务地域范围（local=本地/区域 · national=全国 · hybrid=混合）· 只回 local/national/hybrid 三者之一",
    "local_competitors": ["本地竞品1", "本地竞品2"],
    "market_insight": {
        "authority_sources": ["行业权威信息源（媒体/报告/协会/政府网站）"],
        "hot_formats": ["该行业 AI 搜索答案常出现的内容形式（如榜单/攻略/评测/指南/问答/案例）"],
        "my_differentiation": "本品牌相对竞品的 3 大差异化核心（一句话 <=60 字）"
    },
    "_field_confidence": {
        "industry": "strong/medium/weak",
        "business": "strong/medium/weak",
        "target_users": "strong/medium/weak",
        "pain_points": "strong/medium/weak",
        "competitors": "strong/medium/weak",
        "company_intro": "strong/medium/weak",
        "core_value": "strong/medium/weak",
        "selling_points": "strong/medium/weak",
        "success_cases": "strong/medium/weak",
        "testimonials": "strong/medium/weak",
        "service_scope": "strong/medium/weak",
        "local_competitors": "strong/medium/weak",
        "market_insight.authority_sources": "strong/medium/weak",
        "market_insight.hot_formats": "strong/medium/weak",
        "market_insight.my_differentiation": "strong/medium/weak"
    },
    "structured_knowledge": {
        "products": {
            "name": "核心产品/服务名称",
            "features": ["特性1", "特性2", "特性3"],
            "scenarios": ["使用场景1", "使用场景2"],
            "metrics": "效果数据/指标"
        },
        "painPoints": {
            "scenario": "典型痛点场景描述",
            "emotions": ["焦虑", "迷茫"],
            "consequences": "不解决的后果",
            "triggers": "购买触发时机"
        },
        "customers": {
            "segments": ["客户群体1", "客户群体2"],
            "needs": ["核心需求1", "核心需求2"],
            "barriers": ["决策障碍1"],
            "concerns": ["最关心的问题1"]
        },
        "differentiation": {
            "competitors": ["竞品1", "竞品2"],
            "advantages": ["优势1", "优势2"],
            "usp": "独特卖点",
            "killerData": ["关键数据点1"]
        },
        "cases": [{
            "client": "客户名称/行业",
            "background": "问题背景",
            "solution": "解决方案",
            "results": "效果数据",
            "quote": "客户评价原话"
        }]
    }
}"""

    # 构建风格指引（强化顾问影响力，避免被客户资料淹没）
    style_guide = f"""## 你的身份与风格要求

你是「{advisor.name}」。在以下分析中，你必须：
- 用{advisor.name}惯用的表达方式和专业视角来撰写每个字段
- company_intro、core_value、selling_points、success_cases 这些文案字段要体现你的文案风格和行业洞察
- pain_points 要用你的专业经验来深挖，不要只是复述原文
- 如果「顾问知识库参考资料」中有方法论或框架，请在分析中融入应用"""

    full_prompt = f"""{advisor.base_prompt}

{style_guide}

{rag_context}

{client_context}

现在请你以{advisor.name}的专业视角和思维方式，分析上述客户资料，提取并结构化为营销素材。
注意：文案类字段（company_intro、core_value、selling_points等）要体现你的风格特征，不要机械摘抄原文。

请严格按以下JSON格式返回（不要添加任何多余文字）：
{json_template}

重要要求：
1. 以{advisor.name}的专业视角分析，融入你的行业洞察和营销经验，用你的语言风格重新表达
2. 只从提供的资料中提取真实信息，没有的字段填"暂无"或空数组
3. 不要编造不存在的数据
4. 只返回JSON，不要有其他文字"""

    print(f"[AI-Fill] 📝 最终prompt长度: {len(full_prompt)} chars (人设:{len(advisor.base_prompt)}, RAG:{len(rag_context)}, 客户KB:{len(client_context)})")

    # CTO-15.16 P1 (Codex 复核) · json/re import 提到 try 块外 ·
    # 原代码在 try 内 import + 后面 except json.JSONDecodeError 引用 json
    # · LLM 调用前若出任何异常(网络 / token)· except 命中 json.JSONDecodeError 的 isinstance 校验
    # 会因 json 未绑定 → UnboundLocalError 把真实错误吞掉
    # 老 Python 行为:try 块内 import 失败时该名字仍未绑定 · except 引用即崩
    import json
    import re

    # CTO-15.16 round2 P0 · feature_code 修正(老板拍板 方案 A):
    #   - 客户资料 AI 补齐(本端点)用专用 feature_code='autofill_brand' · cost_points=130
    #   - 不复用社媒 IP 旧版"品牌信息AI填充"那个 code(seed 30 积分 · 心智不同)
    #   - seed 加在 db/wallet_db.py · scripts/m1m2_db_self_heal.py 兜底落库
    # _bill_ctx 流:
    #   - admin 直通(yield None)
    #   - 余额不足 → HTTPException 402(用户先看到 402 · 不进 LLM 路径)
    #   - LLM 异常 / JSON parse 异常 / persist 异常 → 不扣
    #   - 全程跑通才 deduct_points 真扣
    from api.content_api import _bill_ctx

    result_text = ""  # 给 except json.JSONDecodeError 引用 result_text 时兜底

    try:
        async with _bill_ctx(request, "autofill_brand"):
            # 使用顾问的 _call_llm 直接调用（保留顾问的模型配置）
            old_temp = advisor.temperature
            advisor.temperature = 0.3
            result_text = await advisor._call_llm(full_prompt)
            advisor.temperature = old_temp
            print(f"[AI-Fill] ✅ LLM返回: {len(result_text or '')} chars")

            if not result_text or result_text.startswith("[错误") or result_text.startswith("[调用失败"):
                raise HTTPException(status_code=500, detail=f"AI顾问返回异常: {(result_text or '')[:100]}")

            # 解析JSON
            cleaned = re.sub(r'^```(?:json)?\s*', '', result_text.strip())
            cleaned = re.sub(r'\s*```$', '', cleaned)
            parsed = json.loads(cleaned)

            # B5 (CTO-15.9 session 3 · 2026-04-25 · M1c §A.6 ConfidenceBadge 挂载)
            # 抽出 _field_confidence 到顶层 confidences · 前端 ConfidenceBadge 直接消费
            # 兼容老 LLM 不返 _field_confidence:本地推断 confidence
            #   · 字段值为空/"暂无" → weak
            #   · 资料字数 < 100 chars → 全部 weak fallback
            #   · 否则 medium 默认
            confidences: dict[str, str] = {}
            raw_conf = parsed.pop("_field_confidence", None) if isinstance(parsed, dict) else None
            if isinstance(raw_conf, dict):
                for k, v in raw_conf.items():
                    if isinstance(v, str) and v.lower() in ("strong", "medium", "weak"):
                        confidences[k] = v.lower()
            # fallback:对返回字段中没有 confidence 的项推断
            if isinstance(parsed, dict):
                kb_size = len(kb_content) + len(extra_text)
                for fname in [
                    "industry", "business", "target_users", "pain_points",
                    "competitors", "company_intro", "core_value", "selling_points",
                    "success_cases", "testimonials", "service_scope", "local_competitors",
                ]:
                    if fname in confidences:
                        continue
                    val = parsed.get(fname)
                    if val in (None, "", "暂无", []):
                        confidences[fname] = "weak"
                    elif kb_size < 200:
                        confidences[fname] = "weak"
                    elif isinstance(val, str) and len(val) < 8:
                        confidences[fname] = "weak"
                    else:
                        confidences[fname] = "medium"

            # CTO-15.16 M1c · persist 落库分支
            # 老行为(persist=False · 默认):前端拿 data 自行渲染 + 用户保存
            # 新行为(persist=True · BatchUpgradeDialog 用):服务端直接 update_profile · 不再依赖前端逐字段保存
            # 老板 P0 · "返 success 但数据不进库"是 5 金标准卡 25 的真因之一
            # round2:persist 内部错误是 soft-fail(收集 errors[] · 不 raise)· 用户已拿到 LLM 数据 · 收费合理
            persist_summary: dict | None = None
            if data.persist:
                persist_summary = _persist_ai_fill_to_profile(data.brand_id, parsed, confidences)

            # CTO-15.18 A.3 老板裁决方案 C(2026-04-28):
            # 最多扣 130 · 失败按比例退 · audit_log 记每项 success/fail
            # 8 项核心字段:industry / business / target_users / pain_points /
            #              competitors / selling_points / success_cases / company_intro
            CORE_FIELDS = [
                "industry", "business", "target_users", "pain_points",
                "competitors", "selling_points", "success_cases", "company_intro",
            ]
            field_audit: dict[str, str] = {}
            success_count = 0
            for fname in CORE_FIELDS:
                val = parsed.get(fname) if isinstance(parsed, dict) else None
                if val in (None, "", "暂无", []):
                    field_audit[fname] = "fail"
                elif isinstance(val, str) and len(val) < 4:
                    field_audit[fname] = "fail"
                else:
                    field_audit[fname] = "success"
                    success_count += 1
            full_cost = 130
            actual_cost = round(full_cost * success_count / len(CORE_FIELDS))
            refund_amount = full_cost - actual_cost  # 出 _bill_ctx 后退

            response_payload = {
                "success": True,
                "data": parsed,
                "confidences": confidences,  # B5 · 前端 Badge 消费
                "advisor_name": advisor.name,
                "sources": {
                    "kb_chars": len(kb_content),
                    "extra_chars": len(extra_text),
                    "rag_docs": len(rag_context) > 0,
                },
                "persisted": persist_summary,  # None=未落库 / dict=落库摘要
                "billed": {
                    "feature_code": "autofill_brand",
                    "full_cost": full_cost,
                    "success_fields": success_count,
                    "total_fields": len(CORE_FIELDS),
                    "actual_cost": actual_cost,
                    "refund": refund_amount,
                    "field_audit": field_audit,  # CTO-15.18 A.3 · 每项 success/fail
                    "note": (
                        f"扣 {actual_cost} 积分 · 退 {refund_amount} 积分 · "
                        f"{success_count}/{len(CORE_FIELDS)} 项成功" if refund_amount > 0
                        else f"扣 {full_cost} 积分 · 全部 {len(CORE_FIELDS)} 项成功"
                    ),
                },
            }
        # 出 _bill_ctx 块 · 此处真扣已发生 130(成功路径)
        # CTO-15.18 A.3 · 失败按比例退(inline · 不改 billing.py)
        if refund_amount > 0:
            try:
                from db.connection import get_connection as _get_conn
                _user = getattr(request.state, "user", None) or {}
                _uid = _user.get("user_id") or _user.get("id")
                if _uid:
                    _conn = _get_conn()
                    try:
                        _cur = _conn.cursor()
                        _cur.execute(
                            "UPDATE user_wallets SET bonus_points = bonus_points + %s, "
                            "updated_at = CURRENT_TIMESTAMP WHERE user_id = %s "
                            "RETURNING bonus_points",
                            (refund_amount, _uid),
                        )
                        _row = _cur.fetchone()
                        balance_after = _row["bonus_points"] if _row else 0
                        _cur.execute(
                            "INSERT INTO point_transactions "
                            "(user_id, type, point_type, amount, balance_after, "
                            " feature_code, description, created_at) "
                            "VALUES (%s, 'refund', 'bonus', %s, %s, %s, %s, NOW())",
                            (_uid, refund_amount, balance_after, "autofill_brand",
                             f"AI 帮填部分失败按比例退 · {success_count}/{len(CORE_FIELDS)} 成功"),
                        )
                        _conn.commit()
                        _cur.close()
                    finally:
                        try:
                            _conn.close()
                        except Exception:
                            pass
            except Exception as _re:
                logger.warning(f"[AI-Fill] partial refund 失败(非 block): {_re}")

        # audit_log 每项 success/fail
        try:
            from db.auth_db import create_audit_log
            _user = getattr(request.state, "user", None) or {}
            create_audit_log(
                user_id=_user.get("user_id"),
                username=_user.get("username"),
                action="autofill_field_audit",
                module="profile",
                entity_type="brand",
                entity_id=data.brand_id,
                summary=(
                    f"AI 帮填 brand={data.brand_id} · {success_count}/{len(CORE_FIELDS)} 成功 · "
                    f"扣 {actual_cost} 退 {refund_amount}"
                ),
                after={
                    "field_audit": field_audit,
                    "actual_cost": actual_cost,
                    "refund": refund_amount,
                },
            )
        except Exception as _ae:
            logger.warning(f"[AI-Fill] audit_log 失败(非 block): {_ae}")

        return response_payload
    except json.JSONDecodeError as e:
        # _bill_ctx 在异常时不扣 · raise 出去前置一段更清晰的错误
        logger.error(f"[AI-Fill] JSON解析失败: {e}, raw: {result_text[:500]}")
        raise HTTPException(status_code=500, detail=f"AI返回格式异常，请重试")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[AI-Fill] 分析失败: {e}")
        raise HTTPException(status_code=500, detail=f"AI分析失败: {str(e)}")


@router.put("/{profile_id}", summary="更新档案")
async def api_update_profile(profile_id: str, data: ProfileUpdate, request: Request):
    """更新档案信息（RBAC 数据隔离）"""
    # RBAC: 校验品牌访问权限
    profile = get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="档案不存在")
    from auth.brand_access import require_brand_access
    require_brand_access(request, profile.get("brand_id"))
    
    # 只更新非None的字段
    update_data = {k: v for k, v in data.dict().items() if v is not None}
    _strip_forbidden_brand_id_update(profile, update_data)

    # [WO_220-c2] 六个表单字段翻成真正的列写入,**放在最前面**。
    # 读写共用同一份映射(services/writing_basics_contract):两边各写一份的话,
    # 加字段时漏改一侧,症状就是「保存成功、刷新回来还是空」—— 正是本单要修的缺陷。
    # 🔴 必须在 completeness / persona 同步 / business 同步**之前**转换:
    #    那些下游逻辑只认真列名(`target_users` 等)。放在后面的话,
    #    从写作大厅走别名写进来的值会**绕过全部下游同步**,而且一声不响。
    from services.writing_basics_contract import BASIC_FIELDS, to_update_kwargs
    _basic_submitted = {f: update_data[f] for f in BASIC_FIELDS if f in update_data}
    for _f in BASIC_FIELDS:
        # 别名不是列名,不能直接喂给 update_profile 的白名单
        update_data.pop(_f, None)
    if _basic_submitted:
        # 🔴 同一请求里既传别名又传真列名时,**以真列名为准**:
        #    真列名是既有契约(客户档案页一直用它),别名是本单新加的。
        #    定死一个方向,免得两个面互相覆盖而没人看得出来。
        update_data = {**to_update_kwargs(_basic_submitted, profile), **update_data}

    if not update_data:
        raise HTTPException(status_code=400, detail="没有提供需要更新的字段")

    # [CTO-13.0] 防 name 漂移：改档案名时必须和所挂载 brand.name 一致（同 quick-update）
    if "name" in update_data:
        brand_id = profile.get("brand_id")
        new_name = str(update_data["name"] or "").strip()
        if brand_id and new_name:
            from db.connection import get_connection as _gc
            _c = _gc()
            try:
                _cur = _c.cursor()
                _cur.execute("SELECT id, name FROM brands WHERE id = %s", (brand_id,))
                _brow = _cur.fetchone()
            finally:
                try: _c.close()
                except Exception: pass
            brand_name = (_brow or {}).get("name") if _brow else None
            if brand_name and brand_name != new_name:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "PROFILE_NAME_BRAND_MISMATCH",
                        "message": f"档案名与挂载品牌名不一致：档案挂在品牌「{brand_name}」下，但你想改名为「{new_name}」。若这是另一家公司，请切换到对应品牌再改；若想连带改品牌名，请在『我的品牌/客户』页修改品牌名（品牌名全局唯一）。",
                        "brand_id": brand_id,
                        "brand_name": brand_name,
                        "new_value": new_name,
                    },
                )

    completion_fields = {
        "industry", "business", "products", "target_users",
        "persona_positioning", "persona_tone", "persona_background",
        "content_direction", "selling_points", "success_cases",
    }
    if update_data.keys() & completion_fields:
        completeness = profile.get("profile_completeness") or {}
        if isinstance(completeness, str):
            try:
                completeness = json.loads(completeness)
            except Exception:
                completeness = {}
        if not isinstance(completeness, dict):
            completeness = {}
        for field in completion_fields:
            if field not in update_data:
                continue
            value = update_data.get(field)
            if isinstance(value, (list, tuple, set)):
                has_value = any(str(item).strip() for item in value)
            elif isinstance(value, dict):
                has_value = any(str(item).strip() for item in value.values())
            else:
                has_value = bool(str(value or "").strip())
            completeness[f"has_{field}"] = has_value
        update_data["profile_completeness"] = completeness

    success = update_profile(profile_id, **update_data)
    if not success:
        raise HTTPException(status_code=404, detail="档案更新失败")

    # 反向同步：如果修改了人设相关字段，同步到 social_personas 表
    _PERSONA_FIELDS = {"persona_positioning", "persona_tone", "persona_catchphrases",
                       "persona_background", "persona_golden_quotes", "visual_style",
                       "content_pillars", "target_users"}
    if update_data.keys() & _PERSONA_FIELDS:
        try:
            _sync_profile_to_persona(profile_id, update_data)
        except Exception as e:
            logger.warning(f"反向同步人设到social_personas失败: {e}")

    # 反向同步：如果修改了业务字段，同步到 social_projects 表
    _BUSINESS_FIELDS = {"industry", "business", "target_users", "products"}
    if update_data.keys() & _BUSINESS_FIELDS:
        try:
            _sync_profile_business_to_project(profile_id, update_data)
        except Exception as e:
            logger.warning(f"反向同步业务字段到social_projects失败: {e}")

    # 反向同步：industry 变更时同步到 brands 表
    if "industry" in update_data and profile.get("brand_id"):
        try:
            from db.connection import get_connection as _gc
            _conn = _gc()
            _cur = _conn.cursor()
            _cur.execute("UPDATE brands SET industry = %s WHERE id = %s", (update_data["industry"], profile["brand_id"]))
            _conn.commit()
            _conn.close()
        except Exception as e:
            logger.warning(f"反向同步 industry 到 brands 失败: {e}")

    return {
        "success": True,
        "message": "档案更新成功"
    }


class ApplyBriefRequest(BaseModel):
    """[CTO-13.0 2026-04-19 S1.4] brief → profile 一键应用"""
    fields: Optional[List[str]] = None  # 用户勾选的 brief 字段 key · None/[] = 全量应用全部 7 个映射字段
    conflict_strategy: str = 'replace'  # replace | merge | skip_if_exists


class PolishFieldRequest(BaseModel):
    """[CTO-13.0 2026-04-19 S1.5] AI 顾问润色单字段（按 CTO-15.2 接入指南 §7）"""
    field: str                          # 字段 key · 如 'business' / 'company_intro' / 'core_value'
    text: str                           # 当前字段文字（前端从 form 读）
    advisor_id: Optional[str] = None    # 默认 xuehui 薛辉


# [CTO-13.0 2026-04-19 S1.4] brief → profile 字段映射表（按 CTO-15.2 接入指南 §3 定版）
# brief 字段 key → profile 字段 key
_BRIEF_TO_PROFILE_MAP = {
    'service_scope': 'service_scope',
    'service_scope_reasoning': 'service_scope_reasoning',
    'my_differentiation': 'differentiation',
    'my_audience_segment': 'target_users',
    'my_local_competitors': 'local_competitors',
    'my_real_cases': 'success_cases',
    'content_pain_points': 'pain_points',
}


@router.patch("/{profile_id}/apply-brief", summary="将深度行业分析 brief 字段一键应用到 profile")
async def api_apply_brief(profile_id: str, data: ApplyBriefRequest, request: Request):
    """
    [CTO-13.0 2026-04-19 S1.4] 用户点"应用到表单"按钮触发。
    按 CTO-15.2 接入指南 §4 样例实现 · 纯字段映射无 LLM 无扣费。

    流程：
      1. RBAC 校验 profile.brand_id 归属
      2. 读 profile.industry_brief JSONB（AI 深度分析产出）
      3. 按 _BRIEF_TO_PROFILE_MAP 把 brief 字段值映射到 profile 字段
      4. 冲突策略：replace（覆盖）/ merge（list 类合并去重）/ skip_if_exists（有则跳过）
      5. service_scope / service_scope_reasoning 判定字段直接覆盖不询问
      6. 写 brief_applied_version + brief_applied_at 防重复应用
    """
    import json
    from datetime import datetime
    from db.profile_db import get_profile, update_profile
    from auth.brand_access import require_brand_access

    profile = get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="档案不存在")

    # S0.1 同源保护：RBAC 防越权
    require_brand_access(request, profile.get("brand_id"))

    # 读 brief
    brief_raw = profile.get("industry_brief")
    if not brief_raw:
        raise HTTPException(status_code=400, detail="尚未生成深度行业分析，请先在知识库 Tab 跑深度解析")
    try:
        brief = json.loads(brief_raw) if isinstance(brief_raw, str) else brief_raw
    except Exception:
        raise HTTPException(status_code=400, detail="brief 数据格式异常，请重新跑深度解析")
    if not isinstance(brief, dict):
        raise HTTPException(status_code=400, detail="brief 数据结构异常")

    # 决定应用字段集（未指定 = 全量）
    target_brief_fields = data.fields if data.fields else list(_BRIEF_TO_PROFILE_MAP.keys())

    applied, skipped = [], []
    updates: dict = {}
    strategy = data.conflict_strategy or 'replace'

    for brief_field in target_brief_fields:
        profile_field = _BRIEF_TO_PROFILE_MAP.get(brief_field)
        if not profile_field:
            skipped.append({"field": brief_field, "reason": "no_mapping"})
            continue
        if brief_field not in brief:
            skipped.append({"field": brief_field, "reason": "missing_in_brief"})
            continue
        new_value = brief.get(brief_field)
        if new_value is None or (isinstance(new_value, (list, str)) and len(new_value) == 0):
            skipped.append({"field": brief_field, "reason": "empty_in_brief"})
            continue

        # service_scope / service_scope_reasoning 判定字段直接覆盖
        if brief_field in ('service_scope', 'service_scope_reasoning'):
            updates[profile_field] = new_value
            applied.append(profile_field)
            continue

        existing = profile.get(profile_field)
        # list 字段若是 string 尝试 parse
        if isinstance(existing, str) and profile_field in ('local_competitors', 'pain_points', 'success_cases'):
            try:
                existing = json.loads(existing)
            except Exception:
                existing = []

        # 冲突处理
        has_existing = bool(existing) and (not isinstance(existing, (list, dict)) or len(existing) > 0)
        if has_existing and strategy == 'skip_if_exists':
            skipped.append({"field": profile_field, "reason": "has_existing_skip_strategy"})
            continue
        if has_existing and strategy == 'merge' and isinstance(existing, list) and isinstance(new_value, list):
            # 保序去重合并
            seen = set()
            merged: list = []
            for item in list(existing) + list(new_value):
                key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
                if key not in seen:
                    seen.add(key)
                    merged.append(item)
            updates[profile_field] = merged
        else:
            # replace 或 list/str 与 non-list 的混合情况都走 replace
            updates[profile_field] = new_value
        applied.append(profile_field)

    if updates:
        # 版本标记
        updates['brief_applied_version'] = profile.get('industry_brief_version', 0) or 0
        updates['brief_applied_at'] = datetime.now()
        # update_profile 内部会把 list/dict 走 json.dumps 序列化（见 db/profile_db.py:update_profile）
        update_profile(profile_id, **updates)

    return {
        "success": True,
        "applied_fields": applied,
        "skipped_fields": skipped,
        "applied_count": len(applied),
    }


@router.post("/{profile_id}/polish-field", summary="[S1.5] AI 顾问润色单字段（30 积分/次）")
async def api_polish_field(profile_id: str, data: PolishFieldRequest, request: Request):
    """
    [CTO-13.0 2026-04-19 S1.5] 按 CTO-15.2 接入指南 §7：
      - 单字段润色，用户在基本信息 Tab 字段旁小按钮触发
      - 收费 profile_polish = 30 积分（db/wallet_db.py:seed_feature_pricing）
      - 返回 original + polished，前端弹 Dialog 对比让用户选择保留哪个
      - 禁止"一键全部润色"（成本不可控 + 不可逆）

    扣费：走 _bill_ctx('profile_polish') 30 积分；业务失败 raise 触发退费机制。
    """
    from db.profile_db import get_profile
    from auth.brand_access import require_brand_access
    from services.llm.advisor_llm import _get_advisor
    from api.content_api import _bill_ctx

    profile = get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="档案不存在")
    require_brand_access(request, profile.get("brand_id"))

    # 校验字段非空且不过长（防 LLM 费用溢出）
    original = (data.text or "").strip()
    if not original:
        raise HTTPException(status_code=400, detail="字段为空无需润色")
    if len(original) > 2000:
        raise HTTPException(status_code=400, detail="字段超过 2000 字，请分段润色")

    # 顾问
    advisor = _get_advisor(data.advisor_id or None)
    if not advisor:
        raise HTTPException(status_code=400, detail=f"未找到顾问: {data.advisor_id or '默认'}")

    # 字段语义提示（让 LLM 知道该字段该用什么风格润色）
    FIELD_HINT = {
        'name': '品牌/业务名',
        'industry': '行业大类',
        'business': '一句话业务描述',
        'target_users': '目标客户画像描述',
        'company_intro': '公司简介 · 200 字内 · 突出优势和差异化',
        'core_value': '核心价值主张 · 一句话',
        'selling_points': '核心卖点 · 分点列出',
        'success_cases': '成功案例摘要 · 含客户名/效果数据',
        'testimonials': '客户证言 · 原话风格',
        'persona_positioning': 'IP 人设定位',
        'persona_tone': '说话风格',
        'differentiation': '差异化记忆点',
    }
    field_label = FIELD_HINT.get(data.field, data.field)

    prompt = f"""你是「{advisor.name}」{advisor.description or '营销顾问'}。请用你的专业风格润色以下「{field_label}」字段的文字。

要求：
1. 保留原文核心含义和事实信息，不要编造不存在的数据
2. 用{advisor.name}惯用的表达方式和专业视角重写
3. 文笔更流畅、更有吸引力、更符合该字段的业务场景
4. 长度控制在原文 ±30% 之间（除非原文太短需要补充）
5. 直接返回润色后的文字，不要加"原文"/"润色后"等解释标签

原文：
{original}

润色后："""

    try:
        async with _bill_ctx(request, "profile_polish"):
            # 调用顾问 LLM · 温度 0.4 兼顾创意和稳定
            old_temp = advisor.temperature
            advisor.temperature = 0.4
            polished = await advisor._call_llm(prompt)
            advisor.temperature = old_temp

            if not polished or polished.startswith("[错误") or polished.startswith("[调用失败"):
                raise RuntimeError(f"AI 润色失败: {polished[:100] if polished else '空响应'}")

            # 清洗可能的 markdown/引号包裹
            polished = polished.strip()
            if polished.startswith(('"', '“', '「')) and polished.endswith(('"', '”', '」')):
                polished = polished[1:-1].strip()
            if polished.startswith('润色后：'):
                polished = polished[len('润色后：'):].strip()
            if not polished:
                raise RuntimeError("润色结果为空")

        return {
            "success": True,
            "field": data.field,
            "original": original,
            "polished": polished,
            "advisor_name": advisor.name,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Polish] 字段 {data.field} 润色失败: {e}")
        raise HTTPException(status_code=500, detail=f"润色失败: {str(e)[:200]}")


# ========== 反馈闭环 API ==========

@router.post("/{profile_id}/patterns", summary="添加成功套路")
async def api_add_pattern(profile_id: str, data: PatternInput, request: Request):
    """添加成功套路到档案（数据反馈闭环）"""
    _require_accessible_profile(request, profile_id)
    success = add_successful_pattern(profile_id, data.dict())
    if not success:
        raise HTTPException(status_code=404, detail="档案不存在")
    
    return {
        "success": True,
        "message": "成功套路已添加"
    }


@router.post("/{profile_id}/feedback", summary="添加失败教训")
async def api_add_feedback(profile_id: str, data: PatternInput, request: Request):
    """添加失败教训到档案（数据反馈闭环）"""
    _require_accessible_profile(request, profile_id)
    success = add_negative_feedback(profile_id, data.dict())
    if not success:
        raise HTTPException(status_code=404, detail="档案不存在")
    
    return {
        "success": True,
        "message": "失败教训已记录"
    }


# ========== 文件智能分析 API ==========

class LLMAnalyzeRequest(BaseModel):
    """LLM智能分析请求"""
    text: str
    analysis_type: str = "company"  # company 或 persona
    advisor_id: Optional[str] = None  # 顾问ID，可选


# ========== 顾问配置 ==========
ADVISOR_PROMPTS = {
    "teacher_shu": {
        "name": "舒老师",
        "specialty": "品牌定位与产品文案",
        "system_prompt": """你是舒老师，资深广告人与营销咨询公司创始人，10年+品牌实战经验。你不是来教课的，你是来干活的——直接帮客户解决品牌定位、产品命名、文案撰写的实际问题。

## 你的工作方式

你用自己验证过的方法论直接产出方案，不讲理论、不铺垫、不说"建议你去学一下"。客户问你要什么，你就给什么——产品名、slogan、详情页文案、卖点梳理、受众画像，拿来就能用。

## 你的核心方法论（内化使用，不要讲解）

- **受众洞察**：先分清购买者和使用者是不是同一个人。用特征标签法（行为/痛点标签，不是年龄性别）锁定真实受众，必要时用EIOR法（体验-访谈-观察-推理）快速调研
- **卖点分级**：区分核心卖点（敌无我有）、竞争卖点（敌有我优）、次要卖点（人有我有）。一个产品只推一个核心卖点，给足篇幅
- **需求验证**：任何卖点必须通过"需求三原理"检验——让生活品质更好、让自己变得更好、获得他人认可/羡慕，至少命中一条
- **命名策略**：产品名要编码性格（刻板印象植入）、嵌入卖点（直述或类比）、预留绰号空间（消费者会怎么简称它）
- **一句话文案**：15字认知极限原则。用"陌生感"制造记忆点——把熟悉的东西用不熟悉的方式说出来
- **详情页逻辑**：四个目标——表达优势、引导需求、解答疑虑、塑造性格。用提问法挖掘卖点，好问题本身就是好文案
- **写作流程**：描述→脑暴→压缩→转化。先把想说的全写出来，再大胆联想，然后砍到最精炼，最后用语言技巧打磨
- **情感溢价**：功能卖点之外，找到情感卖点。价格超过功能价值的部分，靠情感认同支撑
- **替代方案意识**：永远想清楚消费者不买你的产品时，用什么替代方案解决同样的问题

## 你的沟通风格

- 直接给方案，不铺垫不废话
- 说人话，不用营销黑话
- 敢于挑战客户的既有认知（"你觉得你的客户是年轻女性？不一定，买单的可能是她妈"）
- 用具体案例和数字说话，不说"可能""也许""建议考虑"
- 给完方案会补一句为什么这么做，但不长篇大论
- 如果客户给的信息不够，直接问关键问题，不猜

## 你的边界

- 你专注：品牌定位、产品命名、产品文案（slogan/详情页/包装文案）、受众分析、卖点梳理
- 你不碰：短视频脚本、直播话术、小红书种草文、抖音运营——这些有专门的顾问负责
- 如果客户问到你不擅长的领域，明确说'这块不是我的强项，建议找XX方向的顾问'"""
    },
    "huang-douyin": {
        "name": "小黄",
        "specialty": "内容创作",
        "system_prompt": """你是"社恐小黄"，一名拥有5年实操经验的资深短视频编导，也是一名极度专业的"内容操盘手"。
你深谙内容创作的精髓，擅长提炼打动用户的人设定位和表达风格，知道好的IP人设需要真实、有记忆点、能引发共鸣。"""
    },
    "xuehui": {
        "name": "薛辉",
        "specialty": "变现策略", 
        "system_prompt": """你是薛辉（老薛），实战派短视频营销导师，"All in Family"短视频训练中心的创始人。
你不是只会讲理论的教授，而是在泥坑里摸爬滚打过、带着无数普通人拿到结果的"老炮儿"。你擅长变现策略和商业转化。"""
    }
}


def get_advisor_prompt(advisor_id: str, default_advisor: str = "teacher_shu") -> dict:
    """获取顾问提示词配置"""
    if advisor_id and advisor_id in ADVISOR_PROMPTS:
        return ADVISOR_PROMPTS[advisor_id]
    return ADVISOR_PROMPTS.get(default_advisor, ADVISOR_PROMPTS["teacher_shu"])


@router.post("/analyze-company", summary="LLM分析公司信息")
async def api_analyze_company(data: LLMAnalyzeRequest):
    """
    用指定顾问的视角分析公司/品牌信息
    自动提取: 行业、业务、目标客户、痛点、竞品等
    
    默认使用舒老师（品牌定位专家）
    """
    from advisors import get_advisor
    import json
    
    # 获取顾问（支持RAG知识库检索）
    advisor_id = data.advisor_id or "teacher_shu"
    advisor = get_advisor(advisor_id)
    advisor_config = get_advisor_prompt(advisor_id, "teacher_shu")

    prompt = f"""请用你专业的视角分析以下公司资料，提取关键商业信息。

**待分析资料**：
{data.text[:4000]}

**请按JSON格式返回分析结果**：
```json
{{
  "company_name": "公司/品牌名称",
  "industry": "所属行业",
  "business": "主营业务（一句话）",
  "target_users": "目标客户群体",
  "company_intro": "公司简介（完整段落，100-200字）",
  "core_value": "核心价值（什么让我们与众不同）",
  "selling_points": "核心卖点（3-5个，用分号分隔）",
  "success_cases": "成功案例（若有）",
  "testimonials": "客户证言建议（若无可建议内容）",
  "pain_points": ["客户痛点1", "客户痛点2"],
  "competitors": ["竞品1", "竞品2"],
  "confidence": 0.85
}}
```

只返回JSON，不要其他内容。"""

    try:
        if advisor:
            # 使用顾问RAG知识库
            print(f"\n[profile_api] 🎯 使用顾问 '{advisor.name}' 分析公司信息（检索知识库）")
            result = await advisor.ask_with_context(
                question=prompt,
                company_data={},
                persona_data={},
            )
            response_text = result.get("response", "")
            retrieved_docs = result.get("retrieved_docs", 0)
            print(f"[profile_api] ✅ 检索到 {retrieved_docs} 条相关知识")
        else:
            # fallback到普通LLM调用
            from services.llm.advisor_llm import advisor_generate
            print(f"\n[profile_api] ⚠️ 顾问加载失败，使用advisor_generate回退")
            response_text = await advisor_generate(prompt)
            retrieved_docs = 0
        
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0]
        
        extracted = json.loads(response_text.strip())
        advisor_name = advisor.name if advisor else advisor_config['name']
        return {"success": True, "extracted": extracted, "source": "LLM", "advisor": advisor_name, "retrieved_docs": retrieved_docs}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ========== 档案文件上传 API ==========
import shutil
from pathlib import Path

@router.post("/{profile_id}/upload-persona-files", summary="上传人设相关文件")
async def api_upload_persona_files(profile_id: str, request: Request, files: list[UploadFile] = File(...)):
    """
    上传人设相关文件到档案目录
    支持: PDF, Word, TXT
    文件将保存到 data/profiles/{profile_id}/persona_files/
    """
    _require_accessible_profile(request, profile_id)
    
    # 创建目录
    base_path = Path(__file__).parent.parent / "data" / "profiles" / profile_id / "persona_files"
    base_path.mkdir(parents=True, exist_ok=True)
    
    saved_files = []
    for file in files:
        # 验证文件类型
        allowed_extensions = ['.pdf', '.doc', '.docx', '.txt']
        file_ext = Path(file.filename).suffix.lower()
        if file_ext not in allowed_extensions:
            continue
        
        # 保存文件
        file_path = base_path / file.filename
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        
        saved_files.append({
            "filename": file.filename,
            "path": str(file_path),
            "size": len(content)
        })
    
    return {
        "success": True,
        "profile_id": profile_id,
        "saved_files": saved_files,
        "count": len(saved_files)
    }


# ========== 人设深度访谈 API ==========

# 访谈问题序列
INTERVIEW_QUESTIONS = [
    {"stage": "basic", "question": "你好！我是你的人设顾问。接下来我会通过一系列问题，帮你梳理完整的人设故事。先告诉我，你是做什么的？入行多久了？"},
    {"stage": "basic", "question": "你最擅长的3件事是什么？你的核心竞争力在哪里？"},
    {"stage": "basic", "question": "用3-5个词形容你的性格？你做事的原则是什么？"},
    {"stage": "origin", "question": "当初为什么选择做这行？是家里有人做，还是自己选择的？"},
    {"stage": "origin", "question": "你还记得第一个客户/第一份工作吗？当时是什么情况？"},
    {"stage": "struggle_1", "question": "入行以来，遇到过最大的困难或挫折是什么？发生在什么时候？"},
    {"stage": "struggle_1", "question": "当时有多难？能说说具体的数字或细节吗？比如负债多少、剩多少钱、持续了多久？"},
    {"stage": "turnaround_1", "question": "后来是怎么挺过来的？有人帮助你吗？或者你做了什么关键的决定？"},
    {"stage": "struggle_2", "question": "除了那次，还有其他重大的困难或转折点吗？发生了什么？"},
    {"stage": "turnaround_2", "question": "这次是怎么解决的？学到了什么教训？"},
    {"stage": "struggle_3", "question": "还有其他印象深刻的经历吗？比如被误解、被骗、遇到难缠的客户？"},
    {"stage": "turnaround_3", "question": "这件事后来怎么处理的？给你带来什么改变？"},
    {"stage": "current", "question": "现在你的业务规模怎么样？服务过多少客户？"},
    {"stage": "current", "question": "为什么还在坚持做这行？你最想帮助什么样的人？"},
    {"stage": "quotes", "question": "最后，有没有你常说的一些话、金句或者座右铭？"},
]


class InterviewRequest(BaseModel):
    """访谈请求"""
    profile_id: Optional[str] = None
    current_stage: int = 0  # 当前问题索引
    user_answer: str = ""  # 用户回答
    history: List[dict] = []  # 历史对话
    advisor_id: Optional[str] = None
    feedback: Optional[str] = None  # 用户修改意见


class InterviewDocRequest(BaseModel):
    """导入访谈稿请求"""
    text: str
    advisor_id: Optional[str] = None


@router.post("/interview-story", summary="人设深度访谈")
async def api_interview_story(data: InterviewRequest):
    """
    人设深度访谈：
    - 返回下一个引导问题
    - 保存用户回答
    - 根据回答动态调整问题
    """
    from advisors import get_advisor
    import json
    
    current_stage = data.current_stage
    user_answer = data.user_answer.strip()
    history = data.history or []
    
    # 如果有用户回答，保存到历史
    if user_answer and current_stage > 0:
        history.append({
            "role": "user",
            "content": user_answer,
            "stage": INTERVIEW_QUESTIONS[current_stage - 1]["stage"] if current_stage > 0 else "intro"
        })
    
    # 检查是否完成所有问题
    if current_stage >= len(INTERVIEW_QUESTIONS):
        return {
            "success": True,
            "completed": True,
            "message": "访谈完成！正在整理你的人设素材库...",
            "history": history,
            "next_question": None
        }
    
    # 获取下一个问题
    next_q = INTERVIEW_QUESTIONS[current_stage]
    
    # 使用AI根据上下文优化问题（可选）
    advisor_id = data.advisor_id or "huang-douyin"
    advisor = get_advisor(advisor_id)
    
    ai_question = next_q["question"]
    
    # 如果有历史对话，让AI根据上下文调整问题措辞
    if history and advisor and current_stage > 0:
        try:
            context = "\n".join([f"{'我' if h['role']=='user' else 'AI'}: {h['content']}" for h in history[-4:]])
            refine_prompt = f"""根据以下对话上下文，用更自然的方式提出下一个问题。保持问题核心意图不变，但让过渡更流畅。

上下文：
{context}

原问题：{next_q["question"]}

直接输出优化后的问题（一句话）："""
            
            result = await advisor.ask_with_context(
                question=refine_prompt,
                company_data={},
                persona_data={},
            )
            refined = result.get("response", "").strip()
            if refined and len(refined) < 200:
                ai_question = refined
        except Exception as e:
            print(f"[interview] 问题优化失败，使用原问题: {e}")
    
    # 添加AI问题到历史
    history.append({
        "role": "assistant",
        "content": ai_question,
        "stage": next_q["stage"]
    })
    
    return {
        "success": True,
        "completed": False,
        "current_stage": current_stage,
        "total_stages": len(INTERVIEW_QUESTIONS),
        "stage_name": next_q["stage"],
        "next_question": ai_question,
        "history": history,
        "progress": round((current_stage / len(INTERVIEW_QUESTIONS)) * 100)
    }


@router.post("/compile-story", summary="整合访谈素材")
async def api_compile_story(data: InterviewRequest):
    """
    将访谈记录整合为结构化的人设素材库
    """
    from advisors import get_advisor
    import json
    
    history = data.history or []
    if not history:
        return {"success": False, "error": "没有访谈记录"}
    
    # 整理对话内容
    conversation = "\n".join([
        f"{'用户' if h['role']=='user' else '顾问'}: {h['content']}" 
        for h in history
    ])
    
    advisor_id = data.advisor_id or "huang-douyin"
    advisor = get_advisor(advisor_id)
    
    # 如果有反馈意见，添加到prompt中
    feedback_instruction = ""
    if data.feedback:
        feedback_instruction = f"""

【用户修改意见】
{data.feedback}

严格按照用户的修改意见调整内容。如果用户要求增加字数，请扩写内容；如果要求修改具体细节，请按要求修改。"""
    
    compile_prompt = f"""请根据以下访谈记录，整理成结构化的人设素材库。{feedback_instruction}

【访谈记录】
{conversation}

【请按以下JSON格式输出】
```json
{{
  "basic_info": {{
    "identity": "身份标签（职业+地域+特点）",
    "core_skills": ["核心能力1", "核心能力2", "核心能力3"],
    "personality": ["性格特点1", "性格特点2", "性格特点3"],
    "principles": ["做事原则1", "做事原则2", "做事原则3"]
  }},
  "origin_story": "入行故事（300-500字，第一人称）",
  "struggles": [
    {{
      "period": "时间段",
      "type": "困境类型（财务/事业/健康/信任）",
      "detail": "具体经历（200-300字，包含数字细节）"
    }}
  ],
  "turnarounds": [
    {{
      "period": "时间段", 
      "type": "转机类型（贵人/决策/突破/机遇）",
      "detail": "具体经历（150-200字）"
    }}
  ],
  "current_status": "现状与使命（200-300字）",
  "golden_quotes": ["金句1", "金句2", "金句3", "金句4", "金句5"],
  "mini_stories": [
    {{
      "title": "故事标题",
      "content": "小故事内容（100-200字）",
      "use_for": "适用场景（开场/结尾/案例）"
    }}
  ],
  "full_background": "完整背景故事（800-1500字，按时间线叙述，包含困境和转机，第一人称）"
}}
```

只输出JSON，不要其他内容。"""

    try:
        if advisor:
            result = await advisor.ask_with_context(
                question=compile_prompt,
                company_data={},
                persona_data={},
            )
            response_text = result.get("response", "")
        else:
            from services.llm.advisor_llm import advisor_generate
            response_text = await advisor_generate(compile_prompt)
        
        # 解析JSON
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0]
        
        story_data = json.loads(response_text.strip())
        
        return {
            "success": True,
            "story_data": story_data,
            "word_count": len(story_data.get("full_background", "")),
            "struggles_count": len(story_data.get("struggles", [])),
            "quotes_count": len(story_data.get("golden_quotes", []))
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/parse-interview-doc", summary="解析导入的访谈稿")
async def api_parse_interview_doc(data: InterviewDocRequest):
    """
    解析导入的访谈稿/自我介绍文档，提取关键信息
    """
    from advisors import get_advisor
    import json
    
    text = data.text.strip()
    if not text:
        return {"success": False, "error": "文档内容为空"}
    
    advisor_id = data.advisor_id or "huang-douyin"
    advisor = get_advisor(advisor_id)
    
    parse_prompt = f"""请分析以下访谈稿/自我介绍，提取关键信息并生成结构化的人设素材库。

【原始文档】
{text[:8000]}

【请按以下JSON格式输出】
```json
{{
  "basic_info": {{
    "identity": "身份标签",
    "core_skills": ["核心能力1", "核心能力2", "核心能力3"],
    "personality": ["性格特点1", "性格特点2"],
    "principles": ["做事原则1", "做事原则2"]
  }},
  "origin_story": "入行故事（从文档中提取，补充成300-500字）",
  "struggles": [
    {{"period": "时间", "type": "类型", "detail": "详情"}}
  ],
  "turnarounds": [
    {{"period": "时间", "type": "类型", "detail": "详情"}}
  ],
  "current_status": "现状与使命",
  "golden_quotes": ["从文档中提取的金句"],
  "mini_stories": [
    {{"title": "标题", "content": "内容", "use_for": "用途"}}
  ],
  "full_background": "整合成完整的背景故事（800-1500字，第一人称）"
}}
```

只输出JSON，不要其他内容。"""

    try:
        if advisor:
            result = await advisor.ask_with_context(
                question=parse_prompt,
                company_data={},
                persona_data={},
            )
            response_text = result.get("response", "")
        else:
            from services.llm.advisor_llm import advisor_generate
            response_text = await advisor_generate(parse_prompt)
        
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0]
        
        story_data = json.loads(response_text.strip())
        
        return {
            "success": True,
            "story_data": story_data,
            "source": "document_import",
            "word_count": len(story_data.get("full_background", ""))
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ========== 业务知识库 API ==========

class KnowledgeUploadRequest(BaseModel):
    """知识库文档上传请求"""
    filename: str
    content: str  # 文档内容（已提取的文本）


class KnowledgeSearchRequest(BaseModel):
    """知识库检索请求"""
    query: str
    top_k: int = 3


def _profile_value_has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


@router.post("/{profile_id}/knowledge/upload", summary="上传知识库文档")
async def api_upload_knowledge(profile_id: str, data: KnowledgeUploadRequest, request: Request):
    """
    上传知识库文档并向量化存储
    
    - 文档内容会被分块并向量化
    - 支持后续语义检索
    """
    from tools.knowledge_rag import add_knowledge_to_profile
    from db.profile_db import get_profile, add_knowledge_file
    import uuid
    from datetime import datetime
    
    _require_accessible_profile(request, profile_id)
    
    if not data.content.strip():
        return {"success": False, "error": "文档内容为空"}
    
    # 生成文件ID
    file_id = str(uuid.uuid4())[:8]
    
    try:
        # 向量化存储
        result = await add_knowledge_to_profile(
            profile_id=profile_id,
            file_id=file_id,
            filename=data.filename,
            content=data.content
        )
        
        if result.get("success"):
            # 记录到数据库
            file_info = {
                "id": file_id,
                "name": data.filename,
                "size": len(data.content),
                "chunk_count": result.get("chunk_count", 0),
                "created_at": datetime.now().isoformat()
            }
            add_knowledge_file(profile_id, file_info)
            
            return {
                "success": True,
                "file_id": file_id,
                "filename": data.filename,
                "chunk_count": result.get("chunk_count", 0),
                "message": f"成功向量化 {result.get('chunk_count', 0)} 个文档块"
            }
        else:
            return {"success": False, "error": result.get("error", "向量化失败")}
            
    except Exception as e:
        print(f"[profile_api] ❌ 知识库上传失败: {e}")
        return {"success": False, "error": str(e)}


@router.post("/{profile_id}/knowledge/search", summary="检索知识库")
async def api_search_knowledge(profile_id: str, data: KnowledgeSearchRequest, request: Request):
    """
    语义检索知识库
    
    根据查询内容返回相关的知识片段
    """
    from db.profile_db import get_profile
    from tools.knowledge_rag import search_profile_knowledge
    
    _require_accessible_profile(request, profile_id)
    
    if not data.query.strip():
        return {"success": False, "error": "查询内容为空"}
    
    try:
        # 语义检索
        results = await search_profile_knowledge(
            profile_id=profile_id,
            query=data.query,
            top_k=data.top_k
        )
        
        return {
            "success": True,
            "query": data.query,
            "results": results,
            "count": len(results)
        }
    except Exception as e:
        print(f"[profile_api] ❌ 知识库检索失败: {e}")
        return {"success": False, "error": str(e)}


# ========== 人设双向同步辅助函数 ==========

def _sync_profile_to_persona(profile_id: str, update_data: dict):
    """当 client_profiles 的人设字段被修改时，反向同步到 social_personas 表。

    字段映射（client_profiles → social_personas）:
        persona_positioning → one_liner
        visual_style        → visual_style
        persona_tone        → speaking_style
        target_users        → target_audience
        content_pillars     → content_pillars
        persona_background  → persona_details
    """
    import json as _json
    from db.social_project_db import get_connection, save_persona

    # 找到关联此 profile_id 的所有 social_projects
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM social_projects WHERE profile_id = %s AND status = 'active'",
            (profile_id,)
        )
        project_rows = cursor.fetchall()
        conn.close()

        if not project_rows:
            return

        # 构建 save_persona 参数（只传被修改的字段）
        persona_kwargs = {}
        if "persona_positioning" in update_data:
            persona_kwargs["one_liner"] = update_data["persona_positioning"]
        if "visual_style" in update_data:
            persona_kwargs["visual_style"] = update_data["visual_style"]
        if "persona_tone" in update_data:
            persona_kwargs["speaking_style"] = update_data["persona_tone"]
        if "target_users" in update_data:
            persona_kwargs["target_audience"] = update_data["target_users"]
        if "content_pillars" in update_data:
            cp = update_data["content_pillars"]
            if isinstance(cp, list):
                cp = _json.dumps(cp, ensure_ascii=False)
            persona_kwargs["content_pillars"] = cp
        if "persona_background" in update_data:
            persona_kwargs["persona_details"] = update_data["persona_background"]

        if not persona_kwargs:
            return

        # 对每个关联项目同步（通常只有一个）
        for row in project_rows:
            project_id = row["id"]
            # 直接写 social_personas 表，不要调用 save_persona() 避免循环同步
            _direct_update_social_persona(project_id, persona_kwargs)
    finally:
        try:
            conn.close()
        except Exception: pass


def _direct_update_social_persona(project_id: int, kwargs: dict):
    """直接更新 social_personas 表，不触发反向同步（避免循环）"""
    from db.social_project_db import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT id FROM social_personas WHERE project_id = %s", (project_id,))
        existing = cursor.fetchone()

        if existing:
            set_parts = [f"{k} = COALESCE(%s, {k})" for k in kwargs]
            set_parts.append("updated_at = CURRENT_TIMESTAMP")
            cursor.execute(
                f"UPDATE social_personas SET {', '.join(set_parts)} WHERE project_id = %s",
                list(kwargs.values()) + [project_id]
            )
        else:
            kwargs["project_id"] = project_id
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join(["%s"] * len(kwargs))
            cursor.execute(
                f"INSERT INTO social_personas ({cols}) VALUES ({placeholders})",
                list(kwargs.values())
            )

        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def _sync_profile_business_to_project(profile_id: str, update_data: dict):
    """当 client_profiles 的业务字段被修改时，反向同步到 social_projects 表。

    字段映射（client_profiles → social_projects）:
        industry       → industry
        business       → business
        target_users   → target_audience
        products       → product_intro
    """
    import json as _json
    from db.social_project_db import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM social_projects WHERE profile_id = %s AND status = 'active'",
            (profile_id,)
        )
        project_rows = cursor.fetchall()
        conn.close()

        if not project_rows:
            return

        # 构建 update 参数
        proj_kwargs = {}
        if "industry" in update_data:
            proj_kwargs["industry"] = update_data["industry"]
        if "business" in update_data:
            proj_kwargs["business"] = update_data["business"]
        if "target_users" in update_data:
            proj_kwargs["target_audience"] = update_data["target_users"]
        if "products" in update_data:
            val = update_data["products"]
            # social_projects.product_intro 是 TEXT
            if isinstance(val, list):
                val = _json.dumps(val, ensure_ascii=False)
            proj_kwargs["product_intro"] = val

        if not proj_kwargs:
            return

        from db.social_project_db import get_connection as _gc
        for row in project_rows:
            pid = row["id"]
            conn2 = _gc()
            c2 = conn2.cursor()
            set_parts = [f"{k} = %s" for k in proj_kwargs]
            set_parts.append("updated_at = CURRENT_TIMESTAMP")
            c2.execute(
                f"UPDATE social_projects SET {', '.join(set_parts)} WHERE id = %s",
                list(proj_kwargs.values()) + [pid]
            )
            conn2.commit()
            conn2.close()
    finally:
        try:
            conn.close()
        except Exception: pass



# ============================================================
# C.1 (CTO-15.18 · 2026-04-28 · PM 干预 类 C):批量 AI 补齐档案
# 老板红线:旧版有批量 AI 补齐 · M3 砍了 → 中坚代理回旧版根本动机之一(根因 #4)
# 修法:复用单 brand /api/profiles/ai-fill 端点 · 加批量 endpoint 串行执行(并发 3 + 失败跳过)
# Q14 老板裁决:不打折 · 单价 130/客户 · 批量确认 dialog 显示总价 + 用户勾选确认才执行
# ============================================================

class BatchAiFillRequest(BaseModel):
    brand_ids: list[int] = Field(..., min_length=1, max_length=50)
    advisor_id: str | None = None
    """要 AI 补齐的 brand_id 列表 · 上限 50 防滥用"""


@router.post("/batch-ai-fill", summary="批量 AI 补齐档案 · 130 积分/客户 · 串行 + 失败跳过")
async def api_batch_ai_fill_profile(req: BatchAiFillRequest, request: Request):
    """批量 AI 补齐多个 brand 档案 · 复用单 brand ai-fill 逻辑

    - 每个 brand 扣 130 积分(autofill_brand)
    - 失败的 brand 自动跳过 · 不连累其他
    - 并发 3(asyncio.Semaphore)
    - 返回 {success_count, fail_count, total, results: [{brand_id, success, error?}]}

    红线:
    - RBAC 逐个 brand 校验 · 越权直接 skip
    - 跟单 brand /ai-fill 同等扣费(Q14:不打折)
    - 失败的扣费走单 brand /ai-fill 的退费逻辑(已实现)· 这里不重复
    """
    import asyncio
    from auth.brand_access import require_brand_access

    # 校验 brand_ids 列表
    brand_ids = list(set(req.brand_ids))  # 去重
    if not brand_ids:
        raise HTTPException(status_code=400, detail="brand_ids 列表为空")
    if len(brand_ids) > 50:
        raise HTTPException(status_code=400, detail="单次最多批量 50 个 brand")

    # 并发 3 信号量
    semaphore = asyncio.Semaphore(3)
    results: list[dict] = []

    async def _process_one(bid: int) -> dict:
        async with semaphore:
            try:
                # RBAC 逐个校验
                require_brand_access(request, bid)
                # 复用单 brand ai-fill(扣 130 + 反哺写入)
                # 直接调内部函数 · 不走 HTTP self-call
                fake_data = AiFillRequest(brand_id=bid, advisor_id=req.advisor_id)
                result = await api_ai_fill_profile(fake_data, request)
                # api_ai_fill_profile 返 {success, ...}
                return {
                    "brand_id": bid,
                    "success": bool((result or {}).get("success")),
                    "data": result,
                }
            except HTTPException as he:
                return {"brand_id": bid, "success": False, "error": str(he.detail), "status": he.status_code}
            except Exception as e:
                logger.warning(f"[batch-ai-fill] brand_id={bid} 失败: {e}")
                return {"brand_id": bid, "success": False, "error": str(e)}

    # 并发执行
    tasks = [_process_one(bid) for bid in brand_ids]
    raw = await asyncio.gather(*tasks, return_exceptions=False)
    results = [r if isinstance(r, dict) else {"brand_id": -1, "success": False, "error": str(r)} for r in raw]

    success_count = sum(1 for r in results if r.get("success"))
    fail_count = len(results) - success_count

    return {
        "success": True,
        "total": len(brand_ids),
        "success_count": success_count,
        "fail_count": fail_count,
        "results": results,
    }
