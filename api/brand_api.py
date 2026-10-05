"""
品牌/客户管理 API v4
- /api/my-brand — 用户自己的品牌（self）
- /api/my-clients — 用户服务的客户列表（client）
- /api/brand/auto-fill — AI 智能填充
"""

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
import json
import logging

# 🔴 关系类私有字段清单 = 全系统唯一一份(工单 §0.1 R5:「不要另起一套」)。
from services.relationship_privacy import RELATIONSHIP_PRIVATE_FIELDS

logger = logging.getLogger("GEO-Brand-API")

router = APIRouter(tags=["品牌/客户管理"])


# ========== 工具 ==========

def _get_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _validated_industry_category_or_422(value: str) -> str:
    """[WO_267] 行业大类只收字典里的**新 key**;空串 = 清空选择(回到按行业文本自动判)。

    存量旧中文名(房产家居 / 科技服务…)不许经这里写入 —— 新写入一律大类 key(工单 ④),
    旧值只在读侧翻译。前端 12 值下拉此前**从未**提交过这个字段(`BrandInfoUpdate` 没有它,
    extra=ignore 会静默丢弃),所以加校验不会打断任何现有表单。
    """
    from services.industry_taxonomy import category_keys

    s = str(value or "").strip()
    if not s:
        return ""
    if s not in category_keys():
        raise HTTPException(status_code=422, detail={
            "code": "INVALID_INDUSTRY_CATEGORY",
            "message": "行业大类不在大类列表里,请从列表中选择",
        })
    return s


# ========== 请求模型 ==========

class BrandInfoUpdate(BaseModel):
    model_config = {"extra": "ignore"}  # 忽略前端多传的字段，不报 422
    name: Optional[str] = None
    industry: Optional[str] = None
    # [WO_267] 用户在大类下拉 / 对话确认卡里**选定**的行业大类 key(此后行业判定以它为准);空串 = 清空
    industry_category: Optional[str] = None
    company_name: Optional[str] = None
    brand_display_names: Optional[List[str]] = None
    cities: Optional[str] = None
    # client_profiles 同步字段
    business: Optional[str] = None
    target_users: Optional[str] = None
    products: Optional[List[str]] = None
    pain_points: Optional[List[str]] = None
    competitors: Optional[List[str]] = None  # S1.1 ai-fill 产出
    persona_positioning: Optional[str] = None
    persona_tone: Optional[str] = None
    content_direction: Optional[str] = None
    # [CTO-13.0 2026-04-19 S1.1] AI 顾问深度填充新字段 — 对齐 ai-fill json_template 返回
    # 允许前端 PUT /api/my-brand 或 /api/my-clients/:id 保存这些营销资料字段
    company_intro: Optional[str] = None          # 公司简介 200 字内
    core_value: Optional[str] = None             # 核心价值主张
    # [2026-06-02 GEO CTO] 联系方式 4 字段(写作结尾引流 · 落 client_profiles · 自发布完整/媒体软化)
    contact_phone: Optional[str] = None          # 电话
    contact_wechat: Optional[str] = None         # 微信/企业微信
    contact_website: Optional[str] = None        # 官网
    contact_address: Optional[str] = None        # 地址
    selling_points: Optional[str] = None         # 核心卖点（分点 \n 分隔）
    success_cases: Optional[str] = None          # 成功案例摘要
    testimonials: Optional[str] = None           # 客户证言
    structured_knowledge: Optional[dict] = None  # 5 维度嵌套（products/painPoints/customers/differentiation/cases）
    # [CTO-15.9 2026-04-25 M1c T4] 市场洞察 5 字段对齐 brand_completeness E 组
    service_scope: Optional[str] = None           # local / national / hybrid
    local_competitors: Optional[List[str]] = None # 本地竞品 list(区别于 structured_knowledge 里 competitors)
    market_insight: Optional[dict] = None         # {authority_sources, hot_formats, my_differentiation}
    # [CTO-15.9 A.5 + A.8] 业务类型 + 地域范围 · profile SSOT + brands denormalized 缓存
    business_type: Optional[str] = None           # B2C / B2B / 政企(profile SSOT · brands 冗余加速)
    city_scope: Optional[str] = None              # local / national


def _persist_confirmed_display_names(brand_id: int, values: Optional[List[str]]) -> None:
    """Persist the brand SSOT and compatibility mirror in one transaction."""
    if values is None:
        return
    from services.brand_identity_resolver import persist_confirmed_display_names

    try:
        persist_confirmed_display_names(brand_id, values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="品牌不存在") from exc


def _merge_market_insight_into_brief(brand_id: int, market_insight: dict) -> Optional[str]:
    """M1c T4 · market_insight 3 键合并进 industry_brief JSONB · 返回 JSON 字符串或 None

    非覆盖 · 只写 authority_sources / hot_formats / my_differentiation 三键
    保留 brief 其他字段(policy_redlines / industry_terms / 道法层数据)
    """
    import json as _json
    try:
        from db.connection import get_connection as _gc
        _conn = _gc()
        _cur = _conn.cursor()
        _cur.execute(
            "SELECT industry_brief FROM client_profiles WHERE brand_id = %s ORDER BY updated_at DESC LIMIT 1",
            (brand_id,),
        )
        _row = _cur.fetchone()
        _conn.close()
        existing = {}
        if _row and _row.get("industry_brief"):
            raw = _row["industry_brief"]
            if isinstance(raw, dict):
                existing = raw
            elif isinstance(raw, str) and raw.strip():
                try:
                    existing = _json.loads(raw) or {}
                except Exception:
                    existing = {}
        for key in ("authority_sources", "hot_formats", "my_differentiation"):
            if key in market_insight and market_insight[key] not in (None, "", []):
                existing[key] = market_insight[key]
        return _json.dumps(existing, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"[market_insight merge] brand_id={brand_id} 失败: {e}")
        return None


def _clean_brand_name_or_400(value, *, field: str = "品牌名") -> str:
    """品牌名入库校验（P0-1）。手工输入与 AI 自动填充共用同一校验。

    生产实证 brands.id=278 = "深圳驰鲸科技\n\n城市:深圳" → 品牌识别恒不命中
    → 诊断 456/468 都是 0 分、客户付费两次拿废报告。品牌名是识别匹配键，
    脏进去整份报告作废，因此按 H0「对象身份与数据完整性」拒绝；
    但必须带出口（§13）：detail 里给建议名 + 修复提示，前端可一键采用。
    """
    from utils.brand_name_hygiene import BrandNameHygieneError, validate_brand_name

    try:
        return validate_brand_name(value)
    except BrandNameHygieneError as exc:
        detail = exc.to_detail()
        detail["field"] = field
        raise HTTPException(status_code=400, detail=detail)


class AddClientRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    industry: Optional[str] = ""
    city: Optional[str] = ""
    business: Optional[str] = ""
    from_diagnosis_id: Optional[int] = None  # 从诊断导入
    # Phase F · CTO-15.10 · 2026-04-27 · 关键词从启动诊断页搬到快录新客户页
    # AI 帮填或代理手填的 8-15 个种子关键词 · 启动诊断时直接预填(免重复输入)
    seed_keywords: Optional[List[str]] = None


class AutoFillRequest(BaseModel):
    text: str = Field(..., min_length=2, max_length=50000)
    brand_id: Optional[int] = None
    # M3 快录客户页会同时传结构化上下文。
    # 短文本路径不能把 "客户名\n行业:...\n城市:..." 整段当 brand_name 搜索。
    brand_name: Optional[str] = ""
    industry: Optional[str] = ""
    city: Optional[str] = ""
    source: Optional[str] = ""


# ========== client_profiles 落库(upsert)==========


def _upsert_client_profile(cur, brand_id: int, profile_updates: dict,
                           *, fallback_name: Optional[str] = None) -> List[str]:
    """把 profile 字段真正写进库,**没有行就建行**,并返回落库的字段名。

    🔴 [WO_252 ① 2026-09-20] 这个 helper 是为了消灭一种"成功"的形态:
       原来 `PUT /api/my-clients/{id}` 先 `SELECT id FROM client_profiles`,
       **取不到行就跳过 UPDATE,照样 `return {"success": True}`** ——
       用户填了电话/微信/网址/地址,点保存,页面说成功,库里什么都没有。
       生产实测:真品牌 233/377 在 `client_profiles` 没有行,
       brand 19 当天六次 PUT 全 200 而一个字段都没落。
       静默丢弃比报错坏得多:报错用户会重试或找我们,静默丢弃没人知道。

    🔴 `name` 是 NOT NULL 且无默认值 ⇒ 建行时必须有名字。
       只改联系方式(不带 name)的请求最常见,所以这里要 `fallback_name` 兜底;
       没有它,"修好了静默丢弃"会变成"建行时 NotNullViolation 500" ——
       换一种失败,不是修好。

    Returns: 真正写进库的列名(排序);一列都没有则 ``[]``。
    """
    if not profile_updates:
        return []
    cur.execute(
        "SELECT id FROM client_profiles WHERE brand_id = %s "
        "AND (is_deleted = 0 OR is_deleted IS NULL) ORDER BY updated_at DESC LIMIT 1",
        (brand_id,),
    )
    row = cur.fetchone()
    cols = sorted(profile_updates.keys())
    if row:
        sets = [f"{k} = %s" for k in cols]
        cur.execute(
            f"UPDATE client_profiles SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP "
            f"WHERE id = %s",
            [profile_updates[k] for k in cols] + [row["id"]],
        )
    else:
        import shortuuid
        payload = dict(profile_updates)
        payload["id"] = shortuuid.uuid()[:8]
        payload["brand_id"] = brand_id
        if not payload.get("name"):
            payload["name"] = fallback_name or f"客户 #{brand_id}"
        keys = sorted(payload.keys())
        cur.execute(
            "INSERT INTO client_profiles (%s) VALUES (%s)"
            % (", ".join(keys), ", ".join(["%s"] * len(keys))),
            [payload[k] for k in keys],
        )
    if cur.rowcount != 1:
        # 🔴 不许"写了 0 行还回成功" —— 这正是本单要消灭的那种成功。
        raise HTTPException(
            500,
            detail={
                "code": "PROFILE_PERSIST_FAILED",
                "message": "客户资料没有保存成功，请重试；若反复出现请联系我们",
                "brand_id": brand_id,
            },
        )
    return cols


# ========== 我的品牌 ==========

@router.get("/api/my-brand")
async def get_my_brand(request: Request):
    """获取当前用户的 self 品牌（含 profile）"""
    user = _get_user(request)
    user_id = user["user_id"]

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()

        # 找 self 品牌
        cur.execute("""
            SELECT b.* FROM brands b
            WHERE b.owner_user_id = %s AND b.brand_type = 'self'
              AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
            LIMIT 1
        """, (user_id,))
        brand = cur.fetchone()

        if not brand:
            # 兜底：找 user_clients 里的第一个品牌
            cur.execute("""
                SELECT b.* FROM brands b
                JOIN user_clients uc ON uc.brand_id = b.id
                WHERE uc.user_id = %s AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
                ORDER BY b.created_at ASC LIMIT 1
            """, (user_id,))
            brand = cur.fetchone()

        if not brand:
            return {"success": True, "brand": None, "profile": None, "need_setup": True}

        brand = dict(brand)

        # 加载 profile
        cur.execute("""
            SELECT * FROM client_profiles
            WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
            ORDER BY updated_at DESC LIMIT 1
        """, (brand["id"],))
        profile = cur.fetchone()

        # [CTO-15.3 2026-04-20] 多维度营销资料完善度(0-100, 参考 BrandDetailPage 完整字段)
        try:
            from utils.brand_completeness import compute_brand_completeness
            comp = compute_brand_completeness(brand, dict(profile) if profile else None)
            brand["completeness"] = comp["score"]
            brand["completeness_groups"] = comp["groups"]
            brand["completeness_missing"] = comp["missing"]
        except Exception as e:
            logger.warning(f"[my-brand] compute_brand_completeness 失败: {e}")
            brand["completeness"] = 0

        # 非 admin 用户过滤敏感字段（admin_insight 是内部战略分析，不应对普通用户可见）
        is_admin = user.get("is_admin", False)
        if not is_admin:
            profile_dict = dict(profile) if profile else None
            if profile_dict:
                for f in ("admin_insight", "admin_insight_level", "admin_insight_updated_at"):
                    profile_dict.pop(f, None)
        else:
            profile_dict = dict(profile) if profile else None

        return {
            "success": True,
            "brand": brand,
            "profile": profile_dict,
            "need_setup": False,
        }
    finally:
        conn.close()


@router.put("/api/my-brand")
async def update_my_brand(request: Request):
    """更新我的品牌信息（brands + client_profiles 双写）
    v1_3 (CTO-15.1 2026-04-19 老板反馈"保存失败" 根因不明):
    外层 try/except 兜底 — 任何未捕获异常都转成 HTTPException 500 + detail 真实错误信息
    + logger.exception 记完整 traceback 方便看 docker logs 定位
    """
    try:
        return await _update_my_brand_impl(request)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[my-brand PUT] 未捕获异常: {e}")
        raise HTTPException(status_code=500, detail=f"保存失败: {type(e).__name__}: {str(e)[:200]}")


async def _update_my_brand_impl(request: Request):
    # 手动解析 body，避免 Pydantic 422
    import json as _json
    raw_body = await request.body()
    try:
        body = _json.loads(raw_body)
    except Exception:
        body = {}
    logger.info(f"[my-brand PUT] raw keys: {list(body.keys())}, types: {{k: type(v).__name__ for k, v in body.items()}}")
    req = BrandInfoUpdate(**{k: v for k, v in body.items() if k in BrandInfoUpdate.model_fields})

    user = _get_user(request)
    user_id = user["user_id"]

    from db.connection import get_connection, get_db
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 优先找 self 品牌（欢迎页创建），没有则找 legacy（注册自动创建）
        cur.execute("SELECT id, name FROM brands WHERE owner_user_id = %s AND brand_type = 'self' LIMIT 1", (user_id,))
        row = cur.fetchone()
        if not row:
            # 没有 self 品牌，找用户关联的任意品牌
            cur.execute("""
                SELECT b.id, b.name FROM brands b
                JOIN user_clients uc ON uc.brand_id = b.id
                WHERE uc.user_id = %s LIMIT 1
            """, (user_id,))
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="未找到你的品牌，请先完成注册引导")
        brand_id = row["id"]
        brand_name_now = row.get("name")
    finally:
        conn.close()

    _persist_confirmed_display_names(brand_id, req.brand_display_names)

    # 更新 brands 表
    brand_updates = {}
    # [P0-1] 改名同样过品牌名校验（自助端 / 代理端同一把闸；AI 填充也走这里）
    if req.name is not None: brand_updates["name"] = _clean_brand_name_or_400(req.name)
    if req.industry is not None: brand_updates["industry"] = req.industry
    if req.industry_category is not None:
        brand_updates["industry_category"] = _validated_industry_category_or_422(req.industry_category)
    if req.company_name is not None: brand_updates["company_name"] = req.company_name
    if req.cities is not None: brand_updates["cities"] = req.cities
    # [CTO-15.9 A.8] denormalized 缓存(SSOT 仍在 profile · brands 同步加速 hot-path)
    if req.business_type is not None: brand_updates["business_type"] = req.business_type
    if req.city_scope is not None: brand_updates["city_scope"] = req.city_scope

    # [CTO-13.0] 标记 brand.name 是否真的更新成功 — 用于 profile 层决定是否同步 name
    # 防 name 漂移：如果 brand.name UNIQUE 冲突被跳过，profile.name 也必须跳过，否则
    # 两者分叉（老 bug：profile.name="全域上榜" 但 brand.name="朵朵出海"）
    brand_name_updated = False
    if brand_updates:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                sets = [f"{k} = %s" for k in brand_updates]
                cur.execute(f"UPDATE brands SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                           list(brand_updates.values()) + [brand_id])
            brand_name_updated = "name" in brand_updates
        except Exception as e:
            if "duplicate key" in str(e).lower():
                # 品牌名冲突，去掉 name 重试（brand.name 未更新）
                brand_updates.pop("name", None)
                if brand_updates:
                    with get_db() as conn:
                        cur = conn.cursor()
                        sets = [f"{k} = %s" for k in brand_updates]
                        cur.execute(f"UPDATE brands SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                                   list(brand_updates.values()) + [brand_id])
            else:
                raise

    # 同步更新 client_profiles
    import json
    profile_updates = {}
    # [CTO-13.0] 只在 brand.name 同步更新成功时才写 profile.name，避免漂移
    if req.name is not None and brand_name_updated:
        profile_updates["name"] = req.name
    if req.industry is not None: profile_updates["industry"] = req.industry
    if req.business is not None: profile_updates["business"] = req.business
    if req.target_users is not None: profile_updates["target_users"] = req.target_users
    if req.products is not None: profile_updates["products"] = json.dumps(req.products, ensure_ascii=False)
    if req.pain_points is not None: profile_updates["pain_points"] = json.dumps(req.pain_points, ensure_ascii=False)
    if req.competitors is not None: profile_updates["competitors"] = json.dumps(req.competitors, ensure_ascii=False)
    if req.persona_positioning is not None: profile_updates["persona_positioning"] = req.persona_positioning
    if req.persona_tone is not None: profile_updates["persona_tone"] = req.persona_tone
    if req.content_direction is not None: profile_updates["content_direction"] = req.content_direction
    # [CTO-13.0 2026-04-19 S1.1] ai-fill 深度字段落地到 client_profiles
    if req.company_intro is not None: profile_updates["company_intro"] = req.company_intro
    if req.core_value is not None: profile_updates["core_value"] = req.core_value
    if req.selling_points is not None: profile_updates["selling_points"] = req.selling_points
    if req.success_cases is not None: profile_updates["success_cases"] = req.success_cases
    if req.testimonials is not None: profile_updates["testimonials"] = req.testimonials
    # [2026-06-02 GEO CTO] 联系方式 4 字段落地 client_profiles(写作引流 · 自发布完整/媒体软化)
    if req.contact_phone is not None: profile_updates["contact_phone"] = req.contact_phone
    if req.contact_wechat is not None: profile_updates["contact_wechat"] = req.contact_wechat
    if req.contact_website is not None: profile_updates["contact_website"] = req.contact_website
    if req.contact_address is not None: profile_updates["contact_address"] = req.contact_address
    if req.structured_knowledge is not None:
        profile_updates["structured_knowledge"] = json.dumps(req.structured_knowledge, ensure_ascii=False)
    # [CTO-15.9 M1c T4] 市场洞察 5 字段落库(self 端)
    if req.service_scope is not None:
        profile_updates["service_scope"] = req.service_scope
    if req.local_competitors is not None:
        profile_updates["local_competitors"] = json.dumps(req.local_competitors, ensure_ascii=False)
    if req.market_insight is not None:
        merged = _merge_market_insight_into_brief(brand_id, req.market_insight)
        if merged is not None:
            profile_updates["industry_brief"] = merged
    # [CTO-15.9 A.8] business_type / city_scope SSOT(profile)落库
    if req.business_type is not None:
        profile_updates["business_type"] = req.business_type
    if req.city_scope is not None:
        profile_updates["city_scope"] = req.city_scope

    # [WO_252 ①] 自助端本来就会建行 —— 但它建行时**不给 name 兜底**,
    # 而 `client_profiles.name` 是 NOT NULL:只改联系方式(不带 name)且还没有
    # profile 行的用户,会吃一个 NotNullViolation 500。两端收进同一个 helper。
    persisted: List[str] = []
    if profile_updates:
        with get_db() as conn:
            cur = conn.cursor()
            persisted = _upsert_client_profile(
                cur, brand_id, profile_updates, fallback_name=brand_name_now)

    return {"success": True, "brand_id": brand_id,
            "persisted": sorted(set(list(brand_updates.keys()) + persisted))}


@router.get("/api/my-brand/check-onboarding")
async def check_onboarding(request: Request):
    """检查用户是否已完成欢迎页引导

    判定条件：用户有 brand_type='self' 的品牌，且该品牌下有非空 profile（industry 非空）。
    注册时创建的品牌有 brand_type='self'，但没有 profile（profile 在欢迎页创建）。
    所以只有走过欢迎页的用户才会通过检查。
    """
    user = _get_user(request)
    user_id = user["user_id"]
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 检查：有 self 品牌 + 该品牌下有非空 profile（industry 非空说明走过欢迎页）
        cur.execute("""
            SELECT 1 FROM brands b
            JOIN client_profiles cp ON cp.brand_id = b.id
            WHERE b.owner_user_id = %s
              AND b.brand_type = 'self'
              AND (cp.is_deleted = 0 OR cp.is_deleted IS NULL)
              AND cp.industry IS NOT NULL AND cp.industry != ''
            LIMIT 1
        """, (user_id,))
        completed = cur.fetchone() is not None
        cur.close()
        return {"completed": completed}
    finally:
        conn.close()


# ========== M3 · brand completeness by id (CTO-15.5 Phase 4 PLAN 01) ==========
# 起源:
#   CTO-15.3 commit 43 把完善度算法沉到 utils/brand_completeness.py (SSOT).
#   现 C 端 GEO 方案软拦截 Dialog 需要按任意 brand_id 查完善度(多 brand 场景),
#   /api/my-brand 只能查 self 品牌,不够用. 本端点补齐能力,保持 SSOT 不让前端
#   重写 Python 算法(违反"完善度 SSOT"元指令 · CTO-15.3 session 3 通告).
# RBAC: 强制 owner_user_id == current_user_id (brand_id 全链路元指令 · CTO-15.4 47.3)

@router.get("/api/brands/{brand_id}/completeness")
async def get_brand_completeness(brand_id: int, request: Request):
    """按 brand_id 查品牌完善度 (供 C 端软拦截 Dialog / 前端完善度进度条用).

    Returns:
      {score: int 0-100, groups: {identity, business, marketing, deep_analysis},
       missing: [field1, ...]}

    Raises:
      403 brand_access_denied · 404 brand_not_found · 401 未登录
    """
    user = _get_user(request)
    user_id = user["user_id"]

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 读 brand (含 RBAC 字段) · SELECT * 兼容不同 DB schema (测试 DB 无 description 列)
        cur.execute(
            "SELECT * FROM brands WHERE id = %s LIMIT 1",
            (brand_id,),
        )
        brand_row = cur.fetchone()
        if not brand_row:
            raise HTTPException(status_code=404, detail={
                "code": "brand_not_found",
                "message": f"品牌 {brand_id} 不存在",
            })
        brand = dict(brand_row) if hasattr(brand_row, "keys") else brand_row
        # 软删也算 404 (用户视角已删)
        if brand.get("is_deleted"):
            raise HTTPException(status_code=404, detail={
                "code": "brand_not_found",
                "message": f"品牌 {brand_id} 已删除",
            })
        # RBAC: keep parity with the rest of the brand-scoped APIs.
        from auth.brand_access import require_brand_access
        require_brand_access(request, brand_id)

        # 读 profile (最新一条)
        cur.execute(
            "SELECT * FROM client_profiles WHERE brand_id = %s "
            "AND (is_deleted = 0 OR is_deleted IS NULL) "
            "ORDER BY updated_at DESC LIMIT 1",
            (brand_id,),
        )
        profile_row = cur.fetchone()
        profile = dict(profile_row) if profile_row else None
    finally:
        conn.close()

    # 复用 SSOT 算法
    try:
        from utils.brand_completeness import compute_brand_completeness
        result = compute_brand_completeness(brand, profile)
    except Exception as e:
        logger.exception(f"[brand/completeness] compute_brand_completeness 异常: {e}")
        raise HTTPException(status_code=500, detail={
            "code": "completeness_compute_error",
            "message": "完善度计算失败",
        })

    return {
        "brand_id": brand_id,
        "score": int(result.get("score", 0)),
        "groups": result.get("groups", {}),
        "missing": result.get("missing", []),
    }


@router.post("/api/my-brand/init")
async def init_my_brand(request: Request):
    """欢迎页完成时：更新注册时创建的 self 品牌 + 创建有内容的 profile

    流程：
    1. 找到注册时创建的 brand_type='self' 品牌 → 更新名字和行业
    2. 在该品牌下创建 profile（带行业/业务信息，不是空壳）
    3. 如果已有非空 profile，说明用户重复提交，直接返回
    """
    user = _get_user(request)
    user_id = user["user_id"]

    body = await request.json()
    name = body.get("name", "").strip()
    industry = body.get("industry", "").strip()
    city = body.get("city", "")
    business = body.get("business", "")

    if not name and not industry:
        raise HTTPException(400, "请描述你的业务或选择行业")
    # Welcome allows either free-text business input or an industry tag.  The
    # onboarding checker uses profile.industry as the completion marker, so
    # free-text-only users must still get a non-empty industry-like value.
    if not industry:
        industry = name

    from db.connection import get_db
    import json

    with get_db() as conn:
        cur = conn.cursor()

        # 1. 找注册时创建的 self 品牌（一定存在，注册时创建的）
        cur.execute("SELECT id FROM brands WHERE owner_user_id = %s AND brand_type = 'self' LIMIT 1", (user_id,))
        existing = cur.fetchone()

        if existing:
            brand_id = existing["id"]

            # 检查是否已有非空 profile（重复提交保护）
            cur.execute("""
                SELECT id FROM client_profiles
                WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
                  AND industry IS NOT NULL AND industry != ''
                LIMIT 1
            """, (brand_id,))
            existing_profile = cur.fetchone()
            if existing_profile:
                return {"success": True, "brand_id": brand_id, "profile_id": existing_profile["id"], "already_exists": True}

            # 更新品牌名和行业。先预查冲突;不使用 SAVEPOINT,因为池化连接可能被
            # 前序只读/DDL 调成 autocommit,SAVEPOINT 在该状态下会直接 500。
            #
            # [WO 快修 2026-08-08] 🔴 原注释断言这一列是全局唯一的 —— 那是错的,
            #   预查也照着这个错口径写成了全局 + 不看 is_deleted。生产实查:
            #     brands_name_owner_key UNIQUE (name, owner_user_id) WHERE is_deleted = false
            #   ——库约束早就是**按 owner** 的、而且**只管活行**。
            #   应用层比库严格 = 新用户填了一个**别人家已经用过的品牌名**就被静默挡掉,
            #   名字更新被跳过、onboarding 表面成功但品牌名不是他填的那个。
            #   重名跨 owner 是合法的(生产实证:「全域上榜(深圳)科技有限公司」7 行 7 个
            #   owner、QZQZ 三行三个 owner —— 不同代理服务同一家企业本来就会同名)。
            #   → 预查逐字对齐库约束:同 owner + 活行。
            if name:
                cur.execute(
                    "SELECT id FROM brands "
                    "WHERE name = %s AND owner_user_id = %s AND is_deleted = false "
                    "  AND id <> %s LIMIT 1",
                    (name, user_id, brand_id),
                )
                name_conflict = cur.fetchone()
                if not name_conflict:
                    cur.execute(
                        "UPDATE brands SET name = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                        (name, brand_id),
                    )
                # 品牌名冲突时跳过名字更新（不影响 onboarding）

            update_fields = []
            update_values = []
            if industry:
                update_fields.append("industry = %s")
                update_values.append(industry)
            if city:
                update_fields.append("cities = %s")
                update_values.append(city)
            if update_fields:
                update_values.append(brand_id)
                cur.execute(
                    f"UPDATE brands SET {', '.join(update_fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    update_values,
                )
        else:
            # 兜底：如果注册时品牌创建失败，这里补建
            # A.2 CTO-15.18 · 名字含"测试|test|_demo|_test|验收"自动 is_test=true
            from utils.is_test_brand import detect_is_test_for_new_brand
            _name = name or f"用户{user_id}"
            _is_test = detect_is_test_for_new_brand(_name)
            cur.execute("""
                INSERT INTO brands (name, industry, cities, brand_type, owner_user_id, status, is_test)
                VALUES (%s, %s, %s, 'self', %s, 'active', %s)
                RETURNING id
            """, (_name, industry, city, user_id, _is_test))
            brand_id = cur.fetchone()["id"]
            cur.execute("UPDATE brands SET brand_code = %s WHERE id = %s AND brand_code IS NULL",
                        (f"BRD-{brand_id:04d}", brand_id))
            # 绑定 user_clients
            cur.execute("INSERT INTO user_clients (user_id, brand_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (user_id, brand_id))

        # 2. 创建 profile（带真实行业/业务信息）— 先查是否已有
        import shortuuid
        cur.execute("SELECT id FROM client_profiles WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL) LIMIT 1", (brand_id,))
        existing_p = cur.fetchone()
        if existing_p:
            profile_id = existing_p["id"]
            profile_updates = []
            profile_values = []
            if name or industry:
                profile_updates.append("name = %s")
                profile_values.append(name or industry)
            if industry:
                profile_updates.append("industry = %s")
                profile_values.append(industry)
            if business:
                profile_updates.append("business = %s")
                profile_values.append(business)
            if profile_updates:
                profile_values.append(profile_id)
                cur.execute(
                    f"UPDATE client_profiles SET {', '.join(profile_updates)}, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    profile_values,
                )
        else:
            profile_id = shortuuid.uuid()[:8]
            cur.execute("""
                INSERT INTO client_profiles (id, name, industry, business, brand_id)
                VALUES (%s, %s, %s, %s, %s)
            """, (profile_id, name or industry, industry, business, brand_id))

    return {"success": True, "brand_id": brand_id, "profile_id": profile_id}


# ========== 我的客户 ==========

@router.get("/api/my-clients")
async def list_my_clients(request: Request, page: int = 1, page_size: int = 50, show_all: bool = False, search: str = "", include_test: bool = False):
    """获取客户品牌列表。管理员传 show_all=true 可查看所有用户的客户。search 支持后端模糊搜索。

    A.3 (CTO-15.18 · 2026-04-28):跟 /api/m3/customers SSOT 对齐
    - 默认 include_test=false 隐藏测试客户(跟 M3 客户池一致)
    - admin 调试用 ?include_test=true 看全部
    """
    user = _get_user(request)
    user_id = user["user_id"]
    is_admin = user.get("is_admin", False)
    organization_identity = getattr(request.state, "organization_identity", None)
    organization_member = bool(organization_identity is not None and organization_identity.is_member)
    assigned_ids = list(getattr(request.state, "organization_brand_ids", []) or []) if organization_member else []

    # 管理员 + show_all → 查全部；否则只查自己的
    query_all = is_admin and show_all

    # A.2/A.3 测试客户隔离 filter(默认 ON · 跟 M3 SSOT)
    # [BUG6 2026-06-05] include_test=true 仅 admin 生效 · 非 admin 强制排除测试客户(防 query 参数绕过)
    _effective_include_test = include_test and is_admin
    # 🔴 [#67 2026-09-05] 隔离**只对平台级视图**生效,不对服务商自己的列表。
    #    M3 铁律 5 的目标是平台视图与统计;而「我的客户」列表里,
    #    服务商自己建的「测试科技有限公司」被自动打上 is_test 后就此消失 ——
    #    她不知道它去哪了,也没有任何提示。
    # 🔴 [#116] 规则搬到唯一定义处 —— dashboard 的 brand_count 此前各写一份
    #    且写反了(它在自己名下的范围里仍排除 is_test),两处给出不同答案而不报错。
    from services.brand_test_visibility import (
        own_scope_test_clause as _own_scope_clause,
        platform_test_clause as _platform_clause)
    platform_test_clause = _platform_clause(
        include_test=bool(include_test), is_admin=bool(is_admin))
    #    🔴 自己名下那一支**不隔离**:下面那条 SQL 的 WHERE 已经是
    #    `b.owner_user_id = <本人或其组织 principal>`(再被 assigned_ids 收窄),
    #    每一行本来就在请求者自己的范围内,不存在「看到别人的测试客户」这回事。
    #    刻意**不**写成 `OR b.owner_user_id = %s`:那一支里它是冗余,
    #    而写进公共 clause 会连带放宽 admin 的平台视图 —— 两支要各自表达各自的意思。
    own_scope_test_clause = _own_scope_clause()

    # 搜索条件（后端过滤，解决分页导致前端搜索不全的问题）
    # [CTO-15.23 2026-05-20] 老板报"我的客户搜'揭阳'搜不到 · 但 DB 里有 2 个揭阳客户":
    # 之前 search 只覆盖 name + industry · 漏 cities + company_name
    # 4 字段全搜:name(品牌名)/industry(行业)/company_name(公司名)/cities(所在地)
    # 覆盖搜城市("深圳"/"广州")/搜公司名/搜行业关键字全场景
    # [客户反馈③ 2026-08-09] 排序换口径:星标优先 → 最近服务活跃 → 建档时间。
    #
    # 旧口径恒 `ORDER BY b.updated_at DESC NULLS LAST` 的问题是**它不是活跃度**:
    #   `brands.updated_at` 被任何一次字段编辑顶到最前(改个备注也顶),
    #   而且会被批处理整批顶起来。2026-08-09 生产快照实测(客户最多的那个代理,32 个客户):
    #   按 updated_at 排出来的**前 15 名里有 14 个 `last_service_at` 为空** —— 一次报价、
    #   一篇文章都没有;真正 08-05 / 08-04 有服务动作的两家排在第 17 / 18 位。
    #
    # 新口径:
    #   1) `is_starred` 置顶(代理自己按的),同为星标按 `starred_at` 新的在前;
    #   2) `last_service_at` = 该品牌**最近一次报价或文章的 created_at**(真服务动作);
    #   3) 🔴 `b.created_at` 参与的是 **GREATEST 里面**,不是排在后面的次级键。
    #      写成次级键的话,一个 2026-05 有文章的老客户会永远压在"今天刚建的新客户"上面
    #      —— 而"我刚加完客户上哪找"是同一批投诉里的相邻场景。放进 GREATEST 后语义是
    #      "这个客户身上最近发生的一件正经事":有服务动作就是服务动作,没有就是建档本身。
    #      `b.updated_at`(改个字段就顶)始终**不参与**这个 GREATEST,这是本条修复的要点。
    #   4) 最后仍以 `b.updated_at` 破平,保证全序稳定(分页不跳行)。
    _order_by = (
        " ORDER BY b.is_starred DESC, b.starred_at DESC NULLS LAST,"
        " GREATEST("
        "   COALESCE((SELECT MAX(q.created_at) FROM quotes q WHERE q.brand_id = b.id), '-infinity'::timestamp),"
        "   COALESCE((SELECT MAX(a.created_at) FROM articles a WHERE a.brand_id = b.id), '-infinity'::timestamp),"
        "   COALESCE(b.created_at, '-infinity'::timestamp)"
        " ) DESC,"
        " b.updated_at DESC NULLS LAST"
    )

    search_clause = ""
    search_params: list = []
    if search.strip():
        search_clause = (
            " AND (b.name ILIKE %s "
            "OR COALESCE(b.industry, '') ILIKE %s "
            "OR COALESCE(b.company_name, '') ILIKE %s "
            "OR COALESCE(b.cities, '') ILIKE %s)"
        )
        like_val = f"%{search.strip()}%"
        search_params = [like_val, like_val, like_val, like_val]

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()

        # [CTO-15.23 2026-05-20] count SQL 也用 `FROM brands b` alias · search_clause 直接复用
        # 之前 `test_clause_no_alias` 需要把 `b.is_test` 转 `is_test` 是因为 count SQL 没 alias
        # 现在 count SQL 加了 alias · 直接用 test_clause 即可 · 这个变量不再需要

        if query_all:
            cur.execute(f"""
                SELECT b.*,
                       COALESCE((SELECT total_score FROM diagnosis_records WHERE brand_id = b.id AND (result_visibility IS NULL OR result_visibility = 'published') ORDER BY created_at DESC LIMIT 1), 0) as latest_score,
                       (SELECT COUNT(*) FROM diagnosis_records WHERE brand_id = b.id AND (result_visibility IS NULL OR result_visibility = 'published')) as diagnosis_count,
                       EXISTS(SELECT 1 FROM client_profiles WHERE brand_id = b.id AND is_deleted = 0) as has_profile,
                       u.display_name as owner_name,
                       COALESCE(NULLIF(u.display_name, ''), NULLIF(u.username, ''),
                                CASE WHEN b.owner_user_id IS NOT NULL THEN '账号#' || b.owner_user_id::text ELSE NULL END) as owner_account
                FROM brands b
                LEFT JOIN users u ON u.id = b.owner_user_id
                WHERE (b.is_deleted IS NULL OR b.is_deleted = FALSE){platform_test_clause}{search_clause}
                {_order_by}
                LIMIT %s OFFSET %s
            """, (*search_params, page_size, (page - 1) * page_size))
        else:
            diagnosis_scope_sql = ""
            diagnosis_scope_params: list = []
            if organization_member:
                diagnosis_scope_sql = " AND organization_id=%s AND created_by_membership_id=%s"
                diagnosis_scope_params = [
                    organization_identity.organization_id,
                    organization_identity.membership_id,
                ]
            owner_user_id = (
                organization_identity.principal_user_id if organization_member else user_id
            )
            assignment_sql = ""
            assignment_params: list = []
            if organization_member:
                if not assigned_ids:
                    assignment_sql = " AND FALSE"
                else:
                    assignment_sql = " AND b.id IN (" + ",".join(["%s"] * len(assigned_ids)) + ")"
                    assignment_params = assigned_ids
            cur.execute(f"""
                SELECT b.*,
                       COALESCE((SELECT total_score FROM diagnosis_records WHERE brand_id = b.id AND (result_visibility IS NULL OR result_visibility = 'published'){diagnosis_scope_sql} ORDER BY created_at DESC LIMIT 1), 0) as latest_score,
                       (SELECT COUNT(*) FROM diagnosis_records WHERE brand_id = b.id AND (result_visibility IS NULL OR result_visibility = 'published'){diagnosis_scope_sql}) as diagnosis_count,
                       EXISTS(SELECT 1 FROM client_profiles WHERE brand_id = b.id AND is_deleted = 0) as has_profile
                FROM brands b
                WHERE b.owner_user_id = %s AND b.brand_type = 'client'
                  AND (b.is_deleted IS NULL OR b.is_deleted = FALSE){assignment_sql}{own_scope_test_clause}{search_clause}
                {_order_by}
                LIMIT %s OFFSET %s
            """, (
                *diagnosis_scope_params,
                *diagnosis_scope_params,
                owner_user_id,
                *assignment_params,
                *search_params,
                page_size,
                (page - 1) * page_size,
            ))
        clients = [dict(r) for r in cur.fetchall()]
        from services.client_status import client_status_label, normalize_client_status
        for client in clients:
            status_value = normalize_client_status(client.get("status"))
            client["client_status"] = status_value
            client["brand_status"] = status_value
            client["status_label"] = client_status_label(status_value)
            if not query_all:
                # 🔴 [invrel 2026-08-13] 关系类私有字段清单收敛到单点
                # `services/relationship_privacy.RELATIONSHIP_PRIVATE_FIELDS`。
                # 原来这里和 :1404 各写一份字面量 —— 两份各自演化必有一处漏字段。
                # 本地额外字段(owner_name 等)仍在这里叠加,基础清单不许缩小。
                for private_field in (
                    *RELATIONSHIP_PRIVATE_FIELDS, "owner_name", "owner_account",
                    "created_by", "updated_by",
                ):
                    client.pop(private_field, None)
                if organization_member:
                    client.pop("agent_payment_note", None)

        # [CTO-15.23 2026-05-20] count SQL 必须用 `b` alias · 跟 search_clause(`b.name` 等)对齐
        # 之前 `FROM brands`(无 alias)+ search_clause 用 `b.name` → SQL 500 missing FROM-clause
        # → frontend catch silent · UI 显原 total 视觉上"搜不出来"(老板搜"揭阳"复现)
        # 修法:`FROM brands b` 加 alias · search_clause 兼容
        if query_all:
            cur.execute(f"""
                SELECT COUNT(*) as cnt FROM brands b
                WHERE (b.is_deleted IS NULL OR b.is_deleted = FALSE){platform_test_clause}{search_clause}
            """, search_params)
        else:
            owner_user_id = (
                organization_identity.principal_user_id if organization_member else user_id
            )
            assignment_sql = ""
            assignment_params = []
            if organization_member:
                if not assigned_ids:
                    assignment_sql = " AND FALSE"
                else:
                    assignment_sql = " AND b.id IN (" + ",".join(["%s"] * len(assigned_ids)) + ")"
                    assignment_params = assigned_ids
            cur.execute(f"""
                SELECT COUNT(*) as cnt FROM brands b
                WHERE b.owner_user_id = %s AND b.brand_type = 'client'
                  AND (b.is_deleted IS NULL OR b.is_deleted = FALSE){assignment_sql}{own_scope_test_clause}{search_clause}
            """, (owner_user_id, *assignment_params, *search_params))
        total = cur.fetchone()["cnt"]

        return {"success": True, "clients": clients, "total": total, "page": page, "show_all": query_all}
    finally:
        conn.close()


class ClientStarRequest(BaseModel):
    starred: bool


@router.patch("/api/my-clients/{brand_id}/star")
async def set_client_star(brand_id: int, req: ClientStarRequest, request: Request):
    """[客户反馈③ 2026-08-09] 星标/取消星标一个客户。

    🔴 RBAC:**核验的 target 与写入的 target 必须是同一个 brand_id**。
      下面 UPDATE 的 WHERE 里逐字重复 `id = %s AND owner_user_id = %s`,
      不靠"上面查过了"这句话 —— 查完到写之间隔着一次网络往返,
      而且 admin 分支还放宽了 owner 条件,单靠前置查询会给出一条"查 A 写 B"的缝。
      admin 的放宽写在 SQL 里(`OR %s`),同一条语句里生效,不另开一条 UPDATE。
    🔴 组织成员:只能标被指派给自己的品牌(与列表口径同源 organization_brand_ids)。
    """
    user = _get_user(request)
    user_id = user["user_id"]
    is_admin = bool(user.get("is_admin", False))

    organization_identity = getattr(request.state, "organization_identity", None)
    organization_member = bool(organization_identity is not None and organization_identity.is_member)
    if organization_member:
        assigned_ids = list(getattr(request.state, "organization_brand_ids", []) or [])
        if brand_id not in assigned_ids:
            raise HTTPException(status_code=404, detail="客户不存在或无权操作")
        owner_user_id = organization_identity.principal_user_id
    else:
        owner_user_id = user_id

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE brands
               SET is_starred = %s,
                   starred_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END
             WHERE id = %s
               AND (is_deleted IS NULL OR is_deleted = FALSE)
               AND (owner_user_id = %s OR %s)
            RETURNING id, is_starred, starred_at
            """,
            (req.starred, req.starred, brand_id, owner_user_id, is_admin),
        )
        row = cur.fetchone()
        if not row:
            # 不区分"不存在"与"不是你的" —— 区分了就是一个枚举他人 brand_id 的信道。
            conn.rollback()
            raise HTTPException(status_code=404, detail="客户不存在或无权操作")
        conn.commit()
        logger.info(
            "[ClientStar] user=%s brand=%s starred=%s admin=%s",
            user_id, brand_id, req.starred, is_admin,
        )
        return {
            "success": True,
            "brand_id": row["id"],
            "is_starred": bool(row["is_starred"]),
            "starred_at": row["starred_at"].isoformat() if row["starred_at"] else None,
        }
    finally:
        conn.close()


@router.get("/api/my-clients/check-duplicate")
async def check_duplicate_client(name: str, request: Request):
    """检测重名 / **疑似重复**客户（P1-11 · 2026-07-26 升级）。

    生产实证：``brands.id=278``「深圳驰鲸科技」与 ``brands.id=712``
    「深圳市驰鲸科技有限公司」是同一家公司，各自独立建档、独立计费、数据不互通
    （其中一个还因为名字畸形连拿两份 0 分报告）。旧版只做**精确同名**比对，
    这两个名字永远匹配不上。

    现在：名称归一（去公司后缀 / 行政区划后缀 / 标点空白）+ 相似度，
    命中就提示"疑似已存在，是否使用已有品牌"。

    边界：本接口**只提示，不做任何写入**。合并/改归属必须由用户在弹窗里显式选择
    （使用已有品牌 = 换 brand_id；登记别名 = 走既有 brand_display_names 审计路径），
    绝不静默改归属（SSOT §16 软删保留审计、不无痕重写）。

    返回：
      - ``duplicate``：精确同名（保持旧字段，前端旧逻辑不变）
      - ``similar``：疑似重复列表 [{brand_id, name, industry, city, match, similarity}]
    """
    user = _get_user(request)
    user_id = user["user_id"]

    name_trimmed = (name or "").strip()
    if not name_trimmed:
        return {"success": True, "duplicate": None, "similar": []}

    from utils.brand_name_hygiene import brand_dedupe_key, normalize_brand_name

    target_key = brand_dedupe_key(name_trimmed)
    target_clean = normalize_brand_name(name_trimmed)

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 一次取回该 owner 名下的候选（数量级 = 单个代理的客户数，可控）
        cur.execute(
            """
            SELECT id, name, industry, cities
            FROM brands
            WHERE owner_user_id = %s
              AND brand_type = 'client'
              AND (is_deleted IS NULL OR is_deleted = FALSE)
            ORDER BY id DESC
            LIMIT 500
            """,
            (user_id,),
        )
        rows = [dict(r) for r in (cur.fetchall() or [])]
    finally:
        conn.close()

    exact = None
    similar: list[dict] = []
    for row in rows:
        existing_name = str(row.get("name") or "")
        if not existing_name:
            continue
        entry = {
            "brand_id": row["id"],
            "name": existing_name,
            "industry": row.get("industry") or None,
            "city": row.get("cities") or None,
        }
        if existing_name.strip() == name_trimmed:
            if exact is None:
                exact = entry
            continue

        existing_key = brand_dedupe_key(existing_name)
        if not target_key or not existing_key:
            continue
        if existing_key == target_key:
            similar.append({**entry, "match": "normalized_equal", "similarity": 1.0})
            continue
        # 一方是另一方的子串（"驰鲸科技" vs "深圳市驰鲸科技有限公司"）
        if len(target_key) >= 3 and len(existing_key) >= 3 and (
            target_key in existing_key or existing_key in target_key
        ):
            shorter, longer = sorted((target_key, existing_key), key=len)
            similar.append({
                **entry,
                "match": "containment",
                "similarity": round(len(shorter) / len(longer), 3),
            })
            continue
        # 兜底：标准库序列相似度（纯本地、无 LLM）
        from difflib import SequenceMatcher

        ratio = SequenceMatcher(None, target_key, existing_key).ratio()
        if ratio >= 0.82:
            similar.append({**entry, "match": "fuzzy", "similarity": round(ratio, 3)})

    similar.sort(key=lambda item: -float(item.get("similarity") or 0))
    return {
        "success": True,
        "duplicate": exact,
        "similar": similar[:5],
        "normalized_name": target_clean,
        "rule_version": "brand-duplicate-detection-v1",
    }


@router.post("/api/my-clients")
async def add_client(req: AddClientRequest, request: Request):
    """添加客户（创建 brand_type=client + profile）

    同名合并场景（existing 命中）走复用:给已有记录补字段,不新建。

    🔴 [WO_222-c0 · Owner 直令 2026-09-15] 原「L0 只能管 1 个非-self 品牌」**已撤**。
       普通账号也要能给别人代运营 —— 除经营后台外权限与服务商一致。
       触发:真客户把一个客户认证成「我的品牌」后,再也建不了第二个客户做体检报告。
    """
    user = _get_user(request)
    user_id = user["user_id"]

    # [F-1] **归属**按商业主体(principal)判定,不按操作者。
    #   🔴 [WO_222-c0] 这段原本还管**额度** —— 额度已整体撤销(见下),
    #      但归属这一半仍然活着,别连着一起读成历史。
    #
    # 事故形态:组织员工(操作员席位)自己的 agent_level 恒为 0(他不是服务商,
    # 服务商是他所在组织的 owner)。旧代码拿操作者的 agent_level 判额度,于是
    # 服务商雇的员工建第 2 个客户就被 402 UPGRADE_REQUIRED 拦死 —— 而额度本来
    # 就是买给 owner 的。
    #
    # 修法:员工代 owner 作业时,额度看 owner 的等级、品牌也落在 owner 名下
    # (归属不因谁动手而改变,避免出现"挂在员工名下、owner 看不见"的孤儿品牌;
    # 员工是谁做的这件事由组织审计与席位分配记录,不靠 owner_user_id 表达)。
    # 员工的 agent_level **一个字都不改** —— 资金身份语义不动。
    organization_identity = getattr(request.state, "organization_identity", None)
    organization_member = bool(organization_identity is not None and organization_identity.is_member)
    principal_user_id = int(organization_identity.principal_user_id) if organization_member else user_id

    # [P0-1] 品牌名入库校验必须在同名查询之前：脏名字会污染
    # 同名合并判断（brand 278 那种带换行 + "城市:" 标签的名字永远匹配不上已有品牌）。
    req.name = _clean_brand_name_or_400(req.name, field="客户名")

    from db.connection import get_db, get_connection
    import shortuuid

    # 🔴 [WO_222-c0 · Owner 直令 2026-09-15] **L0 品牌额度已撤**。
    #   原逻辑:`effective_agent_level(...) < 1` 且已有 client/legacy 品牌 ≥1
    #   ⇒ 402 UPGRADE_REQUIRED「普通用户只能管理 1 个品牌」。
    #
    #   Owner 原话(2026-09-15 13:27 北京):「之前有一段时间说的普通账户只能自己用,
    #   后面想着普通账号也能给别人代运营……除了经营后台,其他的权限应该和服务商一样才对。」
    #   真客户实证:把一个客户认证成「我的品牌」之后,就再也建不了第二个客户做体检报告。
    #
    #   🔴 `auth.principal_identity.effective_agent_level` **不删**:它是员工继承商业
    #      主体等级的单点,别处还在用;这里只是不再有额度可继承。顺手删函数会把
    #      那些调用点一起带走(本仓 deleting-a-branch-orphans-producers-in-files-you-never-touched)。
    #   🔴 同名合并那一路(原 `will_merge` 探测)**不是额度逻辑**,它在下面的主查询里
    #      本来就有一份;额度块里那份是**同一谓词的第二处副本**,随额度一起撤。

    with get_db() as conn:
        cur = conn.cursor()

        # 品牌按 owner_user_id 隔离：不同用户可同名，同用户重名则合并到已有 brand_id
        # （CTO-13.0 修：此前按全局 name 查导致撞到其他用户的 self 品牌，list_my_clients 过滤 brand_type='client' 看不到）
        cur.execute("""
            SELECT id FROM brands
            WHERE name = %s AND owner_user_id = %s
              AND (is_deleted IS NULL OR is_deleted = FALSE)
            LIMIT 1
        """, (req.name, principal_user_id))
        existing = cur.fetchone()
        if existing:
            brand_id = existing["id"]
            cur.execute("""
                UPDATE brands SET
                    industry = COALESCE(NULLIF(industry, ''), %s),
                    cities = COALESCE(NULLIF(cities, ''), %s),
                    brand_type = CASE WHEN brand_type = 'legacy' OR brand_type IS NULL THEN 'client' ELSE brand_type END,
                    updated_at = NOW()
                WHERE id = %s
            """, (req.industry, req.city, brand_id))
        else:
            # A.2 CTO-15.18 · 名字含"测试|test|_demo|_test|验收"自动 is_test=true
            from utils.is_test_brand import detect_is_test_for_new_brand
            _is_test = detect_is_test_for_new_brand(req.name)
            cur.execute("""
                INSERT INTO brands (name, industry, cities, brand_type, owner_user_id, status, is_test, updated_at)
                VALUES (%s, %s, %s, 'client', %s, 'active', %s, NOW())
                RETURNING id
            """, (req.name, req.industry, req.city, principal_user_id, _is_test))
            brand_id = cur.fetchone()["id"]

        cur.execute("UPDATE brands SET brand_code = %s WHERE id = %s",
                    (f"BRD-{brand_id:04d}", brand_id))

        # Phase F · 2026-04-27 · 落 seed_keywords(关键词从启动诊断页搬过来)
        if req.seed_keywords:
            try:
                # 去重 + trim + 限 20 个
                seen_kw: set = set()
                clean_kws: list[str] = []
                for k in (req.seed_keywords or []):
                    if not isinstance(k, str):
                        continue
                    k = k.strip()
                    if k and k not in seen_kw and len(clean_kws) < 20:
                        seen_kw.add(k)
                        clean_kws.append(k)
                if clean_kws:
                    cur.execute(
                        "UPDATE brands SET seed_keywords = %s::jsonb WHERE id = %s",
                        (json.dumps(clean_kws, ensure_ascii=False), brand_id),
                    )
            except Exception as ke:
                logger.warning(f"[add_client] seed_keywords 落库失败(非 block) brand={brand_id}: {ke}")

        # 创建 profile（已有则复用）
        cur.execute("SELECT id FROM client_profiles WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL) LIMIT 1", (brand_id,))
        existing_profile = cur.fetchone()
        if existing_profile:
            profile_id = existing_profile["id"]
        else:
            profile_id = shortuuid.uuid()[:8]
            cur.execute("""
                INSERT INTO client_profiles (id, name, industry, business, brand_id)
                VALUES (%s, %s, %s, %s, %s)
            """, (profile_id, req.name, req.industry, req.business, brand_id))

        # user_clients 映射
        if organization_member:
            # [F-1] 员工路径**不写 user_clients**:席位模型在开户时刻意清空过遗留
            # 访问权(snapshot_and_clear_legacy_access),这里再写回去等于在组织授权
            # 体系之外开一条影子通道,离职回收也管不到。
            # 改为落**组织席位分配**:员工能看见自己刚录的客户,且这条可见性完全
            # 受组织治理(转交/回收/离职级联)管辖。
            cur.execute(
                """
                INSERT INTO organization_brand_assignments(
                  organization_id, membership_id, brand_id, status, assigned_by_user_id, reason, request_id
                ) VALUES (%s, %s, %s, 'active', %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    organization_identity.organization_id,
                    organization_identity.membership_id,
                    brand_id,
                    user_id,
                    "员工录入新客户自动分配",
                    f"add-client:{brand_id}:{user_id}",
                ),
            )
        else:
            cur.execute("INSERT INTO user_clients (user_id, brand_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (user_id, brand_id))

        # 如果从诊断导入，关联诊断数据
        if req.from_diagnosis_id:
            try:
                # [对抗审核订正 R8-CAN-010] 复原本批 fix:原 fix gate 在 operator_user_id 列上,
                # 但该列在 diagnosis_records 从未被写入(INSERT 不含它)→ UPDATE 恒 0 行 →
                # 打断合法的"从诊断导入客户"功能(违反不破坏现有功能铁律)。
                # diagnosis_records 无可靠创建者列,NULL-brand 诊断归属无法验证 → 需 schema
                # 加 creator 列的设计变更,本批不安全修复,标 SKIPPED/needs-design,恢复原行为。
                cur.execute("""
                    UPDATE diagnosis_records SET brand_id = %s
                    WHERE id = %s AND brand_id IS NULL
                """, (brand_id, req.from_diagnosis_id))

                # 从诊断数据预填 profile
                # 🔴 [#114] 生产 diagnosis_records **没有 `data` 列** —— 它是 `raw_data_json`,
                #    而且 business_context 在**里面一层**:
                #      workflows/diagnosis_workflow.py:717  results["data"]["business_context"] = …
                #      db/diagnosis_db.py:2618              result["data"] = json.loads(raw_data_json)["data"]
                #    原写法 UndefinedColumn,被下面 `except Exception as e` 吞成一条 warning ⇒
                #    「从诊断预填 profile」这个功能**从来没生效过**,而用户看不出来。
                cur.execute(
                    "SELECT raw_data_json FROM diagnosis_records WHERE id = %s",
                    (req.from_diagnosis_id,),
                )
                diag = cur.fetchone()
                if diag and diag.get("raw_data_json"):
                    try:
                        _raw = diag["raw_data_json"]
                        raw = json.loads(_raw) if isinstance(_raw, str) else _raw
                        data = raw.get("data") or {}
                        biz = data.get("business_context", {})
                        if biz.get("core_business"):
                            cur.execute("UPDATE client_profiles SET business = %s WHERE id = %s",
                                       (biz["core_business"], profile_id))
                        if biz.get("target_customers"):
                            cur.execute("UPDATE client_profiles SET target_users = %s WHERE id = %s",
                                       (biz["target_customers"], profile_id))
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"从诊断导入失败: {e}")

    return {"success": True, "brand_id": brand_id, "profile_id": profile_id}


@router.get("/api/my-clients/{brand_id}")
async def get_client_detail(brand_id: int, request: Request):
    """获取客户详情（brand + profile）"""
    user = _get_user(request)
    from auth.brand_access import require_brand_access
    require_brand_access(request, brand_id, allow_null=False)
    organization_identity = getattr(request.state, "organization_identity", None)
    organization_member = bool(organization_identity is not None and organization_identity.is_member)

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT b.* FROM brands b
            WHERE b.id = %s AND (b.is_deleted IS NULL OR b.is_deleted = FALSE)
        """, (brand_id,))
        brand = cur.fetchone()
        if not brand:
            raise HTTPException(404, "客户不存在")

        # Legacy permission fallback. Organization members were already
        # checked against the live assignment table above.
        if not organization_member and not user.get("is_admin") and brand.get("owner_user_id") != user["user_id"]:
            # 兜底：检查 user_clients
            cur.execute("SELECT 1 FROM user_clients WHERE user_id = %s AND brand_id = %s", (user["user_id"], brand_id))
            if not cur.fetchone():
                raise HTTPException(403, "无权访问此客户")

        cur.execute("""
            SELECT * FROM client_profiles
            WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
            ORDER BY updated_at DESC LIMIT 1
        """, (brand_id,))
        profile = cur.fetchone()

        # 诊断记录数 [返工2 P1-2] published-only(排 withheld+pending)
        if organization_member:
            cur.execute(
                "SELECT COUNT(*) as cnt FROM diagnosis_records WHERE brand_id=%s "
                "AND organization_id=%s AND created_by_membership_id=%s "
                "AND result_visibility='published'",
                (brand_id, organization_identity.organization_id, organization_identity.membership_id),
            )
        else:
            cur.execute("SELECT COUNT(*) as cnt FROM diagnosis_records WHERE brand_id = %s "
                        "AND (result_visibility IS NULL OR result_visibility = 'published')", (brand_id,))
        diag_count = cur.fetchone()["cnt"]

        # v1_2 (CTO-14.0 2026-04-19): 补品牌详情 4 维度 GEO 真数据，修 Bug 1 "扣 260 积分 UI 全 --" 事故
        #   - GEO 文章数：content_records 里属于该 brand 的
        #   - 已上榜关键词数：monitoring_keywords 里 ranked=true 的
        #   - 知识库文件数：client_knowledge_files 里属于该 brand 的
        #   每条 SQL 独立 try-except 容错：表不存在或字段缺失不影响诊断数主路径
        article_count = 0
        try:
            if organization_member:
                # [BUG-9 2026-07-27] 品牌归属【不能】读 articles.brand_id —— 生产 1199 篇该列全为 NULL,
                # 全站真实链路是 quote_id → quotes.brand_id(本文件 :1425 的非组织分支就是这么写的)。
                # 改成 JOIN quotes 后口径与全站统一,组织维度过滤保留不变。
                # ⚠️ articles.organization_id / created_by_membership_id 在生产同样全为 NULL
                #    (组织产物列三件套都没有写入方),所以本分支当前仍会返回 0 —— 那是组织功能
                #    整体未接通,不是这一行的问题,已在 EXIT 单独上报,不在本次改动范围内 backfill。
                cur.execute(
                    "SELECT COUNT(*) as cnt FROM articles a JOIN quotes q ON q.id = a.quote_id "
                    "WHERE q.brand_id=%s AND a.organization_id=%s AND a.created_by_membership_id=%s",
                    (brand_id, organization_identity.organization_id, organization_identity.membership_id),
                )
            else:
                cur.execute("SELECT COUNT(*) as cnt FROM content_records WHERE brand_id = %s", (brand_id,))
            article_count = cur.fetchone()["cnt"]
        except Exception:
            pass

        ranked_keyword_count = 0
        try:
            # 优先按 monitoring_keywords.is_ranked / ranked 字段；字段不存在则退化为 COUNT 全部
            cur.execute("""
                SELECT COUNT(*) as cnt FROM monitoring_keywords
                WHERE brand_id = %s AND (ranked = TRUE OR is_ranked = TRUE)
            """, (brand_id,))
            ranked_keyword_count = cur.fetchone()["cnt"]
        except Exception:
            try:
                cur.execute("SELECT COUNT(*) as cnt FROM monitoring_keywords WHERE brand_id = %s", (brand_id,))
                ranked_keyword_count = cur.fetchone()["cnt"]
            except Exception:
                pass

        knowledge_count = 0
        try:
            cur.execute("SELECT COUNT(*) as cnt FROM client_knowledge_files WHERE brand_id = %s", (brand_id,))
            knowledge_count = cur.fetchone()["cnt"]
        except Exception:
            try:
                # 兼容 knowledge_documents 表名
                cur.execute("SELECT COUNT(*) as cnt FROM knowledge_documents WHERE brand_id = %s", (brand_id,))
                knowledge_count = cur.fetchone()["cnt"]
            except Exception:
                pass

        # GEO 完整度（基础信息 30% + 知识库非空 30% + 深度解析完成 40%）
        completeness = 0
        try:
            brand_dict = dict(brand)
            # 基础信息：name / industry / city / description 有值
            has_base = all([brand_dict.get(f) for f in ("name", "industry_category")])
            if has_base:
                completeness += 30
            if knowledge_count > 0:
                completeness += 30
            # 深度解析完成
            profile_dict = dict(profile) if profile else {}
            if profile_dict.get("industry_brief_status") == "done" and profile_dict.get("industry_brief"):
                completeness += 40
        except Exception:
            pass

        # v1_2: 把计数字段 merge 到 brand 对象里（前端 L94 用 brand?.* 读，保持兼容）
        brand_dict = dict(brand)
        brand_dict["diagnosis_count"] = diag_count
        brand_dict["article_count"] = article_count
        brand_dict["ranked_keyword_count"] = ranked_keyword_count
        brand_dict["knowledge_count"] = knowledge_count
        brand_dict["geo_completeness"] = completeness
        # [WO_267 读侧] 附大类 key / 中文名(只增不改,原 industry_category 原样保留)。
        #   前端下拉按 key 选中;不附的话存量旧名对不上字典名,只能显示「自动判断」。
        from services.industry_taxonomy import category_fields
        brand_dict.update(category_fields(brand_dict.get("industry_category")))

        # 非 admin 用户过滤 profile 敏感字段
        is_admin = user.get("is_admin", False)
        if not is_admin:
            # 同上:基础清单单点化,本地只叠加 created_by / updated_by。
            for private_field in (*RELATIONSHIP_PRIVATE_FIELDS, "created_by", "updated_by"):
                brand_dict.pop(private_field, None)
        if organization_member:
            brand_dict.pop("agent_payment_note", None)
        profile_out = dict(profile) if profile else None
        if profile_out and not is_admin:
            for f in ("admin_insight", "admin_insight_level", "admin_insight_updated_at"):
                profile_out.pop(f, None)

        # [WO_220-c2''] 写作大厅基础资料表六字段也要挂在**这个**端点上。
        # 🔴 c2 只挂在 `/api/profiles/{id}`,而写作大厅读的是**这一个** ——
        #    于是 c2 上线后 products_services / proof_cases 仍然永远是空的。
        #    我的判据验的是「做事方」(profiles 端点),没问「被服务方」
        #    (前端实际调的这一个)。A 端到端第一发就逮到了。
        # 🔴 **复用同一份映射**,绝不在这里另拼一份:
        #    两处各拼各的,加字段时漏改一边,症状又是「保存成功、刷新还是空」。
        if profile_out is not None:
            from services.writing_basics_contract import (
                from_profile as _writing_basics_from_profile)
            profile_out["writing_basics"] = _writing_basics_from_profile(profile_out)

        return {
            "success": True,
            "brand": brand_dict,
            "profile": profile_out,
            # 顶层也保留（兼容可能已读 res.data.diagnosis_count 的老代码）
            "diagnosis_count": diag_count,
            "article_count": article_count,
            "ranked_keyword_count": ranked_keyword_count,
            "knowledge_count": knowledge_count,
            "geo_completeness": completeness,  # 0-100 分
        }
    finally:
        conn.close()


@router.put("/api/my-clients/{brand_id}")
async def update_client(brand_id: int, req: BrandInfoUpdate, request: Request):
    """更新客户信息（复用 my-brand 更新逻辑）"""
    user = _get_user(request)
    from auth.brand_access import require_brand_access
    require_brand_access(request, brand_id, allow_null=False)
    organization_identity = getattr(request.state, "organization_identity", None)
    organization_member = bool(organization_identity is not None and organization_identity.is_member)

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        # [WO_252 ①] 带上 name:建 profile 行时 `name` NOT NULL,
        # 而"只改联系方式"的请求不带 name —— 没有兜底就会 500。
        cur.execute("SELECT owner_user_id, name FROM brands WHERE id = %s", (brand_id,))
        brand = cur.fetchone()
        if not brand:
            raise HTTPException(404, "客户不存在")
        brand_name_now = brand.get("name")
        if not organization_member and not user.get("is_admin") and brand.get("owner_user_id") != user["user_id"]:
            raise HTTPException(403, "无权修改此客户")
    finally:
        conn.close()

    # 复用 my-brand 的更新逻辑
    from db.connection import get_db
    import json

    _persist_confirmed_display_names(brand_id, req.brand_display_names)

    brand_updates = {}
    # [P0-1] 改名同样过品牌名校验（自助端 / 代理端同一把闸；AI 填充也走这里）
    if req.name is not None: brand_updates["name"] = _clean_brand_name_or_400(req.name)
    if req.industry is not None: brand_updates["industry"] = req.industry
    if req.industry_category is not None:
        brand_updates["industry_category"] = _validated_industry_category_or_422(req.industry_category)
    if req.company_name is not None: brand_updates["company_name"] = req.company_name
    if req.cities is not None: brand_updates["cities"] = req.cities

    if brand_updates:
        with get_db() as conn:
            cur = conn.cursor()
            sets = [f"{k} = %s" for k in brand_updates]
            cur.execute(f"UPDATE brands SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                       list(brand_updates.values()) + [brand_id])

    profile_updates = {}
    if req.name is not None: profile_updates["name"] = req.name
    if req.industry is not None: profile_updates["industry"] = req.industry
    if req.business is not None: profile_updates["business"] = req.business
    if req.target_users is not None: profile_updates["target_users"] = req.target_users
    if req.products is not None: profile_updates["products"] = json.dumps(req.products, ensure_ascii=False)
    if req.pain_points is not None: profile_updates["pain_points"] = json.dumps(req.pain_points, ensure_ascii=False)
    if req.competitors is not None: profile_updates["competitors"] = json.dumps(req.competitors, ensure_ascii=False)
    if req.persona_positioning is not None: profile_updates["persona_positioning"] = req.persona_positioning
    if req.persona_tone is not None: profile_updates["persona_tone"] = req.persona_tone
    if req.content_direction is not None: profile_updates["content_direction"] = req.content_direction
    # [CTO-13.0 2026-04-19 S1.1] ai-fill 深度字段落地到 client_profiles（client 端同 self 端）
    if req.company_intro is not None: profile_updates["company_intro"] = req.company_intro
    if req.core_value is not None: profile_updates["core_value"] = req.core_value
    if req.selling_points is not None: profile_updates["selling_points"] = req.selling_points
    if req.success_cases is not None: profile_updates["success_cases"] = req.success_cases
    if req.testimonials is not None: profile_updates["testimonials"] = req.testimonials
    # [2026-06-02 GEO CTO] 联系方式 4 字段落地 client_profiles(写作引流 · 自发布完整/媒体软化)
    if req.contact_phone is not None: profile_updates["contact_phone"] = req.contact_phone
    if req.contact_wechat is not None: profile_updates["contact_wechat"] = req.contact_wechat
    if req.contact_website is not None: profile_updates["contact_website"] = req.contact_website
    if req.contact_address is not None: profile_updates["contact_address"] = req.contact_address
    if req.structured_knowledge is not None:
        profile_updates["structured_knowledge"] = json.dumps(req.structured_knowledge, ensure_ascii=False)
    # [CTO-15.9 M1c T4] 市场洞察 5 字段落库(client 端)
    if req.service_scope is not None:
        profile_updates["service_scope"] = req.service_scope
    if req.local_competitors is not None:
        profile_updates["local_competitors"] = json.dumps(req.local_competitors, ensure_ascii=False)
    if req.market_insight is not None:
        merged = _merge_market_insight_into_brief(brand_id, req.market_insight)
        if merged is not None:
            profile_updates["industry_brief"] = merged
    # [CTO-15.9 A.8] business_type / city_scope SSOT(profile)落库(client 端)
    if req.business_type is not None:
        profile_updates["business_type"] = req.business_type
    if req.city_scope is not None:
        profile_updates["city_scope"] = req.city_scope

    # [WO_252 ① 2026-09-20] 原来这里"没有行就跳过 UPDATE,照样回 success" ——
    # 六成真客户在 client_profiles 没有行,于是联系方式一直静默丢弃。改 upsert。
    persisted: List[str] = []
    if profile_updates:
        with get_db() as conn:
            cur = conn.cursor()
            persisted = _upsert_client_profile(
                cur, brand_id, profile_updates, fallback_name=brand_name_now)

    # `persisted` = 这次**真正落库**的字段名(brands + client_profiles 合并去重)。
    # 前端据它回显"保存了什么";它为空就说明这次什么都没落,不该被读成"保存成功"。
    return {"success": True,
            "persisted": sorted(set(list(brand_updates.keys()) + persisted))}


def _client_archive_preview_with_cursor(cur, brand_id: int) -> dict:
    cur.execute(
        """
        SELECT b.id, b.name, b.owner_user_id, b.is_deleted,
               (SELECT COUNT(*) FROM client_profiles p WHERE p.brand_id=b.id AND COALESCE(p.is_deleted,0)=0) AS profile_count,
               (SELECT COUNT(*) FROM diagnosis_records d WHERE d.brand_id=b.id AND COALESCE(d.is_deleted,FALSE)=FALSE) AS diagnosis_count,
               (SELECT COUNT(*) FROM quotes q WHERE q.brand_id=b.id AND q.deleted_at IS NULL) AS quote_count,
               (SELECT COUNT(*) FROM keyword_selection_sessions s WHERE s.brand_id=b.id) AS session_count,
               (SELECT COUNT(*) FROM confirmed_keywords k JOIN quotes q ON q.id=k.quote_id WHERE q.brand_id=b.id) AS keyword_count,
               (SELECT COUNT(*) FROM topics t JOIN quotes q ON q.id=t.quote_id WHERE q.brand_id=b.id) AS topic_count,
               (SELECT COUNT(*) FROM articles a JOIN quotes q ON q.id=a.quote_id WHERE q.brand_id=b.id) AS article_count,
               (SELECT COUNT(*) FROM user_clients uc WHERE uc.brand_id=b.id) AS assignment_count
        FROM brands b WHERE b.id=%s
        """,
        (brand_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "客户不存在")
    return {
        "object": {
            "type": "brand",
            "brand_id": int(row["id"]),
            "display_name": row.get("name") or f"客户 #{row['id']}",
        },
        "associations": {
            "profiles": int(row.get("profile_count") or 0),
            "diagnoses": int(row.get("diagnosis_count") or 0),
            "quotes": int(row.get("quote_count") or 0),
            "selection_sessions": int(row.get("session_count") or 0),
            "keywords": int(row.get("keyword_count") or 0),
            "topics": int(row.get("topic_count") or 0),
            "articles": int(row.get("article_count") or 0),
            "access_assignments": int(row.get("assignment_count") or 0),
        },
        "impact": [
            "客户、诊断与报价进入回收站",
            "公开报价链接失效，员工分配立即撤销",
            "关键词、文章与审计事实保留",
        ],
        "recovery": {
            "action": "restore",
            "target": f"/api/my-clients/{int(row['id'])}/restore",
            "permission": "clients.profile_edit",
            "window_days": 7,
            "note": "恢复客户和业务数据；员工分配需重新治理授权",
        },
        "already_archived": bool(row.get("is_deleted")),
        "_owner_user_id": int(row["owner_user_id"]),
    }


@router.get("/api/my-clients/{brand_id}/archive-preview")
async def preview_client_archive(brand_id: int, request: Request):
    user = _get_user(request)
    from db.connection import get_db
    with get_db() as conn:
        preview = _client_archive_preview_with_cursor(conn.cursor(), brand_id)
    if not user.get("is_admin") and preview["_owner_user_id"] != user["user_id"]:
        raise HTTPException(403, "无权归档此客户")
    preview.pop("_owner_user_id", None)
    return preview


@router.delete("/api/my-clients/{brand_id}")
async def delete_client(brand_id: int, request: Request, reason: str = ""):
    """软删除客户

    C.6 (CTO-15.18 · 2026-04-28):加 deleted_reason 参数 + 7 天回收站
    Q15 老板裁决:7 天可恢复 · 7 天后保留软删(DB 不真删)· admin 单独 button hard delete
    """
    user = _get_user(request)

    from db.connection import get_db
    organization_charge_ids: list[int] = []
    legacy_revoked_user_ids: list[int] = []
    with get_db() as conn:
        cur = conn.cursor()
        # 🔴 [工单 V5-A · Codex fix-of-fix2 P1-3] 品牌级序列化点 —— **本事务第一把锁**。
        #    下面那句 `UPDATE quotes … deleted_at=CURRENT_TIMESTAMP` 翻的正是
        #    `canonical_quote_id()` 的谓词(deleted_at IS NULL)。不拿这把锁的话,
        #    门户 token 轮换可以在它的 canonical 复核之后、写 token 之前被这里插进来,
        #    于是新签的 active token 指向一个刚被归档的报价。
        #    与建报价的三个 INSERT 入口、与轮换本身**同一把锁、同一个锁序**。
        from db.diagnosis_db import lock_brand_quote_serialization_point
        lock_brand_quote_serialization_point(cur, brand_id)
        # Establish a stable brand boundary before counting associations or
        # revoking access. New foreign-key children cannot race this archive.
        cur.execute("SELECT id FROM brands WHERE id=%s FOR UPDATE", (brand_id,))
        if not cur.fetchone():
            raise HTTPException(404, "客户不存在")
        preview = _client_archive_preview_with_cursor(cur, brand_id)
        owner_user_id = preview["_owner_user_id"]
        if not user.get("is_admin") and owner_user_id != user["user_id"]:
            raise HTTPException(403, "无权删除此客户")
        if preview["already_archived"]:
            preview.pop("_owner_user_id", None)
            return {"success": True, "archived": True, **preview}

        # Lock organization authority before artifact/brand rows.  Assignment
        # revocation, plan cancellation and the tombstone commit together; any
        # unstarted wallet reservation is made immediately recoverable before
        # this transaction can succeed.
        from services.organization_service import cascade_revoke_brand_with_cursor
        from services.organization_plans import cancel_brand_plans_for_soft_delete
        cascade_revoke_brand_with_cursor(
            cur,
            brand_id=brand_id,
            previous_owner_user_id=owner_user_id,
            request_id=getattr(request.state, "organization_request_id", None)
            or request.headers.get("X-Request-ID")
            or f"brand-soft-delete:{brand_id}",
            reason="brand soft deleted",
        )
        organization_charge_ids = cancel_brand_plans_for_soft_delete(
            cur,
            brand_id=brand_id,
            owner_user_id=owner_user_id,
            request_id=getattr(request.state, "organization_request_id", None)
            or request.headers.get("X-Request-ID")
            or f"brand-soft-delete:{brand_id}",
        )

        # organization cascade already removed member projection rows. Any
        # remaining rows are legacy access assignments and must be revoked in
        # this same tombstone transaction, with permission generation bumped.
        cur.execute(
            "SELECT user_id FROM user_clients WHERE brand_id=%s ORDER BY user_id FOR UPDATE",
            (brand_id,),
        )
        legacy_revoked_user_ids = [int(row["user_id"]) for row in cur.fetchall()]
        if legacy_revoked_user_ids:
            cur.execute("DELETE FROM user_clients WHERE brand_id=%s", (brand_id,))
            cur.execute(
                "UPDATE users SET permission_version=permission_version+1 WHERE id=ANY(%s)",
                (legacy_revoked_user_ids,),
            )

        # 级联软删除关联数据
        cur.execute(
            """UPDATE client_profiles
               SET is_deleted=1, deleted_at=CURRENT_TIMESTAMP,
                   archived_with_brand_at=CURRENT_TIMESTAMP
               WHERE brand_id=%s AND COALESCE(is_deleted,0)=0""",
            (brand_id,),
        )
        cur.execute(
            """UPDATE quotes
               SET status_before_archive=COALESCE(status_before_archive,status), status='archived',
                   deleted_at=CURRENT_TIMESTAMP, archive_reason=%s,
                   archived_with_brand_at=CURRENT_TIMESTAMP, archived_by_user_id=%s
               WHERE brand_id=%s AND deleted_at IS NULL""",
            (reason or "客户归档", user["user_id"], brand_id),
        )
        cur.execute(
            """UPDATE keyword_selection_sessions
               SET status_before_archive=COALESCE(status_before_archive,status), status='expired',
                   archived_at=CURRENT_TIMESTAMP, archived_with_brand_at=CURRENT_TIMESTAMP,
                   archived_by_user_id=%s
               WHERE brand_id=%s AND archived_at IS NULL
                 AND (quote_id IS NULL OR EXISTS (
                     SELECT 1 FROM quotes q
                     WHERE q.id=keyword_selection_sessions.quote_id
                       AND q.archived_with_brand_at IS NOT NULL
                 ))""",
            (user["user_id"], brand_id),
        )
        # [audit P2 2026-06-10] 移除请求热路径里的 DDL:旧版每次软删都跑 ALTER TABLE ADD COLUMN
        #   IF NOT EXISTS → 高频拿 ACCESS EXCLUSIVE 锁、且本函数在 get_db() 事务内(非 autocommit),
        #   一旦 ALTER 因并发/锁失败会污染整笔软删事务。列 is_deleted/deleted_at 已是 prod 既有
        #   (启动 migration 保障),这里直接 UPDATE,DDL 删掉。
        cur.execute(
            """UPDATE diagnosis_records
               SET is_deleted=TRUE, deleted_at=CURRENT_TIMESTAMP,
                   archived_with_brand_at=CURRENT_TIMESTAMP
               WHERE brand_id=%s AND COALESCE(is_deleted,FALSE)=FALSE""",
            (brand_id,),
        )
        # C.6:写 deleted_reason(brands.deleted_reason 字段在 A.2 schema 已加)
        cur.execute(
            """
            UPDATE brands
            SET is_deleted = TRUE, deleted_at = CURRENT_TIMESTAMP, deleted_reason = %s
            WHERE id = %s AND owner_user_id=%s
            """,
            (reason or "代理删除", brand_id, owner_user_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "客户归属已变化，请刷新后重试")

    if legacy_revoked_user_ids:
        from auth.perm_cache import invalidate_cache
        for revoked_user_id in legacy_revoked_user_ids:
            invalidate_cache(revoked_user_id)

    release_pending: list[int] = []
    if organization_charge_ids:
        from services.organization_billing import release_charge
        for charge_id in organization_charge_ids:
            try:
                await release_charge(
                    charge_link_id=charge_id,
                    reason="brand soft deleted before provider call",
                )
            except Exception as exc:
                release_pending.append(charge_id)
                import logging as _logging
                _logging.getLogger("GEO-Brand-API").warning(
                    "[soft-delete-brand] organization release deferred brand=%s charge=%s error_type=%s",
                    brand_id,
                    charge_id,
                    type(exc).__name__,
                )

    # CTO-15.23 2026-05-09 · 客户软删 → 取消该 brand 下所有监测订阅(防 daily 跑被删客户的 keyword)
    # 事务外调用 · 失败不阻塞软删主流程
    try:
        from db.monitoring_db import cancel_subscriptions_by_brand
        cancel_subscriptions_by_brand(brand_id, reason="brand_soft_deleted")
    except Exception as e:
        import logging as _logging
        _logging.getLogger("GEO-Brand-API").warning(
            f"[soft-delete-brand] cancel monitor subs failed brand={brand_id}: {e}"
        )

    preview.pop("_owner_user_id", None)
    return {
        "success": True,
        "organization_release_pending": release_pending,
        **preview,
    }


# ============================================================
# C.6 · 7 天回收站 endpoints(CTO-15.18 · 2026-04-28)
# ============================================================

@router.get("/api/my-clients/recycle-bin")
async def list_recycle_bin(request: Request):
    """列出 7 天内软删的客户(可恢复)

    Q15 老板裁决:7 天可恢复 · 7 天后保留软删(列表里不再显示 · 但 DB 不真删)
    admin 看全部用户的回收站 · 普通代理只看自己的
    """
    user = _get_user(request)
    is_admin = user.get("is_admin", False)
    user_id = user.get("user_id")

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 7 天窗口
        if is_admin:
            cur.execute(
                """
                SELECT b.id, b.name, b.brand_code, b.industry, b.deleted_at, b.deleted_reason,
                       b.owner_user_id, u.display_name AS owner_name
                FROM brands b
                LEFT JOIN users u ON u.id = b.owner_user_id
                WHERE b.is_deleted = TRUE
                  AND b.deleted_at >= NOW() - INTERVAL '7 days'
                ORDER BY b.deleted_at DESC
                """
            )
        else:
            cur.execute(
                """
                SELECT id, name, brand_code, industry, deleted_at, deleted_reason
                FROM brands
                WHERE is_deleted = TRUE
                  AND deleted_at >= NOW() - INTERVAL '7 days'
                  AND owner_user_id = %s
                ORDER BY deleted_at DESC
                """,
                (user_id,),
            )
        rows = cur.fetchall() or []
        items = [dict(r) for r in rows]
        # ISO 化时间
        for it in items:
            if it.get("deleted_at"):
                it["deleted_at"] = it["deleted_at"].isoformat()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"success": True, "items": items, "total": len(items)}


@router.post("/api/my-clients/{brand_id}/restore")
async def restore_deleted_client(brand_id: int, request: Request):
    """从回收站恢复软删的客户(C.6)

    限制:仅 7 天内可恢复 · 超过则需联系 admin 撤销 hard delete
    """
    user = _get_user(request)
    is_admin = user.get("is_admin", False)
    user_id = user.get("user_id")

    from db.connection import get_db
    with get_db() as conn:
        cur = conn.cursor()
        # 🔴 [工单 V5-A · Codex fix-of-fix2 P1-3] 品牌级序列化点 —— **本事务第一把锁**。
        #    下面那句 `UPDATE quotes … deleted_at=NULL` 翻的正是
        #    `canonical_quote_id()` 的谓词(deleted_at IS NULL)。不拿这把锁的话,
        #    门户 token 轮换可以在它的 canonical 复核之后、写 token 之前被这里插进来,
        #    于是恢复出来的报价可以在轮换复核之后突然变成 canonical,而她签的是另一个。
        #    与建报价的三个 INSERT 入口、与轮换本身**同一把锁、同一个锁序**。
        from db.diagnosis_db import lock_brand_quote_serialization_point
        lock_brand_quote_serialization_point(cur, brand_id)
        cur.execute(
            "SELECT owner_user_id, is_deleted, deleted_at FROM brands WHERE id = %s",
            (brand_id,),
        )
        brand = cur.fetchone()
        if not brand:
            raise HTTPException(404, "客户不存在")
        if not is_admin and brand.get("owner_user_id") != user_id:
            raise HTTPException(403, "无权恢复此客户")
        if not brand.get("is_deleted"):
            raise HTTPException(400, "该客户未删除 · 无需恢复")

        # 检查 7 天窗口
        deleted_at = brand.get("deleted_at")
        if deleted_at:
            from datetime import datetime as _dt, timedelta as _td
            now = _dt.now(deleted_at.tzinfo) if deleted_at.tzinfo else _dt.now()
            if now - deleted_at > _td(days=7):
                if not is_admin:
                    raise HTTPException(400, "已超过 7 天回收期 · 请联系 admin 撤销")

        cur.execute(
            "UPDATE brands SET is_deleted = FALSE, deleted_at = NULL, deleted_reason = NULL WHERE id = %s",
            (brand_id,),
        )
        # 关联数据也恢复
        cur.execute(
            """UPDATE client_profiles
               SET is_deleted=0, deleted_at=NULL, archived_with_brand_at=NULL
               WHERE brand_id=%s AND archived_with_brand_at IS NOT NULL""",
            (brand_id,),
        )
        cur.execute(
            """UPDATE diagnosis_records
               SET is_deleted=FALSE, deleted_at=NULL, archived_with_brand_at=NULL
               WHERE brand_id=%s AND archived_with_brand_at IS NOT NULL""",
            (brand_id,),
        )
        cur.execute(
            """UPDATE quotes
               SET status=COALESCE(NULLIF(status_before_archive,''),'draft'),
                   status_before_archive=NULL, deleted_at=NULL, archive_reason=NULL,
                   archived_with_brand_at=NULL, archived_by_user_id=NULL
               WHERE brand_id=%s AND archived_with_brand_at IS NOT NULL""",
            (brand_id,),
        )
        restored_quotes = cur.rowcount
        cur.execute(
            """UPDATE keyword_selection_sessions
               SET status=COALESCE(NULLIF(status_before_archive,''),'selecting'),
                   status_before_archive=NULL, archived_at=NULL,
                   archived_with_brand_at=NULL, archived_by_user_id=NULL
               WHERE brand_id=%s AND archived_with_brand_at IS NOT NULL""",
            (brand_id,),
        )
        restored_sessions = cur.rowcount

    return {
        "success": True,
        "brand_id": brand_id,
        "restored": {"quotes": restored_quotes, "selection_sessions": restored_sessions},
        "recovery_note": "员工客户分配未自动恢复，请按组织治理权限重新授权。",
    }


# ========== AI 智能填充 ==========

@router.post("/api/brand/auto-fill")
async def auto_fill_brand(req: AutoFillRequest, request: Request):
    """
    AI 智能填充品牌信息。
    短文本（<50字）→ 联网搜索公司信息
    长文本（≥50字）→ 直接用 LLM 从粘贴的资料中提取结构化信息
    """
    _user_obj = _get_user(request)

    text = req.text.strip()
    if not text:
        return {"success": False, "error": "请输入内容", "data": {}}

    # [2026-06-01 扣费链对齐 · 老板拍板 A] 此前角标显 brand_fill=40 但本端点 0 扣费(白嫖)· 现真扣 40
    # 模式 = 对齐 server.py /api/diagnosis/autofill:check 上限(余额不足 402)+ "成功才扣"(LLM 成功再 commit · 失败/空输入不扣)
    # 3 调用方(AiFillDialog 快速 / BasicBlock / M3 新增客户页)均显式用户点击 · 无自动触发 · 不静默扣费
    # ⚠️ 扣费链铁律:须 prod 真机 SQL 对账 point_transactions(成功 -40 brand_fill · 失败 0 · 余额不足 402)
    _bill_user_id = None
    if _user_obj and not _user_obj.get("is_admin"):
        _bill_user_id = _user_obj.get("user_id")
    if _bill_user_id:
        from middleware.billing import check_balance_only as _check_bf
        await _check_bf(_bill_user_id, "brand_fill")  # 余额不足 → 402 直接抛(预检不扣)

    async def _commit_brand_fill_charge():
        if _bill_user_id:
            try:
                from middleware.billing import deduct_points as _dp_bf
                await _dp_bf(_bill_user_id, "brand_fill")
            except Exception as _e:
                logger.error(f"[auto-fill] commit 扣费失败(数据已发放): {_e}")

    async def _finalize(result: dict) -> dict:
        # 成功才扣:result.success=True → commit brand_fill · 否则(失败/异常)不扣
        # [GEO-R6-CAN-004 P1] 防双扣:_search_and_fill 走 HTTP loopback 到
        # /api/diagnosis/autofill,内层已扣一次 brand_fill(转发同一用户 Authorization)。
        # 若结果带 _billed_via_loopback 标记 → 本层不再扣(否则同一操作扣两次 brand_fill)。
        # _extract_from_text 路径无 loopback → 无标记 → 由本层扣一次(正确)。
        _billed = isinstance(result, dict) and result.pop("_billed_via_loopback", False)
        if isinstance(result, dict) and result.get("success") and not _billed:
            await _commit_brand_fill_charge()
        return result

    structured_brand = (req.brand_name or "").strip()
    structured_industry = (req.industry or "").strip()
    structured_city = (req.city or "").strip()
    has_structured_context = any([structured_brand, structured_industry, structured_city])

    # M3 快录客户页:结构化上下文优先,避免短文本搜索把标签串当品牌名。
    if has_structured_context:
        normalized_text = _compose_autofill_context(
            text=text,
            brand_name=structured_brand,
            industry=structured_industry,
            city=structured_city,
        )
        # 用户粘贴了客户原话/资料/链接时,走长文本抽取,并用显式字段约束输出。
        if len(text) >= 50 or "参考链接:" in text or "\n" in text:
            result = await _extract_from_text(normalized_text)
        else:
            # 只填了客户名/城市/行业时,仍允许联网搜索,但 brand_name 只用客户名。
            result = await _search_and_fill(
                structured_brand or text,
                request,
                user_city=structured_city,
                user_industry=structured_industry,
            )
            if not result.get("success"):
                result = _fallback_fill_from_context(
                    brand_name=structured_brand or text,
                    industry=structured_industry,
                    city=structured_city,
                )
        if result.get("success"):
            data = result.get("data") or {}
            if structured_brand:
                data["brand_name"] = structured_brand
            if structured_industry:
                data["industry"] = structured_industry
            if structured_city:
                data["city"] = structured_city
            data["keywords"] = _sanitize_business_probe_keywords(
                data.get("keywords") or [],
                brand_name=data.get("brand_name") or structured_brand or text,
                industry=data.get("industry") or structured_industry,
                city=data.get("city") or structured_city,
            )
            result["data"] = data
        return await _finalize(result)

    # 长文本：直接 LLM 提取（用户粘贴的公司资料）
    if len(text) >= 50:
        result = await _extract_from_text(text)
        if result.get("success"):
            data = result.get("data") or {}
            data["keywords"] = _sanitize_business_probe_keywords(
                data.get("keywords") or [],
                brand_name=data.get("brand_name") or text,
                industry=data.get("industry") or "",
                city=data.get("city") or "",
            )
            result["data"] = data
        return await _finalize(result)

    # 短文本：联网搜索
    return await _finalize(await _search_and_fill(text, request))


def _compose_autofill_context(text: str, brand_name: str = "", industry: str = "", city: str = "") -> str:
    """给 LLM 的结构化上下文。显式字段是硬约束,原始文本只作补充。"""
    parts = []
    if brand_name:
        parts.append(f"客户名/品牌名:{brand_name}")
    if industry:
        parts.append(f"行业:{industry}")
    if city:
        parts.append(f"城市:{city}")
    raw = (text or "").strip()
    if raw:
        parts.append(f"代理补充资料:\n{raw}")
    return "\n".join(parts)


def _fallback_fill_from_context(brand_name: str, industry: str = "", city: str = "") -> dict:
    """搜索失败时也给代理一个可审阅的最低可用结果,不编造公司事实。"""
    data = {
        "brand_name": brand_name,
        "industry": industry,
        "city": city,
        "business": f"{city}{industry}服务"[:30] if (city or industry) else "",
        "target_users": "",
        "selling_points": "",
        "persona_positioning": "",
        "persona_tone": "",
        "products": [],
        "pain_points": [],
        "company_intro": "",
        "competitors": [],
        "keywords": _sanitize_business_probe_keywords([], brand_name, industry, city),
    }
    return {"success": True, "data": data}


def _sanitize_business_probe_keywords(
    keywords: list,
    brand_name: str = "",
    industry: str = "",
    city: str = "",
) -> list[str]:
    """业务调研词只保留客户会问的行业/本地/场景问题,剔除品牌名变体。"""
    brand = (brand_name or "").strip()
    banned_suffixes = ("怎么样", "靠谱吗", "口碑", "案例", "评价", "电话", "地址")
    cleaned: list[str] = []
    seen = set()
    for raw in keywords or []:
        kw = str(raw).strip()
        if not kw:
            continue
        # 只剔除典型品牌词变体,避免误删品牌名恰好等于行业词的极端情况。
        if brand and brand in kw and any(s in kw for s in banned_suffixes):
            continue
        if len(kw) > 40:
            kw = kw[:40]
        if kw and kw not in seen:
            seen.add(kw)
            cleaned.append(kw)
    if len(cleaned) >= 5:
        return cleaned[:10]

    ind = (industry or "").strip()
    ct = (city or "").strip()
    local_service = f"{ct}{ind}" if ct and ind else (ind or "这类服务")
    # [WO 2026-08-06 §2.2-2] 兜底池全部换成**提名型**(会让 AI 点名厂商的问法)。
    #
    # 原池里的「怎么选 / 多少钱 / 避坑 / 怎么收费 / 哪种方案适合我 / 口碑怎么判断」
    # 六条,AI 的回答全是方法论清单或价格区间 —— **一个厂商名都不会出现**。
    # 这些词是品牌档案的核心词,下游会被喂进诊断出题 prompt 当"关键词",
    # 于是 LLM 照着出一批测不出提及的题(553 的「服务商怎么选才不踩坑」即此形态)。
    # 出口(提名型闸)修了而兜底池还在产不合格形态 = 白修,brandq 包刚踩过同一坑。
    fallback = [
        f"{local_service}哪家靠谱",
        f"{local_service}哪家好",
        f"{local_service}推荐",
        f"{local_service}哪家性价比高",
        f"{local_service}口碑好的有哪几家",
        f"{local_service}排名",
    ]
    if ct and ind:
        fallback.append(f"{ct}{ind}公司有哪些")
    if ind:
        fallback.extend([f"{ind}哪家专业", f"{ind}服务商推荐几家"])
    for kw in fallback:
        if kw not in seen:
            seen.add(kw)
            cleaned.append(kw)
    return cleaned[:10]


async def _extract_from_text(text: str) -> dict:
    """从粘贴的长文本中用 LLM 提取品牌信息"""
    try:
        from services.llm.advisor_llm import advisor_generate
        import json as _json, re

        # 超长文本：取头部 + 尾部，保留关键信息
        max_chars = 4000
        if len(text) > max_chars:
            head = text[:2500]
            tail = text[-1500:]
            trimmed = f"{head}\n\n...(中间省略)...\n\n{tail}"
        else:
            trimmed = text

        prompt = f"""从以下文本中提取品牌/公司的结构化信息。

文本内容:
{trimmed}

请提取以下字段(没有的不要编造,留空即可),返回 JSON:
{{
    "brand_name": "品牌/公司名",
    "industry": "所属行业",
    "city": "所在城市",
    "business": "一句话业务描述(30字以内)",
    "target_users": "目标客户群体",
    "selling_points": "差异化卖点 / 核心卖点(50字以内)",
    "persona_positioning": "IP 人设定位(如:专业顾问型 / 亲和分享型 / 行业专家型 / 实战派老板型等,30字以内)",
    "persona_tone": "品牌语气调性(如:亲和自然 / 专业严谨 / 幽默风趣 / 犀利直接等,20字以内)",
    "products": ["产品/服务1", "产品/服务2"],
    "pain_points": ["客户痛点1", "客户痛点2"],
    "company_intro": "公司简介(100字以内)",
    "competitors": ["竞品1", "竞品2"],
    "keywords": ["业务调研探测词1(给 4 引擎测客户群体真实提问 · 不是品牌推广词)", "..."]
}}

⚠️ "keywords" 字段语义重定义(老板红线 2026-04-28 · 不可搞错):
  - 这里的 keywords 是 **业务调研探测词** · 给诊断 4 引擎(豆包/Kimi/DeepSeek/qwen3-max)
    模拟"客户群体在 AI 搜索时会问的话",看这家公司是否会被推荐
  - **不是**客户要推广的品牌词 / SEO 词(那是后面报价阶段的事)
  - **不是**"<品牌名> 怎么样 / 口碑 / 案例"这种品牌名变体词

  正确产出(8-15 个 · 模拟真实用户在 ChatGPT/豆包问的话 ·
  [SSOT geo-commercial-intent-governance-v1.0 §4.2] 每个问题都必须有
  品牌识别、推荐、比较、选择或转化价值 —— 答案会出现具体公司/服务商):
  - 行业核心提问 4-6 个:"如何选 GEO 服务" / "深圳家装公司哪家好" / "B2B SaaS 怎么选"
  - 供应商比较/价格问询 2-4 个:"GEO 优化服务商哪家靠谱" / "AI 搜索优化服务多少钱" / "社媒代运营哪家靠谱"
  - 场景选型查询 2-3 个:"小公司做 AI 推荐找哪家服务商" / "帮餐厅做抖音曝光的公司推荐"
  - 本地服务词(如有 city)1-2 个:"深圳哪里能做 GEO"

  禁止:
  - ❌ "全域上榜 怎么样" / "全域上榜 口碑" / "全域上榜 案例"(品牌名变体 · 推广词)
  - ❌ 纯知识/百科题:"GEO 和 SEO 区别" / "AI 搜索优化怎么做" / "行业趋势"
    (答案是知识不是公司 · 不会让 AI 推荐任何客户 · 不得占用探测名额)
  - ❌ 裸 SEO 短词片段:"GEO优化 排名" / "家装 推荐"(没有自然问句结构)
  - ✅ 自然的排名/推荐/对比/价格问句完全合法且核心:"深圳家装公司排名前十有哪些"
  - ✅ 要像 30-40 岁老板真的会在豆包/ChatGPT 里输入的那种话

只返回 JSON,不要 markdown 代码块。"""

        raw = await advisor_generate(prompt, temperature=0.3)
        if not raw:
            logger.warning("AI 文本提取返回空")
            return {"success": False, "error": "AI 未返回结果，请缩短内容重试", "data": {}}

        # 去掉 markdown 代码块
        cleaned = re.sub(r'```json?\s*', '', raw)
        cleaned = re.sub(r'```\s*', '', cleaned)
        match = re.search(r'\{[\s\S]*\}', cleaned)
        if not match:
            logger.warning(f"AI 返回无法解析为 JSON: {raw[:200]}")
            return {"success": False, "error": "AI 返回格式异常，请缩短内容重试", "data": {}}
        result = _json.loads(match.group())
        return {"success": True, "data": result}
    except Exception as e:
        import traceback
        logger.warning(f"AI 文本提取失败: {e}")
        traceback.print_exc()
        return {"success": False, "error": f"AI 分析失败: {str(e)[:80]}", "data": {}}


async def _search_and_fill(text: str, request, user_city: str = "", user_industry: str = "") -> dict:
    """短文本：联网搜索公司信息"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            auth_header = request.headers.get("authorization", "")
            resp = await client.post(
                "http://localhost:8000/api/diagnosis/autofill",
                json={
                    "brand_name": text,
                    "user_city": user_city or "",
                    "user_industry": user_industry or "",
                },
                headers={"Authorization": auth_header, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            autofill_data = resp.json()

        if not autofill_data.get("success"):
            return {"success": False, "error": "AI 搜索失败", "data": {}}

        d = autofill_data.get("data", {})
        result = {
            "brand_name": text,
            "industry": d.get("industry", ""),
            "city": d.get("clientLocation", ""),
            "business": d.get("additionalInfo", "")[:100] if d.get("additionalInfo") else "",
            "target_users": "",
            "selling_points": "",
            # IP 人设 Tab 字段 - 联网搜索难以直接拿到, 给空串由用户后续完善或调用长文本提取
            "persona_positioning": d.get("persona_positioning", ""),
            "persona_tone": d.get("persona_tone", ""),
            "products": [],
            "pain_points": [],
            "company_intro": d.get("additionalInfo", ""),
            "competitors": d.get("competitors", []),
            "keywords": [],
        }

        # Bug 1 修复：联网搜回的 additionalInfo 够长时再走一次 LLM 提取补齐 products/pain_points/target_users
        # 原因：底层 /api/diagnosis/autofill 不返回这 3 字段，用户只填公司名会看到空
        # [CTO-13.3 2026-04-19 R8] 阈值 100 → 30（哪怕只搜回 1 行简介也值得二次提取），
        #   合并字段补 competitors（底层可能返 [] 或漏漏）+ target_users，
        #   并记诊断日志让部署后能看到实际 additionalInfo 长度
        additional = d.get("additionalInfo", "") or ""
        logger.info(f"[auto-fill] brand={text} additional_len={len(additional)} "
                    f"raw_pain_points={len(result['pain_points'])} raw_competitors={len(result['competitors'])}")
        if len(additional) > 30:
            try:
                extra = await _extract_from_text(additional)
                if extra.get("success"):
                    ed = extra.get("data", {}) or {}
                    if not result["products"] and ed.get("products"):
                        result["products"] = ed["products"]
                    if not result["pain_points"] and ed.get("pain_points"):
                        result["pain_points"] = ed["pain_points"]
                    if not result["target_users"] and ed.get("target_users"):
                        result["target_users"] = ed["target_users"]
                    if not result["competitors"] and ed.get("competitors"):
                        result["competitors"] = ed["competitors"]
                    if not result["selling_points"] and ed.get("selling_points"):
                        result["selling_points"] = ed["selling_points"]
                    if not result["keywords"] and ed.get("keywords"):
                        result["keywords"] = ed["keywords"]
            except Exception as e:
                logger.warning(f"AI 联网搜索后二次提取失败（不阻断主流程）: {e}")

        # F+ 兜底关键词推断(老板红线 2026-04-28 重定义):
        # keywords 是 **业务调研探测词** · 给 4 引擎测客户群体真实提问
        # 不是品牌推广词 · 不再拼"<品牌名> 怎么样 / 口碑 / 案例"那些 SEO 词
        # 兜底从 industry / city / business 派生 8-10 个客户群体提问词
        if not result.get("keywords"):
            result["keywords"] = _sanitize_business_probe_keywords(
                [],
                brand_name=text,
                industry=result.get("industry") or "",
                city=result.get("city") or "",
            )
            logger.info(f"[auto-fill] keywords 兜底模板生成 count={len(result['keywords'])}")
        else:
            result["keywords"] = _sanitize_business_probe_keywords(
                result.get("keywords") or [],
                brand_name=text,
                industry=result.get("industry") or "",
                city=result.get("city") or "",
            )

        # [CTO-13.3 2026-04-19 R8] 兜底痛点推断：pain_points 仍为空且 business/industry 有值时，
        # 让 LLM 基于业务场景推断 3 条典型客户痛点 (而非从官方资料抠 · 解决老板截图"痛点永远空"问题)
        if not result["pain_points"] and (result["business"] or result["industry"]):
            try:
                from services.llm.advisor_llm import advisor_generate
                import json as _json, re
                ctx_parts = []
                if result.get("industry"): ctx_parts.append(f"行业：{result['industry']}")
                if result.get("business"): ctx_parts.append(f"业务：{result['business']}")
                if result.get("target_users"): ctx_parts.append(f"目标客户：{result['target_users']}")
                ctx_block = "\n".join(ctx_parts)
                guess_prompt = (
                    f"以下是一家公司的基本信息：\n{ctx_block}\n\n"
                    f"请推断这家公司的目标客户最可能面临的 3 个核心痛点（每条 10-20 字，具体且可被营销文案呼应）。\n"
                    "只返回 JSON 数组，不要 markdown：\n"
                    '["痛点1", "痛点2", "痛点3"]'
                )
                raw = await advisor_generate(guess_prompt, temperature=0.4)
                if raw:
                    cleaned = re.sub(r'```json?\s*', '', raw)
                    cleaned = re.sub(r'```\s*', '', cleaned)
                    m = re.search(r'\[[\s\S]*\]', cleaned)
                    if m:
                        guessed = _json.loads(m.group())
                        if isinstance(guessed, list) and guessed:
                            result["pain_points"] = [str(x).strip() for x in guessed if str(x).strip()][:5]
                            logger.info(f"[auto-fill] pain_points 兜底推断成功 count={len(result['pain_points'])}")
            except Exception as e:
                logger.warning(f"[auto-fill] 痛点兜底推断失败（不阻断）: {e}")

        # [GEO-R6-CAN-004 P1] 标记:本函数经 loopback 到 /api/diagnosis/autofill 已扣一次 brand_fill,
        # _finalize 见此标记则不再重复扣费(防同一操作双扣 brand_fill)。
        return {"success": True, "data": result, "_billed_via_loopback": True}

    except Exception as e:
        logger.warning(f"AI 联网填充失败: {e}")
        return {"success": False, "error": "AI 分析失败，请手动填写", "data": {}}
