"""
分享海报 API
- 二维码生成
- 海报数据生成（5种类型）
- 短链创建与重定向
"""

import base64
import json
import logging
from io import BytesIO
from typing import Any, Optional

import qrcode
import shortuuid
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response, RedirectResponse
from pydantic import BaseModel, Field

from db.connection import get_db, get_connection
from services.public_whitelabel import get_public_whitelabel_data, is_whitelabel_active_for_customer
from auth.brand_access import require_diagnosis_access  # [GEO-R1-CAN-121] 归属校验
from html import escape as _html_escape
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-Share-API")

router = APIRouter(tags=["分享海报"])


# ==================== 数据库初始化 ====================

def init_share_tables():
    """创建短链表（幂等）"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS short_links (
                code VARCHAR(8) PRIMARY KEY,
                target_url TEXT NOT NULL,
                creator_user_id INTEGER,
                link_type VARCHAR(20),
                metadata JSONB DEFAULT '{}',
                click_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_short_links_creator
            ON short_links(creator_user_id)
        """)
    logger.info("[Share] 短链表初始化完成")


# ==================== 请求模型 ====================

class PosterRequest(BaseModel):
    type: str = Field(..., description="海报类型: register/interview/team/profile/report")
    params: dict = Field(default={}, description="类型相关参数")


# ==================== 工具函数 ====================

def _generate_qr_bytes(url: str, size: int = 300) -> bytes:
    """生成二维码 PNG 字节"""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=max(1, size // 30),
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _generate_qr_base64(url: str, size: int = 300) -> str:
    """生成二维码 base64 data URI"""
    png_bytes = _generate_qr_bytes(url, size)
    b64 = base64.b64encode(png_bytes).decode()
    return f"data:image/png;base64,{b64}"


def _create_short_link(target_url: str, link_type: str, creator_user_id: int, metadata: dict = None) -> str:
    """创建短链，返回 code"""
    code = shortuuid.uuid()[:8].lower()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO short_links (code, target_url, creator_user_id, link_type, metadata)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET target_url = EXCLUDED.target_url
            RETURNING code
        """, (code, target_url, creator_user_id, link_type,
              json.dumps(metadata or {}, ensure_ascii=False)))
        conn.commit()
        result = cursor.fetchone()
        return result["code"]
    finally:
        conn.close()


def _get_user_info(user_id: int) -> dict:
    """获取用户基本信息"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, display_name, username FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _get_referral_code(user_id: int) -> Optional[str]:
    """获取用户推荐码"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT code FROM referral_codes WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        return row["code"] if row else None
    finally:
        conn.close()


def _select_customer_report_branch(
    has_whitelabel: bool,
    should_v3: bool,
    force_legacy_rollback: bool = False,
) -> str:
    """选客户报告渲染分支：'v3' | 'customer_decision' | 'legacy'。

    v3.6 闭环（修 Codex T4b P1）：授权白标（has_whitelabel）→ 强制 'customer_decision'（唯一已支持
    白标的分支），禁 report_v3 / legacy —— 否则授权代理的客户报告在 v3/legacy 分支会露 OmniRank。
    未授权 → V3 明确 Gate 优先；仅显式紧急回滚才走 legacy；其余 V2 公开网页默认
    customer_decision。配置缺失或异常不能等价于紧急回滚。
    """
    if has_whitelabel:
        return "customer_decision"
    if should_v3:
        return "v3"
    if force_legacy_rollback:
        return "legacy"
    return "customer_decision"


def _render_whitelabel_error_page(whitelabel_payload) -> str:
    """授权白标报告渲染失败时的极简兜底页（fail-closed · 0 平台品牌 · 不退 legacy）。

    只用代理 company_name（转义防 XSS）+ 静态文案，绝不含 OmniRank/全域上榜。
    """
    co = ((whitelabel_payload or {}).get("company_name") or "").strip() or "诊断报告"
    co = _html_escape(co)
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{co}</title></head>"
        "<body style=\"font-family:system-ui,-apple-system,sans-serif;padding:48px;"
        "text-align:center;color:#374151\">"
        f"<h2 style='font-weight:600;margin-bottom:8px'>{co}</h2>"
        "<p style='color:#6b7280'>报告生成暂时遇到问题，请稍后刷新重试。</p>"
        "</body></html>"
    )


def _public_report_not_ready_payload() -> dict:
    from services.report_html_renderer import (
        CLIENT_REPORT_NOT_READY_CODE,
        CLIENT_REPORT_NOT_READY_MESSAGE,
    )

    detail = {
        "code": CLIENT_REPORT_NOT_READY_CODE,
        "message": CLIENT_REPORT_NOT_READY_MESSAGE,
    }
    return {
        "status": "not_ready",
        "not_ready": True,
        "code": CLIENT_REPORT_NOT_READY_CODE,
        "message": CLIENT_REPORT_NOT_READY_MESSAGE,
        # Backward-compatible with the current public page's error normalizer.
        "detail": detail,
    }


def _render_authorized_whitelabel_report_or_error(whitelabel_payload, render_customer_decision):
    """授权白标渲染：先 customer_decision，失败 → 白标极简错误页。

    fail-closed：legacy renderer 不作为参数传入 → 结构上不可能 fallback 到会露 OmniRank 的 legacy。
    （修 Codex T4b P1：异常兜底二次失败曾退 legacy 泄漏平台品牌。）
    """
    try:
        return render_customer_decision()
    except Exception:
        logger.warning(
            "[whitelabel] customer_decision render failed for authorized whitelabel "
            "— fail-closed 白标错误页（不退 legacy / 不露 OmniRank）"
        )
        return _render_whitelabel_error_page(whitelabel_payload)


def _get_whitelabel(user_id: int) -> Optional[dict]:
    """获取白标设置"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM whitelabel_settings WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ==================== 海报数据构建器 ====================

def _build_register_poster(user: dict, origin: str) -> dict:
    """推荐注册海报"""
    user_id = user.get("user_id") or user.get("id")
    referral_code = _get_referral_code(user_id)

    target_url = f"{origin}/login?mode=register"

    code = _create_short_link(target_url, "register", user_id, {"referral_code": referral_code})
    short_url = f"{origin}/api/sl/{code}"

    return {
        "poster_data": {
            "type": "register",
            "title": "OmniRank 活动邀请",
            "subtitle": "你的客户正在 AI 搜索里找你，你在吗？",
            "features": [
                "查一下品牌在 AI 搜索里排第几",
                "看同行用什么方法抢排名",
                "发 1 条顶别人 10 条的效果",
            ],
            "cta": "注册就送 3888 积分，先查再说",
            "qr_hint": "扫码，30秒注册",
            "qr_url": short_url,
            "qr_image_base64": _generate_qr_base64(short_url),
        },
        "share_url": short_url,
        "short_code": code,
    }


def _build_register_social_poster(user: dict, origin: str) -> dict:
    """推荐注册海报 — 社媒创作者版"""
    user_id = user.get("user_id") or user.get("id")
    referral_code = _get_referral_code(user_id)

    target_url = f"{origin}/login?mode=register"

    code = _create_short_link(target_url, "register_social", user_id, {"referral_code": referral_code})
    short_url = f"{origin}/api/sl/{code}"

    return {
        "poster_data": {
            "type": "register_social",
            "title": "OmniRank 活动邀请",
            "subtitle": "AI 写的内容千篇一律？\n因为它不认识你",
            "features": [
                "先建档人设，再按你的风格写",
                "告别模板灌水，每条都像亲写的",
                "拆博主 + 排选题，10 分钟搞定一周",
            ],
            "cta": "注册就送 3888 积分，先试再说",
            "qr_hint": "扫码，30秒注册",
            "qr_url": short_url,
            "qr_image_base64": _generate_qr_base64(short_url),
        },
        "share_url": short_url,
        "short_code": code,
    }


def _build_interview_poster(user: dict, params: dict, origin: str) -> dict:
    """AI面试邀请海报"""
    user_id = user.get("user_id") or user.get("id")
    user_info = _get_user_info(user_id)
    profile_id = params.get("profile_id", "")

    target_url = f"{origin}/s/interview?profile_id={profile_id}&invited_by={user_id}"
    code = _create_short_link(target_url, "interview", user_id, {"profile_id": profile_id})
    short_url = f"{origin}/api/sl/{code}"

    # 获取档案品牌名
    brand_name = ""
    if profile_id:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT cp.name, b.name AS brand_name FROM client_profiles cp
                LEFT JOIN brands b ON b.id = cp.brand_id
                WHERE cp.id = %s
            """, (profile_id,))
            row = cursor.fetchone()
            if row:
                brand_name = row.get("brand_name") or row.get("name") or ""
        finally:
            conn.close()

    return {
        "poster_data": {
            "type": "interview",
            "title": "聊 5 分钟，AI 帮你找到你的表达风格",
            "subtitle": "你是什么型的创作者？",
            "features": [
                "发现你自己都没意识到的说话特点",
                "找到最适合你的内容方向，别再硬拗人设",
                "生成你的专属 IP 画像，发朋友圈都好看",
            ],
            "cta": "",
            "qr_hint": "扫码开聊，不用准备",
            "qr_url": short_url,
            "qr_image_base64": _generate_qr_base64(short_url),
            "sharer_name": user_info.get("display_name", ""),
            "brand_name": brand_name,
        },
        "share_url": short_url,
        "short_code": code,
    }


def _build_team_poster(user: dict, params: dict, origin: str) -> dict:
    """团队邀请海报"""
    user_id = user.get("user_id") or user.get("id")
    user_info = _get_user_info(user_id)
    team_id = params.get("team_id")

    if not team_id:
        raise HTTPException(400, detail="缺少 team_id 参数")

    from db.team_db import get_team, get_team_members
    team = get_team(team_id)
    if not team:
        raise HTTPException(404, detail="团队不存在")

    team_code = team.get("team_code", "")
    members = get_team_members(team_id) or []

    # 获取品牌名
    brand_name = ""
    if team.get("brand_id"):
        conn = get_connection()
        try:
            cursor = conn.cursor()
            # 🔴 [#114] 生产 brands 的列是 `name`,没有 `brand_name`。
            #    这一处**没有** except 兜底 ⇒ UndefinedColumn 直接冒到端点,团队分享链接 500。
            cursor.execute("SELECT name FROM brands WHERE id = %s", (team["brand_id"],))
            row = cursor.fetchone()
            if row:
                brand_name = row["name"]
        finally:
            conn.close()

    target_url = f"{origin}/s?join={team_code}"
    code = _create_short_link(target_url, "team", user_id, {"team_id": team_id, "team_code": team_code})
    short_url = f"{origin}/api/sl/{code}"

    leader_name = user_info.get("display_name", "")

    return {
        "poster_data": {
            "type": "team",
            "title": team.get("name", "团队"),
            "subtitle": f"{leader_name} 喊你一起干活",
            "member_count": len(members),
            "brand_name": brand_name,
            "features": [
                "团队的素材库你直接用，不用自己找",
                "有专属 AI 教练带你上手",
                "别单打独斗了，一起出内容快 3 倍",
            ],
            "qr_hint": "扫码加入，马上开工",
            "qr_url": short_url,
            "qr_image_base64": _generate_qr_base64(short_url),
            "team_code": team_code,
        },
        "share_url": short_url,
        "short_code": code,
    }


def _build_profile_poster(user: dict, params: dict, origin: str) -> dict:
    """IP人设卡海报"""
    user_id = user.get("user_id") or user.get("id")
    profile_id = params.get("profile_id", "")

    if not profile_id:
        raise HTTPException(400, detail="缺少 profile_id 参数")

    # 获取分享卡数据
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT name, personality_profile, profile_completeness
            FROM client_profiles WHERE id = %s
        """, (profile_id,))
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, detail="档案不存在")

    personality_profile = row.get("personality_profile")
    if isinstance(personality_profile, str):
        try:
            personality_profile = json.loads(personality_profile)
        except (json.JSONDecodeError, TypeError):
            personality_profile = {}
    personality_profile = personality_profile or {}

    profile_completeness = row.get("profile_completeness")
    if isinstance(profile_completeness, str):
        try:
            profile_completeness = json.loads(profile_completeness)
        except (json.JSONDecodeError, TypeError):
            profile_completeness = {}
    profile_completeness = profile_completeness or {}

    share_code = profile_completeness.get("share_code")
    if not share_code:
        share_code = shortuuid.uuid()[:8]
        profile_completeness["share_code"] = share_code
        conn2 = get_connection()
        try:
            cur2 = conn2.cursor()
            cur2.execute(
                "UPDATE client_profiles SET profile_completeness = %s WHERE id = %s",
                (json.dumps(profile_completeness, ensure_ascii=False), profile_id)
            )
            conn2.commit()
        finally:
            conn2.close()

    target_url = f"{origin}/s/match/{share_code}"
    code = _create_short_link(target_url, "profile", user_id, {"profile_id": profile_id, "share_code": share_code})
    short_url = f"{origin}/api/sl/{code}"

    mbti = personality_profile.get("mbti", {})
    radar = personality_profile.get("radar", {})
    soul_tags = personality_profile.get("soul_tags", [])
    ip_declaration = personality_profile.get("ip_declaration", "")
    expression_type = personality_profile.get("expression_type", {})
    creator_type = expression_type.get("type", "") if isinstance(expression_type, dict) else str(expression_type)

    # radar 值可能是 {score, reason}，提取纯数字
    radar_flat = {}
    for k, v in radar.items():
        radar_flat[k] = v.get("score", 0) if isinstance(v, dict) else (v if isinstance(v, (int, float)) else 0)

    return {
        "poster_data": {
            "type": "profile",
            "title": f"{row.get('name', '')} 的创作者画像",
            "creator_type": creator_type,
            "mbti": mbti.get("type", "") if isinstance(mbti, dict) else str(mbti),
            "radar": radar_flat,
            "soul_tags": soul_tags[:5] if isinstance(soul_tags, list) else [],
            "ip_declaration": ip_declaration,
            "qr_hint": "扫码看看我们多匹配",
            "qr_url": short_url,
            "qr_image_base64": _generate_qr_base64(short_url),
        },
        "share_url": short_url,
        "short_code": code,
    }


def _build_report_poster(user: dict, params: dict, origin: str, request: Request) -> dict:
    """诊断报告海报"""
    user_id = user.get("user_id") or user.get("id")
    diagnosis_id = params.get("diagnosis_id")

    if not diagnosis_id:
        raise HTTPException(400, detail="缺少 diagnosis_id 参数")

    # [GEO-R1-CAN-121] 归属校验 fail-closed:任何登录用户都可传 type=report + 他人 diagnosis_id,
    # 旧代码仅按 id 直读 diagnosis_records(跨租户读品牌名/评分/维度/发现),并铸造可绕过 P0-1
    # 反枚举的 /public/report 短链。改为读取任何报告内容/铸链前先校验归属(brand owner / 分配)。
    # allow_null=False:NULL-brand 诊断无法验证归属时拒绝,而非放行。
    require_diagnosis_access(request, int(diagnosis_id), allow_null=False)

    # 获取诊断数据
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, brand_name, total_score, level,
                   web_search_score, platform_score, content_quality_score,
                   authority_score, brand_ownership_score,
                   ai_visibility_score, ai_citation_score, update_frequency_score,
                   report_md_path, result_visibility
            FROM diagnosis_records WHERE id = %s
        """, (diagnosis_id,))
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, detail="诊断报告不存在")

    # 兼容旧字段名
    row = dict(row)
    # [WORKERS=4 · SPEC §3.6 P0-3] 可见性严闸:pending/withheld 的诊断不出分享海报(退款/未完成结果不可外传)
    if row.get("result_visibility") in ("pending", "withheld"):
        raise HTTPException(404, detail="该诊断报告当前不可用于分享")
    row["score"] = row.get("total_score")
    # 从维度分数构建 dimensions
    row["dimensions"] = {
        "web_search": row.get("web_search_score"),
        "platform": row.get("platform_score"),
        "content_quality": row.get("content_quality_score"),
        "authority": row.get("authority_score"),
        "brand_ownership": row.get("brand_ownership_score"),
        "ai_visibility": row.get("ai_visibility_score"),
        "ai_citation": row.get("ai_citation_score"),
        "update_frequency": row.get("update_frequency_score"),
    }
    # 从报告 md 文件读取摘要
    row["summary"] = ""
    md_path = row.get("report_md_path")
    if md_path:
        import os
        for base in [".", "/app"]:
            full = os.path.join(base, md_path)
            if os.path.exists(full):
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        lines = f.readlines()[:20]
                    row["summary"] = "\n".join(l.strip() for l in lines if l.strip())
                except Exception:
                    pass
                break

    target_url = f"{origin}/public/report/{diagnosis_id}"
    code = _create_short_link(target_url, "report", user_id, {"diagnosis_id": diagnosis_id})
    short_url = f"{origin}/api/sl/{code}"

    # 解析 dimensions
    dimensions = row.get("dimensions")
    if isinstance(dimensions, str):
        try:
            dimensions = json.loads(dimensions)
        except (json.JSONDecodeError, TypeError):
            dimensions = {}

    # 从 summary 中提取关键发现
    summary = row.get("summary", "")
    findings = []
    if summary:
        lines = summary.split("\n")
        for line in lines[:3]:
            line = line.strip().lstrip("-").lstrip("•").strip()
            if line:
                findings.append(line)

    brand_name = row.get("brand_name", "")
    score = row.get("score")

    return {
        "poster_data": {
            "type": "report",
            "title": f"你的品牌在 AI 搜索里表现如何？",
            "brand_name": brand_name,
            "score": score,
            "level": row.get("level", ""),
            "dimensions": dimensions,
            "findings": findings,
            "qr_hint": "扫码看完整报告",
            "qr_url": short_url,
            "qr_image_base64": _generate_qr_base64(short_url),
        },
        "share_url": short_url,
        "short_code": code,
    }


# ==================== API 端点 ====================

@router.get("/api/share/qrcode")
async def generate_qrcode(url: str, size: int = 300):
    """生成二维码 PNG 图片"""
    if not url:
        raise HTTPException(400, detail="缺少 url 参数")
    size = max(100, min(1000, size))
    png_bytes = _generate_qr_bytes(url, size)
    return Response(content=png_bytes, media_type="image/png")


@router.post("/api/share/poster")
async def generate_poster(req: PosterRequest, request: Request):
    """
    生成海报数据 JSON（含二维码 base64）
    type: register/interview/team/profile/report
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, detail="未登录")

    user_id = user.get("user_id") or current_user_id(user)
    # [白标继承] 海报是对外物料:员工出的海报也必须是团队长的品牌。
    from auth.principal_identity import resolve_branding_principal_user_id
    brand_owner_user_id = resolve_branding_principal_user_id(request, fallback_user_id=int(user_id or 0))
    origin = str(request.base_url).rstrip("/")
    # 优先用 X-Forwarded-Host（反向代理场景）
    forwarded_host = request.headers.get("X-Forwarded-Host")
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "https")
    if forwarded_host:
        origin = f"{forwarded_proto}://{forwarded_host}"

    builders = {
        "register": lambda: _build_register_poster(user, origin),
        "register_social": lambda: _build_register_social_poster(user, origin),
        "interview": lambda: _build_interview_poster(user, req.params, origin),
        "team": lambda: _build_team_poster(user, req.params, origin),
        "profile": lambda: _build_profile_poster(user, req.params, origin),
        "report": lambda: _build_report_poster(user, req.params, origin, request),  # [GEO-R1-CAN-121] 传 request 供归属校验
    }

    builder = builders.get(req.type)
    if not builder:
        raise HTTPException(400, detail=f"不支持的海报类型: {req.type}")

    result = builder()

    # 白标适配（v3.6：海报是 customer surface，走 mode/status/unlocked 授权判定，不再按 agent_level）
    _wl = _get_whitelabel(brand_owner_user_id)
    from services.public_whitelabel import public_branding_from_record
    public_branding = public_branding_from_record(_wl, surface="customer")
    result["whitelabel"] = (
        public_branding["brand"]
        if public_branding["display_scope"] == "approved_whitelabel"
        else None
    )
    result["branding_status"] = public_branding["display_scope"]

    return {"success": True, **result}


@router.get("/api/sl/{code}")
async def short_link_redirect(code: str, request: Request):
    """短链重定向 + 点击计数（/api/sl/ 避免和选词页 /api/s/ 冲突）

    Report links carry only the opaque share token. User identities never enter
    the URL, cache key, browser history, referrer, or exported QR payload.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE short_links SET click_count = click_count + 1
            WHERE code = %s RETURNING target_url, link_type
        """, (code,))
        row = cursor.fetchone()
        redirect_url = None
        if row:
            redirect_url = row["target_url"]
            if (
                row.get("link_type") == "report"
                and "/public/report/" in redirect_url
            ):
                extras = []
                if "st=" not in redirect_url:
                    extras.append(f"st={code}")
                if extras:
                    sep = "&" if "?" in redirect_url else "?"
                    redirect_url = f"{redirect_url}{sep}{'&'.join(extras)}"
                    cursor.execute(
                        "UPDATE short_links SET target_url = %s WHERE code = %s",
                        (redirect_url, code),
                    )
        conn.commit()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, detail="链接已失效 · 请联系发你链接的人重新发送")

    response = RedirectResponse(url=redirect_url or row["target_url"], status_code=302)
    if row.get("link_type") in ("register", "register_social"):
        forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").lower()
        _secure = (request.url.scheme == "https" or forwarded_proto == "https")
        # [WO_REFERRAL_CHAIN §2.2 2026-08-05] 归因有效期 1h → 7 天:扫码到注册跨天是常态。
        # URL 仍不带推荐码(隐私口径不变);cookie 里存的也只是不透明短链 code。
        _attribution_max_age = 7 * 24 * 3600
        response.set_cookie(
            "omnirank_invite_attribution",
            code,
            max_age=_attribution_max_age,
            httponly=True,
            secure=_secure,
            samesite="lax",
            path="/api/auth/register",
        )
        # [WO_REFERRAL_CHAIN §2.2] 展示用 cookie(非 HttpOnly):只存短链 code,
        # 供注册页向 /api/auth/register/attribution 查邀请人显示名。不存推荐码原文。
        response.set_cookie(
            "omnirank_invite_display",
            code,
            max_age=_attribution_max_age,
            httponly=False,
            secure=_secure,
            samesite="lax",
            path="/",
        )
    return response


# ==================== 公开报告页 API ====================

def _verify_public_report_token(diagnosis_id: int, st: Optional[str] = None) -> bool:
    """[P0-1 fix 2026-05-23 老板授权] 校验 share_token 与 diagnosis_id 绑定
    [self-review r4 2026-05-23] 老板独立 worktree 复审发现 substring 漏洞 + 闭环未成
    返 True 表示 token 匹配 / 兼容窗口允许无 token / 检查通过"""
    import os as _os
    from urllib.parse import urlparse
    # [audit P1 2026-06-10] 默认翻 strict:旧默认 "false" 且 prod 未设置该 env → no-token 直接放行,
    # 251 份诊断报告(含成本字段)可被匿名按自增 id 枚举。现 no-token 默认拒绝;
    # 分享链路均已带 st(短链 /api/sl 已回填),老裸链接失效提示"联系发你链接的人重新发送"。
    # 应急回退只接受精确字面量 ``false``。任何其他值(含空串、1、yes、拼写错误)
    # 都按严格模式处理，避免配置漂移意外打开匿名枚举入口。
    token_gate_disabled = _os.getenv("PUBLIC_REPORT_REQUIRE_TOKEN", "true") == "false"
    if not st:
        return token_gate_disabled
    # st 提供 · 必须匹配
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            # diagnosis_records.share_token 直接匹配(主路径)
            cursor.execute(
                "SELECT id FROM diagnosis_records WHERE id = %s AND share_token = %s",
                (diagnosis_id, st),
            )
            if cursor.fetchone():
                return True
            # short_links 反查 · 必须 URL path 精确匹配防 substring 漏洞
            # 旧 bug:f"/public/report/{3}" in "/public/report/335" → True · 跨报告越权
            cursor.execute(
                "SELECT target_url FROM short_links WHERE code = %s",
                (st,),
            )
            sl = cursor.fetchone()
            if sl:
                target = sl.get("target_url") or ""
                try:
                    path = urlparse(target).path
                    if path == f"/public/report/{diagnosis_id}":
                        return True
                except Exception:
                    pass
        finally:
            conn.close()
    except Exception as e:
        # bearer share_token 任何片段都不得进入日志；只保留错误类型供排障。
        logger.warning(
            "[public_report] token verification unavailable diagnosis_id=%s error_type=%s",
            diagnosis_id,
            type(e).__name__,
        )
    return False


def _optional_bearer_identity(request: Any) -> Optional[dict]:
    """公开报告端点内的**可选**身份解析 —— 带 token 就认,不带就当匿名。

    🔴 [返工单 2026-08-08 §1.4 真因 · §4.1 必做方案] 为什么解析要写在端点里:

    `auth/middleware.py:116` 对 `/api/public/` 前缀**提前放行** —— 不解析 token、
    不填 `request.state.user`。于是 `_viewer_can_calibrate` 在这条路由上恒返
    False,门户校准段在生产**永不出现**。函数本身没错(构造输入四条全对),
    错在它读的那一层在这条路由上从来没有数据。

    中间件是「不能改的核心文件」,且 `/api/public/` 前缀下挂着 19 条路由,
    给它加软鉴权是全站级改动,blast radius 远超本单 —— 所以只在这一个端点里
    解析:受限、可审。
    🔴 代价必须明说:这确实引入了**第二处 token 解析点**(第一处是中间件)。
    长期是否该在中间件统一做软鉴权,单列交 Owner,本单不擅自决定。

    三条硬约束(每条都有对应判别锁):

      ① **失败一律当匿名,绝不拒绝请求** —— 坏 token / 过期 / 畸形 Authorization
         头都只是"没有身份",HTTP 仍 200。这条路由是客户拿到的那条链接,
         给它加上任何鉴权 = 再来一次 P0(红线②)。
      ② **不自己写解析** —— 走 `auth.jwt_utils.decode_jwt`,与中间件 2.2 步
         (以及 `/api/auth/me` 背后那条链)同一个函数、同一把密钥、同一个过期判定。
      ③ **门户短 token 不算服务商身份** —— 它是客户侧凭证。认了它就等于给
         客户开了校准入口,方向正好与红线④ 相反(宁可服务商少看见,不可客户多看见)。
         这里按中间件 2.1 步同一条判据(长度 ≤20 且非 `eyJ` 开头)识别并拒认。

    返回:JWT payload(与中间件 fast-path 注入 `request.state.user` 的结构同源),
    或 None(匿名)。**本函数不做归属判定** —— 归属仍然只由 `_viewer_can_calibrate`
    用本报告自己那一行的 `brands.owner_user_id` 现场比对。
    """
    try:
        headers = getattr(request, "headers", None)
        if headers is None:
            return None
        auth_header = headers.get("Authorization") or ""
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header[7:].strip()
        if not token:
            return None
        # 门户短 token(客户侧凭证)不是服务商登录态 —— 判据与 auth/middleware.py
        # 2.1 步一致。这里只"不认",不拒绝请求。
        if len(token) <= 20 and not token.startswith("eyJ"):
            return None
        from auth.jwt_utils import decode_jwt

        payload = decode_jwt(token)
        if not isinstance(payload, dict):
            return None
        if payload.get("user_id") is None and payload.get("id") is None:
            return None
        return payload
    except Exception:  # pragma: no cover - 解析出任何岔子都只当匿名
        return None


def _viewer_can_calibrate(
    request: Any,
    *,
    brand_id: Any,
    brand_owner_user_id: Any,
) -> bool:
    """这个访问者能不能看到「待确认校准」段。

    [WO 2026-08-07 · Owner 拍板] 同一个分享链接两种视角。判据两条**都**要过:

      ① 有有效登录态(匿名 token 访客一律 False —— 客户视角零入口);
      ② **资源级归属**:这个 brand 归属这个登录用户,或者对方是 admin。

    🔴 第 ② 条是重点。「登录了就行」不算数 —— 那等于任意登录用户拿到别人的
    分享链接就能看到、甚至去改别人品牌的判定。系统缺资源级归属层是已知雷区
    (2026-08-04 IDOR 扫描:中间件三个分支没有一条做归属),这里不许复发:
    归属用**本报告自己那一行的 `brands.owner_user_id`** 现场比对,
    不查别的表、不靠调用方传进来的 id。

    任何一步取不到 → False(fail-closed:宁可服务商少看见,不可客户多看见)。
    """
    try:
        user = getattr(getattr(request, "state", None), "user", None)
        if not isinstance(user, dict):
            return False
        viewer_id = user.get("id") or user.get("user_id")
        if viewer_id is None:
            return False
        if not brand_id:
            return False
        roles = user.get("roles") or []
        is_admin = bool(user.get("is_admin")) or "admin" in {
            str(r).lower() for r in roles if r
        }
        if is_admin:
            return True
        if brand_owner_user_id is None:
            return False
        return int(viewer_id) == int(brand_owner_user_id)
    except Exception:  # pragma: no cover - 判不出来就当没权限
        return False


@router.get("/api/public/report/{diagnosis_id}")
async def get_public_report(
    diagnosis_id: int,
    include_html: int = 0,
    st: Optional[str] = None,
    request: Request = None,  # type: ignore[assignment]
):
    # 🔴 `request` 必须排在**最后**且带默认值:既有调用方(含测试)用位置参数调
    #    `get_public_report(diagnosis_id, include_html, st)` —— 把它插到第一位
    #    会把 diagnosis_id 挤走,整条走进 legacy 分支。我第一版就是这么写的,
    #    被 test_public_report_web_backend 当场抓出来(pytest 甚至因此 INTERNALERROR)。
    #    FastAPI 按**类型注解**注入 Request,与位置无关,所以放末尾不影响注入。
    """公开报告页数据 — 无需登录。

    [P0-1 fix 2026-05-23 老板授权] Codex 跨 AI 审计 · 加 share_token 校验
      · query 参数 st 提供 · 必须匹配 diagnosis_records.share_token 或 short_links 表
      · 不匹配 → 404
      · 未提供 st · 默认拒绝；仅 env 精确等于 PUBLIC_REPORT_REQUIRE_TOKEN=false 时进入应急兼容
      · 防数字 ID 枚举攻击

    CTO-B 2026-04-26 W4 · 客户版/内部版双线贯通(决策点 2 落库策略):
      · 优先读 diagnosis_records.report_v2_client_md(v2 客户版 · light 风格)
      · v2 不可用时降级 report_md_path 文件(向后兼容)
      · v2 异常对外只返回通用状态，不透传内部生成错误
      · 公开报告**只返客户版** · 不暴露内部版(代理利润提示等)
    """
    if not _verify_public_report_token(diagnosis_id, st):
        raise HTTPException(404, detail="链接已失效 · 请联系发你链接的人重新发送")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT dr.id, dr.brand_id, b.owner_user_id AS brand_owner_user_id,
                   dr.brand_name, dr.total_score, dr.level, dr.industry,
                   dr.web_search_score, dr.platform_score, dr.content_quality_score,
                   dr.authority_score, dr.brand_ownership_score,
                   dr.ai_visibility_score, dr.ai_citation_score, dr.update_frequency_score,
                   dr.report_md_path, dr.keywords, dr.created_at,
                   dr.report_v2_version, dr.report_v2_client_md,
                   dr.report_v2_modules_jsonb, dr.report_v2_generated_at,
                   dr.data_completeness_score, dr.data_completeness_breakdown,
                   dr.result_visibility
            FROM diagnosis_records dr
            LEFT JOIN brands b ON b.id = dr.brand_id
            WHERE dr.id = %s
        """, (diagnosis_id,))
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, detail="链接已失效 · 请联系发你链接的人重新发送")

    row = dict(row)

    # [WORKERS=4 · SPEC §3.6 P0-3] 结果可见性严闸(公开客户面 · "已退款仍白拿结果"机械阻断):
    #   NULL/published → 放行(旧数据 NULL 视 published 向后兼容);pending(生成中)→ 不下发正式结果;
    #   withheld(结算退款/中断)→ 404 不可用。公开面无登录态 → 无 owner-exempt(owner 走内部鉴权端点)。
    _vis = row.get("result_visibility")
    if _vis == "withheld":
        raise HTTPException(404, detail="该报告当前不可用 · 请联系发你链接的人")
    if _vis == "pending":
        return {"status": "pending", "pending": True,
                "message": "报告正在生成中,请稍后刷新查看"}

    modules_jsonb = None
    if row.get("report_v2_version") == "v2":
        modules_jsonb = row.get("report_v2_modules_jsonb")
        if isinstance(modules_jsonb, str):
            try:
                modules_jsonb = json.loads(modules_jsonb)
            except Exception:
                modules_jsonb = {}
        from services.report_html_renderer import is_client_report_ready

        if not is_client_report_ready(modules_jsonb):
            # Do not expose score, level, branding or other partial report data
            # until the explicit customer artifact exists.
            return _public_report_not_ready_payload()

    # CTO-B W4 · 优先 v2 客户版 · 决策点 2 落库 + 失效后重算
    content = ""
    report_version = "v1"
    v2_client_md = row.get("report_v2_client_md")
    if row.get("report_v2_version") == "v2":
        report_version = "v2"
        if isinstance(v2_client_md, str) and v2_client_md.strip():
            content = v2_client_md
    else:
        # Only true V1 reports may read the legacy markdown artifact.  A V2
        # record with a missing customer edition must not fall back to a file
        # that may contain the internal/legacy report.
        md_path = row.get("report_md_path")
        if md_path:
            import os
            for base in [".", "/app"]:
                full = os.path.join(base, md_path)
                if os.path.exists(full):
                    try:
                        with open(full, "r", encoding="utf-8") as f:
                            content = f.read()
                    except Exception:
                        pass
                    break

    keywords = row.get("keywords", "")
    kw_count = len([k for k in keywords.split(",") if k.strip()]) if keywords else 0

    # HTML 版报告（~2MB）— 仅 include_html=1 时加载，避免默认 response 过大导致白屏
    html_content = ""
    has_html = False
    import os
    # V2 public reports are rendered server-side through the canonical web route.
    # Never return a stale on-disk legacy HTML snapshot inside the public DTO.
    if report_version != "v2":
        for base in [".", "/app"]:
            html_path = os.path.join(base, "output", "reports", f"{diagnosis_id}_report.html")
            if os.path.exists(html_path):
                has_html = True
                if include_html:
                    try:
                        with open(html_path, "r", encoding="utf-8") as f:
                            html_content = f.read()
                    except Exception:
                        pass
                break

    # 完整度 + 决策点 5 异常透传
    completeness_score = row.get("data_completeness_score")
    completeness_breakdown = row.get("data_completeness_breakdown")
    if isinstance(completeness_breakdown, str):
        try:
            import json as _json
            completeness_breakdown = _json.loads(completeness_breakdown)
        except Exception:
            completeness_breakdown = None

    # P1-4 SSOT (2026-04-27 fix/integration-r3-browser-p1):
    # 公开视角与 /api/diagnosis/{id} 内部视角必须读同一份 GEO 总分 + 等级。
    # V2 只取漏斗 3 层 SSOT；V1 才取旧 total_score 列。无效数据 fail-closed。
    from services.report_v2_score import resolve_canonical_score

    canonical = resolve_canonical_score(row)

    # 🔴 [返工单 2026-08-08 §4.1] **接线**:把中间件在这条路由上没填的
    #    `request.state.user` 在端点内补上。`_viewer_can_calibrate` 的判据
    #    一个字不动(§0 已实证四条构造输入全对),只是让它读的那一层
    #    在生产上真的有数据。匿名 / 坏 token / 门户短 token 仍然是"没有身份"。
    _viewer_identity = _optional_bearer_identity(request)
    if _viewer_identity is not None:
        try:
            request.state.user = _viewer_identity
        except Exception:  # pragma: no cover - state 写不进去就当匿名,绝不因此报错
            pass

    presentation = None
    if report_version == "v2":
        from services.public_report_presentation import build_public_report_presentation

        presentation = build_public_report_presentation(
            modules_jsonb,
            industry=row.get("industry"),
            canonical_score=canonical.get("total_score"),
            generated_at=row.get("report_v2_generated_at"),
            keyword_count=kw_count,
            brand_name=row.get("brand_name"),
            viewer_can_calibrate=_viewer_can_calibrate(
                request,
                brand_id=row.get("brand_id"),
                brand_owner_user_id=row.get("brand_owner_user_id"),
            ),
        )

    # [audit #13 2026-06-10] 匿名面不下发内部 user_id / brand_id —— 防攻击者枚举 report/{id} 拿到
    #   brand_owner_user_id 后再调 /api/public/whitelabel/{uid} 串联出"哪个品牌属于哪个代理"+ 代理画像。
    #   改为服务端内联解析 customer-surface 白标(已 mode-gate + 联系方式脱敏),前端直接读 report.whitelabel,
    #   无需按 user_id 二次调白标。SQL 仍取 brand_owner_user_id 供服务端解析,只是不再放进对外响应。
    _wl = {"whitelabel": None, "display_scope": "platform"}
    try:
        from services.public_whitelabel import get_public_whitelabel_data
        _wl = get_public_whitelabel_data(
            brand_owner_user_id=row.get("brand_owner_user_id"),
            brand_id=row.get("brand_id"),
        )
    except Exception as _wl_err:
        logger.debug(f"[public/report] whitelabel 内联解析跳过 id={diagnosis_id}: {_wl_err}")

    return {
        "status": "success",
        "report": {
            "whitelabel": _wl.get("whitelabel"),
            "branding_status": _wl.get("display_scope", "platform"),
            "brand_name": row["brand_name"],
            "score": canonical.get("total_score"),
            "level": canonical.get("level"),
            "content": content,
            "html_content": html_content,
            "has_html": has_html,
            "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
            "keyword_count": kw_count,
            # CTO-B W4 · v2 字段(前端 SharedReport / M3 报告页 用)
            "report_version": report_version,
            "audience": "client",  # 公开页只返客户版
            "data_completeness_score": completeness_score,
            "data_completeness_breakdown": completeness_breakdown,
            "report_v2_generated_at": (
                row["report_v2_generated_at"].isoformat()
                if row.get("report_v2_generated_at") and hasattr(row["report_v2_generated_at"], "isoformat")
                else (str(row["report_v2_generated_at"]) if row.get("report_v2_generated_at") else None)
            ),
            "presentation": presentation,
        },
    }


# CTO-B 2026-04-26 W5 · 公开 HTML 版咨询报告(决策点 3:网页 + PDF 共用)
# /public/report/:id 前端检测 v2 时拉这个 endpoint · 直接 inject HTML
# 老板验收第 4/5 条:客户版公开报告是 light 专业报告 · 不是 markdown 长文
@router.get("/api/public/report/{diagnosis_id}/v2.html")
async def get_public_report_v2_html(
    diagnosis_id: int,
    request: Request,
    theme: str = "light_corporate",
    st: Optional[str] = None,
):
    """公开 v2 客户版咨询报告 HTML(无需登录)

    [P0-1 fix 2026-05-23 老板授权] Codex 跨 AI 审计 · 加 share_token 校验(同 GET /api/public/report)

    决策点 4:light 默认
    决策点 5:v2 不可用时 404 · 前端降级 markdown 渲染(老路径)

    Branding is resolved server-side from the report object and an approved
    configuration. No account identity is accepted from the public URL.
    """
    from fastapi.responses import HTMLResponse

    if not _verify_public_report_token(diagnosis_id, st):
        raise HTTPException(404, detail="链接已失效 · 请联系发你链接的人重新发送")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT dr.id, dr.brand_id, b.owner_user_id AS brand_owner_user_id,
                   dr.brand_name, dr.total_score, dr.level, dr.industry,
                   dr.created_at,
                   dr.report_v2_version, dr.report_v2_modules_jsonb,
                   dr.data_completeness_score, dr.data_completeness_breakdown,
                   dr.raw_data_json, dr.result_visibility
            FROM diagnosis_records dr
            LEFT JOIN brands b ON b.id = dr.brand_id
            WHERE dr.id = %s
        """, (diagnosis_id,))
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, detail="链接已失效 · 请联系发你链接的人重新发送")
    row = dict(row)
    # [WORKERS=4 · SPEC §3.6 P0-3] 结果可见性严闸(公开客户面 HTML):pending/withheld 不下发正式结果
    _vis = row.get("result_visibility")
    if _vis in ("pending", "withheld"):
        raise HTTPException(404, detail="报告正在生成中或当前不可用 · 请稍后刷新或联系发你链接的人")
    if row.get("report_v2_version") != "v2":
        # 决策点 5:不静默降级 · 让前端走 markdown 路径
        raise HTTPException(404, detail="报告正在生成中 · 请稍后刷新查看")

    modules_jsonb = row.get("report_v2_modules_jsonb")
    if isinstance(modules_jsonb, str):
        try:
            modules_jsonb = json.loads(modules_jsonb)
        except Exception:
            modules_jsonb = {}

    # The customer report is a separately generated artifact.  Validate it
    # before raw appendix augmentation, which may otherwise create an empty
    # client.modules container and hide the fact that only internal data exists.
    from services.report_html_renderer import (
        is_client_report_ready,
        render_client_report_not_ready_html,
    )
    if not is_client_report_ready(modules_jsonb):

        html = render_client_report_not_ready_html({"brand_name": row.get("brand_name")})
        return HTMLResponse(content=html, status_code=503)

    cb = row.get("data_completeness_breakdown")
    if isinstance(cb, str):
        try:
            cb = json.loads(cb)
        except Exception:
            cb = {}
    completeness = {
        "score": row.get("data_completeness_score"),
        "level": (cb or {}).get("level", ""),
        "groups": (cb or {}).get("groups", []),
        "impact_notes": (cb or {}).get("impact_notes", []),
        "missing_summary": (cb or {}).get("missing_summary", ""),
    }

    raw_data = row.get("raw_data_json")
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except Exception:
            raw_data = {}

    # P0 · 老 v2 记录可能是在"原始 AI 问答附录"上线前生成的,modules_jsonb 里没有 3_raw。
    # 公开 HTML 版仍可从 raw_data_json.data.ai_visibility 反查并补上,避免客户展开原文时看不到完整证据。
    try:
        raw_ai = (
            ((raw_data or {}).get("data") or {}).get("ai_visibility")
            or (raw_data or {}).get("ai_visibility")
            or (raw_data or {}).get("ai_visibility_data")
        )
        if raw_ai and isinstance(modules_jsonb, dict):
            from services.report_writer_v2 import build_module_3_raw_ai_appendix
            raw_module = build_module_3_raw_ai_appendix({
                "diagnosis_data": {"ai_visibility_data": raw_ai}
            })
            for side_key in ("client", "internal"):
                side_payload = modules_jsonb.setdefault(side_key, {})
                modules_payload = side_payload.setdefault("modules", {})
                existing_raw = modules_payload.get("3_raw") or {}
                if not existing_raw.get("tests"):
                    modules_payload["3_raw"] = raw_module
    except Exception:
        # 附录补充失败不能阻断公开报告;主报告照常展示。
        pass

    from services.report_v2_score import build_canonical_report_meta

    meta = build_canonical_report_meta(row, raw_data=raw_data)

    # v3.6（修 Codex T4b P1）：白标（customer surface 授权）提到分支选择前，所有分支共用。
    # 授权白标 → 强制可白标的 customer_decision 分支（禁 report_v3 / legacy，它们尚未支持白标 · 阶段1补），
    # 避免授权代理的客户报告在 v3/legacy 分支泄漏 OmniRank。
    whitelabel_payload = None
    try:
        from services.public_whitelabel import get_public_whitelabel_data
        _public_branding = get_public_whitelabel_data(
            brand_owner_user_id=row.get("brand_owner_user_id"),
            brand_id=row.get("brand_id"),
        )
        if _public_branding.get("display_scope") == "approved_whitelabel":
            whitelabel_payload = _public_branding.get("whitelabel")
    except Exception as _wl_err:
        logger.warning(
            "[whitelabel] report branding unavailable diagnosis_id=%s error_type=%s",
            diagnosis_id,
            type(_wl_err).__name__,
        )

    def _render_customer_decision_html():
        from services.report_html_renderer import render_customer_decision_page_html
        return render_customer_decision_page_html(
            meta=meta,
            modules_jsonb=modules_jsonb or {},
            completeness=completeness,
            audience="client",
            whitelabel=whitelabel_payload,
        )

    def _render_legacy_html():
        from services.report_html_renderer import render_report_html
        return render_report_html(
            meta=meta,
            modules_jsonb=modules_jsonb or {},
            completeness=completeness,
            audience="client",
            branding=whitelabel_payload,
        )

    # 🔴 [WO_PUBLIC_PREFIX_SOFT_AUTH §5.3 · 甲案 2026-08-08] **接线**:这条路由在
    #    `auth/middleware.py` 的 `PUBLIC_PREFIXES`(`/api/public/`)下,中间件命中就提前
    #    return、压根不填 `request.state.user` → 下游 `is_admin_preview_request` 读它恒 False
    #    → `?preview_v3=1` 从上线起一次都没生效过。
    #
    #    2026-08-08 生产实测(QA admin 112 · diagnosis 536)证伪确认:匿名与 admin+preview_v3=1
    #    四条响应**逐字节同 hash**、且都不含 v3 渲染器特征串 `report-v3-section`
    #    —— 排除了"v3 已全局开启所以都一样"这个混淆解释。
    #
    #    修法与上面 `get_public_report` 第 1015-1024 行**逐字同形**(第 15 班车已在生产验证):
    #    `is_admin_preview_request` 的判据一个字不改(它是对的),只补它读的那一层。
    #    🔴 fail-open 铁律:客户面门户是 token-only 不登录的,身份解析失败一律当匿名,
    #       绝不引入 401 —— `_optional_bearer_identity` 内部对无头/坏 token/门户短 token/
    #       畸形头四种输入全部 `return None`,不抛异常。
    _viewer_identity = _optional_bearer_identity(request)
    if _viewer_identity is not None:
        try:
            request.state.user = _viewer_identity
        except Exception:  # pragma: no cover - state 写不进去就当匿名,绝不因此报错
            pass

    should_v3 = False
    force_legacy_rollback = False
    try:
        from config.settings_manager import load_settings
        from services.report_v3_gating import (
            is_admin_preview_request,
            resolve_report_v3_subject_user_id,
            should_render_report_v3,
        )

        settings = load_settings()
        force_legacy_rollback = bool(
            getattr(settings, "public_report_legacy_rollback_enabled", False)
        )
        subject_uid = resolve_report_v3_subject_user_id(
            request=request,
            diagnosis_record=row,
            shared_by=None,
        )
        should_v3 = should_render_report_v3(
            settings,
            subject_uid,
            is_admin_preview_request(request),
        )
    except Exception as gate_error:
        # Fail closed to the web-native V2 renderer. Missing/broken config is
        # never authority to activate the legacy emergency rollback path.
        logger.warning(
            "[public_report] renderer gate unavailable diagnosis_id=%s error_type=%s",
            diagnosis_id,
            type(gate_error).__name__,
        )

    branch = _select_customer_report_branch(
        whitelabel_payload is not None,
        should_v3,
        force_legacy_rollback,
    )
    try:
        if branch == "v3":
            from services.report_html_renderer_v3 import render_report_v3_html
            from services.public_whitelabel import resolve_branding_context

            # v3.6 白标:customer surface 解析 branding(授权→代理品牌 / 未授权→平台默认)·
            # Customer branding is derived from this report's brand only.
            _v3_ctx = resolve_branding_context(
                surface="customer",
                owner_user_id=row.get("brand_owner_user_id"),
                brand_id=row.get("brand_id"),
            )
            # [P0 Codex 2026-06-06] 无有效白标(platform_default)→ 传 None,绝不把平台默认 dict
            #   喂 render_report_v3_html(否则封面 eyebrow 露 OmniRank · 全域上榜)。
            _v3_branding = _v3_ctx.get("brand") if _v3_ctx.get("source") != "platform_default" else None
            html = render_report_v3_html(
                meta=meta,
                modules_jsonb=modules_jsonb or {},
                narrative=None,
                theme=theme,
                as_pdf=False,
                branding=_v3_branding,
            )
        elif branch == "customer_decision":
            html = _render_customer_decision_html()
        else:
            html = _render_legacy_html()
    except HTTPException:
        raise
    except Exception as render_error:
        logger.warning(
            "[public_report] renderer failed diagnosis_id=%s branch=%s error_type=%s",
            diagnosis_id,
            branch,
            type(render_error).__name__,
        )
        # V3 and explicit legacy rollback may safely fall back to the default
        # customer-decision renderer. A customer-decision failure returns a
        # clear privacy-safe error; it never silently revives legacy.
        if branch != "customer_decision":
            try:
                html = _render_customer_decision_html()
            except Exception as fallback_error:
                logger.warning(
                    "[public_report] customer-decision fallback failed "
                    "diagnosis_id=%s error_type=%s",
                    diagnosis_id,
                    type(fallback_error).__name__,
                )
                html = _render_whitelabel_error_page(whitelabel_payload)
                return HTMLResponse(content=html, status_code=503)
        else:
            html = _render_whitelabel_error_page(whitelabel_payload)
            return HTMLResponse(content=html, status_code=503)
    return HTMLResponse(content=html, status_code=200)


@router.post("/api/public/report/{diagnosis_id}/lead")
async def submit_report_lead(diagnosis_id: int, request: Request, st: Optional[str] = None):
    """[2026-06-01 玩法B 防穿帮 · 老板拍板] 客户面报告留资入口已下线 —— 报告 = 纯交付物 · 不挂回连漏斗。

    前端入口(cdv2 lead form + React CTA)已全部移除;本端点保留为 410,拦截旧缓存页/直接请求,
    不再写 report_leads / 不再通知分享者。历史:原"访客提交联系方式(线索)" → report_leads + notify。
    若未来产品要恢复留资,见 git 历史 commit 复原。
    """
    raise HTTPException(status_code=410, detail="此入口已下线 · 如需沟通请直接联系发您这份报告的人")


# ==================== 公开白标 + 操作通知 API ====================

@router.get("/api/public/whitelabel")
async def get_public_whitelabel(quote_id: int = None, surface: str = "customer"):
    """Return an approved brand for an opaque business object, never a user id.

    板块 C（Owner 2026-07-22 裁决 D1/D2 · 合同 §4.2-C3）：服务端写死 surface gate。
    本公开端点只服务 customer surface；调用方传 surface=agent/admin（或任何非法值）
    → 一律返回平台默认、不下发 whitelabel 字段（内部后台品牌判定只能走带鉴权的
    /api/referral/whitelabel，禁止经匿名端点取回代理品牌）。
    """
    if surface != "customer":
        from services.public_whitelabel import public_branding_from_record
        return {
            "status": "success",
            **public_branding_from_record(None, surface="customer"),
            "whitelabel": None,
        }
    try:
        payload = get_public_whitelabel_data(quote_id=quote_id) if quote_id else None
        if not payload:
            from services.public_whitelabel import public_branding_from_record
            payload = {**public_branding_from_record(None, surface="customer"), "whitelabel": None}
        return {"status": "success", **payload}
    except Exception:
        from services.public_whitelabel import public_branding_from_record
        return {
            "status": "success",
            **public_branding_from_record(None, surface="customer"),
            "whitelabel": None,
        }


@router.post("/api/public/report/{diagnosis_id}/action")
async def submit_report_action(diagnosis_id: int, request: Request, st: Optional[str] = None):
    """[2026-06-01 玩法B 防穿帮 · 老板拍板] 客户面报告操作按钮(留资/通知分享者)入口已下线 —— 报告 = 纯交付物 · 不挂回连漏斗。

    前端 React CTA(对这份报告感兴趣 / 预约顾问 / 帮我做方案)已移除;本端点保留为 410,拦截旧缓存页/直接请求,
    不再写 report_leads / 不再通知分享者。历史:原"客户点击操作 → report_leads + notify"。
    """
    raise HTTPException(status_code=410, detail="此入口已下线 · 如需沟通请直接联系发您这份报告的人")


@router.get("/api/public/creator-card/{share_code}")
async def get_public_creator_card(share_code: str):
    """公开创作者画像卡（不需登录）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, industry, personality_profile, persona_positioning
            FROM client_profiles
            WHERE share_code = %s AND (is_deleted = 0 OR is_deleted IS NULL)
        """, (share_code,))
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        return {"status": "error", "error": "链接已失效 · 请联系发你链接的人重新发送"}

    pp = {}
    if row.get("personality_profile"):
        try:
            pp = json.loads(row["personality_profile"]) if isinstance(row["personality_profile"], str) else row["personality_profile"]
        except Exception:
            pp = {}

    return {
        "status": "success",
        "card": {
            "name": row["name"],
            "industry": row.get("industry", ""),
            "positioning": row.get("persona_positioning", ""),
            "creator_dna": pp.get("creator_dna"),
            "radar": pp.get("radar"),
            "expression_type": pp.get("expression_type"),
            "soul_tags": pp.get("soul_tags", []),
            "ip_declaration": pp.get("ip_declaration", ""),
            "creator_type": pp.get("creator_dna", {}).get("archetype") or pp.get("creator_type", ""),
        },
        "share_code": share_code,
    }
