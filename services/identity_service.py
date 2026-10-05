"""
V3.3.1 三层身份(L0/L1/L2)+ 邀请码验证

L0 社媒 C 端:自助手机号注册 · 仅社媒 · 看不到 GEO
L1 GEO 用户:必须 user 邀请码注册 · GEO + 社媒 · 看不到服务费 / 提现
L2 GEO 代理:必须 agent 邀请码注册 · GEO + 社媒 + 完整 CRM/白标 + 服务费

判定:
- L2:user_wallets.agent_level >= 2
- L1:agent_level >= 1 AND agent_level < 2
- L0:agent_level = 0(或字段缺失)

邀请码:
- code_type='user'  → 注册后 L1
- code_type='agent' → 注册后 L2
- expires_at 过期 + is_active=true 才可用

L1 月度配额:10 张/月(referral_code_quota 表)

L2 升级路径(Q5):
- A. v1.1 审核制(身份证 + 协议 + AI+人工 1-3 工作日)· 推荐
- 通过 PARTNER_APPLY_ENABLED feature flag 切换

关联:
- 决策书 §1 §2 §5
- IDENTITY_DECISIONS_LOCK Q1-Q5
- RED_LINES R7
"""

import logging
import secrets
import string
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from db.connection import get_db
from config.v3_3_1_flags import (
    is_v3_3_1_enabled,
    is_invite_code_required,
    get_l1_monthly_invite_quota,
)

logger = logging.getLogger("GEO-V3.3.1-Identity")


# ============================================
# 身份判定
# ============================================

def classify_user(user_id: int) -> str:
    """返回 'L0' / 'L1' / 'L2'"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(agent_level, 0) AS lv FROM user_wallets WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                return "L0"
            lv = int((row["lv"] if isinstance(row, dict) else row[0]) or 0)
            if lv >= 2:
                return "L2"
            if lv >= 1:
                return "L1"
            return "L0"


def is_l0(user_id: int) -> bool:
    return classify_user(user_id) == "L0"


def is_l1(user_id: int) -> bool:
    return classify_user(user_id) == "L1"


def is_l2(user_id: int) -> bool:
    return classify_user(user_id) == "L2"


def is_geo_eligible(user_id: int) -> bool:
    """L1/L2 可见 GEO · L0 不可见"""
    return classify_user(user_id) in ("L1", "L2")


# ============================================
# 邀请码
# ============================================

class InviteCodeError(Exception):
    code = "invite_code_error"
    http_status = 400


class InviteCodeNotFoundError(InviteCodeError):
    code = "invite_code_not_found"
    http_status = 404


class InviteCodeExpiredError(InviteCodeError):
    code = "invite_code_expired"


class InviteCodeRevokedError(InviteCodeError):
    code = "invite_code_revoked"


class InviteCodeQuotaExceededError(InviteCodeError):
    code = "invite_code_quota_exceeded"
    http_status = 429


class InviteCodeAlreadyUsedError(InviteCodeError):
    """邀请码已被消费过(并发或重复扫码)· Codex 四审 P1-1 区分:已用不能 fallback 老码"""
    code = "invite_code_already_used"
    http_status = 409


def _gen_code(prefix: str = "U", length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return prefix + "".join(secrets.choice(alphabet) for _ in range(length))


def issue_invite_code(
    user_id: int,
    *,
    code_type: str = "user",
    ttl_days: int = 30,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """签发邀请码

    code_type:
      - 'user'  → 邀请的下游成为 L1
      - 'agent' → 邀请的下游成为 L2(仅 L2 自己可签发 agent 码)
    """
    if not is_v3_3_1_enabled():
        raise InviteCodeError("V3.3.1 未启用")

    issuer_level = classify_user(user_id)
    if code_type == "agent" and issuer_level != "L2":
        raise InviteCodeError("仅 L2 代理可签发 agent 邀请码")
    if code_type == "user" and issuer_level == "L0":
        raise InviteCodeError("L0 不可签发邀请码 · 请先升 L1")
    if code_type not in ("user", "agent"):
        raise InviteCodeError(f"未知 code_type: {code_type}")

    # L1 月度配额
    if issuer_level == "L1":
        yyyymm = int(datetime.now().strftime("%Y%m"))
        limit = get_l1_monthly_invite_quota()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO referral_code_quota (user_id, yyyymm, codes_issued, monthly_limit)
                    VALUES (%s, %s, 0, %s)
                    ON CONFLICT (user_id, yyyymm) DO NOTHING
                    """,
                    (user_id, yyyymm, limit),
                )
                cur.execute(
                    "SELECT codes_issued, monthly_limit FROM referral_code_quota WHERE user_id=%s AND yyyymm=%s FOR UPDATE",
                    (user_id, yyyymm),
                )
                row = cur.fetchone()
                issued = int(row["codes_issued"] if isinstance(row, dict) else row[0] or 0)
                monthly_limit = int(row["monthly_limit"] if isinstance(row, dict) else row[1] or limit)
                if issued >= monthly_limit:
                    raise InviteCodeQuotaExceededError(
                        f"L1 月度配额已用尽({issued}/{monthly_limit})· 请升 L2"
                    )

    code = _gen_code("A" if code_type == "agent" else "U", 8)
    expires_at = datetime.now() + timedelta(days=ttl_days)

    with get_db() as conn:
        with conn.cursor() as cur:
            # V3.3.1 新表 invite_codes(有 id BIGSERIAL · 一用户多码)· 老 referral_codes 表保留不动
            cur.execute(
                """
                INSERT INTO invite_codes (user_id, code, code_type, expires_at, is_active, note, created_at)
                VALUES (%s, %s, %s, %s, true, %s, NOW())
                ON CONFLICT (code) DO NOTHING
                RETURNING id
                """,
                (user_id, code, code_type, expires_at, note),
            )
            row = cur.fetchone()
            if not row:
                # 极小概率撞码 · 重试一次
                code = _gen_code("A" if code_type == "agent" else "U", 10)
                cur.execute(
                    """
                    INSERT INTO invite_codes (user_id, code, code_type, expires_at, is_active, note, created_at)
                    VALUES (%s, %s, %s, %s, true, %s, NOW())
                    RETURNING id
                    """,
                    (user_id, code, code_type, expires_at, note),
                )
                row = cur.fetchone()
            cid = row["id"] if isinstance(row, dict) else row[0]

            if issuer_level == "L1":
                cur.execute(
                    "UPDATE referral_code_quota SET codes_issued = codes_issued + 1 WHERE user_id=%s AND yyyymm=%s",
                    (user_id, int(datetime.now().strftime("%Y%m"))),
                )
            conn.commit()

    return {
        "code_id": cid,
        "code": code,
        "code_type": code_type,
        "expires_at": expires_at.isoformat(),
        "issuer_user_id": user_id,
        "issuer_level": issuer_level,
        "note": note,
    }


def verify_invite_code(code: str) -> Dict[str, Any]:
    """校验邀请码 · 返回 issuer + code_type

    Raises:
        InviteCodeNotFoundError / InviteCodeExpiredError / InviteCodeRevokedError
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, code, code_type, expires_at, is_active, revoked_at, revoked_reason
                  FROM invite_codes
                 WHERE code = %s
                 LIMIT 1
                """,
                (code,),
            )
            row = cur.fetchone()
            if not row:
                raise InviteCodeNotFoundError(f"邀请码 {code} 不存在")
            d = dict(row) if isinstance(row, dict) else {
                "id": row[0], "user_id": row[1], "code": row[2], "code_type": row[3],
                "expires_at": row[4], "is_active": row[5], "revoked_at": row[6],
                "revoked_reason": row[7],
            }
            if not d.get("is_active"):
                raise InviteCodeRevokedError(
                    f"邀请码已撤销:{d.get('revoked_reason') or '未知'}"
                )
            if d.get("expires_at") and d["expires_at"] < datetime.now():
                raise InviteCodeExpiredError(f"邀请码已过期({d['expires_at']})")
            return {
                "code_id": d["id"],
                "issuer_user_id": d["user_id"],
                "code_type": d.get("code_type") or "user",
                "code": d["code"],
            }


def consume_invite_code(code: str, invitee_user_id: int) -> Dict[str, Any]:
    """注册时消费邀请码并记录不可变邀请来源。

    V3.3.1 修复:复用已存在的 referral_links 表(老 V3.2 表 · 字段 referrer_id/referred_id/level/commission_rate)
    默认保持 invitee 为普通客户；仅旧的显式回滚开关可恢复扫码升级身份。
    """
    info = verify_invite_code(code)
    code_type = info["code_type"]
    # 注册不自动升级被邀请人的 agent_level。referral_links 始终保留来源事实；
    # 合格服务商邀请还会为普通客户建立首次商业归属，服务商账号另走渠道关系。
    #   成为服务商唯一途径 = agent_applications 审核制(不允许单靠扫码自动升级·含 code_type='agent')
    #   全局 kill-switch:AGENT_AUTO_UPGRADE_L0_ENABLED=true 回滚恢复扫码自动升级(C2·默认 false·现注册都是自己人)
    import os as _os
    _auto_upgrade_l0 = _os.getenv("AGENT_AUTO_UPGRADE_L0_ENABLED", "false").lower() == "true"
    assigned_level = (2 if code_type == "agent" else 1) if _auto_upgrade_l0 else 0
    issuer_id = info["issuer_user_id"]
    code_id = info["code_id"]

    with get_db() as conn:
        with conn.cursor() as cur:
            # 标记 invite_codes 已消费(防同一码多次使用)· UPDATE FROM is_active=true 防并发
            cur.execute(
                """
                UPDATE invite_codes
                   SET used_by_user_id = %s, used_at = NOW(), is_active = false
                 WHERE id = %s AND is_active = true AND used_by_user_id IS NULL
                 RETURNING id
                """,
                (invitee_user_id, code_id),
            )
            if not cur.fetchone():
                # 并发场景:其他线程已消费此码 / 已被注册过
                # Codex 四审 P1-1:用专门异常类 · 不能 fallback 老码绕风控
                conn.rollback()
                raise InviteCodeAlreadyUsedError("邀请码已被使用 · 并发冲突或重复扫码")

            # 写 referral_links level=1 直推关系(防重复 · PK 是 (referrer_id, referred_id))
            try:
                cur.execute(
                    """
                    INSERT INTO referral_links (referrer_id, referred_id, level, commission_rate, created_at)
                    VALUES (%s, %s, 1, %s, NOW())
                    ON CONFLICT (referrer_id, referred_id) DO NOTHING
                    """,
                    (issuer_id, invitee_user_id,
                     0.15 if code_type == "user" else 0.18),
                )
            except Exception as exc:
                logger.warning("consume_invite_code: referral_links insert failed · %s", exc)

            # [D4] 确保 invitee 钱包存在 · agent_level 默认 0(客户·见上 assigned_level)
            #   GREATEST 防降级已实名服务商(重复消费场景);新注册客户 assigned_level=0 → 保持 L0 随上级系数
            cur.execute(
                """
                INSERT INTO user_wallets (user_id, agent_level, agent_tier)
                VALUES (%s, %s, 'standard')
                ON CONFLICT (user_id) DO UPDATE
                  SET agent_level = GREATEST(COALESCE(user_wallets.agent_level, 0), EXCLUDED.agent_level)
                """,
                (invitee_user_id, assigned_level),
            )

            # 商业归属只能消费已落库的权威业务身份。先建钱包再判断，避免
            # 新注册用户因为 user_wallets 尚不存在而被误判或整笔事务失败。
            from services.commercial_service_routing import solidify_service_provider_invitation

            commercial_result = solidify_service_provider_invitation(
                cur, int(invitee_user_id), int(issuer_id), str(code)
            )
            conn.commit()

    return {
        "invitee_user_id": invitee_user_id,
        "inviter_user_id": issuer_id,
        "code_type": code_type,
        "assigned_level": assigned_level,
        "commercial_binding_action": commercial_result["action"],
    }


def is_invite_code_required_for_signup(intent: str) -> bool:
    """注册时是否必须邀请码

    intent:
      - 'social' (L0 社媒 C 端)→ False(自助)
      - 'geo'    (L1/L2 GEO)  → True · 但 feature flag 关时退化为 False
    """
    if intent == "social":
        return False
    if not is_invite_code_required():
        return False
    return True
