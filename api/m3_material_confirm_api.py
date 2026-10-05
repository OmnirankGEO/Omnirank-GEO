"""
M3 写作资料确认 API (CTO-F 2026-04-27 · feat/m3-writing-material-confirmation)

工作流闭环 — 客户成交后:
  1. 销售/交付在 /m3/customer/:brandId 的 WritingMaterialsConfirmCard 粘贴/上传客户原始资料
  2. POST /api/m3/material-confirm/clean         → AI 整理成结构化写作资料 + 写 client_materials
  3. POST /api/m3/material-confirm/generate-link → 生成 /m/:token 客户确认链
  4. 客户打开 /m/:token → 确认 OR 提反馈(走老 /api/m/* 公开端 · 不重写)
  5. GET  /api/m3/material-confirm/status/:brandId → M3 卡片实时状态机
  6. 写作链路自动读 get_confirmed_materials(brand_id) 的 confirmed snapshot

5 端点 (M3 包装层 · 复用 marketing_confirm_sessions 表):
  GET  /api/m3/material-confirm/status/{brand_id}     RBAC + 完整状态机 + materials_summary
  POST /api/m3/material-confirm/clean                 AI 清洗 + 写 client_materials/profiles
  POST /api/m3/material-confirm/generate-link         force_new flag · 否则复用未过期 pending
  POST /api/m3/material-confirm/resend/{brand_id}     刷新最新资料快照 + 重发
  POST /api/m3/material-confirm/revoke/{session_id}   set status='revoked'

不动:
  - 老 /api/marketing-confirm/* 端点(从客户管理/MarketingTab 进的入口仍 work)
  - /api/m/{token} 公开端(MaterialConfirmPage + 写作端共用)
  - marketing_confirm_sessions 表 schema(只新增 'revoked' 状态值 · 已存在的 status 列允许任意 string)
  - get_confirmed_materials(brand_id) 写作端入口

RBAC:
  所有端点(除公开 /api/m/* 外)都走 require_brand_access · session_id 端点反查 brand_id 后校验
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections import deque
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Deque, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from auth.brand_access import require_brand_access
from db.diagnosis_db import get_connection, save_client_materials

logger = logging.getLogger("GEO-M3-MaterialConfirm")

router = APIRouter(prefix="/api/m3/material-confirm", tags=["M3 写作资料确认"])


# ============================================================
# 数据模型
# ============================================================


class CleanRequest(BaseModel):
    brand_id: int = Field(..., ge=1)
    diagnosis_id: Optional[int] = Field(default=None, ge=1)
    raw_text: Optional[str] = Field(default=None, max_length=20000)
    source_url: Optional[str] = Field(default=None, max_length=2000)
    uploaded_file_refs: Optional[List[str]] = Field(default=None)
    notes: Optional[str] = Field(default=None, max_length=2000)


class GenerateLinkRequest(BaseModel):
    brand_id: int = Field(..., ge=1)
    force_new: bool = Field(default=False)


# ============================================================
# 内部工具 — 复用 marketing_confirm_api 的 snapshot 构造逻辑
# (导入而非重新声明 · 改一份)
# ============================================================


def _ensure_table() -> None:
    """marketing_confirm_sessions 幂等建表(复用老逻辑兼容旧库)"""
    from api.marketing_confirm_api import _ensure_table as _ensure
    _ensure()


def _get_brand_info(brand_id: int) -> dict:
    from api.marketing_confirm_api import _get_brand_info as _g
    return _g(brand_id)


def _get_profile_for_brand(brand_id: int) -> Optional[dict]:
    from api.marketing_confirm_api import _get_profile_for_brand as _g
    return _g(brand_id)


def _get_materials_for_brand(brand_id: int) -> Optional[dict]:
    from api.marketing_confirm_api import _get_materials_for_brand as _g
    return _g(brand_id)


def _build_materials_snapshot(brand_info: dict, profile: Optional[dict], materials: Optional[dict]) -> dict:
    from api.marketing_confirm_api import _build_materials_snapshot as _b
    return _b(brand_info, profile or {}, materials or {})


# ============================================================
# 状态机推导
# ============================================================


def _has_any_materials(profile: Optional[dict], materials: Optional[dict]) -> bool:
    """判断是否有任何写作素材 · 决定 status 是 none 还是 draft"""
    if profile:
        text_fields = ("company_intro", "core_value", "selling_points", "target_users",
                       "success_cases", "testimonials", "structured_knowledge")
        for f in text_fields:
            v = profile.get(f)
            if isinstance(v, str) and v.strip():
                return True
            if isinstance(v, (list, dict)) and v:
                return True
    if materials:
        if any(materials.get(k) for k in (
            "company_intro", "unique_value", "methodology", "core_selling_points",
            "case_studies", "testimonials", "credentials"
        )):
            return True
    return False


def _summarize_materials(snapshot: dict) -> Dict[str, Any]:
    """给前端卡片用的 materials_summary · 不返完整快照"""
    company = snapshot.get("company") or {}
    sp = snapshot.get("selling_points") or {}
    products = snapshot.get("products") or {}
    customers = snapshot.get("customers") or {}
    cases = snapshot.get("cases") or []
    testimonials = snapshot.get("testimonials") or []

    has_intro = bool((company.get("intro") or "").strip())
    has_value = bool((company.get("core_value") or "").strip())
    has_usp = bool((sp.get("usp") or "").strip())
    has_items = bool(sp.get("items"))
    has_products = bool(products.get("features") or products.get("scenarios") or products.get("name"))
    has_customers = bool(customers.get("segments") or customers.get("pain_scenario"))
    has_cases = bool(cases)
    has_testimonials = bool(testimonials)

    fields = {
        "company_intro": has_intro,
        "core_value": has_value,
        "usp": has_usp,
        "selling_points": has_items,
        "products": has_products,
        "customers": has_customers,
        "cases": has_cases,
        "testimonials": has_testimonials,
    }
    filled = [k for k, v in fields.items() if v]
    missing = [k for k, v in fields.items() if not v]

    return {
        "company_name": company.get("name") or company.get("brand_name") or "",
        "industry": company.get("industry") or "",
        "intro_excerpt": (company.get("intro") or "")[:160],
        "usp_excerpt": (sp.get("usp") or "")[:160],
        "fields_filled": filled,
        "fields_missing": missing,
        "filled_count": len(filled),
        "total_fields": len(fields),
        "selling_points_count": len(sp.get("items") or []),
        "cases_count": len(cases),
        "testimonials_count": len(testimonials),
    }


def _normalize_session_status(session: dict) -> str:
    """处理 expired 自动降级"""
    status = session.get("status") or "pending"
    if status == "pending" and session.get("expires_at"):
        try:
            exp = datetime.fromisoformat(str(session["expires_at"]))
            if datetime.now() > exp:
                return "expired"
        except (ValueError, TypeError):
            pass
    return status


# ============================================================
# 双窗口限流(/clean LLM 端点 · 防代理误点 + 恶意刷)
# 同 user_id+brand_id:60s 最多 3 次 · 24h 最多 20 次
# 内存 sliding window · 进程内 · 重启清零(可接受 · 端点低频)
# 优先尝试 Redis(全局共享)· Redis 不可用时 fallback 内存
# ============================================================

_CLEAN_RATE_SHORT_WINDOW = 60          # 秒
_CLEAN_RATE_SHORT_LIMIT = 3
_CLEAN_RATE_LONG_WINDOW = 24 * 60 * 60  # 24 小时
_CLEAN_RATE_LONG_LIMIT = 20

_clean_rate_buckets: Dict[str, Deque[float]] = {}
_clean_rate_lock = Lock()


def _clean_rate_check_inmem(key: str, window: int, limit: int) -> Tuple[bool, int]:
    """内存滑动窗口检查 · 返回 (allowed, retry_after_seconds)"""
    now = time.time()
    window_start = now - window
    with _clean_rate_lock:
        bucket = _clean_rate_buckets.get(key)
        if bucket is None:
            bucket = deque()
            _clean_rate_buckets[key] = bucket
        # 清过期
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= limit:
            oldest = bucket[0]
            retry_after = max(1, int(oldest + window - now))
            return False, retry_after
        bucket.append(now)
        # 简单内存治理
        if len(_clean_rate_buckets) > 5000:
            try:
                _clean_rate_buckets.pop(next(iter(_clean_rate_buckets)), None)
            except Exception:
                pass
    return True, 0


def _clean_rate_check_redis(key: str, window: int, limit: int) -> Optional[Tuple[bool, int]]:
    """Redis 滑动窗口检查 · Redis 不可用时返 None 走 fallback"""
    try:
        from cache.redis_client import get_redis
        r = get_redis()
        if r is None:
            return None
        now = time.time()
        window_start = now - window
        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {f"{now}:{secrets.token_hex(4)}": now})
        pipe.expire(key, window + 1)
        results = pipe.execute()
        current_count = results[1]
        if current_count >= limit:
            try:
                # 回滚本次写入
                r.zremrangebyscore(key, now, now + 0.001)
            except Exception:
                pass
            # 取最早记录算 retry_after
            try:
                earliest = r.zrange(key, 0, 0, withscores=True)
                if earliest:
                    retry_after = max(1, int(earliest[0][1] + window - now))
                    return False, retry_after
            except Exception:
                pass
            return False, window
        return True, 0
    except Exception as e:
        logger.warning(f"[M3-MaterialConfirm] redis rate check failed: {type(e).__name__}: {e}")
        return None


def _enforce_clean_rate_limit(user_id: int, brand_id: int) -> None:
    """
    /clean 端点限流 · 同 user_id+brand_id 双窗口
    超限 → HTTPException(429, detail, headers={'Retry-After'})
    """
    short_key_redis = f"m3mc:clean:short:{user_id}:{brand_id}"
    long_key_redis = f"m3mc:clean:long:{user_id}:{brand_id}"
    short_key_mem = f"short:{user_id}:{brand_id}"
    long_key_mem = f"long:{user_id}:{brand_id}"

    # 双层(短窗 + 长窗)各自检查 · 任一超限就拒
    # ⚠ headers 必须 ASCII(latin-1)· 中文文案只能进 detail(JSON body UTF-8 OK)
    # 修 staging blocker:之前 X-RateLimit-Window 用 "60 秒"/"24 小时" 中文导致 Starlette
    #   UnicodeEncodeError: 'latin-1' codec can't encode character '秒' · 把 429 变 500
    for window, limit, redis_key, mem_key, header_slug, detail_label in [
        (
            _CLEAN_RATE_SHORT_WINDOW, _CLEAN_RATE_SHORT_LIMIT,
            short_key_redis, short_key_mem,
            "60s", "60 秒",
        ),
        (
            _CLEAN_RATE_LONG_WINDOW, _CLEAN_RATE_LONG_LIMIT,
            long_key_redis, long_key_mem,
            "24h", "24 小时",
        ),
    ]:
        # 优先 Redis · 不可用时 fallback 内存
        result = _clean_rate_check_redis(redis_key, window, limit)
        if result is None:
            allowed, retry_after = _clean_rate_check_inmem(mem_key, window, limit)
        else:
            allowed, retry_after = result

        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"AI 整理操作过于频繁 · 同一客户 {detail_label}内最多 {limit} 次 · "
                    f"请 {retry_after} 秒后重试"
                ),
                headers={
                    # 全部 ASCII · header_slug 是 "60s"/"24h" 不是中文
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Window": header_slug,
                    "X-RateLimit-Limit": str(limit),
                },
            )


# ============================================================
# 服务期激活校验(/generate-link 和 /resend 必须激活后才能发链接)
# /clean 不校验 · 资料整理在签约前后都允许
# ============================================================


#: 续费去处 —— 与监测页「去续费 →」同一个目标(`pages/Monitoring/index.tsx:1784`)。
#: 一个出口只写一处:两边分头写迟早分叉,而分叉的那一天只有客户会发现。
RENEW_URL = "/pricing"


def _as_date(value):
    """把 quotes 里的日期列归一成 `date`(列是 DATE,但历史路径也塞过字符串)。"""
    from datetime import date as _date
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    try:
        return datetime.fromisoformat(str(value).split("T")[0]).date()
    except (ValueError, TypeError):
        return None


def _service_period_notice(brand_id: int) -> Dict[str, Any]:
    """这个客户的服务期**现在是什么状态**,以及没在期时从哪儿出去。

    🔴 [WO_252 ② 2026-09-20] 这里原来是一道**硬闸**:未激活直接 400,
       发不出客户确认链,页面上**没有任何出口**。而且它的话术还说错了状态 ——
       它取 `ORDER BY created_at DESC LIMIT 5` 的**第一行**来解释原因,
       brand 19 最新那张是 draft,于是提示「服务期还没激活 · 最新报价状态 draft」,
       而真相是**付过款、已到期、需要续费**。
       一个说错原因的拒绝,比不说原因更糟:代理照着它去"重新走报价流程",
       白忙一轮还是发不出去。

    改成:**不拦**,把状态和出口如实交出去,由前端展示。

    Returns:
        ``{state, paid_at, service_end_date, renew_url, message}``,
        ``state`` ∈ ``active`` | ``expired`` | ``never_paid``。
        🔴 三个状态**按付款事实分**,不按"最新那张报价长什么样"分 ——
           后者正是上面那句话术说错的原因。
    """
    today = datetime.now().date()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 🔴 不加 LIMIT 5:付费那张可能早就不在最近 5 张里(brand 19 就是)。
        #    只看付费过的报价 —— "有没有服务期"由付款决定,与草稿无关。
        cursor.execute(
            """
            SELECT id, status, service_status, service_start_date,
                   service_end_date, paid_at
            FROM quotes
            WHERE brand_id = %s AND deleted_at IS NULL
              AND (status = 'paid' OR paid_at IS NOT NULL)
            ORDER BY COALESCE(service_end_date, paid_at::date, created_at::date) DESC
            """,
            (brand_id,),
        )
        rows = [dict(r) for r in (cursor.fetchall() or [])]
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not rows:
        return {
            "state": "never_paid",
            "paid_at": None,
            "service_end_date": None,
            "renew_url": RENEW_URL,
            "message": "这位客户还没有付款记录 · 确认链仍可以发,客户确认资料不受影响",
        }

    best = rows[0]
    end_date = _as_date(best.get("service_end_date"))
    paid_at = best.get("paid_at")
    service_status = (best.get("service_status") or "").lower()
    if end_date is not None and end_date >= today and service_status in ("active", "expiring"):
        state = "active"
        message = f"服务期内 · 到 {end_date.isoformat()} 结束"
    elif end_date is not None:
        state = "expired"
        message = f"服务期已于 {end_date.isoformat()} 到期 · 去续费"
    else:
        # 🔴 工单只写了三态,而生产上有**第四种**:付过款、但服务期还没起
        #    (`service_end_date` 为空,销售端还没「标已付」落日期)。
        #    硬塞进 expired 会让页面叫人再去付一次 —— 那正是本单要治的
        #    「话术说错状态」。所以单立一态,并在交付单里点名这处偏离。
        state = "paid_not_started"
        message = "已付款 · 服务期还没开始(销售端「标已付」后自动进入)· 确认链仍可以发"
    return {
        "state": state,
        "paid_at": paid_at.isoformat() if hasattr(paid_at, "isoformat") else paid_at,
        "service_end_date": end_date.isoformat() if end_date else None,
        "renew_url": RENEW_URL,
        "message": message,
    }


# ============================================================
# GET /status/{brand_id}
# ============================================================


@router.get("/status/{brand_id}")
async def get_status(brand_id: int, request: Request):
    """
    返回完整状态机 + materials_summary · M3 卡片唯一数据源

    status 可能值:
      none       — 还没整理写作资料(profile 和 materials 都空)
      draft      — 有资料 · 还没生成确认链
      pending    — 链接已发 · 等客户动作
      feedback   — 客户提了修改意见
      confirmed  — 客户确认
      expired    — 链接过期
      revoked    — 代理撤销
    """
    require_brand_access(request, brand_id)
    _ensure_table()

    brand_info = _get_brand_info(brand_id)
    profile = _get_profile_for_brand(brand_id)
    materials = _get_materials_for_brand(brand_id)
    has_materials = _has_any_materials(profile, materials)

    # 拉最新 session
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, token, status, materials_snapshot, customer_notes,
                   confirmed_at, expires_at, created_at
            FROM marketing_confirm_sessions
            WHERE brand_id = %s
            ORDER BY created_at DESC LIMIT 1
            """,
            (brand_id,),
        )
        row = cursor.fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 当前可用资料(用于 summary · 即使 session 不存在也能展示)
    current_snapshot = _build_materials_snapshot(brand_info, profile, materials)
    summary_current = _summarize_materials(current_snapshot)

    if not row:
        # 没有 session
        status = "draft" if has_materials else "none"
        return {
            "status": status,
            "has_session": False,
            "token": None,
            "token_url": None,
            "expires_at": None,
            "confirmed_at": None,
            "customer_notes": "",
            "materials_summary": summary_current,
            "last_session_id": None,
            "can_generate_link": has_materials,
            "brand_id": brand_id,
            "brand_name": brand_info.get("name", ""),
        }

    session = dict(row)
    status = _normalize_session_status(session)

    # confirmed/feedback session 的 summary 从 snapshot 读(锁定客户看到的版本)
    if session.get("materials_snapshot") and status in ("pending", "feedback", "confirmed", "expired", "revoked"):
        try:
            session_snapshot = json.loads(session["materials_snapshot"])
            summary_session = _summarize_materials(session_snapshot)
        except (json.JSONDecodeError, TypeError):
            summary_session = summary_current
    else:
        summary_session = summary_current

    # 关键判断 · 是否还能生成链(confirmed 后不允许覆盖 · 必须先 force_new 或 revoke)
    can_generate_link = (
        status in ("none", "draft", "expired", "revoked", "feedback") or
        (status == "confirmed") or  # confirmed 也可以生成新一轮(下次资料更新)
        False
    ) and has_materials

    return {
        "status": status,
        "has_session": True,
        "token": session["token"],
        "token_url": f"/m/{session['token']}",
        "expires_at": session.get("expires_at"),
        "confirmed_at": session.get("confirmed_at"),
        "customer_notes": session.get("customer_notes") or "",
        "materials_summary": summary_session,
        "current_summary": summary_current,  # 当前 DB 资料 vs session 锁定的资料(代理对比)
        "last_session_id": session["id"],
        "can_generate_link": bool(can_generate_link),
        "brand_id": brand_id,
        "brand_name": brand_info.get("name", ""),
    }


# ============================================================
# POST /clean — AI 整理粘贴/上传的资料
# ============================================================


def _build_clean_prompt(brand_info: dict, profile: Optional[dict], existing_materials: Optional[dict],
                        raw_text: str, notes: str = "") -> str:
    """
    把原始资料 + 现有 profile/materials + agent notes 一起喂给 LLM
    要求输出 JSON · 结构对齐 client_materials schema
    """
    lines: List[str] = [
        "你是 GEO 内容写作素材整理专家 · 任务是把代理粘贴的客户原始资料整理成结构化写作资料。",
        "",
        f"客户品牌: {brand_info.get('name', '')}",
        f"行业: {(profile or {}).get('industry') or brand_info.get('industry', '')}",
        f"城市: {brand_info.get('cities', '')}",
        "",
        "==== 现有结构化资料(可补充/纠正 · 不要丢失) ====",
    ]
    if profile:
        if profile.get("company_intro"):
            lines.append(f"company_intro: {profile['company_intro']}")
        if profile.get("core_value"):
            lines.append(f"core_value: {profile['core_value']}")
        if profile.get("selling_points"):
            lines.append(f"selling_points: {profile['selling_points']}")
        if profile.get("target_users"):
            lines.append(f"target_users: {profile['target_users']}")
    if existing_materials:
        if existing_materials.get("company_intro"):
            lines.append(f"company_intro(materials): {existing_materials['company_intro']}")
        if existing_materials.get("unique_value"):
            lines.append(f"unique_value: {existing_materials['unique_value']}")
        if existing_materials.get("methodology"):
            lines.append(f"methodology: {existing_materials['methodology']}")
    lines.extend([
        "",
        "==== 代理新提供的原始资料 ====",
        raw_text or "(空)",
        "",
    ])
    if notes:
        lines.extend(["==== 代理备注 ====", notes, ""])
    lines.extend([
        "==== 输出要求 ====",
        "严格输出 JSON · 不要 markdown 代码块 · 不要解释:",
        "{",
        '  "company_intro": "公司介绍 200-400 字 · 简体中文",',
        '  "unique_value": "独特价值主张 1-2 句",',
        '  "service_area": "服务区域",',
        '  "methodology": "方法论/服务流程 · 可选",',
        '  "core_selling_points": [',
        '    {"point": "卖点", "evidence": "支撑证据"}, ...',
        '  ],',
        '  "case_studies": [',
        '    {"client": "...", "background": "...", "solution": "...", "results": "..."}',
        '  ],',
        '  "testimonials": [',
        '    {"name": "...", "title": "...", "company": "...", "quote": "..."}',
        '  ],',
        '  "credentials": [',
        '    {"type": "资质类型", "name": "证书名"}',
        '  ],',
        # [CTO-15.23 2026-05-08 P0-A] 5 维 structured_knowledge JSON · 给写作大厅 missing_fields 用
        # · 老板报客户已写公司简介 但 4 项仍标"待补"(differentiation/products/customers/cases)
        # · 真因:LLM 旧版只输出 7 字段 · 没填 client_profiles.structured_knowledge.{5 维}
        # · _build_materials_snapshot 5 维空 → fields_missing 4 项 → UI"待补"
        # · 修:LLM 同时输出 5 维 · _persist 写 structured_knowledge
        '  "structured_knowledge": {',
        '    "differentiation": {',
        '      "usp": "1 句独特卖点(不等于 unique_value · 更短锐利)",',
        '      "advantages": ["相对竞品的具体优势点 1", "..."]',
        '    },',
        '    "products": {',
        '      "name": "主产品/服务名",',
        '      "features": ["核心功能/品类 1", "..."],',
        '      "scenarios": ["使用场景 1", "..."]',
        '    },',
        '    "customers": {',
        '      "segments": ["客户细分 1(如:中高产人士)", "..."],',
        '      "needs": ["核心诉求 1", "..."],',
        '      "pain_scenario": "客户在什么场景下会想到这家"',
        '    },',
        '    "cases": [',
        '      {"client": "...", "results": "..."} ',
        '    ]',
        '  },',
        '  "missing_fields": ["明显缺失/客户没提的关键字段"],',
        '  "warnings": ["数据真实性疑虑/可疑表述"],',
        '  "confidence": 0.0-1.0',
        "}",
        "",
        "硬约束:",
        "1. 不能编造客户没说的具体数字/客户名/案例",
        "2. 缺数据的字段返空字符串/空数组 · 同时在 missing_fields 列出",
        "3. 客户已确认的内容不要改动语义 · 只能补充",
        "4. confidence 反映你对整理结果的把握度",
        "5. structured_knowledge.cases 跟 case_studies 可重叠(简版即可)· customers 不能空说",
    ])
    return "\n".join(lines)


async def _ai_clean_materials(brand_info: dict, profile: Optional[dict], existing_materials: Optional[dict],
                              raw_text: str, notes: str = "") -> Dict[str, Any]:
    """调用现有 LLM helper · 失败兜底返结构化空值 + warning"""
    prompt = _build_clean_prompt(brand_info, profile, existing_materials, raw_text, notes)
    try:
        from tools.multi_llm_caller import call_llm_with_fallback
        content = await call_llm_with_fallback(prompt, verbose=False)
    except Exception as e:
        logger.warning(f"[M3-MaterialConfirm] LLM clean failed: {type(e).__name__}: {e}")
        return {
            "company_intro": "",
            "unique_value": "",
            "service_area": "",
            "methodology": "",
            "core_selling_points": [],
            "case_studies": [],
            "testimonials": [],
            "credentials": [],
            "missing_fields": ["all"],
            "warnings": [f"AI 整理失败: {type(e).__name__} · 请稍后重试或自己填"],
            "confidence": 0.0,
            "_llm_error": True,
        }

    # 尝试解析 JSON · LLM 偶尔会包 markdown 代码块
    text = content.strip()
    if text.startswith("```"):
        # 去 ```json ... ``` 包裹
        text = text.split("```", 2)[1] if "```" in text else text
        if text.startswith("json"):
            text = text[4:].strip()
        text = text.rsplit("```", 1)[0].strip()

    try:
        # 先试标准 JSON
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 容错 · 找第一个 { 和最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                parsed = None
        else:
            parsed = None

    if not isinstance(parsed, dict):
        return {
            "company_intro": "",
            "unique_value": "",
            "service_area": "",
            "methodology": "",
            "core_selling_points": [],
            "case_studies": [],
            "testimonials": [],
            "credentials": [],
            "missing_fields": ["parse_error"],
            "warnings": ["AI 输出无法解析为 JSON · 请稍后重试"],
            "confidence": 0.0,
            "_llm_raw": text[:500],
        }

    # 字段兜底
    parsed.setdefault("company_intro", "")
    parsed.setdefault("unique_value", "")
    parsed.setdefault("service_area", "")
    parsed.setdefault("methodology", "")
    parsed.setdefault("core_selling_points", [])
    parsed.setdefault("case_studies", [])
    parsed.setdefault("testimonials", [])
    parsed.setdefault("credentials", [])
    parsed.setdefault("missing_fields", [])
    parsed.setdefault("warnings", [])
    parsed.setdefault("confidence", 0.6)

    return parsed


async def _retrieve_client_knowledge_context(brand_id: int, brand_info: dict, top_k: int = 8) -> str:
    """
    从客户知识库向量表检索写作资料相关片段。

    目标：销售/交付上传资料并完成向量化后，即使没有再手动粘贴文本，
    “AI整理资料”也能直接读取同一份客户知识库来补全结构化写作资料。
    """
    try:
        from tools.unified_knowledge import get_unified_rag
    except Exception as e:
        logger.warning(f"[M3-MaterialConfirm] import unified knowledge failed: {e}")
        return ""

    query_parts = [
        brand_info.get("name") or "",
        brand_info.get("industry") or "",
        brand_info.get("cities") or "",
        "公司介绍 产品服务 核心价值 差异化卖点 成功案例 客户评价 资质 方法论 服务流程",
    ]
    query = " ".join([str(p).strip() for p in query_parts if str(p or "").strip()])

    try:
        rag = get_unified_rag()
        results = await rag.retrieve(
            query=query,
            brand_id=brand_id,
            top_k=top_k,
            use_client=True,
            use_role=False,
        )
    except Exception as e:
        logger.warning(f"[M3-MaterialConfirm] client knowledge retrieve failed: {e}")
        return ""

    if not results:
        return ""

    parts: List[str] = ["==== 客户知识库向量检索结果（来自已上传资料） ===="]
    total = len(parts[0])
    max_chars = 10000
    for idx, item in enumerate(results, 1):
        content = str(item.get("full_content") or item.get("content") or "").strip()
        if not content:
            continue
        source = str(item.get("source") or item.get("filename") or "客户知识库")
        score = item.get("score")
        score_text = f" · score={score:.3f}" if isinstance(score, (int, float)) else ""
        chunk = f"【向量资料{idx} · {source}{score_text}】\n{content[:1800]}"
        remaining = max_chars - total
        if remaining <= 200:
            break
        if len(chunk) > remaining:
            chunk = chunk[:remaining] + "\n...(知识库资料已截断)"
        parts.append(chunk)
        total += len(chunk)

    return "\n\n".join(parts) if len(parts) > 1 else ""


def _persist_cleaned_materials(
    brand_id: int,
    diagnosis_id: Optional[int],
    cleaned: Dict[str, Any],
    *,
    organization_identity=None,
) -> None:
    """
    把 AI 清洗结果写回 client_materials(经 save_client_materials 自动同步到 client_profiles)
    没有 diagnosis_id 时 · 不写 client_materials(没主键)· 仅靠 client_profiles 路径
    """
    if not diagnosis_id:
        # 找最新 diagnosis_id 兜底
        conn = get_connection()
        try:
            cursor = conn.cursor()
            # [返工3 P2] 业务消费点(把物料挂到最新诊断)→ 必须 published-only,禁把 pending/withheld(结算前/已退款/
            #   中断)诊断当"最新"挂物料(否则未结算诊断被后续物料确认引用)。NULL=旧数据兼容 published。
            cursor.execute(
                "SELECT id FROM diagnosis_records WHERE brand_id = %s "
                "AND (result_visibility IS NULL OR result_visibility = 'published') "
                "ORDER BY created_at DESC LIMIT 1",
                (brand_id,),
            )
            row = cursor.fetchone()
            if row:
                diagnosis_id = row["id"]
        finally:
            try:
                conn.close()
            except Exception:
                pass

    if not diagnosis_id:
        if organization_identity is not None:
            from db.diagnosis_db import save_profile_materials_for_brand
            save_profile_materials_for_brand(
                brand_id,
                cleaned,
                organization_identity=organization_identity,
            )
            return
        # 实在没 diagnosis · 走 client_profiles 直接更新关键文本字段
        try:
            def _points_to_text(points: Any) -> str:
                if not points:
                    return ""
                if isinstance(points, str):
                    return points
                if isinstance(points, list):
                    items = []
                    for item in points:
                        if isinstance(item, dict):
                            point = item.get("point") or item.get("name") or ""
                            evidence = item.get("evidence") or ""
                            items.append(f"{point}: {evidence}" if point and evidence else (point or evidence))
                        else:
                            items.append(str(item))
                    return "\n".join(x for x in items if x)
                return str(points)

            def _json_text(value: Any) -> str:
                if not value:
                    return ""
                if isinstance(value, str):
                    return value
                return json.dumps(value, ensure_ascii=False)

            profile_update = {}
            if cleaned.get("company_intro"):
                profile_update["company_intro"] = cleaned.get("company_intro") or ""
            if cleaned.get("unique_value"):
                profile_update["core_value"] = cleaned.get("unique_value") or ""
            selling_points_text = _points_to_text(cleaned.get("core_selling_points"))
            if selling_points_text:
                profile_update["selling_points"] = selling_points_text
            success_cases_text = _json_text(cleaned.get("case_studies"))
            if success_cases_text:
                profile_update["success_cases"] = success_cases_text
            testimonials_text = _json_text(cleaned.get("testimonials"))
            if testimonials_text:
                profile_update["testimonials"] = testimonials_text

            has_structured_knowledge = bool(cleaned.get("structured_knowledge"))
            if not profile_update and not has_structured_knowledge:
                return

            profile_id = _ensure_profile_for_brand(brand_id, profile_update)
            if profile_id:
                _persist_structured_knowledge(profile_id, cleaned)
        except Exception as e:
            logger.warning(f"[M3-MaterialConfirm] profile fallback update failed: {e}")
        return

    # 走 save_client_materials · material/profile/stamp 同一事务；异常必须
    # 传播到 HTTP 层，不能对外声称成功后留下半提交。
    save_client_materials(diagnosis_id, {
            "company_intro": cleaned.get("company_intro") or "",
            "service_area": cleaned.get("service_area") or "",
            "core_selling_points": cleaned.get("core_selling_points") or [],
            "unique_value": cleaned.get("unique_value") or "",
            "methodology": cleaned.get("methodology") or "",
            "case_studies": cleaned.get("case_studies") or [],
            "testimonials": cleaned.get("testimonials") or [],
            "credentials": cleaned.get("credentials") or [],
            "structured_knowledge": cleaned.get("structured_knowledge") or {},
        }, organization_identity=organization_identity, expected_brand_id=brand_id)

    # [CTO-15.23 2026-05-08 P0-A] LLM 输出的 5 维 structured_knowledge 写到 client_profiles
    # · save_client_materials 不动 structured_knowledge · 必须额外路径写入
    # · _build_materials_snapshot 读 sk.{differentiation/products/customers/cases} 才能让 missing_fields 不假报
    if organization_identity is None:
        try:
            profile_id = _ensure_profile_for_brand(brand_id)
            if profile_id:
                _persist_structured_knowledge(profile_id, cleaned)
        except Exception as e:
            logger.warning(f"[M3-MaterialConfirm] structured_knowledge persist failed: {e}")


def _ensure_profile_for_brand(brand_id: int, profile_update: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """确保 brand 有 client_profiles 行；缺失时用 brands.name 建档。"""
    from db.profile_db import create_profile, list_profiles, update_profile

    profiles = list_profiles(brand_ids=[brand_id])
    if profiles:
        profile_id = profiles[0]["id"]
        if profile_update:
            update_profile(profile_id, **profile_update)
        return profile_id

    brand_info = _get_brand_info(brand_id)
    new_profile_id = create_profile(
        name=brand_info.get("name") or f"brand_{brand_id}",
        industry=brand_info.get("industry") or "",
        brand_id=brand_id,
        **(profile_update or {}),
    )
    new_profiles = list_profiles(brand_ids=[brand_id])
    if new_profiles:
        return new_profiles[0]["id"]
    return new_profile_id


def _persist_structured_knowledge(profile_id: int, cleaned: Dict[str, Any]) -> None:
    """[CTO-15.23 2026-05-08 P0-A] LLM 输出 5 维 → 合并写 client_profiles.structured_knowledge

    · 读现有 sk(text JSON)· 跟新 5 维深度合并(老数据有则不覆盖 · 仅补空 dimension)
    · 兜底:LLM 没输出 structured_knowledge 时 · 用 cleaned.case_studies / unique_value 衍生关键 dimension
    · update_profile 内部 json.dumps 写入(profile_db.py 已支持 structured_knowledge JSON)
    """
    try:
        from db.profile_db import update_profile, get_profile
        existing = get_profile(profile_id) or {}
        existing_sk_raw = existing.get("structured_knowledge") or "{}"
        if isinstance(existing_sk_raw, str):
            try:
                existing_sk = json.loads(existing_sk_raw) if existing_sk_raw.strip() else {}
            except (json.JSONDecodeError, ValueError):
                existing_sk = {}
        elif isinstance(existing_sk_raw, dict):
            existing_sk = existing_sk_raw
        else:
            existing_sk = {}

        new_sk = cleaned.get("structured_knowledge") or {}
        if not isinstance(new_sk, dict):
            new_sk = {}

        # 兜底:LLM 没输出 structured_knowledge 时 · 用老字段衍生
        if not new_sk.get("cases") and cleaned.get("case_studies"):
            new_sk["cases"] = cleaned.get("case_studies") or []
        if not new_sk.get("differentiation") and cleaned.get("unique_value"):
            new_sk["differentiation"] = {"usp": cleaned.get("unique_value") or "", "advantages": []}

        # 深度合并 · 老 sk 优先(不丢已确认内容)· 新 sk 仅覆盖空 dimension
        merged = dict(existing_sk)
        for key in ("differentiation", "products", "customers", "painPoints", "cases"):
            old_v = merged.get(key)
            new_v = new_sk.get(key)
            # 老的有数据(dict 非空 / list 非空)→ 不覆盖
            if isinstance(old_v, dict) and any(old_v.values()):
                continue
            if isinstance(old_v, list) and len(old_v) > 0:
                continue
            # 老空 + 新有 → 覆盖
            if new_v:
                merged[key] = new_v

        # 仅当合并后有变化 · 才 update(避免无效写)
        if merged != existing_sk:
            update_profile(profile_id, structured_knowledge=merged)
            logger.info(f"[M3-MaterialConfirm] structured_knowledge persisted profile_id={profile_id} "
                        f"keys={list(merged.keys())}")
    except Exception as e:
        logger.warning(f"[M3-MaterialConfirm] _persist_structured_knowledge failed pid={profile_id}: {e}")


@router.post("/clean")
async def clean_materials(req: CleanRequest, request: Request):
    """
    AI 整理代理粘贴/上传的客户原始资料

    行为:
      1. 拉现有 profile + materials 作为 LLM 上下文(避免覆盖客户已确认内容)
      2. 调 multi_llm_caller 整理为结构化 JSON
      3. 若 diagnosis_id 在 → 写 client_materials · 自动同步 client_profiles
      4. 否则 → fallback 仅更新 client_profiles 关键字段
      5. 不直接刷新 confirmed session(必须重新 generate-link 或 resend)

    返回:
      cleaned_materials, missing_fields, warnings, confidence, can_generate_link
    """
    require_brand_access(request, req.brand_id)

    # 双窗口限流(LLM 端点保护)· 同 user_id+brand_id:60s 最多 3 次 · 24h 最多 20 次
    user = getattr(request.state, "user", None) or {}
    user_id = user.get("user_id") or user.get("id") or 0
    if not user_id:
        # 走到这一步必须已有登录态(require_brand_access 校验过)· 兜底拒绝
        raise HTTPException(401, "未登录 · 不能调用 AI 整理")
    _enforce_clean_rate_limit(int(user_id), int(req.brand_id))

    brand_info = _get_brand_info(req.brand_id)
    profile = _get_profile_for_brand(req.brand_id)
    materials = _get_materials_for_brand(req.brand_id)

    raw_text_for_llm = (req.raw_text or "").strip()
    notes_for_llm = (req.notes or "").strip()
    if req.source_url:
        raw_text_for_llm += f"\n\n[参考网址: {req.source_url}]"

    # 已上传并向量化的客户资料也必须成为 AI 整理上下文。
    # uploaded_file_refs 保留为兼容字段；当前以 brand_id 检索客户知识库，覆盖“只上传文件、不粘贴文本”的操作路径。
    kb_context = await _retrieve_client_knowledge_context(req.brand_id, brand_info)
    if kb_context:
        raw_text_for_llm = f"{raw_text_for_llm}\n\n{kb_context}".strip()

    if not raw_text_for_llm and not notes_for_llm:
        raise HTTPException(400, "请先上传客户资料文件并完成向量化，或粘贴客户原始资料后再整理")

    cleaned = await _ai_clean_materials(brand_info, profile, materials, raw_text_for_llm, notes_for_llm)

    # 持久化(LLM 完全失败时不持久化 · 只把 warning 给前端)
    if not cleaned.get("_llm_error"):
        _persist_cleaned_materials(
            req.brand_id,
            req.diagnosis_id,
            cleaned,
            organization_identity=getattr(request.state, "organization_identity", None),
        )

    # 重算 has_materials(刚写入应该是 True)
    profile_after = _get_profile_for_brand(req.brand_id)
    materials_after = _get_materials_for_brand(req.brand_id)
    has_materials = _has_any_materials(profile_after, materials_after)

    return {
        "success": not cleaned.get("_llm_error", False),
        "cleaned_materials": {
            "company_intro": cleaned.get("company_intro", ""),
            "unique_value": cleaned.get("unique_value", ""),
            "service_area": cleaned.get("service_area", ""),
            "methodology": cleaned.get("methodology", ""),
            "core_selling_points": cleaned.get("core_selling_points", []),
            "case_studies": cleaned.get("case_studies", []),
            "testimonials": cleaned.get("testimonials", []),
            "credentials": cleaned.get("credentials", []),
            "structured_knowledge": cleaned.get("structured_knowledge") or {},
        },
        "missing_fields": cleaned.get("missing_fields", []),
        "warnings": cleaned.get("warnings", []),
        "confidence": cleaned.get("confidence", 0.0),
        "can_generate_link": has_materials,
    }


# ============================================================
# POST /generate-link
# ============================================================


@router.post("/generate-link")
async def generate_link(req: GenerateLinkRequest, request: Request):
    """
    生成新的客户确认链接

    force_new=False(默认):
      若已有 pending/feedback 且未过期 → 复用 token + 刷新 snapshot
      若已 confirmed/revoked/expired → 新建 token

    force_new=True:
      失效旧 active session · 强制建新 token
    """
    require_brand_access(request, req.brand_id)
    _ensure_table()

    # [WO_252 ② 2026-09-20] 原来这里硬拦 400(旧注释:避免"未付款客户拿到 /m 链")。
    # Owner 09-20:「一定不能阻塞,出现问题一定要有出口」⇒ 改为**不拦 + 出声 + 出口**:
    # 每个返回都带 service_notice,前端据它提示真实状态与续费入口。
    # 🔴 只放开这一道闸,**不碰任何扣费闸**(写文章/生成标题的收费路径一行没动)。
    service_notice = _service_period_notice(req.brand_id)

    brand_info = _get_brand_info(req.brand_id)
    profile = _get_profile_for_brand(req.brand_id)
    materials = _get_materials_for_brand(req.brand_id)

    if not _has_any_materials(profile, materials):
        raise HTTPException(400, "还没整理写作资料 · 请先粘贴客户资料并 AI 整理")

    snapshot = _build_materials_snapshot(brand_info, profile, materials)
    snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    expires_at = datetime.now() + timedelta(days=7)

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 找最新 session
        cursor.execute(
            """
            SELECT id, token, status, expires_at FROM marketing_confirm_sessions
            WHERE brand_id = %s
            ORDER BY created_at DESC LIMIT 1
            """,
            (req.brand_id,),
        )
        latest = cursor.fetchone()

        if not req.force_new and latest:
            current_status = _normalize_session_status(dict(latest))
            if current_status in ("pending", "feedback"):
                # 复用 token · 刷新 snapshot
                cursor.execute(
                    """
                    UPDATE marketing_confirm_sessions
                    SET status = 'pending',
                        materials_snapshot = %s,
                        customer_notes = '',
                        expires_at = %s
                    WHERE id = %s
                    """,
                    (snapshot_json, expires_at, latest["id"]),
                )
                conn.commit()
                logger.info(f"[M3-MaterialConfirm] reuse session brand={req.brand_id} token={latest['token']}")
                return {
                    "success": True,
                    "token": latest["token"],
                    "url": f"/m/{latest['token']}",
                    "expires_at": expires_at.isoformat(),
                    "reused": True,
                    "service_notice": service_notice,
                }

        # 失效所有 active session
        cursor.execute(
            """
            UPDATE marketing_confirm_sessions
            SET status = 'expired'
            WHERE brand_id = %s AND status IN ('pending', 'feedback')
            """,
            (req.brand_id,),
        )

        # 新建 token
        token = secrets.token_urlsafe(12)
        cursor.execute(
            """
            INSERT INTO marketing_confirm_sessions
                (token, brand_id, status, materials_snapshot, expires_at)
            VALUES (%s, %s, 'pending', %s, %s)
            RETURNING id, token
            """,
            (token, req.brand_id, snapshot_json, expires_at),
        )
        new_row = cursor.fetchone()
        conn.commit()
        logger.info(f"[M3-MaterialConfirm] new session brand={req.brand_id} token={token}")

        return {
            "success": True,
            "token": new_row["token"],
            "url": f"/m/{new_row['token']}",
            "expires_at": expires_at.isoformat(),
            "reused": False,
            "service_notice": service_notice,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# POST /resend/{brand_id}
# ============================================================


@router.post("/resend/{brand_id}")
async def resend(brand_id: int, request: Request):
    """
    刷新最新资料快照后重新发送给客户(适合 feedback 处理后再发)
    若无 active session → 自动新建一个(等同 generate-link force_new)
    """
    require_brand_access(request, brand_id)
    _ensure_table()

    # [WO_252 ②] 同 generate-link:不拦,改带 service_notice(状态 + 出口)。
    service_notice = _service_period_notice(brand_id)

    brand_info = _get_brand_info(brand_id)
    profile = _get_profile_for_brand(brand_id)
    materials = _get_materials_for_brand(brand_id)

    if not _has_any_materials(profile, materials):
        raise HTTPException(400, "还没整理写作资料 · 请先粘贴客户资料并 AI 整理")

    snapshot = _build_materials_snapshot(brand_info, profile, materials)
    snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    expires_at = datetime.now() + timedelta(days=7)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, token, status FROM marketing_confirm_sessions
            WHERE brand_id = %s
            ORDER BY created_at DESC LIMIT 1
            """,
            (brand_id,),
        )
        latest = cursor.fetchone()

        if latest and _normalize_session_status(dict(latest)) in ("pending", "feedback"):
            # 复用 token · 刷新 snapshot + 状态回 pending
            cursor.execute(
                """
                UPDATE marketing_confirm_sessions
                SET status = 'pending',
                    materials_snapshot = %s,
                    customer_notes = '',
                    expires_at = %s
                WHERE id = %s
                """,
                (snapshot_json, expires_at, latest["id"]),
            )
            conn.commit()
            return {
                "success": True,
                "token": latest["token"],
                "url": f"/m/{latest['token']}",
                "reused": True,
                "service_notice": service_notice,
            }

        # 没 active session → 新建
        token = secrets.token_urlsafe(12)
        cursor.execute(
            """
            INSERT INTO marketing_confirm_sessions
                (token, brand_id, status, materials_snapshot, expires_at)
            VALUES (%s, %s, 'pending', %s, %s)
            RETURNING id, token
            """,
            (token, brand_id, snapshot_json, expires_at),
        )
        new_row = cursor.fetchone()
        conn.commit()
        return {
            "success": True,
            "token": new_row["token"],
            "url": f"/m/{new_row['token']}",
            "reused": False,
            "service_notice": service_notice,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# POST /revoke/{session_id}
# ============================================================


@router.post("/revoke/{session_id}")
async def revoke(session_id: int, request: Request):
    """
    撤销 session(误发链接 / 客户身份变更场景)
    confirmed 状态不可撤销(已是不可变快照 · 写作链路依赖)
    """
    _ensure_table()

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, brand_id, status FROM marketing_confirm_sessions WHERE id = %s",
            (session_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "session 不存在")

        # 反查 brand_id 后 RBAC
        require_brand_access(request, row["brand_id"])

        if row["status"] == "confirmed":
            raise HTTPException(400, "已确认的 session 不可撤销 · 写作链路依赖该快照")

        cursor.execute(
            "UPDATE marketing_confirm_sessions SET status = 'revoked' WHERE id = %s",
            (session_id,),
        )
        conn.commit()
        logger.info(f"[M3-MaterialConfirm] revoke session_id={session_id} brand={row['brand_id']}")
        return {"success": True}
    finally:
        try:
            conn.close()
        except Exception:
            pass
