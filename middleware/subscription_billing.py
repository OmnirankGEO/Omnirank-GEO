"""
Social Studio 订阅扣费模块 V3.1

来源: docs/AI-CONTEXT/SOCIAL_STUDIO_PRICING_V3_1_EXECUTION_2026-05-10.md
计划: .planning/phases/07-social-studio-subscription/PLAN.md B02

V3.1 vs V2:
  - 不再扣 user_wallets.subscription_points(V2 设计废弃)
  - 改为扣 user_social_entitlements 13 维 quota
  - 新增 cache 命中规则(0 扣权益,但写 usage_events)
  - 新增 usage_events 流水写入(P0-4)
  - 新增 clawback API
  - SOCIAL_FEATURES 白名单约束(防扣到 GEO)

红线:
  - 本模块不修改 middleware/billing.py(CLAUDE.md 红线核心)
  - 不动 db/wallet_db.py 主体
  - 不动 db/connection.py
"""

import hashlib
import json
import logging
import shortuuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from db.connection import get_connection

logger = logging.getLogger("GEO-SubBilling-V31")


# ==========================================================================
# Feature → entitlement 维度映射(13 维 SSOT)
# ==========================================================================

# 仅 social_* feature 可扣 entitlements,防跨板块扣到 GEO
SOCIAL_FEATURES = {
    # Social Studio 现有 feature_pricing(对齐 docker exec omnirank-db psql 查询结果)
    "social_diagnosis": "review",         # 社媒专项诊断
    "topic_gen": "monthly_plan",           # 选题生成
    "script_gen": "pro_write",             # 完整脚本生成
    "video_framework": "video_breakdown",  # 视频框架提取
    "single_video": "video_breakdown",     # 单视频采集+分析
    "hook_script_combo": "pro_write",      # 开篇+文案一套
    "profile_polish": "light_chat",        # 档案字段润色
    # V3.1 新增 feature
    "light_chat": "light_chat",
    "super_write": "super_write",
    "web_search": "web_search",
    "video_asr": "video_minutes",          # 按分钟扣
    "rewrite": "rewrite",
    "author_breakdown": "author_breakdown",
    "review": "review",
    "team_profile": "team_profile",
    # 2026-05-12 老板 P0 "一次性修复":Social Studio 业务代码全接入 V3.1 entitlements
    # 之前 _bill/_bill_ctx 直接走 deduct_points 绕过 entitlements,月卡配额 0 消耗,白买月卡
    "hook_gen": "pro_write",               # 开篇生成 = 写稿类(对齐 hook_script_combo)
    "deep_analyze": "review",              # 深度解析 = 数据复盘类
    "industry_brief_rerun": "review",      # 行业简报重跑 = 数据复盘类
    "content_review": "review",            # 内容复盘 = 数据复盘类(明示)
    "comment_gen": "light_chat",           # 评论生成 = 轻量对话
    "dm_gen": "light_chat",                # 私信生成 = 轻量对话
    "scenario_gen": "light_chat",          # 场景话术 = 轻量对话
    "team_analysis": "team_profile",       # 团队分析 = 团队画像
    "ai_coach": "light_chat",              # AI 顾问对话(advisor_api)= 轻量对话(26 points)
}


# B21 修: V3.1 新 feature_code 还没在 feature_pricing 表注册,
# fallback 到 paid 时如果直接传 'rewrite' 给 billing.deduct_points 会 ValueError.
# 映射到现有 feature_pricing 表里的 feature_code (avoid migration changes)
BILLING_FALLBACK_CODE_MAP = {
    # V3.1 feature → 现有 feature_pricing.feature_code
    "rewrite": "rewrite_gen",          # 650 points 一致
    "light_chat": "profile_polish",    # v1.7.6.3 SSOT: profile_polish=5 (轻量档)
    "super_write": "hook_script_combo",# v1.7.6.3 SSOT: hook_script_combo=80 (超级/组合档)
    # v1.7.6.3.1 P0(Codex 审核 2026-05-24 抓到):web_search → web_search 自查表(SSOT 联网 10)
    # 旧映射 web_search → find_trending=0(免费) · 让 web_search 超量 fallback 仍 0 扣 · 跟 SSOT 冲突
    # find_trending=0 保留(产品要免费"找热点")· 不再承接 web_search 计费
    "web_search": "web_search",        # 10 points (联网档 · SSOT)
    # v1.7.6.3 P0 SSOT 真根治(2026-05-24 老板审核)· video_asr 暗雷
    # 旧映射 video_asr→single_video 在 single_video=390 后会让 video_asr 也变 390/分钟
    # 必须断开映射 · video_asr 自己有独立 40/分钟 价(seed + migration_015 已加)
    "video_asr": "video_asr",          # 40/分钟 (按分钟 × 40)
    "review": "content_review",        # v1.7.6.3 SSOT: content_review=390 (拆视频短档)
    "team_profile": "team_portrait",   # v1.7.6.3 SSOT: team_portrait=1950 (拆博主短档)
    # 这些 V3.1 feature_code 已存在于 feature_pricing 表,直接复用:
    "social_diagnosis": "social_diagnosis",
    "topic_gen": "topic_gen",
    "script_gen": "script_gen",
    "video_framework": "video_framework",
    "single_video": "single_video",
    "hook_script_combo": "hook_script_combo",
    "profile_polish": "profile_polish",
    "author_breakdown": "author_breakdown",
    # 2026-05-12 一次性修复扩展(实证 feature_pricing 表 · 53 rows):
    "hook_gen": "hook_gen",                         # 260 points 表内存在
    "deep_analyze": "deep_analyze",                 # 500 points 表内存在
    "industry_brief_rerun": "industry_brief_rerun", # 130 points 表内存在
    "content_review": "content_review",             # 260 points 表内存在
    "comment_gen": "comment_gen",                   # 26 points 表内存在
    "dm_gen": "dm_gen",                             # 26 points 表内存在
    "scenario_gen": "scenario_gen",                 # 26 points 表内存在
    "team_analysis": "team_portrait",               # 1300 points 表内是 team_portrait(同含义)
}


# Cache 复用规则(0 扣权益,写 usage_events.cache_hit=true)
CACHE_TTL_HOURS = {
    "single_video": 24,           # 同 video_url 24h
    "video_framework": 24,
    "author_breakdown": 24 * 7,   # 同 blogger_uid+depth 7d
    "video_asr": None,            # 永久(audio_md5)
    "topic_gen": 24 * 7,          # profile+season_week 7d
}

# 升档建议(超量弹窗)
UPGRADE_PATH = {
    "free": ("personal", 49),
    "personal": ("growth", 99),
    "growth": ("agency", 299),
    "agency": ("partner", 599),
    "partner": (None, None),
}


# ==========================================================================
# 查询当前 active 订阅 + entitlements
# ==========================================================================

def get_active_subscription_full(user_id: int) -> Optional[Dict[str, Any]]:
    """查 active 订阅 + 套餐 SSOT + 当前 entitlements 周期(全合一)"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                s.id AS subscription_id,
                s.plan_id, s.started_at, s.expires_at,
                s.auto_renew, s.is_first_month, s.status,
                s.referrer_user_id, s.indirect_referrer_user_id,
                s.price_locked_yuan,
                p.display_name, p.monthly_yuan,
                e.id AS entitlement_id,
                e.period_start, e.period_end,
                e.light_chat_limit, e.light_chat_used,
                e.pro_write_limit, e.pro_write_used,
                e.super_write_limit, e.super_write_used,
                e.web_search_limit, e.web_search_used,
                e.video_minutes_limit, e.video_minutes_used,
                e.video_single_minutes_cap,
                e.rewrite_limit, e.rewrite_used,
                e.video_breakdown_limit, e.video_breakdown_used,
                e.author_breakdown_limit, e.author_breakdown_used,
                e.review_limit, e.review_used,
                e.monthly_plan_limit, e.monthly_plan_used,
                e.team_profile_limit, e.team_profile_used,
                p.quota_workspace, p.quota_knowledge_mb
            FROM user_social_subscriptions s
            JOIN subscription_plans p ON s.plan_id = p.plan_id
            LEFT JOIN user_social_entitlements e
              ON e.subscription_id = s.id AND e.status = 'active'
              AND e.period_start <= CURRENT_TIMESTAMP AND e.period_end > CURRENT_TIMESTAMP
            WHERE s.user_id = %s AND s.status = 'active'
              AND s.expires_at > CURRENT_TIMESTAMP
            ORDER BY s.expires_at DESC
            LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ==========================================================================
# Cache 命中检查
# ==========================================================================

def _make_cache_key(feature_code: str, ctx: Dict[str, Any]) -> Optional[str]:
    """根据 feature 生成 cache key,None 表示不可缓存"""
    ttl = CACHE_TTL_HOURS.get(feature_code)
    if ttl is None and feature_code != "video_asr":
        return None

    if feature_code in ("single_video", "video_framework"):
        url = ctx.get("video_url") or ctx.get("url")
        if not url:
            return None
        return f"{feature_code}:{hashlib.md5(url.encode()).hexdigest()}"

    if feature_code == "author_breakdown":
        uid = ctx.get("blogger_uid")
        depth = ctx.get("depth", "lite")
        if not uid:
            return None
        return f"author_breakdown:{uid}:{depth}"

    if feature_code == "video_asr":
        audio_md5 = ctx.get("audio_md5")
        if not audio_md5:
            return None
        return f"video_asr:{audio_md5}"

    if feature_code == "topic_gen":
        profile_id = ctx.get("profile_id")
        if not profile_id:
            return None
        season_week = datetime.utcnow().strftime("%Y-W%U")
        return f"topic_gen:{profile_id}:{season_week}"

    return None


def check_cache_hit(feature_code: str, cache_key: str) -> Optional[Dict[str, Any]]:
    """查 usage_events 找最近 TTL 内的 cache_hit 或非 cache 但同 key 的成功扣费记录,返其 metadata 复用"""
    if not cache_key:
        return None
    ttl_hours = CACHE_TTL_HOURS.get(feature_code)
    if ttl_hours is None and feature_code != "video_asr":
        return None

    conn = get_connection()
    try:
        cur = conn.cursor()
        if feature_code == "video_asr":
            # 永久缓存
            cur.execute("""
                SELECT metadata FROM subscription_usage_events
                WHERE cache_key = %s
                ORDER BY id DESC LIMIT 1
            """, (cache_key,))
        else:
            cutoff = datetime.utcnow() - timedelta(hours=ttl_hours)
            cur.execute("""
                SELECT metadata FROM subscription_usage_events
                WHERE cache_key = %s AND created_at >= %s
                ORDER BY id DESC LIMIT 1
            """, (cache_key, cutoff))
        row = cur.fetchone()
        if row and row.get("metadata"):
            return row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
        return None
    finally:
        conn.close()


# ==========================================================================
# 超量预检(前端弹窗用)
# ==========================================================================

def subscription_overrun_check(user_id: int, feature_code: str,
                                 video_minutes: int = 0) -> Dict[str, Any]:
    """检查权益是否够,返超量提示供前端弹窗

    返回:
        {
            "in_quota": bool,        # True = 走 entitlement / False = 超量
            "quota_type": str,
            "used": int,
            "limit": int,
            "would_consume": int,
            "fallback_points": int,  # 超量后扣多少积分
            "fallback_yuan": float,
            "addon_pack": dict,      # 推荐扩展包
            "upgrade_to": str,       # 推荐升级套餐
            "upgrade_yuan": float,
            "current_plan": str,
        }
    """
    if feature_code not in SOCIAL_FEATURES:
        return {"in_quota": True, "quota_type": None, "noop": True}

    quota_type = SOCIAL_FEATURES[feature_code]
    sub = get_active_subscription_full(user_id)
    if not sub:
        return {
            "in_quota": False,
            "quota_type": quota_type,
            "no_active_subscription": True,
            "fallback_points": _feature_fallback_points(feature_code, video_minutes),
            "fallback_yuan": round(_feature_fallback_points(feature_code, video_minutes) / 130.0, 2),
            "current_plan": "free",
            "upgrade_to": "personal",
            "upgrade_yuan": 49,
        }

    limit_field = f"{quota_type}_limit"
    used_field = f"{quota_type}_used"
    limit = int(sub.get(limit_field, 0) or 0)
    used = int(sub.get(used_field, 0) or 0)
    would_consume = video_minutes if quota_type == "video_minutes" else 1

    if limit == -1:
        return {"in_quota": True, "quota_type": quota_type, "limit": -1}

    in_quota = (used + would_consume) <= limit
    fallback_points = _feature_fallback_points(feature_code, video_minutes)
    plan_id = sub["plan_id"]
    upgrade_to, upgrade_yuan = UPGRADE_PATH.get(plan_id, (None, None))
    addon_pack = _suggest_addon_pack(feature_code)

    return {
        "in_quota": in_quota,
        "quota_type": quota_type,
        "used": used,
        "limit": limit,
        "would_consume": would_consume,
        "fallback_points": fallback_points,
        "fallback_yuan": round(fallback_points / 130.0, 2),
        "addon_pack": addon_pack,
        "upgrade_to": upgrade_to,
        "upgrade_yuan": upgrade_yuan,
        "current_plan": plan_id,
    }


def _feature_fallback_points(feature_code: str, video_minutes: int = 0) -> int:
    """超量后从积分扣多少

    B22 修: 实时查 feature_pricing 表,保证 overrun hint 显示的金额 == 实际扣的金额。
    避免前端报"5 积分"实际扣 30 积分的不一致。
    """
    billing_code = BILLING_FALLBACK_CODE_MAP.get(feature_code, feature_code)
    base_points = None
    try:
        from db.wallet_db import get_feature_pricing
        pricing = get_feature_pricing(billing_code)
        base_points = int(pricing["cost_points"])
    except Exception:
        # 兜底硬编码(feature_pricing 表不可用时)
        # v1.7.6.3 P0 SSOT align (2026-05-24 老板拍板 · migration_015)
        # 配套 db/wallet_db.py PRICING_DATA + frontend/.../PricingPage.tsx 同步
        hardcoded = {
            # 轻量 5
            "light_chat": 5, "profile_polish": 5, "ai_coach": 5,
            "comment_gen": 5, "dm_gen": 5, "scenario_gen": 5,
            "corpus_text": 5, "corpus_upload": 5, "task_route": 5,
            # 专业 40
            "pro_write": 40, "script_gen": 40, "hook_gen": 40,
            "brand_fill": 40, "personality_refresh": 40, "industry_brief_rerun": 40,
            # 超级/组合 80
            "super_write": 80, "hook_script_combo": 80, "topic_gen": 80,
            # 联网 10 (v1.7.6.3.1 P0 · Codex 审核 · 之前漏写 0 · SSOT 联网档 = 10)
            "web_search": 10,
            # 视频分钟 40/min
            "video_asr": 40,  # 按分钟 × 40 · 见下 video_asr if 分支
            # 拆视频短 390
            "single_video": 390, "video_framework": 390,
            "learn_viral": 390, "content_review": 390, "review": 390,
            # 拆博主短 1950
            "author_breakdown": 1950, "team_portrait": 1950, "team_profile": 1950,
            # 保持(已对 SSOT)
            "rewrite": 650, "rewrite_gen": 650,
            "social_diagnosis": 650,
            "deep_analyze": 500,
            # 老 V3.1 残留 · 不在 SSOT 9 档 · 老板未拍 · 保留
            "monthly_plan": 260,
        }
        # [WO_240] 🔴 这里有**两件**要出声的事,它们的后果不同,不许压成一种:
        #   ① 走进了这个 except —— 取价整个没走通,下面用的是**一整份影子目录**
        #      (30+ 键,与 feature_pricing 各写一份;它们一起漂就一起错)。
        #   ② 连影子目录里都没有这个 feature —— 那就是 `100`,一个**谁也没定过的价**。
        #      「没见过的功能一律按 100 收」不是兜底,是**对着一个未知功能开价**。
        from services.fallback_observability import fired as _fb_fired
        if feature_code in hardcoded:
            _fb_fired(feature=feature_code,
                      where="middleware/subscription_billing.py:_feature_fallback_points",
                      key="cost_points", used=hardcoded[feature_code],
                      reason="lookup_raised", detail="shadow_catalog")
        else:
            _fb_fired(feature=feature_code,
                      where="middleware/subscription_billing.py:_feature_fallback_points",
                      key="cost_points", used=100,
                      reason="unknown_feature",
                      detail="影子目录里也没有这个 feature_code")
        base_points = hardcoded.get(feature_code, 100)

    # video_asr 按分钟数累加(基础价就是 1 分钟,超时按倍数算)
    if feature_code == "video_asr" and video_minutes > 1:
        return base_points * video_minutes
    return base_points


def _suggest_addon_pack(feature_code: str) -> Optional[Dict[str, Any]]:
    if feature_code == "video_asr":
        return {"name": "视频时长包", "yuan": 49, "unit": "100 分钟"}
    if feature_code == "author_breakdown":
        return {"name": "拆博主 Lite 包", "yuan": 99, "unit": "5 次 Lite"}
    if feature_code in ("single_video", "video_framework"):
        return {"name": "拆博主 Lite 包", "yuan": 99, "unit": "5 次 Lite"}
    return None


# ==========================================================================
# 扣权益(核心)
# ==========================================================================

async def charge_subscription_entitlement(
    user_id: int,
    feature_code: str,
    video_minutes: int = 0,
    request_id: Optional[str] = None,
    cache_ctx: Optional[Dict[str, Any]] = None,
    brand_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
    fallback_to_points: bool = False,
) -> Dict[str, Any]:
    """主扣费入口

    优先级:
      1. 检查 cache 命中 → 0 扣权益,返 cache_hit=True
      2. 检查 entitlement 够不够 → 扣 entitlement counter,写 usage_events
      3. 不够 + fallback_to_points=True → 走 billing.charge_on_success(扣 paid/bonus/commission)
      4. 不够 + fallback_to_points=False → 抛 InsufficientQuotaError

    返回:
        {
            "ok": True,
            "from": "cache" | "entitlement" | "fallback_points" | "free",
            "deducted_amount": int,
            "request_id": str,
            "cache_hit": bool,
            "metadata_replay": dict | None,  # cache 命中时返复用结果
        }
    """
    if feature_code not in SOCIAL_FEATURES:
        raise ValueError(
            f"feature {feature_code} 不在 SOCIAL_FEATURES 白名单,"
            f"防跨板块扣到 GEO。请走 middleware.billing.charge_on_success"
        )

    request_id = request_id or f"REQ-{shortuuid.uuid()[:10]}"
    metadata = metadata or {}
    cache_ctx = cache_ctx or {}
    # [BUG-P3] 预初始化 used/limit:无活跃订阅(sub=None)+ fallback_to_points=False 时,
    # used/limit 仅在下方 `if sub:` 块内赋值,末尾 InsufficientQuotaError 的 f-string 引用
    # 会 UnboundLocalError → 把"配额不足"业务错变成 500(且误导上层补偿逻辑)。
    used = 0
    limit = 0

    # 1. cache 命中
    cache_key = _make_cache_key(feature_code, cache_ctx)
    if cache_key:
        cached = check_cache_hit(feature_code, cache_key)
        if cached:
            _write_usage_event(
                user_id=user_id,
                subscription_id=None,
                entitlement_id=None,
                request_id=request_id,
                feature_code=feature_code,
                consumed_quantity=0,
                consumed_unit="cache",
                cache_hit=True,
                cache_key=cache_key,
                deduction_source="cache_hit_free",
                deduction_amount=0,
                brand_id=brand_id,
                metadata={"cache_replay": True, **metadata},
            )
            return {
                "ok": True,
                "from": "cache",
                "deducted_amount": 0,
                "request_id": request_id,
                "cache_hit": True,
                "metadata_replay": cached,
            }

    # 2. entitlement 扣减
    quota_type = SOCIAL_FEATURES[feature_code]
    consume = video_minutes if quota_type == "video_minutes" else 1

    sub = get_active_subscription_full(user_id)
    if sub:
        # 单条视频上限 cap(视频类必须先检查)
        if quota_type == "video_minutes":
            single_cap = int(sub.get("video_single_minutes_cap") or 0)
            if single_cap > 0 and video_minutes > single_cap:
                raise QuotaExceededError(
                    f"单条视频 {video_minutes} 分钟超出本套餐上限 {single_cap} 分钟,"
                    f"请压短或升级到代运营版"
                )

        limit = int(sub.get(f"{quota_type}_limit") or 0)
        used = int(sub.get(f"{quota_type}_used") or 0)

        if limit == -1 or (used + consume) <= limit:
            # Bug 5 修(Codex Round 3): 扣权益 + 写 usage_event 同事务,fail-closed
            ok = _consume_entitlement_with_event(
                entitlement_id=sub["entitlement_id"],
                quota_type=quota_type,
                amount=consume,
                user_id=user_id,
                subscription_id=sub["subscription_id"],
                request_id=request_id,
                feature_code=feature_code,
                consumed_unit=("minutes" if quota_type == "video_minutes" else "count"),
                cache_key=cache_key,
                brand_id=brand_id,
                metadata=metadata,
            )
            if ok:
                return {
                    "ok": True,
                    "from": "entitlement",
                    "deducted_amount": consume,
                    "request_id": request_id,
                    "cache_hit": False,
                }

    # 3. fallback to points(调 billing.deduct_points · 4 轨钱包按偏好扣)
    # B21 修: V3.1 feature_code 'rewrite' 等不在 feature_pricing 表, 用映射兜底
    if fallback_to_points:
        try:
            from middleware.billing import deduct_points
        except ImportError:
            raise InsufficientQuotaError("billing 模块不可用,无法 fallback")

        fallback_points = _feature_fallback_points(feature_code, video_minutes)
        # B21 映射到现有 feature_pricing 中的 feature_code(避免 ValueError: 未知 feature)
        billing_feature_code = BILLING_FALLBACK_CODE_MAP.get(feature_code, feature_code)
        # B22 修: deduct_points 内部 total = pricing.cost_points + extra_cost
        # 我们的 fallback_points 已经是实时查 feature_pricing 算的, 所以 extra_cost=0,
        # 让 deduct_points 用 base price (避免重复计 → 扣双倍)
        # video_asr 多分钟需 extra_cost = (minutes-1) * base
        extra = 0
        if feature_code == "video_asr" and video_minutes > 1:
            try:
                from db.wallet_db import get_feature_pricing
                base = int(get_feature_pricing(billing_feature_code)["cost_points"])
                extra = base * (video_minutes - 1)
            except Exception as _fb_exc:
                # [WO_240] 🔴 与 `server.py:_bill_feature_ctx` **同一个形状、同一个后果**:
                #   默认 0 在这里不是恒等元,是「多出来的分钟不收钱」——
                #   `video_minutes` 份**只收一份**,静默少收。
                #   (这一处是 Review 复核时指出的:我第一轮的分母按**文件**划,
                #    看不见两个文件以外的同形缺陷。分母要按**后果**划。)
                from services.fallback_observability import raised as _fb_raised
                _fb_raised(feature=feature_code,
                           where="middleware/subscription_billing.py:charge_subscription_entitlement",
                           key="cost_points", used=0, exc=_fb_exc)
                extra = 0

        result = await deduct_points(
            user_id=user_id,
            feature_code=billing_feature_code,
            brand_id=brand_id,
            extra_cost=extra,
        )
        _write_usage_event(
            user_id=user_id,
            subscription_id=sub["subscription_id"] if sub else None,
            entitlement_id=None,
            request_id=request_id,
            feature_code=feature_code,
            consumed_quantity=consume,
            consumed_unit=("minutes" if quota_type == "video_minutes" else "count"),
            cache_hit=False,
            cache_key=cache_key,
            deduction_source="paid_fallback",
            deduction_amount=fallback_points,
            brand_id=brand_id,
            metadata={"fallback_reason": "entitlement_exhausted", **metadata},
        )
        return {
            "ok": True,
            "from": "fallback_points",
            "deducted_amount": fallback_points,
            "request_id": request_id,
            "cache_hit": False,
            "fallback_detail": result,
        }

    # 4. 不允许 fallback → 抛错
    raise InsufficientQuotaError(
        f"feature {feature_code} 配额已用满({used}/{limit}),"
        f"请升级套餐或确认从积分扣 {_feature_fallback_points(feature_code, video_minutes)} 积分"
    )


def _consume_entitlement(entitlement_id: int, quota_type: str, amount: int) -> bool:
    """扣减 entitlement counter(原子操作 + 校验)

    保留独立 API 供 release / 兜底场景使用。
    主扣费路径走 _consume_entitlement_with_event 同事务(Bug 5 修)。
    """
    field_used = f"{quota_type}_used"
    field_limit = f"{quota_type}_limit"

    conn = get_connection()
    try:
        conn.autocommit = False
        cur = conn.cursor()
        # 原子扣减 + 校验不超 limit(-1 表示不限)
        cur.execute(f"""
            UPDATE user_social_entitlements
            SET {field_used} = {field_used} + %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND status = 'active'
              AND ({field_limit} = -1 OR {field_used} + %s <= {field_limit})
            RETURNING {field_used}, {field_limit}
        """, (amount, entitlement_id, amount))
        row = cur.fetchone()
        conn.commit()
        return bool(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _consume_entitlement_with_event(
    entitlement_id: int,
    quota_type: str,
    amount: int,
    *,
    user_id: int,
    subscription_id: int,
    request_id: str,
    feature_code: str,
    consumed_unit: str,
    cache_key: Optional[str],
    brand_id: Optional[int],
    metadata: Optional[Dict[str, Any]],
) -> bool:
    """Bug 5 修(Codex Round 3): 扣 entitlement + 写 usage_event 同事务原子提交

    旧实现两个独立 conn → 扣权益 commit 成功后 event 写失败只 warning,
    结果"扣了权益但账本缺记" 账务黑洞 + reconciliation 也扫不到(权益已扣)。

    新实现:扣 entitlement RETURNING + INSERT usage event 在同 conn 一次 commit
    任意环节失败整体 rollback,fail-closed(返 False,上层判失败抛 QuotaExceeded
    或 fallback paid),用户体验最坏=偶发重试,绝不出现"扣了钱没账本"。
    """
    field_used = f"{quota_type}_used"
    field_limit = f"{quota_type}_limit"

    conn = get_connection()
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute(f"""
            UPDATE user_social_entitlements
            SET {field_used} = {field_used} + %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND status = 'active'
              AND ({field_limit} = -1 OR {field_used} + %s <= {field_limit})
            RETURNING {field_used}, {field_limit}
        """, (amount, entitlement_id, amount))
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return False

        # 同事务写 usage event
        _write_usage_event_inline(
            cur,
            user_id=user_id,
            subscription_id=subscription_id,
            entitlement_id=entitlement_id,
            request_id=request_id,
            feature_code=feature_code,
            consumed_quantity=amount,
            consumed_unit=consumed_unit,
            cache_hit=False,
            cache_key=cache_key,
            deduction_source="entitlement",
            deduction_amount=0,
            brand_id=brand_id,
            metadata=metadata,
        )

        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_subscription_entitlement(
    user_id: int,
    feature_code: str,
    video_minutes: int = 0,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """失败回滚 entitlement counter(used -= n,不能 < 0)"""
    if feature_code not in SOCIAL_FEATURES:
        return {"ok": True, "noop": True}

    quota_type = SOCIAL_FEATURES[feature_code]
    consume = video_minutes if quota_type == "video_minutes" else 1

    sub = get_active_subscription_full(user_id)
    if not sub or not sub.get("entitlement_id"):
        return {"ok": True, "noop": True}

    field_used = f"{quota_type}_used"
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            UPDATE user_social_entitlements
            SET {field_used} = GREATEST({field_used} - %s, 0),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING {field_used}
        """, (consume, sub["entitlement_id"]))
        cur.fetchone()  # consume RETURNING
        conn.commit()
        # 写 release event 便于审计
        _write_usage_event(
            user_id=user_id,
            subscription_id=sub["subscription_id"],
            entitlement_id=sub["entitlement_id"],
            request_id=request_id or f"RELEASE-{shortuuid.uuid()[:8]}",
            feature_code=feature_code,
            consumed_quantity=-consume,
            consumed_unit="release",
            cache_hit=False,
            cache_key=None,
            deduction_source="release",
            deduction_amount=0,
            brand_id=None,
            metadata={"reason": "task_failed_rollback"},
        )
        return {"ok": True, "released": consume}
    finally:
        conn.close()


# ==========================================================================
# Usage events 写入 (P0-4)
# ==========================================================================

def _write_usage_event(
    user_id: int,
    subscription_id: Optional[int],
    entitlement_id: Optional[int],
    request_id: str,
    feature_code: str,
    consumed_quantity: int,
    consumed_unit: str,
    cache_hit: bool,
    cache_key: Optional[str],
    deduction_source: str,
    deduction_amount: int,
    brand_id: Optional[int],
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO subscription_usage_events
                (user_id, subscription_id, entitlement_id, request_id, feature_code,
                 consumed_quantity, consumed_unit, cache_hit, cache_key,
                 deduction_source, deduction_amount, brand_id, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            user_id, subscription_id, entitlement_id, request_id, feature_code,
            consumed_quantity, consumed_unit, cache_hit, cache_key,
            deduction_source, deduction_amount, brand_id,
            json.dumps(metadata) if metadata else None,
        ))
        row = cur.fetchone()
        conn.commit()
        return int(row["id"]) if row else 0
    except Exception as e:
        logger.warning(f"_write_usage_event failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        conn.close()


# ==========================================================================
# 月底重置(cron 调用)
# ==========================================================================

def month_end_reset(subscription_id: int) -> Dict[str, Any]:
    """月底:旧 entitlements 标 expired,新建本月 entitlements

    幂等:同月重复跑只重置一次(用 last_renewal_attempt_at 判断)
    """
    now = datetime.utcnow()
    current_month_key = now.strftime("%Y%m")

    conn = get_connection()
    try:
        conn.autocommit = False
        cur = conn.cursor()

        # B6 修复: explicit column list 避免 s.created_at vs p.created_at 等同名冲突
        cur.execute("""
            SELECT
                s.id AS subscription_id, s.user_id, s.plan_id,
                s.started_at, s.expires_at, s.auto_renew,
                s.last_renewal_attempt_at,
                p.quota_light_chat, p.quota_pro_write, p.quota_super_write, p.quota_web_search,
                p.quota_video_minutes, p.quota_video_single_cap, p.quota_rewrite,
                p.quota_video_breakdown, p.quota_author_breakdown, p.quota_review,
                p.quota_monthly_plan, p.quota_team_profile
            FROM user_social_subscriptions s
            JOIN subscription_plans p ON p.plan_id = s.plan_id
            WHERE s.id = %s AND s.status = 'active'
        """, (subscription_id,))
        sub = cur.fetchone()
        if not sub:
            return {"ok": True, "noop": True, "reason": "subscription_not_active"}

        last_attempt = sub.get("last_renewal_attempt_at")
        if last_attempt and last_attempt.strftime("%Y%m") == current_month_key:
            return {"ok": True, "noop": True, "reason": "already_reset_this_month"}

        # 旧 entitlements 标 expired
        cur.execute("""
            UPDATE user_social_entitlements
            SET status = 'expired', updated_at = CURRENT_TIMESTAMP
            WHERE subscription_id = %s AND status = 'active'
        """, (subscription_id,))

        # 新建本月 entitlements(套餐 SSOT 数字)
        cur.execute("""
            INSERT INTO user_social_entitlements
                (subscription_id, user_id, period_start, period_end,
                 light_chat_limit, pro_write_limit, super_write_limit, web_search_limit,
                 video_minutes_limit, video_single_minutes_cap, rewrite_limit,
                 video_breakdown_limit, author_breakdown_limit, review_limit,
                 monthly_plan_limit, team_profile_limit, status)
            VALUES
                (%(subscription_id)s, %(user_id)s, %(period_start)s, %(period_end)s,
                 %(quota_light_chat)s, %(quota_pro_write)s, %(quota_super_write)s, %(quota_web_search)s,
                 %(quota_video_minutes)s, %(quota_video_single_cap)s, %(quota_rewrite)s,
                 %(quota_video_breakdown)s, %(quota_author_breakdown)s, %(quota_review)s,
                 %(quota_monthly_plan)s, %(quota_team_profile)s, 'active')
            RETURNING id
        """, {
            "subscription_id": subscription_id,
            "user_id": sub["user_id"],
            "period_start": now,
            "period_end": now + timedelta(days=30),
            "quota_light_chat": sub["quota_light_chat"],
            "quota_pro_write": sub["quota_pro_write"],
            "quota_super_write": sub["quota_super_write"],
            "quota_web_search": sub["quota_web_search"],
            "quota_video_minutes": sub["quota_video_minutes"],
            "quota_video_single_cap": sub["quota_video_single_cap"],
            "quota_rewrite": sub["quota_rewrite"],
            "quota_video_breakdown": sub["quota_video_breakdown"],
            "quota_author_breakdown": sub["quota_author_breakdown"],
            "quota_review": sub["quota_review"],
            "quota_monthly_plan": sub["quota_monthly_plan"],
            "quota_team_profile": sub["quota_team_profile"],
        })
        new_ent = cur.fetchone()
        new_entitlement_id = int(new_ent["id"])

        # 标 last_renewal_attempt_at 防重跑
        cur.execute("""
            UPDATE user_social_subscriptions
            SET last_renewal_attempt_at = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (now, subscription_id))

        # 写 reset event
        _write_usage_event_inline(cur,
            user_id=sub["user_id"],
            subscription_id=subscription_id,
            entitlement_id=new_entitlement_id,
            request_id=f"RESET-{current_month_key}-{subscription_id}",
            feature_code="month_end_reset",
            consumed_quantity=0,
            consumed_unit="reset",
            cache_hit=False,
            cache_key=None,
            deduction_source="reset",
            deduction_amount=0,
            brand_id=None,
            metadata={"month_key": current_month_key, "plan_id": sub["plan_id"]},
        )

        conn.commit()
        logger.info(f"month_end_reset sub={subscription_id} → new entitlement {new_entitlement_id}")
        return {
            "ok": True,
            "subscription_id": subscription_id,
            "new_entitlement_id": new_entitlement_id,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _write_usage_event_inline(cur, **kwargs):
    """事务内写 usage event(不开新连接)"""
    cur.execute("""
        INSERT INTO subscription_usage_events
            (user_id, subscription_id, entitlement_id, request_id, feature_code,
             consumed_quantity, consumed_unit, cache_hit, cache_key,
             deduction_source, deduction_amount, brand_id, metadata)
        VALUES (%(user_id)s, %(subscription_id)s, %(entitlement_id)s, %(request_id)s,
                %(feature_code)s, %(consumed_quantity)s, %(consumed_unit)s, %(cache_hit)s,
                %(cache_key)s, %(deduction_source)s, %(deduction_amount)s, %(brand_id)s,
                %(metadata)s)
    """, {
        **kwargs,
        "metadata": json.dumps(kwargs.get("metadata")) if kwargs.get("metadata") else None,
    })


# ==========================================================================
# 订阅创建 + entitlements 发放
# ==========================================================================

def grant_subscription(
    user_id: int,
    plan_id: str,
    channel: str = "wechat",
    is_first_month: bool = False,
    referrer_user_id: Optional[int] = None,
    indirect_referrer_user_id: Optional[int] = None,
    agreement_version: str = "v1",
    order_id: Optional[str] = None,
    duration_days: int = 30,
) -> Dict[str, Any]:
    """新订阅 + entitlements 发放(支付 callback 调用)"""
    now = datetime.utcnow()
    expires = now + timedelta(days=duration_days)

    conn = get_connection()
    try:
        conn.autocommit = False
        cur = conn.cursor()

        # 取 plan SSOT
        cur.execute("SELECT * FROM subscription_plans WHERE plan_id = %s", (plan_id,))
        plan = cur.fetchone()
        if not plan:
            raise ValueError(f"plan_id {plan_id} 不存在")

        # 锁价
        if is_first_month and plan["first_month_yuan"] is not None:
            price_locked = float(plan["first_month_yuan"])
        else:
            price_locked = float(plan["monthly_yuan"])

        # 创建订阅(order_id UNIQUE,幂等保护)
        cur.execute("""
            INSERT INTO user_social_subscriptions
                (user_id, plan_id, channel, started_at, expires_at,
                 auto_renew, is_first_month, status,
                 referrer_user_id, indirect_referrer_user_id,
                 subscription_agreement_version, price_locked_yuan, order_id)
            VALUES (%s, %s, %s, %s, %s, FALSE, %s, 'active', %s, %s, %s, %s, %s)
            ON CONFLICT (order_id) DO NOTHING
            RETURNING id
        """, (
            user_id, plan_id, channel, now, expires,
            is_first_month,
            referrer_user_id, indirect_referrer_user_id,
            agreement_version, price_locked, order_id,
        ))
        row = cur.fetchone()
        if not row:
            # order_id 已存在 → 幂等返已有订阅
            cur.execute("""
                SELECT id FROM user_social_subscriptions WHERE order_id = %s
            """, (order_id,))
            row = cur.fetchone()
            conn.commit()
            return {"ok": True, "subscription_id": int(row["id"]) if row else None, "duplicate": True}

        sub_id = int(row["id"])

        # 发放 entitlements
        cur.execute("""
            INSERT INTO user_social_entitlements
                (subscription_id, user_id, period_start, period_end,
                 light_chat_limit, pro_write_limit, super_write_limit, web_search_limit,
                 video_minutes_limit, video_single_minutes_cap, rewrite_limit,
                 video_breakdown_limit, author_breakdown_limit, review_limit,
                 monthly_plan_limit, team_profile_limit, status)
            VALUES (%s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, 'active')
            RETURNING id
        """, (
            sub_id, user_id, now, expires,
            plan["quota_light_chat"], plan["quota_pro_write"], plan["quota_super_write"], plan["quota_web_search"],
            plan["quota_video_minutes"], plan["quota_video_single_cap"], plan["quota_rewrite"],
            plan["quota_video_breakdown"], plan["quota_author_breakdown"], plan["quota_review"],
            plan["quota_monthly_plan"], plan["quota_team_profile"],
        ))
        ent_row = cur.fetchone()
        ent_id = int(ent_row["id"])

        # 写 grant event
        _write_usage_event_inline(cur,
            user_id=user_id,
            subscription_id=sub_id,
            entitlement_id=ent_id,
            request_id=f"GRANT-{order_id or shortuuid.uuid()[:8]}",
            feature_code="subscribe",
            consumed_quantity=0,
            consumed_unit="grant",
            cache_hit=False,
            cache_key=None,
            deduction_source="grant",
            deduction_amount=0,
            brand_id=None,
            metadata={"plan_id": plan_id, "is_first_month": is_first_month, "price_locked_yuan": price_locked},
        )

        conn.commit()
        return {
            "ok": True,
            "subscription_id": sub_id,
            "entitlement_id": ent_id,
            "expires_at": expires.isoformat(),
            "price_locked_yuan": price_locked,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ==========================================================================
# 异常类
# ==========================================================================

class InsufficientQuotaError(Exception):
    """配额不足且未授权 fallback 到积分"""
    pass


class QuotaExceededError(Exception):
    """单条任务超出本套餐硬上限(如视频 >60 分钟)"""
    pass
